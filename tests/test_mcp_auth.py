"""MCP login and token checks. Run: .venv/bin/python tests/test_mcp_auth.py

The fixture server sits behind the SDK's own authorization server, so these
run the real protocol: discovery, dynamic client registration, PKCE, the
loopback redirect, refresh with rotation. The "browser" is a script named in
$BROWSER that follows the redirects, which is all a person clicking "allow"
amounts to here.
"""

from __future__ import annotations

import json
import os
import pty
import re
import select
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import common  # noqa: E402
import mcp_fixture  # noqa: E402
import mock_server  # noqa: E402
import ptyrun  # noqa: E402
from common import check  # noqa: E402

import mcp_auth  # noqa: E402
import mcp_config  # noqa: E402

PY = sys.executable

BROWSER_SCRIPT = """\
import sys, urllib.request
urllib.request.urlopen(sys.argv[1], timeout=10).read()
"""


class Env:
    """One login scenario: a config file, a state dir, and the CLI to drive."""

    def __init__(self, tmp: Path, servers: dict) -> None:
        self.tmp = tmp
        tmp.mkdir(parents=True, exist_ok=True)
        self.config = tmp / "mcp.json"
        self.state = tmp / "state"
        self.write(servers)
        browser = tmp / "browser.py"
        browser.write_text(BROWSER_SCRIPT)
        self.env = dict(os.environ, OPENAI_API_KEY="sk-test-abcdefghijklmnopqrstu",
                        BROWSER=f"{PY} {browser} %s")
        self.env.pop("MCP_ENABLED", None)

    def write(self, servers: dict) -> None:
        self.config.write_text(json.dumps({"mcpServers": servers}))

    def args(self) -> list[str]:
        return [PY, str(ROOT / "main.py"), "--mcp-config", str(self.config),
                "--mcp-state-dir", str(self.state), "--workspace", str(self.tmp / "ws"),
                "--log-dir", str(self.tmp / "logs")]

    def cli(self, *extra: str, timeout: float = 60):
        return subprocess.run([*self.args(), *extra], capture_output=True, text=True,
                              env=self.env, timeout=timeout, stdin=subprocess.DEVNULL)

    def run(self, base_url: str, *extra: str):
        return self.cli("--mcp", "-p", "go", "--base-url", base_url, "--model", "mock",
                        "--result-file", str(self.tmp / "result.json"), *extra)

    def result(self) -> dict:
        return json.loads((self.tmp / "result.json").read_text())

    def stored(self, name: str) -> dict:
        try:
            return json.loads((self.state / f"{name}.json").read_text())
        except OSError:
            return {}

    def logs(self) -> str:
        return "".join(p.read_text() for p in (self.tmp / "logs").glob("*.jsonl"))


def test_login_flow(tmp: Path, base_url: str) -> None:
    print("authorization-code login, then unattended runs")
    auth = mcp_fixture.MemoryAuth(access_ttl=3600)
    url, stop = mcp_fixture.serve_http(auth)
    port = mcp_fixture.free_port()
    env = Env(tmp / "login", {"web": {"type": "http", "url": url,
                                      "oauth": {"callbackPort": port}}})
    try:
        mock_server.set_script([("should not run", [])])
        proc = env.run(base_url)
        check("-p before login: exit 1", proc.returncode == 1, proc.stderr[-300:])
        check("it says how to fix it", "--mcp-login web" in (env.result()["error"] or ""),
              env.result()["error"])
        check("model never called", not [r for r in mock_server.REQUESTS if r.get("messages")])

        proc = env.cli("--mcp-login", "web")
        check("login exit 0", proc.returncode == 0, proc.stderr[-600:])
        check("login reports the tools", "tools available" in proc.stderr,
              proc.stderr[-300:])
        check("authorization URL shown", "/authorize?" in proc.stderr)
        check("PKCE S256 used", "code_challenge_method=S256" in proc.stderr)
        check("resource indicator sent (RFC 8707)", "resource=" in proc.stderr)
        stored = env.stored("web")
        check("tokens stored", bool(stored.get("tokens", {}).get("access_token")))
        check("bound to the server URL", stored.get("url") == url)
        check("token endpoint remembered",
              "token_endpoint" in (stored.get("metadata") or {}).get("oauth", {}))
        check("file is 0600", (env.state / "web.json").stat().st_mode & 0o777 == 0o600)
        check("directory is 0700", env.state.stat().st_mode & 0o777 == 0o700)
        check("one registration", auth.registrations == 1)

        status = mcp_auth.status(mcp_auth.StateStore(env.state), spec_of(env, "web"))
        check("status says logged in", status.startswith("logged in") and
              "refreshable" in status, status)

        mock_server.set_script([("", [("mcp__web__echo", {"text": "authed"})]),
                                ("done", [])])
        proc = env.run(base_url)
        check("-p after login works", proc.returncode == 0 and proc.stdout.strip() == "done",
              proc.stderr[-400:])
        check("access token kept out of the log",
              stored["tokens"]["access_token"] not in env.logs())

        # Expire it, here and at the server: a fresh process has to refresh,
        # not ask for a new login. The SDK alone forgets the expiry and the
        # token endpoint across restarts; the state file brings both back.
        stored["expires_at"] = time.time() - 30
        (env.state / "web.json").write_text(json.dumps(stored))
        for token in auth.access.values():
            token.expires_at = int(time.time()) - 5
        old_refresh = stored["tokens"]["refresh_token"]
        mock_server.set_script([("", [("mcp__web__echo", {"text": "refreshed"})]),
                                ("done", [])])
        proc = env.run(base_url)
        renewed = env.stored("web")
        check("expired token: run still works", proc.returncode == 0, proc.stderr[-400:])
        check("it refreshed", auth.refreshes == 1, str(auth.refreshes))
        check("rotated refresh token saved",
              renewed["tokens"]["refresh_token"] != old_refresh)
        check("no second registration", auth.registrations == 1)
        check("new token also scrubbed",
              renewed["tokens"]["access_token"] not in env.logs())

        proc = env.cli("--mcp-list")
        check("--mcp-list connects", proc.returncode == 0 and
              re.search(r"status\s+\d+ tools", proc.stdout),
              proc.stdout[-400:] + proc.stderr[-300:])

        # Same name, different server: the stored tokens must not go there.
        other_url, other_stop = mcp_fixture.serve_http(mcp_fixture.MemoryAuth())
        try:
            env.write({"web": {"type": "http", "url": other_url,
                               "oauth": {"callbackPort": port}}})
            mock_server.set_script([("should not run", [])])
            proc = env.run(base_url)
            check("a changed URL needs a new login", proc.returncode == 1 and
                  "--mcp-login web" in (env.result()["error"] or ""), proc.stderr[-300:])
        finally:
            other_stop()
            env.write({"web": {"type": "http", "url": url, "oauth": {"callbackPort": port}}})

        proc = env.cli("--mcp-logout", "web")
        check("logout removes the file", proc.returncode == 0 and
              not (env.state / "web.json").exists())

        env.write({"web": {"type": "http", "url": url, "optional": True,
                           "oauth": {"callbackPort": port}}})
        mock_server.set_script([("ran without it", [])])
        proc = env.run(base_url)
        check("an optional server not logged in does not block",
              proc.returncode == 0 and proc.stdout.strip() == "ran without it",
              proc.stderr[-300:])
    finally:
        stop()


def test_paste_login(tmp: Path) -> None:
    """In a container the browser cannot reach the loopback listener; the user
    pastes the address the browser ended on instead."""
    print("login by pasting the redirect URL")
    auth = mcp_fixture.MemoryAuth()
    url, stop = mcp_fixture.serve_http(auth)
    # Hold the callback port, so the listener cannot start and paste is the
    # only way in — the same position as a container without -p.
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    env = Env(tmp / "paste", {"web": {"type": "http", "url": url,
                                      "oauth": {"callbackPort": port}}})
    master, slave = pty.openpty()
    proc = subprocess.Popen([*env.args(), "--mcp-login", "web", "--no-browser"],
                            stdin=slave, stdout=slave, stderr=slave,
                            preexec_fn=ptyrun._own_the_tty, env=env.env, close_fds=True)
    os.close(slave)
    screen = ""
    try:
        screen = _read_until(master, proc, r"(http://\S+/authorize\?\S+)", 20)
        match = re.search(r"(http://\S+/authorize\?\S+)", screen)
        check("URL printed", bool(match), screen[-300:])
        check("busy port reported", "is busy" in screen)
        check("asks for a paste", "paste it here" in screen)
        if match:
            location = _authorize_without_following(match.group(1))
            check("authorization redirected to the loopback",
                  location.startswith(f"http://localhost:{port}/callback?"), location)
            os.write(master, b"not a url\n")
            screen += _read_until(master, proc, r"not the redirect URL", 5)
            check("rejects something that is not the redirect", "not the redirect" in screen)
            os.write(master, location.encode() + b"\n")
            screen += _read_until(master, proc, r"logged in to web", 20)
        proc.wait(timeout=20)
        check("pasted login succeeds", proc.returncode == 0, screen[-400:])
        check("tokens stored", bool(env.stored("web").get("tokens")))
    finally:
        if proc.poll() is None:
            proc.kill()
        os.close(master)
        blocker.close()
        stop()


def test_client_credentials(tmp: Path, base_url: str) -> None:
    print("client_credentials: headless, separate issuer")
    issuer = mcp_fixture.ExternalIssuer("machine", "machine-secret-0123456789")
    issuer.start()
    url, stop = mcp_fixture.serve_http(issuer=issuer)
    env = Env(tmp / "cc", {"m2m": {"type": "http", "url": url, "oauth": {
        "grant": "client_credentials", "clientId": "machine",
        "clientSecret": "${MCPT_CC_SECRET}", "issuer": issuer.url}}})
    env.env["MCPT_CC_SECRET"] = "machine-secret-0123456789"
    try:
        proc = env.cli("--mcp-login", "m2m")
        check("login is refused: nothing to log in to", proc.returncode == 1 and
              "needs no login" in proc.stderr, proc.stderr[-300:])
        mock_server.set_script([("", [("mcp__m2m__echo", {"text": "machine"})]),
                                ("done", [])])
        proc = env.run(base_url)
        check("runs with no login at all", proc.returncode == 0 and
              proc.stdout.strip() == "done", proc.stderr[-500:])
        check("token obtained from the issuer", issuer.issued == 1, str(issuer.issued))
        check("client secret kept out of the log",
              "machine-secret-0123456789" not in env.logs())
        stored = env.stored("m2m")
        check("token cached", bool(stored.get("tokens")))
        check("issuer's token endpoint remembered",
              issuer.url in json.dumps(stored.get("metadata") or {}))

        mock_server.set_script([("", [("mcp__m2m__echo", {"text": "again"})]),
                                ("done", [])])
        proc = env.run(base_url)
        check("second run reuses the cached token", proc.returncode == 0 and
              issuer.issued == 1, f"issued={issuer.issued}")

        env.env["MCPT_CC_SECRET"] = "wrong-secret-0123456789"
        (env.state / "m2m.json").unlink()
        mock_server.set_script([("should not run", [])])
        proc = env.run(base_url)
        check("a wrong secret fails the run", proc.returncode == 1, proc.stderr[-300:])
    finally:
        stop()
        issuer.stop()


def test_storage_unit(tmp: Path) -> None:
    print("state store")
    store = mcp_auth.StateStore(tmp / "unit")
    store.update("a", url="https://x/", tokens={"access_token": "abcdefghijklmnop"})
    store.update("a", expires_at=123.0)
    data = store.load("a")
    check("updates merge", data["url"] == "https://x/" and data["expires_at"] == 123.0)
    store.update("a", expires_at=None)
    check("None deletes a key", "expires_at" not in store.load("a"))
    check("secrets collected", store.secrets("a") == ["abcdefghijklmnop"])
    check("no stray temp files", not list((tmp / "unit").glob("*.tmp")))
    check("delete", store.delete("a") and not store.delete("a"))
    result = mcp_auth.parse_redirect("http://localhost:1/callback?code=c1&state=s1&iss=i")
    check("redirect parsed", result and (result.code, result.state, result.iss)
          == ("c1", "s1", "i"))
    check("non-redirect ignored", mcp_auth.parse_redirect("hello") is None)
    try:
        mcp_auth.parse_redirect("http://localhost:1/callback?error=access_denied")
        check("refusal reported", False)
    except RuntimeError as exc:
        check("refusal reported", "access_denied" in str(exc))


# -- helpers -----------------------------------------------------------------------
def spec_of(env: Env, name: str):
    return mcp_config.load(env.config).servers[name]


def _read_until(fd: int, proc, pattern: str, timeout: float) -> str:
    out = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.1)
        if ready:
            try:
                chunk = os.read(fd, 65536).decode(errors="replace")
            except OSError:
                break
            out += chunk
            if re.search(pattern, out):
                break
        elif proc.poll() is not None:
            break
    return out


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _authorize_without_following(url: str) -> str:
    """What the browser's address bar shows: the redirect it could not load."""
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        opener.open(url, timeout=10)
    except urllib.error.HTTPError as exc:
        return exc.headers.get("Location", "")
    return ""


def main_() -> int:
    server, base_url = mock_server.start()
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        try:
            test_storage_unit(tmp)
            test_login_flow(tmp, base_url)
            test_paste_login(tmp)
            test_client_credentials(tmp, base_url)
        finally:
            server.shutdown()
    return common.report()


if __name__ == "__main__":
    sys.exit(main_())
