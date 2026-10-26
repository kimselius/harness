"""Just enough markdown-to-ANSI rendering for terminal replies.

Line oriented on purpose: a streaming reply is rendered one completed line at a
time, which keeps inline spans intact even when they arrive split across chunks.
Code block contents are passed through untouched so they stay copy-pasteable.
"""

from __future__ import annotations

import re
import shutil

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
ITALIC = "\033[3m"
UNDERLINE = "\033[4m"
STRIKE = "\033[9m"
CYAN = "\033[36m"

FENCE = re.compile(r"^(\s*)(```|~~~)\s*(\S*)\s*$")
HEADING = re.compile(r"^\s*(#{1,6})\s+(.*?)\s*#*$")
CHECKBOX = re.compile(r"^(\s*)[-*+]\s+\[([ xX])\]\s+(.*)$")
BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
NUMBERED = re.compile(r"^(\s*)(\d+[.)])\s+(.*)$")
QUOTE = re.compile(r"^\s*>\s?(.*)$")
RULE = re.compile(r"^\s*([-*_])\s*(\1\s*){2,}$")

CODE_SPAN = re.compile(r"(`+[^`]*`+)")
BOLD_SPAN = re.compile(r"\*\*(\S.*?\S|\S)\*\*|__(\S.*?\S|\S)__")
# Both delimiters must hug a non-space, non-asterisk character, or shell globs
# like "ls *.py and 2 * 3" or "**/*.py" get read as emphasis.
ITALIC_SPAN = re.compile(r"(?<![\w*])\*([^*\s]|[^*\s][^*\n]*?[^*\s])\*(?![\w*])")
STRIKE_SPAN = re.compile(r"~~(\S.*?\S|\S)~~")
LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")


def _width(cap: int = 80) -> int:
    return min(cap, max(20, shutil.get_terminal_size(fallback=(80, 24)).columns))


def _wrap(code: str, text: str) -> str:
    return f"{code}{text}{RESET}"


def _wrap_outer(code: str, text: str) -> str:
    """Wrap text that may already contain styled spans.

    Each inner RESET would otherwise also clear the outer style for the rest of
    the line, so re-apply it after every reset.
    """
    return f"{code}{text.replace(RESET, RESET + code)}{RESET}"


def render_inline(text: str) -> str:
    """Style emphasis, inline code and links, leaving code spans literal."""
    out: list[str] = []
    for part in CODE_SPAN.split(text):
        if part.startswith("`") and part.endswith("`") and len(part) > 1:
            out.append(_wrap(CYAN, part.strip("`")))
            continue
        part = LINK.sub(lambda m: f"{_wrap(UNDERLINE, m[1])} {_wrap(DIM, m[2])}", part)
        part = BOLD_SPAN.sub(lambda m: _wrap(BOLD, m[1] or m[2]), part)
        part = ITALIC_SPAN.sub(lambda m: _wrap(ITALIC, m[1]), part)
        part = STRIKE_SPAN.sub(lambda m: _wrap(STRIKE, m[1]), part)
        out.append(part)
    return "".join(out)


class Renderer:
    """Stateful because fenced code blocks span lines."""

    def __init__(self, style: bool = True) -> None:
        self.style = style
        self.in_code = False

    def line(self, raw: str) -> str:
        if not self.style:
            return raw

        fence = FENCE.match(raw)
        if fence:
            self.in_code = not self.in_code
            label = fence[3] if self.in_code and fence[3] else ""
            bar = "─" * max(3, _width() - len(label) - 4)
            return _wrap(DIM, f"{label} {bar}" if label else bar)

        if self.in_code:
            return raw  # verbatim: still copy-pasteable

        if RULE.match(raw):
            return _wrap(DIM, "─" * _width())

        heading = HEADING.match(raw)
        if heading:
            code = BOLD + UNDERLINE if len(heading[1]) == 1 else BOLD
            return _wrap_outer(code, render_inline(heading[2]))

        box = CHECKBOX.match(raw)
        if box:
            mark = "✓" if box[2].lower() == "x" else "·"
            return f"{box[1]}{_wrap(DIM, mark)} {render_inline(box[3])}"

        bullet = BULLET.match(raw)
        if bullet:
            return f"{bullet[1]}{_wrap(DIM, '•')} {render_inline(bullet[2])}"

        numbered = NUMBERED.match(raw)
        if numbered:
            return f"{numbered[1]}{_wrap(DIM, numbered[2])} {render_inline(numbered[3])}"

        quote = QUOTE.match(raw)
        if quote:
            return f"{_wrap(DIM, '│')} {render_inline(quote[1])}"

        return render_inline(raw)


class StreamRenderer:
    """Buffers a stream into lines and renders each completed line."""

    def __init__(self, write, style: bool = True) -> None:
        self.renderer = Renderer(style)
        self.write = write
        self.buffer = ""
        self.wrote_anything = False

    def feed(self, chunk: str) -> None:
        self.buffer += chunk
        while "\n" in self.buffer:
            line, _, self.buffer = self.buffer.partition("\n")
            self._emit(line)

    def close(self) -> None:
        if self.buffer:
            self._emit(self.buffer)
            self.buffer = ""

    def _emit(self, line: str) -> None:
        self.wrote_anything = True
        self.write(self.renderer.line(line) + "\n")


def largest_code_block(text: str) -> int:
    """Lines in the biggest fenced code block. Used to catch paste-at-the-user."""
    largest = 0
    current = 0
    in_code = False
    for line in text.split("\n"):
        if FENCE.match(line):
            if in_code:
                largest = max(largest, current)
            in_code = not in_code
            current = 0
            continue
        if in_code:
            current += 1
    return max(largest, current if in_code else 0)


def render(text: str, style: bool = True) -> str:
    """Render a complete string (used for non-streaming replies)."""
    renderer = Renderer(style)
    return "\n".join(renderer.line(line) for line in text.split("\n"))
