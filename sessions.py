"""Finding, summarizing and replaying sessions.

The JSONL log is the only source of truth, so a session can be resumed even if the
process was killed. Imports stay light (stdlib plus util/context) so config.py can
use this without an import cycle.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import context
from util import truncate

SESSION_SUFFIX = ".jsonl"
# 20261026-101530-a1b2c3
SESSION_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")
LATEST = {"last", "latest", "-"}


class SessionError(Exception):
    """Raised when a requested session cannot be used."""


def new_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


def session_files(log_dir: Path) -> list[Path]:
    """Session logs, newest first."""
    if not log_dir.is_dir():
        return []
    files = [p for p in log_dir.glob(f"*{SESSION_SUFFIX}") if p.is_file()]
    return sorted(files, key=lambda p: (p.stat().st_mtime, p.name), reverse=True)


def resolve(log_dir: Path, token: str) -> Path:
    """Turn a user-supplied session reference into a log path.

    Accepts "last", a full session id, an id prefix, or a path to a log file.
    """
    token = (token or "").strip()
    files = session_files(log_dir)
    if not token or token in LATEST:
        if not files:
            raise SessionError(f"no sessions found in {log_dir}")
        return files[0]

    direct = Path(token).expanduser()
    if direct.is_file():
        return direct

    exact = [p for p in files if p.stem == token]
    if exact:
        return exact[0]
    prefix = [p for p in files if p.stem.startswith(token)]
    if len(prefix) == 1:
        return prefix[0]
    if len(prefix) > 1:
        names = ", ".join(p.stem for p in prefix[:5])
        raise SessionError(f"{token!r} matches several sessions: {names}")
    raise SessionError(f"no session matching {token!r} in {log_dir}")


def _records(path: Path):
    """Yield parsed records, skipping damaged lines (a killed process can cut one)."""
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def last_seq(path: Path) -> int:
    """Highest seq already written, so a resumed session keeps counting up."""
    if not path.is_file():
        return 0
    highest = 0
    for record in _records(path):
        value = record.get("seq")
        if isinstance(value, int) and value > highest:
            highest = value
    return highest


def summarize(path: Path) -> dict[str, Any]:
    """Overview of a session for listings."""
    info: dict[str, Any] = {
        "id": path.stem,
        "path": path,
        "started": None,
        "last": None,
        "model": None,
        "turns": 0,
        "tool_calls": 0,
        "compactions": 0,
        "first_message": "",
        "interrupted": False,
    }
    for record in _records(path):
        kind = record.get("type")
        info["last"] = record.get("ts") or info["last"]
        if kind == "session_start":
            info["started"] = record.get("ts")
            info["model"] = record.get("model")
        elif kind == "user_message":
            content = record.get("content") or ""
            if not content.startswith("[harness]"):
                info["turns"] += 1
                if not info["first_message"]:
                    info["first_message"] = " ".join(content.split())[:60]
        elif kind == "tool_call":
            info["tool_calls"] += 1
        elif kind == "compaction":
            info["compactions"] += 1
        elif kind == "interrupted":
            info["interrupted"] = True
    if info["started"] is None:
        info["started"] = info["last"]
    return info


def load_history(
    path: Path, max_chars: int, max_lines: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Rebuild the conversation from a session log.

    Tool results are re-truncated with the current limits rather than stored twice,
    and recorded compactions are honoured so a resumed session starts as small as
    it ended. Returns (history, stats).
    """
    history: list[dict[str, Any]] = []
    stats = {"compactions_applied": 0, "compactions_ignored": 0, "records": 0}

    for record in _records(path):
        stats["records"] += 1
        kind = record.get("type")

        if kind == "user_message":
            history.append({"role": "user", "content": record.get("content") or ""})

        elif kind == "llm_response" and record.get("purpose", "turn") == "turn":
            message: dict[str, Any] = {
                "role": "assistant",
                "content": record.get("content") or "",
            }
            calls = record.get("tool_calls") or []
            if calls:
                message["tool_calls"] = [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": call.get("name", ""),
                            "arguments": call.get("arguments", "{}"),
                        },
                    }
                    for call in calls
                ]
            if message["content"] or calls:
                history.append(message)

        elif kind == "tool_result":
            clamped, _ = truncate(record.get("output") or "", max_chars, max_lines)
            history.append(
                {
                    "role": "tool",
                    "tool_call_id": record.get("id", ""),
                    "content": clamped,
                }
            )

        elif kind == "interrupted":
            history.append({"role": "user", "content": context.INTERRUPT_NOTE})

        elif kind == "max_steps_reached":
            history.append(
                {
                    "role": "user",
                    "content": context.step_limit_note(record.get("steps", 0)),
                }
            )

        elif kind == "compaction":
            kept = record.get("messages_kept")
            summarized = record.get("messages_summarized")
            if (
                isinstance(kept, int)
                and isinstance(summarized, int)
                and len(history) == kept + summarized
            ):
                summary = record.get("summary") or context.SUMMARY_FALLBACK
                tail = history[len(history) - kept :] if kept else []
                history = [context.summary_message(summary), *tail]
                stats["compactions_applied"] += 1
            else:
                # Counts do not line up with the replay (an older log, or trimming
                # happened). Keep everything; compaction will re-fire if needed.
                stats["compactions_ignored"] += 1

    return history, stats


def format_row(info: dict[str, Any]) -> str:
    started = (info.get("started") or "")[:16].replace("T", " ")
    bits = [f"{info['turns']} turns", f"{info['tool_calls']} tools"]
    if info["compactions"]:
        bits.append(f"{info['compactions']} compactions")
    if info["interrupted"]:
        bits.append("interrupted")
    detail = ", ".join(bits)
    first = info["first_message"] or "(no messages)"
    return f"{info['id']}  {started}  {detail}\n    {first}"
