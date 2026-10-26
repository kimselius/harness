"""Output truncation and workspace path confinement."""

from __future__ import annotations

from pathlib import Path


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


def resolve_in_workspace(root: Path, raw: str) -> Path:
    """Resolve a model-supplied path, refusing anything outside the workspace."""
    if not raw or not raw.strip():
        raise ToolError("path must not be empty")
    root = root.resolve()
    candidate = Path(raw.strip()).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
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
