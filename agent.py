"""The agent loop: user message -> model -> tools -> model -> ... -> answer."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import context
import markdown
import tools as toolkit
import ui
from config import Config
from context import INTERRUPT_NOTE, SUMMARY_FALLBACK, deadline_note, step_limit_note
from llm import LLM
from logbook import Logbook
from session import Session
from util import ToolError, ToolInterrupted, truncate

# How a turn ended. `ok` is the only status that means the model decided it was
# finished; everything else means the turn was cut short, which a caller driving
# the harness from a script needs to be able to tell apart.
OK = "ok"
ERROR = "error"
MAX_STEPS = "max_steps"
INTERRUPTED = "interrupted"
TIMEOUT = "timeout"


@dataclass
class Outcome:
    """The result of one turn, for callers that are not a human at a terminal."""

    status: str = OK
    text: str = ""
    steps: int = 0
    tool_calls: int = 0
    error: str | None = None
    tokens: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == OK


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
        # Nothing opens the reply with blank lines, so a leading run of them is
        # model padding. Dropping it here covers the raw (--no-markdown) path,
        # where there is no line renderer to hold them back.
        if not self.started and not text.strip():
            return
        if not self.started:
            ui.assistant_prefix()
            self.started = True
        print(text, end="", flush=True)

    def feed_reasoning(self, chunk: str) -> None:
        """Streamed reasoning text. Hidden unless asked for, never in history.

        Goes to stderr: it is commentary on the work, not the answer, so it must
        not land in a redirected stdout.
        """
        if not self.show_thinking:
            return
        if not self.thinking_open:
            ui.narrate("thinking ")
            self.thinking_open = True
        ui.narrate(chunk)

    def end_reasoning(self) -> None:
        if self.thinking_open:
            ui.narrate("\n")
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
        self.llm = LLM(cfg, log, notify=ui.warn)
        self.history: list[dict[str, Any]] = []
        self.last_usage: dict[str, Any] | None = None
        self.session = Session(cfg.workspace)
        # The harness's own credentials are of no use to the model.
        self.redactor = cfg.redactor()
        self.meter = context.TokenMeter(toolkit.TOOL_SCHEMAS)
        self.compactions = 0
        self.interrupted = False
        self.last_reasoning_tokens: int | None = None
        # How the last turn ended. Set at every exit of run().
        self.outcome = Outcome()
        # Optional InputQueue: messages typed while the turn is running.
        self.inbox: Any = None

    def model_changed(self) -> None:
        """Forget tokenizer calibration and let dropped parameters be retried."""
        self.meter = context.TokenMeter(toolkit.TOOL_SCHEMAS)
        self.llm.supports_effort = True
        self.llm.supports_extra_body = True

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
            {"role": "user", "content": self.session.state_block()},
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

        ui.info(
            f"⟳ compacting context ({before} tokens, {len(head)} of "
            f"{len(self.history)} messages; keeping the newest {len(tail)})"
        )
        # The opening request is pinned verbatim ahead of the summary. Everything
        # else about the past is a paraphrase; the job itself must not be.
        pinned = self._pin_task(head)
        summary = self._summarize(head)
        replacement = context.summary_message(summary or SUMMARY_FALLBACK)
        self.history = [*([pinned] if pinned else []), replacement, *tail]

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
            # Recorded verbatim so a replay rebuilds the identical history: the
            # pinned copy is capped, and re-deriving it would use a different cap.
            pinned_task=pinned["content"] if pinned else None,
        )
        ui.info(
            f"⟳ context compacted: {before} → {after} tokens "
            f"({len(head)} messages summarized, {len(tail)} kept)"
        )
        if after >= self.cfg.compact_threshold:
            # Compacting and landing back above the trigger means the next turn
            # compacts again immediately. Say so: the window is too small for
            # what this session is holding.
            ui.warn(
                f"! still at {after} tokens, over the {self.cfg.compact_threshold} "
                "trigger — raise --context-tokens or lower --compact-keep, or the "
                "next turn will compact again"
            )
        return True

    def _pin_task(self, head: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Copy of the user's opening request, to survive this compaction.

        Capped, because a huge pasted brief must not claim the whole window back
        on every compaction. The cap is generous: losing the end of the request is
        still worse than losing a few recent messages.
        """
        task = context.first_task(head)
        if task is None:
            return None
        budget = max(500, self.cfg.compact_keep_tokens // 3)
        text, note = truncate(
            task["content"], int(budget * self.meter.chars_per_token), 10**9
        )
        if note:
            self.log.event("pinned_task_truncated", chars=len(text))
        return {"role": "user", "content": text}

    def _summarize(self, head: list[dict[str, Any]]) -> str:
        """Ask the model for a handoff note covering the messages being dropped."""
        transcript = context.render_transcript(head)
        # The summary request must itself fit the window.
        budget_chars = int(self.cfg.context_tokens * 0.5 * self.meter.chars_per_token)
        transcript, _ = truncate(transcript, budget_chars, 100_000)
        sent = int(len(transcript) / self.meter.chars_per_token)
        model = self.cfg.summary_model or self.cfg.model
        # This is the longest unexplained pause in the harness: the server has to
        # prefill the whole transcript before the first token of the summary comes
        # back, which is minutes on a large local model. Show that it is alive.
        ticker = ui.Ticker(f"⟳ summarizing ~{sent} tokens with {model} ·")
        written = 0

        def on_text(chunk: str) -> None:
            nonlocal written
            written += len(chunk)
            ticker.set(f" {written} chars written")

        ticker.start()
        summary = ""
        try:
            reply = self.llm.complete(
                [
                    {"role": "system", "content": context.SUMMARY_SYSTEM},
                    {"role": "user", "content": transcript},
                ],
                tools=None,
                on_text=on_text,
                model=self.cfg.summary_model,
                purpose="compaction",
                effort=None,  # summarizing is mechanical; thinking is wasted here
            )
            summary = reply.content.strip()
            return summary
        except KeyboardInterrupt:
            ui.warn("! compaction interrupted")
            raise
        except Exception as exc:
            ui.warn(f"! compaction summary failed: {type(exc).__name__}: {exc}")
            self.log.event("compaction_error", error=f"{type(exc).__name__}: {exc}")
            return ""
        finally:
            ticker.stop()
            self.log.event(
                "compaction_summary_done",
                seconds=round(ticker.elapsed, 1),
                sent_tokens=sent,
                # The real size either way; `streamed_chars` is 0 with
                # --no-stream, where there are no deltas to count.
                summary_chars=len(summary),
                streamed_chars=written,
                model=model,
            )

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
    def _finish(
        self, status: str, text: str, steps: int, calls: int, error: str | None = None
    ) -> str:
        """Record how the turn ended, then hand the text back as before."""
        self.outcome = Outcome(
            status=status,
            text=text,
            steps=steps,
            tool_calls=calls,
            error=error,
            tokens=self.session.totals(),
        )
        self.log.event(
            "turn_end", status=status, steps=steps, tool_calls=calls, error=error
        )
        return text

    def run(self, user_message: str) -> str:
        self.history.append({"role": "user", "content": user_message})
        self.log.event("user_message", content=user_message)
        self.interrupted = False
        self.last_reasoning_tokens = None
        final_text = ""
        calls = 0
        step = 0
        started = time.monotonic()

        for step in range(1, self.cfg.max_steps + 1):
            if self._past_deadline(started):
                return self._finish(TIMEOUT, final_text, step - 1, calls)
            # Pick up steering typed while the previous step was running, before
            # the next request goes out. History is well formed at this point:
            # every tool call of the last round already has its result.
            self.take_queued_input()
            self.maybe_compact()
            printer = self._printer()

            messages = self._messages()
            sent_chars = self.meter.request_chars(messages)
            # Prefill is silent, and on a big prompt it is the longest wait in a
            # turn. Tick until the first byte of the reply arrives, then get out
            # of the way so the reply owns the line.
            waiting = ui.Ticker(
                f"⟳ thinking · step {step} · ~{self.used_tokens()} tokens sent ·"
            )
            waiting.start()

            # Both the ticker and the printer are bound as defaults: they belong
            # to this iteration, and a closure over the loop variable would see
            # whatever the last iteration left behind.
            def first_byte(chunk: str, feed=printer.feed, tick=waiting) -> None:
                tick.stop()
                feed(chunk)

            def first_thought(
                chunk: str, feed=printer.feed_reasoning, tick=waiting
            ) -> None:
                tick.stop()
                feed(chunk)

            try:
                reply = self.llm.complete(
                    messages,
                    toolkit.TOOL_SCHEMAS,
                    on_text=first_byte,
                    on_reasoning=first_thought,
                )
            except KeyboardInterrupt:
                # Interrupted while waiting for the model; nothing partial to keep.
                printer.finish("")
                self.note_interrupt("request")
                return self._finish(INTERRUPTED, final_text, step, calls)
            finally:
                # Also covers the non-streaming path, where no chunk ever arrives
                # to stop it, and any error on the way out.
                waiting.stop()
            printer.finish(reply.content)
            self._note_reasoning(reply)
            self._warn_about_pasted_code(reply.content)

            if reply.interrupted:
                if reply.content.strip():
                    self.history.append(reply.as_message())
                    final_text = reply.content
                self.note_interrupt("stream")
                return self._finish(INTERRUPTED, final_text, step, calls)

            self.session.add_usage(reply.usage, reply.reasoning_tokens)
            if reply.usage:
                self.last_usage = reply.usage
                self.meter.calibrate(sent_chars, reply.usage.get("prompt_tokens"))
            self.history.append(reply.as_message())
            if reply.content.strip():
                final_text = reply.content

            if not reply.tool_calls:
                return self._finish(OK, final_text, step, calls)

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
                calls += 1
                self.history.append(self._run_tool(call, step))
            if self.interrupted:
                self.note_interrupt("tool")
                return self._finish(INTERRUPTED, final_text, step, calls)

        ui.warn(f"! stopped after {self.cfg.max_steps} tool rounds (--max-steps)")
        self.log.event("max_steps_reached", steps=self.cfg.max_steps)
        self.history.append(
            {"role": "user", "content": step_limit_note(self.cfg.max_steps)}
        )
        return self._finish(MAX_STEPS, self._wrap_up(final_text), step, calls)

    def _wrap_up(self, fallback: str) -> str:
        """One tool-free turn, so a run that hit the ceiling still reports back.

        Without this the turn ended on whatever text happened to be lying around,
        which after a round of pure tool calls is often a dangling intent
        ("Let me check one more thing…") or nothing at all — and that string is
        what a driving script reads as the result. The note asking for a summary
        was already being appended to the history and then never sent, so only a
        human continuing the session ever saw it answered.

        Tools are withheld rather than discouraged. That is what makes this
        terminate: a model that is looping cannot loop one more time.
        """
        self.maybe_compact()  # the history is at its largest right now
        printer = self._printer()
        ticker = ui.Ticker("⟳ asking what it got done ·")
        ticker.start()

        def first(chunk: str, feed=printer.feed, tick=ticker) -> None:
            tick.stop()
            feed(chunk)

        try:
            reply = self.llm.complete(
                self._messages(), tools=None, on_text=first, purpose="wrapup"
            )
        except KeyboardInterrupt:
            printer.finish("")
            self.note_interrupt("wrapup")
            return fallback
        except Exception as exc:
            # A failed wrap-up must not turn a step-limit stop into a crash.
            ui.warn(f"! could not get a closing summary: {type(exc).__name__}: {exc}")
            self.log.event("wrapup_error", error=f"{type(exc).__name__}: {exc}")
            return fallback
        finally:
            ticker.stop()

        printer.finish(reply.content)
        self._note_reasoning(reply)
        self.session.add_usage(reply.usage, reply.reasoning_tokens)
        if reply.usage:
            self.last_usage = reply.usage
        if reply.tool_calls:
            # Some servers answer with tool calls even when none were offered.
            # They are deliberately not run, so they must not be stored either:
            # an unanswered tool_calls message is an invalid history.
            self.log.event("wrapup_tool_calls_ignored",
                           names=[c["name"] for c in reply.tool_calls])
        self.history.append({"role": "assistant", "content": reply.content})
        return reply.content.strip() or fallback

    def _past_deadline(self, started: float) -> bool:
        """Whether --max-seconds has run out. Checked between steps only.

        A tool already running is not cut short here; `--shell-timeout` bounds
        that, so the worst case is one command's timeout past the deadline.
        """
        limit = self.cfg.max_seconds
        if not limit:
            return False
        elapsed = time.monotonic() - started
        if elapsed < limit:
            return False
        ui.warn(f"! stopped after {elapsed:.0f}s (--max-seconds {limit:g})")
        self.log.event("deadline_reached", limit=limit, elapsed=round(elapsed, 1))
        self.history.append({"role": "user", "content": deadline_note(limit)})
        return True

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
                result, is_error = toolkit.execute(name, args, self.cfg, self.session), False
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

        result = self.redactor.scrub_output(result)
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
