"""Terminal output helpers: colors and compact tool activity lines."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
import time
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
_VT = not os.getenv("NO_COLOR") and _enable_windows_vt()
# Two streams, two answers. stdout carries the model's reply, stderr carries the
# harness's narration, and either one may be a terminal while the other is a file.
_COLOR_OUT = sys.stdout.isatty() and _VT
_COLOR_ERR = sys.stderr.isatty() and _VT
# Rewriting a status line in place needs a terminal, but not colour: NO_COLOR
# turns the colour off and the progress line should still work.
_TTY_ERR = sys.stderr.isatty()
_QUIET = False


def colors_enabled() -> bool:
    """Whether the model's reply (stdout) can carry colour."""
    return _COLOR_OUT


def set_quiet(quiet: bool) -> None:
    """Silence progress narration. Warnings and errors still come through."""
    global _QUIET
    _QUIET = quiet


def _c(code: str, text: str) -> str:
    # Either destination being a terminal is enough to build the escape codes;
    # _emit strips them again when stderr in particular cannot show them.
    return f"\033[{code}m{text}\033[0m" if (_COLOR_OUT or _COLOR_ERR) else text


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


def _emit(text: str) -> None:
    """Write one line of narration.

    All of it goes to stderr, so stdout holds nothing but the model's reply and
    `harness -p … > answer.txt` is exactly that answer. Flushed on every line
    because the two streams interleave on a terminal.
    """
    if not _COLOR_ERR:
        text = _ANSI.sub("", text)
    print(text, file=sys.stderr, flush=True)


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


def one_line(text: str, width: int | None = None) -> str:
    """Collapse to a single line, ending with … when it had to be shortened."""
    if width is None:
        width = max(40, shutil.get_terminal_size(fallback=(100, 24)).columns - 6)
    flat = " ".join(str(text).split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


_one_line = one_line  # internal alias


def tool_call(name: str, args: dict) -> None:
    """One-line summary of what the model is about to do."""
    if _QUIET:
        return
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
    _emit(f"{cyan('⏺')} {bold(name)} {dim(_one_line(detail))}")


def tool_result(text: str, is_error: bool = False, lines: int = 3) -> None:
    """Preview of a tool result: first few lines, dimmed."""
    if _QUIET and not is_error:
        return
    body = text.splitlines() or [""]
    marker = red("  ✗") if is_error else green("  ⎿")
    out = [f"{marker} {dim(_one_line(body[0], 110))}"]
    out += [f"    {dim(_one_line(extra, 110))}" for extra in body[1:lines]]
    remaining = len(body) - lines
    if remaining > 0:
        out.append(f"    {dim(f'… +{remaining} more lines')}")
    _emit("\n".join(out))


def assistant_prefix() -> None:
    """Label the reply on stderr, so the reply itself stays clean on stdout."""
    if _QUIET:
        return
    if _COLOR_ERR:
        print(green("assistant"), end=" ", file=sys.stderr, flush=True)
    else:
        print("assistant", file=sys.stderr, flush=True)


class Ticker:
    """Keeps one status line alive while something slow happens.

    A big request spends its first stretch in prefill, where the server has
    accepted the prompt and is processing it: nothing streams back, so without
    this the harness looks hung. The thread owns the writing; the main thread
    only hands over a new detail string, so the two never interleave.
    """

    def __init__(self, label: str, interval: float = 0.5) -> None:
        self.label = label
        self.interval = interval
        self.detail = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = 0.0
        # Rewriting a line needs a terminal. Elsewhere (a pipe, a cron log) the
        # surrounding one-off messages are the progress report.
        self._live = _TTY_ERR and not _QUIET

    def start(self) -> None:
        if self._thread or not self._live:
            return
        self._started = time.monotonic()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            elapsed = time.monotonic() - self._started
            line = f"{self.label} {elapsed:.0f}s{self.detail}"
            print(f"\r\033[K{dim(line)}", end="", file=sys.stderr, flush=True)
            self._stop.wait(self.interval)

    def set(self, detail: str) -> None:
        """Replace the trailing detail, e.g. how much has arrived so far."""
        self.detail = detail

    def stop(self, clear: bool = True) -> None:
        if not self._thread:
            return
        self._stop.set()
        self._thread.join(timeout=1.0)
        self._thread = None
        if clear:
            print("\r\033[K", end="", file=sys.stderr, flush=True)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started if self._started else 0.0

    def __enter__(self) -> Ticker:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


def narrate(chunk: str) -> None:
    """Dimmed narration without a line break, for streamed reasoning text."""
    if _QUIET:
        return
    text = dim(chunk) if _COLOR_ERR else _ANSI.sub("", chunk)
    print(text, end="", file=sys.stderr, flush=True)


def plain(text: str) -> None:
    """Narration at full brightness (the startup banner)."""
    if not _QUIET:
        _emit(text)


def info(text: str) -> None:
    if not _QUIET:
        _emit(dim(text))


def warn(text: str) -> None:
    _emit(yellow(text))


def error(text: str) -> None:
    _emit(red(text))
