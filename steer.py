"""Collecting typed messages while a turn is running.

The main thread is busy driving the model, so a reader thread watches stdin for
the duration of a turn and queues whole lines. The agent loop drains the queue
between steps, which lets you redirect work in progress instead of interrupting
it. The thread only lives during a turn, so the REPL keeps readline editing and
history when it is your turn to type.
"""

from __future__ import annotations

import os
import select
import sys
import threading
from collections import deque

def read_pending(fd: int, settle: float = 0.05) -> str:
    """Input already waiting to be read. Empty string if there is none.

    POSIX only in practice: select() cannot watch stdin on Windows, where it
    raises and this returns "" — pastes then arrive one line per message.
    """
    chunks: list[str] = []
    timeout = 0.0  # a line typed at the prompt must not pay any latency
    while True:
        try:
            ready, _, _ = select.select([fd], [], [], timeout)
        except (OSError, ValueError):
            break
        if not ready:
            break
        try:
            data = os.read(fd, 65536)
        except OSError:
            break
        if not data:
            break
        chunks.append(data.decode("utf-8", errors="replace"))
        timeout = settle  # a paste can arrive in several chunks
    return "".join(chunks)


class InputQueue:
    def __init__(self, fd: int | None = None, poll: float = 0.1) -> None:
        self.fd = sys.stdin.fileno() if fd is None else fd
        self.poll = poll
        self._lines: deque[str] = deque()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._buffer = ""
        self.on_queued = None  # callback(text), for echoing a confirmation

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=2 * self.poll + 0.5)
        self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None

    # -- reading -----------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self.fd], [], [], self.poll)
            except (OSError, ValueError):
                return
            if not ready:
                continue
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                return
            if not data:
                return
            self._absorb(data.decode("utf-8", errors="replace"))

    def _absorb(self, text: str) -> None:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        self._buffer += text
        while "\n" in self._buffer:
            line, _, self._buffer = self._buffer.partition("\n")
            line = line.strip()
            if not line:
                continue
            with self._lock:
                self._lines.append(line)
            if self.on_queued:
                self.on_queued(line)

    # -- consuming ---------------------------------------------------------
    def drain(self) -> list[str]:
        with self._lock:
            taken = list(self._lines)
            self._lines.clear()
        return taken

    def pending(self) -> int:
        with self._lock:
            return len(self._lines)

    @property
    def leftover(self) -> str:
        """Text typed without a closing newline when the turn ended."""
        return self._buffer

    def take_leftover(self) -> str:
        text = self._buffer
        self._buffer = ""
        return text
