"""Output truncation, workspace path confinement, and secret redaction."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

REDACTED = "[redacted]"

# High-confidence shapes only. A false positive here damages a transcript you
# may need later, so this deliberately does not try to catch everything.
SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.S,
    ),
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}"),
]

# `NAME=value` / `NAME: value` for names that are secrets by convention. Only the
# value is replaced, so the surrounding line still reads sensibly.
SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([a-z0-9_]*(?:api[_-]?key|secret|password|passwd|token|credential)"
    r"[a-z0-9_]*)(\s*[:=]\s*)(['\"]?)([^\s'\"]{8,})\3"
)

# https://user:password@host
URL_PASSWORD = re.compile(r"(://[^/\s:@]+:)([^/\s@]+)(@)")


class Redactor:
    """Keeps known secrets out of the session log.

    Two levels on purpose:

    - `scrub_output` removes only *our own* literal secrets (the API key, a
      password in base_url). Those are never useful to the model, so they are
      removed everywhere, including what the model sees.
    - `scrub_log` additionally removes anything matching a secret shape. That is
      applied to the log only: the agent may legitimately need to use a token it
      found, but the log is the artifact that leaves the container.
    """

    def __init__(self, secrets: Iterable[str] = (), patterns: bool = True) -> None:
        # Short strings would match everywhere; "sk-no-key" style placeholders are
        # not worth redacting either.
        self.secrets = sorted(
            {s for s in secrets if s and len(s) >= 12}, key=len, reverse=True
        )
        self.patterns = patterns

    def scrub_output(self, text: str) -> str:
        """Remove the harness's own secrets from text shown to anyone."""
        if not isinstance(text, str):
            return text
        for secret in self.secrets:
            if secret in text:
                text = text.replace(secret, REDACTED)
        return text

    def scrub_log(self, value: Any) -> Any:
        """Scrub anything on its way into the log, walking containers."""
        if isinstance(value, str):
            text = self.scrub_output(value)
            if not self.patterns:
                return text
            for pattern in SECRET_PATTERNS:
                text = pattern.sub(REDACTED, text)
            text = SECRET_ASSIGNMENT.sub(rf"\1\2\3{REDACTED}\3", text)
            return URL_PASSWORD.sub(rf"\1{REDACTED}\3", text)
        if isinstance(value, dict):
            return {key: self.scrub_log(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.scrub_log(item) for item in value]
        return value


class ToolError(Exception):
    """Raised by tools for errors the model should see and recover from."""


class ToolInterrupted(Exception):
    """Raised when the user interrupts a running tool.

    Carries whatever output was produced before the interrupt so the model (and
    the user) can see how far it got.
    """

    def __init__(self, partial: str = "") -> None:
        super().__init__("interrupted by user")
        self.partial = partial


def truncate(text: str, max_chars: int, max_lines: int) -> tuple[str, str | None]:
    """Clamp text to limits, keeping head and tail.

    Returns (clamped_text, note) where note is None if nothing was cut.
    """
    lines = text.splitlines()
    notes: list[str] = []

    if len(lines) > max_lines:
        head = max_lines * 7 // 10
        tail = max_lines - head
        omitted = len(lines) - max_lines
        lines = lines[:head] + [f"... [{omitted} lines omitted] ..."] + lines[-tail:]
        notes.append(f"{omitted} lines")
        text = "\n".join(lines)

    if len(text) > max_chars:
        head = max_chars * 7 // 10
        tail = max_chars - head
        omitted = len(text) - max_chars
        text = f"{text[:head]}\n... [{omitted} characters omitted] ...\n{text[-tail:]}"
        notes.append(f"{omitted} characters")

    if not notes:
        return text, None
    note = (
        f"[output truncated: {' and '.join(notes)} omitted; "
        "full output is in the session log]"
    )
    return f"{text}\n{note}", note


def resolve_in_workspace(root: Path, raw: str, base: Path | None = None) -> Path:
    """Resolve a model-supplied path, refusing anything outside the workspace.

    Relative paths resolve against `base` (the session's working directory) and
    fall back to the workspace root.
    """
    if not raw or not raw.strip():
        raise ToolError("path must not be empty")
    root = root.resolve()
    start = (base or root).resolve()
    candidate = Path(raw.strip()).expanduser()
    if not candidate.is_absolute():
        candidate = start / candidate
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise ToolError(
            f"path {raw!r} resolves outside the workspace root {root}; "
            "use a relative path inside the workspace"
        )
    return resolved


def display_path(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
