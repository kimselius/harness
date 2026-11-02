"""Offline end-to-end checks. Run: .venv/bin/python tests/test_harness.py"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import common  # noqa: E402
from common import check  # noqa: E402


def make_config(tmp: Path, base_url: str, stream: bool):
    # The limits this suite asserts against.
    return common.make_config(
        tmp, base_url,
        ["--max-output-chars", "2000", "--max-output-lines", "40",
         "--shell-timeout", "10", "--prompt-file", str(tmp / "PROMPT.md")],
        stream=stream,
    )

import mock_server  # noqa: E402

from agent import Agent  # noqa: E402
from logbook import Logbook  # noqa: E402
from util import ToolError, resolve_in_workspace, truncate  # noqa: E402


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


def test_tool_registry() -> None:
    print("tool registry")
    import tools as toolkit

    names = [tool.name for tool in toolkit.TOOLS]
    check("one entry per tool", len(toolkit.TOOLS) == 4, str(len(toolkit.TOOLS)))
    check("schemas derive from it",
          [s["function"]["name"] for s in toolkit.TOOL_SCHEMAS] == names)
    check("dispatch derives from it", list(toolkit.REGISTRY) == names)
    check("no duplicate names", len(set(names)) == len(names))
    check("every tool is callable", all(callable(t.run) for t in toolkit.TOOLS))
    check("every schema has a description",
          all(s["function"].get("description") for s in toolkit.TOOL_SCHEMAS))
    check("every tool takes args, cfg, session",
          all(t.run.__code__.co_argcount == 3 for t in toolkit.TOOLS))


def test_session_state(tmp: Path, base_url: str) -> None:
    print("session state")
    from session import Session

    cfg = make_config(tmp / "sess", base_url, stream=False)
    session = Session(cfg.workspace)
    check("starts at the root", session.cwd == session.root)
    check("label is a dot at the root", session.cwd_label == ".")
    check("state block names both", str(session.root) in session.state_block()
          and "(.)" in session.state_block())
    check("nothing counted yet", session.totals() == {
        "tokens_in": 0, "tokens_out": 0, "tokens_thinking": 0, "requests": 0})

    session.add_usage({"prompt_tokens": 1200, "completion_tokens": 300}, 120)
    session.add_usage({"prompt_tokens": 800, "completion_tokens": 100}, None)
    check("input totalled", session.tokens_in == 2000, str(session.tokens_in))
    check("output totalled", session.tokens_out == 400, str(session.tokens_out))
    check("thinking totalled", session.tokens_thinking == 120)
    check("requests counted", session.requests == 2)
    check("a response with no usage still counts as a request",
          (session.add_usage(None, None), session.requests)[1] == 3)
    line = session.totals_line()
    check("totals read in thousands", "in 2.0k" in line and "out 400" in line, line)
    check("thinking shown when present", "thinking 120" in line, line)

    quiet = Session(cfg.workspace)
    quiet.add_usage({"prompt_tokens": 5, "completion_tokens": 5}, 0)
    check("thinking omitted when zero", "thinking" not in quiet.totals_line(),
          quiet.totals_line())

    # Config no longer carries the directory, which is what tools used to mutate.
    check("config has no cwd", not hasattr(cfg, "cwd"))
    check("config has no state_block", not hasattr(cfg, "state_block"))


def test_completion(tmp: Path, base_url: str) -> None:
    print("command completion")
    from main import COMMANDS, completions, setup_completion

    names = [command.name for command in COMMANDS]

    cfg = make_config(tmp / "comp", base_url, stream=False)
    agent = Agent(cfg, Logbook(cfg.log_path))
    (cfg.workspace / "subdir" / "nested").mkdir(parents=True, exist_ok=True)
    (cfg.workspace / ".hidden").mkdir(exist_ok=True)

    check("bare slash offers everything",
          completions("/", "/", agent) == [f"/{n}" for n in names])
    check("every command has help text",
          all(command.help for command in COMMANDS))
    check("aliases dispatch too", completions("/q", "/q", agent) == [])
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


def test_step_limit_wraps_up(tmp: Path, base_url: str) -> None:
    """Running out of tool rounds must still produce a usable answer.

    It used to return whatever text was lying around — after a round of pure
    tool calls, a dangling intent or nothing — and that string is what a driving
    script reads as the result.
    """
    print("step limit wrap-up")
    import mock_server as mock

    from agent import MAX_STEPS

    mock.reset()
    # Two tool-calling turns, then the answer the wrap-up call will receive.
    mock.set_script([
        ("", [("shell", {"command": "echo one"})]),
        ("", [("shell", {"command": "echo two"})]),
        ("I wrote a.txt and b.txt. The migration is still unwritten.", []),
    ])
    agent = common.make_agent(tmp / "wrap", base_url, ["--max-steps", "2"])
    final = agent.run("do a long job")

    check("the closing summary is the result",
          final == "I wrote a.txt and b.txt. The migration is still unwritten.",
          repr(final))
    check("status still reports the limit", agent.outcome.status == MAX_STEPS,
          agent.outcome.status)
    check("and the text matches", agent.outcome.text == final)
    check("tool rounds counted, not the wrap-up", agent.outcome.steps == 2,
          str(agent.outcome.steps))
    check("history stays valid", common.pairing_ok(agent.history))

    # The wrap-up must not be able to call tools — that is what ends the loop.
    wrapup = [r for r in common.records(agent)
              if r["type"] == "llm_request" and r.get("purpose") == "wrapup"]
    check("exactly one wrap-up request", len(wrapup) == 1, str(len(wrapup)))
    check("it offered no tools", not wrapup[0]["tool_names"],
          str(wrapup[0]["tool_names"]))
    check("the limit note was actually sent",
          any("Step limit" in (m.get("content") or "")
              for m in wrapup[0]["messages"]),
          "note missing from the wrap-up request")
    check("the summary is in history for a resume",
          agent.history[-1]["role"] == "assistant"
          and "migration" in agent.history[-1]["content"],
          str(agent.history[-1])[:120])


def test_wrap_up_discards_tool_calls(tmp: Path, base_url: str) -> None:
    """A server may return tool calls even when none were offered."""
    print("wrap-up with unsolicited tool calls")
    import mock_server as mock

    mock.reset()
    # One entry, so it repeats: the wrap-up gets tool calls too.
    mock.set_script([("still going", [("shell", {"command": "echo loop"})])])
    agent = common.make_agent(tmp / "wrap-calls", base_url, ["--max-steps", "1"])
    agent.run("loop forever")

    check("no unanswered tool call left behind", common.pairing_ok(agent.history))
    check("the ignored calls are logged",
          any(r["type"] == "wrapup_tool_calls_ignored"
              for r in common.records(agent)))
    check("nothing extra was executed", sum(
        1 for r in common.records(agent) if r["type"] == "tool_call") == 1)

    # The real test of a valid history: the next turn must work.
    mock.set_script([("second turn fine", [])])
    check("the session still works afterwards",
          agent.run("anything else?") == "second turn fine")


def test_wrap_up_failure_is_survivable(tmp: Path, base_url: str) -> None:
    """A failed closing call must not turn a step-limit stop into a crash."""
    print("wrap-up that fails")
    import mock_server as mock

    from agent import MAX_STEPS

    mock.reset()
    mock.set_script([("partial progress", [("shell", {"command": "echo x"})])])
    agent = common.make_agent(tmp / "wrap-fail", base_url, ["--max-steps", "1"])

    # Fail only the closing call. Setting mock_server.FAIL_NEXT would break the
    # first request instead, which is a different path entirely.
    real = agent.llm.complete

    def flaky(*args, **kwargs):
        if kwargs.get("purpose") == "wrapup":
            raise RuntimeError("endpoint exploded")
        return real(*args, **kwargs)

    agent.llm.complete = flaky
    with common.capture() as cap:
        final = agent.run("do a long job")
    agent.llm.complete = real

    check("no crash", agent.outcome.status == MAX_STEPS, agent.outcome.status)
    check("falls back to the last real text", final == "partial progress",
          repr(final))
    check("says it could not summarize", "closing summary" in cap.err, cap.err[-200:])
    check("the failure is logged",
          any(r["type"] == "wrapup_error" for r in common.records(agent)))
    check("history still valid", common.pairing_ok(agent.history))


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
        test_tool_registry()
        test_session_state(tmp, base_url)
        test_completion(tmp, base_url)
        test_commands(tmp, base_url)
        test_step_limit_wraps_up(tmp, base_url)
        test_wrap_up_discards_tool_calls(tmp, base_url)
        test_wrap_up_failure_is_survivable(tmp, base_url)
    server.shutdown()
    return common.report()


if __name__ == "__main__":
    sys.exit(main())
