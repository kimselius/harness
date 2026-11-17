# Sessions

Every run is a session with an id like `20261026-105057-b1c53e`, and its log is
`logs/<id>.jsonl` — the filename *is* the id.

```sh
.venv/bin/python main.py --sessions            # list them
.venv/bin/python main.py --resume              # continue the most recent
.venv/bin/python main.py --resume 20261026-105057-b1c53e
.venv/bin/python main.py --resume 20261026-10  # unambiguous prefix is enough
```

Resuming replays the conversation from the log, prints it to the terminal so you
can see where you left off, and appends new events to that same file, so one session stays one file however many times you come back to it.
`seq` numbering continues where it stopped. In the REPL, `/sessions` lists and
`/resume <id>` switches without restarting.

The log is the only state, so a session survives the process being killed:

- A half-written final line (a `kill -9` mid-write) is skipped.
- Tool calls with no recorded result are answered with an error on load, so the
  replayed history is valid for strict APIs.
- Tool output is re-truncated with your *current* limits rather than stored twice,
  so lowering `--max-output-chars` shrinks a resumed session's context too.
- Recorded compactions are replayed, so a session that compacted three times comes
  back at its compacted size instead of re-expanding to everything.
- A note is appended telling the model it is a new process, so it does not assume a
  command it started earlier is still running.

The replay shows the last 12 messages in the same shapes a live turn uses — your
prompts, tool calls, tool results, replies — with a count of anything older.
`/history all` prints the lot, `/history 40` a specific number. Harness-authored
messages (the resume note, compaction summaries) are indented and dimmed rather than
shown as things you typed. One-shot `-p` runs skip the replay so their output stays
machine-readable.

What is *not* restored: the shell's working directory (back to the workspace root)
and anything the earlier process had in memory. Files in `workspace/` are untouched,
since they live on disk.
