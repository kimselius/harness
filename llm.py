"""Thin wrapper over any OpenAI-compatible /chat/completions endpoint."""

from __future__ import annotations

from typing import Any

from openai import OpenAI

from config import Config
from logbook import Logbook


class Reply:
    """Normalized assistant turn."""

    def __init__(
        self,
        content: str,
        tool_calls: list[dict[str, Any]],
        usage: dict[str, Any] | None,
        finish_reason: str | None,
        interrupted: bool = False,
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls
        self.usage = usage
        self.finish_reason = finish_reason
        self.interrupted = interrupted

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
    def __init__(self, cfg: Config, log: Logbook) -> None:
        self.cfg = cfg
        self.log = log
        self.client = OpenAI(
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            timeout=cfg.request_timeout,
            max_retries=3,
        )
        self._supports_stream_usage = True

    def _params(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        model: str | None = None,
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
        return params

    def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        on_text: Any = None,
        model: str | None = None,
        purpose: str = "turn",
    ) -> Reply:
        """One model call. on_text(chunk) is called with streamed text deltas."""
        params = self._params(messages, tools, model=model)
        self.log.event(
            "llm_request",
            model=params["model"],
            purpose=purpose,
            stream=self.cfg.stream,
            messages=messages,
            tool_names=[t["function"]["name"] for t in tools or []],
        )
        try:
            if self.cfg.stream:
                reply = self._stream(params, on_text)
            else:
                reply = self._once(params)
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
        )
        return reply

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
        return Reply(choice.message.content or "", calls, usage, choice.finish_reason)

    def _stream(self, params: dict, on_text: Any) -> Reply:
        kwargs = dict(params, stream=True)
        if self._supports_stream_usage:
            kwargs["stream_options"] = {"include_usage": True}
        try:
            stream = self.client.chat.completions.create(**kwargs)
        except Exception:
            if not self._supports_stream_usage:
                raise
            # Some compatible servers reject stream_options; retry without it once.
            self._supports_stream_usage = False
            kwargs.pop("stream_options", None)
            stream = self.client.chat.completions.create(**kwargs)

        text_parts: list[str] = []
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
            return Reply("".join(text_parts), [], usage, "interrupted", True)

        calls = [
            {
                "id": slot["id"] or f"call_{index}",
                "name": slot["name"],
                "arguments": slot["arguments"] or "{}",
            }
            for index, slot in sorted(slots.items())
        ]
        return Reply("".join(text_parts), calls, usage, finish_reason)
