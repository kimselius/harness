"""Configuration: CLI flags + environment, plus the system prompt."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

import sessions

# Env vars that must not leak into shell commands the model runs.
SECRET_ENV_PREFIXES = ("OPENAI_", "HARNESS_", "ANTHROPIC_")

# Everything the harness creates (workspace, logs, REPL history, .env) lives here,
# so a checkout stays self-contained no matter which directory you launch from.
BASE_DIR = Path(__file__).resolve().parent

DEFAULT_PROMPT = """\
You are an autonomous agent running inside a disposable container. You have shell
and file access and are expected to use it rather than guess.

Operating rules:
- Inspect before you change. Read a file before editing it.
- If you see a truncation marker in tool output, narrow the request (grep, head,
  tail, or the offset/limit arguments of read_file) instead of re-running the same
  broad command.
- For anything long running, redirect output to a file and start it in the
  background, then poll.
- edit_file replaces an exact substring. old_string must match exactly once unless
  replace_all is true, so include enough surrounding context to be unique.
- Chain tool calls until the task is actually done, then reply to the user with a
  short, concrete summary. Do not claim success you have not verified.
"""

# Always appended, so the model knows the real limits even with a custom PROMPT.md.
RUNTIME_FACTS = """\
<environment>
Tools: shell, read_file, write_file, edit_file.
Workspace root: {workspace}
Nothing outside that root is reachable, and every path a tool prints is relative to
it.

Two separate rules, which never change:
- read_file, write_file and edit_file resolve their `path` from the workspace root.
  A path means the same file no matter what the shell has been doing, so a path you
  read in an earlier tool result is always safe to pass back.
- shell keeps a working directory between calls, like a real session: `cd sub` in
  one call applies to later calls. It affects shell commands only, never the file
  tools. So to work on a subproject, `cd` into it once and then use `git diff`,
  `git log` or tests without prefixes.

The last message of every request is a <state> block written by the harness, not by
the user. It carries the exact workspace root and the shell's current directory.
Read it rather than assuming, and do not reply to it.

You act on files yourself. Use read_file to inspect, write_file to create or
replace, edit_file to change part of a file. Never print a file's contents, a patch
or a diff for the user to copy and paste, and never ask the user to run a command
you can run with shell. If you are about to show code in your reply, write it to a
file instead and say what you wrote. The exception is a short excerpt you are
explaining, a few lines at most.

The user can interrupt you at any time. If a tool result says you were interrupted,
the command was killed: stop, do not retry it, and wait for instructions.
Tool output is truncated to about {max_chars} characters / {max_lines} lines.
Shell commands are killed after {timeout:.0f}s by default ({max_timeout:.0f}s max).
Your replies are printed in a terminal about {columns} columns wide. Write plain
prose in short paragraphs. Inline code, paths and fenced code blocks are fine;
headings, tables and nested bullet lists are not worth the space.
</environment>
"""


@dataclass
class Config:
    model: str
    api_key: str
    base_url: str | None
    workspace: Path
    log_path: Path
    max_output_chars: int
    max_output_lines: int
    shell_timeout: float
    max_shell_timeout: float
    max_steps: int
    stream: bool
    temperature: float | None
    max_tokens: int | None
    request_timeout: float
    prompt_file: Path
    resumed: bool
    context_tokens: int
    compact_at: float
    compact_keep: float
    compact: bool
    summary_model: str | None
    markdown: bool

    # Mutable session state, not a flag: the shell's working directory, which
    # persists between tool calls like a real shell session.
    cwd: Path = field(init=False)

    def __post_init__(self) -> None:
        self.cwd = self.workspace

    def set_cwd(self, path: Path) -> bool:
        """Move the session cwd. False if the path escaped the workspace."""
        resolved = path.resolve()
        if resolved != self.workspace and self.workspace not in resolved.parents:
            self.cwd = self.workspace
            return False
        self.cwd = resolved
        return True

    @property
    def cwd_label(self) -> str:
        if self.cwd == self.workspace:
            return "."
        try:
            return str(self.cwd.relative_to(self.workspace))
        except ValueError:
            return str(self.cwd)

    @property
    def compact_threshold(self) -> int:
        """Token count at which history gets summarized."""
        return int(self.context_tokens * self.compact_at)

    @property
    def compact_keep_tokens(self) -> int:
        """Tokens of the most recent history kept verbatim through a compaction."""
        return int(self.context_tokens * self.compact_keep)

    def system_prompt(self) -> str:
        """PROMPT.md if present, else the built-in prompt; runtime facts appended."""
        if self.prompt_file.is_file():
            body = self.prompt_file.read_text(encoding="utf-8").strip()
        else:
            body = DEFAULT_PROMPT.strip()
        facts = RUNTIME_FACTS.format(
            workspace=self.workspace,
            max_chars=self.max_output_chars,
            max_lines=self.max_output_lines,
            timeout=self.shell_timeout,
            max_timeout=self.max_shell_timeout,
            columns=shutil.get_terminal_size(fallback=(80, 24)).columns,
        )
        return f"{body}\n\n{facts}"

    def state_block(self) -> str:
        """Authoritative session state, sent with every request.

        It goes at the end of the message list rather than into the system prompt:
        the prompt stays byte-identical all session, so the server's cached prefix
        survives, and the model still gets current values every turn.
        """
        return (
            "<state>\n"
            f"workspace root: {self.workspace}\n"
            f"file tool paths resolve from: {self.workspace} (always)\n"
            f"shell working directory: {self.cwd} ({self.cwd_label})\n"
            "</state>"
        )

    @property
    def prompt_source(self) -> str:
        return str(self.prompt_file) if self.prompt_file.is_file() else "built-in"

    def child_env(self) -> dict[str, str]:
        """Environment for shell commands, with harness credentials stripped."""
        return {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(SECRET_ENV_PREFIXES)
        }


def anchor(raw: str) -> Path:
    """Resolve a path, treating relative paths as relative to BASE_DIR."""
    path = Path(raw).expanduser()
    return path.resolve() if path.is_absolute() else (BASE_DIR / path).resolve()


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env loader. Existing environment always wins."""
    path = path or BASE_DIR / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        os.environ.setdefault(key, value)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="harness",
        description="Minimal agent harness for OpenAI-compatible APIs.",
    )
    p.add_argument(
        "-p",
        "--prompt",
        help="Run a single prompt non-interactively and exit.",
    )
    p.add_argument(
        "--model",
        default=os.getenv("HARNESS_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4o-mini",
        help="Model name (env: HARNESS_MODEL / OPENAI_MODEL).",
    )
    p.add_argument(
        "--base-url",
        default=os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE"),
        help="OpenAI-compatible endpoint, e.g. http://localhost:11434/v1.",
    )
    p.add_argument(
        "--workspace",
        default=os.getenv("HARNESS_WORKSPACE", "workspace"),
        help="Directory the agent is confined to (default: ./workspace).",
    )
    p.add_argument(
        "--prompt-file",
        default=os.getenv("HARNESS_PROMPT_FILE", "PROMPT.md"),
        help="System prompt file; used if it exists (default: ./PROMPT.md).",
    )
    p.add_argument(
        "--resume",
        nargs="?",
        const="last",
        default=None,
        metavar="SESSION",
        help=(
            "Continue an earlier session: its id, an unambiguous prefix, a path to "
            "the log, or nothing for the most recent one. New events append to that "
            "session's log."
        ),
    )
    p.add_argument(
        "--sessions",
        action="store_true",
        help="List recorded sessions and exit.",
    )
    p.add_argument(
        "--log-dir",
        default=os.getenv("HARNESS_LOG_DIR", "logs"),
        help="Directory for the JSONL transcript (default: ./logs).",
    )
    p.add_argument(
        "--max-output-chars",
        type=int,
        default=_env_int("HARNESS_MAX_OUTPUT_CHARS", 6000),
        help="Max characters of tool output handed back to the model.",
    )
    p.add_argument(
        "--max-output-lines",
        type=int,
        default=_env_int("HARNESS_MAX_OUTPUT_LINES", 250),
        help="Max lines of tool output handed back to the model.",
    )
    p.add_argument(
        "--shell-timeout",
        type=float,
        default=_env_float("HARNESS_SHELL_TIMEOUT", 120.0),
        help="Default shell command timeout in seconds.",
    )
    p.add_argument(
        "--max-shell-timeout",
        type=float,
        default=_env_float("HARNESS_MAX_SHELL_TIMEOUT", 900.0),
        help="Ceiling for a per-call timeout the model asks for.",
    )
    p.add_argument(
        "--max-steps",
        type=int,
        default=_env_int("HARNESS_MAX_STEPS", 30),
        help="Max tool-calling rounds per user message.",
    )
    p.add_argument(
        "--context-tokens",
        type=int,
        default=_env_int("HARNESS_CONTEXT_TOKENS", 32000),
        help=(
            "Usable context window in tokens. Must fit what the server actually "
            "serves (e.g. ollama's num_ctx), not the model's advertised maximum."
        ),
    )
    p.add_argument(
        "--compact-at",
        type=float,
        default=_env_float("HARNESS_COMPACT_AT", 0.75),
        help="Fraction of the window that triggers compaction (default 0.75).",
    )
    p.add_argument(
        "--compact-keep",
        type=float,
        default=_env_float("HARNESS_COMPACT_KEEP", 0.25),
        help="Fraction of the window kept verbatim as recent history (default 0.25).",
    )
    p.add_argument(
        "--no-compact",
        action="store_true",
        help="Disable compaction (history then grows until the model complains).",
    )
    p.add_argument(
        "--summary-model",
        default=os.getenv("HARNESS_SUMMARY_MODEL"),
        help="Model used for compaction summaries (default: the main model).",
    )
    p.add_argument(
        "--no-stream",
        action="store_true",
        help="Disable response streaming.",
    )
    p.add_argument(
        "--no-markdown",
        action="store_true",
        help=(
            "Print replies verbatim instead of rendering markdown. Also restores "
            "token-by-token streaming (rendering buffers by line)."
        ),
    )
    p.add_argument("--temperature", type=float, default=None)
    p.add_argument("--max-tokens", type=int, default=None)
    p.add_argument(
        "--request-timeout",
        type=float,
        default=_env_float("HARNESS_REQUEST_TIMEOUT", 600.0),
        help="HTTP timeout per model request.",
    )
    return p


def config_from_args(args: argparse.Namespace) -> Config:
    workspace = anchor(args.workspace)
    try:
        workspace.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        sys.exit(
            f"Cannot create workspace {workspace}: {exc.strerror}. "
            "Pass --workspace with a writable path (in a container, check that the "
            "directory is mounted read-write)."
        )
    if not os.access(workspace, os.W_OK):
        sys.exit(f"Workspace {workspace} is not writable.")

    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("HARNESS_API_KEY")
    if not api_key:
        if args.base_url:
            api_key = "sk-no-key"  # local servers usually ignore it
        else:
            sys.exit(
                "No API key. Set OPENAI_API_KEY (see .env.example), or pass "
                "--base-url for a local server."
            )

    log_dir = anchor(args.log_dir)
    resumed = False
    if args.resume is not None:
        try:
            log_path = sessions.resolve(log_dir, args.resume)
        except sessions.SessionError as exc:
            sys.exit(f"{exc}")
        resumed = True
    else:
        log_path = log_dir / f"{sessions.new_id()}{sessions.SESSION_SUFFIX}"

    return Config(
        model=args.model,
        api_key=api_key,
        base_url=args.base_url,
        workspace=workspace,
        log_path=log_path,
        max_output_chars=max(500, args.max_output_chars),
        max_output_lines=max(20, args.max_output_lines),
        shell_timeout=args.shell_timeout,
        max_shell_timeout=args.max_shell_timeout,
        max_steps=max(1, args.max_steps),
        stream=not args.no_stream,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        request_timeout=args.request_timeout,
        prompt_file=anchor(args.prompt_file),
        resumed=resumed,
        context_tokens=max(2000, args.context_tokens),
        compact_at=min(0.95, max(0.3, args.compact_at)),
        compact_keep=min(0.6, max(0.05, args.compact_keep)),
        compact=not args.no_compact,
        summary_model=args.summary_model,
        markdown=not args.no_markdown,
    )
