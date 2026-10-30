"""Shared test scaffolding.

Every suite used to carry its own copy of check()/make_agent(), which meant ten
places to change when the Config signature moved. The runner (tests/run.py) relies
on reset()/report() so several suites can run in one process.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for entry in (str(ROOT), str(ROOT / "tests")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from agent import Agent  # noqa: E402
from config import build_parser, config_from_args  # noqa: E402
from logbook import Logbook  # noqa: E402

failures: list[str] = []
passes: list[str] = []


def check(label: str, condition: object, detail: str = "") -> bool:
    """Record one assertion. Truthy condition passes."""
    if condition:
        passes.append(label)
        print(f"  ok   {label}")
        return True
    failures.append(label)
    print(f"  FAIL {label} {detail}")
    return False


def reset() -> None:
    failures.clear()
    passes.clear()


def report(name: str = "") -> int:
    """Print the summary and return a process exit code."""
    print()
    if failures:
        print(f"{len(failures)} failed: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


def make_config(
    tmp: Path,
    base_url: str | None = None,
    extra: list[str] | None = None,
    stream: bool = False,
):
    """A Config rooted under tmp. Streaming off unless asked for."""
    os.environ.setdefault("OPENAI_API_KEY", "x")
    argv = [
        "--workspace", str(tmp / "ws"),
        "--log-dir", str(tmp / "logs"),
        "--prompt-file", str(tmp / "nope.md"),
    ]
    if base_url:
        argv += ["--model", "mock", "--base-url", base_url]
    if not stream:
        argv.append("--no-stream")
    argv += extra or []
    return config_from_args(build_parser().parse_args(argv))


def make_agent(
    tmp: Path,
    base_url: str | None = None,
    extra: list[str] | None = None,
    stream: bool = False,
) -> Agent:
    cfg = make_config(tmp, base_url, extra, stream)
    return Agent(cfg, Logbook(cfg.log_path))


def pairing_ok(history: list[dict]) -> bool:
    """Every tool message must answer a tool call that was actually made."""
    pending: set[str] = set()
    for message in history:
        role = message.get("role")
        if role == "assistant":
            pending |= {call["id"] for call in message.get("tool_calls") or []}
        elif role == "tool":
            if message["tool_call_id"] not in pending:
                return False
            pending.discard(message["tool_call_id"])
    return not pending


def records(agent: Agent) -> list[dict]:
    """Parsed JSONL log lines for a session."""
    return [
        json.loads(line)
        for line in agent.cfg.log_path.read_text().splitlines()
        if line.strip()
    ]


def real_users(messages: list[dict]) -> list[str]:
    """User messages, minus the state block appended to every request."""
    return [
        m["content"]
        for m in messages
        if m["role"] == "user" and not m["content"].startswith("<state>")
    ]
