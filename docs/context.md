# Context window and compaction

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
comfortable — compaction triggers at 72k with ~35k spare. Setting it *at* the
ceiling (128000 against 131072) leaves nothing for the reply or for estimate
drift, and a single overshoot is a failed turn.

**`--compact-keep` is the other half of the decision.** At the 0.25 default,
compacting at 75% throws away roughly two thirds of the context in one go, then
refills. That is a lot of recent work to convert into prose at once. For a job
with many steps, 0.4–0.5 keeps more of the actual work verbatim and loses less
per compaction, at the cost of compacting more often:

```sh
main.py --context-tokens 96000 --compact-at 0.75 --compact-keep 0.45
```

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
2. **Your opening request, verbatim.** Not a paraphrase of it.
3. A summary of everything else dropped: progress so far, files touched,
   environment facts, and what is still open.
4. The most recent `--compact-keep` worth of messages, verbatim.

Point 2 exists because of a specific failure. A run working through 19 items,
14 done, compacted — and started over doing different work. The reason: the
message stating the job ("all 19 items, strictly in order, append to `out/<n>.md`")
was in the half being summarized, so the only surviving copy was the summarizer's
paraphrase. The agent re-derived the task from prose and got a different task.

Three things changed:

- The opening request is now pinned ahead of the summary and kept word for word
  through every compaction, however many there are. A very large pasted brief is
  capped rather than reclaiming the window each time.
- The summary carries an explicit instruction not to start over. A summary sits in
  the `user` role, where instructions come from, so "the goal is X, still open: Y"
  reads like a fresh request unless it says otherwise.
- The summary prompt demands exact progress accounting — "items 1-14 of 19 done,
  15 in progress, 16-19 untouched", never "most items are done" — because that
  enumeration is what tells the agent not to redo work.

If a session resumes, replay rebuilds exactly this shape from the log, pinned
request included, so a resumed run sees the same task the original did.

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
