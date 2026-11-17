# Known limits

- Compaction is lossy by design. The summary is written by the same small model
  doing the work, so check `/context` and the `compaction` log events if a session
  starts forgetting things; `--summary-model` can point at a stronger model.
- Nothing stops the model from running a destructive command. That's the point of
  running it in a container.
- A long-running command's **output** is not streamed: you see a status line with
  its elapsed time, then everything at once when it exits. The system prompt tells
  the model to redirect long jobs to a file and poll.
- `--max-seconds` is checked between tool rounds, not inside one. A command already
  running is bounded by `--shell-timeout` instead, so the worst case is one
  command's timeout past the deadline.
- MCP results are text. Images, audio and binary resources are described, not
  shown, because the harness has no vision path.
- MCP resource subscriptions are not used: a turn-based loop has nothing to consume
  update events between turns. `mcp__read_resource` reads the current state.
- A run never logs in interactively. An OAuth server whose refresh token has been
  revoked or has expired fails unattended runs until someone runs `--mcp-login`.
- Behind the hardened proxy, a refused MCP host surfaces as the SDK's generic
  "error response"; the proxy log is where the host name is.
- There is no pipeline runner inside the harness. Chaining is a shell script
  (`examples/report/run.sh`), which also means no way to re-run "step 4 onwards"
  except by what the script itself offers.
