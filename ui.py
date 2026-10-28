"""Terminal output helpers: colors and compact tool activity lines."""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

def _enable_windows_vt() -> bool:
    """Turn on ANSI handling in the Windows console. True if colour is usable."""
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


# Old consoles that cannot do VT get no colour, which also means no escape codes
# in the prompt and so nothing for a line editor to miscount.
_ENABLED = sys.stdout.isatty() and not os.getenv("NO_COLOR") and _enable_windows_vt()


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


_ANSI = re.compile(r"(\033\[[0-9;]*m)")
_READLINE_SAFE = False  # set by enable_readline_prompts()


def supports_prompt_markers(module: Any) -> bool:
    """Whether this line editor understands \\001/\\002 around escape codes.

    GNU readline defines them (RL_PROMPT_START_IGNORE) and libedit honours them
    too, and both ship as CPython's own `readline` extension. Third-party
    stand-ins such as pyreadline3 on Windows do not, and would print the markers
    as literal control characters — so only opt in when the module is recognised.
    """
    if module is None:
        return False
    backend = getattr(module, "backend", None)
    if backend in ("readline", "editline"):
        return True
    if backend is not None:
        return False  # a backend we have not verified
    # Older CPython has no `backend`, but its extension module exposes this.
    return hasattr(module, "_READLINE_LIBRARY_VERSION")


def enable_readline_prompts(module: Any = None) -> bool:
    """Opt in to zero-width prompt markers if the editor supports them."""
    global _READLINE_SAFE
    _READLINE_SAFE = supports_prompt_markers(module)
    return _READLINE_SAFE


def input_prompt(text: str) -> str:
    """Mark colour codes as zero-width for readline.

    Both GNU readline and libedit count every byte of the prompt when working out
    where the line wraps, so raw escape codes make them wrap too early: a long
    line then redraws over itself and editing corrupts the display. \\001/\\002
    tell them the enclosed bytes occupy no columns.
    """
    if not _READLINE_SAFE:
        return text
    return _ANSI.sub("\001\\1\002", text)


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
