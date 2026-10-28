"""Run the REPL on a pty, the way a real terminal does.

The child must own the pty as its controlling terminal. Without that, a tool
command's own `setsid()` leaves the pty with no owning session, and macOS revokes
it as soon as that command exits — the harness then dies on its next print with
EIO. A real terminal never looks like that, so the test rig has to match.
"""

from __future__ import annotations

import fcntl
import os
import pty
import select
import subprocess
import sys
import termios
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _own_the_tty() -> None:
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def repl(
    base_url: str,
    tmp: Path,
    script: list[tuple[float, bytes]],
    extra_args: list[str] | None = None,
    settle: float = 2.0,
) -> str:
    """Start main.py, write each chunk after its delay, return everything printed."""
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [
            sys.executable, "-u", str(ROOT / "main.py"),
            "--model", "mock",
            "--base-url", base_url,
            "--workspace", str(tmp / "ws"),
            "--log-dir", str(tmp / "logs"),
            "--prompt-file", str(tmp / "nope.md"),
            "--no-stream",
            "--no-markdown",
            *(extra_args or []),
        ],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        preexec_fn=_own_the_tty,
        env={**os.environ, "OPENAI_API_KEY": "x", "TERM": "dumb"},
        close_fds=True,
    )
    os.close(slave)
    output: list[str] = []

    def pump(seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.05)
            if not ready:
                continue
            try:
                data = os.read(master, 65536)
            except OSError:
                return
            if not data:
                time.sleep(0.05)
                continue
            output.append(data.decode("utf-8", errors="replace"))

    pump(settle)  # banner and first prompt
    for delay, chunk in script:
        pump(delay)
        try:
            os.write(master, chunk)
        except OSError:
            break
    pump(settle)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    os.close(master)
    return "".join(output)
