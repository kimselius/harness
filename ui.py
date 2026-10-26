"""Terminal output helpers: colors and compact tool activity lines."""

from __future__ import annotations

import json
import os
import sys

_ENABLED = sys.stdout.isatty() and not os.getenv("NO_COLOR")


def colors_enabled() -> bool:
    return _ENABLED


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _ENABLED else text


def dim(t: str) -> str:
    return _c("2", t)


def bold(t: str) -> str:
    return _c("1", t)


def cyan(t: str) -> str:
    return _c("36", t)


def green(t: str) -> str:
    return _c("32", t)


def yellow(t: str) -> str:
    return _c("33", t)


def red(t: str) -> str:
    return _c("31", t)


def _one_line(text: str, width: int = 100) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def tool_call(name: str, args: dict) -> None:
    """One-line summary of what the model is about to do."""
    if name == "shell":
        detail = args.get("command", "")
    elif name == "read_file":
        detail = args.get("path", "")
        span = []
        if args.get("offset"):
            span.append(f"offset={args['offset']}")
        if args.get("limit"):
            span.append(f"limit={args['limit']}")
        if span:
            detail += f" ({', '.join(span)})"
    elif name == "write_file":
        content = args.get("content") or ""
        detail = f"{args.get('path', '')} ({len(content)} chars)"
    elif name == "edit_file":
        detail = args.get("path", "")
        if args.get("replace_all"):
            detail += " (replace_all)"
    else:
        detail = json.dumps(args, default=str)
    print(f"{cyan('⏺')} {bold(name)} {dim(_one_line(detail))}")


def tool_result(text: str, is_error: bool = False, lines: int = 3) -> None:
    """Preview of a tool result: first few lines, dimmed."""
    body = text.splitlines() or [""]
    marker = red("  ✗") if is_error else green("  ⎿")
    print(f"{marker} {dim(_one_line(body[0], 110))}")
    for extra in body[1:lines]:
        print(f"    {dim(_one_line(extra, 110))}")
    remaining = len(body) - lines
    if remaining > 0:
        print(f"    {dim(f'… +{remaining} more lines')}")


def assistant_prefix() -> None:
    print(green("assistant"), end=" ", flush=True)


def info(text: str) -> None:
    print(dim(text))


def warn(text: str) -> None:
    print(yellow(text))


def error(text: str) -> None:
    print(red(text))
