#!/usr/bin/env python3
"""Entry point: interactive prompt (or -p for one shot)."""

from __future__ import annotations

import atexit
import os
import select
import sys
from pathlib import Path

import tools as toolkit
import ui
from agent import Agent
from config import Config, build_parser, config_from_args, load_dotenv
from logbook import Logbook


def history_file(cfg: Config) -> Path:
    """REPL history, kept beside the session logs rather than in $HOME.

    The log directory is writable by definition, so this still works when the
    source tree is mounted read-only.
    """
    return cfg.log_path.parent / ".harness_history"

HELP = """\
commands
  /help              this list
  /tools             list available tools
  /model [name]      show or switch model
  /prompt            show the active system prompt
  /history           message count in context
  /cwd [dir]         show or set the shell's working directory
  /context           token usage vs the context window
  /compact           summarize history now
  /reset             clear conversation history
  /log               path to the JSONL session log
  /limits            current output/timeout limits
  /exit, /quit       leave (Ctrl-D also works)
input
  paste              multi-line pastes are kept together as one message
  <<<                type a multi-line message, end with a line containing >>>
  Ctrl-C             interrupt the running turn (kills any running command)
"""


def setup_readline(path: Path) -> None:
    try:
        import readline
    except ImportError:
        return
    try:
        readline.read_history_file(path)
    except OSError:
        pass
    readline.set_history_length(2000)
    atexit.register(lambda: _save_history(readline, path))


def _save_history(readline, path: Path) -> None:
    try:
        readline.write_history_file(path)
    except OSError:
        pass


# Terminals send a paste as one burst, so anything still buffered when input()
# returns is the rest of that paste rather than a new message.
PASTE_SETTLE = 0.05
BRACKET_START = "\x1b[200~"
BRACKET_END = "\x1b[201~"


def drain_buffered(fd: int, settle: float = PASTE_SETTLE) -> str:
    """Read input that is already waiting on fd. Empty string if there is none."""
    chunks: list[str] = []
    timeout = 0.0  # typed lines must not pay any latency for this
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


def assemble_paste(first: str, rest: str, read_more=input) -> str:
    """Join a first line with the buffered remainder of a paste."""
    text = f"{first}\n{rest}"
    for marker in (BRACKET_START, BRACKET_END):
        text = text.replace(marker, "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if not text.endswith("\n"):
        # The paste ended mid-line, so the user has not pressed Enter yet.
        tail = lines.pop()
        try:
            tail += read_more()
        except (EOFError, KeyboardInterrupt):
            pass
        lines.append(tail)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def read_message(prompt: str) -> str:
    """One message from the user, pasted multi-line text included."""
    first = input(prompt)
    if first.strip() == "<<<" or not sys.stdin.isatty():
        return first
    rest = drain_buffered(sys.stdin.fileno())
    if not rest.strip():
        return first
    message = assemble_paste(first, rest)
    count = len(message.split("\n"))
    if count > 1:
        ui.info(f"({count} lines pasted)")
    return message


def read_multiline() -> str:
    ui.info("multi-line mode: finish with a line containing only >>>")
    lines: list[str] = []
    while True:
        try:
            line = input("… ")
        except EOFError:
            break
        if line.strip() == ">>>":
            break
        lines.append(line)
    return "\n".join(lines)


def handle_command(line: str, agent: Agent) -> bool:
    """Returns True if the loop should continue, False to exit."""
    cmd, _, rest = line[1:].strip().partition(" ")
    cmd = cmd.lower()
    cfg = agent.cfg

    if cmd in ("exit", "quit", "q"):
        return False
    if cmd in ("help", "h", "?"):
        print(HELP, end="")
    elif cmd == "tools":
        for schema in toolkit.TOOL_SCHEMAS:
            fn = schema["function"]
            params = ", ".join(fn["parameters"]["properties"])
            print(f"  {ui.bold(fn['name']):<24} {ui.dim(params)}")
    elif cmd == "model":
        if rest.strip():
            cfg.model = rest.strip()
            agent.log.event("model_changed", model=cfg.model)
        ui.info(f"model: {cfg.model}  base_url: {cfg.base_url or 'api.openai.com'}")
    elif cmd == "prompt":
        ui.info(f"source: {cfg.prompt_source}")
        print(cfg.system_prompt())
    elif cmd == "history":
        ui.info(f"{len(agent.history)} messages in context")
    elif cmd == "context":
        used = agent.used_tokens()
        pct = 100 * used / cfg.context_tokens
        measured = agent.meter.last_prompt_tokens
        ui.info(
            f"~{used} / {cfg.context_tokens} tokens ({pct:.0f}%) · "
            f"{len(agent.history)} messages · {agent.compactions} compactions"
        )
        ui.info(
            f"compact at {cfg.compact_threshold if cfg.compact else 'disabled'} · "
            f"keep {cfg.compact_keep_tokens} · "
            f"{agent.meter.chars_per_token:.2f} chars/token"
            + (f" (calibrated, last prompt {measured} tokens)" if measured else " (estimate)")
        )
    elif cmd == "compact":
        if not agent.compact(reason="manual"):
            ui.info("nothing to compact yet")
    elif cmd == "reset":
        agent.reset()
        ui.info("history cleared")
    elif cmd == "cwd":
        if rest.strip():
            target = (cfg.workspace / rest.strip()).resolve()
            if not target.is_dir():
                ui.warn(f"no such directory: {rest.strip()}")
            elif not cfg.set_cwd(target):
                ui.warn("outside the workspace, reset to the root")
            agent.log.event("cwd_set", cwd=str(cfg.cwd))
        ui.info(f"{cfg.cwd}  (workspace root: {cfg.workspace})")
    elif cmd == "log":
        ui.info(f"{cfg.log_path}  (session {agent.log.session_id})")
    elif cmd == "limits":
        ui.info(
            f"output: {cfg.max_output_chars} chars / {cfg.max_output_lines} lines · "
            f"shell timeout: {cfg.shell_timeout:g}s (max {cfg.max_shell_timeout:g}s) · "
            f"max steps: {cfg.max_steps}"
        )
    else:
        ui.warn(f"unknown command /{cmd} — try /help")
    return True


def run_turn(agent: Agent, message: str) -> None:
    try:
        agent.run(message)
    except KeyboardInterrupt:
        # Backstop: the agent handles interrupts per step, this catches the gaps.
        print()
        agent.note_interrupt("turn")
    except Exception as exc:
        ui.error(f"! {type(exc).__name__}: {exc}")
        agent.repair_history(reason=f"harness error: {exc}")
    cfg = agent.cfg
    parts = []
    if agent.last_usage:
        usage = agent.last_usage
        parts.append(
            f"tokens in={usage.get('prompt_tokens', '?')} "
            f"out={usage.get('completion_tokens', '?')}"
        )
    used = agent.used_tokens()
    parts.append(f"ctx ~{used}/{cfg.context_tokens} ({100 * used / cfg.context_tokens:.0f}%)")
    ui.info(f"  [{cfg.model}] " + " · ".join(parts))


def main() -> int:
    load_dotenv()
    args = build_parser().parse_args()
    cfg = config_from_args(args)
    log = Logbook(cfg.log_path)
    log.session_start(cfg)
    agent = Agent(cfg, log)

    # One-shot: -p flag, or a message piped on stdin.
    oneshot = args.prompt
    if oneshot is None and not sys.stdin.isatty():
        piped = sys.stdin.read().strip()
        oneshot = piped or None
    if oneshot:
        run_turn(agent, oneshot)
        log.event("session_end", mode="oneshot", interrupted=agent.interrupted)
        return 130 if agent.interrupted else 0  # 130 = terminated by SIGINT

    setup_readline(history_file(cfg))
    print(
        ui.bold("harness")
        + ui.dim(" — /help for commands, Ctrl-C to interrupt, Ctrl-D to exit")
    )
    ui.info(
        f"model {cfg.model} · workspace {cfg.workspace} · prompt {cfg.prompt_source}"
    )
    ui.info(
        f"context {cfg.context_tokens} tokens · "
        + (f"compact at {cfg.compact_threshold}" if cfg.compact else "compaction off")
    )
    ui.info(f"log {cfg.log_path}")

    while True:
        try:
            line = read_message(ui.cyan("> "))
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            # Ctrl-C interrupts work, it does not quit the session.
            print()
            ui.info("(interrupted — Ctrl-D or /exit to quit)")
            continue
        line = line.strip()
        if not line:
            continue
        # A multi-line paste is a message, never a command, even if it opens with /.
        if line.startswith("/") and "\n" not in line:
            if not handle_command(line, agent):
                break
            continue
        if line == "<<<":
            line = read_multiline()
            if not line.strip():
                continue
        run_turn(agent, line)

    log.event("session_end", mode="interactive", messages=len(agent.history))
    ui.info("bye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
