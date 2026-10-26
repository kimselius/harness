"""Working directory checks. Run: .venv/bin/python tests/test_cwd.py"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tools as toolkit  # noqa: E402
from config import build_parser, config_from_args  # noqa: E402
from util import ToolError, ToolInterrupted  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        failures.append(label)
        print(f"  FAIL {label} {detail}")


def make_cfg(workspace: Path):
    os.environ.setdefault("OPENAI_API_KEY", "x")
    return config_from_args(
        build_parser().parse_args(
            ["--workspace", str(workspace), "--log-dir", str(workspace.parent / "logs")]
        )
    )


def sh(cfg, command: str, **kw) -> str:
    return toolkit.tool_shell({"command": command, **kw}, cfg)


def body(result: str) -> str:
    return result.split("--- output ---\n", 1)[-1].strip()


def test_starts_at_root(tmp: Path) -> None:
    print("initial directory")
    cfg = make_cfg(tmp / "a")
    check("cwd is the workspace", cfg.cwd == cfg.workspace)
    check("label is dot", cfg.cwd_label == ".")
    check("pwd agrees", body(sh(cfg, "pwd")) == str(cfg.workspace))
    check("status reports cwd", "cwd: ." in sh(cfg, "true"))


def test_cd_persists(tmp: Path) -> None:
    print("cd persists between calls")
    cfg = make_cfg(tmp / "b")
    (cfg.workspace / "one" / "two").mkdir(parents=True)
    sh(cfg, "cd one")
    check("after cd one", cfg.cwd_label == "one", cfg.cwd_label)
    check("next call starts there", body(sh(cfg, "pwd")).endswith("/one"))
    sh(cfg, "cd two")
    check("relative cd nests", cfg.cwd_label == "one/two", cfg.cwd_label)
    sh(cfg, "cd ..")
    check("cd .. goes back up", cfg.cwd_label == "one", cfg.cwd_label)
    sh(cfg, "cd")  # bare cd would go $HOME in a shell; clamped here
    check("bare cd stays inside the workspace",
          cfg.cwd == cfg.workspace or cfg.workspace in cfg.cwd.parents,
          str(cfg.cwd))


def test_escape_is_clamped(tmp: Path) -> None:
    print("escaping the workspace")
    cfg = make_cfg(tmp / "c")
    result = sh(cfg, "cd ../../.. && pwd")
    check("reset to the root", cfg.cwd == cfg.workspace, str(cfg.cwd))
    check("told the model", "left the workspace" in result, result.splitlines()[0])
    check("following call is back at the root",
          body(sh(cfg, "pwd")) == str(cfg.workspace))


def test_marker_hidden(tmp: Path) -> None:
    print("probe stays invisible")
    cfg = make_cfg(tmp / "d")
    for command in ["pwd", "echo hi", "true", "ls /", "printf 'no trailing newline'"]:
        result = sh(cfg, command)
        check(f"no marker in {command!r}", toolkit.CWD_MARKER not in result)
    check("exit code survives the wrapper", "exit code: 3" in sh(cfg, "exit 3"))
    check("stderr still captured", "nope" in body(sh(cfg, "echo nope >&2")))
    check("multiline command works", body(sh(cfg, "echo a\necho b")) == "a\nb")
    # A command that exits early never reaches the probe: cwd must not change.
    sh(cfg, "mkdir -p sub && cd sub")
    before = cfg.cwd
    sh(cfg, "exit 7")
    check("early exit leaves cwd alone", cfg.cwd == before, str(cfg.cwd))


def test_file_tools_follow(tmp: Path) -> None:
    print("file tools follow the cwd")
    cfg = make_cfg(tmp / "e")
    (cfg.workspace / "proj").mkdir()
    (cfg.workspace / "proj" / "f.txt").write_text("alpha\n", encoding="utf-8")
    (cfg.workspace / "f.txt").write_text("root file\n", encoding="utf-8")

    check("reads from the root first",
          "root file" in toolkit.tool_read_file({"path": "f.txt"}, cfg))
    sh(cfg, "cd proj")
    check("now reads the child copy",
          "alpha" in toolkit.tool_read_file({"path": "f.txt"}, cfg))
    toolkit.tool_write_file({"path": "new.txt", "content": "x\n"}, cfg)
    check("writes into the child", (cfg.workspace / "proj" / "new.txt").is_file())
    check("not into the root", not (cfg.workspace / "new.txt").exists())
    toolkit.tool_edit_file(
        {"path": "f.txt", "old_string": "alpha", "new_string": "beta"}, cfg
    )
    check("edits the child copy",
          (cfg.workspace / "proj" / "f.txt").read_text() == "beta\n")
    check("root copy untouched",
          (cfg.workspace / "f.txt").read_text() == "root file\n")
    check("paths shown relative to the root",
          "proj/f.txt" in toolkit.tool_read_file({"path": "f.txt"}, cfg))
    # Escapes are still refused, now measured from the cwd.
    for bad in ["../../etc/passwd", "/etc/passwd"]:
        try:
            toolkit.tool_read_file({"path": bad}, cfg)
            check(f"still refuses {bad}", False)
        except ToolError:
            check(f"still refuses {bad}", True)
    check("parent inside the workspace is allowed",
          "root file" in toolkit.tool_read_file({"path": "../f.txt"}, cfg))


def test_git_in_child(tmp: Path) -> None:
    print("git in a child directory")
    cfg = make_cfg(tmp / "f")
    sh(cfg, "mkdir -p repo && cd repo && git init -q . "
            "&& printf 'a\\nb\\n' > f.txt && git add -A "
            "&& git -c user.email=t@t -c user.name=t commit -qm init "
            "&& printf 'a\\nCHANGED\\n' > f.txt")
    check("cd inside a compound command sticks", cfg.cwd_label == "repo", cfg.cwd_label)
    diff = body(sh(cfg, "git diff --stat"))
    check("git diff works with no path prefix", "f.txt" in diff and "1 file changed" in diff,
          diff)
    check("git status works", "f.txt" in body(sh(cfg, "git status --porcelain")))
    check("git log works", "init" in body(sh(cfg, "git log --oneline")))


def test_interrupt_keeps_cwd(tmp: Path) -> None:
    print("interrupt during a command")
    cfg = make_cfg(tmp / "g")
    (cfg.workspace / "sub").mkdir()
    sh(cfg, "cd sub")
    timer = threading.Timer(1.0, lambda: os.kill(os.getpid(), signal.SIGINT))
    timer.daemon = True
    timer.start()
    try:
        sh(cfg, "echo before; sleep 30", timeout=60)
        check("interrupted", False, "no exception")
    except ToolInterrupted as exc:
        check("interrupted", True)
        check("partial output kept", "before" in exc.partial, repr(exc.partial))
        check("no marker leaked", toolkit.CWD_MARKER not in exc.partial)
    check("cwd preserved", cfg.cwd_label == "sub", cfg.cwd_label)


def test_missing_cwd(tmp: Path) -> None:
    print("deleted working directory")
    cfg = make_cfg(tmp / "h")
    (cfg.workspace / "gone").mkdir()
    sh(cfg, "cd gone")
    subprocess.run(["rm", "-rf", str(cfg.workspace / "gone")], check=True)
    result = sh(cfg, "pwd")  # must not raise: falls back to the root
    check("falls back to the root", body(result) == str(cfg.workspace), body(result))


def test_repl_command(tmp: Path) -> None:
    print("/cwd command")
    from agent import Agent
    from logbook import Logbook
    from main import handle_command

    cfg = make_cfg(tmp / "i")
    (cfg.workspace / "here").mkdir()
    agent = Agent(cfg, Logbook(cfg.log_path))
    check("/cwd keeps running", handle_command("/cwd", agent) is True)
    handle_command("/cwd here", agent)
    check("/cwd sets the directory", cfg.cwd_label == "here", cfg.cwd_label)
    check("shell sees it", body(sh(cfg, "pwd")).endswith("/here"))
    handle_command("/cwd missing-dir", agent)
    check("unknown dir ignored", cfg.cwd_label == "here", cfg.cwd_label)
    handle_command("/cwd ..", agent)
    check("/cwd .. is clamped to the root", cfg.cwd == cfg.workspace, str(cfg.cwd))


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        test_starts_at_root(tmp)
        test_cd_persists(tmp)
        test_escape_is_clamped(tmp)
        test_marker_hidden(tmp)
        test_file_tools_follow(tmp)
        test_git_in_child(tmp)
        test_interrupt_keeps_cwd(tmp)
        test_missing_cwd(tmp)
        test_repl_command(tmp)
    print()
    if failures:
        print(f"{len(failures)} failed: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
