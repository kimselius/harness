"""Compaction checks. Run: .venv/bin/python tests/test_compaction.py"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import common  # noqa: E402
import mock_server  # noqa: E402
from common import check, make_agent, pairing_ok, records  # noqa: E402

import context  # noqa: E402
import tools as toolkit  # noqa: E402


def assistant(text: str, calls: list[tuple[str, str]] | None = None) -> dict:
    msg: dict = {"role": "assistant", "content": text}
    if calls:
        msg["tool_calls"] = [
            {"id": cid, "type": "function",
             "function": {"name": "shell", "arguments": json.dumps({"command": cmd})}}
            for cid, cmd in calls
        ]
    return msg


def bulk_history(rounds: int, size: int = 2000) -> list[dict]:
    """Realistic history: user, assistant+tool_call, tool result, repeated."""
    out: list[dict] = []
    for i in range(rounds):
        out.append({"role": "user", "content": f"task {i}"})
        out.append(assistant("", [(f"c{i}", f"echo {i}")]))
        out.append({"role": "tool", "tool_call_id": f"c{i}", "content": "x" * size})
    return out




def test_meter() -> None:
    print("token meter")
    meter = context.TokenMeter(toolkit.TOOL_SCHEMAS)
    check("tool schemas counted", meter.tools_chars > 500, str(meter.tools_chars))
    msgs = [{"role": "user", "content": "x" * 3600}]
    check("estimate scales with chars", 900 < meter.tokens(msgs, with_tools=False) < 1100,
          str(meter.tokens(msgs, with_tools=False)))
    check("tools included in request estimate",
          meter.tokens(msgs) > meter.tokens(msgs, with_tools=False))
    before = meter.tokens(msgs)
    request_chars = meter.request_chars(msgs)
    meter.calibrate(request_chars, 2000)  # server says it was 2000 tokens
    check("calibrates to server usage", meter.tokens(msgs) > before,
          f"{before} -> {meter.tokens(msgs)}")
    check("chars/token matches measurement",
          abs(meter.chars_per_token - request_chars / 2000) < 0.01,
          f"{meter.chars_per_token} vs {request_chars / 2000}")
    check("estimate now agrees with the server",
          abs(meter.tokens(msgs) - 2000) <= 1, str(meter.tokens(msgs)))
    meter.calibrate(0, None)
    check("ignores missing usage", meter.samples == 1)
    meter.calibrate(100, 1)  # absurd ratio
    check("ratio clamped", context.MIN_CHARS_PER_TOKEN <= meter.chars_per_token
          <= context.MAX_CHARS_PER_TOKEN, str(meter.chars_per_token))


def test_split_point() -> None:
    print("split point")
    meter = context.TokenMeter([])
    history = bulk_history(10)
    for keep in (100, 500, 2000, 10_000):
        split = context.split_point(history, meter, keep)
        tail = history[split:]
        check(f"keep={keep}: tail starts clean", tail and tail[0]["role"] != "tool",
              tail[0]["role"] if tail else "empty")
        check(f"keep={keep}: tail valid pairing", pairing_ok(tail))
        check(f"keep={keep}: tail non-empty", len(tail) >= 1)
    check("bigger keep means bigger tail",
          context.split_point(history, meter, 10_000)
          <= context.split_point(history, meter, 100))
    # history ending in tool results (the mid-turn case)
    mid = bulk_history(3)
    split = context.split_point(mid, meter, 10)
    check("mid-turn history splits safely", mid[split:][0]["role"] != "tool"
          and pairing_ok(mid[split:]), mid[split:][0]["role"])
    check("empty history", context.split_point([], meter, 100) == 0)
    check("single message kept", context.split_point(
        [{"role": "user", "content": "hi"}], meter, 1) == 0)


def test_render() -> None:
    print("transcript render")
    text = context.render_transcript(
        [
            {"role": "user", "content": "do it"},
            assistant("working", [("c1", "ls -la")]),
            {"role": "tool", "tool_call_id": "c1", "content": "y" * 5000},
        ]
    )
    check("includes user", "USER: do it" in text)
    check("includes tool call", "ls -la" in text)
    check("caps tool output", "chars omitted" in text and len(text) < 3000, str(len(text)))


def test_compaction_runs(tmp: Path, base_url: str) -> None:
    print("compaction")
    mock_server.REQUESTS.clear()
    mock_server.set_script([("summary: did stuff", [])])
    agent = make_agent(tmp / "c1", base_url, ["--context-tokens", "8000"])
    agent.history = bulk_history(12)  # ~72k chars, way over threshold
    before = agent.used_tokens()
    check("starts over threshold", before > agent.cfg.compact_threshold, str(before))

    did = agent.maybe_compact()
    after = agent.used_tokens()
    check("compaction triggered", did)
    check("under threshold after", after < agent.cfg.compact_threshold,
          f"{before} -> {after}")
    # The shape is [original request, summary, recent tail]: the job statement
    # itself must never be replaced by a paraphrase of it.
    check("the opening request is pinned verbatim",
          agent.history[0]["content"] == "task 0", repr(agent.history[0]["content"]))
    check("summary follows it", agent.history[1]["content"].startswith(
        context.SUMMARY_HEADER), repr(agent.history[1]["content"][:80]))
    check("summary content kept", "did stuff" in agent.history[1]["content"])
    check("summary says not to start over",
          "not start the task over" in agent.history[1]["content"])
    check("recent history kept", any("task 11" in (m.get("content") or "")
                                     for m in agent.history))
    check("pairing intact", pairing_ok(agent.history))
    check("counter incremented", agent.compactions == 1)

    recs = [json.loads(l) for l in agent.cfg.log_path.read_text().splitlines()]
    event = next(r for r in recs if r["type"] == "compaction")
    check("logged before/after", event["tokens_before"] == before
          and event["tokens_after"] == after)
    check("logged summary", "did stuff" in event["summary"])
    check("summary request logged with purpose",
          any(r.get("purpose") == "compaction" for r in recs if r["type"] == "llm_request"))
    check("summary request sent no tools",
          all(not r["tool_names"] for r in recs
              if r["type"] == "llm_request" and r.get("purpose") == "compaction"))

    check("no-op when already small", agent.maybe_compact() is False)


def test_compaction_in_loop(tmp: Path, base_url: str) -> None:
    print("compaction during a turn")
    mock_server.REQUESTS.clear()
    # Each shell call floods ~30k chars of output, forcing compaction mid-turn.
    mock_server.set_script(
        [("", [("shell", {"command": "seq 1 9000"})])] * 4 + [("all done", [])]
    )
    agent = make_agent(
        tmp / "c2",
        base_url,
        ["--context-tokens", "6000", "--max-output-chars", "12000",
         "--max-output-lines", "20000"],  # let chars, not lines, be the cap
    )
    final = agent.run("flood the context")
    check("turn completed", final == "all done", final)
    check("compacted at least once", agent.compactions >= 1, str(agent.compactions))
    check("ended under window", agent.used_tokens() < agent.cfg.context_tokens,
          str(agent.used_tokens()))
    check("pairing intact", pairing_ok(agent.history))
    turn_requests = [r for r in mock_server.REQUESTS if r.get("tools")]
    biggest = max(len(json.dumps(r["messages"])) for r in turn_requests)
    check("no request exceeded the window",
          biggest / agent.meter.chars_per_token < agent.cfg.context_tokens * 1.2,
          f"{int(biggest / agent.meter.chars_per_token)} tokens")


def test_summary_failure(tmp: Path, base_url: str) -> None:
    print("summary failure fallback")
    mock_server.REQUESTS.clear()
    mock_server.set_script([("", [])])
    # Enough failures to exhaust the SDK's retries, so the error reaches us.
    mock_server.FAIL_NEXT.extend([True] * 5)
    agent = make_agent(tmp / "c3", base_url, ["--context-tokens", "8000"])
    agent.history = bulk_history(12)
    did = agent.compact(reason="test")
    check("still compacts", did)
    check("placeholder inserted",
          any("summary unavailable" in (m.get("content") or "")
              for m in agent.history[:2]),
          str(agent.history[:2])[:160])
    check("the request is still pinned even when summarizing failed",
          agent.history[0]["content"] == "task 0", repr(agent.history[0]["content"]))
    check("under threshold", agent.used_tokens() < agent.cfg.compact_threshold)
    check("pairing intact", pairing_ok(agent.history))
    mock_server.FAIL_NEXT.clear()


def test_disabled(tmp: Path, base_url: str) -> None:
    print("compaction disabled")
    agent = make_agent(tmp / "c4", base_url, ["--no-compact", "--context-tokens", "8000"])
    agent.history = bulk_history(12)
    check("no automatic compaction", agent.maybe_compact() is False)
    check("history untouched", len(agent.history) == 36)
    check("manual still works", agent.compact(reason="manual") is False
          or agent.compactions == 1)


def test_trim_guard(tmp: Path, base_url: str) -> None:
    print("trim fallback")
    mock_server.set_script([("tiny summary", [])])
    agent = make_agent(tmp / "c5", base_url, ["--context-tokens", "6000"])
    # A single pasted message larger than the whole trigger. Compaction keeps at
    # least one message, so the kept tail alone overflows and must be dropped;
    # there are no tool results to shrink first.
    agent.history = bulk_history(4) + [
        {"role": "user", "content": "huge " + "x" * 20000}
    ]
    agent.compact(reason="test")
    check("trimmed under threshold",
          agent.used_tokens() < agent.cfg.compact_threshold, str(agent.used_tokens()))
    check("summary survived trim",
          any((m.get("content") or "").startswith(context.SUMMARY_HEADER)
              for m in agent.history[:2]), str(agent.history[:2])[:160])
    check("the oversized message was shrunk, not dropped",
          any((m.get("content") or "").startswith("huge ") for m in agent.history[2:]))
    check("pairing intact", pairing_ok(agent.history))
    recs = [json.loads(l) for l in agent.cfg.log_path.read_text().splitlines()]
    check("shrink logged", any(r["type"] == "message_shrunk" for r in recs))
    check("it was the pasted message",
          any(r["type"] == "message_shrunk" and r["role"] == "user" for r in recs))


def test_shrink_before_drop(tmp: Path, base_url: str) -> None:
    print("shrink before drop")
    mock_server.set_script([("summary", [])])
    agent = make_agent(tmp / "c6", base_url,
                       ["--context-tokens", "6000", "--compact-keep", "0.05"])
    # One tool result far larger than the keep budget: it must be shrunk, not lost.
    agent.history = [
        {"role": "user", "content": "run it"},
        assistant("", [("c0", "seq 1 100000")]),
        {"role": "tool", "tool_call_id": "c0", "content": "RESULT-HEAD " + "x" * 60000},
    ]
    agent.compact(reason="test")
    tool_msgs = [m for m in agent.history if m.get("role") == "tool"]
    check("newest tool result survived", len(tool_msgs) == 1)
    check("it was shrunk, not dropped",
          tool_msgs and 600 < len(tool_msgs[0]["content"]) < 20000,
          str(len(tool_msgs[0]["content"]) if tool_msgs else 0))
    check("shrink marker present",
          tool_msgs and "truncated" in tool_msgs[0]["content"])
    check("head of output preserved",
          tool_msgs and tool_msgs[0]["content"].startswith("RESULT-HEAD"))
    check("under threshold", agent.used_tokens() < agent.cfg.compact_threshold,
          str(agent.used_tokens()))
    check("pairing intact", pairing_ok(agent.history))
    recs = [json.loads(l) for l in agent.cfg.log_path.read_text().splitlines()]
    check("shrink logged", any(r["type"] == "message_shrunk" for r in recs))


def test_task_survives_compaction(tmp: Path, base_url: str) -> None:
    """The reported failure: a part-done job restarted itself after compacting.

    The authoritative statement of the work was in the summarized half, so it
    was replaced by a paraphrase and the agent re-derived a different task.
    """
    print("the task itself survives")
    mock_server.reset()
    mock_server.set_script([("items 1-14 of 19 done, 15 next", [])])
    agent = make_agent(tmp / "pin", base_url, ["--context-tokens", "20000"])
    task = ("Process all 19 items listed in items.txt. For each, read it, "
            "transform it, and append the result to out/<n>.md. Strictly in order.")
    agent.history = [{"role": "user", "content": task}]
    for n in range(1, 15):
        agent.history.append({"role": "assistant", "content": f"Item {n} done."})
        agent.history.append({"role": "user", "content": "noise " + "x" * 2000})

    agent.compact(reason="test")

    check("the request is kept word for word",
          any(task == (m.get("content") or "") for m in agent.history),
          "the task statement did not survive")
    check("it comes first", agent.history[0]["content"] == task,
          agent.history[0]["content"][:60])
    check("exactly one copy of it",
          sum(1 for m in agent.history if (m.get("content") or "") == task) == 1)
    check("the summary is marked as history, not a request",
          context.SUMMARY_GUIDANCE in agent.history[1]["content"])
    check("recent work still verbatim",
          any("Item 14 done." == (m.get("content") or "") for m in agent.history))
    check("it did get smaller", agent.used_tokens() < 20000 * 0.75,
          str(agent.used_tokens()))

    # A second compaction must not drop it either, or a long run loses the task
    # on the round after next.
    mock_server.set_script([("still going", [])])
    for n in range(15, 19):
        agent.history.append({"role": "assistant", "content": f"Item {n} done."})
        agent.history.append({"role": "user", "content": "noise " + "x" * 2000})
    agent.compact(reason="test")
    check("survives a second compaction", agent.history[0]["content"] == task,
          agent.history[0]["content"][:60])
    check("still only one copy",
          sum(1 for m in agent.history if (m.get("content") or "") == task) == 1)

    # An enormous pasted brief is capped rather than reclaiming the window.
    mock_server.set_script([("ok", [])])
    big = make_agent(tmp / "pin-big", base_url, ["--context-tokens", "8000"])
    huge = "BRIEF: " + "requirement " * 8000
    big.history = [{"role": "user", "content": huge}]
    big.history += [{"role": "user", "content": "noise " + "y" * 2000}
                    for _ in range(8)]
    big.compact(reason="test")
    first = big.history[0]["content"]
    check("a huge brief is kept but capped",
          first.startswith("BRIEF: ") and len(first) < len(huge),
          f"{len(first)} of {len(huge)}")
    check("the cap is recorded",
          any(r["type"] == "pinned_task_truncated" for r in records(big)))


def test_progress_reporting(tmp: Path, base_url: str) -> None:
    """Compaction is the longest silent wait in the harness; it has to report."""
    print("compaction progress")
    import ui

    mock_server.reset()
    mock_server.set_script([("a summary of the old messages", [])])
    agent = common.make_agent(tmp / "progress", base_url,
                              ["--context-tokens", "8000"])
    agent.history = [{"role": "user", "content": f"task {i} " + "x" * 3000}
                     for i in range(6)]
    with common.capture() as cap:
        agent.compact(reason="test")

    check("says what it is compacting", "compacting context" in cap.err, cap.err)
    check("says how much it is keeping", "keeping the newest" in cap.err, cap.err)
    check("reports the result", "context compacted" in cap.err, cap.err)
    check("progress stays off stdout", cap.out == "", repr(cap.out))

    record = next(r for r in records(agent) if r["type"] == "compaction_summary_done")
    check("timing logged", isinstance(record["seconds"], (int, float)))
    check("input size logged", record["sent_tokens"] > 0, str(record["sent_tokens"]))
    check("summary size logged", record["summary_chars"] > 0,
          str(record["summary_chars"]))
    check("nothing streamed without --stream", record["streamed_chars"] == 0,
          str(record["streamed_chars"]))
    check("names the model it used", record["model"] == agent.cfg.model,
          str(record["model"]))

    # Streaming is where the live char count comes from.
    mock_server.reset()
    mock_server.set_script([("a streamed summary of the old messages", [])])
    streamed = common.make_agent(tmp / "progress-stream", base_url,
                                 ["--context-tokens", "8000"], stream=True)
    streamed.history = [{"role": "user", "content": f"task {i} " + "x" * 3000}
                        for i in range(6)]
    with common.capture():
        streamed.compact(reason="test")
    live = next(r for r in records(streamed)
                if r["type"] == "compaction_summary_done")
    check("streamed chars counted", live["streamed_chars"] > 0,
          str(live["streamed_chars"]))
    check("streamed count matches the summary",
          live["streamed_chars"] >= live["summary_chars"] > 0,
          f"{live['streamed_chars']} vs {live['summary_chars']}")

    # The Ticker must be inert when stderr is not a terminal, or a cron log fills
    # up with half-rewritten status lines.
    ticker = ui.Ticker("test")
    ticker.start()
    ticker.set(" detail")
    ticker.stop()
    check("ticker is silent off a terminal", not ticker._live)
    check("ticker survives stop without start", ui.Ticker("x").stop() is None)


def test_warns_when_compaction_did_not_help(tmp: Path, base_url: str) -> None:
    """Landing back over the trigger means it will fire again next turn."""
    print("compaction that does not help")
    mock_server.reset()
    mock_server.set_script([("tiny", [])])
    agent = common.make_agent(tmp / "nohelp", base_url, ["--context-tokens", "4000"])
    # One message too big to drop and too big to fit: the tail must stay.
    agent.history = [{"role": "user", "content": "keep " * 400},
                     {"role": "user", "content": "huge " * 4000}]
    with common.capture() as cap:
        agent.compact(reason="test")
    if agent.used_tokens() >= agent.cfg.compact_threshold:
        check("warns it is still over the trigger",
              "still at" in cap.err and "compact again" in cap.err, cap.err)
    else:
        check("got under the trigger, so no warning",
              "still at" not in cap.err, cap.err)


def main() -> int:
    server, base_url = mock_server.start()
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        test_meter()
        test_split_point()
        test_render()
        test_compaction_runs(tmp, base_url)
        test_compaction_in_loop(tmp, base_url)
        test_summary_failure(tmp, base_url)
        test_disabled(tmp, base_url)
        test_trim_guard(tmp, base_url)
        test_shrink_before_drop(tmp, base_url)
        test_task_survives_compaction(tmp, base_url)
        test_progress_reporting(tmp, base_url)
        test_warns_when_compaction_did_not_help(tmp, base_url)
    server.shutdown()
    return common.report()


if __name__ == "__main__":
    sys.exit(main())
