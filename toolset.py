"""The tools one agent can call: the built-ins plus whatever MCP servers add.

`tools.py` declares the built-in tools and stays a fixed list. This is the
registry the agent actually uses, and it can change at runtime — MCP servers are
only known once they connect, and a server may announce a new tool list
mid-session. Everything that sends, counts or dispatches tools goes through one
ToolSet, so the advertised list and the dispatched list cannot drift apart.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import tools as builtin
from config import Config
from session import Session
from util import ToolError

BUILTIN = "built-in"

# (args, cfg, session, progress) -> text for the model. `progress(detail)` updates
# the status line while the tool runs; built-ins never call it.
Runner = Callable[[dict, Config, Session, Callable[[str], None] | None], str]


@dataclass(frozen=True)
class Entry:
    schema: dict[str, Any]
    run: Runner
    source: str = BUILTIN  # "built-in", or the MCP server it came from

    @property
    def name(self) -> str:
        return self.schema["function"]["name"]


def _wrap(tool: builtin.Tool) -> Runner:
    def run(args: dict, cfg: Config, session: Session, progress=None) -> str:
        return tool.run(args, cfg, session)

    return run


class ToolSet:
    def __init__(self, entries: list[Entry] | None = None) -> None:
        self._entries: dict[str, Entry] = {}
        # Bumped on every change, so the agent can tell when the schemas it is
        # metering are stale without comparing them.
        self.version = 0
        # Extra text for the system prompt's environment block, written by
        # whoever added tools that need explaining (the MCP layer).
        self.notes = ""
        for entry in entries or []:
            self._entries[entry.name] = entry

    @classmethod
    def builtin(cls) -> ToolSet:
        return cls([Entry(tool.schema, _wrap(tool)) for tool in builtin.TOOLS])

    # -- reading -----------------------------------------------------------
    @property
    def schemas(self) -> list[dict[str, Any]]:
        return [entry.schema for entry in self._entries.values()]

    def names(self) -> list[str]:
        return list(self._entries)

    def get(self, name: str) -> Entry | None:
        return self._entries.get(name)

    def source(self, name: str) -> str | None:
        entry = self._entries.get(name)
        return entry.source if entry else None

    def groups(self) -> dict[str, list[Entry]]:
        """Entries by where they came from, built-ins first."""
        out: dict[str, list[Entry]] = {}
        for entry in self._entries.values():
            out.setdefault(entry.source, []).append(entry)
        return out

    @staticmethod
    def schema_chars(entries: list[Entry]) -> int:
        """What these tools cost on the wire, per request."""
        return len(json.dumps([entry.schema for entry in entries], default=str))

    # -- changing ----------------------------------------------------------
    def replace_source(self, source: str, entries: list[Entry]) -> None:
        """Swap every tool from `source` for `entries`.

        A built-in name is never replaced: a server must not be able to shadow
        `shell` or `write_file`. MCP names are prefixed, so this only matters for
        a deliberately hostile or broken entry.
        """
        if source == BUILTIN:
            raise ValueError("built-in tools are fixed")
        kept = {n: e for n, e in self._entries.items() if e.source != source}
        for entry in entries:
            existing = kept.get(entry.name)
            if existing is not None and existing.source == BUILTIN:
                continue
            kept[entry.name] = entry
        self._entries = kept
        self.version += 1

    def remove_source(self, source: str) -> None:
        self.replace_source(source, [])

    # -- dispatch ----------------------------------------------------------
    def execute(
        self,
        name: str,
        args: dict,
        cfg: Config,
        session: Session,
        progress: Callable[[str], None] | None = None,
    ) -> str:
        entry = self._entries.get(name)
        if entry is None:
            raise ToolError(
                f"unknown tool {name!r}; available: {', '.join(self._entries)}"
            )
        if not isinstance(args, dict):
            raise ToolError("tool arguments must be a JSON object")
        return entry.run(args, cfg, session, progress)
