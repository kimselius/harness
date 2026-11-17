# Tools

- **shell** — `sh -c` in the workspace root, combined stdout/stderr, exit code
  reported. Killed by process group on timeout (default 120s, per-call override
  capped at 900s). Harness credentials (`OPENAI_*`, `HARNESS_*`, `ANTHROPIC_*`) are
  stripped from the child environment.
- **read_file** — line-numbered text with `offset`/`limit` paging; refuses binaries
  and files over 20 MB.
- **write_file** — create or overwrite, parent directories created automatically.
- **edit_file** — exact substring find/replace. Fails if `old_string` is missing or
  ambiguous unless `replace_all` is set, so edits can't land in the wrong place.

Paths are resolved (symlinks included) and rejected if they fall outside the
workspace.

With MCP enabled, servers add their own tools next to these, as
`mcp__<server>__<tool>` — see [MCP](mcp.md). The four above are always first and
cannot be replaced.

## Two path rules, and the state block

The harness never asks the model to infer where it is. Each request ends with a
state block the harness computes:

```
<state>
workspace root: /srv/harness/workspace
file tool paths resolve from: /srv/harness/workspace (always)
shell working directory: /srv/harness/workspace/app (app)
</state>
```

It is appended to the message list rather than folded into the system prompt, so
the prompt stays byte-identical for the whole session and the server's cached
prefix survives, while the values are still current every turn. It is never stored
in the history, so there are no stale copies. The role is `user`, not `system`:
Gemma-style chat templates have no system role, and a trailing system message can
be dropped by the template.

The two rules it reports never change:

| | anchor |
| --- | --- |
| `read_file` / `write_file` / `edit_file` | always the workspace root |
| `shell` | its own working directory, which persists between calls |

So `cd app` then `git diff` works without prefixes, while `write_file app/README.md`
means the same file regardless of where the shell has wandered. Every path a tool
prints is workspace-root relative, so a path from an earlier result can always be
passed straight back.

```
cd app                     → cwd: app
git diff --stat            → runs in app
read_file app/main.py      → app/main.py, not app/app/main.py
```

The shell's directory is read back from the real `pwd` after each command, so a
`cd` inside a compound command sticks too. It cannot leave the workspace: a command
that ends up outside resets to the root and says so. If the directory is deleted
underneath it, the next command falls back to the root. `/cwd` shows it, `/cwd sub`
sets it, and the REPL prompt shows it when it is not the root.

Two more things exist so a wrong path cannot be silent: `write_file` reports any
directories it had to create (`new directory created: a, a/b`), and a missing file
reports the path it actually resolved to.

## Output limits

Tool results handed to the model are clamped to ~6000 chars / 250 lines, keeping the
head and tail with an explicit truncation marker. The **full, untruncated** output
always goes to the JSONL log, so nothing is lost for later follow-up. Tune with
`--max-output-chars` / `--max-output-lines`.

Each user message gets at most `--max-steps` (default 30) tool rounds. On overrun
the harness does **not** simply stop: it spends one more request asking the model
to say what it got done and what is left, with **no tools offered**. Withholding
the tools is what guarantees that call terminates — a model stuck in a loop cannot
loop again.

That turn is the difference between a usable result and a shrug. Without it the
run returned whatever text happened to be lying around, which after a round of
pure tool calls is a dangling intent — a real one, from a pipeline step that hit
the ceiling:

```json
{"status": "max_steps", "text": "Let me run a final integrity pass, then write
 the corrections file."}
```

The status and exit code are unchanged (`max_steps`, 3): the task still did not
finish on its own, and a driving script must still know that. Only the text
improves. If the closing call itself fails the run falls back to the old text and
logs `wrapup_error` rather than crashing.

`--max-seconds` deliberately does *not* get this treatment. It is a promise about
wall-clock time, and an extra request of unknown duration would break it, so a
deadline stops where it stands.
