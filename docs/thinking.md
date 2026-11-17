# Thinking

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

## Endpoint differences

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
