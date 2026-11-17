# harness

A small agent harness for any OpenAI-compatible chat API. Four tools (`shell`,
`read_file`, `write_file`, `edit_file`), a REPL, output truncation, timeouts, and a
JSONL transcript of every request, response, tool call and tool result.

- Works with any OpenAI-compatible endpoint: OpenAI, OpenRouter, Ollama, vLLM,
  llama.cpp.
- Interactive REPL with steering (type while it works), paste handling and Ctrl-C
  that never leaves processes behind.
- One-shot mode for scripts and cron, with honest exit codes and a JSON result file.
- Sessions you can resume, and context compaction that keeps your original request
  word for word.
- Optional MCP client, off unless you turn it on.

Built for throwaway VPS / container use: there is no sandboxing beyond confining
file paths to a workspace directory. **The container is the security boundary** —
do not point it at a machine you care about.

## Quick start with Docker

**1. Configure.** Copy the example and fill in your key, model and endpoint:

```sh
cp .env.example .env
chmod 600 .env
```

```sh
OPENAI_API_KEY=sk-...
HARNESS_MODEL=gpt-4o-mini
# OPENAI_BASE_URL=http://host.docker.internal:11434/v1   # e.g. Ollama on the host
```

`docker --env-file` does not strip quotes, so write values bare. A model server
running on your own machine is not `localhost` from inside the container: use
`host.docker.internal` (Docker Desktop) or `--network host` (Linux).

**2. Build.**

```sh
docker build -t harness .
```

Ubuntu 24.04 with Python, `curl git jq ripgrep less procps`, `tini` as PID 1, and
an unprivileged user (uid 1001). `.env` is never baked into the image.

**3. Run.** Give it a workspace and a log directory on the host, so the agent's
work and the transcripts outlive the container:

```sh
mkdir -p workspace logs
docker run -it --rm \
  --env-file .env \
  -v "$PWD/workspace":/ws -e HARNESS_WORKSPACE=/ws \
  -v "$PWD/logs":/logs     -e HARNESS_LOG_DIR=/logs \
  --memory 2g --cpus 2 --pids-limit 512 \
  harness
```

That opens the REPL. Type a request; `/help` lists commands, Ctrl-C stops the
current turn, Ctrl-D quits.

On **Linux** the mounted directories must be writable by uid 1001 — run
`sudo chown -R 1001:1001 workspace logs` once, or add `--user "$(id -u):$(id -g)"`.
Docker Desktop on macOS and Windows handles this for you.

**One shot.** Anything after the image name goes to the harness, so `-p` runs a
single task and exits. stdout carries only the answer; progress goes to stderr:

```sh
docker run --rm --env-file .env \
  -v "$PWD/workspace":/ws -e HARNESS_WORKSPACE=/ws \
  harness -p "summarize every file in this directory" > summary.txt
echo $?   # 0 = done; see docs/unattended.md for the rest
```

**Pick up where you left off.** Every run is a session logged to `logs/`:

```sh
docker run -it --rm --env-file .env \
  -v "$PWD/workspace":/ws -e HARNESS_WORKSPACE=/ws \
  -v "$PWD/logs":/logs     -e HARNESS_LOG_DIR=/logs \
  harness --resume          # most recent; --sessions lists them
```

**Your own system prompt.** Copy `PROMPT.md.example` to `PROMPT.md` and either
rebuild, or mount it to iterate without rebuilding:
`-v "$PWD/PROMPT.md":/harness/PROMPT.md:ro`. It is re-read before every request.

Next steps: [locking the container down further](docs/docker.md) and
[`examples/hardened/`](examples/hardened/) for an egress allowlist;
[`examples/report/`](examples/report/) for a multi-step pipeline.

## Without Docker

Python 3.11+ on Linux or macOS (on Windows, use WSL or Docker):

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # add your key + model

.venv/bin/python main.py                          # interactive
.venv/bin/python main.py -p "check disk usage"    # one shot
echo "summarize README.md" | .venv/bin/python main.py
.venv/bin/python main.py --base-url http://localhost:11434/v1 --model qwen2.5-coder
```

The agent's shell then runs on your machine with your permissions. Only the file
tools are confined to `workspace/`; the shell is not.

## Documentation

Everything else lives in [`docs/`](docs/README.md):

| | |
| --- | --- |
| [Running in a container](docs/docker.md) | the image, uid and mounts, hardening, the no-build recipe |
| [Unattended runs](docs/unattended.md) | exit codes, `--result-file`, chaining, cron, signals |
| [Interactive use](docs/repl.md) | REPL commands, pasting, steering, interrupting |
| [Sessions](docs/sessions.md) | resuming, and what survives a crash |
| [Tools](docs/tools.md) | the built-ins, path rules, output limits |
| [Context and compaction](docs/context.md) | sizing the window for your server |
| [Thinking](docs/thinking.md) | `reasoning_effort` across endpoints |
| [MCP](docs/mcp.md) | servers, OAuth login, unattended use |
| [Configuration](docs/configuration.md) | flags, `.env`, system prompt, file locations |
| [Security](docs/security.md) | what is protected, and what is not |
| [Log format](docs/logs.md) | the JSONL transcript, `jq` recipes |
| [Platforms](docs/platforms.md) · [Known limits](docs/known-limits.md) · [Development](docs/development.md) | |

## Tests

Offline, against a scripted mock server — no API key, no network:

```sh
.venv/bin/python tests/run.py
```
