# Interactive use

## REPL commands

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
| `/mcp [help\|…]` | MCP servers, their tools, resources and prompts; login (see [MCP](mcp.md)) |
| `/mcp__<server>__<prompt>` | run a server's prompt as your message |
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

## Pasting

A multi-line paste is kept together as one message, and **held until you press
Enter**:

```
> please review this patch:
def f():
    retrun 1
⏎ 3 lines pasted, 44 chars — Enter to send, keep typing to add, Ctrl-C to discard:
  │ please review this patch:
  │ def f():
  │     retrun 1
… and fix the typo
…
```

Enter sends it, typing adds to it (continuing the last line if the paste stopped
mid-line, otherwise starting a new one), another paste is absorbed into the same
message, and Ctrl-C throws the whole thing away.

Holding it is the point. A terminal delivers a paste as one burst and `input()`
returns at the first newline, so the harness collects the rest from the input
buffer — and it used to send that straight to the model. Pasting fired off a
message you had neither finished composing nor seen, since the terminal had only
echoed the first line by the time it was gone.

The clean fix would be bracketed paste, where the line editor holds the block
itself. GNU readline 8.1+ can (`enable-bracketed-paste`), but libedit — which is
what `readline` is on every macOS Python — cannot, and silently accepts the
setting while ignoring it. So the confirmation lives in the harness, where it
behaves the same on every platform.

Assembly also normalizes CRLF and bare CR, and strips bracketed-paste markers if
the terminal sends them. To type a multi-line message by hand instead, start with
`<<<` and end with `>>>` on its own line.

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

### Knowing it is alive

Most of a turn produces no output at all, and every one of those stretches used to
look identical to a hang. There are four, and each now keeps a status line going
on stderr, rewritten in place:

```
⟳ step 1 · ~41200 tokens sent · 8s          prefill: the server is reading the prompt
⟳ thinking · 21s 4300 chars so far          reasoning, with --show-thinking off
⟳ write_file · 26s 18400 chars of arguments a tool call being assembled
  ⎿ npm run build · 54s                     a command actually running
```

The seconds are cumulative for the request, so the number never restarts. The
reasoning line matters most: hidden thinking streams nothing, and a long thinking
phase on a large model is the commonest "it's stuck" moment. Tool arguments are
the next commonest — a `write_file` of a few hundred lines arrives as tens of
thousands of characters with nothing else on the wire.

The line yields the moment real output starts, and never resumes over a
half-written line of prose. It requires a terminal: under `--quiet`, in a pipe or
in a cron log there is no status line, because rewriting one there would just fill
the file with fragments.

The running-command line is **not** a substitute for streaming a command's output,
which is still not implemented — it tells you the thing is alive and for how long,
not what it is doing. The system prompt still directs long jobs to redirect to a
file and poll.
