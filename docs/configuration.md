# Configuration

Every setting can be given three ways, highest priority first: a command-line flag,
an environment variable, or a line in `.env` next to the harness (real environment
variables win over `.env`). `.env.example` lists every variable with its default
and a note on when to change it; `main.py --help` lists the flags.

The settings with their own pages: [context window and compaction](context.md),
[thinking](thinking.md), [output limits](tools.md#output-limits),
[unattended runs](unattended.md) and [MCP](mcp.md).

## System prompt

If `PROMPT.md` exists it is used as the system prompt, otherwise the built-in one
is. It is re-read before every model request, so edits take effect on your next
message — no restart. An `<environment>` block is always appended so the model
knows its real constraints: workspace root, output limits, shell timeout, terminal
width, and the tool-use contract — act on files with `read_file`/`write_file`/
`edit_file`, never print contents or diffs for the user to copy and paste, never ask
the user to run a command it can run itself, and stop rather than retry when a tool
reports an interrupt. Those rules survive a custom `PROMPT.md` because they describe
the harness, not style.

```sh
cp PROMPT.md.example PROMPT.md
```

## Everything stays in this directory

Relative paths are resolved against the harness directory, not your shell's cwd,
so nothing is written outside the checkout:

| path | contents |
| --- | --- |
| `workspace/` | the only directory the agent can touch (`--workspace`) |
| `logs/<id>.jsonl` | one transcript per session (`--log-dir`) |
| `logs/.harness_history` | REPL line history (lives with the logs, so a read-only source tree still works) |
| `.env` | credentials and defaults |
| `mcp.json` | MCP servers (`--mcp-config`) |
| `mcp-state/` | MCP logins and tokens, `0600` files (`--mcp-state-dir`) |
