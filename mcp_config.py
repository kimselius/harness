"""mcp.json: which MCP servers to connect to, and how.

The format is Claude Code's `.mcp.json`, so a server's published setup snippet
can be pasted in as-is:

    {"mcpServers": {"name": {"command": "...", "args": [...], "env": {...}},
                    "other": {"type": "http", "url": "...", "headers": {...}}}}

`${VAR}` and `${VAR:-default}` are expanded from the environment, so secrets stay
in `.env` and the file itself can be committed. Every expanded value is
collected so it can be scrubbed from the session log.

A few keys are the harness's own and are ignored by other clients: `enabled`,
`optional`, `tools`, `disabledTools`, `timeout`, `sampling`, and the `oauth`
block's `grant`.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TRANSPORTS = ("stdio", "http", "sse")
GRANTS = ("authorization_code", "client_credentials", "private_key_jwt")
SAMPLING_POLICIES = ("ask", "allow", "deny")
# Fixed, so a client registered during one login is still valid at the next:
# dynamic registration records the exact redirect URI.
DEFAULT_CALLBACK_PORT = 33418

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,47}$")


class McpConfigError(Exception):
    """mcp.json is unusable. The message names the server and key."""


@dataclass
class OAuthSpec:
    grant: str = "authorization_code"
    scope: str | None = None
    callback_port: int = DEFAULT_CALLBACK_PORT
    # A pre-registered client, for servers without dynamic registration.
    client_id: str | None = None
    client_secret: str | None = field(default=None, repr=False)
    # client_credentials / private_key_jwt: the authorization server that issued
    # the credentials. The SDK refuses to send them anywhere else.
    issuer: str | None = None
    # private_key_jwt
    private_key_file: str | None = None
    key_id: str | None = None
    algorithm: str = "RS256"
    # A URL-based client id (client metadata document) instead of registration.
    client_metadata_url: str | None = None


@dataclass
class ServerSpec:
    name: str
    transport: str
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict, repr=False)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    oauth: OAuthSpec | None = None
    enabled: bool = True
    optional: bool = False
    tools: list[str] | None = None  # allowlist; None means all
    disabled_tools: list[str] = field(default_factory=list)
    timeout: float | None = None
    sampling: str = "ask"

    @property
    def remote(self) -> bool:
        return self.transport in ("http", "sse")

    @property
    def uses_oauth(self) -> bool:
        return self.remote and self.oauth is not None

    def describe(self) -> str:
        if self.remote:
            return f"{self.transport} {self.url}"
        return " ".join([self.command or "", *self.args]).strip()


@dataclass
class McpConfig:
    path: Path
    servers: dict[str, ServerSpec]
    # Every value that came out of an expansion or a credential field. These are
    # added to the redactor so they never reach a log or a result file.
    secrets: list[str] = field(default_factory=list, repr=False)

    @property
    def enabled(self) -> list[ServerSpec]:
        return [s for s in self.servers.values() if s.enabled]


def expand(value: str, where: str, secrets: list[str]) -> str:
    """Replace ${VAR} / ${VAR:-default}. A missing variable with no default is
    an error rather than an empty string: an empty token fails much later and
    much more confusingly."""

    def sub(match: re.Match) -> str:
        name, default = match.group(1), match.group(2)
        if name in os.environ:
            result = os.environ[name]
        elif default is not None:
            result = default
        else:
            raise McpConfigError(f"{where}: ${{{name}}} is not set")
        if result:
            secrets.append(result)
        return result

    return _VAR.sub(sub, value)


def _str(raw: dict, key: str, where: str, secrets: list[str]) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise McpConfigError(f"{where}.{key} must be a string")
    return expand(value, f"{where}.{key}", secrets)


def _str_list(raw: dict, key: str, where: str, secrets: list[str]) -> list[str] | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise McpConfigError(f"{where}.{key} must be a list of strings")
    return [expand(v, f"{where}.{key}", secrets) for v in value]


def _str_map(
    raw: dict, key: str, where: str, secrets: list[str], sensitive: bool = False
) -> dict[str, str]:
    value = raw.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise McpConfigError(f"{where}.{key} must be an object of strings")
    out = {}
    for k, v in value.items():
        expanded = expand(v, f"{where}.{key}.{k}", secrets)
        # A header value is a credential more often than not, even when written
        # literally. Env values are only treated as secret when they came from
        # an expansion (handled by expand()), or every path would be scrubbed.
        if sensitive and expanded:
            secrets.append(expanded)
        out[k] = expanded
    return out


def _oauth(raw: Any, where: str, secrets: list[str]) -> OAuthSpec | None:
    if raw is False:
        return None
    if raw is None or raw is True:
        return OAuthSpec()
    if not isinstance(raw, dict):
        raise McpConfigError(f"{where} must be an object, true or false")
    spec = OAuthSpec()
    grant = raw.get("grant", spec.grant)
    if grant not in GRANTS:
        raise McpConfigError(f"{where}.grant must be one of {', '.join(GRANTS)}")
    spec.grant = grant
    spec.scope = _str(raw, "scope", where, secrets)
    port = raw.get("callbackPort", spec.callback_port)
    if not isinstance(port, int) or not 0 < port < 65536:
        raise McpConfigError(f"{where}.callbackPort must be a port number")
    spec.callback_port = port
    spec.client_id = _str(raw, "clientId", where, secrets)
    spec.client_secret = _str(raw, "clientSecret", where, secrets)
    if spec.client_secret:
        secrets.append(spec.client_secret)
    spec.issuer = _str(raw, "issuer", where, secrets)
    spec.private_key_file = _str(raw, "privateKeyFile", where, secrets)
    spec.key_id = _str(raw, "keyId", where, secrets)
    spec.algorithm = _str(raw, "algorithm", where, secrets) or spec.algorithm
    spec.client_metadata_url = _str(raw, "clientMetadataUrl", where, secrets)

    if grant == "client_credentials" and not (spec.client_id and spec.client_secret):
        raise McpConfigError(f"{where}: client_credentials needs clientId and clientSecret")
    if grant == "private_key_jwt" and not (spec.client_id and spec.private_key_file):
        raise McpConfigError(f"{where}: private_key_jwt needs clientId and privateKeyFile")
    return spec


def parse_server(name: str, raw: Any, secrets: list[str]) -> ServerSpec:
    where = f"mcpServers.{name}"
    if not _NAME.match(name):
        raise McpConfigError(
            f"{where}: server names are letters, digits, '_', '-' and '.', "
            "starting with a letter or digit, at most 48 characters"
        )
    if not isinstance(raw, dict):
        raise McpConfigError(f"{where} must be an object")

    transport = raw.get("type")
    if transport is None:
        transport = "stdio" if "command" in raw else "http" if "url" in raw else None
    if transport == "streamable-http":
        transport = "http"
    if transport not in TRANSPORTS:
        raise McpConfigError(
            f"{where}: needs \"command\" (stdio) or \"url\" (http), or a \"type\" "
            f"of {', '.join(TRANSPORTS)}"
        )

    spec = ServerSpec(name=name, transport=transport)
    if transport == "stdio":
        spec.command = _str(raw, "command", where, secrets)
        if not spec.command:
            raise McpConfigError(f"{where}.command is required for a stdio server")
        spec.args = _str_list(raw, "args", where, secrets) or []
        spec.env = _str_map(raw, "env", where, secrets)
        spec.cwd = _str(raw, "cwd", where, secrets)
    else:
        spec.url = _str(raw, "url", where, secrets)
        if not spec.url or not spec.url.startswith(("http://", "https://")):
            raise McpConfigError(f"{where}.url must be an http(s) URL")
        spec.headers = _str_map(raw, "headers", where, secrets, sensitive=True)
        # OAuth is on unless a static Authorization header is given or it is
        # switched off: the SDK only starts a flow when the server answers 401,
        # so a server that needs no auth is unaffected.
        has_auth_header = any(k.lower() == "authorization" for k in spec.headers)
        default = False if has_auth_header else None
        spec.oauth = _oauth(raw.get("oauth", default), f"{where}.oauth", secrets)

    for key, attr in (("enabled", "enabled"), ("optional", "optional")):
        value = raw.get(key, getattr(spec, attr))
        if not isinstance(value, bool):
            raise McpConfigError(f"{where}.{key} must be true or false")
        setattr(spec, attr, value)
    spec.tools = _str_list(raw, "tools", where, secrets)
    spec.disabled_tools = _str_list(raw, "disabledTools", where, secrets) or []
    timeout = raw.get("timeout")
    if timeout is not None:
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise McpConfigError(f"{where}.timeout must be a positive number of seconds")
        spec.timeout = float(timeout)
    sampling = raw.get("sampling", spec.sampling)
    if sampling not in SAMPLING_POLICIES:
        raise McpConfigError(
            f"{where}.sampling must be one of {', '.join(SAMPLING_POLICIES)}"
        )
    spec.sampling = sampling
    return spec


def load(path: Path) -> McpConfig:
    """Read and validate mcp.json. A missing file is an empty config."""
    if not path.is_file():
        return McpConfig(path=path, servers={})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise McpConfigError(f"{path} is not valid JSON: {exc}") from None
    except OSError as exc:
        raise McpConfigError(f"cannot read {path}: {exc.strerror}") from None
    if not isinstance(data, dict):
        raise McpConfigError(f"{path}: the top level must be an object")
    servers_raw = data.get("mcpServers", {})
    if not isinstance(servers_raw, dict):
        raise McpConfigError(f"{path}: mcpServers must be an object")

    secrets: list[str] = []
    servers = {}
    for name, raw in servers_raw.items():
        try:
            servers[name] = parse_server(name, raw, secrets)
        except McpConfigError as exc:
            raise McpConfigError(f"{path}: {exc}") from None
    # Short values would scrub ordinary words out of the log; a real credential
    # is never this short.
    secrets = sorted({s for s in secrets if len(s) >= 8}, key=len, reverse=True)
    return McpConfig(path=path, servers=servers, secrets=secrets)
