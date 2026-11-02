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

## Unattended runs

`-p` is meant to be driven by something other than a person: a shell script, a
crontab, a `docker run`. Three things make that safe to rely on.

**The exit code is true.**

| code | meaning |
| --- | --- |
| 0 | the model finished and answered |
| 1 | it failed — the endpoint was unreachable, the request was rejected, or no prompt was given without a terminal |
| 3 | it ran out of tool rounds (`--max-steps`), so the task is probably half done — `text` holds a closing summary of what did get done |
| 4 | it ran past `--max-seconds` |
| 130 | Ctrl-C |
| 143 | SIGTERM (`docker stop`, `timeout`, a supervisor shutting down) |

Only 0 means the model decided it was done. 3 and 4 mean it was cut off
mid-task — usually a sign the step is trying to do too much.

**The streams are separate.** stdout carries the model's reply and nothing else;
every bit of harness narration — tool calls, results, warnings, token counts —
goes to stderr. So this works:

```sh
answer=$(.venv/bin/python main.py -p "what is in notes.txt?")   # just the answer
.venv/bin/python main.py -p "do the thing" > out.txt 2> progress.log
```

Colour follows each stream independently, so a redirected file never gets escape
codes while a terminal still shows coloured progress. `--quiet` drops progress
entirely and keeps warnings and errors.

**There is a machine-readable result.** `--result-file` writes one JSON object
when the run ends, including when it fails, so a driver never parses stdout:

```sh
.venv/bin/python main.py -p "plan the work" \
    --label planning --result-file /tmp/step1.json
```

```json
{
  "status": "ok", "exit_code": 0,
  "text": "Planned 5 sections covering …",
  "steps": 4, "tool_calls": 7,
  "session": "20261102-141233-9ad41c", "label": "planning", "error": null,
  "tokens_in": 38210, "tokens_out": 2904, "tokens_thinking": 0, "requests": 4
}
```

`status` is one of `ok`, `error`, `max_steps`, `timeout`, `interrupted`. The file
is written atomically and scrubbed like the session log, since it is another
artifact that leaves the container. `--label` tags the run so it is findable in
`--sessions` when a pipeline produces a dozen sessions a night.

### Chaining runs

Several agents in sequence, each starting with an empty context and handing over
through files in the workspace:

```sh
set -e   # stop at the first failing step
H=".venv/bin/python main.py --workspace work --quiet"
$H --prompt-file prompts/plan.md    --label plan  -p "Read notes/ and write outline.txt"
$H --prompt-file prompts/write.md   --label write -p "Write each section in outline.txt"
$H --prompt-file prompts/review.md  --label check -p "Check the sections against the outline"
```

Each step is a fresh process, so there is nothing to clear between them, and any
step can be re-run on its own against the files already there. The workspace is
the contract — design the file layout deliberately, and make every prompt
self-describing, because a step cannot see the conversation that produced its
input.

`examples/report/` is a complete worked example: seven steps that turn raw notes
into a report, including two fan-out phases that run one agent per section. It
ships with `run.sh` (host or inside a container) and `docker-pipeline.sh` (one
container per step), and its README records what a real run against a 31B model
cost — including the step that had to be split in two after it blew through
`--max-steps`.

If a step genuinely needs the previous step's *reasoning* rather than its files,
share the session instead of the workspace:

```sh
.venv/bin/python main.py -p "analyse the logs" --result-file /tmp/a.json
id=$(jq -r .session /tmp/a.json)
.venv/bin/python main.py --resume "$id" -p "now write the fix"
```

### From cron

```cron
17 3 * * *  flock -n /tmp/harness.lock /srv/harness/examples/report/run.sh \
              >> /var/log/harness.log 2>&1
```

`flock -n` stops a slow night from overlapping with the next one. Prefer
`--max-seconds` over wrapping the call in `timeout`: the harness stops itself at
a tool-round boundary, writes `session_end` and returns 4, where an external kill
leaves the log short. Because the exit code is honest, cron's own
mail-on-failure works.

Note that cron has no terminal. Without `-p` and without anything on stdin the
harness now exits 1 with an explanation, rather than printing a banner and
reporting success — which is what a misconfigured job used to do, silently, every
night.

### Signals and containers

`docker stop` sends SIGTERM and waits ten seconds before SIGKILL. The harness
handles SIGTERM as an interrupt: it kills any running command *and its children*,
records `session_end`, writes the result file, and exits 143 straight away.

Two different things are being protected here. Being SIGKILLed instead costs you
the `session_end` record, the result file and ten seconds — that is the container
case, where tearing down the namespace does at least kill the agent's processes
for you. Outside a container there is more at stake: shell commands run in their
own process group on purpose, so a killed harness leaves whatever it started
running. That is why the handler kills the group rather than just exiting, and
`tests/test_oneshot.py` checks with `pgrep` that nothing survives.

```sh
docker run --rm --env-file .env harness -p "run the test suite"; echo $?
docker run -d --env-file .env harness -p "long job"   # then: docker wait <id>
```

Both report the agent's exit code, so the container's status *is* the run's
status. Verified against the image: a `docker stop` mid-command exits in under a
second with 143, the `✗ ERROR: interrupted by user` tool result is recorded, and
no child survives.

One caveat, and it is why the image runs `tini` as PID 1: the handler cannot be
installed until Python and the OpenAI SDK have loaded, and **PID 1 silently
discards signals it has no handler for**. A `docker stop` inside that startup
window was dropped entirely — ten seconds of waiting, then SIGKILL, no
`session_end`. With `tini` in front there is no window. If you override the
entrypoint, pass `--init`.

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

**PID 1 is `tini`, not the harness**, which matters more than it sounds for an
agent that spawns processes. Two things were measured in this image before it was
added. A `docker stop` in the first fraction of a second was *silently discarded*,
because PID 1 drops any signal it has no handler for and the harness does not
install its SIGTERM handler until the interpreter and the OpenAI SDK have loaded —
the container then sat out the full ten-second grace period and was SIGKILLed,
losing its `session_end` record. And every backgrounded command that outlived its
shell left a permanent `[sleep] <defunct>` zombie, since the harness never calls
`wait()` on processes it did not start. `tini` costs 250 KB and removes both.

If you override the entrypoint, you take that on yourself — add `--init` (see
below).

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
  --init \
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

`--init` is not optional here. The outer `sh -c` would otherwise be PID 1, and a
shell neither forwards SIGTERM to the `python` it is waiting on nor reaps orphans —
so `docker stop` would wait out its grace period and kill the container every
time.

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
| `/history [n\|all]` | replay the conversation (default: last 12 messages) |
| `/cwd [dir]` | show or set the shell's working directory |
| `/context` | token usage vs the window, the calibrated chars/token, and session totals |
| `/compact` | summarize history now |
| `/reset` | clear conversation history |
| `/log` | path to this session's JSONL log |
| `/sessions` | list recorded sessions |
| `/resume <id>` | load an earlier session (id, prefix, or `last`) |
| `/limits` | current output limits, timeouts and reasoning settings |
| `/think [level]` | show or set `reasoning_effort` (`high`, `low`, `off`, …) |
| `/exit` | quit (Ctrl-D works too) |

Tab completes command names (`/his` → `/history`) and their arguments: levels for
`/think`, session ids for `/resume`, workspace directories for `/cwd`. Plain prose
is never completed, so Tab stays out of the way when you are writing a message.
`/help` is generated from the same command list the completer uses, so the two
cannot drift apart. (Binding Tab differs between GNU readline and libedit; both are
handled.)

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

## Security

The agent runs arbitrary shell commands because that is the point. **The container
is the security boundary, not the harness.** Nothing here tries to stop the agent
from doing damage inside its container; what it does is avoid spreading your
credentials into the artifacts that leave it.

### What the harness does

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

### What it does not protect, honestly

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
- **No command approval, by design.** Nothing prompts before `rm -rf`. A blocklist
  would give a false sense of safety; a throwaway container is the real answer.

### Practical checklist

```sh
chmod 600 .env                  # or use --api-key-file
docker run --read-only --memory 2g --cpus 2 --pids-limit 512 ...   # cap the blast radius
```

Use a scoped key per container, rotate it after the container is gone, keep the
workspace free of anything you would not paste into a chat, and read `logs/*.jsonl`
before sharing one — redaction catches known shapes, not everything.

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
| `--compact-keep` | 0.25 | fraction kept verbatim as recent history (must be below `--compact-at`) |
| `--summary-model` | main model | model used to write the summary |
| `--no-compact` | off | disable compaction entirely |

**Set `--context-tokens` to what the server actually serves, not what the model
card claims.** With Ollama the ceiling is `num_ctx` (`OLLAMA_CONTEXT_LENGTH`, or
`PARAMETER num_ctx` in the Modelfile), which is far below the model's advertised
maximum by default. The 32000 default is deliberately conservative.

Find your real ceiling by asking for too much. A well-behaved endpoint names it:

```
prompt is 173419 tokens, which does not fit the 131072 token context
of model "gemma4:31b-it-qat"
```

That is the good case — a loud 400 rather than silent damage. Some Ollama
configurations instead drop the *front* of an oversize prompt, taking the system
prompt and tool definitions with it, and the agent then misbehaves with no error
at all. Worth knowing which yours does before raising the number: send a
deliberately huge prompt once and see whether you get an error or a confused
reply. `ollama ps` on the server also lists the context size per loaded model,
when you have access to it.

Then leave headroom. The reply shares the same window as the prompt, and the
harness's own token count is an estimate. Against a 131072 ceiling, 96000 is
comfortable — compaction triggers at 72k with ~35k spare.

**Raising it is a trade.** Fewer compactions, but a slower first token on *every*
request, because the server prefills the whole prompt before emitting anything.
On a large local model a 70k-token prompt is a visible wait. If a session reads a
lot of files and compacts constantly, a bigger window is the right fix; if it
feels sluggish from the first message, the window is too big for the hardware.

Token counting is estimated from characters, then calibrated against the
`prompt_tokens` each response reports, so it converges on the server's real
tokenizer after a request or two. `/context` shows the current estimate, the
calibrated chars-per-token, and which model writes the summaries.

What survives a compaction:

1. The system prompt (always rebuilt from `PROMPT.md`).
2. A summary message covering everything dropped: the goal and instructions, work
   done, files touched, environment facts, and what is still open.
3. The most recent `--compact-keep` worth of messages, verbatim.

Split points never separate an assistant `tool_calls` message from its results, so
the history stays valid for strict APIs. If the kept tail still doesn't fit, the
biggest message is shrunk in place before anything is dropped, tool results first,
since losing the tail of a command's output is cheaper than losing a whole result
the model just worked for. A single pasted message larger than the window is shrunk
too, because the last message can never be dropped. A failed
summary call degrades to dropping the old messages with a placeholder rather than
overflowing the window.

**Summaries use your main model.** `--summary-model` is opt-in only; point it at
something cheaper and faster if compaction is the slow part of your sessions.
`/context` tells you which one is in use.

Compaction is the longest wait in the harness — it sends the whole dropped
transcript and waits out the server's prefill before a single token comes back —
so it reports as it goes:

```
⟳ compacting context (48206 tokens, 42 of 69 messages; keeping the newest 27)
⟳ summarizing ~21400 tokens with gemma4:31b-it-qat · 47s 1203 chars written
⟳ context compacted: 48206 → 14870 tokens (42 messages summarized, 27 kept)
```

The middle line rewrites in place on a terminal and is absent elsewhere; the
seconds tick up during prefill, and the character count starts moving once the
summary begins streaming. Ctrl-C works throughout. Every compaction also logs a
`compaction_summary_done` event with its duration, input size and model, so you
can see what it costs over a long session:

```sh
jq -r 'select(.type=="compaction_summary_done")
       | "\(.seconds)s  \(.sent_tokens) tokens in  \(.model)"' logs/<id>.jsonl
```

If a compaction finishes still above the trigger it says so explicitly — that
means the next turn will compact again immediately, and the window is too small
for what the session is holding.

## Log format

One JSON object per line, with `ts`, `session`, `seq`, `type`. Types:
`session_start`, `session_resumed`, `session_restored`, `user_message`, `llm_request` (full message array sent),
`llm_response` (content, tool calls, usage), `tool_call`, `tool_result` (full
output, duration, truncation flag), `compaction` (token counts before/after plus
the summary text), `compaction_error`, `compaction_skipped`, `message_shrunk`,
`history_trimmed`, `history_repaired`, `cwd_set`, `user_message` with
`queued: true` for steering, `interrupted` (with `where`:
`request`/`stream`/`tool`/`turn`), `pasted_code_block`, `llm_error`,
`max_steps_reached`, `params_dropped`, `reasoning_effort_changed`, and
`session_end` (which carries the session's token totals). Every field is scrubbed
of known secrets on the way in.

`llm_request`/`llm_response` carry `purpose`: `turn` for normal calls,
`compaction` for summary calls.

```sh
# what did the agent actually run?
jq -r 'select(.type=="tool_call" and .name=="shell") | .arguments.command' logs/*.jsonl

# what did a whole session cost?
jq -r 'select(.type=="session_end") | "\(.tokens_in) in, \(.tokens_out) out, \(.tokens_thinking) thinking"' logs/*.jsonl

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
.venv/bin/python tests/run.py              # every suite, one command
.venv/bin/python tests/run.py cwd steer    # just these
```

A suite that records no checks is reported as a failure, not a pass — that catches
a suite wired up wrongly, which is otherwise silent. Lint with
`.venv/bin/pip install ruff && .venv/bin/ruff check .`; the rules live in
`pyproject.toml`.

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

`test_security.py` covers redaction (literal keys, every supported secret shape,
and the innocent strings that must survive), that the key is absent from
`Config`'s repr, that log files land at `0600` in a `0700` directory, that a whole
turn reading a secret-bearing file leaks it to neither the model, the screen nor the
log, `--api-key-file`, and the stripped child environment.

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

`test_oneshot.py` covers everything a script depends on, through real
subprocesses: every exit code (including an unreachable endpoint, which used to
exit 0), that no prompt without a terminal fails instead of succeeding silently,
the stdout/stderr split and the absence of escape codes when redirected,
`--quiet`, the result file's shape and redaction on both success and failure,
`--max-seconds`, `--label`, and a SIGTERM mid-command that must exit 143 promptly,
write its result file, and leave no orphaned child.

## Layout

```
main.py      CLI + REPL, exit codes, result file, signal handling
agent.py     tool loop, truncation, compaction, history repair, Outcome
llm.py       OpenAI-compatible client (streaming + non-streaming)
tools.py     shell / read_file / write_file / edit_file + JSON schemas
session.py   per-run state: working directory and token totals
context.py   token metering, split points, summary prompt, harness notes
sessions.py  session discovery, listing, replay from the log
steer.py     queued stdin while a turn is running
markdown.py  markdown to ANSI, streaming and whole-string
config.py    flags, env, .env loading, system prompt assembly
logbook.py   JSONL writer
util.py      truncation + path confinement + redaction
ui.py        terminal formatting, stdout/stderr split

examples/report/   seven-step pipeline: notes in, report out
```

## Known limits

- Compaction is lossy by design. The summary is written by the same small model
  doing the work, so check `/context` and the `compaction` log events if a session
  starts forgetting things; `--summary-model` can point at a stronger model.
- Nothing stops the model from running a destructive command. That's the point of
  running it in a container.
- A long-running command prints nothing until it finishes. The system prompt tells
  the model to redirect long jobs to a file and poll, but live tool-output
  streaming is not implemented.
- `--max-seconds` is checked between tool rounds, not inside one. A command already
  running is bounded by `--shell-timeout` instead, so the worst case is one
  command's timeout past the deadline.
- There is no pipeline runner inside the harness. Chaining is a shell script
  (`examples/report/run.sh`), which also means no way to re-run "step 4 onwards"
  except by what the script itself offers.
