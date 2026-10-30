"""Mid-turn steering checks. Run: .venv/bin/python tests/test_steer.py"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import common  # noqa: E402
import mock_server  # noqa: E402
import ptyrun  # noqa: E402
from common import check, make_agent, real_users  # noqa: E402

import steer  # noqa: E402
from llm import Reply  # noqa: E402


def test_queue_reads_lines() -> None:
    print("queue mechanics")
    read_fd, write_fd = os.pipe()
    queue = steer.InputQueue(fd=read_fd, poll=0.02)
    seen: list[str] = []
    queue.on_queued = seen.append
    queue.start()
    check("running", queue.running)

    os.write(write_fd, b"first\nsecond\n")
    time.sleep(0.2)
    check("both lines queued", queue.pending() == 2, str(queue.pending()))
    check("callback fired per line", seen == ["first", "second"], str(seen))
    check("drain returns them", queue.drain() == ["first", "second"])
    check("drain empties", queue.pending() == 0)

    os.write(write_fd, b"  spaced  \n\n\nthird\n")
    time.sleep(0.2)
    check("blank lines skipped", queue.drain() == ["spaced", "third"])

    os.write(write_fd, b"partial without newline")
    time.sleep(0.2)
    check("partial not queued", queue.pending() == 0)
    check("partial kept as leftover", queue.leftover == "partial without newline",
          repr(queue.leftover))
    check("leftover can be taken", queue.take_leftover() == "partial without newline")
    check("leftover cleared", queue.leftover == "")

    os.write(write_fd, b"crlf\r\nbare\r")
    time.sleep(0.2)
    check("line endings normalized", queue.drain() == ["crlf", "bare"])

    queue.stop()
    check("stopped", not queue.running)
    os.write(write_fd, b"after stop\n")
    time.sleep(0.15)
    check("nothing read after stop", queue.pending() == 0)
    queue.stop()  # idempotent
    check("stop is idempotent", not queue.running)
    os.close(read_fd)
    os.close(write_fd)




class FakeInbox:
    """Feeds canned messages, one batch per step."""

    def __init__(self, batches: list[list[str]]) -> None:
        self.batches = list(batches)

    def drain(self) -> list[str]:
        return self.batches.pop(0) if self.batches else []

    def pending(self) -> int:
        return sum(len(b) for b in self.batches)


def test_injected_between_steps(tmp: Path, base_url: str) -> None:
    print("steering reaches the next request")
    agent = make_agent(tmp / "a", base_url)
    agent.inbox = FakeInbox([[], ["actually, use python3 instead"]])

    sent: list[list[dict]] = []

    def fake_complete(messages, tools, **kw):
        sent.append([dict(m) for m in messages])
        if len(sent) == 1:
            return Reply("", [{"id": "c0", "name": "shell",
                               "arguments": json.dumps({"command": "true"})}],
                         None, "tool_calls")
        return Reply("understood", [], None, "stop")

    agent.llm.complete = fake_complete
    agent.run("do the thing")

    check("two requests made", len(sent) == 2, str(len(sent)))
    first_contents = [m.get("content") for m in sent[0]]
    check("not in the first request",
          "actually, use python3 instead" not in first_contents)
    second_contents = [m.get("content") for m in sent[1]]
    check("present in the second request",
          "actually, use python3 instead" in second_contents, str(second_contents[-3:]))
    # Ignore the trailing state block the harness appends to every request.
    body = [m for m in sent[1] if not (m.get("content") or "").startswith("<state>")]
    roles = [m["role"] for m in body]
    check("queued message sits after the tool result",
          roles[-2:] == ["tool", "user"], str(roles))
    records = [json.loads(l) for l in agent.cfg.log_path.read_text().splitlines()]
    queued = [r for r in records if r["type"] == "user_message" and r.get("queued")]
    check("logged as queued", len(queued) == 1 and "python3" in queued[0]["content"],
          str(queued))


def test_no_inbox_is_fine(tmp: Path, base_url: str) -> None:
    print("without an inbox")
    mock_server.set_script([("plain answer", [])])
    agent = make_agent(tmp / "b", base_url)
    check("no inbox by default", agent.inbox is None)
    check("turn still works", agent.run("hello") == "plain answer")
    check("take_queued_input is a no-op", agent.take_queued_input() == 0)


def pty_session(base_url: str, tmp: Path, script: list[tuple[float, bytes]]) -> str:
    return ptyrun.repl(base_url, tmp, script)


def test_typing_while_a_tool_runs(tmp: Path, base_url: str) -> None:
    print("typing while a command runs")
    # Step 1 runs a slow command; the steering line is typed while it runs.
    mock_server.set_script(
        [
            ("", [("shell", {"command": "sleep 3"})]),
            ("", [("shell", {"command": "echo second"})]),
            ("finished", []),
        ]
    )
    screen = pty_session(
        base_url,
        tmp / "pty1",
        [
            (0.0, b"start the slow thing\n"),
            (1.0, b"while you work: check the logs too\n"),  # mid-tool
            (8.0, b"/exit\n"),
        ],
    )
    check("queue was acknowledged", "queued" in screen, screen[-400:])
    check("message was applied", "added to the context" in screen, screen[-400:])

    requests = [r for r in mock_server.REQUESTS if r.get("messages")]
    check("more than one request", len(requests) >= 2, str(len(requests)))
    if len(requests) < 2:
        return
    first = [m.get("content") for m in requests[0]["messages"]]
    later = [m.get("content") for m in requests[1]["messages"]]
    check("not in the first request",
          "while you work: check the logs too" not in first)
    check("in the next request",
          "while you work: check the logs too" in later, str(later[-2:]))
    check("the slow command still ran", "sleep 3" in json.dumps(requests[1]["messages"]))


def test_typed_after_turn_runs_next(tmp: Path, base_url: str) -> None:
    print("typing during the last step")
    mock_server.set_script([("all done", []), ("and the follow up", [])])
    screen = pty_session(
        base_url,
        tmp / "pty2",
        [(0.0, b"quick question\n"), (0.2, b"follow up please\n"), (4.0, b"/exit\n")],
    )
    requests = [r for r in mock_server.REQUESTS if r.get("messages")]
    check("two turns ran", len(requests) == 2, str(len(requests)))
    if len(requests) == 2:
        users = real_users(requests[1]["messages"])
        check("both messages present",
              users == ["quick question", "follow up please"], str(users))
    check("session ended cleanly", "bye" in screen, screen[-200:])


def main_() -> int:
    server, base_url = mock_server.start()
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        test_queue_reads_lines()
        test_injected_between_steps(tmp, base_url)
        test_no_inbox_is_fine(tmp, base_url)
        test_typing_while_a_tool_runs(tmp, base_url)
        test_typed_after_turn_runs_next(tmp, base_url)
    server.shutdown()
    return common.report()


if __name__ == "__main__":
    sys.exit(main_())
