# MCP

The harness is an MCP client. It connects to the servers listed in `mcp.json`,
gives the model their tools, resources and prompts, and handles the protocol's
client side: OAuth login, sampling, elicitation, roots, progress and
cancellation. It is built on the official Python SDK (`mcp` 2.x), which speaks
both the 2026-07-28 protocol and the handshake-era revisions before it.

**It is off unless you turn it on.** Without `MCP_ENABLED=1` in `.env` (or
`--mcp`), nothing is connected and the SDK is not even imported: the requests
the model sees are byte-for-byte what they were before MCP existed. That is the
switch for small local models, which do badly with dozens of extra tools.
`--no-mcp` turns it off for one run.

## Configuring servers

`mcp.json`, next to the harness (or `--mcp-config` / `HARNESS_MCP_CONFIG`), in the
same format as Claude Code's `.mcp.json`, so a server's published setup snippet
pastes straight in. `mcp.json.example` has one of each kind:

```json
{
  "mcpServers": {
    "linear":   {"type": "http", "url": "https://mcp.linear.app/mcp"},
    "internal": {"type": "http", "url": "https://mcp.example.com/mcp",
                 "headers": {"Authorization": "Bearer ${INTERNAL_MCP_TOKEN}"}},
    "files":    {"command": "npx",
                 "args": ["-y", "@modelcontextprotocol/server-filesystem", "/srv/data"]}
  }
}
```

`${VAR}` and `${VAR:-default}` are filled from the environment, so secrets live in
`.env` and `mcp.json` itself can be committed. An unset variable with no default is
an error naming the server and key, not an empty string. Every expanded value, and
every header value, is added to the redactor and never reaches a log.

Per-server keys the harness adds (other clients ignore them):

| key | meaning |
| --- | --- |
| `enabled` | `false` keeps it in the file but off |
| `optional` | `true` lets a `-p` run go ahead without it (see below) |
| `tools` / `disabledTools` | allowlist / denylist of tool names to expose |
| `timeout` | seconds for one tool call (default `--mcp-timeout`, 120) |
| `sampling` | `ask` (default), `allow` or `deny` — see below |
| `oauth` | `false`, or the auth settings described below |

Tools appear to the model as `mcp__<server>__<tool>`, so no server can shadow a
built-in. While servers are connected the system prompt names them, says plainly
that their tools act outside the workspace, repeats that what they return is data
rather than instructions, and includes each server's own `instructions` (capped at
2000 characters).

## Logging in

A remote server authenticates one of three ways, decided by `mcp.json`:

- **A static header** — `"headers": {"Authorization": "Bearer ${TOKEN}"}`. Nothing
  else to do, nothing stored.
- **A machine grant** — `"oauth": {"grant": "client_credentials", "clientId": …,
  "clientSecret": …, "issuer": …}` (or `"private_key_jwt"` with `privateKeyFile`).
  Fully headless: the harness fetches a token itself, caches it, and fetches a new
  one when it expires. No login step.
- **OAuth login** — the default for an HTTP server with no `Authorization` header.
  Log in once:

```sh
python3 main.py --mcp-login linear
```

That runs the full flow: discovery, dynamic client registration (or a client you
pre-registered via `oauth.clientId`), PKCE, and the RFC 8707 resource binding. It
prints the authorization URL, tries to open a browser, and waits for the redirect
two ways at once: a listener on `localhost:33418` (`oauth.callbackPort` to change
it), and **you pasting the address the browser ended on**. The paste is what makes
login work in a container or over SSH: the browser cannot reach the listener, so
it ends on a page that fails to load — copy that page's address and paste it.
`--no-browser` just prints the URL.

Tokens go to `mcp-state/<server>.json` (`0600`, directory `0700`; change with
`--mcp-state-dir` / `HARNESS_MCP_STATE_DIR`). Every later run, `-p` included, uses
them and **refreshes them when they expire**, writing the rotated tokens back. The
file also remembers when the token expires and where the token endpoint is, which
the SDK alone forgets between processes: without that, a fresh process sends an
expired token, gets a 401, and asks for a new login instead of refreshing.

Stored tokens are bound to the server URL: point the same name at another URL and
they are not sent there. `--mcp-logout <server>` forgets a login; `--mcp-list`
connects to everything and shows the auth state, token expiry and what each server
offers. These three work whether or not `MCP_ENABLED` is set.

## Unattended runs

A run never starts an interactive login. In `-p` mode, if any enabled server
cannot connect — not logged in, refresh refused, unreachable, broken `mcp.json` —
the run stops **before the model is called**, exit 1, with the reason on stderr and
in `--result-file`:

```
! MCP unavailable: linear: not logged in — run: harness --mcp-login linear
```

A pipeline step written around a server would otherwise run without it and quietly
do something else. Mark a server `"optional": true` when the run should go ahead
anyway. In the REPL a failed server is a warning, and `/mcp login <server>` fixes
it in place.

A server prompt can be the `-p` message itself, which lets a pipeline step use a
prompt a server maintains:

```sh
python3 main.py -p "/mcp__github__review_pr 1432"
```

## When a server needs you

Some servers call back into the client mid-call:

- **Sampling** — the server asks to use *your* model. `ask` (the default) shows
  what it wants and waits for y/N; `allow` and `deny` decide without asking. In
  `-p` there is nobody to ask, so `ask` means no. Approved requests run through
  the same client, are logged with `purpose: sampling`, and count toward the
  session's tokens. Servers that run their own small tool loop inside sampling
  are supported.
- **Elicitation** — the server asks you a question: a form (each field prompted and
  validated: strings, numbers, booleans, choices) or a URL to visit. In `-p` it is
  always declined.
- **Roots** — servers are told the workspace is the root they may work in.

These happen during a tool call, while the steering input is watching stdin and a
status line is ticking, so both are paused while you answer and resumed after.

## In the REPL

`/mcp` shows each server's state, auth and counts. `/mcp tools <server>` lists
every tool it offers, including the ones your allowlist hides. Also: `/mcp
resources`, `/mcp prompts`, `/mcp login|logout|reconnect <server>`. Server prompts
are commands — `/mcp__<server>__<prompt> arg…`, positional or `name=value`, with
Tab completing names and, where the server supports it, argument values.

Resources are offered to the model as two tools, `mcp__list_resources` and
`mcp__read_resource`, added only when a connected server has any. Results are
text: an image, audio or binary resource is described (`[image/png image, 48 KB —
not shown]`), since there is no vision path here.

When a server changes its tool list mid-session, the model sees the new list on its
next request. A server whose tool definitions differ from what it advertised on the
previous run gets a warning at connect — a reviewed server that quietly rewrites its
own tool descriptions later is the classic way to slip instructions to a model.

Server stderr and SDK diagnostics go to `logs/<session>.mcp.log`, never the
terminal. The JSONL log gets `mcp_connect` (protocol, counts, schema size, tool-list
hash), `mcp_tools_changed`, `mcp_log`, `mcp_sampling_*` and `mcp_elicitation`
events, and every `tool_call` of an MCP tool carries a `server` field.

## In a container

The image includes the SDK (about 26 MB, mostly `cryptography`). Two things to mount:

```sh
docker run --rm --env-file .env -e MCP_ENABLED=1 \
  -v "$PWD/mcp.json":/harness/mcp.json:ro \
  -v harness-mcp:/harness/mcp-state \
  -v "$PWD/workspace":/ws -e HARNESS_WORKSPACE=/ws \
  harness -p "do a thing"
```

The state directory must be **read-write**: a token refresh writes back to it, and
with a read-only mount every run would succeed until the first access token
expired. A named volume picks up the image's ownership (uid 1001, mode 0700) on
first use, so no `chown` is needed.

Logging in, either way works:

```sh
# on the host, into a directory you then mount
python3 main.py --mcp-login linear --mcp-state-dir ./mcp-state
# or inside the container: open the URL anywhere, paste the address you land on
docker run -it --rm -v "$PWD/mcp.json":/harness/mcp.json:ro \
  -v harness-mcp:/harness/mcp-state harness --mcp-login linear --no-browser
```

stdio servers run inside the container, so their runtime has to be in the image —
`FROM harness`, then install node or uv and the server itself at build time. `npx
-y` at run time needs a writable `HOME` and egress to the package registry, which a
locked-down container does not have. stdio servers get only `HOME`, `PATH`,
`SHELL`, `TERM`, `USER`, `LOGNAME`, the proxy variables, and their own `env` from
`mcp.json` — never the harness's API key.

For the hardened stack: every HTTP server needs an allowlist entry for its own host
**and** its authorization server's host, since token refresh goes there. A server
the proxy refuses fails the run cleanly (`MCP unavailable: … error response`), and
the proxy log names it. See [`examples/hardened/README.md`](../examples/hardened/README.md).

## What MCP changes about security

The [Security](security.md) page's boundary still holds — the container is it —
but MCP moves two things:

- **"Nothing outside the workspace is reachable" stops being true.** MCP tools act
  on real accounts and services by design. The system prompt says so while servers
  are connected.
- **Tokens are readable by the agent**, the same way the harness's own API key is:
  same uid, and `mcp-state/` is a directory it can `cat`. They are kept out of the
  logs, the result file and the shell's environment, and the harness warns if the
  state directory is inside the workspace — but that stops accidents, not a
  determined agent. Give each server **the narrowest token or scopes it will
  accept**, and treat a container that ran with a token as having seen it.
