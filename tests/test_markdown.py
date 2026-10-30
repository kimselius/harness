"""Markdown rendering checks. Run: .venv/bin/python tests/test_markdown.py"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import common  # noqa: E402
from common import check  # noqa: E402

import markdown  # noqa: E402

ANSI = re.compile(r"\033\[[0-9;]*m")


def plain(text: str) -> str:
    return ANSI.sub("", text)


def test_inline() -> None:
    print("inline spans")
    out = markdown.render_inline("**bold** and `code` and *em* and ~~gone~~")
    check("bold styled", markdown.BOLD in out)
    check("code styled", markdown.CYAN in out)
    check("italic styled", markdown.ITALIC in out)
    check("strike styled", markdown.STRIKE in out)
    check("markers removed", plain(out) == "bold and code and em and gone", plain(out))

    out = markdown.render_inline("see [the docs](https://x.dev) now")
    check("link text kept", "the docs" in plain(out))
    check("link url kept", "https://x.dev" in plain(out))

    # Things that look like markdown but are not. Both delimiters of an emphasis
    # span must hug non-space, so stray asterisks on one line stay literal.
    for text in [
        "ls *.py here",
        "a * b * c",
        "2 * 3",
        "note: ls *.py and 2 * 3 stay literal",
        "glob **/*.py and 4 * 5",
        "snake_case_name",
        "x_1 and y_2",
        "5 * 3 = 15, 6 * 2 = 12",
    ]:
        out = markdown.render_inline(text)
        check(f"literal preserved: {text}", plain(out) == text, plain(out))

    out = markdown.render_inline("run `rm -rf **/*.pyc` now")
    check("no styling inside code span", plain(out) == "run rm -rf **/*.pyc now",
          plain(out))


def test_blocks() -> None:
    print("block elements")
    r = markdown.Renderer()
    check("h1 bold+underline", markdown.BOLD in r.line("# Title")
          and markdown.UNDERLINE in r.line("# Title"))
    check("h2 bold only", markdown.UNDERLINE not in r.line("## Sub"))
    check("hashes stripped", plain(r.line("### Deep heading")) == "Deep heading",
          plain(r.line("### Deep heading")))
    check("trailing hashes stripped", plain(r.line("## Closed ##")) == "Closed")
    check("bullet becomes dot", plain(r.line("- item")).strip() == "• item",
          plain(r.line("- item")))
    check("bullet indent kept", plain(r.line("    - deep")).startswith("    •"))
    check("numbered kept", plain(r.line("3. third")).strip() == "3. third")
    check("checkbox done", "✓" in plain(r.line("- [x] done")))
    check("checkbox open", "·" in plain(r.line("- [ ] todo")))
    check("quote gutter", plain(r.line("> quoted")).startswith("│ "))
    check("rule becomes line", set(plain(r.line("---")).strip()) == {"─"})
    check("plain text untouched", plain(r.line("just a sentence.")) == "just a sentence.")


def test_code_blocks() -> None:
    print("fenced code")
    text = (
        "Here:\n"
        "```python\n"
        "x = **not bold**  # - not a bullet\n"
        "# not a heading\n"
        "```\n"
        "done"
    )
    out = markdown.render(text)
    lines = out.split("\n")
    check("code line verbatim", lines[2] == "x = **not bold**  # - not a bullet")
    check("comment not a heading", lines[3] == "# not a heading")
    check("fence replaced with rule", "```" not in out)
    check("language label kept", "python" in plain(lines[1]))
    check("text after fence rendered", plain(lines[5]) == "done")

    r = markdown.Renderer()
    r.line("```")
    check("fence state on", r.in_code)
    r.line("```")
    check("fence state off", not r.in_code)
    check("tilde fences work", markdown.Renderer().line("~~~sh").startswith(markdown.DIM))


def test_style_off() -> None:
    print("style disabled")
    text = "# Title\n- item\n**bold** `code`\n```\nraw\n```"
    out = markdown.render(text, style=False)
    check("byte-identical passthrough", out == text, repr(out))
    check("no escape codes", "\033" not in out)


def test_stream_matches_whole() -> None:
    print("streaming equals whole-text rendering")
    text = (
        "# Report\n\n"
        "Found **3 issues** in `main.py`:\n\n"
        "- first *thing*\n"
        "- second thing\n\n"
        "```sh\n"
        "grep -rn '**' .\n"
        "```\n"
        "Done.\n"
    )
    expected = markdown.render(text.rstrip("\n")) + "\n"

    for size in (1, 2, 3, 7, 50, 10_000):
        chunks: list[str] = []
        renderer = markdown.StreamRenderer(chunks.append)
        for i in range(0, len(text), size):
            renderer.feed(text[i : i + size])
        renderer.close()
        got = "".join(chunks)
        check(f"chunk size {size} matches", got == expected,
              f"\n got: {got!r}\n want: {expected!r}")

    # A span split across chunks must not be mangled.
    chunks = []
    renderer = markdown.StreamRenderer(chunks.append)
    for piece in ["**bo", "ld** and `co", "de`\n"]:
        renderer.feed(piece)
    renderer.close()
    check("split span still styled", markdown.BOLD in "".join(chunks)
          and markdown.CYAN in "".join(chunks))
    check("split span text intact", plain("".join(chunks)) == "bold and code\n",
          repr(plain("".join(chunks))))


def test_partial_line_flushed() -> None:
    print("partial line handling")
    chunks: list[str] = []
    renderer = markdown.StreamRenderer(chunks.append)
    renderer.feed("no trailing newline")
    check("nothing emitted before newline", chunks == [])
    renderer.close()
    check("flushed on close", plain("".join(chunks)) == "no trailing newline\n")
    renderer.close()
    check("close is idempotent", len(chunks) == 1)


def test_no_escape_leaks() -> None:
    print("escape hygiene")
    samples = [
        "# Title",
        "- **bold** item with `code`",
        "> quote with *em*",
        "normal",
        "```py",
        "x=1",
        "```",
    ]
    r = markdown.Renderer()
    for sample in samples:
        out = r.line(sample)
        styled = "\033" in out
        check(
            f"closed at end of line: {sample[:18]!r}",
            not styled or out.endswith(markdown.RESET),
            repr(out[-12:]),
        )
    # An inline span inside a heading must not kill the heading style.
    out = markdown.Renderer().line("## Fix `main.py` now")
    tail = out.split(markdown.RESET)[-2]
    check("heading style survives inline span", markdown.BOLD in out
          and tail.startswith(markdown.BOLD), repr(out))
    check("heading text intact", plain(out) == "Fix main.py now", plain(out))


def main() -> int:
    test_inline()
    test_blocks()
    test_code_blocks()
    test_style_off()
    test_stream_matches_whole()
    test_partial_line_flushed()
    test_no_escape_leaks()
    return common.report()


if __name__ == "__main__":
    sys.exit(main())
