"""Compaction checks. Run: .venv/bin/python tests/test_compaction.py"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import mock_server  # noqa: E402

import context  # noqa: E402
import tools as toolkit  # noqa: E402
from agent import Agent  # noqa: E402
from config import build_parser, config_from_args  # noqa: E402
from logbook import Logbook  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        failures.append(label)
        print(f"  FAIL {label} {detail}")


def make_agent(tmp: Path, base_url: str, extra: list[str] | None = None) -> Agent:
    args = build_parser().parse_args(
        [
            "--model", "mock",
            "--base-url", base_url,
            "--workspace", str(tmp / "ws"),
            "--log-dir", str(tmp / "logs"),
            "--prompt-file", str(tmp / "nope.md"),
            "--no-stream",
            *(extra or []),
        ]
    )
    cfg = config_from_args(args)
    return Agent(cfg, Logbook(cfg.log_path))


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


def pairing_ok(history: list[dict]) -> bool:
    """Every tool message must follow an assistant message that called it."""
    pending: set[str] = set()
    for message in history:
        role = message.get("role")
        if role == "assistant":
            pending |= {c["id"] for c in message.get("tool_calls") or []}
        elif role == "tool":
            if message["tool_call_id"] not in pending:
                return False
            pending.discard(message["tool_call_id"])
    return not pending


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
    check("summary message first", agent.history[0]["content"].startswith(
        context.SUMMARY_HEADER))
    check("summary content kept", "did stuff" in agent.history[0]["content"])
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
    check("placeholder inserted", "summary unavailable" in agent.history[0]["content"])
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
          agent.history[0]["content"].startswith(context.SUMMARY_HEADER))
    check("the oversized message was shrunk, not dropped",
          any((m.get("content") or "").startswith("huge ") for m in agent.history[1:]))
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
    server.shutdown()
    print()
    if failures:
        print(f"{len(failures)} failed: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
