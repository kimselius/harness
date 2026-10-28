"""Mock OpenAI-compatible /chat/completions server used by test_harness.py.

Scripted: it walks a fixed list of canned assistant turns, one per request, so the
harness' tool loop, streaming parser and history handling can be exercised offline.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Each entry: (text, [(tool_name, args_dict), ...])
SCRIPT: list[tuple[str, list[tuple[str, dict]]]] = [
    ("Creating the file.", [("write_file", {"path": "notes.txt", "content": "alpha\nbeta\n"})]),
    ("", [("edit_file", {"path": "notes.txt", "old_string": "beta", "new_string": "gamma"})]),
    ("", [("read_file", {"path": "notes.txt"})]),
    ("", [("shell", {"command": "ls && printf 'x\\n' && cat notes.txt"})]),
    ("", [("shell", {"command": "seq 1 5000"})]),           # truncation path
    ("", [("read_file", {"path": "../../../etc/passwd"})]),  # escape attempt
    ("", [("edit_file", {"path": "notes.txt", "old_string": "nope", "new_string": "x"})]),
    ("", [("shell", {"command": "sleep 30", "timeout": 1})]),  # timeout path
    ("", [("bogus_tool", {})]),
    ("Done: notes.txt contains alpha/gamma.", []),
]

REQUESTS: list[dict] = []

# Set by set_script() to override SCRIPT for a given test.
ACTIVE: list[tuple[str, list[tuple[str, dict]]]] = SCRIPT

# Reasoning text echoed back as reasoning_content, and the token count reported.
REASONING: list[str] = []
# Request body keys that make the server answer 400, for testing degradation.
REJECT: set[str] = set()

# Append True to make the next request fail with a 500 (retries are exhausted by
# the SDK, so one entry is enough to surface the error to the caller).
FAIL_NEXT: list[bool] = []


def set_script(script: list[tuple[str, list[tuple[str, dict]]]]) -> None:
    """Replace the canned turns and reset the request counter."""
    global ACTIVE
    ACTIVE = script
    REQUESTS.clear()


def reset() -> None:
    REASONING.clear()
    REJECT.clear()
    FAIL_NEXT.clear()
    REQUESTS.clear()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        REQUESTS.append(body)
        bad = sorted(REJECT & set(body))
        if bad:
            payload = json.dumps({
                "error": {"message": f"unsupported parameter: {bad[0]}",
                          "type": "invalid_request_error"}
            }).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if FAIL_NEXT:
            FAIL_NEXT.pop()
            payload = json.dumps({"error": {"message": "mock failure"}}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        index = min(len(REQUESTS) - 1, len(ACTIVE) - 1)
        text, calls = ACTIVE[index]
        if body.get("stream"):
            self._stream(text, calls)
        else:
            self._whole(text, calls)

    def _usage(self) -> dict:
        usage = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
        if REASONING:
            usage["reasoning_tokens"] = len(REASONING[0]) // 4
        return usage

    def _message(self, text: str, calls: list[tuple[str, dict]]) -> dict:
        message: dict = {"role": "assistant", "content": text}
        if REASONING:
            message["reasoning_content"] = REASONING[0]
        if calls:
            message["tool_calls"] = [
                {
                    "id": f"call_{i}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
                for i, (name, args) in enumerate(calls)
            ]
        return message

    def _send(self, payload: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _whole(self, text: str, calls: list[tuple[str, dict]]) -> None:
        payload = {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": 0,
            "model": "mock",
            "choices": [
                {
                    "index": 0,
                    "message": self._message(text, calls),
                    "finish_reason": "tool_calls" if calls else "stop",
                }
            ],
            "usage": self._usage(),
        }
        self._send(json.dumps(payload).encode(), "application/json")

    def _stream(self, text: str, calls: list[tuple[str, dict]]) -> None:
        chunks: list[dict] = []

        def frame(delta: dict, finish=None) -> dict:
            return {
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "mock",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }

        # reasoning arrives before content, in its own deltas
        if REASONING:
            blob = REASONING[0]
            for i in range(0, len(blob), 5):
                chunks.append(frame({"reasoning_content": blob[i : i + 5]}))
        # text split into small pieces to exercise delta accumulation
        for i in range(0, len(text), 4):
            chunks.append(frame({"content": text[i : i + 4]}))
        for i, (name, args) in enumerate(calls):
            chunks.append(
                frame(
                    {
                        "tool_calls": [
                            {
                                "index": i,
                                "id": f"call_{i}",
                                "type": "function",
                                "function": {"name": name, "arguments": ""},
                            }
                        ]
                    }
                )
            )
            blob = json.dumps(args)
            for j in range(0, len(blob), 7):  # split arguments across chunks
                chunks.append(
                    frame(
                        {
                            "tool_calls": [
                                {
                                    "index": i,
                                    "function": {"arguments": blob[j : j + 7]},
                                }
                            ]
                        }
                    )
                )
        chunks.append(frame({}, "tool_calls" if calls else "stop"))
        usage_frame = {
            "id": "chatcmpl-mock",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "mock",
            "choices": [],
            "usage": self._usage(),
        }
        chunks.append(usage_frame)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")  # body ends at EOF
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True


def start() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}/v1"
