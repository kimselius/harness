"""Cross-platform behaviour. Run: .venv/bin/python tests/test_platform.py

The Windows branches cannot be executed here, so they are exercised by faking the
pieces they depend on: a missing os.killpg, an absent signal.SIGKILL, select()
refusing a non-socket the way Winsock does, and the cwd probe disabled.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import common  # noqa: E402
from common import check  # noqa: E402

import steer  # noqa: E402
import tools as toolkit  # noqa: E402
import ui  # noqa: E402
from session import Session  # noqa: E402


def test_kill_without_process_groups() -> None:
    """Windows has no os.killpg, so _kill_group must fall back to the child."""
    print("killing a command without process groups")
    saved = getattr(os, "killpg", None)
    try:
        if hasattr(os, "killpg"):
            del os.killpg
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        toolkit._kill_group(proc)
        proc.communicate(timeout=10)
        check("the child is dead", proc.poll() is not None, str(proc.poll()))
        toolkit._kill_group(proc)  # already dead
        check("killing twice is safe", True)
    except AttributeError as exc:
        check("no exception raised", False, f"AttributeError: {exc}")
    finally:
        if saved is not None:
            os.killpg = saved


def test_kill_without_sigkill() -> None:
    """And no SIGKILL either. A real Popen.kill() uses it on POSIX, so this uses
    a stand-in process to isolate _kill_group's own behaviour."""
    print("killing without SIGKILL")

    class FakeProc:
        pid = 4242

        def __init__(self):
            self.kills = 0

        def kill(self):
            self.kills += 1

    saved = (getattr(os, "killpg", None), getattr(signal, "SIGKILL", None))
    try:
        if hasattr(os, "killpg"):
            del os.killpg
        if hasattr(signal, "SIGKILL"):
            del signal.SIGKILL
        proc = FakeProc()
        toolkit._kill_group(proc)
        check("falls back to killing the child", proc.kills == 1, str(proc.kills))
    except AttributeError as exc:
        check("falls back to killing the child", False, f"AttributeError: {exc}")
    finally:
        if saved[0] is not None:
            os.killpg = saved[0]
        if saved[1] is not None:
            signal.SIGKILL = saved[1]

    # A child that is already gone must not raise either.
    class Gone(FakeProc):
        def kill(self):
            raise ProcessLookupError("gone")

    try:
        toolkit._kill_group(Gone())
        check("a vanished child is tolerated", True)
    except Exception as exc:
        check("a vanished child is tolerated", False, f"{type(exc).__name__}: {exc}")


def test_probe_is_posix_only(tmp: Path) -> None:
    print("cwd probe is not appended off POSIX")
    os.environ.setdefault("OPENAI_API_KEY", "x")
    cfg = common.make_config(tmp)
    session = Session(cfg.workspace)
    check("probe present on this platform", bool(toolkit.CWD_PROBE) is toolkit.POSIX_SHELL)

    # Simulate the Windows build of the module: no probe appended at all.
    saved = toolkit.CWD_PROBE
    toolkit.CWD_PROBE = ""
    try:
        (cfg.workspace / "sub").mkdir(exist_ok=True)
        result = toolkit.tool_shell({"command": "cd sub"}, cfg, session)
        check("command still runs", "exit code: 0" in result, result.splitlines()[0])
        check("no marker leaks", toolkit.CWD_MARKER not in result)
        check("directory simply stays put", session.cwd == session.root,
              str(session.cwd))
        out = toolkit.tool_shell({"command": "echo hello"}, cfg, session)
        check("output still captured", "hello" in out, out)
    finally:
        toolkit.CWD_PROBE = saved


def test_queue_survives_unwatchable_stdin() -> None:
    print("input queue with an unusable fd")
    # Winsock's select() rejects anything that is not a socket.
    queue = steer.InputQueue(fd=-1, poll=0.01)
    queue.start()
    import time

    time.sleep(0.1)
    check("thread exits instead of raising", queue.pending() == 0)
    queue.stop()
    check("stop still works", not queue.running)
    check("drain is empty", queue.drain() == [])


def test_colour_detection() -> None:
    print("colour detection")
    check("vt helper is a no-op off windows", ui._enable_windows_vt() is True)
    check("colours_enabled is a bool", isinstance(ui.colors_enabled(), bool))
    # With colour off there are no escapes at all, so prompt width cannot be wrong.
    check("no escapes when colour is off",
          "\033" not in ui.dim("x") if not ui.colors_enabled() else True)


def main_() -> int:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        test_kill_without_process_groups()
        test_kill_without_sigkill()
        test_probe_is_posix_only(tmp)
        test_queue_survives_unwatchable_stdin()
        test_colour_detection()
    return common.report()


if __name__ == "__main__":
    sys.exit(main_())
