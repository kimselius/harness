# A seven-step report pipeline

Seven agents, one after another. Each one starts with an empty context, does one
job, writes files, and exits. The next one picks up those files.

```
notes/ ──▶ 1 outline      → outline.txt, plan.md
           2 sections     → sections/<id>.md          (one agent per section)
           3 references   → sections/<id>.md, sourced (one agent per section)
           4 assemble     → report.md
           5 review       → corrections.md          (finds problems only)
           6 fix          → edits report.md         (applies them only)
           7 summary      → executive-summary.md
```

Steps 2 and 3 fan out: if the outline has six sections, that is six separate
agent runs, each holding one section's worth of context instead of the whole
report's.

## Why it is a chain and not one agent

Three reasons, in order of how much they matter:

**Context.** A 31B model with a 32k window cannot hold the notes, the plan, six
sections and the assembled report at once. Each step here holds what it needs and
nothing else.

**Re-runnability.** When step 3 produces nonsense you fix `prompts/03-references.md`
and re-run step 3 against the files that are already there. One long agent run
would have to start over.

**Deterministic control flow.** The order of the steps is known in advance, so it
belongs in a shell script, not in a model's judgement. Nothing here asks the model
to decide what happens next.

The handoff is **the workspace**, not a pipe. That is the thing to design
deliberately: the file layout *is* the contract between steps. Because each step
starts fresh, every prompt in `prompts/` has to be self-describing — it says what
it reads, what it writes, and what it must not touch.

## Three ways to run it

### 1. On a host

```sh
mkdir -p ../../workspace/report/notes
cp sample-notes/* ../../workspace/report/notes/
./run.sh
```

Each step is a `python main.py -p …` that exits when its agent finishes. `run.sh`
stops at the first failing step and tells you which one.

Re-run a single phase after editing its prompt:

```sh
./run.sh outline       # also: sections, references, assemble, review, fix, summary
```

### 2. Inside one container, all seven steps

The script and the harness are both in the image, so run the script *there*. One
container start, every agent inside it — the cheapest option, and the one to reach
for by default.

```sh
docker build -t harness ../..

docker run --rm --init \
  --env OPENAI_API_KEY --env OPENAI_BASE_URL --env HARNESS_MODEL \
  -v report-work:/work \
  -v "$PWD":/pipeline:ro \
  --entrypoint sh harness \
  -c 'HARNESS=/harness WORKSPACE=/work LOG_DIR=/work/.harness-logs /pipeline/run.sh'
```

`--entrypoint sh` is needed because the image's entrypoint is `tini -- python3
main.py`; here we want the script in charge, calling the harness once per step.
`HARNESS=/harness` points it at the harness in the image rather than next to the
mounted script, and `LOG_DIR` puts the session transcripts on the volume — leave
it out and they die with the container.

`--init` replaces the `tini` the entrypoint override just discarded. Without it
`sh` is PID 1, and a shell neither forwards SIGTERM to the step it is waiting on
nor reaps orphans: `docker stop` would wait out its ten-second grace period and
kill the container mid-step.

The volume has to be writable by uid 1001, which the image runs as:

```sh
docker run --rm --user 0:0 --entrypoint chown \
  -v report-work:/work harness -R 1001:1001 /work
```

The container exits when the pipeline finishes, with the pipeline's exit code — so
`docker run … ; echo $?` tells you whether it worked, and `docker wait <id>` does
the same for a detached run. A failing step ends the container immediately.

### 3. One container per step

```sh
docker build -t harness ../..
./docker-pipeline.sh
```

Every step is its own `docker run` against a shared named volume. `docker run`
without `-d` blocks and returns the agent's exit code, which is what lets the
script fail fast.

Reach for this when you want each step isolated — its own filesystem and memory,
nothing inherited from the step before — or when steps run on different hosts
against shared storage. It costs a container start per step (and per section in
the fan-out phases), so it is the slowest of the three.

## Reading what happened

Each step writes a result file:

```sh
cat ../../workspace/report/.pipeline/outline.json
```

```json
{
  "status": "ok",
  "exit_code": 0,
  "text": "Planned 5 sections covering …",
  "steps": 4,
  "tool_calls": 7,
  "session": "20261102-141233-9ad41c",
  "label": "outline",
  "tokens_in": 38210,
  "tokens_out": 2904
}
```

`status` is `ok` only when the model decided it was finished. `max_steps` and
`timeout` mean it was cut off mid-task, which is usually a prompt problem — the
step is doing too much.

The session id points at the full transcript:

```sh
python ../../main.py --sessions          # labels show up in the listing
```

## From cron

```cron
17 3 * * *  flock -n /tmp/report.lock /path/to/harness/examples/report/run.sh \
              >> /var/log/report.log 2>&1
```

`flock -n` keeps a slow night from overlapping with the next one. Each step
already carries `--max-seconds 900` (override with `MAX_SECONDS`), so a wedged
step fails the run instead of hanging until morning. Because `run.sh` exits
non-zero on any failing step, cron's own mail-on-failure works.

## Making it yours

- `prompts/*.md` are the system prompts. Everything about *what* the report should
  be lives here, nothing in the harness.
- Add `instructions.md` to the workspace and step 5 will check the report against
  it. Without it, step 5 falls back to generic standards.
- Change the step list by editing `run.sh`. Adding a step is one `step` call; a new
  fan-out is one `while read` loop.
- `outline.txt` is deliberately dumb — `id<TAB>title`, nothing else — so the shell
  can parse it without `jq`. `run.sh` validates it right after step 1, because a
  model that ignored the format must fail there and not three steps later.

## What a real run cost, and why step 5 got split

Measured against `gemma4:31b-it-qat`, six sections — so sixteen agent runs in the
version that had a single combined review step:

| step | tool rounds | input tokens |
| --- | --- | --- |
| outline | 9 | 34k |
| each section writer | 4–5 | 15–21k |
| each sourcer | 5–10 | 18–50k |
| assemble | 7 | 48k |
| review *(find **and** fix, as one step)* | 20 | 283k |
| summary | 5 | 37k |

Every step finished `ok` on the host. Then the same pipeline run as one container
per step hit the wall: the combined review step used **48 tool calls, 30 rounds
and 451k input tokens**, exceeded `--max-steps`, and exited 3 — so steps 6 and 7
never ran. Nothing was wrong with the harness; the step was wrong.

It is now two steps. `05-review.md` reads the report and writes `corrections.md`
in a fixed `WHERE / PROBLEM / ACTION` format and is forbidden from editing;
`06-fix.md` reads only `corrections.md` and applies the entries marked `FIX`,
then appends an `## Applied` record of what it actually did. Measured on the same
report:

| | tool rounds | input tokens | result |
| --- | --- | --- | --- |
| review + fix as one step | 30 (ceiling) | 451k | **exit 3, pipeline stopped** |
| 5 review | 12 | 140k | ok |
| 6 fix | 16 | 186k | ok |

Splitting is not free — 326k across two steps against 283k for the combined
version that *did* fit on the host. What it buys is headroom: neither step is
near the ceiling, so the run stops being a coin flip. It also means a bad review
no longer throws away the editing work — re-run `./run.sh review`, then
`./run.sh fix`.

That is the whole argument for chaining, arriving the hard way: **a step that
needs a big context is a step that should be two steps.** Watch `tool_rounds` and
`tokens_in` in the result files; when one step dwarfs the rest, split it before it
starts failing.

