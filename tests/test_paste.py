"""Multi-line paste checks. Run: .venv/bin/python tests/test_paste.py

The end-to-end case drives the real REPL through a pty, which is the only way to
reproduce what a terminal actually does with a pasted block.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import common  # noqa: E402
import mock_server  # noqa: E402
import ptyrun  # noqa: E402
from common import check, real_users  # noqa: E402

import main  # noqa: E402


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
    """Assembly only joins and normalises; trimming is a separate step, because
    whether the paste ended on a newline decides where typed text goes."""
    print("assembling a paste")
    out = main.assemble_paste("one", "two\nthree\n")
    check("joins all lines", out == "one\ntwo\nthree\n", repr(out))
    check("keeps the trailing newline", out.endswith("\n"))

    out = main.assemble_paste("one", "two\r\nthree\r\n")
    check("normalizes CRLF", out == "one\ntwo\nthree\n", repr(out))
    out = main.assemble_paste("one", "two\rthree\r")
    check("normalizes bare CR", out == "one\ntwo\nthree\n", repr(out))

    bracketed = f"{main.BRACKET_START}two\nthree{main.BRACKET_END}\n"
    out = main.assemble_paste("one", bracketed)
    check("strips bracketed paste markers", out == "one\ntwo\nthree\n", repr(out))

    out = main.assemble_paste("one", "two\nthr")
    check("an unfinished last line is kept as-is", out == "one\ntwo\nthr", repr(out))
    check("and is marked by having no trailing newline", not out.endswith("\n"))

    code = "def f():\n    return 1\n"
    out = main.assemble_paste("# snippet", code)
    check("indentation preserved", out == "# snippet\ndef f():\n    return 1\n",
          repr(out))

    check("trims trailing blank lines",
          main.trim_blank_tail("one\ntwo\n\n\n") == "one\ntwo",
          repr(main.trim_blank_tail("one\ntwo\n\n\n")))
    check("keeps blank lines in the middle",
          main.trim_blank_tail("a\n\nb\n") == "a\n\nb")
    check("leaves a clean message alone",
          main.trim_blank_tail("a\nb") == "a\nb")


def test_hold_paste() -> None:
    """A paste is held for review; Enter sends it, Ctrl-C throws it away."""
    print("holding a paste for confirmation")

    def hold(text, answers, extras=None):
        """Run hold_paste with scripted keystrokes and buffered bursts."""
        replies = list(answers)
        bursts = list(extras or [])

        def fake_input(_prompt=""):
            if not replies:
                raise EOFError
            value = replies.pop(0)
            if isinstance(value, type) and issubclass(value, BaseException):
                raise value
            return value

        def fake_drain(_fd, settle=0.0):
            return bursts.pop(0) if bursts else ""

        # `input` is a builtin, so there is normally no such module global. Put
        # one in to shadow it, then remove it again — restoring it would leave a
        # permanent shadow for every later test in this process.
        had_input = "input" in vars(main)
        saved_drain = main.drain_buffered
        main.input = fake_input
        main.drain_buffered = fake_drain
        try:
            with common.capture() as cap:
                return main.hold_paste(text), cap.err
        finally:
            main.drain_buffered = saved_drain
            if not had_input:
                del main.input

    out, screen = hold("one\ntwo\n", [""])
    check("bare Enter sends it", out == "one\ntwo", repr(out))
    check("the content is shown first", "│ one" in screen and "│ two" in screen,
          screen)
    check("it says Enter sends", "Enter to send" in screen, screen)

    out, _ = hold("one\ntwo\n", ["and do this", ""])
    check("typing adds a line", out == "one\ntwo\nand do this", repr(out))

    # A paste that stopped mid-line is continued, not pushed onto a new line.
    out, _ = hold("one\ntw", ["o", ""])
    check("an unfinished line is continued", out == "one\ntwo", repr(out))

    out, _ = hold("one\ntwo\n", [KeyboardInterrupt])
    check("Ctrl-C discards the whole paste", out == "", repr(out))

    out, _ = hold("one\ntwo\n", [])
    check("EOF sends what there is", out == "one\ntwo", repr(out))

    # A second burst while it is held gets appended and re-shown.
    out, screen = hold("one\n", ["", ""], extras=["two\nthree\n", ""])
    check("a further paste is absorbed", out == "one\ntwo\nthree", repr(out))
    check("and re-shown", screen.count("Enter to send") == 2,
          str(screen.count("Enter to send")))

    long_text = "".join(f"line {i}\n" for i in range(40))
    out, screen = hold(long_text, [""])
    check("a long paste is not echoed in full", "more lines" in screen, screen[:200])
    check("but all of it is sent", out.count("\n") == 39, str(out.count("\n")))


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


# Gap between keystroke groups, so each arrives as its own paste or line rather
# than one burst. PASTE_SETTLE is 0.05s, so this is a wide margin; it is the
# dominant cost of this suite, hence not wider than it needs to be.
KEYSTROKE_GAP = 1.2


def repl_session(base_url: str, keystrokes: list[bytes], tmp: Path) -> str:
    script = [
        (0.0 if i == 0 else KEYSTROKE_GAP, chunk)
        for i, chunk in enumerate(keystrokes)
    ]
    return ptyrun.repl(base_url, tmp, script, settle=KEYSTROKE_GAP)


def test_repl_paste(tmp: Path, base_url: str) -> None:
    """A real paste, confirmed with Enter, arrives as one message."""
    print("pasting into the real REPL")
    mock_server.set_script([("got it", [])])
    paste = b"first line\nsecond line\nthird line\n"
    screen = repl_session(base_url, [paste, b"\n", b"/exit\n"], tmp / "e2e")

    requests = [r for r in mock_server.REQUESTS if r.get("messages")]
    check("the model was called once", len(requests) == 1, str(len(requests)))
    if not requests:
        return
    users = real_users(requests[0]["messages"])
    check("exactly one user message", len(users) == 1, str(users))
    check("all three lines in it",
          users and users[0] == "first line\nsecond line\nthird line",
          repr(users[0]) if users else "")
    check("paste was announced", "3 lines pasted" in screen, screen[-400:])
    check("the pasted lines were echoed back",
          "│ first line" in screen and "│ third line" in screen, screen[-500:])
    check("session exited cleanly", "bye" in screen)


def test_repl_paste_waits_for_enter(tmp: Path, base_url: str) -> None:
    """The bug: a paste used to be sent the instant it landed.

    Nothing may reach the model until Enter confirms it, however long the paste
    sits there.
    """
    print("a paste is not sent on arrival")
    mock_server.set_script([("should not happen", [])])
    paste = b"do something dangerous\nand another line\n"
    screen = repl_session(base_url, [paste, b"/exit\n"], tmp / "wait")

    check("the model was never called",
          not [r for r in mock_server.REQUESTS if r.get("messages")],
          str(len(mock_server.REQUESTS)))
    check("it was shown instead", "Enter to send" in screen, screen[-400:])
    check("the second line is visible",
          "│ and another line" in screen, screen[-400:])


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


def test_tab_completion_in_the_repl(tmp: Path, base_url: str) -> None:
    print("tab completion end to end")
    mock_server.set_script([("ok", [])])
    (tmp / "tab" / "ws" / "subdir").mkdir(parents=True, exist_ok=True)
    screen = ptyrun.repl(
        base_url,
        tmp / "tab",
        [
            (0.0, b"/limi\t"),      # completes to /limits
            (1.0, b"\n"),
            (1.0, b"/cwd sub\t"),   # completes to subdir
            (1.0, b"\n"),
            (1.0, b"/exit\n"),
        ],
        settle=2.0,
    )
    check("command completed and ran", "shell timeout" in screen, screen[-400:])
    check("directory argument completed", "subdir" in screen, screen[-400:])
    check("no model call for either", not [
        r for r in mock_server.REQUESTS if r.get("messages")])


def main_() -> int:
    server, base_url = mock_server.start()
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        test_drain()
        test_input_prompt_markers()
        test_assemble()
        test_hold_paste()
        test_repl_paste(tmp, base_url)
        test_repl_paste_waits_for_enter(tmp, base_url)
        test_repl_single_line_unchanged(tmp, base_url)
        test_repl_command_after_paste(tmp, base_url)
        test_tab_completion_in_the_repl(tmp, base_url)
    server.shutdown()
    return common.report()


if __name__ == "__main__":
    sys.exit(main_())
