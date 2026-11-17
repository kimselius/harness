# Log format

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
