"""Working directory checks. Run: .venv/bin/python tests/test_cwd.py"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import common  # noqa: E402
from common import check  # noqa: E402


def make_cfg(workspace: Path):
    """Config plus the Session the tools now mutate."""
    cfg = common.make_config(workspace)
    return cfg, Session(cfg.workspace)


import tools as toolkit  # noqa: E402
from session import Session  # noqa: E402
from util import ToolError, ToolInterrupted  # noqa: E402


def sh(cfg, session, command: str, **kw) -> str:
    return toolkit.tool_shell({"command": command, **kw}, cfg, session)


def body(result: str) -> str:
    return result.split("--- output ---\n", 1)[-1].strip()


def test_starts_at_root(tmp: Path) -> None:
    print("initial directory")
    cfg, session = make_cfg(tmp / "a")
    check("cwd is the workspace", session.cwd == cfg.workspace)
    check("label is dot", session.cwd_label == ".")
    check("pwd agrees", body(sh(cfg, session, "pwd")) == str(cfg.workspace))
    check("status reports cwd", "cwd: ." in sh(cfg, session, "true"))


def test_cd_persists(tmp: Path) -> None:
    print("cd persists between calls")
    cfg, session = make_cfg(tmp / "b")
    (cfg.workspace / "one" / "two").mkdir(parents=True)
    sh(cfg, session, "cd one")
    check("after cd one", session.cwd_label == "one", session.cwd_label)
    check("next call starts there", body(sh(cfg, session, "pwd")).endswith("/one"))
    sh(cfg, session, "cd two")
    check("relative cd nests", session.cwd_label == "one/two", session.cwd_label)
    sh(cfg, session, "cd ..")
    check("cd .. goes back up", session.cwd_label == "one", session.cwd_label)
    sh(cfg, session, "cd")  # bare cd would go $HOME in a shell; clamped here
    check("bare cd stays inside the workspace",
          session.cwd == cfg.workspace or cfg.workspace in session.cwd.parents,
          str(session.cwd))


def test_escape_is_clamped(tmp: Path) -> None:
    print("escaping the workspace")
    cfg, session = make_cfg(tmp / "c")
    result = sh(cfg, session, "cd ../../.. && pwd")
    check("reset to the root", session.cwd == cfg.workspace, str(session.cwd))
    check("told the model", "left the workspace" in result, result.splitlines()[0])
    check("following call is back at the root",
          body(sh(cfg, session, "pwd")) == str(cfg.workspace))


def test_marker_hidden(tmp: Path) -> None:
    print("probe stays invisible")
    cfg, session = make_cfg(tmp / "d")
    for command in ["pwd", "echo hi", "true", "ls /", "printf 'no trailing newline'"]:
        result = sh(cfg, session, command)
        check(f"no marker in {command!r}", toolkit.CWD_MARKER not in result)
    check("exit code survives the wrapper", "exit code: 3" in sh(cfg, session, "exit 3"))
    check("stderr still captured", "nope" in body(sh(cfg, session, "echo nope >&2")))
    check("multiline command works", body(sh(cfg, session, "echo a\necho b")) == "a\nb")
    # A command that exits early never reaches the probe: cwd must not change.
    sh(cfg, session, "mkdir -p sub && cd sub")
    before = session.cwd
    sh(cfg, session, "exit 7")
    check("early exit leaves cwd alone", session.cwd == before, str(session.cwd))


def test_file_tools_are_root_anchored(tmp: Path) -> None:
    print("file tools ignore the shell cwd")
    cfg, session = make_cfg(tmp / "e")
    (cfg.workspace / "proj").mkdir()
    (cfg.workspace / "proj" / "f.txt").write_text("alpha\n", encoding="utf-8")
    (cfg.workspace / "f.txt").write_text("root file\n", encoding="utf-8")
    sh(cfg, session, "cd proj")
    check("shell did move", session.cwd_label == "proj", session.cwd_label)

    check("reads the root copy, not the child one",
          "root file" in toolkit.tool_read_file({"path": "f.txt"}, cfg, session))
    check("result shows the root-relative path",
          toolkit.tool_read_file({"path": "f.txt"}, cfg, session).startswith("f.txt "))

    # The path a result printed must round-trip to the same file. This is the
    # regression: it used to resolve against the cwd and double the prefix.
    shown = toolkit.tool_read_file({"path": "proj/f.txt"}, cfg, session).splitlines()[0]
    check("printed path round-trips", shown.startswith("proj/f.txt"), shown)
    toolkit.tool_write_file({"path": "proj/f2.txt", "content": "x\n"}, cfg, session)
    check("no doubled prefix", (cfg.workspace / "proj" / "f2.txt").is_file())
    check("nothing created at proj/proj",
          not (cfg.workspace / "proj" / "proj").exists())

    toolkit.tool_write_file({"path": "new.txt", "content": "x\n"}, cfg, session)
    check("bare name lands at the root", (cfg.workspace / "new.txt").is_file())
    check("not in the shell's directory",
          not (cfg.workspace / "proj" / "new.txt").exists())

    toolkit.tool_edit_file(
        {"path": "proj/f.txt", "old_string": "alpha", "new_string": "beta"}, cfg, session
    )
    check("edit hits the intended file",
          (cfg.workspace / "proj" / "f.txt").read_text() == "beta\n")
    check("root copy untouched",
          (cfg.workspace / "f.txt").read_text() == "root file\n")

    # A missing file states what was resolved, without guessing intent.
    try:
        toolkit.tool_read_file({"path": "nope.txt"}, cfg, session)
        check("missing file errors", False)
    except ToolError as exc:
        check("missing file errors", True)
        check("error names the resolved path",
              "nope.txt" in str(exc) and str(cfg.workspace) in str(exc), str(exc))

    # New directories are reported, so a wrong path cannot be silent.
    out = toolkit.tool_write_file({"path": "a/b/c.txt", "content": "y\n"}, cfg, session)
    check("new directories reported", "new directory created: a, a/b" in out, out)
    out = toolkit.tool_write_file({"path": "a/b/d.txt", "content": "y\n"}, cfg, session)
    check("existing directories not reported", "new directory" not in out, out)

    for bad in ["../../etc/passwd", "/etc/passwd"]:
        try:
            toolkit.tool_read_file({"path": bad}, cfg, session)
            check(f"still refuses {bad}", False)
        except ToolError:
            check(f"still refuses {bad}", True)


def test_state_block(tmp: Path) -> None:
    print("state block")
    from agent import Agent
    from logbook import Logbook

    cfg, _ = make_cfg(tmp / "state")
    (cfg.workspace / "sub").mkdir()
    agent = Agent(cfg, Logbook(cfg.log_path))
    session = agent.session  # the state block reads the agent's own session
    agent.history = [{"role": "user", "content": "hi"}]

    prompt_before = cfg.system_prompt()
    messages = agent._messages()
    check("state block is last", messages[-1]["content"].startswith("<state>"),
          messages[-1]["content"][:20])
    check("sent as user role", messages[-1]["role"] == "user")
    check("names the workspace", str(cfg.workspace) in messages[-1]["content"])
    check("says cwd is the root", "(.)" in messages[-1]["content"],
          messages[-1]["content"])

    sh(cfg, session, "cd sub")
    after = agent._messages()
    check("state block tracks the cwd", "(sub)" in after[-1]["content"],
          after[-1]["content"])
    check("system prompt is byte-identical", cfg.system_prompt() == prompt_before)
    check("history keeps no copy of the state",
          all("<state>" not in (m.get("content") or "") for m in agent.history))
    check("history untouched", len(agent.history) == 1)


def test_git_in_child(tmp: Path) -> None:
    print("git in a child directory")
    cfg, session = make_cfg(tmp / "f")
    sh(cfg, session, "mkdir -p repo && cd repo && git init -q . "
            "&& printf 'a\\nb\\n' > f.txt && git add -A "
            "&& git -c user.email=t@t -c user.name=t commit -qm init "
            "&& printf 'a\\nCHANGED\\n' > f.txt")
    check("cd inside a compound command sticks", session.cwd_label == "repo", session.cwd_label)
    diff = body(sh(cfg, session, "git diff --stat"))
    check("git diff works with no path prefix", "f.txt" in diff and "1 file changed" in diff,
          diff)
    check("git status works", "f.txt" in body(sh(cfg, session, "git status --porcelain")))
    check("git log works", "init" in body(sh(cfg, session, "git log --oneline")))


def test_interrupt_keeps_cwd(tmp: Path) -> None:
    print("interrupt during a command")
    cfg, session = make_cfg(tmp / "g")
    (cfg.workspace / "sub").mkdir()
    sh(cfg, session, "cd sub")
    timer = threading.Timer(1.0, lambda: os.kill(os.getpid(), signal.SIGINT))
    timer.daemon = True
    timer.start()
    try:
        sh(cfg, session, "echo before; sleep 30", timeout=60)
        check("interrupted", False, "no exception")
    except ToolInterrupted as exc:
        check("interrupted", True)
        check("partial output kept", "before" in exc.partial, repr(exc.partial))
        check("no marker leaked", toolkit.CWD_MARKER not in exc.partial)
    check("cwd preserved", session.cwd_label == "sub", session.cwd_label)


def test_missing_cwd(tmp: Path) -> None:
    print("deleted working directory")
    cfg, session = make_cfg(tmp / "h")
    (cfg.workspace / "gone").mkdir()
    sh(cfg, session, "cd gone")
    subprocess.run(["rm", "-rf", str(cfg.workspace / "gone")], check=True)
    result = sh(cfg, session, "pwd")  # must not raise: falls back to the root
    check("falls back to the root", body(result) == str(cfg.workspace), body(result))


def test_repl_command(tmp: Path) -> None:
    print("/cwd command")
    from agent import Agent
    from logbook import Logbook
    from main import handle_command

    cfg, _ = make_cfg(tmp / "i")
    (cfg.workspace / "here").mkdir()
    agent = Agent(cfg, Logbook(cfg.log_path))
    session = agent.session  # /cwd moves the agent's session, not a detached one
    check("/cwd keeps running", handle_command("/cwd", agent) is True)
    handle_command("/cwd here", agent)
    check("/cwd sets the directory", session.cwd_label == "here", session.cwd_label)
    check("shell sees it", body(sh(cfg, session, "pwd")).endswith("/here"))
    handle_command("/cwd missing-dir", agent)
    check("unknown dir ignored", session.cwd_label == "here", session.cwd_label)
    handle_command("/cwd ..", agent)
    check("/cwd .. is clamped to the root", session.cwd == cfg.workspace, str(session.cwd))


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        test_starts_at_root(tmp)
        test_cd_persists(tmp)
        test_escape_is_clamped(tmp)
        test_marker_hidden(tmp)
        test_file_tools_are_root_anchored(tmp)
        test_state_block(tmp)
        test_git_in_child(tmp)
        test_interrupt_keeps_cwd(tmp)
        test_missing_cwd(tmp)
        test_repl_command(tmp)
    return common.report()


if __name__ == "__main__":
    sys.exit(main())
