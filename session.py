"""Per-run mutable state, kept out of Config.

Config is settings: flags and limits decided at startup. This is the state that
changes while the harness runs — where the shell is standing, and what the session
has spent. It used to live on Config, which meant `tools.py` reached into the
settings object to move the working directory; that coupling is how file paths
ended up resolving against the wrong anchor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Session:
    root: Path  # the workspace; nothing may move outside it
    cwd: Path = field(init=False)
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_thinking: int = 0
    requests: int = 0

    def __post_init__(self) -> None:
        self.root = self.root.resolve()
        self.cwd = self.root

    # -- working directory -------------------------------------------------
    def set_cwd(self, path: Path) -> bool:
        """Move the shell's directory. False if the path escaped the workspace."""
        resolved = path.resolve()
        if resolved != self.root and self.root not in resolved.parents:
            self.cwd = self.root
            return False
        self.cwd = resolved
        return True

    @property
    def cwd_label(self) -> str:
        """The directory as the model and the prompt see it: relative, "." at root."""
        if self.cwd == self.root:
            return "."
        try:
            return str(self.cwd.relative_to(self.root))
        except ValueError:
            return str(self.cwd)

    def state_block(self) -> str:
        """Authoritative state, sent as the last message of every request.

        It is appended rather than folded into the system prompt so the prompt
        stays byte-identical all session and the server's cached prefix survives,
        while the values stay current every turn.
        """
        return (
            "<state>\n"
            f"workspace root: {self.root}\n"
            f"file tool paths resolve from: {self.root} (always)\n"
            f"shell working directory: {self.cwd} ({self.cwd_label})\n"
            "</state>"
        )

    # -- accounting --------------------------------------------------------
    def add_usage(self, usage: dict[str, Any] | None, thinking: int | None) -> None:
        """Accumulate one response's token usage."""
        self.requests += 1
        if not usage:
            return
        for key, field_name in (
            ("prompt_tokens", "tokens_in"),
            ("completion_tokens", "tokens_out"),
        ):
            value = usage.get(key)
            if isinstance(value, int):
                setattr(self, field_name, getattr(self, field_name) + value)
        if isinstance(thinking, int):
            self.tokens_thinking += thinking

    def totals_line(self) -> str:
        def short(count: int) -> str:
            return f"{count / 1000:.1f}k" if count >= 1000 else str(count)

        parts = [f"in {short(self.tokens_in)}", f"out {short(self.tokens_out)}"]
        if self.tokens_thinking:
            parts.append(f"thinking {short(self.tokens_thinking)}")
        return f"{' · '.join(parts)} over {self.requests} requests"

    def totals(self) -> dict[str, int]:
        return {
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "tokens_thinking": self.tokens_thinking,
            "requests": self.requests,
        }
