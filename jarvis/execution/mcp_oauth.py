"""L4 — OAuth login state for remote MCP servers (ADR 0032).

The SDK's ``OAuthClientProvider`` speaks the protocol (discovery, dynamic
client registration, PKCE, refresh). This module gives it what a daemon
needs around that: a 0600 token file per server, a loopback listener for the
browser's redirect, an absolute expiry (the SDK reloads tokens without one,
so a restarted process would present a stale token as valid), and a redirect
handler that refuses to open a browser unless the login command is running.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from concurrent.futures import Future
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import parse_qs, urlparse

from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

DEFAULT_OAUTH_CALLBACK_PORT: Final[int] = 8789
"""``tools.mcp.oauth_callback_port`` when unset; the registered redirect URI carries it."""

LOGIN_HINT: Final = "run `python -m jarvis mcp-login {server}` in a terminal"


class FileTokenStorage:
    """One 0600 JSON file per server: tokens, client registration, absolute expiry."""

    def __init__(self, path: Path) -> None:
        """Bind to ``path``; nothing is read or created until the SDK asks."""
        self.path = path

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, **fields: object) -> None:
        data = {**self._read(), **fields}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        tmp.replace(self.path)

    def expires_at(self) -> float | None:
        """When the stored access token dies, or None when the server never said."""
        value = self._read().get("expires_at")
        return float(value) if isinstance(value, int | float) else None

    def has_tokens(self) -> bool:
        """Whether a login ever completed; a stored registration alone is not one."""
        return bool(self._read().get("tokens"))

    async def get_tokens(self) -> OAuthToken | None:
        """The stored tokens, if a login ever completed."""
        raw = self._read().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        """Persist a fresh token set with the wall-clock instant it expires."""
        expires_at = time.time() + tokens.expires_in if tokens.expires_in else None
        self._write(tokens=tokens.model_dump(mode="json", exclude_none=True), expires_at=expires_at)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        """The dynamic client registration, if one was made."""
        raw = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        """Persist the registration the authorization server issued."""
        self._write(client_info=client_info.model_dump(mode="json", exclude_none=True))


class _Listener(ThreadingHTTPServer):
    """The loopback server the browser is redirected back to; one result per login."""

    def __init__(self, port: int) -> None:
        super().__init__(("127.0.0.1", port), _CallbackHandler)
        self.result: Future[AuthorizationCodeResult] = Future()


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        listener = self.server
        if parsed.path != "/callback" or not isinstance(listener, _Listener):
            self.send_error(404)
            return
        query = {k: v[0] for k, v in parse_qs(parsed.query).items() if v}
        code = query.get("code")
        if not listener.result.done():
            if code is None:
                error = query.get("error", "no code in the callback")
                msg = f"{error}: {query.get('error_description', '')}".strip(": ")
                listener.result.set_exception(RuntimeError(msg))
            else:
                listener.result.set_result(
                    AuthorizationCodeResult(
                        code=code, state=query.get("state"), iss=query.get("iss")
                    )
                )
        body = b"Jarvis: login received, you can close this tab."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — the base class names it so.
        """Stay quiet; the login command narrates instead."""


class BrowserLogin:
    """The interactive half: bind the listener, open the browser, wait for the code."""

    def __init__(self, port: int, open_url: Callable[[str], object]) -> None:
        """``open_url`` gets the authorization URL once the listener is bound."""
        self._port = port
        self._open_url = open_url
        self._listener: _Listener | None = None

    async def redirect(self, url: str) -> None:
        """Bind the loopback listener first, so the redirect always has somewhere to land."""
        self._listener = _Listener(self._port)
        threading.Thread(
            target=self._listener.serve_forever, kwargs={"poll_interval": 0.25}, daemon=True
        ).start()
        sys.stderr.write(f"Open this URL to log in (a browser should open by itself):\n{url}\n")
        self._open_url(url)

    async def callback(self) -> AuthorizationCodeResult:
        """Wait for the browser to come back, then release the port."""
        if self._listener is None:
            msg = "callback before redirect"
            raise RuntimeError(msg)
        try:
            return await asyncio.wrap_future(self._listener.result)
        finally:
            self._listener.shutdown()
            self._listener.server_close()


def build_oauth(
    server: str,
    url: str,
    token_path: Path,
    *,
    callback_port: int,
    open_url: Callable[[str], object] | None,
) -> OAuthClientProvider:
    """The SDK provider for one server; without ``open_url`` it may only reuse a stored login."""
    storage = FileTokenStorage(token_path)
    metadata = OAuthClientMetadata.model_validate(
        {"client_name": "Jarvis", "redirect_uris": [f"http://127.0.0.1:{callback_port}/callback"]}
    )
    if open_url is None:

        async def refuse(_url: str) -> None:
            msg = f"{server}: not logged in; {LOGIN_HINT.format(server=server)}"
            raise RuntimeError(msg)

        async def never() -> AuthorizationCodeResult:
            msg = f"{server}: no browser in the daemon; {LOGIN_HINT.format(server=server)}"
            raise RuntimeError(msg)

        provider = OAuthClientProvider(
            server_url=url,
            client_metadata=metadata,
            storage=storage,
            redirect_handler=refuse,
            callback_handler=never,
        )
    else:
        login = BrowserLogin(callback_port, open_url)
        provider = OAuthClientProvider(
            server_url=url,
            client_metadata=metadata,
            storage=storage,
            redirect_handler=login.redirect,
            callback_handler=login.callback,
        )
    # The SDK reloads tokens without their expiry; seed it so a restart refreshes
    # instead of presenting a stale token and re-authorizing on the 401.
    expires_at = storage.expires_at()
    if expires_at is not None:
        provider.context.token_expiry_time = expires_at
    return provider
