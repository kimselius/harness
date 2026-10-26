"""Context accounting and history compaction.

Token counts are estimated from characters and then self-calibrated against the
`prompt_tokens` the server reports, so the estimate converges on the real
tokenizer within a couple of requests instead of relying on a fixed guess.
"""

from __future__ import annotations

import json
from typing import Any

# Starting guess, replaced by measured values once usage comes back.
DEFAULT_CHARS_PER_TOKEN = 3.6
MIN_CHARS_PER_TOKEN = 1.5
MAX_CHARS_PER_TOKEN = 8.0

# Per-message cap when rendering old tool output into the summary request.
RENDER_TOOL_CHARS = 800

SUMMARY_SYSTEM = """\
You compact an agent transcript so work can continue with a shorter context.

Write a dense handoff note, no preamble, no pleasantries. Cover, in this order and
only where applicable:
1. The user's goal and any explicit instructions or constraints they gave.
2. What has been done so far, including commands run and their outcomes.
3. Files created or modified, with their paths and current state.
4. Facts discovered about the environment that would be expensive to rediscover
   (versions, paths, config values, error messages, credentials locations).
5. What is still open: the next step, and anything known to be broken.

Preserve exact paths, names, flags and numbers. Drop conversational filler and
superseded intermediate attempts. Prefer short lines over prose.
"""

SUMMARY_HEADER = "[context summary of the earlier part of this session]"

# Used when the summary call itself fails: better to drop the old messages with a
# marker than to overflow the window.
SUMMARY_FALLBACK = (
    "(summary unavailable: earlier messages were dropped to stay within the "
    "context window; ask the user if you need details)"
)

# Harness-authored messages. They live here because session replay has to be able
# to reinsert them without importing the agent loop.
INTERRUPT_NOTE = (
    "[harness] The user interrupted you. Whatever you were doing was stopped, and "
    "any command still running was killed. Do not resume it. Wait for new "
    "instructions."
)


def step_limit_note(max_steps: int) -> str:
    return (
        f"[harness] Step limit ({max_steps}) reached. Stop calling tools and "
        "summarize what you did and what is left."
    )


RESUME_NOTE = (
    "[harness] This session was resumed in a new process. Everything above is "
    "earlier history, so do not assume a command you ran before is still running."
)


class TokenMeter:
    """Character-based token estimator, calibrated from server usage reports."""

    def __init__(self, tools: list[dict] | None = None) -> None:
        self.chars_per_token = DEFAULT_CHARS_PER_TOKEN
        self.tools_chars = len(json.dumps(tools or [], default=str))
        self.samples = 0
        self.last_prompt_tokens: int | None = None

    @staticmethod
    def chars(messages: list[dict[str, Any]]) -> int:
        return sum(len(json.dumps(m, default=str)) for m in messages)

    def request_chars(self, messages: list[dict[str, Any]]) -> int:
        """Characters actually put on the wire: messages plus tool schemas."""
        return self.chars(messages) + self.tools_chars

    def tokens(self, messages: list[dict[str, Any]], with_tools: bool = True) -> int:
        chars = self.chars(messages) + (self.tools_chars if with_tools else 0)
        return int(chars / self.chars_per_token)

    def calibrate(self, request_chars: int, prompt_tokens: int | None) -> None:
        if not prompt_tokens or prompt_tokens < 50 or request_chars <= 0:
            return
        self.last_prompt_tokens = prompt_tokens
        observed = max(
            MIN_CHARS_PER_TOKEN, min(MAX_CHARS_PER_TOKEN, request_chars / prompt_tokens)
        )
        if self.samples == 0:
            self.chars_per_token = observed
        else:
            self.chars_per_token = 0.6 * self.chars_per_token + 0.4 * observed
        self.samples += 1


def split_point(
    history: list[dict[str, Any]], meter: TokenMeter, keep_tokens: int
) -> int:
    """Index where the verbatim tail starts.

    Walks back from the end until `keep_tokens` is reached, then moves forward to
    the next safe boundary. The tail may not begin with a `tool` message: its
    parent assistant message carries the matching tool_call and would be gone.
    """
    if not history:
        return 0
    budget = 0
    start = len(history)
    for i in range(len(history) - 1, -1, -1):
        budget += meter.tokens([history[i]], with_tools=False)
        if budget > keep_tokens:
            break
        start = i
    # Prefer moving forward (summarize more), which pushes stray tool results into
    # the head alongside the assistant message that called them.
    while start < len(history) and history[start].get("role") == "tool":
        start += 1
    # Never summarize away everything.
    start = min(start, len(history) - 1)
    # If that landed on a tool result, pull its parent assistant into the tail too.
    while start > 0 and history[start].get("role") == "tool":
        start -= 1
    return start


def render_transcript(
    messages: list[dict[str, Any]], tool_chars: int = RENDER_TOOL_CHARS
) -> str:
    """Flatten messages into text for the summarizer, capping old tool output."""
    lines: list[str] = []
    for message in messages:
        role = message.get("role", "?")
        content = (message.get("content") or "").strip()
        if role == "tool":
            body = content[:tool_chars]
            if len(content) > tool_chars:
                body += f"\n...[{len(content) - tool_chars} chars omitted]"
            lines.append(f"TOOL RESULT: {body}")
            continue
        if content:
            lines.append(f"{role.upper()}: {content}")
        for call in message.get("tool_calls") or []:
            fn = call.get("function", {})
            args = fn.get("arguments", "")
            if len(args) > tool_chars:
                args = args[:tool_chars] + f"...[{len(args) - tool_chars} chars omitted]"
            lines.append(f"ASSISTANT TOOL CALL {fn.get('name', '?')}: {args}")
    return "\n".join(lines)


def summary_message(text: str) -> dict[str, Any]:
    """The replacement message that stands in for everything summarized."""
    return {"role": "user", "content": f"{SUMMARY_HEADER}\n{text.strip()}"}
