# Documentation

Start with the [README](../README.md) to get it running. These pages cover
everything else.

**Running it**

- [Running in a container](docker.md) — the image, bind mounts and uid, hardening,
  and the no-build `docker run` recipe
- [Unattended runs](unattended.md) — `-p`, exit codes, `--result-file`, chaining
  steps, cron, signals
- [Interactive use](repl.md) — REPL commands, pasting, steering while it works,
  interrupting, terminal output
- [Sessions](sessions.md) — ids, `--resume`, what survives a crash
- [Platforms](platforms.md) — Linux, macOS, and why Windows means WSL or Docker

**How it behaves**

- [Tools](tools.md) — the four built-ins, the two path rules, output limits and
  `--max-steps`
- [Context window and compaction](context.md) — sizing the window and what a
  compaction keeps
- [Thinking](thinking.md) — `reasoning_effort` and endpoint differences
- [MCP](mcp.md) — servers, OAuth login, unattended use, security
- [Configuration](configuration.md) — flags, `.env`, the system prompt, where files
  go

**Operating it**

- [Security](security.md) — what the harness protects, and what it does not
- [Log format](logs.md) — the JSONL transcript and useful `jq` queries
- [Known limits](known-limits.md)
- [Development](development.md) — tests and source layout

**Worked examples**

- [`examples/report/`](../examples/report/) — a seven-step pipeline, notes in,
  report out
- [`examples/hardened/`](../examples/hardened/) — a locked-down container with an
  egress allowlist
