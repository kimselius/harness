"""A real MCP server for the MCP tests, built with the SDK's own server package.

Run as a script it serves stdio. `serve_http()` runs it over streamable HTTP on a
random local port, optionally behind a real OAuth authorization server (the
SDK's own discovery, registration, PKCE and token endpoints, backed by the
in-memory provider below) — so the login tests exercise the actual protocol
rather than a stand-in.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import secrets
import socket
import sys
import threading
import time
from typing import Annotated, Any
from urllib.parse import urlencode

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.resolve import Elicit, ListRoots, Resolve, Sample
from mcp_types import (
    CreateMessageResult,
    ImageContent,
    ListRootsResult,
    SamplingMessage,
    TextContent,
)
from pydantic import BaseModel

FIXTURE_LOG = os.environ.get("FIXTURE_LOG")
# Set by serve_http(): in-process, the server's stderr is the test's own.
IN_PROCESS = False
PIXEL = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64).decode()


def note(text: str) -> None:
    if FIXTURE_LOG:
        with open(FIXTURE_LOG, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")


class Person(BaseModel):
    name: str
    age: int


def _ask_person() -> Elicit[Person]:
    return Elicit("Who are you?", Person)


def _sample_request() -> Sample:
    return Sample(
        [SamplingMessage(role="user", content=TextContent(type="text", text="say hi"))],
        max_tokens=50,
        system_prompt="Be brief.",
    )


def _roots_request() -> ListRoots:
    return ListRoots()


def build(auth: dict[str, Any] | None = None, instructions: str | None = None) -> MCPServer:
    server = MCPServer(
        "fixture",
        instructions=instructions or "Fixture server. Prefer echo for testing.",
        **(auth or {}),
    )

    @server.tool()
    def echo(text: str) -> str:
        """Echo text back."""
        if not IN_PROCESS:  # stdio: the test checks this lands in the .mcp.log
            print(f"stderr: echo {text}", file=sys.stderr, flush=True)
        return f"echo: {text}"

    @server.tool()
    def env(name: str) -> str:
        """Report one environment variable of the server process."""
        return f"{name}={os.environ.get(name, '<unset>')}"

    @server.tool()
    async def slow(seconds: float, ctx: Context) -> str:
        """Sleep, reporting progress."""
        steps = max(1, int(seconds * 10))
        try:
            for i in range(steps):
                await ctx.report_progress(i, steps, "sleeping")
                await asyncio.sleep(0.1)
        except BaseException as exc:
            note(f"slow cancelled: {type(exc).__name__}")
            raise
        return "slept"

    @server.tool()
    def fail(message: str) -> str:
        """Always fails."""
        raise ValueError(message)

    @server.tool(structured_output=False)
    def picture() -> list[Any]:
        """Return an image and a caption."""
        return [TextContent(type="text", text="a caption"),
                ImageContent(type="image", data=PIXEL, mime_type="image/png")]

    @server.tool()
    def stats() -> dict[str, int]:
        """Structured output."""
        return {"files": 3, "lines": 120}

    @server.tool()
    def crash() -> str:
        """Kill the server process mid-call."""
        os._exit(3)

    @server.tool()
    def ask_person(person: Annotated[Person, Resolve(_ask_person)]) -> str:
        """Elicit a person from the user."""
        return f"hello {person.name}, {person.age}"

    @server.tool()
    def ask_model(reply: Annotated[CreateMessageResult, Resolve(_sample_request)]) -> str:
        """Sample the client's model."""
        content = reply.content
        text = content.text if isinstance(content, TextContent) else str(content)
        return f"model said: {text}"

    @server.tool()
    def ask_roots(roots: Annotated[ListRootsResult, Resolve(_roots_request)]) -> str:
        """List the client's roots."""
        return "roots: " + ", ".join(str(r.uri) for r in roots.roots)

    @server.tool()
    async def grow(ctx: Context) -> str:
        """Add a tool, and tell the client its tool list changed."""

        def extra(x: int) -> str:
            """Added at runtime."""
            return f"extra {x}"

        server.add_tool(extra, name="extra")
        # Both eras: 2026-07-28 clients hear it on their listen stream, earlier
        # ones as a pushed notification. A real server has to do the same.
        await ctx.notify_tools_changed()
        with contextlib.suppress(Exception):
            await ctx.session.send_tool_list_changed()
        return "grown"

    @server.resource("note://hello", mime_type="text/plain")
    def hello() -> str:
        """A greeting."""
        return "hello resource"

    @server.resource("note://item/{item}")
    def item(item: str) -> str:
        """One item."""
        return f"item {item}"

    @server.prompt()
    def greet(name: str, mood: str = "warm") -> str:
        """Greet someone."""
        return f"Write a {mood} greeting for {name}."

    return server


# -- HTTP, optionally behind OAuth ----------------------------------------------
def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class MemoryAuth:
    """An authorization server that approves everything, for tests.

    `authorize` answers with the redirect straight away instead of showing a
    login page, so a "browser" that simply follows redirects completes the
    flow. Access tokens can be made short-lived to exercise refresh.
    """

    def __init__(self, access_ttl: int = 3600) -> None:
        self.access_ttl = access_ttl
        self.clients: dict[str, Any] = {}
        self.codes: dict[str, Any] = {}
        self.access: dict[str, Any] = {}
        self.refresh: dict[str, Any] = {}
        self.resources: dict[str, str | None] = {}
        self.refreshes = 0
        self.registrations = 0
        self.client_credentials: dict[str, str] = {}  # id -> secret

    async def get_client(self, client_id: str):
        return self.clients.get(client_id)

    async def register_client(self, client_info) -> None:
        self.registrations += 1
        self.clients[client_info.client_id] = client_info

    async def authorize(self, client, params) -> str:
        from mcp.server.auth.provider import AuthorizationCode

        code = secrets.token_urlsafe(16)
        self.codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=time.time() + 300,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        query = {"code": code}
        if params.state:
            query["state"] = params.state
        return f"{params.redirect_uri}?{urlencode(query)}"

    async def load_authorization_code(self, client, authorization_code: str):
        return self.codes.get(authorization_code)

    def _issue(self, client_id: str, scopes: list[str], resource: str | None):
        from mcp.server.auth.provider import AccessToken, RefreshToken
        from mcp.shared.auth import OAuthToken

        access = f"at_{secrets.token_urlsafe(24)}"
        refresh = f"rt_{secrets.token_urlsafe(24)}"
        self.access[access] = AccessToken(
            token=access, client_id=client_id, scopes=scopes,
            expires_at=int(time.time()) + self.access_ttl, resource=resource,
        )
        self.refresh[refresh] = RefreshToken(
            token=refresh, client_id=client_id, scopes=scopes,
        )
        # The audience a refreshed token must keep; RefreshToken has no field
        # for it, and a refresh that drops it yields a token the resource
        # server (rightly) refuses.
        self.resources[refresh] = resource
        return OAuthToken(access_token=access, token_type="Bearer",
                          expires_in=self.access_ttl, refresh_token=refresh,
                          scope=" ".join(scopes) or None)

    async def exchange_authorization_code(self, client, authorization_code):
        self.codes.pop(authorization_code.code, None)
        return self._issue(client.client_id, authorization_code.scopes,
                           authorization_code.resource)

    async def load_refresh_token(self, client, refresh_token: str):
        return self.refresh.get(refresh_token)

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        self.refreshes += 1
        self.refresh.pop(refresh_token.token, None)  # rotate
        resource = self.resources.pop(refresh_token.token, None)
        return self._issue(client.client_id, scopes or refresh_token.scopes, resource)

    async def load_access_token(self, token: str):
        found = self.access.get(token)
        if found and found.expires_at and found.expires_at < time.time():
            return None
        return found

    async def revoke_token(self, token) -> None:
        self.access.pop(getattr(token, "token", ""), None)


class ExternalIssuer:
    """A separate authorization server that only does client_credentials.

    The SDK's own authorization server does not implement that grant, and a
    real machine-to-machine setup usually has the issuer on another host
    anyway — which is the case that needs the token endpoint remembered across
    restarts. Tokens it issues are accepted by the fixture's `verify_token`.
    """

    def __init__(self, client_id: str, client_secret: str, ttl: int = 3600) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.ttl = ttl
        self.tokens: dict[str, float] = {}
        self.issued = 0
        self.url = ""
        self._server = None

    async def verify_token(self, token: str):
        from mcp.server.auth.provider import AccessToken

        expires = self.tokens.get(token)
        if expires is None or expires < time.time():
            return None
        return AccessToken(token=token, client_id=self.client_id, scopes=[],
                           expires_at=int(expires))

    def start(self) -> str:
        import json
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.parse import parse_qs

        issuer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, code: int, body: dict) -> None:
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                if self.path.startswith("/.well-known/oauth-authorization-server"):
                    self._json(200, {
                        "issuer": issuer.url,
                        "authorization_endpoint": f"{issuer.url}/authorize",
                        "token_endpoint": f"{issuer.url}/token",
                        "response_types_supported": ["code"],
                        "grant_types_supported": ["client_credentials"],
                        "token_endpoint_auth_methods_supported": [
                            "client_secret_basic", "client_secret_post"],
                    })
                else:
                    self._json(404, {"error": "not_found"})

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                form = parse_qs(self.rfile.read(length).decode())
                basic = self.headers.get("Authorization", "")
                ident = secret = None
                if basic.startswith("Basic "):
                    ident, _, secret = base64.b64decode(basic[6:]).decode().partition(":")
                ident = ident or form.get("client_id", [None])[0]
                secret = secret or form.get("client_secret", [None])[0]
                if form.get("grant_type") != ["client_credentials"]:
                    return self._json(400, {"error": "unsupported_grant_type"})
                if (ident, secret) != (issuer.client_id, issuer.client_secret):
                    return self._json(401, {"error": "invalid_client"})
                token = f"cc_{secrets.token_urlsafe(16)}"
                issuer.tokens[token] = time.time() + issuer.ttl
                issuer.issued += 1
                self._json(200, {"access_token": token, "token_type": "Bearer",
                                 "expires_in": issuer.ttl})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self.url

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()


def serve_http(oauth: MemoryAuth | None = None, instructions: str | None = None,
               issuer: ExternalIssuer | None = None):
    """Start the fixture over streamable HTTP. Returns (url, stop)."""
    import uvicorn

    global IN_PROCESS
    IN_PROCESS = True

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    auth: dict[str, Any] = {}
    if issuer is not None:
        from mcp.server.auth.settings import AuthSettings

        auth = {
            "token_verifier": issuer,
            "auth": AuthSettings(issuer_url=issuer.url,
                                 resource_server_url=f"{base}/mcp",
                                 validate_token_resource=False),
        }
    elif oauth is not None:
        from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions

        auth = {
            "auth_server_provider": oauth,
            "auth": AuthSettings(
                issuer_url=base,
                resource_server_url=f"{base}/mcp",
                client_registration_options=ClientRegistrationOptions(enabled=True),
                validate_token_resource=True,
            ),
        }
    server = build(auth, instructions)
    app = server.streamable_http_app(host="127.0.0.1")
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    runner = uvicorn.Server(config)
    thread = threading.Thread(target=runner.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not runner.started and time.monotonic() < deadline:
        time.sleep(0.02)

    def stop() -> None:
        runner.should_exit = True
        thread.join(timeout=5)

    return f"{base}/mcp", stop


if __name__ == "__main__":
    build().run()
