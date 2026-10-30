"""Append-only JSONL transcript of everything that crosses the wire."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sessions

# Hard cap per logged string, so a runaway command cannot produce a 2 GB log.
MAX_FIELD_CHARS = 1_000_000


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_FIELD_CHARS:
        return value[:MAX_FIELD_CHARS] + f"...[clipped {len(value) - MAX_FIELD_CHARS}]"
    if isinstance(value, list):
        return [_clip(v) for v in value]
    if isinstance(value, dict):
        return {k: _clip(v) for k, v in value.items()}
    return value


class Logbook:
    def __init__(self, path: Path, scrub: Any = None) -> None:
        self.path = path
        # Transcripts hold full file contents and command output, so they are
        # private by default: 0700 on the directory, 0600 on the file.
        self.scrub = scrub or (lambda value: value)
        try:
            self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        except OSError as exc:
            raise SystemExit(
                f"Cannot create log directory {self.path.parent}: {exc.strerror}. "
                "Pass --log-dir with a writable path."
            ) from None
        # The filename *is* the session id, and a resumed session keeps appending
        # to its own file, so seq numbering has to continue where it left off.
        self.session_id = path.stem
        self._seq = sessions.last_seq(path)

    def event(self, kind: str, **fields: Any) -> None:
        self._seq += 1
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "session": self.session_id,
            "seq": self._seq,
            "type": kind,
            **_clip(fields),
        }
        line = json.dumps(self.scrub(record), ensure_ascii=False, default=str)
        existed = self.path.exists()
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        if not existed:
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    def session_start(self, cfg: Any, resumed: bool = False) -> None:
        self.event(
            "session_resumed" if resumed else "session_start",
            model=cfg.model,
            base_url=cfg.base_url,
            workspace=str(cfg.workspace),
            prompt_source=cfg.prompt_source,
            pid=os.getpid(),
            limits={
                "max_output_chars": cfg.max_output_chars,
                "max_output_lines": cfg.max_output_lines,
                "shell_timeout": cfg.shell_timeout,
                "max_steps": cfg.max_steps,
                "context_tokens": cfg.context_tokens,
                "compact_threshold": cfg.compact_threshold if cfg.compact else None,
                "compact_keep_tokens": cfg.compact_keep_tokens,
            },
        )
