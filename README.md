# harness

A small agent harness for any OpenAI-compatible chat API. Four tools (`shell`,
`read_file`, `write_file`, `edit_file`), a REPL, output truncation, timeouts, and a
JSONL transcript of every request, response, tool call and tool result.

Built for throwaway VPS / container use: there is no sandboxing beyond confining
file paths to a workspace directory. Do not point it at a machine you care about.

## Platforms

| | Linux | macOS | Windows (native) |
| --- | --- | --- | --- |
| REPL, sessions, compaction, logging | yes | yes | yes |
| Prompt wrapping / line editing | GNU readline | libedit | plain console, or pyreadline3 |
| Colour | yes | yes | VT enabled at startup, else colour off |
| Paste kept as one message | yes | yes | no, one line per message |
| Typing while it works | yes | yes | no, wait for the prompt |
| `shell` tool | `/bin/sh -c` | `/bin/sh -c` | `cmd.exe`, POSIX syntax will not work |
| Persistent shell directory | yes | yes | no, stays at the workspace root |
| Timeout / Ctrl-C kills the whole process tree | yes | yes | direct child only |

Everything platform-specific degrades rather than crashing, but **on Windows use
WSL or Docker**. Two deliberate non-goals keep the code simple: there is one shell
implementation (POSIX `sh`) and one way of watching stdin (`select`). Supporting
`cmd.exe` would mean a second command dialect, a second working-directory probe and
job objects instead of process groups — doubling the surface of the riskiest tool to
solve something WSL already solves.

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
| runs as | uid 1001 | uid 1001 |
| best for | one-offs, trying it out | repeated use, servers |

#### Build an image

```sh
docker build -t harness .
mkdir -p workspace logs
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
is a one-shot.

It runs as **uid 1001**, not root (1001 because Ubuntu 24.04 already gives its own
`ubuntu` user 1000). The source files stay owned by root and world-readable, so the
agent cannot modify the harness that is running it even inside the container; only
`/ws` and `/logs` are writable.

**Bind mounts and uid.** The mounted directories must be writable by uid 1001.
Docker Desktop on macOS and Windows maps ownership for you, so it just works. On
Linux the host uid is passed through, so do one of:

```sh
sudo chown -R 1001:1001 workspace logs      # give the container's uid access
docker run --user "$(id -u):$(id -g)" ...   # or run as yourself instead
```

Without those two `-v` mounts, the workspace and transcripts live inside the
container and `--rm` throws them away. Your `PROMPT.md` is baked in at build time;
to iterate on it without rebuilding, mount it:
`-v "$PWD/PROMPT.md":/harness/PROMPT.md:ro`.

To harden further, add `--read-only --tmpfs /tmp:rw,size=256m`. Everything the
harness needs is already installed, so only `/tmp` and the two mounts stay writable.
Note that non-root already means the agent cannot `apt-get install` anything
mid-session; `--read-only` additionally stops `pip install --user`. If you want the
agent to be able to install things, build them into the image instead.

#### Just docker run

No image to build. The source goes in read-only, only the workspace and logs are
writable, and it runs as uid 1001:

```sh
mkdir -p workspace logs
docker run -it --rm \
  --user 1001:1001 \
  -v "$PWD":/harness:ro \
  -v "$PWD/workspace":/ws \
  -v "$PWD/logs":/logs \
  -w /harness \
  --env-file .env \
  -e HARNESS_WORKSPACE=/ws \
  -e HARNESS_LOG_DIR=/logs \
  -e HOME=/tmp \
  -e PYTHONPATH=/tmp/deps \
  -e PYTHONDONTWRITEBYTECODE=1 \
  --memory 2g --cpus 2 --pids-limit 512 \
  python:3.13-slim \
  sh -c "pip install -q --target /tmp/deps -r requirements.txt && python main.py"
```

The `--target /tmp/deps` plus `PYTHONPATH` is what makes this work as non-root: uid
1001 cannot write to the image's `site-packages`, but `/tmp` is writable, and
`HOME=/tmp` keeps anything looking for a home directory happy. The same uid caveat
as above applies to the two writable mounts.

For a one-shot, extend the inner command — flags go to `main.py`, not to
`docker run`:

```sh
  sh -c "pip install -q --target /tmp/deps -r requirements.txt && python main.py -p 'list the files'"
```

`python:3.13-slim` has bash and coreutils but no `curl`, `git` or `ripgrep`, and as
non-root you cannot apt-install them at startup. If you want them, build the image.

Worth knowing:

- Non-root plus a read-only source is a guard rail, not a sandbox. The shell tool
  can still `cd` out of `/ws` and read anything in the container that is
  world-readable. What it stops is the agent rewriting `agent.py` or `tools.py`
  underneath itself, and touching anything owned by root.
- Drop `:ro` and the `--user` flag if you want the agent working on the harness's
  own code. Then the `HARNESS_*` overrides are unnecessary too — `-v "$PWD":/harness`
  alone is enough.
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
| `/cwd [dir]` | show or set the shell's working directory |
| `/context` | token usage vs the window, plus the calibrated chars/token |
| `/compact` | summarize history now |
| `/reset` | clear conversation history |
| `/log` | path to this session's JSONL log |
| `/sessions` | list recorded sessions |
| `/resume <id>` | load an earlier session (id, prefix, or `last`) |
| `/limits` | current output limits, timeouts and reasoning settings |
| `/think [level]` | show or set `reasoning_effort` (`high`, `low`, `off`, …) |
| `/exit` | quit (Ctrl-D works too) |

Long lines wrap and edit correctly: colour codes in the prompt are marked
zero-width (`\001`/`\002`), because GNU readline and libedit otherwise count those
bytes as columns, wrap early, and then redraw over the start of your line. The
markers are only used with a line editor known to understand them — CPython's own
`readline`, whether built against GNU readline (Linux) or libedit (macOS). An
unrecognised editor such as pyreadline3 gets the prompt untouched, since it would
print the markers as literal control characters.

Paste freely: a multi-line paste is kept together as one message rather than being
chopped into one message per line. `input()` returns at the first newline, so the
harness drains whatever the terminal still has buffered and reassembles it, which
also normalizes CRLF and strips bracketed-paste markers. A paste that ends mid-line
waits for you to press Enter. To type a multi-line message by hand, start with `<<<`
and end with `>>>` on its own line.

## Sessions

Every run is a session with an id like `20261026-105057-b1c53e`, and its log is
`logs/<id>.jsonl` — the filename *is* the id.

```sh
.venv/bin/python main.py --sessions            # list them
.venv/bin/python main.py --resume              # continue the most recent
.venv/bin/python main.py --resume 20261026-105057-b1c53e
.venv/bin/python main.py --resume 20261026-10  # unambiguous prefix is enough
```

Resuming replays the conversation from the log and appends new events to that same
file, so one session stays one file however many times you come back to it.
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

What is *not* restored: the shell's working directory (back to the workspace root)
and anything the earlier process had in memory. Files in `workspace/` are untouched,
since they live on disk.

## Steering while it works

You do not have to wait for a turn to finish, and you do not have to interrupt it
to say something. Type while the agent is working and the message is queued:

```
> refactor the parser
⏺ shell  pytest -q
you know what, start with the tokenizer       ← typed while pytest runs
» queued, will be sent next step
  ⎿ exit code: 1 · cwd: .
+ your message was added to the context: you know what, start with the tokenizer
⏺ read_file  tokenizer.py
```

A reader thread watches stdin for the duration of the turn and collects whole
lines. The agent loop drains the queue at the start of each step, right before the
next request goes out — never between a tool call and its result, so the history
stays valid. The model therefore sees your message alongside the latest tool
result and can change course mid-task.

Typed during the model's final step, when there is no next step to pick it up? Then
it runs as the next turn automatically. Typed without pressing Enter? It is handed
back as the start of your next prompt rather than dropped. Lines beginning with `/`
are queued as plain messages, not run as commands — commands need the prompt.

Interrupting and steering are different tools: Ctrl-C stops the work, typing
redirects it.

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

### Two path rules, and the state block

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

## Sessions## Sessions

Every run is a session with an id like `20261026-105057-b1c53e`, and its log is
`logs/<id>.jsonl` — the filename *is* the id.

```sh
.venv/bin/python main.py --sessions            # list them
.venv/bin/python main.py --resume              # continue the most recent
.venv/bin/python main.py --resume 20261026-105057-b1c53e
.venv/bin/python main.py --resume 20261026-10  # unambiguous prefix is enough
```

Resuming replays the conversation from the log and appends new events to that same
file, so one session stays one file however many times you come back to it.
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

What is *not* restored: the shell's working directory (back to the workspace root)
and anything the earlier process had in memory. Files in `workspace/` are untouched,
since they live on disk.

## Steering while it works

You do not have to wait for a turn to finish, and you do not have to interrupt it
to say something. Type while the agent is working and the message is queued:

```
> refactor the parser
⏺ shell  pytest -q
you know what, start with the tokenizer       ← typed while pytest runs
» queued, will be sent next step
  ⎿ exit code: 1 · cwd: .
+ your message was added to the context: you know what, start with the tokenizer
⏺ read_file  tokenizer.py
```

A reader thread watches stdin for the duration of the turn and collects whole
lines. The agent loop drains the queue at the start of each step, right before the
next request goes out — never between a tool call and its result, so the history
stays valid. The model therefore sees your message alongside the latest tool
result and can change course mid-task.

Typed during the model's final step, when there is no next step to pick it up? Then
it runs as the next turn automatically. Typed without pressing Enter? It is handed
back as the start of your next prompt rather than dropped. Lines beginning with `/`
are queued as plain messages, not run as commands — commands need the prompt.

Interrupting and steering are different tools: Ctrl-C stops the work, typing
redirects it.

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

### The working directory persists

The shell behaves like one session rather than a series of unrelated commands: `cd
sub` in one call applies to every later call, and `read_file`/`write_file`/
`edit_file` resolve relative paths against the same directory. So working on a
subproject is what you would expect —

```
cd repo                    → cwd: repo
git diff --stat            → runs in repo, no path prefix needed
read_file f.txt            → reads repo/f.txt
```

— instead of prefixing every command with the path or wrapping everything in
`cd x && …`. Each shell result reports where it is (`exit code: 0 · cwd: repo`), and
the directory is read back from the real `pwd` after the command, so `cd` inside a
compound command sticks too.

It cannot leave the workspace: a command that ends up outside resets to the root and
says so. If the directory is deleted underneath it, the next command falls back to
the root rather than failing. `/cwd` shows it and `/cwd sub` sets it by hand.

## Output limits

Tool results handed to the model are clamped to ~6000 chars / 250 lines, keeping the
head and tail with an explicit truncation marker. The **full, untruncated** output
always goes to the JSONL log, so nothing is lost for later follow-up. Tune with
`--max-output-chars` / `--max-output-lines`.

Each user message gets at most `--max-steps` (default 30) tool rounds; on overrun
the model is told to stop and summarize.

## Thinking

Reasoning is **on by default** at `medium`, settable three ways:

```sh
main.py --reasoning-effort high        # minimal | low | medium | high | off
main.py --show-thinking                # print the reasoning as it streams
```
```sh
HARNESS_REASONING_EFFORT=low           # in .env
```
```
/think high                            # mid-session; /think alone shows it
/think off                             # back to the model's own default
```

`off` (also `none`, `false`, empty) sends no `reasoning_effort` at all, which leaves
the model at whatever it does by default — on the Ollama endpoint that means no
thinking.

The reasoning text is **always written to the session log**, whether or not you
display it — `llm_response` carries `reasoning`, `reasoning_chars` and
`reasoning_tokens`. Without `--show-thinking` the terminal just notes the cost
(`thought for 114 tokens`), and the turn footer shows `(thinking 114)`.

It is never sent back to the model. OpenAI-compatible APIs do not accept reasoning
in an assistant message, and replaying it would burn context for no benefit, so it
stays in the log and on your screen only.

Efficiency choices worth knowing:

- Compaction summaries never request thinking, whatever the session setting is.
  Rewriting a transcript is mechanical, so reasoning tokens there are waste.
- `reasoning_tokens` are counted as output tokens by the server. At `high` on a
  31B, a trivial question can spend a few hundred tokens before answering.

### Endpoint differences

Only the standard `reasoning_effort` parameter is sent, so this works across
endpoints rather than for one vendor. Responses are read tolerantly: reasoning text
is taken from `reasoning_content` (Ollama, vLLM, DeepSeek), `reasoning`
(OpenRouter) or `thinking`, and token counts from either
`completion_tokens_details.reasoning_tokens` (OpenAI) or a flat `reasoning_tokens`.

If an endpoint rejects `reasoning_effort` with a 400, the harness drops the field,
retries once, warns you, logs `params_dropped`, and stops sending it for the rest of
the session — a wrong guess costs one retry, not the turn.

For knobs that are not in the OpenAI schema at all, `--extra-body '{"top_k": 40}'`
(or `HARNESS_EXTRA_BODY`) is merged into the request body and degrades the same way.
Note that Ollama's native `think` parameter is *not* honoured through its
OpenAI-compatible endpoint — `reasoning_effort` is the one that works there.

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
`session_start`, `session_resumed`, `session_restored`, `user_message`, `llm_request` (full message array sent),
`llm_response` (content, tool calls, usage), `tool_call`, `tool_result` (full
output, duration, truncation flag), `compaction` (token counts before/after plus
the summary text), `compaction_error`, `compaction_skipped`, `tool_result_shrunk`,
`history_trimmed`, `history_repaired`, `cwd_set`, `user_message` with
`queued: true` for steering, `interrupted` (with `where`:
`request`/`stream`/`tool`/`turn`), `pasted_code_block`, `llm_error`,
`max_steps_reached`, `params_dropped`, `reasoning_effort_changed`, `session_end`.

`llm_request`/`llm_response` carry `purpose`: `turn` for normal calls,
`compaction` for summary calls.

```sh
# what did the agent actually run?
jq -r 'select(.type=="tool_call" and .name=="shell") | .arguments.command' logs/*.jsonl

# token usage per call
jq -r 'select(.type=="llm_response") | .usage' logs/*.jsonl

# what did it think about, and what did that cost?
jq -r 'select(.type=="llm_response" and .reasoning != "") | "\(.reasoning_tokens) tokens: \(.reasoning)"' logs/*.jsonl

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
.venv/bin/python tests/test_cwd.py
.venv/bin/python tests/test_paste.py
.venv/bin/python tests/test_sessions.py
.venv/bin/python tests/test_steer.py
.venv/bin/python tests/test_reasoning.py
.venv/bin/python tests/test_platform.py
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

`test_cwd.py` covers directory persistence across calls, `cd` inside compound
commands, clamping on escape, a deleted working directory, the file tools following
the cwd, the invisible `pwd` probe (including commands with no trailing newline),
and a real `git init` / `git diff` in a child directory.

`test_sessions.py` covers id generation, one-file-per-session, reference resolution
(`last`, exact, prefix, path, ambiguous, missing), replay fidelity against a live
history, re-truncation on load, compaction replay, a killed session with a damaged
line and an unanswered tool call, and `--resume`/`--sessions` as subprocesses.

`test_platform.py` exercises the Windows branches on POSIX by faking what they
depend on: `os.killpg` and `signal.SIGKILL` removed, a stubbed `msvcrt`, `select()`
refusing a non-socket, and the cwd probe disabled.

`test_reasoning.py` covers both usage shapes and all three reasoning field names,
capture in streaming and non-streaming mode, that the text is logged but never
replayed into history, `--show-thinking`, that summaries omit the parameter, and a
server that rejects `reasoning_effort` with a 400.

`test_steer.py` covers the queue mechanics (line splitting, blank lines, CRLF,
leftovers, stop semantics) and then types into a live REPL while a `sleep 3` tool
call is running, asserting the message is absent from the in-flight request and
present in the next one.

`test_paste.py` drives the actual REPL through a pty and checks that a three-line
paste arrives as one user message, that single lines still produce separate turns,
and that slash commands are unaffected.

## Layout

```
main.py      CLI + REPL
agent.py     tool loop, truncation, compaction, history repair
llm.py       OpenAI-compatible client (streaming + non-streaming)
tools.py     shell / read_file / write_file / edit_file + JSON schemas
context.py   token metering, split points, summary prompt, harness notes
sessions.py  session discovery, listing, replay from the log
steer.py     queued stdin while a turn is running
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
