"""Thin wrapper over any OpenAI-compatible /chat/completions endpoint."""

from __future__ import annotations

from typing import Any

from openai import BadRequestError, OpenAI

from config import Config
from logbook import Logbook

# Sentinel for "use the configured effort"; None means "send no effort at all".
INHERIT = "__inherit__"

# Servers disagree on the field name for chain-of-thought text. These are the ones
# in the wild: reasoning_content (Ollama, vLLM, DeepSeek), reasoning (OpenRouter),
# thinking (some proxies).
REASONING_FIELDS = ("reasoning_content", "reasoning", "thinking")


def _extra(obj: Any, names: tuple[str, ...]) -> str:
    """First non-empty value among `names`, whether a real field or an extra one."""
    if obj is None:
        return ""
    for name in names:
        value = getattr(obj, name, None)
        if isinstance(value, str) and value:
            return value
    extra = getattr(obj, "model_extra", None) or {}
    for name in names:
        value = extra.get(name)
        if isinstance(value, str) and value:
            return value
    return ""


def reasoning_tokens(usage: dict[str, Any] | None) -> int | None:
    """Reasoning tokens from either the OpenAI shape or the flat one."""
    if not usage:
        return None
    details = usage.get("completion_tokens_details") or {}
    for value in (details.get("reasoning_tokens"), usage.get("reasoning_tokens")):
        if isinstance(value, int):
            return value
    return None


class Reply:
    """Normalized assistant turn."""

    def __init__(
        self,
        content: str,
        tool_calls: list[dict[str, Any]],
        usage: dict[str, Any] | None,
        finish_reason: str | None,
        interrupted: bool = False,
        reasoning: str = "",
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls
        self.usage = usage
        self.finish_reason = finish_reason
        self.interrupted = interrupted
        # Captured for the log and the terminal, never sent back: OpenAI-compatible
        # APIs do not accept reasoning in an assistant message, and replaying it
        # would waste context.
        self.reasoning = reasoning

    @property
    def reasoning_tokens(self) -> int | None:
        return reasoning_tokens(self.usage)

    def as_message(self) -> dict[str, Any]:
        """Assistant message to append to history (provider-safe subset)."""
        message: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": call["arguments"],
                    },
                }
                for call in self.tool_calls
            ]
        return message


class LLM:
    def __init__(self, cfg: Config, log: Logbook, notify: Any = None) -> None:
        self.cfg = cfg
        self.log = log
        # How to tell the user something, without this layer knowing about the
        # terminal. Defaults to silence so library use stays quiet.
        self.notify = notify or (lambda message: None)
        self.client = OpenAI(
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            timeout=cfg.request_timeout,
            max_retries=3,
        )
        self.supports_stream_usage = True
        self.supports_effort = True
        self.supports_extra_body = True

    def _params(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        model: str | None = None,
        effort: str | None = INHERIT,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": model or self.cfg.model,
            "messages": messages,
        }
        if tools:  # an empty list upsets some compatible servers
            params["tools"] = tools
        if self.cfg.temperature is not None:
            params["temperature"] = self.cfg.temperature
        if self.cfg.max_tokens is not None:
            params["max_tokens"] = self.cfg.max_tokens

        chosen = self.cfg.reasoning_effort if effort == INHERIT else effort
        if chosen and self.supports_effort:
            params["reasoning_effort"] = chosen
        if self.cfg.extra_body and self.supports_extra_body:
            # Must go through the SDK's extra_body: create() is typed and rejects
            # unknown keyword arguments outright.
            params["extra_body"] = dict(self.cfg.extra_body)
        return params

    def _without_unsupported(
        self, params: dict[str, Any], error: Exception
    ) -> dict[str, Any] | None:
        """Drop provider-specific fields after a rejection.

        Returns None when the rejection has nothing to do with the fields we
        added, so an unrelated 400 (a bad model name, a malformed message) keeps
        its own error instead of being reported as an unsupported parameter.
        """
        sent = [name for name in ("reasoning_effort",) if name in params]
        sent += sorted(self.cfg.extra_body or {}) if params.get("extra_body") else []
        if not sent:
            return None
        blamed = [name for name in sent if name in str(error)]
        if not blamed:
            # The server did not name any of them, so assume they are innocent.
            return None

        params = dict(params)
        dropped = []
        if "reasoning_effort" in blamed:
            params.pop("reasoning_effort", None)
            self.supports_effort = False
            dropped.append("reasoning_effort")
        if any(key in blamed for key in (self.cfg.extra_body or {})):
            params.pop("extra_body", None)
            self.supports_extra_body = False
            dropped.extend(sorted(self.cfg.extra_body or {}))
        if not dropped:
            return None
        self.log.event("params_dropped", fields=dropped, reason=str(error)[:200])
        self.notify(
            f"! this endpoint rejected {', '.join(dropped)}; continuing without"
        )
        return params

    def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        on_text: Any = None,
        model: str | None = None,
        purpose: str = "turn",
        effort: str | None = INHERIT,
        on_reasoning: Any = None,
    ) -> Reply:
        """One model call.

        on_text(chunk) and on_reasoning(chunk) receive streamed deltas. Pass
        effort=None to send no reasoning_effort for this call.
        """
        params = self._params(messages, tools, model=model, effort=effort)
        self.log.event(
            "llm_request",
            model=params["model"],
            purpose=purpose,
            stream=self.cfg.stream,
            reasoning_effort=params.get("reasoning_effort"),
            messages=messages,
            tool_names=[t["function"]["name"] for t in tools or []],
        )
        try:
            reply = self._dispatch(params, on_text, on_reasoning)
        except BadRequestError as exc:
            # Some OpenAI-compatible servers reject reasoning_effort outright.
            # Drop the fields they name and retry once, rather than failing the
            # turn — but only when the error actually blames one of them.
            retry = self._without_unsupported(params, exc)
            if retry is None:
                self.log.event("llm_error", error=f"{type(exc).__name__}: {exc}")
                raise
            reply = self._dispatch(retry, on_text, on_reasoning)
        except Exception as exc:
            self.log.event("llm_error", error=f"{type(exc).__name__}: {exc}")
            raise

        self.log.event(
            "llm_response",
            model=params["model"],
            purpose=purpose,
            content=reply.content,
            tool_calls=reply.tool_calls,
            finish_reason=reply.finish_reason,
            usage=reply.usage,
            interrupted=reply.interrupted,
            reasoning=reply.reasoning,
            reasoning_chars=len(reply.reasoning),
            reasoning_tokens=reply.reasoning_tokens,
        )
        return reply

    def _dispatch(self, params: dict, on_text: Any, on_reasoning: Any) -> Reply:
        if self.cfg.stream:
            return self._stream(params, on_text, on_reasoning)
        return self._once(params)

    def _once(self, params: dict) -> Reply:
        response = self.client.chat.completions.create(**params)
        if not response.choices:
            raise RuntimeError("model returned no choices")
        choice = response.choices[0]
        calls = [
            {
                "id": tc.id or f"call_{i}",
                "name": tc.function.name or "",
                "arguments": tc.function.arguments or "{}",
            }
            for i, tc in enumerate(choice.message.tool_calls or [])
        ]
        usage = response.usage.model_dump() if response.usage else None
        return Reply(
            choice.message.content or "",
            calls,
            usage,
            choice.finish_reason,
            reasoning=_extra(choice.message, REASONING_FIELDS),
        )

    def _stream(self, params: dict, on_text: Any, on_reasoning: Any = None) -> Reply:
        kwargs = dict(params, stream=True)
        if self.supports_stream_usage:
            kwargs["stream_options"] = {"include_usage": True}
        try:
            stream = self.client.chat.completions.create(**kwargs)
        except Exception:
            if not self.supports_stream_usage:
                raise
            # Some compatible servers reject stream_options; retry without it once.
            self.supports_stream_usage = False
            kwargs.pop("stream_options", None)
            stream = self.client.chat.completions.create(**kwargs)

        text_parts: list[str] = []
        thought_parts: list[str] = []
        slots: dict[int, dict[str, str]] = {}
        usage: dict[str, Any] | None = None
        finish_reason: str | None = None
        interrupted = False

        try:
            for chunk in stream:
                if getattr(chunk, "usage", None):
                    usage = chunk.usage.model_dump()
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
                delta = choice.delta
                if delta is None:
                    continue
                thought = _extra(delta, REASONING_FIELDS)
                if thought:
                    thought_parts.append(thought)
                    if on_reasoning:
                        on_reasoning(thought)
                if delta.content:
                    text_parts.append(delta.content)
                    if on_text:
                        on_text(delta.content)
                for tc in delta.tool_calls or []:
                    slot = slots.setdefault(
                        tc.index, {"id": "", "name": "", "arguments": ""}
                    )
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            slot["name"] += tc.function.name
                        if tc.function.arguments:
                            slot["arguments"] += tc.function.arguments
        except KeyboardInterrupt:
            # Keep whatever text arrived, but drop half-built tool calls: their
            # arguments are truncated JSON and must never be executed.
            interrupted = True
            slots.clear()
        finally:
            try:
                stream.close()
            except Exception:
                pass

        if interrupted:
            return Reply(
                "".join(text_parts), [], usage, "interrupted", True,
                reasoning="".join(thought_parts),
            )

        calls = [
            {
                "id": slot["id"] or f"call_{index}",
                "name": slot["name"],
                "arguments": slot["arguments"] or "{}",
            }
            for index, slot in sorted(slots.items())
        ]
        return Reply(
            "".join(text_parts), calls, usage, finish_reason,
            reasoning="".join(thought_parts),
        )
