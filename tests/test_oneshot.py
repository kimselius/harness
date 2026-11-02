"""Unattended runs. Run: .venv/bin/python tests/test_oneshot.py

`harness -p …` is a unit in someone else's pipeline — a shell script, a crontab,
a `docker run`. That means its exit code has to be true, its stdout has to be the
answer and nothing else, and a signal has to stop it cleanly instead of leaving
children behind. Everything here is checked through a real subprocess, because
those are properties of the process, not of the Agent object.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import common  # noqa: E402
import mock_server  # noqa: E402
from common import check  # noqa: E402

import main as cli  # noqa: E402
from agent import ERROR, INTERRUPTED, MAX_STEPS, OK, TIMEOUT, Outcome  # noqa: E402

ENV = {**os.environ, "OPENAI_API_KEY": "x"}


def argv(tmp: Path, base_url: str | None, *extra: str) -> list[str]:
    """A one-shot invocation rooted under tmp."""
    args = [
        sys.executable, "-u", str(ROOT / "main.py"),
        "--workspace", str(tmp / "ws"),
        "--log-dir", str(tmp / "logs"),
        "--prompt-file", str(tmp / "nope.md"),
        "--no-stream", "--no-markdown",
    ]
    if base_url:
        args += ["--model", "mock", "--base-url", base_url]
    return args + list(extra)


def run(tmp: Path, base_url: str | None, *extra: str, timeout: float = 120):
    return subprocess.run(
        argv(tmp, base_url, *extra), env=ENV, capture_output=True, text=True,
        timeout=timeout,
    )


# -- the exit code is the contract -----------------------------------------
def test_exit_codes(tmp: Path, base_url: str) -> None:
    print("exit codes")
    mock_server.set_script([("all done", [])])
    done = run(tmp / "ok", base_url, "-p", "do the thing")
    check("success is 0", done.returncode == 0, done.stderr[-300:])

    # An unreachable endpoint used to print the error and still exit 0, so a
    # chained script marched on to the next step with nothing written.
    dead = run(tmp / "dead", "http://127.0.0.1:9/v1", "-p", "do the thing")
    check("unreachable endpoint is 1", dead.returncode == 1, str(dead.returncode))
    check("the error is reported", "Error" in dead.stderr, dead.stderr[-300:])
    check("nothing on stdout", dead.stdout.strip() == "", repr(dead.stdout))

    # The script's last entry repeats, so the model keeps calling tools forever.
    mock_server.set_script([("", [("shell", {"command": "true"})])])
    walled = run(tmp / "wall", base_url, "-p", "loop", "--max-steps", "2")
    check("hitting --max-steps is 3", walled.returncode == 3, str(walled.returncode))
    check("said why", "max-steps" in walled.stderr, walled.stderr[-200:])


def test_no_prompt_without_a_terminal(tmp: Path) -> None:
    """The cron trap: nothing to do used to look like success."""
    print("no prompt, no terminal")
    result = subprocess.run(
        argv(tmp, None), env=ENV, capture_output=True, text=True,
        stdin=subprocess.DEVNULL, timeout=60,
    )
    check("exits non-zero", result.returncode == 1, str(result.returncode))
    check("says what to do", "-p" in result.stderr, result.stderr[-200:])
    check("no banner on stdout", result.stdout.strip() == "", repr(result.stdout))


def test_piped_prompt_still_works(tmp: Path, base_url: str) -> None:
    print("prompt on stdin")
    mock_server.set_script([("piped answer", [])])
    result = subprocess.run(
        argv(tmp, base_url), env=ENV, input="do it from stdin",
        capture_output=True, text=True, timeout=120,
    )
    check("runs", result.returncode == 0, result.stderr[-300:])
    check("answer on stdout", result.stdout.strip() == "piped answer",
          repr(result.stdout))


# -- stream separation ------------------------------------------------------
def test_streams_are_separate(tmp: Path, base_url: str) -> None:
    print("stdout is the answer, stderr is the narration")
    mock_server.set_script(
        [("", [("write_file", {"path": "out.txt", "content": "hi"})]),
         ("wrote the file", [])]
    )
    result = run(tmp / "split", base_url, "-p", "write a file")
    check("exit 0", result.returncode == 0, result.stderr[-300:])
    check("stdout is exactly the reply", result.stdout.strip() == "wrote the file",
          repr(result.stdout))
    check("tool activity on stderr", "write_file" in result.stderr,
          result.stderr[-300:])
    check("tool activity not on stdout", "write_file" not in result.stdout)
    check("token footer on stderr", "ctx ~" in result.stderr, result.stderr[-200:])
    check("no ANSI codes in a redirected stdout", "\033[" not in result.stdout)
    check("no ANSI codes in a redirected stderr", "\033[" not in result.stderr)

    # Small models pad a tool-calling turn with bare newlines. In a pipeline
    # stdout is the deliverable, so that padding must not reach it.
    mock_server.set_script(
        [("\n\n\n", [("shell", {"command": "true"})]),
         ("\n\nthe answer\n\n\n", [])]
    )
    padded = run(tmp / "padded", base_url, "-p", "pad it")
    check("padding stripped from stdout", padded.stdout == "the answer\n",
          repr(padded.stdout))


def test_quiet(tmp: Path, base_url: str) -> None:
    print("--quiet")
    mock_server.set_script(
        [("", [("shell", {"command": "echo hello"})]), ("finished", [])]
    )
    result = run(tmp / "quiet", base_url, "-p", "run something", "--quiet")
    check("exit 0", result.returncode == 0, result.stderr[-300:])
    check("answer still on stdout", result.stdout.strip() == "finished",
          repr(result.stdout))
    check("progress suppressed", "echo hello" not in result.stderr,
          result.stderr[-300:])

    # Failures must survive --quiet, or an unattended run goes silent.
    mock_server.set_script([("", [("read_file", {"path": "missing.txt"})]),
                            ("could not", [])])
    failing = run(tmp / "quiet-err", base_url, "-p", "read a missing file", "--quiet")
    check("errors are still reported", "no such file" in failing.stderr,
          failing.stderr[-300:])


# -- the result file --------------------------------------------------------
RESULT_KEYS = {
    "status", "exit_code", "text", "steps", "tool_calls", "error", "session",
    "label", "tokens_in", "tokens_out", "tokens_thinking", "requests",
}


def test_result_file(tmp: Path, base_url: str) -> None:
    print("--result-file")
    mock_server.set_script(
        [("", [("shell", {"command": "echo one"})]), ("the final word", [])]
    )
    out = tmp / "res" / "nested" / "result.json"
    done = run(tmp / "res", base_url, "-p", "do it", "--result-file", str(out),
               "--label", "step-one")
    check("exit 0", done.returncode == 0, done.stderr[-300:])
    check("file written", out.is_file(), str(out))
    payload = json.loads(out.read_text())
    check("every documented key present", RESULT_KEYS <= set(payload),
          str(sorted(RESULT_KEYS - set(payload))))
    check("status ok", payload["status"] == OK, str(payload["status"]))
    check("exit code recorded", payload["exit_code"] == 0)
    check("final text captured", payload["text"] == "the final word",
          repr(payload["text"]))
    check("tool calls counted", payload["tool_calls"] == 1, str(payload["tool_calls"]))
    check("steps counted", payload["steps"] == 2, str(payload["steps"]))
    check("label carried", payload["label"] == "step-one", str(payload["label"]))
    check("session id present", payload["session"] and "-" in payload["session"],
          str(payload["session"]))
    check("token totals present", payload["tokens_in"] > 0, str(payload["tokens_in"]))
    check("no error", payload["error"] is None, str(payload["error"]))
    mode = out.stat().st_mode & 0o777
    check("private file", mode == 0o600, oct(mode))
    check("no leftover temp file", not out.with_name(out.name + ".tmp").exists())

    # The failure case is the one a driver actually needs.
    bad = tmp / "res2" / "result.json"
    failed = run(tmp / "res2", "http://127.0.0.1:9/v1", "-p", "x",
                 "--result-file", str(bad))
    check("written even on failure", bad.is_file(), str(bad))
    broken = json.loads(bad.read_text())
    check("status error", broken["status"] == ERROR, str(broken["status"]))
    check("exit code agrees", broken["exit_code"] == failed.returncode == 1,
          f"{broken['exit_code']} vs {failed.returncode}")
    check("reason given", "Error" in (broken["error"] or ""), str(broken["error"]))


def test_result_file_is_scrubbed(tmp: Path, base_url: str) -> None:
    """It leaves the container, so it gets the same treatment as the log."""
    print("--result-file redaction")
    key = "sk-live-9f3a2b7c4d5e6f708192a3b4c5d6e7f8"
    mock_server.set_script([(f"the key is {key}", [])])
    out = tmp / "scrub" / "result.json"
    subprocess.run(
        argv(tmp / "scrub", base_url, "-p", "leak it", "--result-file", str(out)),
        env={**os.environ, "OPENAI_API_KEY": key},
        capture_output=True, text=True, timeout=120,
    )
    body = out.read_text()
    check("our key is absent", key not in body, body[:200])
    check("still valid json", json.loads(body)["status"] == OK)


# -- deadlines and signals --------------------------------------------------
def test_max_seconds(tmp: Path, base_url: str) -> None:
    print("--max-seconds")
    mock_server.set_script([("", [("shell", {"command": "sleep 1"})])])
    result = run(tmp / "deadline", base_url, "-p", "keep going",
                 "--max-seconds", "1", "--max-steps", "50")
    check("exit 4", result.returncode == 4, f"{result.returncode} {result.stderr[-200:]}")
    check("said why", "max-seconds" in result.stderr, result.stderr[-200:])
    records = [json.loads(line)
               for path in (tmp / "deadline" / "logs").glob("*.jsonl")
               for line in path.read_text().splitlines() if line.strip()]
    check("deadline logged", any(r["type"] == "deadline_reached" for r in records))
    ended = [r for r in records if r["type"] == "session_end"]
    check("session_end records the status",
          ended and ended[-1]["status"] == TIMEOUT, str(ended[-1:]))


def _child_alive(marker: str) -> bool:
    found = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
    return found.returncode == 0 and bool(found.stdout.strip())


def test_sigterm(tmp: Path, base_url: str) -> None:
    """docker stop sends SIGTERM. It must not take ten seconds or orphan a build."""
    print("SIGTERM")
    marker = "harness_sigterm_marker_sleep"
    mock_server.set_script(
        [("", [("shell", {"command": f"sleep 60 # {marker}", "timeout": 120})])]
    )
    out = tmp / "term" / "result.json"
    proc = subprocess.Popen(
        argv(tmp / "term", base_url, "-p", "start something slow",
             "--result-file", str(out)),
        env=ENV, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not _child_alive(marker):
        time.sleep(0.1)
    check("the slow command started", _child_alive(marker))

    proc.send_signal(signal.SIGTERM)
    try:
        _, err = proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, err = proc.communicate()
        check("exits promptly on SIGTERM", False, "still running after 30s")
        return
    check("exits promptly on SIGTERM", True)
    check("exit 143", proc.returncode == 143, f"{proc.returncode} {err[-200:]}")

    # The command ran in its own process group, so nothing kills it but us.
    time.sleep(0.5)
    check("no orphaned child", not _child_alive(marker))

    check("result file still written", out.is_file(), str(out))
    if out.is_file():
        payload = json.loads(out.read_text())
        check("status interrupted", payload["status"] == INTERRUPTED,
              str(payload["status"]))
        check("exit code recorded", payload["exit_code"] == 143,
              str(payload["exit_code"]))
    records = [json.loads(line)
               for path in (tmp / "term" / "logs").glob("*.jsonl")
               for line in path.read_text().splitlines() if line.strip()]
    check("session_end was still recorded",
          any(r["type"] == "session_end" for r in records))


# -- labels -----------------------------------------------------------------
def test_label(tmp: Path, base_url: str) -> None:
    print("--label")
    mock_server.set_script([("noted", [])])
    run(tmp / "lab", base_url, "-p", "remember this", "--label", "outline-step")
    records = [json.loads(line)
               for path in (tmp / "lab" / "logs").glob("*.jsonl")
               for line in path.read_text().splitlines() if line.strip()]
    start = next(r for r in records if r["type"] == "session_start")
    check("logged on the session", start["label"] == "outline-step",
          str(start.get("label")))

    listing = subprocess.run(
        argv(tmp / "lab", base_url, "--sessions"), env=ENV,
        capture_output=True, text=True, timeout=60,
    )
    check("shown in --sessions", "outline-step" in listing.stdout,
          listing.stdout[-200:])


# -- the exit-code mapping itself -------------------------------------------
def test_exit_code_mapping() -> None:
    print("status to exit code")
    for status, expected in [(OK, 0), (ERROR, 1), (MAX_STEPS, 3), (TIMEOUT, 4)]:
        check(f"{status} is {expected}",
              cli.exit_code(Outcome(status=status)) == expected)
    check("an unknown status is a failure, not a success",
          cli.exit_code(Outcome(status="something-new")) == 1)

    saved = cli._SIGNAL
    try:
        cli._SIGNAL = None
        check("Ctrl-C is 130", cli.exit_code(Outcome(status=INTERRUPTED)) == 130)
        cli._SIGNAL = signal.SIGTERM
        check("SIGTERM is 143", cli.exit_code(Outcome(status=INTERRUPTED)) == 143)
    finally:
        cli._SIGNAL = saved


def main_() -> int:
    server, base_url = mock_server.start()
    try:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            test_exit_code_mapping()
            test_exit_codes(tmp, base_url)
            test_no_prompt_without_a_terminal(tmp / "noprompt")
            test_piped_prompt_still_works(tmp / "piped", base_url)
            test_streams_are_separate(tmp, base_url)
            test_quiet(tmp, base_url)
            test_result_file(tmp, base_url)
            test_result_file_is_scrubbed(tmp, base_url)
            test_max_seconds(tmp, base_url)
            test_sigterm(tmp, base_url)
            test_label(tmp, base_url)
    finally:
        server.shutdown()
    return common.report()


if __name__ == "__main__":
    sys.exit(main_())
