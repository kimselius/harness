# Platforms

| | Linux | macOS | Windows (native) |
| --- | --- | --- | --- |
| REPL, sessions, compaction, logging | yes | yes | yes |
| Prompt wrapping / line editing | GNU readline | libedit | plain console, or pyreadline3 |
| Colour | yes | yes | VT enabled at startup, else colour off |
| Paste held for review, sent on Enter | yes | yes | no, one line per message |
| Typing while it works | yes | yes | no, wait for the prompt |
| `shell` tool | `/bin/sh -c` | `/bin/sh -c` | `cmd.exe`, POSIX syntax will not work |
| Persistent shell directory | yes | yes | no, stays at the workspace root |
| Timeout / Ctrl-C kills the whole process tree | yes | yes | direct child only |

Everything platform-specific degrades rather than crashing, but **on Windows use
WSL or Docker**. Two deliberate non-goals keep the code simple: there is one shell
implementation (POSIX `sh`) and one way of watching stdin (`select`). Supporting
`cmd.exe` would mean a second command dialect, a second working-directory probe and
job objects instead of process groups — doubling the surface of the riskiest tool to
solve something WSL already solves.
