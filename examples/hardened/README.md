# Running the harness locked down

The harness gives an agent a shell on purpose. Nothing here changes that — the
point is to make the blast radius small and the exits few, so a bad command
wrecks a container you were going to throw away and cannot reach anything else.

Two layers, usable separately:

1. **A hardened container** — no capabilities, read-only root, workspace in RAM,
   capped memory/CPU/processes/file size. One `docker run`, no extra parts.
2. **An egress allowlist** — the container is put on a network with *no route
   out*, and the only way to the model is a proxy that permits that one host.
   This is the part that stops "curl some code and run it".

Everything below was run against this image; where something does not work, it
says so.

## 1. Hardened, single container

```sh
docker run --rm --env-file .env \
  --cap-drop=ALL \
  --security-opt no-new-privileges \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --tmpfs /work:rw,nosuid,size=512m,uid=1001,gid=1001 \
  -e HARNESS_WORKSPACE=/work -e HARNESS_LOG_DIR=/work/.logs \
  --memory 2g --cpus 2 --pids-limit 256 \
  --ulimit fsize=268435456 --ulimit nofile=1024 \
  harness -p "do a thing"
```

| setting | what it buys |
| --- | --- |
| `--cap-drop=ALL` | The harness needs no capabilities at all — verified by running a full task with every one removed. No raw sockets, no mount, no chown of other users' files, no ptrace of anything outside the container. |
| `--security-opt no-new-privileges` | A setuid binary in the image cannot raise privileges. Nothing here is setuid, but this closes the route if a package ever adds one. |
| `--read-only` | The agent cannot modify the harness that is running it, or anything else in the image. Already true for the source (owned by root, uid 1001 runs it), this extends it to everything. |
| `--tmpfs /work` | The workspace lives in RAM, capped, and is gone with the container. Swap it for `-v` when the output has to survive. |
| `--tmpfs /tmp` with `noexec` | Somewhere to write, but nothing dropped there can be executed. |
| `--pids-limit` | A fork bomb hits a wall instead of the host. |
| `--ulimit fsize` | `yes > file` stops at 256 MB instead of filling the tmpfs and OOMing. |
| `--memory`, `--cpus` | The usual. Note these are the *container's* limits; the agent can still spend all of them. |

`PYTHONDONTWRITEBYTECODE=1` is already set in the image, so a read-only root
filesystem does not trip Python trying to write `__pycache__`.

**Do not add `--init`.** The image already runs `tini` as PID 1; Docker's init
would take PID 1 from it, and tini then warns on every start that it cannot reap.

## 2. Egress allowlist

```sh
cp ../../.env .env         # the harness still needs its API key
$EDITOR allowlist          # your model host, nothing else
docker compose up --build -d proxy
docker compose run --rm harness -p "do a thing"
```

The shape:

```
            ┌─────────────── locked (internal: true) ───────────────┐
            │                                                       │
            │   harness ────────────────────▶ proxy ─────────────────┼──▶ model host
            │   no route out,                allowlist               │
            │   HTTPS_PROXY=proxy:3128       tinyproxy               │
            └───────────────────────────────────────────────────────┘
                                              └──── egress (normal bridge)
```

`internal: true` is the line that does the work. It gives the network no default
route, so the harness container has no path to anything except other containers
on that network. The proxy is the only member that also sits on a normal network,
and it refuses any host not in `allowlist`.

That split matters: the proxy is what *filters*, but the network is what
*enforces*. The agent can unset `HTTPS_PROXY` — it cannot invent a route that
does not exist.

### What was measured

Run against this stack, with the real model endpoint in the allowlist:

| attempt | result |
| --- | --- |
| harness → model, via the proxy | works, `200` |
| harness → model, proxy unset | `APIConnectionError`, exit 1 — no route |
| agent `curl https://example.com` via the proxy | `403 Filtered`, `curl: (56)` |
| agent `curl --noproxy '*' https://example.com` | no route, fails |
| resolve `example.com` or `google.com` | does not resolve |
| resolve `proxy` (a container) | resolves |
| `echo > /dev/udp/8.8.8.8/53` | blocked |

The DNS result is worth noting: on an internal network Docker's embedded resolver
answers for container names but does not forward external queries, and there is
no route to an external resolver either. So there is no low-bandwidth DNS
side-channel out, which is the usual thing people forget when they block TCP.

Every decision is logged by the proxy, allowed or refused, which makes it an
audit trail of what the agent tried to reach:

```sh
docker compose logs proxy | grep -i filtered
# NOTICE  Proxying refused on filtered domain "example.com"
```

### The allowlist

One POSIX extended regex per line, matched against the destination host:

```
^ai\.profitbyte\.cloud$
```

Anchor them. `ai.profitbyte.cloud` unanchored also matches
`ai.profitbyte.cloud.evil.example`. `ConnectPort 443` in `tinyproxy.conf` keeps
`CONNECT` from tunnelling to arbitrary ports on an allowed host.

If the agent legitimately needs a package index, add it — and understand that you
have then allowed arbitrary code in from it:

```
^(registry\.npmjs\.org|pypi\.org|files\.pythonhosted\.org)$
```

### A note on the proxy container

It runs as `nobody` (`user: "65534:65534"`) rather than dropping privileges
itself. With `cap_drop: ALL` there is no `CAP_SETGID` left, so tinyproxy's own
`User`/`Group` directives fail and it exits 77 — hence they are commented out of
`tinyproxy.conf`. If you see `Unable to change to group "nogroup"` in a loop,
that is what happened.

### MCP servers

MCP works inside this stack; it just has to be let through. Measured with a server
reachable only through the proxy:

| allowlist | result |
| --- | --- |
| MCP host listed | tools work, calls go through the proxy |
| MCP host not listed | proxy refuses (`refused on filtered domain "…"` in its log); `-p` stops before the model is called, exit 1, `MCP unavailable: …` |
| proxy unset | no route, the host does not even resolve; exit 1 |

What to add, per server:

- **HTTP servers**: the server's host, **and the host of its authorization server**
  if it uses OAuth. Discovery, registration and every token refresh go to the
  authorization server, which is often a different host from the MCP endpoint
  (`github.com` vs `api.githubcopilot.com`, say). Forget it and runs work until the
  first access token expires.
- **stdio servers** run inside this container, so they inherit its cage: read-only
  root, no capabilities, no route out except the proxy. Install them into the image
  at build time — `npx -y` at run time needs both a writable `HOME` and registry
  egress. The proxy variables are passed to them automatically; anything they need
  to write (a cache, a profile) needs its own tmpfs or volume, and `HOME` is on the
  read-only root.

The one extra mount is the MCP state directory, and it must be **read-write**,
because refreshing a token writes the new one back:

```yaml
    environment:
      MCP_ENABLED: "1"
    volumes:
      - ./mcp.json:/harness/mcp.json:ro
      - mcp-state:/harness/mcp-state  # logins
```

Mount it over `/harness/mcp-state` specifically: that directory exists in the image,
owned by uid 1001 with mode 0700, and a new named volume copies that ownership on
first use. Mounted anywhere else it starts out root-owned, and the harness cannot
save a login into it. (Declare `mcp-state:` under the top-level `volumes:` key.)

Log in once with `docker compose run --rm harness --mcp-login <server>
--no-browser` — the login URL needs the proxy too, so the authorization host has
to be on the allowlist before this works — then open the URL in any browser and
paste back the address it ends on.

The model endpoint stays the open channel described below, and every allowed MCP
server is another one: anything the agent can read, it can pass to an MCP tool.

## What this does *not* protect against

Worth being blunt, because a list of flags invites false confidence.

- **The model endpoint is an open channel.** The agent can put anything it has
  read into a prompt, and that leaves the container by design. An egress
  allowlist bounds where data can go, not whether it goes. If the workspace holds
  something that must not reach your model provider, do not put it there.
- **The agent still owns its workspace.** It can delete or corrupt everything it
  is working on. That is what sessions and the JSONL log are for.
- **Container escape is not in scope.** This is defence in depth on top of the
  kernel's isolation, not a substitute for it. The honest posture is still the one
  in the main README: run it on a host you are willing to lose.
- **Resource limits bound the container, not the work.** The agent can spend the
  whole 2 GB and both CPUs for as long as the run lasts. `--max-seconds` bounds
  the run.
- **Every MCP server you allow is another exit**, with its own credentials in the
  state directory, which the agent can read. Scope those tokens as narrowly as the
  server allows.
- **The allowlist is host-based, not content-based.** Anything reachable at an
  allowed host is reachable, including whatever a user can upload there.

## Combining with a pipeline

The hardening is orthogonal to `examples/report/`. To run that pipeline locked
down, mount it and point the script at the workspace:

```sh
docker compose run --rm \
  -v "$PWD/../report":/pipeline:ro \
  --entrypoint sh harness \
  -c 'mkdir -p /work/notes && cp /pipeline/sample-notes/* /work/notes/ &&
      HARNESS=/harness WORKSPACE=/work LOG_DIR=/work/.logs /pipeline/run.sh'
```

The seeding step is not optional with the default tmpfs workspace: it starts
empty on every run, and takes the report with it when the container exits. For
anything you want to keep, replace the tmpfs in `compose.yml` with a volume:

```yaml
    # tmpfs:
    #   - /work:rw,nosuid,size=512m,uid=1001,gid=1001
    volumes:
      - report-out:/work
```

and `chown` it once, as in `examples/report/README.md` — the container runs as
uid 1001 and a fresh volume belongs to root.
