# Development

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
toolset.py   the tools one agent can call: built-ins plus MCP, one registry
mcp_config.py  mcp.json parsing, ${VAR} expansion, validation
mcp_client.py  MCP runtime: SDK on a background loop, tools, callbacks
mcp_auth.py    token store, OAuth providers, --mcp-login flow
mcp_cli.py     /mcp, --mcp-list, prompts as commands, answering servers
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
examples/hardened/ locked-down container + egress allowlist
```
