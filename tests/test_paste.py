"""Multi-line paste checks. Run: .venv/bin/python tests/test_paste.py

The end-to-end case drives the real REPL through a pty, which is the only way to
reproduce what a terminal actually does with a pasted block.
"""

from __future__ import annotations

import json
import os
import pty
import select
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import mock_server  # noqa: E402

import main  # noqa: E402
import ptyrun  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        failures.append(label)
        print(f"  FAIL {label} {detail}")


def real_users(messages: list[dict]) -> list[str]:
    """User messages, minus the harness state block appended to every request."""
    return [
        m["content"] for m in messages
        if m["role"] == "user" and not m["content"].startswith("<state>")
    ]


def test_drain() -> None:
    print("draining buffered input")
    read_fd, write_fd = os.pipe()
    check("nothing buffered gives empty", main.drain_buffered(read_fd) == "")
    os.write(write_fd, b"two\nthree\n")
    time.sleep(0.01)
    check("reads what is waiting", main.drain_buffered(read_fd) == "two\nthree\n")
    check("second call is empty again", main.drain_buffered(read_fd) == "")
    os.close(read_fd)
    os.close(write_fd)
    check("closed fd does not raise", main.drain_buffered(read_fd) == "")


def test_assemble() -> None:
    print("assembling a paste")
    out = main.assemble_paste("one", "two\nthree\n")
    check("joins all lines", out == "one\ntwo\nthree", repr(out))

    out = main.assemble_paste("one", "two\r\nthree\r\n")
    check("normalizes CRLF", out == "one\ntwo\nthree", repr(out))
    out = main.assemble_paste("one", "two\rthree\r")
    check("normalizes bare CR", out == "one\ntwo\nthree", repr(out))

    bracketed = f"{main.BRACKET_START}two\nthree{main.BRACKET_END}\n"
    out = main.assemble_paste("one", bracketed)
    check("strips bracketed paste markers", out == "one\ntwo\nthree", repr(out))

    out = main.assemble_paste("one", "two\n\n\n")
    check("trims trailing blank lines", out == "one\ntwo", repr(out))
    out = main.assemble_paste("a", "\nb\n")
    check("keeps blank lines in the middle", out == "a\n\nb", repr(out))

    # Paste ending mid-line: the user has not pressed Enter yet.
    out = main.assemble_paste("one", "two\nthr", read_more=lambda: "ee")
    check("waits for the unfinished line", out == "one\ntwo\nthree", repr(out))
    out = main.assemble_paste("one", "two\nthr", read_more=lambda: (_ for _ in ()).throw(EOFError))
    check("survives EOF while waiting", out == "one\ntwo\nthr", repr(out))

    code = "def f():\n    return 1\n"
    out = main.assemble_paste("# snippet", code)
    check("indentation preserved", out == "# snippet\ndef f():\n    return 1", repr(out))


def test_input_prompt_markers() -> None:
    print("readline prompt width")
    import ui

    coloured = "\033[2mapp \033[0m\033[36m> \033[0m"

    class Fake:
        def __init__(self, **kw):
            for key, value in kw.items():
                setattr(self, key, value)

    # Which line editors understand the zero-width markers.
    check("GNU readline (linux)", ui.supports_prompt_markers(Fake(backend="readline")))
    check("libedit (macos)", ui.supports_prompt_markers(Fake(backend="editline")))
    check("older cpython without .backend",
          ui.supports_prompt_markers(Fake(_READLINE_LIBRARY_VERSION="8.2")))
    check("pyreadline3 on windows is excluded",
          not ui.supports_prompt_markers(Fake(__version__="3.4.1")))
    check("unverified backend is excluded",
          not ui.supports_prompt_markers(Fake(backend="something-new")))
    check("no readline at all", not ui.supports_prompt_markers(None))

    ui.enable_readline_prompts(None)
    check("left alone with no supported editor", ui.input_prompt(coloured) == coloured)

    check("opt-in reports success", ui.enable_readline_prompts(Fake(backend="readline")))
    wrapped = ui.input_prompt(coloured)
    check("every escape is bracketed",
          wrapped.count("\001") == 4 and wrapped.count("\002") == 4, repr(wrapped))
    check("visible text unchanged",
          wrapped.replace("\001", "").replace("\002", "") == coloured, repr(wrapped))
    check("markers wrap only escapes",
          all(chunk.split("\002")[0].startswith("\033[")
              for chunk in wrapped.split("\001")[1:]), repr(wrapped))
    check("plain text is a no-op", ui.input_prompt("> ") == "> ")

    # This machine's actual editor, whatever it is, must be classified.
    import readline

    decided = ui.supports_prompt_markers(readline)
    check("local editor classified", isinstance(decided, bool))
    print(f"       (local backend: {getattr(readline, 'backend', 'unknown')} "
          f"-> markers {'on' if decided else 'off'})")


def repl_session(base_url: str, keystrokes: list[bytes], tmp: Path) -> str:
    # Wait between keystroke groups, so each one is a separate paste or line
    # rather than one burst arriving together.
    script = [(0.0 if i == 0 else 2.5, chunk) for i, chunk in enumerate(keystrokes)]
    return ptyrun.repl(base_url, tmp, script, settle=2.5)


def test_repl_paste(tmp: Path, base_url: str) -> None:
    print("pasting into the real REPL")
    mock_server.set_script([("got it", [])])
    paste = b"first line\nsecond line\nthird line\n"
    screen = repl_session(base_url, [paste, b"/exit\n"], tmp / "e2e")

    requests = [r for r in mock_server.REQUESTS if r.get("messages")]
    check("the model was called once", len(requests) == 1, str(len(requests)))
    if not requests:
        return
    users = real_users(requests[0]["messages"])
    check("exactly one user message", len(users) == 1, str(users))
    check("all three lines in it",
          users and users[0] == "first line\nsecond line\nthird line",
          repr(users[0]) if users else "")
    check("paste was announced", "3 lines pasted" in screen, screen[-300:])
    check("session exited cleanly", "bye" in screen)


def test_repl_single_line_unchanged(tmp: Path, base_url: str) -> None:
    print("single lines still behave")
    mock_server.set_script([("one", []), ("two", [])])
    screen = repl_session(
        base_url, [b"hello\n", b"again\n", b"/exit\n"], tmp / "single"
    )
    requests = [r for r in mock_server.REQUESTS if r.get("messages")]
    check("two separate turns", len(requests) == 2, str(len(requests)))
    if len(requests) == 2:
        users = real_users(requests[1]["messages"])
        check("both messages kept separate", users == ["hello", "again"], str(users))
    check("no paste notice", "lines pasted" not in screen)


def test_repl_command_after_paste(tmp: Path, base_url: str) -> None:
    print("commands still work")
    mock_server.set_script([("ok", [])])
    screen = repl_session(base_url, [b"/tools\n", b"/cwd\n", b"/exit\n"], tmp / "cmds")
    check("/tools ran locally", "write_file" in screen, screen[-400:])
    check("/cwd ran locally", "workspace root" in screen, screen[-400:])
    check("no model call for commands",
          not [r for r in mock_server.REQUESTS if r.get("messages")])


def main_() -> int:
    server, base_url = mock_server.start()
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        test_drain()
        test_input_prompt_markers()
        test_assemble()
        test_repl_paste(tmp, base_url)
        test_repl_single_line_unchanged(tmp, base_url)
        test_repl_command_after_paste(tmp, base_url)
    server.shutdown()
    print()
    if failures:
        print(f"{len(failures)} failed: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main_())
