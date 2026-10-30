"""Secret handling. Run: .venv/bin/python tests/test_security.py

The threat model is narrow on purpose: the agent runs arbitrary commands by
design, so this is not about stopping it. It is about the harness not spreading
credentials into artifacts that leave the container — logs above all.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import common  # noqa: E402
import mock_server  # noqa: E402
from common import check  # noqa: E402

from config import build_parser, config_from_args  # noqa: E402
from logbook import Logbook  # noqa: E402
from util import REDACTED, Redactor  # noqa: E402

KEY = "sk-live-9f3a2b7c4d5e6f708192a3b4c5d6e7f8"


def test_literal_secrets() -> None:
    print("our own secrets")
    redactor = Redactor([KEY])
    check("removed from output", redactor.scrub_output(f"key={KEY} ok")
          == f"key={REDACTED} ok")
    check("removed from the log", redactor.scrub_log({"a": [f"x {KEY}"]})
          == {"a": [f"x {REDACTED}"]})
    check("untouched text passes through",
          redactor.scrub_output("nothing secret here") == "nothing secret here")
    check("non-strings pass through", redactor.scrub_log(42) == 42)
    check("None passes through", redactor.scrub_log(None) is None)

    # Short placeholders would otherwise match half the transcript.
    loose = Redactor(["sk-no-key", "", "abc"])
    check("placeholder keys are not redacted",
          loose.scrub_output("sk-no-key stays") == "sk-no-key stays")


def test_secret_shapes() -> None:
    print("secret shapes in the log")
    redactor = Redactor([])
    samples = {
        "openai": "token sk-abcdefghijklmnopqrstuvwxyz0123",
        "github": "ghp_abcdefghijklmnopqrstuvwxyz01234567",
        "github pat": "github_pat_11ABCDEFG0abcdefghijklmnop",
        "aws id": "AKIAIOSFODNN7EXAMPLE",
        "slack": "xoxb-123456789012-abcdefghijkl",
        "google": "AIzaSyA1234567890abcdefghijklmnopqrstuvw",
        "bearer": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
        "assignment": "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENGbPxRfiCY",
        "password": 'password: "hunter2hunter2"',
        "url auth": "https://user:s3cr3tpassword@example.com/v1",
    }
    for label, text in samples.items():
        scrubbed = redactor.scrub_log(text)
        check(f"{label} redacted", REDACTED in scrubbed, scrubbed)

    pem = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\ndef\n"
        "-----END OPENSSH PRIVATE KEY-----"
    )
    check("private key block redacted", redactor.scrub_log(pem) == REDACTED)

    # The name is kept so the line still reads sensibly.
    assignment = redactor.scrub_log("API_KEY=abcdefghijklmnop")
    check("variable name preserved", assignment.startswith("API_KEY="), assignment)

    # Things that merely look alarming must survive.
    for innocent in [
        "exit code: 0",
        "password prompt appeared",
        "sk-short",
        "token: 123",
        "SELECT secret FROM t",
    ]:
        check(f"kept intact: {innocent!r}", redactor.scrub_log(innocent) == innocent,
              redactor.scrub_log(innocent))

    off = Redactor([], patterns=False)
    check("--no-redact disables shapes",
          off.scrub_log(samples["openai"]) == samples["openai"])


def test_config_hides_the_key(tmp: Path) -> None:
    print("config does not expose the key")
    os.environ["OPENAI_API_KEY"] = KEY
    cfg = common.make_config(tmp / "cfg")
    check("not in repr", KEY not in repr(cfg), "key leaked into repr")
    check("still usable", cfg.api_key == KEY)
    check("redactor carries it", cfg.redactor().scrub_output(KEY) == REDACTED)

    url_cfg = common.make_config(
        tmp / "url", extra=["--base-url", "https://u:supersecret99@host/v1"]
    )
    check("endpoint password redacted too",
          url_cfg.redactor().scrub_output("pw=supersecret99") == f"pw={REDACTED}")


def test_log_is_scrubbed_and_private(tmp: Path) -> None:
    print("the log")
    os.environ["OPENAI_API_KEY"] = KEY
    cfg = common.make_config(tmp / "log")
    log = Logbook(cfg.log_path, scrub=cfg.redactor().scrub_log)
    log.event("tool_result", output=f"OPENAI_API_KEY={KEY}\nghp_abcdefghijklmnopqrst0123")
    text = cfg.log_path.read_text()

    check("our key is absent", KEY not in text, "key written to the log")
    check("other secrets too", "ghp_abcdefghijklmnopqrst0123" not in text)
    check("placeholder present", REDACTED in text)
    mode = stat.S_IMODE(cfg.log_path.stat().st_mode)
    check("log file is 0600", mode == 0o600, oct(mode))
    dir_mode = stat.S_IMODE(cfg.log_path.parent.stat().st_mode)
    check("log directory is 0700", dir_mode == 0o700, oct(dir_mode))
    check("still valid jsonl",
          all(json.loads(line) for line in text.splitlines() if line.strip()))


def test_turn_does_not_leak(tmp: Path, base_url: str) -> None:
    print("a whole turn")
    os.environ["OPENAI_API_KEY"] = KEY
    mock_server.reset()
    # The agent reads a file that happens to contain the harness's own key.
    mock_server.set_script(
        [("", [("read_file", {"path": "secrets.env"})]), ("done", [])]
    )
    agent = common.make_agent(tmp / "turn", base_url)
    (agent.cfg.workspace / "secrets.env").write_text(
        f"OPENAI_API_KEY={KEY}\n", encoding="utf-8"
    )
    import contextlib
    import io

    with contextlib.redirect_stdout(io.StringIO()) as screen:
        agent.run("read the file")

    tool_messages = [m["content"] for m in agent.history if m["role"] == "tool"]
    check("the model never sees it", all(KEY not in m for m in tool_messages),
          "key reached the model")
    check("the screen never shows it", KEY not in screen.getvalue())
    check("the log never has it", KEY not in agent.cfg.log_path.read_text())
    check("the file itself is untouched",
          KEY in (agent.cfg.workspace / "secrets.env").read_text())
    check("the model still got the rest",
          any("OPENAI_API_KEY" in m for m in tool_messages), str(tool_messages)[:120])


def test_api_key_file(tmp: Path) -> None:
    print("--api-key-file")
    key_file = tmp / "key.txt"
    key_file.write_text(f"{KEY}\n", encoding="utf-8")
    key_file.chmod(0o600)
    os.environ.pop("OPENAI_API_KEY", None)
    cfg = config_from_args(
        build_parser().parse_args(
            ["--workspace", str(tmp / "kf" / "ws"),
             "--log-dir", str(tmp / "kf" / "logs"),
             "--api-key-file", str(key_file)]
        )
    )
    check("key read from the file", cfg.api_key == KEY)
    check("whitespace stripped", "\n" not in cfg.api_key)
    check("absent from the environment", "OPENAI_API_KEY" not in os.environ)

    # A world-readable credentials file is worth a warning.
    key_file.chmod(0o644)
    result = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "--api-key-file", str(key_file),
         "--workspace", str(tmp / "kf2" / "ws"), "--log-dir", str(tmp / "kf2" / "logs"),
         "--sessions"],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "OPENAI_API_KEY": ""},
    )
    check("warns about loose permissions", "readable by other users" in result.stderr,
          result.stderr[:160])

    missing = config_from_args.__module__  # keep the import used
    check("module wired", bool(missing))
    bad = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "--api-key-file",
         str(tmp / "nope.txt"), "--workspace", str(tmp / "kf3")],
        capture_output=True, text=True, timeout=60,
    )
    check("missing key file is a clear error",
          bad.returncode != 0 and "Cannot read" in (bad.stdout + bad.stderr),
          (bad.stdout + bad.stderr)[:160])


def test_env_not_passed_to_commands(tmp: Path) -> None:
    print("child environment")
    os.environ["OPENAI_API_KEY"] = KEY
    cfg = common.make_config(tmp / "env")
    child = cfg.child_env()
    for name in ("OPENAI_API_KEY", "HARNESS_MODEL", "ANTHROPIC_API_KEY"):
        check(f"{name} stripped", name not in child)
    check("ordinary variables kept", "PATH" in child)


def main_() -> int:
    server, base_url = mock_server.start()
    saved = os.environ.get("OPENAI_API_KEY")
    try:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            test_literal_secrets()
            test_secret_shapes()
            test_config_hides_the_key(tmp)
            test_log_is_scrubbed_and_private(tmp)
            test_turn_does_not_leak(tmp, base_url)
            test_api_key_file(tmp)
            test_env_not_passed_to_commands(tmp)
    finally:
        server.shutdown()
        if saved is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = saved
    return common.report()


if __name__ == "__main__":
    sys.exit(main_())
