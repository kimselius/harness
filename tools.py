"""The tool set: shell, read_file, write_file, edit_file.

Every tool returns a plain string for the model. Raise ToolError for failures the
model should see and recover from. Tools return the *full* text; truncation happens
in the agent loop so the log can keep the untruncated version.
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from typing import Any, Callable

from config import Config
from util import ToolError, ToolInterrupted, display_path, resolve_in_workspace

MAX_READ_BYTES = 20 * 1024 * 1024

# Appended to every shell command so the final working directory can be read back
# and carried into the next call, the way a real shell session behaves.
CWD_MARKER = "__harness_cwd_6b1f__:"
# The leading newline matters: without it the marker is glued to the last line of
# output whenever a command does not end with one (`printf` without \n).
CWD_PROBE = (
    f'\n__harness_rc=$?\nprintf "\\n%s%s\\n" "{CWD_MARKER}" "$(pwd)"\n'
    "exit $__harness_rc\n"
)

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "shell",
            "description": (
                "Run a shell command (/bin/sh -c). Returns the exit code, the current "
                "directory, and combined stdout/stderr. Use it for listing, searching, "
                "git, running tests, installing packages, and anything else system "
                "level. The working directory persists between calls: `cd sub` in one "
                "call applies to every later call. It starts at the workspace root "
                "and cannot leave it. Note that read_file, write_file and edit_file "
                "ignore it: their paths are always relative to the workspace root, "
                "the same way every tool result prints them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Command line to execute.",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Optional timeout in seconds for this command.",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a UTF-8 text file from the workspace. Output is line numbered. "
                "Use offset/limit to page through large files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "File path, always relative to the workspace root and "
                            "never to the shell's current directory. It may not "
                            "escape the workspace root."
                        ),
                    },
                    "offset": {
                        "type": "integer",
                        "description": "1-based first line to read (default 1).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of lines to read.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Create a file or overwrite it completely. Parent directories are "
                "created automatically. For changing part of an existing file, prefer "
                "edit_file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "File path, always relative to the workspace root and "
                            "never to the shell's current directory. It may not "
                            "escape the workspace root."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": "Full file contents to write.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Find and replace an exact substring in a file. old_string must occur "
                "exactly once unless replace_all is true; include surrounding context "
                "to make it unique. Whitespace and indentation must match exactly."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "File path, always relative to the workspace root and "
                            "never to the shell's current directory. It may not "
                            "escape the workspace root."
                        ),
                    },
                    "old_string": {
                        "type": "string",
                        "description": "Exact text to find.",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "Replacement text (empty string deletes).",
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "Replace every occurrence (default false).",
                    },
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
]


def _require_str(args: dict, key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str):
        raise ToolError(f"argument {key!r} must be a string")
    return value


def _read_text(path: Path, cfg: Config | None = None) -> str:
    if not path.exists():
        # State the resolved path. The rule that produced it is in the per-request
        # state block, so the model never has to infer it from an error message.
        where = f"{display_path(cfg.workspace, path)} (resolved to {path})" if cfg else path
        raise ToolError(f"no such file: {where}")
    if path.is_dir():
        raise ToolError(f"{path} is a directory, not a file")
    size = path.stat().st_size
    if size > MAX_READ_BYTES:
        raise ToolError(f"file is {size} bytes, too large to read (limit {MAX_READ_BYTES})")
    data = path.read_bytes()
    if b"\x00" in data[:8192]:
        raise ToolError(f"{path} looks like a binary file; use shell tools instead")
    return data.decode("utf-8", errors="replace")


def _kill_group(proc: subprocess.Popen) -> None:
    """Kill the command and everything it spawned."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()


def tool_shell(args: dict, cfg: Config) -> str:
    command = _require_str(args, "command")
    requested = args.get("timeout")
    timeout = cfg.shell_timeout
    if isinstance(requested, (int, float)) and requested > 0:
        timeout = min(float(requested), cfg.max_shell_timeout)

    proc = subprocess.Popen(
        command + CWD_PROBE,
        shell=True,
        cwd=str(cfg.cwd if cfg.cwd.is_dir() else cfg.workspace),
        env=cfg.child_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        start_new_session=True,  # own process group, so timeouts kill children too
    )
    timed_out = False
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc)
        output, _ = proc.communicate()
    except KeyboardInterrupt:
        # The child has its own process group, so the terminal's Ctrl-C never
        # reached it. Kill it here or it keeps running after the turn is gone.
        _kill_group(proc)
        output, _ = proc.communicate()
        partial, _ = _take_cwd(output or "", cfg)
        raise ToolInterrupted(partial=partial.rstrip("\n")) from None

    output, notes = _take_cwd(output or "", cfg)
    status = (
        f"timed out after {timeout:g}s (process group killed)"
        if timed_out
        else f"exit code: {proc.returncode}"
    )
    body = output.rstrip("\n") if output.strip() else "(no output)"
    extra = f" ({'; '.join(notes)})" if notes else ""
    return f"{status} · cwd: {cfg.cwd_label}{extra}\n--- output ---\n{body}"


def _take_cwd(output: str, cfg: Config) -> tuple[str, list[str]]:
    """Strip the cwd probe from the output and apply it to the session.

    The probe is missing when the command exits early (`exit 1`), was killed, or
    failed to parse. In that case the directory simply does not change.
    """
    notes: list[str] = []
    at = output.rfind(CWD_MARKER)
    if at == -1:
        return output, notes
    head = output[:at]
    if head.endswith("\n"):  # the newline the probe itself printed
        head = head[:-1]
    reported = output[at + len(CWD_MARKER) :].split("\n", 1)[0].strip()
    if reported and not cfg.set_cwd(Path(reported)):
        notes.append("left the workspace, directory reset to the root")
    return head, notes


def tool_read_file(args: dict, cfg: Config) -> str:
    path = resolve_in_workspace(cfg.workspace, _require_str(args, "path"))
    text = _read_text(path, cfg)
    lines = text.splitlines()

    offset = args.get("offset") or 1
    if not isinstance(offset, int) or offset < 1:
        raise ToolError("offset must be an integer >= 1")
    limit = args.get("limit")
    if limit is not None and (not isinstance(limit, int) or limit < 1):
        raise ToolError("limit must be an integer >= 1")

    if offset > len(lines) and lines:
        raise ToolError(f"offset {offset} is past end of file ({len(lines)} lines)")
    end = len(lines) if limit is None else min(len(lines), offset - 1 + limit)
    window = lines[offset - 1 : end]

    if not window:
        return f"{display_path(cfg.workspace, path)} is empty"
    numbered = "\n".join(
        f"{offset + i:6d}\t{line}" for i, line in enumerate(window)
    )
    header = (
        f"{display_path(cfg.workspace, path)} "
        f"(lines {offset}-{end} of {len(lines)})"
    )
    return f"{header}\n{numbered}"


def tool_write_file(args: dict, cfg: Config) -> str:
    path = resolve_in_workspace(cfg.workspace, _require_str(args, "path"))
    content = _require_str(args, "content")
    if path.is_dir():
        raise ToolError(f"{path} is a directory")
    existed = path.exists()
    made = [p for p in path.parents if not p.exists() and cfg.workspace in p.parents]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    verb = "Overwrote" if existed else "Created"
    note = ""
    if made:
        fresh = ", ".join(sorted(display_path(cfg.workspace, p) for p in made))
        note = f"; new directory created: {fresh}"
    return (
        f"{verb} {display_path(cfg.workspace, path)} "
        f"({len(content)} chars, {len(content.splitlines())} lines){note}"
    )


def tool_edit_file(args: dict, cfg: Config) -> str:
    path = resolve_in_workspace(cfg.workspace, _require_str(args, "path"))
    old = _require_str(args, "old_string")
    new = _require_str(args, "new_string")
    replace_all = bool(args.get("replace_all", False))

    if not old:
        raise ToolError("old_string must not be empty; use write_file to create a file")
    if old == new:
        raise ToolError("old_string and new_string are identical; nothing to do")

    text = _read_text(path, cfg)
    count = text.count(old)
    if count == 0:
        raise ToolError(
            "old_string not found. It must match the file byte for byte, including "
            "indentation and line breaks. Re-read the file and copy the exact text."
        )
    if count > 1 and not replace_all:
        raise ToolError(
            f"old_string occurs {count} times. Add surrounding context to make it "
            "unique, or pass replace_all=true."
        )

    path.write_text(text.replace(old, new), encoding="utf-8")
    where = f"{count} occurrences" if count > 1 else "1 occurrence"
    return f"Edited {display_path(cfg.workspace, path)} ({where} replaced)"


REGISTRY: dict[str, Callable[[dict, Config], str]] = {
    "shell": tool_shell,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "edit_file": tool_edit_file,
}


def execute(name: str, args: dict, cfg: Config) -> str:
    fn = REGISTRY.get(name)
    if fn is None:
        raise ToolError(f"unknown tool {name!r}; available: {', '.join(REGISTRY)}")
    if not isinstance(args, dict):
        raise ToolError("tool arguments must be a JSON object")
    return fn(args, cfg)
