"""Offline end-to-end checks. Run: .venv/bin/python tests/test_harness.py"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import mock_server  # noqa: E402

from agent import Agent  # noqa: E402
from config import build_parser, config_from_args  # noqa: E402
from logbook import Logbook  # noqa: E402
from util import ToolError, resolve_in_workspace, truncate  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        failures.append(label)
        print(f"  FAIL {label} {detail}")


def make_config(tmp: Path, base_url: str, stream: bool):
    args = build_parser().parse_args(
        [
            "--model",
            "mock",
            "--base-url",
            base_url,
            "--workspace",
            str(tmp / "ws"),
            "--log-dir",
            str(tmp / "logs"),
            "--prompt-file",
            str(tmp / "PROMPT.md"),
            "--max-output-chars",
            "2000",
            "--max-output-lines",
            "40",
            "--shell-timeout",
            "10",
            *([] if stream else ["--no-stream"]),
        ]
    )
    return config_from_args(args)


def test_truncate() -> None:
    print("truncate")
    text = "\n".join(str(i) for i in range(1000))
    out, note = truncate(text, 100_000, 50)
    check("line limit applied", len(out.splitlines()) <= 52, str(len(out.splitlines())))
    check("note present", note is not None and "lines" in note)
    check("head kept", out.startswith("0\n1\n"))
    check("tail kept", "999" in out)
    out2, note2 = truncate("short", 100, 100)
    check("short text untouched", out2 == "short" and note2 is None)
    out3, note3 = truncate("x" * 500, 100, 100)
    check("char limit applied", len(out3) < 300 and note3 is not None, str(len(out3)))


def test_paths(tmp: Path) -> None:
    print("path confinement")
    root = (tmp / "ws").resolve()
    root.mkdir(parents=True, exist_ok=True)
    check("relative ok", resolve_in_workspace(root, "a/b.txt") == root / "a/b.txt")
    for bad in ["../etc/passwd", "/etc/passwd", "~/secrets", "a/../../../x"]:
        try:
            resolve_in_workspace(root, bad)
            check(f"rejects {bad}", False)
        except ToolError:
            check(f"rejects {bad}", True)
    (root / "link").symlink_to("/etc")
    try:
        resolve_in_workspace(root, "link/passwd")
        check("rejects symlink escape", False)
    except ToolError:
        check("rejects symlink escape", True)
    (root / "link").unlink()


def test_prompt_file(tmp: Path, base_url: str) -> None:
    print("PROMPT.md")
    cfg = make_config(tmp, base_url, stream=False)
    check("built-in by default", cfg.prompt_source == "built-in")
    check("env block appended", "<environment>" in cfg.system_prompt())
    cfg.prompt_file.write_text("CUSTOM RULES HERE\n", encoding="utf-8")
    check("file used when present", "CUSTOM RULES HERE" in cfg.system_prompt())
    check("env block still appended", str(cfg.workspace) in cfg.system_prompt())
    check("picked up live", cfg.prompt_source.endswith("PROMPT.md"))
    cfg.prompt_file.unlink()
    check("falls back after delete", cfg.prompt_source == "built-in")


def test_loop(tmp: Path, base_url: str, stream: bool) -> None:
    label = "stream" if stream else "non-stream"
    print(f"agent loop ({label})")
    mock_server.set_script(mock_server.SCRIPT)
    cfg = make_config(tmp / label, base_url, stream=stream)
    log = Logbook(cfg.log_path)
    log.session_start(cfg)
    agent = Agent(cfg, log)
    final = agent.run("do the scripted thing")

    check("final text returned", "notes.txt" in final, final)
    check("all script turns consumed", len(mock_server.REQUESTS) == len(mock_server.SCRIPT),
          f"{len(mock_server.REQUESTS)} requests")
    notes = cfg.workspace / "notes.txt"
    check("file written", notes.is_file())
    check("edit applied", notes.read_text() == "alpha\ngamma\n", repr(notes.read_text()))
    check("usage captured", bool(agent.last_usage))

    tool_msgs = [m for m in agent.history if m.get("role") == "tool"]
    check("one tool message per call", len(tool_msgs) == 9, str(len(tool_msgs)))
    check("every tool call answered",
          all(
              call["id"] in {m["tool_call_id"] for m in tool_msgs}
              for m in agent.history
              if m.get("role") == "assistant"
              for call in m.get("tool_calls") or []
          ))

    joined = "\n".join(m["content"] for m in tool_msgs)
    check("truncation marker present", "output truncated" in joined)
    check("escape rejected", "outside the workspace" in joined)
    check("missing old_string reported", "old_string not found" in joined)
    check("timeout reported", "timed out after 1s" in joined)
    check("unknown tool reported", "unknown tool" in joined)
    check("read_file numbered lines", "     1\talpha" in joined)

    records = [json.loads(l) for l in cfg.log_path.read_text().splitlines()]
    kinds = [r["type"] for r in records]
    check("log has session_start", kinds[0] == "session_start")
    check("log has requests", kinds.count("llm_request") == len(mock_server.SCRIPT))
    check("log has responses", kinds.count("llm_response") == len(mock_server.SCRIPT))
    check("log has tool calls", kinds.count("tool_call") == 9)
    check("log has tool results", kinds.count("tool_result") == 9)
    full = next(
        r for r in records if r["type"] == "tool_result" and r.get("truncated")
    )
    check("log keeps untruncated output", "5000" in full["output"] and full["output_chars"] > 2000)
    check("log is valid jsonl", all("ts" in r and "session" in r for r in records))


def test_repair(tmp: Path, base_url: str) -> None:
    print("history repair")
    cfg = make_config(tmp / "repair", base_url, stream=False)
    agent = Agent(cfg, Logbook(cfg.log_path))
    agent.history = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "shell", "arguments": "{}"}},
                {"id": "b", "type": "function", "function": {"name": "shell", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "a", "content": "done"},
    ]
    agent.repair_history()
    check("missing result filled", agent.history[-1]["tool_call_id"] == "b")
    before = len(agent.history)
    agent.repair_history()
    check("idempotent", len(agent.history) == before)


def test_history_file(tmp: Path, base_url: str) -> None:
    print("repl history location")
    from main import history_file

    cfg = make_config(tmp / "hist", base_url, stream=False)
    Logbook(cfg.log_path)  # same order as main(): the log dir is created here
    path = history_file(cfg)
    check("lives with the logs", path.parent == cfg.log_path.parent, str(path))
    check("not next to the source", ROOT not in path.parents, str(path))
    check("directory exists by then", path.parent.is_dir())
    path.write_text("probe", encoding="utf-8")  # must be writable
    check("writable", path.read_text() == "probe")


def test_completion(tmp: Path, base_url: str) -> None:
    print("command completion")
    from main import COMMAND_NAMES, completions, setup_completion

    cfg = make_config(tmp / "comp", base_url, stream=False)
    agent = Agent(cfg, Logbook(cfg.log_path))
    (cfg.workspace / "subdir" / "nested").mkdir(parents=True, exist_ok=True)
    (cfg.workspace / ".hidden").mkdir(exist_ok=True)

    check("bare slash offers everything",
          completions("/", "/", agent) == [f"/{n}" for n in COMMAND_NAMES])
    check("prefix narrows it", completions("/his", "/his", agent) == ["/history"])
    check("several matches", sorted(completions("/h", "/h", agent))
          == ["/help", "/history"])
    check("unknown prefix gives nothing", completions("/zzz", "/zzz", agent) == [])
    check("prose is never completed", completions("write a file", "write", agent) == [])
    check("empty line gives nothing", completions("", "", agent) == [])

    check("think levels", completions("/think ", "", agent)
          == ["minimal", "low", "medium", "high", "off"])
    check("think levels filter", completions("/think h", "h", agent) == ["high"])
    check("history argument", completions("/history ", "", agent) == ["all"])
    check("commands without arguments", completions("/reset ", "", agent) == [])

    dirs = completions("/cwd ", "", agent)
    check("cwd offers directories", "subdir" in dirs and "subdir/nested" in dirs,
          str(dirs))
    check("cwd skips dotted directories", ".hidden" not in dirs, str(dirs))
    check("cwd filters by prefix", completions("/cwd sub", "sub", agent) == [
        "subdir", "subdir/nested"])

    agent.log.event("user_message", content="x")  # make the session file exist
    resumable = completions("/resume ", "", agent)
    check("resume offers last", resumable[0] == "last", str(resumable[:2]))
    check("resume offers session ids", agent.log.session_id in resumable,
          str(resumable[:3]))

    # Binding Tab must not raise whatever the backend is.
    check("setup_completion returns a bool",
          isinstance(setup_completion(agent), bool))


def test_commands(tmp: Path, base_url: str) -> None:
    print("repl commands")
    from main import handle_command

    cfg = make_config(tmp / "cmd", base_url, stream=False)
    agent = Agent(cfg, Logbook(cfg.log_path))
    agent.history = [{"role": "user", "content": "x"}]
    for cmd in ["/help", "/tools", "/prompt", "/history", "/log", "/limits", "/nope"]:
        check(f"{cmd} keeps running", handle_command(cmd, agent) is True)
    check("/model switches", handle_command("/model other-model", agent) and cfg.model == "other-model")
    handle_command("/reset", agent)
    check("/reset clears history", agent.history == [])
    check("/exit stops", handle_command("/exit", agent) is False)


def main() -> int:
    server, base_url = mock_server.start()
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        test_truncate()
        test_paths(tmp)
        test_prompt_file(tmp, base_url)
        test_loop(tmp, base_url, stream=False)
        test_loop(tmp, base_url, stream=True)
        test_repair(tmp, base_url)
        test_history_file(tmp, base_url)
        test_completion(tmp, base_url)
        test_commands(tmp, base_url)
    server.shutdown()
    print()
    if failures:
        print(f"{len(failures)} failed: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
