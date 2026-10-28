"""Reasoning / thinking checks. Run: .venv/bin/python tests/test_reasoning.py"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import mock_server  # noqa: E402

import llm as llm_mod  # noqa: E402
from agent import Agent, _Printer  # noqa: E402
from config import build_parser, config_from_args  # noqa: E402
from logbook import Logbook  # noqa: E402

failures: list[str] = []
THOUGHT = "Let me work through this carefully, step by step, before answering."


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        failures.append(label)
        print(f"  FAIL {label} {detail}")


def make_agent(tmp: Path, base_url: str, extra: list[str] | None = None) -> Agent:
    os.environ.setdefault("OPENAI_API_KEY", "x")
    cfg = config_from_args(
        build_parser().parse_args(
            [
                "--model", "mock",
                "--base-url", base_url,
                "--workspace", str(tmp / "ws"),
                "--log-dir", str(tmp / "logs"),
                "--prompt-file", str(tmp / "nope.md"),
                *(extra or []),
            ]
        )
    )
    return Agent(cfg, Logbook(cfg.log_path))


def records(agent: Agent) -> list[dict]:
    return [
        json.loads(line)
        for line in agent.cfg.log_path.read_text().splitlines()
        if line.strip()
    ]


def test_token_parsing() -> None:
    print("usage shapes")
    check("openai shape", llm_mod.reasoning_tokens(
        {"completion_tokens_details": {"reasoning_tokens": 120}}) == 120)
    check("flat shape", llm_mod.reasoning_tokens({"reasoning_tokens": 77}) == 77)
    check("prefers the nested one", llm_mod.reasoning_tokens(
        {"reasoning_tokens": 1, "completion_tokens_details": {"reasoning_tokens": 2}}) == 2)
    check("absent", llm_mod.reasoning_tokens({"completion_tokens": 5}) is None)
    check("no usage", llm_mod.reasoning_tokens(None) is None)
    check("null details tolerated", llm_mod.reasoning_tokens(
        {"completion_tokens_details": None, "reasoning_tokens": 9}) == 9)


def test_field_aliases() -> None:
    print("field aliases")

    class Fake:
        def __init__(self, **kw):
            self.model_extra = kw

    for name in llm_mod.REASONING_FIELDS:
        check(f"reads {name}",
              llm_mod._extra(Fake(**{name: "thought"}), llm_mod.REASONING_FIELDS)
              == "thought")
    check("empty when absent", llm_mod._extra(Fake(), llm_mod.REASONING_FIELDS) == "")
    check("none tolerated", llm_mod._extra(None, llm_mod.REASONING_FIELDS) == "")


def test_captured_and_logged(tmp: Path, base_url: str, stream: bool) -> None:
    label = "stream" if stream else "non-stream"
    print(f"captured and logged ({label})")
    mock_server.reset()
    mock_server.set_script([("the answer", [])])
    mock_server.REASONING.append(THOUGHT)
    agent = make_agent(
        tmp / f"cap-{label}", base_url,
        ["--reasoning-effort", "high", *([] if stream else ["--no-stream"])],
    )
    out = io.StringIO()
    with redirect_stdout(out):
        final = agent.run("think about it")

    check("answer returned", final == "the answer", final)
    response = next(r for r in records(agent) if r["type"] == "llm_response")
    check("reasoning logged in full", response["reasoning"] == THOUGHT,
          repr(response.get("reasoning"))[:80])
    check("reasoning chars logged", response["reasoning_chars"] == len(THOUGHT))
    check("reasoning tokens logged", response["reasoning_tokens"] == len(THOUGHT) // 4,
          str(response.get("reasoning_tokens")))
    check("agent tracked the tokens",
          agent.last_reasoning_tokens == len(THOUGHT) // 4,
          str(agent.last_reasoning_tokens))

    # Never replayed: OpenAI-compatible APIs do not accept it in an assistant turn.
    check("not in history",
          all(THOUGHT not in json.dumps(m) for m in agent.history),
          json.dumps(agent.history)[:120])
    check("hidden by default", THOUGHT not in out.getvalue())
    check("but its cost is reported", "thought for" in out.getvalue(),
          out.getvalue()[-120:])


def test_show_thinking(tmp: Path, base_url: str) -> None:
    print("--show-thinking")
    mock_server.reset()
    mock_server.set_script([("the answer", [])])
    mock_server.REASONING.append(THOUGHT)
    agent = make_agent(tmp / "show", base_url, ["--show-thinking"])
    out = io.StringIO()
    with redirect_stdout(out):
        agent.run("think about it")
    screen = out.getvalue()
    check("reasoning printed", THOUGHT in screen, screen[:160])
    check("labelled", "thinking" in screen)
    check("answer still printed", "the answer" in screen)
    check("no duplicate cost note", "thought for" not in screen)
    check("still absent from history",
          all(THOUGHT not in json.dumps(m) for m in agent.history))


def test_effort_is_sent(tmp: Path, base_url: str) -> None:
    print("reasoning_effort on the wire")
    mock_server.reset()
    mock_server.set_script([("ok", [])])
    agent = make_agent(tmp / "eff", base_url, ["--reasoning-effort", "medium",
                                               "--no-stream"])
    agent.run("hello")
    sent = mock_server.REQUESTS[-1]
    check("sent with the request", sent.get("reasoning_effort") == "medium",
          str(sent.get("reasoning_effort")))
    request = next(r for r in records(agent) if r["type"] == "llm_request")
    check("logged on the request", request["reasoning_effort"] == "medium")

    mock_server.reset()
    mock_server.set_script([("ok", [])])
    plain = make_agent(tmp / "default", base_url, ["--no-stream"])
    plain.run("hello")
    check("on by default", mock_server.REQUESTS[-1].get("reasoning_effort") == "medium",
          str(mock_server.REQUESTS[-1].get("reasoning_effort")))

    for word in ("off", "none", "false", ""):
        mock_server.reset()
        mock_server.set_script([("ok", [])])
        agent_off = make_agent(tmp / f"off-{word or 'empty'}", base_url,
                               ["--no-stream", "--reasoning-effort", word])
        agent_off.run("hello")
        check(f"--reasoning-effort {word!r} disables it",
              "reasoning_effort" not in mock_server.REQUESTS[-1],
              str(mock_server.REQUESTS[-1].get("reasoning_effort")))


def test_summaries_do_not_think(tmp: Path, base_url: str) -> None:
    print("summaries skip thinking")
    mock_server.reset()
    mock_server.set_script([("a summary", [])])
    agent = make_agent(tmp / "sum", base_url,
                       ["--reasoning-effort", "high", "--no-stream",
                        "--context-tokens", "8000"])
    agent.history = [{"role": "user", "content": f"task {i} " + "x" * 3000}
                     for i in range(6)]
    out = io.StringIO()
    with redirect_stdout(out):
        agent.compact(reason="test")
    summary_requests = [
        r for r in records(agent)
        if r["type"] == "llm_request" and r.get("purpose") == "compaction"
    ]
    check("a summary request was made", len(summary_requests) == 1,
          str(len(summary_requests)))
    check("no effort on it", summary_requests[0]["reasoning_effort"] is None,
          str(summary_requests[0]["reasoning_effort"]))
    check("wire body agrees", "reasoning_effort" not in mock_server.REQUESTS[-1])


def test_extra_body(tmp: Path, base_url: str) -> None:
    print("--extra-body")
    mock_server.reset()
    mock_server.set_script([("ok", [])])
    agent = make_agent(tmp / "extra", base_url,
                       ["--extra-body", '{"top_k": 40}', "--no-stream"])
    agent.run("hello")
    check("merged into the body", mock_server.REQUESTS[-1].get("top_k") == 40,
          str(mock_server.REQUESTS[-1].get("top_k")))


def test_graceful_degradation(tmp: Path, base_url: str) -> None:
    print("endpoint rejects the parameter")
    mock_server.reset()
    mock_server.set_script([("ok anyway", [])])
    mock_server.REJECT.add("reasoning_effort")
    agent = make_agent(tmp / "reject", base_url,
                       ["--reasoning-effort", "high", "--no-stream"])
    out = io.StringIO()
    with redirect_stdout(out):
        final = agent.run("hello")
    check("turn still succeeded", final == "ok anyway", final)
    check("retried without it", "reasoning_effort" not in mock_server.REQUESTS[-1],
          str(mock_server.REQUESTS[-1].keys()))
    check("first attempt did send it",
          "reasoning_effort" in mock_server.REQUESTS[0])
    check("drop logged", any(r["type"] == "params_dropped" for r in records(agent)))
    check("user told", "rejected" in out.getvalue(), out.getvalue()[:160])

    # And it stops trying on later calls in the same session.
    before = len(mock_server.REQUESTS)
    mock_server.set_script([("second", [])])
    with redirect_stdout(io.StringIO()):
        agent.run("again")
    check("not retried on later turns",
          all("reasoning_effort" not in r for r in mock_server.REQUESTS[before:]))
    mock_server.reset()


def test_printer_interleaving() -> None:
    print("printer ordering")
    out = io.StringIO()
    with redirect_stdout(out):
        printer = _Printer(markdown_on=False, style=False, show_thinking=True)
        printer.feed_reasoning("pondering...")
        printer.feed("the answer")
        printer.finish("the answer")
    screen = out.getvalue()
    check("thinking before answer",
          screen.index("pondering") < screen.index("the answer"), repr(screen))
    check("newline between them", "\n" in screen[screen.index("pondering"):],
          repr(screen))

    out = io.StringIO()
    with redirect_stdout(out):
        printer = _Printer(markdown_on=False, style=False, show_thinking=False)
        printer.feed_reasoning("secret")
        printer.finish("visible")
    check("hidden when off", "secret" not in out.getvalue())
    check("answer still shown", "visible" in out.getvalue())


def main_() -> int:
    server, base_url = mock_server.start()
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        test_token_parsing()
        test_field_aliases()
        test_captured_and_logged(tmp, base_url, stream=False)
        test_captured_and_logged(tmp, base_url, stream=True)
        test_show_thinking(tmp, base_url)
        test_effort_is_sent(tmp, base_url)
        test_summaries_do_not_think(tmp, base_url)
        test_extra_body(tmp, base_url)
        test_graceful_degradation(tmp, base_url)
        test_printer_interleaving()
    server.shutdown()
    print()
    if failures:
        print(f"{len(failures)} failed: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main_())
