# Security

The agent runs arbitrary shell commands because that is the point. **The container
is the security boundary, not the harness.** Nothing here tries to stop the agent
from doing damage inside its container; what it does is avoid spreading your
credentials into the artifacts that leave it.

## What the harness does

- **The API key never reaches the model or the screen.** It is stripped from the
  environment every command inherits, and removed from tool output before the
  model or your terminal sees it — so `cat .env` returns `OPENAI_API_KEY=[redacted]`
  while the file on disk is untouched.
- **It is not in any repr.** `Config.api_key` is `repr=False`, so a traceback or a
  debug print cannot dump it.
- **Secret-shaped strings are scrubbed from the log**: OpenAI `sk-…`, GitHub
  `ghp_…`/`github_pat_…`, AWS `AKIA…`, Slack `xox…`, Google `AIza…`,
  `Authorization: Bearer …`, `PRIVATE KEY` blocks, `PASSWORD=`/`TOKEN=`-style
  assignments, and passwords embedded in URLs. Only the value is replaced, so
  `AWS_SECRET_ACCESS_KEY=[redacted]` still reads sensibly. `--no-redact` turns the
  pattern matching off; the harness's own key is scrubbed either way.
- **Transcripts are private**: the log file is `0600` inside a `0700` directory, and
  a workspace the harness creates is `0700`. Logs hold full file contents and
  command output, so they are the most sensitive thing here.
- **Loose credential permissions are reported**: `.env` or an `--api-key-file`
  readable by other accounts prints a warning on startup.
- **The key can stay out of the environment entirely** with
  `--api-key-file PATH` (or `OPENAI_API_KEY_FILE`), which keeps it out of `env`
  dumps, `docker inspect`, and `/proc/<pid>/environ`.
- **The model is told that content is data.** The system prompt instructs it to
  treat file contents, command output and fetched pages as data, and to report
  rather than obey instructions found in them.

## What it does not protect, honestly

- **The agent shares a uid with the harness.** On Linux it can read
  `/proc/<harness pid>/environ` when the key came from the environment, and it can
  read an `--api-key-file` just as easily. Stripping the child environment stops
  accidents, not a determined agent. **Use a dedicated, low-value, rotatable key
  for anything you let an agent near**, and treat a container that ran an agent as
  having seen that key.
- **Do not mount host credentials.** `~/.ssh`, `~/.aws`, cloud metadata access, a
  Docker socket: all reachable by any command the agent runs. The `docker run`
  recipe that bind-mounts the source read-only also exposes `.env` to the agent —
  the built image does not, since `.dockerignore` excludes it.
- **Only the file tools are path-confined.** `read_file`/`write_file`/`edit_file`
  cannot leave the workspace, and symlinks are resolved before the check — but that
  is a guard rail against the model mis-aiming, not a sandbox. The shell goes
  wherever the container allows.
- **Prompt injection is mitigated by instruction only.** A model can be talked out
  of any instruction. Assume anything the agent reads can influence what it does,
  and keep secrets and reachable infrastructure out of the container.
- **MCP servers act outside the workspace.** Their tools reach real accounts, and
  their tokens sit in `mcp-state/`, readable by the agent like everything else.
  See [What MCP changes about security](mcp.md#what-mcp-changes-about-security).
- **No command approval, by design.** Nothing prompts before `rm -rf`. A blocklist
  would give a false sense of safety; a throwaway container is the real answer.

## Practical checklist

```sh
chmod 600 .env                  # or use --api-key-file
docker run --cap-drop=ALL --security-opt no-new-privileges --read-only \
  --memory 2g --cpus 2 --pids-limit 256 ...        # cap the blast radius
cd examples/hardened && docker compose up -d proxy  # and cap where it can reach
```

Use a scoped key per container, rotate it after the container is gone, keep the
workspace free of anything you would not paste into a chat, and read `logs/*.jsonl`
before sharing one — redaction catches known shapes, not everything.

[`examples/hardened/`](../examples/hardened/) has the full treatment with what each flag actually buys,
what was measured, and an explicit list of what it still does not protect
against. The most important item on that list: **the model endpoint is an open
channel.** An egress allowlist bounds where data can go, not whether it goes —
anything the agent reads can leave in a prompt, by design.
