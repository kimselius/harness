"""Interruption checks. Run: .venv/bin/python tests/test_interrupt.py

Real SIGINT semantics are simulated with _thread.interrupt_main(), which raises
KeyboardInterrupt in the main thread exactly like Ctrl-C does.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import common  # noqa: E402
import mock_server  # noqa: E402
from common import check, make_agent, pairing_ok  # noqa: E402

import agent as agent_mod  # noqa: E402
import markdown  # noqa: E402
import tools as toolkit  # noqa: E402
from llm import Reply  # noqa: E402
from util import ToolInterrupted  # noqa: E402


def interrupt_in(seconds: float) -> threading.Timer:
    """Send ourselves a real SIGINT, exactly like Ctrl-C in the terminal.

    _thread.interrupt_main() only sets a flag, which does not wake a blocking
    sleep or a subprocess wait — the two cases that matter most here.
    """
    timer = threading.Timer(seconds, lambda: os.kill(os.getpid(), signal.SIGINT))
    timer.daemon = True
    timer.start()
    return timer


def running(pattern: str) -> bool:
    found = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
    return bool(found.stdout.strip())


def tool_call(cid: str, name: str = "shell", **args) -> dict:
    return {"id": cid, "name": name, "arguments": json.dumps(args)}


def test_shell_interrupt_kills_children(tmp: Path, base_url: str) -> None:
    print("shell interrupt kills the process group")
    agent = make_agent(tmp / "s1", base_url)
    cfg, session = agent.cfg, agent.session
    marker = "harness-interrupt-marker-7391"
    # Parent sh spawns a child sleep: both must die, not just the parent.
    command = f"echo starting; sh -c 'sleep 45 # {marker}' & sleep 45; echo never"

    interrupt_in(1.0)
    started = time.monotonic()
    try:
        toolkit.tool_shell({"command": command, "timeout": 60}, cfg, session)
        check("ToolInterrupted raised", False, "no exception")
    except ToolInterrupted as exc:
        elapsed = time.monotonic() - started
        check("ToolInterrupted raised", True)
        check("returned promptly", elapsed < 10, f"{elapsed:.1f}s")
        check("partial output kept", "starting" in exc.partial, repr(exc.partial))
        check("did not wait for the command", "never" not in exc.partial)
    except KeyboardInterrupt:
        check("ToolInterrupted raised", False, "leaked KeyboardInterrupt")

    time.sleep(0.5)
    check("no orphaned child process", not running(marker))


def test_tool_interrupt_in_turn(tmp: Path, base_url: str) -> None:
    print("interrupt during a tool call")
    agent = make_agent(tmp / "s2", base_url)
    marker = "harness-interrupt-marker-5520"

    def reply_with_two_calls(*a, **kw):
        return Reply("", [
            tool_call("c0", command=f"sleep 40 # {marker}"),
            tool_call("c1", command="echo second"),
        ], None, "tool_calls")

    agent.llm.complete = reply_with_two_calls
    interrupt_in(1.0)
    final = agent.run("run something slow")

    check("turn returned", final == "")
    check("agent marked interrupted", agent.interrupted)
    check("pairing intact", pairing_ok(agent.history), json.dumps(
        [m.get("role") for m in agent.history]))
    tool_msgs = [m for m in agent.history if m.get("role") == "tool"]
    check("both calls answered", len(tool_msgs) == 2, str(len(tool_msgs)))
    check("first reports interrupt", "interrupted by user" in tool_msgs[0]["content"])
    check("second not run", "not run" in tool_msgs[1]["content"],
          tool_msgs[1]["content"])
    check("note appended for the model",
          agent.history[-1]["content"] == agent_mod.INTERRUPT_NOTE)
    check("no further model calls", len([m for m in agent.history
                                         if m.get("role") == "assistant"]) == 1)
    time.sleep(0.3)
    check("no orphaned process", not running(marker))

    records = [json.loads(l) for l in agent.cfg.log_path.read_text().splitlines()]
    event = next((r for r in records if r["type"] == "interrupted"), None)
    check("interrupt logged", event is not None and event["where"] == "tool",
          str(event))


def test_interrupt_while_waiting(tmp: Path, base_url: str) -> None:
    print("interrupt while waiting for the model")
    agent = make_agent(tmp / "s3", base_url)

    def slow_complete(*a, **kw):
        time.sleep(30)
        raise AssertionError("should have been interrupted")

    agent.llm.complete = slow_complete
    interrupt_in(1.0)
    started = time.monotonic()
    agent.run("hello")
    check("returned promptly", time.monotonic() - started < 10)
    check("agent marked interrupted", agent.interrupted)
    check("note appended", agent.history[-1]["content"] == agent_mod.INTERRUPT_NOTE)
    check("pairing intact", pairing_ok(agent.history))
    records = [json.loads(l) for l in agent.cfg.log_path.read_text().splitlines()]
    check("logged as request interrupt",
          any(r["type"] == "interrupted" and r["where"] == "request" for r in records))


def test_interrupted_stream_keeps_partial(tmp: Path, base_url: str) -> None:
    print("interrupted stream")
    agent = make_agent(tmp / "s4", base_url)
    agent.llm.complete = lambda *a, **kw: Reply(
        "I was about to say", [], None, "interrupted", True
    )
    final = agent.run("talk")
    check("partial text returned", final == "I was about to say")
    check("partial text in history", any(
        m.get("role") == "assistant" and m["content"] == "I was about to say"
        for m in agent.history))
    check("no tool calls kept", all(not m.get("tool_calls")
                                    for m in agent.history
                                    if m.get("role") == "assistant"))
    check("note appended", agent.history[-1]["content"] == agent_mod.INTERRUPT_NOTE)
    check("agent marked interrupted", agent.interrupted)


def test_next_turn_recovers(tmp: Path, base_url: str) -> None:
    print("session continues after an interrupt")
    mock_server.set_script([("back to normal", [])])
    agent = make_agent(tmp / "s5", base_url)
    agent.llm.complete = lambda *a, **kw: Reply(
        "", [tool_call("c0", command="sleep 40 # harness-marker-9981")], None, "tool_calls"
    )
    interrupt_in(1.0)
    agent.run("slow thing")

    # Restore the real client and keep going in the same session.
    from llm import LLM

    agent.llm = LLM(agent.cfg, agent.log)
    final = agent.run("are you there?")
    check("next turn works", final == "back to normal", final)
    check("interrupted flag reset", not agent.interrupted)
    check("pairing intact", pairing_ok(agent.history))
    sent = mock_server.REQUESTS[-1]["messages"]
    check("model saw the interrupt note",
          any(m.get("content") == agent_mod.INTERRUPT_NOTE for m in sent))
    check("history accepted by the server", sent[0]["role"] == "system")


def test_duplicate_notes(tmp: Path, base_url: str) -> None:
    print("repeated interrupts")
    agent = make_agent(tmp / "s6", base_url)
    agent.note_interrupt("test")
    agent.note_interrupt("test")
    notes = [m for m in agent.history if m.get("content") == agent_mod.INTERRUPT_NOTE]
    check("note not duplicated", len(notes) == 1, str(len(notes)))


def test_pasted_code_warning(tmp: Path, base_url: str) -> None:
    print("pasted code detection")
    check("no fence", markdown.largest_code_block("just text") == 0)
    check("small fence", markdown.largest_code_block("```\na\nb\n```") == 2)
    check("largest of several", markdown.largest_code_block(
        "```\na\n```\ntext\n```py\n1\n2\n3\n```") == 3)
    check("unterminated fence counted", markdown.largest_code_block(
        "```py\n1\n2\n3\n4") == 4)
    check("language label not counted", markdown.largest_code_block(
        "```python\nx = 1\n```") == 1)

    agent = make_agent(tmp / "s7", base_url)
    big = "here you go:\n```python\n" + "\n".join(f"line {i}" for i in range(12)) + "\n```"
    agent._warn_about_pasted_code(big)
    records = [json.loads(l) for l in agent.cfg.log_path.read_text().splitlines()]
    check("warning logged", any(r["type"] == "pasted_code_block" and r["lines"] == 12
                                for r in records))
    agent._warn_about_pasted_code("```\nshort\n```")
    records = [json.loads(l) for l in agent.cfg.log_path.read_text().splitlines()]
    check("short block not flagged",
          len([r for r in records if r["type"] == "pasted_code_block"]) == 1)


def test_prompt_contract(tmp: Path, base_url: str) -> None:
    print("system prompt contract")
    cfg = make_agent(tmp / "s8", base_url).cfg
    prompt = cfg.system_prompt()
    for phrase in ["read_file", "write_file", "edit_file", "copy and paste",
                   "interrupt"]:
        check(f"prompt mentions {phrase!r}", phrase in prompt)
    # Survives a custom PROMPT.md, because it lives in the appended block.
    cfg.prompt_file.write_text("MY OWN RULES\n", encoding="utf-8")
    prompt = cfg.system_prompt()
    check("custom prompt used", "MY OWN RULES" in prompt)
    check("contract still appended", "copy and paste" in prompt)
    cfg.prompt_file.unlink()


def main() -> int:
    server, base_url = mock_server.start()
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        test_shell_interrupt_kills_children(tmp, base_url)
        test_tool_interrupt_in_turn(tmp, base_url)
        test_interrupt_while_waiting(tmp, base_url)
        test_interrupted_stream_keeps_partial(tmp, base_url)
        test_next_turn_recovers(tmp, base_url)
        test_duplicate_notes(tmp, base_url)
        test_pasted_code_warning(tmp, base_url)
        test_prompt_contract(tmp, base_url)
    server.shutdown()
    return common.report()


if __name__ == "__main__":
    sys.exit(main())
