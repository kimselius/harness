#!/usr/bin/env python3
"""Entry point: interactive prompt (or -p for one shot)."""

from __future__ import annotations

import atexit
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import context
import markdown
import sessions
import steer
import tools as toolkit
import ui
from agent import Agent
from config import (
    Config,
    anchor,
    build_parser,
    config_from_args,
    load_dotenv,
    normalize_effort,
)
from logbook import Logbook


def history_file(cfg: Config) -> Path:
    """REPL history, kept beside the session logs rather than in $HOME.

    The log directory is writable by definition, so this still works when the
    source tree is mounted read-only.
    """
    return cfg.log_path.parent / ".harness_history"

# --- terminal input --------------------------------------------------------
def setup_readline(path: Path) -> None:
    try:
        import readline
    except ImportError:
        return
    # Zero-width prompt markers, but only if this editor understands them.
    ui.enable_readline_prompts(readline)
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
    return steer.read_pending(fd, settle)


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



# --- commands --------------------------------------------------------------
# One table per command: help text, handler and argument completions together.
# Adding a command means adding one entry here and nothing else.


@dataclass(frozen=True)
class Command:
    name: str
    args: str  # argument hint shown in /help
    help: str
    run: Callable[[Agent, str], object]  # return False to leave the REPL
    complete: Callable[[Agent], list[str]] | None = None
    aliases: tuple[str, ...] = ()


THINK_LEVELS = ["minimal", "low", "medium", "high", "off"]


def cmd_help(agent: Agent, rest: str) -> None:
    print(HELP, end="")


def cmd_tools(agent: Agent, rest: str) -> None:
    for tool in toolkit.TOOLS:
        params = ", ".join(tool.schema["function"]["parameters"]["properties"])
        print(f"  {ui.bold(tool.name):<24} {ui.dim(params)}")


def cmd_model(agent: Agent, rest: str) -> None:
    cfg = agent.cfg
    if rest.strip():
        cfg.model = rest.strip()
        agent.model_changed()  # a new tokenizer invalidates the calibration
        agent.log.event("model_changed", model=cfg.model)
    ui.info(f"model: {cfg.model}  base_url: {cfg.base_url or 'api.openai.com'}")


def cmd_prompt(agent: Agent, rest: str) -> None:
    ui.info(f"source: {agent.cfg.prompt_source}")
    print(agent.cfg.system_prompt())


def cmd_history(agent: Agent, rest: str) -> None:
    ui.info(f"{len(agent.history)} messages in context")
    if not agent.history:
        return
    argument = rest.strip().lower()
    if argument in ("all", "full"):
        print_transcript(agent, limit=None)
    elif argument.isdigit():
        print_transcript(agent, limit=max(1, int(argument)))
    else:
        print_transcript(agent)


def cmd_cwd(agent: Agent, rest: str) -> None:
    cfg, session = agent.cfg, agent.session
    if rest.strip():
        target = (session.root / rest.strip()).resolve()
        if not target.is_dir():
            ui.warn(f"no such directory: {rest.strip()}")
        else:
            if not session.set_cwd(target):
                ui.warn("outside the workspace, reset to the root")
            agent.log.event("cwd_set", cwd=str(session.cwd))
    ui.info(f"{session.cwd}  (workspace root: {cfg.workspace})")


def complete_cwd(agent: Agent) -> list[str]:
    root = agent.session.root
    return sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_dir() and not path.name.startswith(".")
    )[:200]


def cmd_context(agent: Agent, rest: str) -> None:
    cfg = agent.cfg
    used = agent.used_tokens()
    measured = agent.meter.last_prompt_tokens
    ui.info(
        f"~{used} / {cfg.context_tokens} tokens "
        f"({100 * used / cfg.context_tokens:.0f}%) · "
        f"{len(agent.history)} messages · {agent.compactions} compactions"
    )
    ui.info(
        f"compact at {cfg.compact_threshold if cfg.compact else 'disabled'} · "
        f"keep {cfg.compact_keep_tokens} · "
        f"{agent.meter.chars_per_token:.2f} chars/token"
        + (
            f" (calibrated, last prompt {measured} tokens)"
            if measured
            else " (estimate)"
        )
    )
    ui.info(f"session total: {agent.session.totals_line()}")


def cmd_compact(agent: Agent, rest: str) -> None:
    if not agent.compact(reason="manual"):
        ui.info("nothing to compact yet")


def cmd_think(agent: Agent, rest: str) -> None:
    cfg = agent.cfg
    if rest.strip():
        cfg.reasoning_effort = normalize_effort(rest)
        agent.log.event("reasoning_effort_changed", value=cfg.reasoning_effort)
        agent.llm.supports_effort = True  # a new value gets a fresh chance
    ui.info(
        f"reasoning_effort: {cfg.reasoning_effort or 'off (model default)'}"
        + ("" if rest.strip() else "   (/think high, /think off)")
    )


def cmd_limits(agent: Agent, rest: str) -> None:
    cfg = agent.cfg
    ui.info(
        f"output: {cfg.max_output_chars} chars / {cfg.max_output_lines} lines · "
        f"shell timeout: {cfg.shell_timeout:g}s (max {cfg.max_shell_timeout:g}s) · "
        f"max steps: {cfg.max_steps}"
    )
    ui.info(
        f"reasoning_effort: {cfg.reasoning_effort or 'off (model default)'} · "
        f"show thinking: {'yes' if cfg.show_thinking else 'no'}"
        + (f" · extra body: {cfg.extra_body}" if cfg.extra_body else "")
    )


def cmd_reset(agent: Agent, rest: str) -> None:
    agent.reset()
    ui.info("history cleared")


def cmd_log(agent: Agent, rest: str) -> None:
    ui.info(f"{agent.cfg.log_path}  (session {agent.log.session_id})")


def cmd_sessions(agent: Agent, rest: str) -> None:
    list_sessions(agent.cfg.log_path.parent)


def cmd_resume(agent: Agent, rest: str) -> None:
    cfg = agent.cfg
    if not rest.strip():
        ui.warn("usage: /resume <session-id|last>")
        return
    try:
        path = sessions.resolve(cfg.log_path.parent, rest.strip())
    except sessions.SessionError as exc:
        ui.warn(str(exc))
        return
    if path == cfg.log_path:
        ui.info("already in that session")
        return
    agent.attach_log(Logbook(path, scrub=cfg.redactor().scrub_log))
    cfg.log_path = path
    agent.log.session_start(cfg, resumed=True)
    report_resume(agent, restore(agent))


def complete_resume(agent: Agent) -> list[str]:
    return ["last"] + [
        path.stem for path in sessions.session_files(agent.cfg.log_path.parent)
    ]


def cmd_exit(agent: Agent, rest: str) -> bool:
    return False


COMMANDS: list[Command] = [
    Command("help", "", "this list", cmd_help, aliases=("h", "?")),
    Command("tools", "", "list available tools", cmd_tools),
    Command("model", "[name]", "show or switch model", cmd_model,
            lambda agent: [agent.cfg.model]),
    Command("prompt", "", "show the active system prompt", cmd_prompt),
    Command("history", "[n|all]",
            "replay the conversation (default: last 12 messages)", cmd_history,
            lambda agent: ["all"]),
    Command("cwd", "[dir]", "show or set the shell's working directory", cmd_cwd,
            complete_cwd),
    Command("context", "", "token usage vs the context window", cmd_context),
    Command("compact", "", "summarize history now", cmd_compact),
    Command("think", "[level]", "show or set reasoning_effort (high, low, off, ...)",
            cmd_think, lambda agent: THINK_LEVELS),
    Command("limits", "", "current output/timeout limits and reasoning settings",
            cmd_limits),
    Command("reset", "", "clear conversation history", cmd_reset),
    Command("log", "", "path to the JSONL session log", cmd_log),
    Command("sessions", "", "list recorded sessions", cmd_sessions),
    Command("resume", "<id>", 'load an earlier session (id, prefix, or "last")',
            cmd_resume, complete_resume),
    Command("exit", "", "leave (also /quit, /q, or Ctrl-D)", cmd_exit,
            aliases=("quit", "q")),
]

BY_NAME: dict[str, Command] = {
    alias: command
    for command in COMMANDS
    for alias in (command.name, *command.aliases)
}

HELP = (
    "commands\n"
    + "".join(
        f"  /{command.name + (' ' + command.args if command.args else ''):<18} "
        f"{command.help}\n"
        for command in COMMANDS
    )
    + """input
  type while busy    messages are queued and picked up at the next step
  paste              multi-line pastes are kept together as one message
  <<<                type a multi-line message, end with a line containing >>>
  Tab                complete command names and their arguments
  Ctrl-C             interrupt the running turn (kills any running command)
"""
)


def completions(buffer: str, text: str, agent: Agent) -> list[str]:
    """Tab completions for the current line. Only commands, never prose."""
    line = buffer.lstrip()
    if not line.startswith("/"):
        return []
    head, space, _ = line.partition(" ")
    if not space:  # still typing the command itself
        prefix = head[1:].lower()
        return [f"/{command.name}" for command in COMMANDS
                if command.name.startswith(prefix)]

    command = BY_NAME.get(head[1:].lower())
    if command is None or command.complete is None:
        return []
    return [option for option in command.complete(agent) if option.startswith(text)]


def setup_completion(agent: Agent) -> bool:
    """Bind Tab to the command completer. False if readline cannot do it."""
    try:
        import readline
    except ImportError:
        return False

    def complete(text: str, state: int):
        try:
            matches = completions(readline.get_line_buffer(), text, agent)
        except Exception:
            return None  # never let completion break the prompt
        return matches[state] if state < len(matches) else None

    readline.set_completer(complete)
    # Keep "/" and "-" out of the delimiters so whole commands and session ids
    # are offered as single words.
    readline.set_completer_delims(" \t\n")
    try:
        if getattr(readline, "backend", "") == "editline":
            readline.parse_and_bind("bind ^I rl_complete")  # libedit syntax
        else:
            readline.parse_and_bind("tab: complete")
    except Exception:
        return False
    return True


def handle_command(line: str, agent: Agent) -> bool:
    """Run a slash command. Returns False when the REPL should exit."""
    name, _, rest = line[1:].strip().partition(" ")
    command = BY_NAME.get(name.lower())
    if command is None:
        ui.warn(f"unknown command /{name} — try /help")
        return True
    return command.run(agent, rest) is not False


def run_turn(agent: Agent, message: str) -> None:
    # Watch stdin for the duration of the turn so typing is queued, not lost.
    if agent.inbox:
        agent.inbox.start()
    try:
        agent.run(message)
    except KeyboardInterrupt:
        # Backstop: the agent handles interrupts per step, this catches the gaps.
        print()
        agent.note_interrupt("turn")
    except Exception as exc:
        ui.error(f"! {type(exc).__name__}: {exc}")
        agent.repair_history(reason=f"harness error: {exc}")
    finally:
        if agent.inbox:
            agent.inbox.stop()
    cfg = agent.cfg
    parts = []
    if agent.last_usage:
        usage = agent.last_usage
        detail = (
            f"tokens in={usage.get('prompt_tokens', '?')} "
            f"out={usage.get('completion_tokens', '?')}"
        )
        thought = agent.last_reasoning_tokens
        if thought:
            detail += f" (thinking {thought})"
        parts.append(detail)
    used = agent.used_tokens()
    parts.append(f"ctx ~{used}/{cfg.context_tokens} ({100 * used / cfg.context_tokens:.0f}%)")
    ui.info(f"  [{cfg.model}] " + " · ".join(parts))


def list_sessions(log_dir: Path, limit: int = 20) -> None:
    files = sessions.session_files(log_dir)
    if not files:
        ui.info(f"no sessions in {log_dir}")
        return
    for path in files[:limit]:
        print(sessions.format_row(sessions.summarize(path)))
    if len(files) > limit:
        ui.info(f"… and {len(files) - limit} older")


TRANSCRIPT_TAIL = 12


def print_transcript(agent: Agent, limit: int | None = TRANSCRIPT_TAIL) -> None:
    """Replay history to the terminal in the same shapes as a live turn."""
    history = agent.history
    shown = history if limit is None else history[-limit:]
    hidden = len(history) - len(shown)
    if hidden > 0:
        ui.info(f"… {hidden} earlier messages not shown (/history all for everything)")
    for message in shown:
        role = message.get("role")
        content = (message.get("content") or "").strip()
        if role == "user":
            # Harness-authored messages are context, not something you typed.
            if content.startswith(("[harness]", "<state>", context.SUMMARY_HEADER)):
                ui.info("  " + ui.one_line(content))
            else:
                print(ui.cyan("> ") + content)
        elif role == "assistant":
            if content:
                ui.assistant_prefix()
                print(markdown.render(content, style=ui.colors_enabled()))
            for call in message.get("tool_calls") or []:
                fn = call.get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {"raw_arguments": fn.get("arguments", "")}
                ui.tool_call(fn.get("name", "?"), args if isinstance(args, dict) else {})
        elif role == "tool":
            ui.tool_result(content, is_error=content.startswith("ERROR"))


def restore(agent: Agent) -> dict:
    """Load an earlier session's conversation into the agent. Prints nothing."""
    cfg = agent.cfg
    history, stats = sessions.load_history(
        cfg.log_path, cfg.max_output_chars, cfg.max_output_lines
    )
    agent.history = history
    # A killed process can leave tool calls unanswered; make the history valid.
    agent.repair_history(reason="not recorded in the session log")
    if history:
        agent.history.append({"role": "user", "content": context.RESUME_NOTE})
    agent.log.event(
        "session_restored",
        messages=len(agent.history),
        tokens=agent.used_tokens(),
        **stats,
    )
    return stats


def report_resume(agent: Agent, stats: dict, transcript: bool = True) -> None:
    """Say what was loaded, and show it. Called after the banner."""
    ui.info(
        f"resumed {agent.cfg.log_path.stem}: {len(agent.history)} messages, "
        f"~{agent.used_tokens()} tokens"
        + (
            f", {stats['compactions_applied']} compactions replayed"
            if stats["compactions_applied"]
            else ""
        )
    )
    if transcript and agent.history:
        print_transcript(agent)


def main() -> int:
    load_dotenv()
    args = build_parser().parse_args()
    if args.sessions:
        list_sessions(anchor(args.log_dir))
        return 0
    cfg = config_from_args(args)
    log = Logbook(cfg.log_path, scrub=cfg.redactor().scrub_log)
    log.session_start(cfg, resumed=cfg.resumed)
    agent = Agent(cfg, log)
    resume_stats = restore(agent) if cfg.resumed else None

    # One-shot: -p flag, or a message piped on stdin.
    oneshot = args.prompt
    if oneshot is None and not sys.stdin.isatty():
        piped = sys.stdin.read().strip()
        oneshot = piped or None
    if oneshot:
        if resume_stats is not None:
            report_resume(agent, resume_stats, transcript=False)
        run_turn(agent, oneshot)
        log.event("session_end", mode="oneshot", interrupted=agent.interrupted,
                  **agent.session.totals())
        return 130 if agent.interrupted else 0  # 130 = terminated by SIGINT

    setup_readline(history_file(cfg))
    setup_completion(agent)
    agent.inbox = steer.InputQueue()
    agent.inbox.on_queued = lambda text: ui.info("» queued, will be sent next step")
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
    if resume_stats is not None:
        report_resume(agent, resume_stats)

    prefix = ""  # text typed without Enter while the agent was working
    while True:
        try:
            # Show the shell's directory when it is not the workspace root, so
            # the state the model sees is visible to you as well.
            where = ("" if agent.session.cwd == agent.session.root
                     else ui.dim(f"{agent.session.cwd_label} "))
            line = prefix + read_message(ui.input_prompt(where + ui.cyan("> ")))
            prefix = ""
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
        # Anything typed during the final step of the turn runs as the next turn.
        while agent.inbox and agent.inbox.pending():
            queued = "\n".join(agent.inbox.drain())
            ui.info("» sending the message you typed while it was working")
            run_turn(agent, queued)
        if agent.inbox:
            prefix = agent.inbox.take_leftover()

    log.event("session_end", mode="interactive", messages=len(agent.history),
              **agent.session.totals())
    ui.info(f"session total: {agent.session.totals_line()}")
    ui.info("bye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
