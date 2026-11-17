# Unattended runs

`-p` is meant to be driven by something other than a person: a shell script, a
crontab, a `docker run`. Three things make that safe to rely on.

**The exit code is true.**

| code | meaning |
| --- | --- |
| 0 | the model finished and answered |
| 1 | it failed — the endpoint was unreachable, the request was rejected, or no prompt was given without a terminal |
| 3 | it ran out of tool rounds (`--max-steps`), so the task is probably half done — `text` holds a closing summary of what did get done |
| 4 | it ran past `--max-seconds` |
| 5 | the model's reply was cut off at the output token limit (`--max-tokens`, or the server's own cap), so the answer is incomplete |
| 130 | Ctrl-C |
| 143 | SIGTERM (`docker stop`, `timeout`, a supervisor shutting down) |

Only 0 means the model decided it was done. 3, 4 and 5 mean it was cut off
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

## Chaining runs

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

[`examples/report/`](../examples/report/) is a complete worked example: seven steps that turn raw notes
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

## From cron

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

## Signals and containers

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

One caveat, and it is why the image runs `tini` as PID 1 (see [Running in a container](docker.md)): the handler cannot be
installed until Python and the OpenAI SDK have loaded, and **PID 1 silently
discards signals it has no handler for**. A `docker stop` inside that startup
window was dropped entirely — ten seconds of waiting, then SIGKILL, no
`session_end`. With `tini` in front there is no window. If you override the
entrypoint, pass `--init`.
