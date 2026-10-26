# harness

A small agent harness for any OpenAI-compatible chat API. Four tools (`shell`,
`read_file`, `write_file`, `edit_file`), a REPL, output truncation, timeouts, and a
JSONL transcript of every request, response, tool call and tool result.

Built for throwaway VPS / container use: there is no sandboxing beyond confining
file paths to a workspace directory. Do not point it at a machine you care about.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # add your key + model
```

## Run

```sh
.venv/bin/python main.py                          # interactive
.venv/bin/python main.py -p "check disk usage"    # one shot
echo "summarize README.md" | .venv/bin/python main.py
```

Any OpenAI-compatible endpoint works:

```sh
.venv/bin/python main.py --base-url http://localhost:11434/v1 --model qwen2.5-coder
```

### In a container

Two ways, both kept because they suit different habits:

| | `docker run` one-liner | `Dockerfile` image |
| --- | --- | --- |
| setup | none | `docker build` once |
| startup | pip install every launch | instant |
| tooling | whatever the base image has | `curl git jq ripgrep less procps` baked in |
| source edits | live, it's a mount | rebuild |
| best for | one-offs, trying it out | repeated use, servers |

#### Build an image

```sh
docker build -t harness .
docker run -it --rm \
  -v "$PWD/workspace":/ws \
  -v "$PWD/logs":/logs \
  --env-file .env \
  -e HARNESS_WORKSPACE=/ws \
  -e HARNESS_LOG_DIR=/logs \
  --memory 2g --cpus 2 --pids-limit 512 \
  harness
```

Ubuntu 24.04 with `python3`, plus the tools an agent reaches for constantly. The
entrypoint is `main.py`, so a bare run opens the REPL and `harness -p "do a thing"`
is a one-shot. Nothing is mounted over the source, so the agent's edits to the
harness itself die with the container.

Without those two `-v` mounts, the workspace and transcripts live inside the
container and `--rm` throws them away. Your `PROMPT.md` is baked in at build time;
to iterate on it without rebuilding, mount it:
`-v "$PWD/PROMPT.md":/harness/PROMPT.md:ro`.

To harden further, add `--read-only --tmpfs /tmp:rw,size=256m`. Everything the
harness needs is already installed, so only `/tmp` and the two mounts stay writable
— at the cost of the agent no longer being able to `apt-get install` or `pip
install` anything mid-session.

#### Just docker run

No image to build. The source goes in read-only and only the workspace and logs are
writable, so the agent cannot edit the harness that runs it:

```sh
mkdir -p workspace logs
docker run -it --rm \
  -v "$PWD":/harness:ro \
  -v "$PWD/workspace":/ws \
  -v "$PWD/logs":/logs \
  -w /harness \
  --env-file .env \
  -e HARNESS_WORKSPACE=/ws \
  -e HARNESS_LOG_DIR=/logs \
  -e PYTHONDONTWRITEBYTECODE=1 \
  --memory 2g --cpus 2 --pids-limit 512 \
  python:3.13-slim \
  sh -c "pip install -q -r requirements.txt && python main.py"
```

The two writable mounts are host directories, so transcripts and the agent's files
survive the container. Create them first, or Docker creates them root-owned.

For a one-shot, extend the inner command — flags go to `main.py`, not to
`docker run`:

```sh
  sh -c "pip install -q -r requirements.txt && python main.py -p 'list the files'"
```

`python:3.13-slim` has bash and coreutils but no `curl`, `git` or `ripgrep`. You can
apt-install them in the inner command, but that runs on every launch — if you want
them, build the image instead.

Worth knowing:

- Read-only source is a guard rail, not a sandbox. The shell tool can still `cd` out
  of `/ws` and read anything in the container, and `pip install` still works because
  site-packages lives in the container's own filesystem. What it stops is the agent
  rewriting `agent.py` or `tools.py` underneath itself.
- Drop `:ro` if you want the agent working on the harness's own code. Then the
  `HARNESS_*` overrides are unnecessary too — `-v "$PWD":/harness` alone is enough.
- `--env-file` does not strip quotes: write `OPENAI_API_KEY=sk-...`, not
  `OPENAI_API_KEY="sk-..."`. Comments need their own line.
- A model server on the host is not reachable at `localhost` from inside the
  container; use `http://host.docker.internal:11434/v1` (Docker Desktop) or
  `--network host` on Linux. Applies to the built image too.

### REPL commands

| command | effect |
| --- | --- |
| `/help` | list commands |
| `/tools` | list tools and their arguments |
| `/model [name]` | show or switch model mid-session |
| `/prompt` | print the active system prompt |
| `/history` | message count in context |
| `/context` | token usage vs the window, plus the calibrated chars/token |
| `/compact` | summarize history now |
| `/reset` | clear conversation history |
| `/log` | path to this session's JSONL log |
| `/limits` | current output limits and timeouts |
| `/exit` | quit (Ctrl-D works too) |

Type `<<<` to start a multi-line message and `>>>` on its own line to send it.

## Interrupting

Ctrl-C stops the turn at whatever stage it is in, and never quits the session
(Ctrl-D or `/exit` does that):

- **During a shell command** the command's whole process group is killed, children
  included. Commands run in their own process group — which means the terminal's
  Ctrl-C does *not* reach them — so the harness kills them explicitly; otherwise a
  `sleep 300` or a runaway build would keep running after the turn was gone. Output
  produced before the interrupt is kept and shown.
- **Mid-stream** the partial reply is kept, the connection is closed, and any
  half-built tool call is discarded — its arguments are truncated JSON and must
  never be executed.
- **While waiting on the model** the request is dropped.

In every case the remaining tool calls of that round are answered with an error
instead of being run, so the history stays valid, and a note is appended telling the
model it was interrupted and not to resume. The next message continues the same
session. One-shot runs (`-p`) exit 130.

## Terminal output

Replies are rendered to ANSI as they stream: headings bold, `**bold**` and
`` `code` `` styled, bullets normalized to `•`, fences replaced with a dim rule
carrying the language label. Code block contents are passed through verbatim so
they stay copy-pasteable, and emphasis is never applied inside a code span — shell
globs like `**/*.py` or `ls *.py and 2 * 3` stay literal.

Rendering is line buffered, so output arrives a line at a time rather than a token
at a time; `--no-markdown` restores verbatim token-by-token streaming. Styling turns
itself off when stdout is not a tty or `NO_COLOR` is set, so piped output and the
JSONL log always hold the model's raw text.

The system prompt also tells the model its output goes to a terminal of N columns
and to skip headings and tables, which is the cheaper half of the fix: less markup
emitted beats more markup parsed.

If a reply does contain a code block of 8+ lines, the harness warns: that usually
means the model pasted a file at you instead of calling `write_file`. The event is
recorded in the log as `pasted_code_block` so you can grep for the habit.

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
| `logs/session-<ts>.jsonl` | one transcript per session (`--log-dir`) |
| `logs/.harness_history` | REPL line history (lives with the logs, so a read-only source tree still works) |
| `.env` | credentials and defaults |

## Tools

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

## Output limits

Tool results handed to the model are clamped to ~6000 chars / 250 lines, keeping the
head and tail with an explicit truncation marker. The **full, untruncated** output
always goes to the JSONL log, so nothing is lost for later follow-up. Tune with
`--max-output-chars` / `--max-output-lines`.

Each user message gets at most `--max-steps` (default 30) tool rounds; on overrun
the model is told to stop and summarize.

## Context window and compaction

Before every request the harness estimates the prompt size; past
`--compact-at` × `--context-tokens` it summarizes the older part of the history and
keeps the recent tail verbatim.

| flag | default | meaning |
| --- | --- | --- |
| `--context-tokens` | 32000 | usable window |
| `--compact-at` | 0.75 | fraction of the window that triggers compaction |
| `--compact-keep` | 0.25 | fraction kept verbatim as recent history |
| `--summary-model` | main model | model used to write the summary |
| `--no-compact` | off | disable compaction entirely |

**Set `--context-tokens` to what the server actually serves, not what the model
card claims.** With Ollama the ceiling is `num_ctx` (`OLLAMA_CONTEXT_LENGTH`, or
`PARAMETER num_ctx` in the Modelfile), which is far below the model's advertised
maximum by default. When a prompt exceeds it, Ollama silently drops the *front* of
the prompt — the system prompt and tool definitions go first, and the agent starts
behaving erratically with no error. Check with `ollama ps` on the server (the
context size is listed per loaded model).

Token counting is estimated from characters, then calibrated against the
`prompt_tokens` each response reports, so it converges on the server's real
tokenizer after a request or two. `/context` shows the current estimate and the
calibrated chars-per-token.

What survives a compaction:

1. The system prompt (always rebuilt from `PROMPT.md`).
2. A summary message covering everything dropped: the goal and instructions, work
   done, files touched, environment facts, and what is still open.
3. The most recent `--compact-keep` worth of messages, verbatim.

Split points never separate an assistant `tool_calls` message from its results, so
the history stays valid for strict APIs. If the kept tail still doesn't fit, big
tool results are shrunk in place before any message is dropped — losing the tail of
an output is cheaper than losing a whole result the model just worked for. A failed
summary call degrades to dropping the old messages with a placeholder rather than
overflowing the window.

## Log format

One JSON object per line, with `ts`, `session`, `seq`, `type`. Types:
`session_start`, `user_message`, `llm_request` (full message array sent),
`llm_response` (content, tool calls, usage), `tool_call`, `tool_result` (full
output, duration, truncation flag), `compaction` (token counts before/after plus
the summary text), `compaction_error`, `compaction_skipped`, `tool_result_shrunk`,
`history_trimmed`, `history_repaired`, `interrupted` (with `where`:
`request`/`stream`/`tool`/`turn`), `pasted_code_block`, `llm_error`,
`max_steps_reached`, `session_end`.

`llm_request`/`llm_response` carry `purpose`: `turn` for normal calls,
`compaction` for summary calls.

```sh
# what did the agent actually run?
jq -r 'select(.type=="tool_call" and .name=="shell") | .arguments.command' logs/*.jsonl

# token usage per call
jq -r 'select(.type=="llm_response") | .usage' logs/*.jsonl

# what did compaction throw away, and what did it keep?
jq -r 'select(.type=="compaction") | "\(.tokens_before) -> \(.tokens_after)\n\(.summary)"' logs/*.jsonl
```

## Tests

Offline end-to-end checks against a scripted mock OpenAI server — no API key, no
network:

```sh
.venv/bin/python tests/test_harness.py
.venv/bin/python tests/test_compaction.py
.venv/bin/python tests/test_markdown.py
.venv/bin/python tests/test_interrupt.py
```

`test_harness.py` covers the tool loop in streaming and non-streaming mode,
truncation, workspace escapes (including symlinks), shell timeouts, edit failure
modes, the `PROMPT.md` override, log completeness, interrupted-turn repair and the
REPL commands.

`test_compaction.py` covers token estimation and calibration, split-point safety
(tool-call pairing preserved in every case), transcript rendering, compaction
mid-turn under a flood of tool output, the shrink-before-drop fallback, summary
failure, and `--no-compact`.

`test_markdown.py` covers inline spans, block elements, fenced code passthrough,
globs that must not be read as emphasis, escape-code hygiene, and that streaming
in 1-char chunks renders byte-identically to rendering the whole string.

`test_interrupt.py` sends real `SIGINT`s mid-run and checks that a killed command
leaves no orphaned children (verified with `pgrep`), that unrun calls of the
interrupted round are still answered, that partial stream text survives while
half-built tool calls do not, that the session keeps working afterwards, and that
the tool-use contract survives a custom `PROMPT.md`.

## Layout

```
main.py      CLI + REPL
agent.py     tool loop, truncation, compaction, history repair
llm.py       OpenAI-compatible client (streaming + non-streaming)
tools.py     shell / read_file / write_file / edit_file + JSON schemas
context.py   token metering, split points, summary prompt
markdown.py  markdown to ANSI, streaming and whole-string
config.py    flags, env, .env loading, system prompt assembly
logbook.py   JSONL writer
util.py      truncation + path confinement
ui.py        terminal formatting
```

## Known limits

- Compaction is lossy by design. The summary is written by the same small model
  doing the work, so check `/context` and the `compaction` log events if a session
  starts forgetting things; `--summary-model` can point at a stronger model.
- Nothing stops the model from running a destructive command. That's the point of
  running it in a container.
