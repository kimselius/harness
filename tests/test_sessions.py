"""Session identity and resume checks. Run: .venv/bin/python tests/test_sessions.py"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import common  # noqa: E402
import mock_server  # noqa: E402
from common import check, pairing_ok  # noqa: E402
from common import make_agent as _make_agent  # noqa: E402

import context  # noqa: E402
import sessions  # noqa: E402


def make_agent(tmp, base_url, extra=None):
    """As common.make_agent, but logs session_start the way main() does: this
    suite reads those events back through sessions.summarize()."""
    agent = _make_agent(tmp, base_url, extra)
    agent.log.session_start(agent.cfg, resumed=agent.cfg.resumed)
    return agent





def test_ids() -> None:
    print("session ids")
    first = sessions.new_id()
    check("matches the expected shape", sessions.SESSION_ID.match(first), first)
    check("ids are unique", first != sessions.new_id())


def test_file_is_the_session(tmp: Path, base_url: str) -> None:
    print("one file per session")
    mock_server.set_script([("done", [])])
    a1 = make_agent(tmp / "s1", base_url)
    check("log named after the session", a1.cfg.log_path.stem == a1.log.session_id)
    check("id is a valid session id", sessions.SESSION_ID.match(a1.log.session_id),
          a1.log.session_id)
    a1.run("first task")
    time.sleep(0.05)
    a2 = make_agent(tmp / "s1", base_url)  # same log dir, new session
    a2.run("second task")
    check("separate files", a1.cfg.log_path != a2.cfg.log_path)
    found = sessions.session_files(a2.cfg.log_path.parent)
    check("both discoverable", len(found) == 2, str(len(found)))
    check("newest first", found[0] == a2.cfg.log_path)


def test_resolve(tmp: Path, base_url: str) -> None:
    print("resolving references")
    mock_server.set_script([("done", [])])
    agent = make_agent(tmp / "s2", base_url)
    agent.run("hello")
    log_dir = agent.cfg.log_path.parent
    sid = agent.log.session_id

    check("last", sessions.resolve(log_dir, "last") == agent.cfg.log_path)
    check("empty means last", sessions.resolve(log_dir, "") == agent.cfg.log_path)
    check("exact id", sessions.resolve(log_dir, sid) == agent.cfg.log_path)
    check("prefix", sessions.resolve(log_dir, sid[:11]) == agent.cfg.log_path)
    check("full path", sessions.resolve(log_dir, str(agent.cfg.log_path))
          == agent.cfg.log_path)
    for bad in ["nope", "19000101-000000-aaaaaa"]:
        try:
            sessions.resolve(log_dir, bad)
            check(f"rejects {bad}", False)
        except sessions.SessionError:
            check(f"rejects {bad}", True)
    try:
        sessions.resolve(log_dir.parent / "empty", "last")
        check("rejects an empty directory", False)
    except sessions.SessionError:
        check("rejects an empty directory", True)


def test_replay_matches(tmp: Path, base_url: str) -> None:
    print("replay reproduces the conversation")
    mock_server.set_script(
        [
            ("", [("write_file", {"path": "a.txt", "content": "alpha\n"})]),
            ("", [("shell", {"command": "ls"})]),
            ("all done", []),
        ]
    )
    agent = make_agent(tmp / "s3", base_url)
    agent.run("do two things")
    original = [dict(m) for m in agent.history]

    history, stats = sessions.load_history(
        agent.cfg.log_path, agent.cfg.max_output_chars, agent.cfg.max_output_lines
    )
    check("same number of messages", len(history) == len(original),
          f"{len(history)} vs {len(original)}")
    check("roles in order",
          [m["role"] for m in history] == [m["role"] for m in original],
          str([m["role"] for m in history]))
    check("user text preserved", history[0]["content"] == "do two things")
    check("assistant tool calls preserved",
          history[1].get("tool_calls") == original[1].get("tool_calls"))
    check("tool results preserved",
          [m["content"] for m in history if m["role"] == "tool"]
          == [m["content"] for m in original if m["role"] == "tool"])
    check("final text preserved", history[-1]["content"] == "all done")
    check("pairing valid", pairing_ok(history))
    check("records counted", stats["records"] > 0)


def test_replay_truncates_with_current_limits(tmp: Path, base_url: str) -> None:
    print("replay respects current output limits")
    mock_server.set_script(
        [("", [("shell", {"command": "seq 1 4000"})]), ("done", [])]
    )
    agent = make_agent(tmp / "s4", base_url, ["--max-output-lines", "4000",
                                              "--max-output-chars", "40000"])
    agent.run("flood")
    big = [m for m in agent.history if m["role"] == "tool"][0]["content"]

    small, _ = sessions.load_history(agent.cfg.log_path, 500, 20)
    tool_text = [m for m in small if m["role"] == "tool"][0]["content"]
    check("log kept the full output", len(big) > 10_000, str(len(big)))
    check("replay re-truncated it", len(tool_text) < 1500, str(len(tool_text)))
    check("truncation marked", "truncated" in tool_text)


def test_replay_honours_compaction(tmp: Path, base_url: str) -> None:
    print("replay honours compaction")
    mock_server.set_script([("a summary of earlier work", [])])
    agent = make_agent(tmp / "s5", base_url, ["--context-tokens", "8000"])
    # Each message must be big enough that the keep budget cannot hold them all,
    # otherwise compaction correctly decides there is nothing to summarize.
    bulk = [{"role": "user", "content": f"task {i} " + "x" * 3000} for i in range(6)]
    agent.history = list(bulk)
    for message in bulk:
        agent.log.event("user_message", content=message["content"])
    agent.compact(reason="test")
    compacted = len(agent.history)

    history, stats = sessions.load_history(agent.cfg.log_path, 6000, 250)
    check("compaction applied", stats["compactions_applied"] == 1, str(stats))
    check("same length as live history", len(history) == compacted,
          f"{len(history)} vs {compacted}")
    check("starts with the summary",
          history[0]["content"].startswith(context.SUMMARY_HEADER), history[0]["content"][:40])
    check("summary text kept", "a summary of earlier work" in history[0]["content"])
    check("tail kept verbatim", history[-1]["content"].startswith("task 5"),
          history[-1]["content"][:20])


def test_replay_repairs_killed_session(tmp: Path, base_url: str) -> None:
    print("replay of a killed session")
    mock_server.set_script([("", [("shell", {"command": "echo hi"})])])
    agent = make_agent(tmp / "s6", base_url)
    # Log a tool call with no result, as a killed process would leave behind.
    agent.log.event("user_message", content="run it")
    agent.log.event(
        "llm_response",
        content="",
        tool_calls=[{"id": "orphan", "name": "shell", "arguments": "{}"}],
        purpose="turn",
    )
    with agent.cfg.log_path.open("a", encoding="utf-8") as fh:
        fh.write('{"type": "llm_resp')  # a half-written line

    history, _ = sessions.load_history(agent.cfg.log_path, 6000, 250)
    check("damaged line skipped", len(history) == 2, str(len(history)))
    check("unanswered call survives replay", history[-1].get("tool_calls"))
    agent.history = history
    agent.repair_history(reason="not recorded")
    check("repair fills the gap", pairing_ok(agent.history),
          str([m["role"] for m in agent.history]))


def test_summarize_listing(tmp: Path, base_url: str) -> None:
    print("session listing")
    mock_server.set_script([("", [("shell", {"command": "true"})]), ("done", [])])
    agent = make_agent(tmp / "s7", base_url)
    agent.run("count my tools")
    info = sessions.summarize(agent.cfg.log_path)
    check("id", info["id"] == agent.log.session_id)
    check("model", info["model"] == "mock")
    check("one turn", info["turns"] == 1, str(info["turns"]))
    check("one tool call", info["tool_calls"] == 1, str(info["tool_calls"]))
    check("first message shown", info["first_message"] == "count my tools")
    check("has a start time", bool(info["started"]))
    row = sessions.format_row(info)
    check("row mentions the id", info["id"] in row)
    check("row mentions the message", "count my tools" in row)
    # Harness-authored notes must not count as user turns.
    agent.log.event("user_message", content=context.INTERRUPT_NOTE)
    check("harness notes ignored", sessions.summarize(agent.cfg.log_path)["turns"] == 1)


def test_resume_appends(tmp: Path, base_url: str) -> None:
    print("resuming appends to the same log")
    mock_server.set_script([("first answer", [])])
    first = make_agent(tmp / "s8", base_url)
    first.run("remember apples")
    sid = first.log.session_id
    seq_before = sessions.last_seq(first.cfg.log_path)

    time.sleep(0.01)
    mock_server.set_script([("second answer", [])])
    resumed = make_agent(tmp / "s8", base_url, ["--resume", sid])
    check("same session id", resumed.log.session_id == sid)
    check("same file", resumed.cfg.log_path == first.cfg.log_path)
    check("config marked resumed", resumed.cfg.resumed)
    check("seq continues", resumed.log._seq >= seq_before, str(resumed.log._seq))

    history, _ = sessions.load_history(resumed.cfg.log_path, 6000, 250)
    resumed.history = history
    resumed.run("and now?")
    records = [json.loads(l) for l in resumed.cfg.log_path.read_text().splitlines()
               if l.strip()]
    seqs = [r["seq"] for r in records]
    check("no duplicate seq numbers", len(seqs) == len(set(seqs)), str(seqs))
    check("seqs ascending", seqs == sorted(seqs))
    check("resume event recorded",
          any(r["type"] == "session_resumed" for r in records))
    sent = mock_server.REQUESTS[-1]["messages"]
    check("model saw the old turn",
          any(m.get("content") == "remember apples" for m in sent))
    check("and the new one",
          any(m.get("content") == "and now?" for m in sent))
    check("only one session file",
          len(sessions.session_files(resumed.cfg.log_path.parent)) == 1)


def test_cli_resume(tmp: Path, base_url: str) -> None:
    print("--resume and --sessions on the command line")
    mock_server.set_script([("noted", [])])
    base = [
        sys.executable, "-u", str(ROOT / "main.py"),
        "--model", "mock", "--base-url", base_url,
        "--workspace", str(tmp / "s9" / "ws"),
        "--log-dir", str(tmp / "s9" / "logs"),
        "--prompt-file", str(tmp / "s9" / "nope.md"),
        "--no-stream", "--no-markdown",
    ]
    import os

    env = {**os.environ, "OPENAI_API_KEY": "x"}
    subprocess.run(base + ["-p", "the magic word is kumquat"], env=env,
                   capture_output=True, text=True, timeout=120)

    listing = subprocess.run(base + ["--sessions"], env=env, capture_output=True,
                             text=True, timeout=60)
    check("--sessions lists it", "kumquat" in listing.stdout, listing.stdout[-200:])

    mock_server.set_script([("kumquat", [])])
    again = subprocess.run(base + ["--resume", "-p", "what was the magic word?"],
                           env=env, capture_output=True, text=True, timeout=120)
    check("--resume runs", again.returncode == 0, again.stderr[-300:])
    check("prints what it resumed", "resumed" in again.stdout, again.stdout[:200])
    sent = mock_server.REQUESTS[-1]["messages"]
    check("earlier turn was replayed",
          any("kumquat" in (m.get("content") or "") for m in sent))
    check("resume note added",
          any(m.get("content") == context.RESUME_NOTE for m in sent))
    check("still one log file",
          len(sessions.session_files((tmp / "s9" / "logs").resolve())) == 1)

    bad = subprocess.run(base + ["--resume", "does-not-exist"], env=env,
                         capture_output=True, text=True, timeout=60)
    check("unknown session exits with a message", bad.returncode != 0
          and "no session matching" in (bad.stderr + bad.stdout), bad.stderr[-200:])


def test_transcript_rendering(tmp: Path, base_url: str) -> None:
    print("transcript rendering")
    import io
    from contextlib import redirect_stdout

    import main as cli

    mock_server.set_script(
        [("", [("shell", {"command": "ls -la"})]), ("all finished", [])]
    )
    agent = make_agent(tmp / "render", base_url)
    with redirect_stdout(io.StringIO()):
        agent.run("look around")

    out = io.StringIO()
    with redirect_stdout(out):
        cli.print_transcript(agent, limit=None)
    screen = out.getvalue()
    check("user message shown", "look around" in screen, screen[:120])
    check("tool call shown", "ls -la" in screen and "shell" in screen, screen[:200])
    check("tool result shown", "exit code" in screen, screen[:300])
    check("assistant reply shown", "all finished" in screen)

    # Harness-authored messages are marked, not shown as things you typed.
    agent.history.append({"role": "user", "content": context.RESUME_NOTE})
    out = io.StringIO()
    with redirect_stdout(out):
        cli.print_transcript(agent, limit=None)
    lines = [l for l in out.getvalue().splitlines() if "resumed in a new process" in l]
    check("harness note is indented, not prompted", lines and lines[0].startswith("  "),
          repr(lines[0]) if lines else "missing")

    # Long sessions show a tail plus a count of what was skipped.
    agent.history = [{"role": "user", "content": f"message {i}"} for i in range(30)]
    out = io.StringIO()
    with redirect_stdout(out):
        cli.print_transcript(agent)
    screen = out.getvalue()
    check("only the tail is printed", "message 29" in screen and "message 0" not in screen)
    check("says how many were hidden", "18 earlier messages" in screen, screen[:120])
    out = io.StringIO()
    with redirect_stdout(out):
        cli.print_transcript(agent, limit=None)
    check("limit=None shows everything", "message 0" in out.getvalue())

    # An unparseable tool call must not crash the replay.
    agent.history = [{
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "x", "type": "function",
                        "function": {"name": "shell", "arguments": "{bad json"}}],
    }]
    out = io.StringIO()
    with redirect_stdout(out):
        cli.print_transcript(agent, limit=None)
    check("broken arguments survive", "shell" in out.getvalue(), out.getvalue()[:120])


def test_resume_shows_the_conversation(tmp: Path, base_url: str) -> None:
    print("resume replays to the screen")
    import ptyrun

    mock_server.set_script([("the first answer", [])])
    first = make_agent(tmp / "replay", base_url)
    first.run("remember the pelican")
    sid = first.log.session_id

    mock_server.set_script([("the second answer", [])])
    screen = ptyrun.repl(
        base_url, tmp / "replay", [(0.0, b"/exit\n")], extra_args=["--resume", sid]
    )
    check("says what it resumed", f"resumed {sid}" in screen, screen[:200])
    check("earlier user message on screen", "remember the pelican" in screen,
          screen[:400])
    check("earlier reply on screen", "the first answer" in screen, screen[:400])
    check("banner comes first",
          screen.index("harness") < screen.index("remember the pelican"), screen[:160])


def main_() -> int:
    server, base_url = mock_server.start()
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        test_ids()
        test_file_is_the_session(tmp, base_url)
        test_resolve(tmp, base_url)
        test_replay_matches(tmp, base_url)
        test_replay_truncates_with_current_limits(tmp, base_url)
        test_replay_honours_compaction(tmp, base_url)
        test_replay_repairs_killed_session(tmp, base_url)
        test_summarize_listing(tmp, base_url)
        test_resume_appends(tmp, base_url)
        test_cli_resume(tmp, base_url)
        test_transcript_rendering(tmp, base_url)
        test_resume_shows_the_conversation(tmp, base_url)
    server.shutdown()
    return common.report()


if __name__ == "__main__":
    sys.exit(main_())
