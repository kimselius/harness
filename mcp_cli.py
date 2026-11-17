"""The terminal side of MCP: management commands, /mcp, prompts, and answering
servers that need the user.

Kept apart from main.py so the REPL stays readable and nothing here is imported
until MCP is actually in use — the SDK costs a few hundred milliseconds to load,
and a run without MCP should not pay it.
"""

from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path
from typing import Any

import ui
from config import Config, anchor


class _NullLog:
    def event(self, kind: str, **fields: Any) -> None:
        pass


def _load(path: Path):
    import mcp_config

    try:
        return mcp_config.load(path)
    except mcp_config.McpConfigError as exc:
        raise SystemExit(f"mcp: {exc}") from None


def _server(config: Any, name: str):
    spec = config.servers.get(name)
    if spec is None:
        known = ", ".join(config.servers) or "none configured"
        raise SystemExit(f"mcp: no server named {name!r} in {config.path} ({known})")
    return spec


# -- --mcp-login / --mcp-logout / --mcp-list -------------------------------------
def manage(args: argparse.Namespace) -> int | None:
    """Run a management command if one was given. None means "not one of ours".

    These work whether or not MCP_ENABLED is set: logging in is something you
    do before turning it on, and they need no model or API key.
    """
    if not (args.mcp_login or args.mcp_logout or args.mcp_list):
        return None
    import mcp_auth

    config = _load(anchor(args.mcp_config))
    store = mcp_auth.StateStore(anchor(args.mcp_state_dir))

    if args.mcp_logout:
        spec = _server(config, args.mcp_logout)
        removed = store.delete(spec.name)
        ui.info(f"{spec.name}: " + ("stored login removed" if removed else "nothing stored"))
        return 0

    if args.mcp_login:
        spec = _server(config, args.mcp_login)

        def out(text: str) -> None:
            print(text, file=sys.stderr, flush=True)

        try:
            return mcp_auth.login(spec, store, out, open_browser=not args.no_browser)
        except KeyboardInterrupt:
            ui.warn("! login cancelled")
            return 130
        except Exception as exc:
            ui.error(f"! login to {spec.name} failed: {_short(exc)}")
            return 1

    return list_servers(config, store, anchor(args.workspace), anchor(args.log_dir),
                        args.mcp_connect_timeout)


def list_servers(config: Any, store: Any, workspace: Path, log_dir: Path,
                 connect_timeout: float) -> int:
    """--mcp-list: connect to everything and say what came back."""
    import mcp_auth
    import mcp_client

    if not config.servers:
        ui.info(f"no servers in {config.path}")
        return 0
    runtime = mcp_client.McpRuntime(
        config,
        state_dir=store.root,
        workspace=workspace,
        log=_NullLog(),
        log_file=log_dir / "mcp-list.mcp.log",
        connect_timeout=connect_timeout,
    )
    runtime.start()
    try:
        runtime.connect_all()
        for server in runtime.servers.values():
            spec = server.spec
            print(f"{ui.bold(spec.name)}  {ui.dim(spec.describe())}")
            print(f"  auth     {mcp_auth.status(store, spec)}")
            print(f"  status   {runtime.summary(server)}")
            if server.protocol:
                print(f"  protocol {server.protocol}"
                      + (f" · {server.server_info}" if server.server_info else ""))
            for tool in server.tools:
                mark = "" if server.allowed(tool.name) else ui.dim("  (not exposed)")
                print(f"    {mcp_client.tool_name(spec.name, tool.name)}{mark}")
        failed = runtime.failures(include_optional=True)
        return 1 if failed else 0
    finally:
        runtime.shutdown()


# -- startup -------------------------------------------------------------------
def start(cfg: Config, agent: Any, log: Any, interactive: bool):
    """Connect the configured servers and install their tools into the agent.

    Returns the runtime, or None when MCP is off or nothing is configured.
    """
    if not cfg.mcp_enabled:
        return None
    config = _load(cfg.mcp_config)
    cfg.add_secrets(config.secrets)
    if not config.enabled:
        ui.warn(f"! MCP is enabled but {cfg.mcp_config} has no enabled servers")
        return None

    import mcp_client

    _warn_state_dir(cfg)
    runtime = mcp_client.McpRuntime(
        config,
        state_dir=cfg.mcp_state_dir,
        workspace=cfg.workspace,
        log=log,
        log_file=mcp_client.mcp_log_path(cfg.log_path),
        timeout=cfg.mcp_timeout,
        connect_timeout=cfg.mcp_connect_timeout,
        llm=agent.llm,
        on_usage=agent.session.add_usage,
        add_secrets=cfg.add_secrets,
        warn=ui.warn,
        info=ui.info,
    )
    runtime.interactive = interactive
    if interactive:
        runtime.bridge.handler = TerminalAnswers(agent)
    runtime.start()
    with ui.Ticker(f"⟳ connecting to {len(config.enabled)} MCP server(s) ·"):
        runtime.connect_all()
    agent.mcp = runtime
    runtime.sync(agent.tools)
    report(runtime)
    return runtime


def _warn_state_dir(cfg: Config) -> None:
    try:
        inside = cfg.mcp_state_dir.resolve().is_relative_to(cfg.workspace.resolve())
    except OSError:
        return
    if inside:
        ui.warn(f"! the MCP state dir {cfg.mcp_state_dir} is inside the workspace: "
                "the agent will see the stored tokens in every listing")


def report(runtime: Any) -> None:
    for server in runtime.servers.values():
        if not server.spec.enabled:
            continue
        line = f"mcp {server.name}: {runtime.summary(server)}"
        if server.status == "connected":
            ui.info(line)
        else:
            ui.warn(f"! {line}")


def unavailable(runtime: Any) -> str | None:
    """Why an unattended run must not go ahead, or None.

    A pipeline step written to use a server would otherwise run without it and
    quietly do something else. `"optional": true` opts a server out of this.
    """
    failed = runtime.failures()
    if not failed:
        return None
    return "; ".join(f"{s.name}: {s.error or s.status}" for s in failed)


# -- answering servers that need the user ------------------------------------------
class TerminalAnswers:
    """Answers a server's UserRequest at the terminal. Main thread only.

    Runs in the middle of a tool call, so it pauses the two things that would
    otherwise fight it for the terminal: the steering input queue, which is
    reading stdin, and the tool's status line, which rewrites the current line.
    """

    def __init__(self, agent: Any) -> None:
        self.agent = agent

    def __call__(self, request: Any) -> Any:
        agent = self.agent
        ticker = agent.active_ticker
        inbox = agent.inbox
        if ticker:
            ticker.stop()
        watching = bool(inbox and inbox.running)
        if watching:
            inbox.stop()
        try:
            handler = getattr(self, f"_{request.kind}", None)
            return handler(request) if handler else None
        except (EOFError, KeyboardInterrupt):
            ui.info("(declined)")
            return {"action": "cancel"} if request.kind == "elicit_form" else None
        finally:
            if watching:
                inbox.start()
            if ticker:
                ticker.start()

    @staticmethod
    def _ask(prompt: str) -> str:
        return input(ui.input_prompt(ui.cyan(prompt))).strip()

    def _sampling(self, request: Any) -> bool:
        p = request.payload
        ui.warn(f"? {request.server} asks to use the model "
                f"(up to {p['max_tokens']} tokens, {p['messages']} messages)")
        if p["system"]:
            ui.info(f"  system: {ui.one_line(p['system'], 100)}")
        if p["last"]:
            ui.info(f"  last:   {ui.one_line(p['last'], 100)}")
        if p["tools"]:
            ui.info(f"  tools:  {', '.join(p['tools'])}")
        return self._ask("  allow? [y/N] ").lower() in ("y", "yes")

    def _elicit_url(self, request: Any) -> str:
        p = request.payload
        ui.warn(f"? {request.server}: {p['message']}")
        ui.info(f"  open: {p['url']}")
        answer = self._ask("  press Enter when done, or n to decline: ").lower()
        return "decline" if answer in ("n", "no") else "accept"

    def _elicit_form(self, request: Any) -> dict[str, Any]:
        p = request.payload
        schema = p.get("schema") or {}
        props = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        ui.warn(f"? {request.server}: {p['message']}")
        if self._ask("  answer? [Y/n] ").lower() in ("n", "no"):
            return {"action": "decline"}
        content: dict[str, Any] = {}
        for name, prop in props.items():
            value = self._field(name, prop, name in required)
            if value is not None:
                content[name] = value
        return {"action": "accept", "content": content}

    def _field(self, name: str, prop: dict[str, Any], required: bool) -> Any:
        label = prop.get("title") or name
        kind = prop.get("type", "string")
        choices = _choices(prop)
        default = prop.get("default")
        hint = []
        if prop.get("description"):
            ui.info(f"    {prop['description']}")
        if choices:
            hint.append("/".join(str(c) for c in choices))
        if default is not None:
            hint.append(f"default {default}")
        if not required:
            hint.append("optional")
        suffix = f" ({', '.join(hint)})" if hint else ""
        while True:
            raw = self._ask(f"  {label}{suffix}: ")
            if not raw:
                if default is not None:
                    return default
                if not required:
                    return None
                ui.warn("  required")
                continue
            try:
                value = _coerce(raw, kind)
            except ValueError:
                ui.warn(f"  expected {kind}")
                continue
            if choices and value not in choices:
                ui.warn(f"  one of: {', '.join(str(c) for c in choices)}")
                continue
            return value


def _choices(prop: dict[str, Any]) -> list[Any]:
    if "enum" in prop:
        return list(prop["enum"])
    options = prop.get("oneOf") or prop.get("anyOf") or []
    return [o["const"] for o in options if isinstance(o, dict) and "const" in o]


def _coerce(raw: str, kind: str) -> Any:
    if kind == "boolean":
        lowered = raw.lower()
        if lowered in ("y", "yes", "true", "1"):
            return True
        if lowered in ("n", "no", "false", "0"):
            return False
        raise ValueError(raw)
    if kind == "integer":
        return int(raw)
    if kind == "number":
        return float(raw)
    return raw


# -- prompts as slash commands ---------------------------------------------------
class PromptError(Exception):
    pass


def expand_prompt(runtime: Any, line: str) -> str | None:
    """`/mcp__server__prompt args…` -> the message to send. None if it is not one.

    Arguments are `name=value`, or positional in the order the server declares
    them. Every message the prompt returns goes into the one user message, so a
    resumed session replays exactly what the model saw.
    """
    if runtime is None or not line.startswith("/mcp__"):
        return None
    head, _, rest = line[1:].partition(" ")
    found = runtime.prompt_commands().get(head)
    if found is None:
        raise PromptError(f"no MCP prompt /{head} (see /mcp prompts)")
    server, prompt = found
    declared = [a.name for a in prompt.arguments or []]
    try:
        words = shlex.split(rest)
    except ValueError as exc:
        raise PromptError(f"could not parse the arguments: {exc}") from None
    values: dict[str, str] = {}
    positional = iter(declared)
    for word in words:
        name, eq, value = word.partition("=")
        if eq and name in declared:
            values[name] = value
            continue
        try:
            slot = next(n for n in positional if n not in values)
        except StopIteration:
            raise PromptError(f"too many arguments; /{head} takes "
                              f"{', '.join(declared) or 'none'}") from None
        values[slot] = word
    missing = [a.name for a in prompt.arguments or [] if a.required and a.name not in values]
    if missing:
        raise PromptError(f"/{head} needs {', '.join(missing)}")
    messages = runtime.get_prompt(server.name, prompt.name, values)
    if len(messages) == 1:
        return messages[0]["content"]
    return "\n\n".join(f"[{m['role']}]\n{m['content']}" for m in messages)


def complete(runtime: Any, buffer: str, text: str) -> list[str]:
    """Tab completions for prompt commands and their arguments."""
    if runtime is None:
        return []
    line = buffer.lstrip()
    head, space, rest = line.partition(" ")
    commands = runtime.prompt_commands()
    if not space:
        return [f"/{name}" for name in commands if f"/{name}".startswith(head)]
    found = commands.get(head[1:])
    if found is None:
        return []
    server, prompt = found
    declared = [a.name for a in prompt.arguments or []]
    done = len(rest.split()) - (0 if rest.endswith(" ") or not rest else 1)
    if done >= len(declared):
        return []
    values = runtime.complete_argument(server.name, prompt.name, declared[done], text)
    return [v for v in values if v.startswith(text)]


# -- /mcp ------------------------------------------------------------------------
MCP_HELP = """\
  /mcp                      servers and their status
  /mcp tools <server>       every tool the server offers, exposed or not
  /mcp resources [server]   resources and templates
  /mcp prompts              prompts, usable as /mcp__<server>__<prompt>
  /mcp login <server>       log in (OAuth), then reconnect
  /mcp logout <server>      forget the stored login and disconnect
  /mcp reconnect <server>   drop the connection and connect again
"""


def command(agent: Any, rest: str) -> None:
    runtime = agent.mcp
    if runtime is None:
        ui.info("MCP is off — set MCP_ENABLED=1 in .env (or pass --mcp) and list "
                f"servers in {agent.cfg.mcp_config}")
        return
    import mcp_auth

    words = rest.split()
    action = words[0].lower() if words else ""
    name = words[1] if len(words) > 1 else None
    if action in ("", "status", "list"):
        for server in runtime.servers.values():
            line = f"  {ui.bold(server.name):<20} {runtime.summary(server)}"
            print(line)
            print(ui.dim(f"      {server.spec.describe()} · "
                         f"{mcp_auth.status(runtime.store, server.spec)}"))
        return
    if action == "help":
        print(MCP_HELP, end="")
        return
    if action == "prompts":
        commands = runtime.prompt_commands()
        if not commands:
            ui.info("no prompts")
        for cmd, (_server, prompt) in commands.items():
            args = " ".join(f"<{a.name}>" if a.required else f"[{a.name}]"
                            for a in prompt.arguments or [])
            print(f"  /{cmd} {args}".rstrip())
            if prompt.description:
                print(ui.dim(f"      {prompt.description}"))
        return
    if action == "resources":
        try:
            print(runtime.describe_resources(name))
        except Exception as exc:
            ui.info(str(exc))
        return
    if name is None or name not in runtime.servers:
        ui.warn(f"usage: /mcp {action or 'help'} <server> — servers: "
                + ", ".join(runtime.servers))
        return
    server = runtime.servers[name]
    if action == "tools":
        import mcp_client

        for tool in server.tools:
            exposed = mcp_client.tool_name(name, tool.name)
            mark = "" if server.allowed(tool.name) else ui.dim(" (not exposed)")
            print(f"  {ui.bold(exposed)}{mark}")
            if tool.description:
                print(ui.dim(f"      {ui.one_line(tool.description, 100)}"))
        return
    if action == "logout":
        runtime.store.delete(name)
        runtime.reconnect(name)
        runtime.sync(agent.tools)
        ui.info(f"{name}: stored login removed")
        return
    if action == "login":
        def out(text: str) -> None:
            print(text, file=sys.stderr, flush=True)

        try:
            code = mcp_auth.login(server.spec, runtime.store, out)
        except KeyboardInterrupt:
            ui.warn("! login cancelled")
            return
        except Exception as exc:
            ui.error(f"! login failed: {_short(exc)}")
            return
        if code != 0:
            return
        action = "reconnect"
    if action == "reconnect":
        fresh = runtime.reconnect(name)
        runtime.sync(agent.tools)
        ui.info(f"{name}: {runtime.summary(fresh)}")
        return
    ui.warn(f"unknown: /mcp {action} — try /mcp help")


def complete_command(agent: Any) -> list[str]:
    runtime = agent.mcp
    actions = ["tools", "resources", "prompts", "login", "logout", "reconnect", "help"]
    if runtime is None:
        return actions
    return actions + list(runtime.servers)


def _short(exc: BaseException) -> str:
    import mcp_client

    return mcp_client._describe(exc)
