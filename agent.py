"""The agent loop: user message -> model -> tools -> model -> ... -> answer."""

from __future__ import annotations

import json
import time
from typing import Any

import context
import markdown
import tools as toolkit
import ui
from config import Config
from context import INTERRUPT_NOTE, SUMMARY_FALLBACK, step_limit_note
from llm import LLM
from logbook import Logbook
from util import ToolError, ToolInterrupted, truncate


class _Printer:
    """Prints an assistant reply, streamed or whole, with optional markdown.

    Markdown rendering is line buffered, so output appears a line at a time
    instead of a token at a time; raw mode keeps the token-by-token feel.
    """

    def __init__(self, markdown_on: bool, style: bool, show_thinking: bool = False) -> None:
        self.markdown_on = markdown_on
        self.style = style
        self.show_thinking = show_thinking
        self.thinking_open = False
        self.stream = (
            markdown.StreamRenderer(self._write, style=style) if markdown_on else None
        )
        self.started = False

    def _write(self, text: str) -> None:
        if not self.started:
            ui.assistant_prefix()
            self.started = True
        print(text, end="", flush=True)

    def feed_reasoning(self, chunk: str) -> None:
        """Streamed reasoning text. Hidden unless asked for, never in history."""
        if not self.show_thinking:
            return
        if not self.thinking_open:
            print(ui.dim("thinking "), end="", flush=True)
            self.thinking_open = True
        print(ui.dim(chunk), end="", flush=True)

    def end_reasoning(self) -> None:
        if self.thinking_open:
            print()
            self.thinking_open = False

    def feed(self, chunk: str) -> None:
        if self.thinking_open:
            self.end_reasoning()
        if self.stream:
            self.stream.feed(chunk)
        else:
            self._write(chunk)

    def finish(self, content: str) -> None:
        self.end_reasoning()
        if self.stream:
            self.stream.close()
        if not self.started and content.strip():  # non-streaming reply
            body = (
                markdown.render(content, style=self.style)
                if self.markdown_on
                else content
            )
            self._write(body + "\n")
        elif self.started and not self.markdown_on:
            print()


class Agent:
    def __init__(self, cfg: Config, log: Logbook) -> None:
        self.cfg = cfg
        self.log = log
        self.llm = LLM(cfg, log)
        self.history: list[dict[str, Any]] = []
        self.last_usage: dict[str, Any] | None = None
        self.meter = context.TokenMeter(toolkit.TOOL_SCHEMAS)
        self.compactions = 0
        self.interrupted = False
        self.last_reasoning_tokens: int | None = None
        # Optional InputQueue: messages typed while the turn is running.
        self.inbox: Any = None

    def model_changed(self) -> None:
        """Forget tokenizer calibration and let dropped parameters be retried."""
        self.meter = context.TokenMeter(toolkit.TOOL_SCHEMAS)
        self.llm._supports_effort = True
        self.llm._supports_extra_body = True

    def attach_log(self, log: Logbook) -> None:
        """Point the agent and its client at another session's log."""
        self.log = log
        self.llm.log = log

    # -- history -----------------------------------------------------------
    def reset(self) -> None:
        self.history.clear()
        self.log.event("history_reset")

    def repair_history(self, reason: str = "interrupted by user") -> None:
        """Fill in missing tool results so the next request stays well formed.

        An aborted turn can leave an assistant message whose tool_calls have no
        matching tool messages, which strict APIs reject.
        """
        answered = {
            m.get("tool_call_id") for m in self.history if m.get("role") == "tool"
        }
        missing = [
            call
            for message in self.history
            if message.get("role") == "assistant"
            for call in message.get("tool_calls") or []
            if call["id"] not in answered
        ]
        for call in missing:
            self.history.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": f"ERROR: {reason}",
                }
            )
        if missing:
            self.log.event("history_repaired", count=len(missing), reason=reason)

    def _messages(self) -> list[dict[str, Any]]:
        # System prompt is rebuilt per request, so PROMPT.md edits apply live.
        # The state block is appended, never stored: the model always sees current
        # values, history keeps no stale copies, and the prompt prefix stays cached.
        return [
            {"role": "system", "content": self.cfg.system_prompt()},
            *self.history,
            # Role "user", not "system": Gemma-style templates have no system role,
            # so a trailing system message can be dropped by the server's template.
            {"role": "user", "content": self.cfg.state_block()},
        ]

    def _printer(self) -> _Printer:
        style = ui.colors_enabled()
        return _Printer(self.cfg.markdown and style, style, self.cfg.show_thinking)

    # -- context accounting ------------------------------------------------
    def used_tokens(self) -> int:
        """Estimated tokens the next request would send (system + history + tools)."""
        return self.meter.tokens(self._messages())

    def maybe_compact(self) -> bool:
        if not self.cfg.compact:
            return False
        if self.used_tokens() < self.cfg.compact_threshold:
            return False
        return self.compact(reason="threshold")

    def compact(self, reason: str = "manual") -> bool:
        """Summarize older history, keep the recent tail verbatim.

        Returns False when there is nothing worth compacting.
        """
        before = self.used_tokens()
        split = context.split_point(
            self.history, self.meter, self.cfg.compact_keep_tokens
        )
        head, tail = self.history[:split], self.history[split:]
        if not head:
            self.log.event("compaction_skipped", reason="nothing to summarize",
                           tokens=before)
            return False

        ui.info(f"⟳ compacting context ({before} tokens, {len(head)} messages)…")
        summary = self._summarize(head)
        if summary:
            self.history = [context.summary_message(summary), *tail]
        else:
            # Summarizing failed; drop the head rather than overflow the window.
            self.history = [context.summary_message(SUMMARY_FALLBACK), *tail]

        self._trim_to_fit()
        after = self.used_tokens()
        self.compactions += 1
        self.log.event(
            "compaction",
            reason=reason,
            tokens_before=before,
            tokens_after=after,
            messages_summarized=len(head),
            messages_kept=len(tail),
            chars_per_token=round(self.meter.chars_per_token, 2),
            summary=summary or "",
        )
        ui.info(
            f"⟳ context compacted: {before} → {after} tokens "
            f"({len(head)} messages summarized, {len(tail)} kept)"
        )
        return True

    def _summarize(self, head: list[dict[str, Any]]) -> str:
        """Ask the model for a handoff note covering the messages being dropped."""
        transcript = context.render_transcript(head)
        # The summary request must itself fit the window.
        budget_chars = int(self.cfg.context_tokens * 0.5 * self.meter.chars_per_token)
        transcript, _ = truncate(transcript, budget_chars, 100_000)
        try:
            reply = self.llm.complete(
                [
                    {"role": "system", "content": context.SUMMARY_SYSTEM},
                    {"role": "user", "content": transcript},
                ],
                tools=None,
                model=self.cfg.summary_model,
                purpose="compaction",
                effort=None,  # summarizing is mechanical; thinking is wasted here
            )
            return reply.content.strip()
        except Exception as exc:
            ui.warn(f"! compaction summary failed: {type(exc).__name__}: {exc}")
            self.log.event("compaction_error", error=f"{type(exc).__name__}: {exc}")
            return ""

    def _trim_to_fit(self) -> None:
        """Force the kept history under the threshold.

        Shrinks oversized tool results first: losing the tail of a big output is
        much cheaper than dropping a whole result the model just worked for. Only
        when there is nothing left to shrink does it drop oldest messages.
        """
        guard = 0
        while self.used_tokens() >= self.cfg.compact_threshold and guard < 500:
            guard += 1
            if self._shrink_largest_message():
                continue
            if len(self.history) <= 2:
                return  # nothing safe left to remove
            dropped = self.history.pop(1)  # keep [0], the summary
            # Never leave an orphaned tool result at the front of the tail.
            while len(self.history) > 1 and self.history[1].get("role") == "tool":
                self.history.pop(1)
            self.log.event(
                "history_trimmed", role=dropped.get("role"), tokens=self.used_tokens()
            )

    def _shrink_largest_message(self, floor: int = 600) -> bool:
        """Halve the biggest oversized message. False when there is nothing left.

        Tool results go first because losing the tail of a command's output is
        the cheapest thing to lose. Only when none are left does it touch a user
        or assistant message — which matters for a single pasted message larger
        than the whole window, where dropping is not an option because the last
        message always has to stay.
        """
        def oversized(roles: tuple[str, ...]) -> list[tuple[int, int]]:
            return [
                (len(m.get("content") or ""), i)
                for i, m in enumerate(self.history)
                if m.get("role") in roles and len(m.get("content") or "") > floor * 2
            ]

        candidates = oversized(("tool",)) or oversized(("user", "assistant"))
        if not candidates:
            return False
        size, index = max(candidates)
        shrunk, _ = truncate(self.history[index]["content"], max(floor, size // 2), 10**9)
        self.history[index]["content"] = shrunk
        self.log.event(
            "message_shrunk",
            index=index,
            role=self.history[index].get("role"),
            chars_before=size,
            chars_after=len(shrunk),
        )
        return True

    def _note_reasoning(self, reply: Any) -> None:
        """Say what the thinking cost when it is not being displayed."""
        # Always overwrite, including with None: otherwise a turn that did not
        # think still shows the previous turn's count in the footer.
        self.last_reasoning_tokens = reply.reasoning_tokens
        if not reply.reasoning and not reply.reasoning_tokens:
            return
        if self.cfg.show_thinking:
            return
        tokens = reply.reasoning_tokens
        size = f"{tokens} tokens" if tokens else f"{len(reply.reasoning)} chars"
        ui.info(f"  (thought for {size}; --show-thinking to see it)")

    def _warn_about_pasted_code(self, content: str, min_lines: int = 8) -> None:
        """Flag replies that dump code instead of writing it to a file."""
        lines = markdown.largest_code_block(content)
        if lines < min_lines:
            return
        ui.warn(
            f"! the model printed a {lines}-line code block instead of writing a "
            "file (tell it to use write_file / edit_file)"
        )
        self.log.event("pasted_code_block", lines=lines)

    # -- steering ----------------------------------------------------------
    def take_queued_input(self) -> int:
        """Add anything typed during the turn to the history. Returns how many."""
        if not self.inbox:
            return 0
        messages = self.inbox.drain()
        for message in messages:
            self.history.append({"role": "user", "content": message})
            self.log.event("user_message", content=message, queued=True)
            ui.info("+ your message was added to the context: "
                    + ui.one_line(message, 60))
        return len(messages)

    # -- interruption ------------------------------------------------------
    def note_interrupt(self, where: str) -> None:
        """Record an interruption so the next turn starts from a clean state."""
        self.interrupted = True
        self.repair_history(reason="interrupted by user")
        if (self.history[-1].get("content") if self.history else "") != INTERRUPT_NOTE:
            self.history.append({"role": "user", "content": INTERRUPT_NOTE})
        self.log.event("interrupted", where=where)
        ui.warn("! interrupted")

    # -- main loop ---------------------------------------------------------
    def run(self, user_message: str) -> str:
        self.history.append({"role": "user", "content": user_message})
        self.log.event("user_message", content=user_message)
        self.interrupted = False
        self.last_reasoning_tokens = None
        final_text = ""

        for step in range(1, self.cfg.max_steps + 1):
            # Pick up steering typed while the previous step was running, before
            # the next request goes out. History is well formed at this point:
            # every tool call of the last round already has its result.
            self.take_queued_input()
            self.maybe_compact()
            printer = self._printer()

            messages = self._messages()
            sent_chars = self.meter.request_chars(messages)
            try:
                reply = self.llm.complete(
                    messages,
                    toolkit.TOOL_SCHEMAS,
                    on_text=printer.feed,
                    on_reasoning=printer.feed_reasoning,
                )
            except KeyboardInterrupt:
                # Interrupted while waiting for the model; nothing partial to keep.
                printer.finish("")
                self.note_interrupt("request")
                return final_text
            printer.finish(reply.content)
            self._note_reasoning(reply)
            self._warn_about_pasted_code(reply.content)

            if reply.interrupted:
                if reply.content.strip():
                    self.history.append(reply.as_message())
                    final_text = reply.content
                self.note_interrupt("stream")
                return final_text

            if reply.usage:
                self.last_usage = reply.usage
                self.meter.calibrate(sent_chars, reply.usage.get("prompt_tokens"))
            self.history.append(reply.as_message())
            if reply.content.strip():
                final_text = reply.content

            if not reply.tool_calls:
                return final_text

            for call in reply.tool_calls:
                if self.interrupted:
                    # Don't start the remaining calls of an interrupted round, but
                    # still answer them so the history stays well formed.
                    self.history.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": "ERROR: not run, the user interrupted the turn",
                        }
                    )
                    continue
                self.history.append(self._run_tool(call, step))
            if self.interrupted:
                self.note_interrupt("tool")
                return final_text

        ui.warn(f"! stopped after {self.cfg.max_steps} tool rounds (--max-steps)")
        self.log.event("max_steps_reached", steps=self.cfg.max_steps)
        self.history.append(
            {"role": "user", "content": step_limit_note(self.cfg.max_steps)}
        )
        return final_text

    # -- tool dispatch -----------------------------------------------------
    def _run_tool(self, call: dict[str, Any], step: int) -> dict[str, Any]:
        name = call["name"]
        raw_args = call["arguments"]
        try:
            args = json.loads(raw_args) if raw_args.strip() else {}
        except json.JSONDecodeError as exc:
            args = None
            result, is_error = (
                f"ERROR: could not parse tool arguments as JSON ({exc}). "
                "Re-issue the call with valid JSON.",
                True,
            )
        if args is not None:
            ui.tool_call(name, args)
            self.log.event(
                "tool_call", step=step, id=call["id"], name=name, arguments=args
            )
            started = time.monotonic()
            try:
                result, is_error = toolkit.execute(name, args, self.cfg), False
            except ToolError as exc:
                result, is_error = f"ERROR: {exc}", True
            except ToolInterrupted as exc:
                self.interrupted = True
                partial = f"\n--- output before interrupt ---\n{exc.partial}" if exc.partial else ""
                result, is_error = f"ERROR: interrupted by user{partial}", True
            except KeyboardInterrupt:
                self.interrupted = True
                result, is_error = "ERROR: interrupted by user", True
            except Exception as exc:  # keep the session alive on tool bugs
                result, is_error = f"ERROR: {type(exc).__name__}: {exc}", True
            elapsed = time.monotonic() - started
        else:
            ui.tool_call(name, {"raw_arguments": raw_args})
            self.log.event(
                "tool_call", step=step, id=call["id"], name=name, raw_arguments=raw_args
            )
            elapsed = 0.0

        clamped, note = truncate(
            result, self.cfg.max_output_chars, self.cfg.max_output_lines
        )
        ui.tool_result(clamped, is_error=is_error)
        if note:
            ui.info(f"    {note}")
        self.log.event(
            "tool_result",
            step=step,
            id=call["id"],
            name=name,
            is_error=is_error,
            duration_s=round(elapsed, 3),
            truncated=bool(note),
            output_chars=len(result),
            output=result,  # full, untruncated, for later follow-up
        )
        return {"role": "tool", "tool_call_id": call["id"], "content": clamped}
