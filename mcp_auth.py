"""MCP authentication: stored credentials, OAuth providers, and the login flow.

Everything an MCP server needs to authenticate lives in one directory, one file
per server (`mcp-state/<server>.json`, 0600). Locally it simply persists; in a
container it is a mounted volume, the same way `logs/` is.

Three ways a remote server authenticates, all decided by mcp.json:
- a static header (`"headers": {"Authorization": "Bearer ${TOKEN}"}`) — nothing
  stored here at all;
- a machine grant (`client_credentials`, `private_key_jwt`) — headless, tokens
  cached here and re-fetched when they expire;
- the authorization-code flow — a person logs in once with `--mcp-login`, and
  every later run, `-p` included, uses and refreshes the stored tokens.

A run never starts an interactive login. Its redirect handler records that a
login is needed and fails, so an unattended run stops with a clear message
instead of waiting forever for a browser nobody is looking at.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import json
import os
import select
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from mcp_config import ServerSpec

try:  # POSIX only; on Windows the atomic replace alone has to do.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

CLIENT_NAME = "harness"


class LoginRequired(Exception):
    """The server wants an interactive login and this is not the time for one."""

    def __init__(self, server: str) -> None:
        super().__init__(f"not logged in — run: harness --mcp-login {server}")
        self.server = server


# -- the state directory ----------------------------------------------------
class StateStore:
    """One JSON file per server, written atomically under a lock.

    The lock matters for refresh-token rotation: two runs refreshing at once
    would otherwise each write the token the other has just invalidated.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            os.chmod(self.root, 0o700)

    def path(self, server: str) -> Path:
        return self.root / f"{server}.json"

    def load(self, server: str) -> dict[str, Any]:
        try:
            data = json.loads(self.path(server).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    @contextlib.contextmanager
    def _locked(self, server: str):
        self.ensure()
        lock_path = self.root / f".{server}.lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if fcntl:
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            if fcntl:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def update(self, server: str, **changes: Any) -> dict[str, Any]:
        """Merge `changes` into the server's file. A value of None deletes a key."""
        with self._locked(server):
            data = self.load(server)
            for key, value in changes.items():
                if value is None:
                    data.pop(key, None)
                else:
                    data[key] = value
            data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            fd, temp = tempfile.mkstemp(dir=self.root, prefix=f".{server}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(data, handle, indent=2)
                os.chmod(temp, 0o600)
                os.replace(temp, self.path(server))
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(temp)
                raise
            return data

    def delete(self, server: str) -> bool:
        try:
            self.path(server).unlink()
            return True
        except FileNotFoundError:
            return False

    def secrets(self, server: str) -> list[str]:
        """Credentials in a stored file, for the redactor."""
        data = self.load(server)
        tokens = data.get("tokens") or {}
        client = data.get("client_info") or {}
        values = [
            tokens.get("access_token"),
            tokens.get("refresh_token"),
            client.get("client_secret"),
        ]
        return [v for v in values if isinstance(v, str) and len(v) >= 8]


def status(store: StateStore, spec: ServerSpec) -> str:
    """One line on whether this server can authenticate without a person."""
    if not spec.remote:
        return "stdio (no auth)"
    if not spec.uses_oauth:
        return "static headers" if spec.headers else "no auth"
    if spec.oauth and spec.oauth.grant != "authorization_code":
        return f"{spec.oauth.grant} (headless)"
    data = store.load(spec.name)
    tokens = data.get("tokens") or {}
    if not tokens.get("access_token") or data.get("url") != spec.url:
        return "not logged in"
    expires = data.get("expires_at")
    refresh = " · refreshable" if tokens.get("refresh_token") else ""
    if not expires:
        return f"logged in{refresh}"
    left = expires - time.time()
    if left > 0:
        return f"logged in · expires in {_duration(left)}{refresh}"
    return f"logged in · access token expired{refresh or ' · log in again'}"


def _duration(seconds: float) -> str:
    if seconds >= 86400:
        return f"{seconds / 86400:.0f}d"
    if seconds >= 3600:
        return f"{seconds / 3600:.0f}h"
    return f"{max(1, seconds / 60):.0f}m"


# -- SDK glue ----------------------------------------------------------------
class FileTokenStorage:
    """The SDK's TokenStorage, backed by the state file.

    Everything is bound to the server URL it was issued for: if mcp.json now
    points the same name somewhere else, the stored tokens are not sent there.
    Besides tokens and client registration it keeps what the SDK forgets across
    restarts — when the token expires and where the token endpoint is — which is
    what lets a fresh process refresh instead of demanding a new login.
    """

    def __init__(self, store: StateStore, spec: ServerSpec) -> None:
        self.store = store
        self.spec = spec
        self.context: Any = None  # the provider's OAuthContext, set by the factory

    def _data(self) -> dict[str, Any]:
        data = self.store.load(self.spec.name)
        if data and data.get("url") != self.spec.url:
            return {}
        return data

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken

        raw = self._data().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens) -> None:
        expires_at = time.time() + tokens.expires_in if tokens.expires_in else None
        self.store.update(
            self.spec.name,
            url=self.spec.url,
            tokens=tokens.model_dump(mode="json", exclude_none=True),
            expires_at=expires_at,
            metadata=self._metadata(),
        )

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull

        raw = self._data().get("client_info")
        if raw:
            return OAuthClientInformationFull.model_validate(raw)
        oauth = self.spec.oauth
        if oauth and oauth.client_id and oauth.grant == "authorization_code":
            # A pre-registered client: use it instead of registering a new one.
            return OAuthClientInformationFull(
                client_id=oauth.client_id,
                client_secret=oauth.client_secret,
                redirect_uris=[redirect_uri(self.spec)],
                grant_types=["authorization_code", "refresh_token"],
                token_endpoint_auth_method=(
                    "client_secret_post" if oauth.client_secret else "none"
                ),
            )
        return None

    async def set_client_info(self, client_info) -> None:
        self.store.update(
            self.spec.name,
            url=self.spec.url,
            client_info=client_info.model_dump(mode="json", exclude_none=True),
        )

    def _metadata(self) -> dict[str, Any] | None:
        ctx = self.context
        if ctx is None:
            return None
        out: dict[str, Any] = {}
        if ctx.oauth_metadata is not None:
            out["oauth"] = ctx.oauth_metadata.model_dump(mode="json", exclude_none=True)
        if ctx.protected_resource_metadata is not None:
            out["resource"] = ctx.protected_resource_metadata.model_dump(
                mode="json", exclude_none=True
            )
        if ctx.auth_server_url:
            out["auth_server_url"] = ctx.auth_server_url
        return out or None

    def restore(self, ctx: Any) -> None:
        """Put back what the SDK does not reload by itself."""
        from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata

        data = self._data()
        expires_at = data.get("expires_at")
        if ctx.current_tokens is not None and isinstance(expires_at, (int, float)):
            ctx.token_expiry_time = float(expires_at)
        meta = data.get("metadata") or {}
        try:
            if meta.get("oauth") and ctx.oauth_metadata is None:
                ctx.oauth_metadata = OAuthMetadata.model_validate(meta["oauth"])
            if meta.get("resource") and ctx.protected_resource_metadata is None:
                ctx.protected_resource_metadata = ProtectedResourceMetadata.model_validate(
                    meta["resource"]
                )
        except Exception:
            pass  # stale or foreign metadata: discovery will find it again
        if meta.get("auth_server_url") and not ctx.auth_server_url:
            ctx.auth_server_url = meta["auth_server_url"]


def redirect_uri(spec: ServerSpec) -> str:
    port = spec.oauth.callback_port if spec.oauth else 0
    return f"http://localhost:{port}/callback"


def _restoring(base: type) -> type:
    """A provider class that also restores expiry and metadata on load."""

    class Restoring(base):  # type: ignore[valid-type, misc]
        async def _initialize(self) -> None:
            await super()._initialize()
            self.context.storage.restore(self.context)

    Restoring.__name__ = f"Harness{base.__name__}"
    return Restoring


def build_provider(
    spec: ServerSpec,
    store: StateStore,
    redirect_handler: Any = None,
    callback_handler: Any = None,
) -> Any:
    """The httpx2 auth object for a remote server, or None for static headers.

    Without handlers, an authorization-code server that needs a login raises
    LoginRequired, which is what every run (as opposed to --mcp-login) wants.
    """
    if not spec.uses_oauth:
        return None
    from mcp.client.auth import OAuthClientProvider
    from mcp.client.auth.extensions.client_credentials import (
        ClientCredentialsOAuthProvider,
        PrivateKeyJWTOAuthProvider,
        SignedJWTParameters,
    )
    from mcp.shared.auth import OAuthClientMetadata

    oauth = spec.oauth
    assert oauth is not None
    storage = FileTokenStorage(store, spec)

    if oauth.grant == "client_credentials":
        provider = _restoring(ClientCredentialsOAuthProvider)(
            server_url=spec.url,
            storage=storage,
            client_id=oauth.client_id,
            client_secret=oauth.client_secret,
            scope=oauth.scope,
            issuer=oauth.issuer,
        )
    elif oauth.grant == "private_key_jwt":
        key = Path(oauth.private_key_file or "").expanduser().read_text(encoding="utf-8")
        claims = {"kid": oauth.key_id} if oauth.key_id else None
        params = SignedJWTParameters(
            issuer=oauth.client_id,
            subject=oauth.client_id,
            signing_key=key,
            signing_algorithm=oauth.algorithm,
            additional_claims=claims,
        )
        provider = _restoring(PrivateKeyJWTOAuthProvider)(
            server_url=spec.url,
            storage=storage,
            client_id=oauth.client_id,
            assertion_provider=params.create_assertion_provider(),
            scope=oauth.scope,
            issuer=oauth.issuer,
        )
    else:
        async def needs_login(url: str) -> None:
            raise LoginRequired(spec.name)

        async def no_callback():
            raise LoginRequired(spec.name)

        metadata = OAuthClientMetadata(
            client_name=CLIENT_NAME,
            redirect_uris=[redirect_uri(spec)],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=oauth.scope,
        )
        provider = _restoring(OAuthClientProvider)(
            server_url=spec.url,
            client_metadata=metadata,
            storage=storage,
            redirect_handler=redirect_handler or needs_login,
            callback_handler=callback_handler or no_callback,
            client_metadata_url=oauth.client_metadata_url,
        )
    storage.context = provider.context
    return provider


# -- interactive login ------------------------------------------------------
def parse_redirect(text: str):
    """An AuthorizationCodeResult from a pasted redirect URL. None if it is not one."""
    from mcp.shared.auth import AuthorizationCodeResult

    query = parse_qs(urlparse(text.strip()).query)
    if "error" in query:
        detail = query.get("error_description", query["error"])[0]
        raise RuntimeError(f"authorization was refused: {detail}")
    code = query.get("code", [None])[0]
    if not code:
        return None
    return AuthorizationCodeResult(
        code=code,
        state=query.get("state", [None])[0],
        iss=query.get("iss", [None])[0],
    )


class _Callback:
    """Where the authorization code comes back: a loopback listener, or a paste.

    Whichever delivers first wins. The listener works when the browser runs on
    this machine; the paste works everywhere else — in a container, over SSH —
    because the browser shows the redirect URL in its address bar even when
    nothing answers it.
    """

    def __init__(self, port: int, out: Any) -> None:
        self.port = port
        self.out = out
        self.result: concurrent.futures.Future = concurrent.futures.Future()
        self.server: HTTPServer | None = None
        self._stop = threading.Event()

    def _deliver(self, value: Any = None, error: Exception | None = None) -> None:
        if self.result.done():
            return
        if error is not None:
            self.result.set_exception(error)
        else:
            self.result.set_result(value)

    def start_listener(self) -> bool:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence
                pass

            def do_GET(self):  # noqa: N802
                if urlparse(self.path).path != "/callback":
                    self.send_response(404)
                    self.end_headers()
                    return
                try:
                    result = parse_redirect(self.path)
                    error = None if result else RuntimeError("no code in the redirect")
                except Exception as exc:
                    result, error = None, exc
                body = (
                    "Logged in. You can close this tab and return to the terminal."
                    if result
                    else f"Login failed: {error}"
                ).encode()
                self.send_response(200 if result else 400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                owner._deliver(result, error)

        try:
            self.server = HTTPServer(("127.0.0.1", self.port), Handler)
        except OSError:
            return False
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return True

    def start_paste(self) -> bool:
        """Read a pasted redirect URL from stdin, without blocking a later reader.

        Polls with select() so it can stop cleanly: a thread parked in input()
        would swallow the next line typed at the REPL.
        """
        if not sys.stdin.isatty():
            return False

        def run() -> None:
            fd = sys.stdin.fileno()
            while not self._stop.is_set() and not self.result.done():
                try:
                    ready, _, _ = select.select([fd], [], [], 0.2)
                except (OSError, ValueError):
                    return
                if not ready:
                    continue
                line = sys.stdin.readline()
                if not line:
                    return
                try:
                    result = parse_redirect(line)
                except Exception as exc:
                    self._deliver(error=exc)
                    return
                if result:
                    self._deliver(result)
                    return
                if line.strip():
                    self.out("  that is not the redirect URL — paste the whole "
                             "address, starting http://localhost")

        threading.Thread(target=run, daemon=True).start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self.server:
            self.server.shutdown()
            self.server.server_close()


async def _login(spec: ServerSpec, store: StateStore, out: Any, open_browser: bool) -> int:
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    oauth = spec.oauth
    assert oauth is not None
    callback = _Callback(oauth.callback_port, out)

    async def show(url: str) -> None:
        listening = callback.start_listener()
        pasting = callback.start_paste()
        out(f"Open this URL to log in to {spec.name}:\n\n  {url}\n")
        if open_browser:
            with contextlib.suppress(Exception):
                webbrowser.open(url)
        if not listening:
            out(f"  (port {oauth.callback_port} is busy, so the redirect cannot be "
                "caught automatically)")
        if pasting:
            out("If the browser cannot reach this machine (container, SSH), it will "
                "end on a page that fails to load. Copy that page's full address "
                "and paste it here.")
        if not listening and not pasting:
            raise RuntimeError("no way to receive the login: the callback port is "
                               "busy and stdin is not a terminal")

    async def wait():
        try:
            return await asyncio.wrap_future(callback.result)
        finally:
            callback.stop()

    provider = build_provider(spec, store, redirect_handler=show, callback_handler=wait)
    headers = dict(spec.headers)
    async with httpx2.AsyncClient(headers=headers, auth=provider) as http:
        async with Client(streamable_http_client(spec.url, http_client=http)) as client:
            tools = await client.list_tools()
    out(f"logged in to {spec.name} — {len(tools.tools)} tools available")
    return 0


def login(spec: ServerSpec, store: StateStore, out: Any, open_browser: bool = True) -> int:
    """Interactive authorization-code login. Returns an exit code."""
    if not spec.remote:
        out(f"{spec.name} is a stdio server; it has no login")
        return 1
    if not spec.uses_oauth:
        out(f"{spec.name} uses static headers from mcp.json; there is nothing to log in to")
        return 1
    if spec.oauth and spec.oauth.grant != "authorization_code":
        out(f"{spec.name} uses {spec.oauth.grant}, which needs no login")
        return 1
    store.ensure()
    # A fresh login replaces whatever was there, including a stale registration
    # made with a different redirect port.
    store.update(spec.name, tokens=None, expires_at=None)
    return asyncio.run(_login(spec, store, out, open_browser))
