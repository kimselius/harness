# Running in a container

Two ways, both kept because they suit different habits:

| | `docker run` one-liner | `Dockerfile` image |
| --- | --- | --- |
| setup | none | `docker build` once |
| startup | pip install every launch | instant |
| tooling | whatever the base image has | `curl git jq ripgrep less procps` baked in |
| source edits | live, it's a mount | rebuild |
| runs as | uid 1001 | uid 1001 |
| best for | one-offs, trying it out | repeated use, servers |

## Build an image

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
[Just docker run](#just-docker-run)).

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

For the full version — every capability dropped, workspace in RAM, and an egress
allowlist the agent has no route around — see **[`examples/hardened/`](../examples/hardened/)**. The short
form:

```sh
docker run --rm --env-file .env \
  --cap-drop=ALL --security-opt no-new-privileges --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --tmpfs /work:rw,nosuid,size=512m,uid=1001,gid=1001 \
  -e HARNESS_WORKSPACE=/work -e HARNESS_LOG_DIR=/work/.logs \
  --memory 2g --cpus 2 --pids-limit 256 --ulimit fsize=268435456 \
  harness -p "do a thing"
```

The harness needs no Linux capabilities at all; that was verified by running a
full task with every one removed. Do **not** add `--init`: the image already runs
`tini` as PID 1, and Docker's init would displace it.

Blocking network access is the interesting half, and it cannot be
`--network=none` — the harness has to reach the model, and the agent shares that
namespace. `examples/hardened/compose.yml` does it properly: the container sits
on an `internal: true` network with no route out, and the only member that can
also reach the outside is a proxy holding a one-host allowlist. Measured on that
stack — the model reachable, `example.com` refused with `403 Filtered` through the
proxy and unroutable without it, and external DNS not resolving at all, so there
is no side-channel either.

## Just docker run

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
