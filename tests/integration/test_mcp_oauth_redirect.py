"""ADR 0202: ``tools.mcp.oauth_redirect_uri`` is the redirect a login registers and sends.

A real OAuth server and a real login: the authorization URL carries the configured redirect
(or the loopback one when unset), the registration on file names it, and the listener answers on
loopback ``/callback`` whatever public path forwards to it. A registration made for another
redirect is dropped at the next login that may happen and kept in the daemon, where it is what
refreshing a token needs; the tokens survive either way.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from jarvis.execution.mcp_oauth import FileTokenStorage, build_oauth
from jarvis.execution.mcp_tools import McpServers
from tests.integration.test_mcp_tools import (
    _free_port,
)
from tests.integration.test_mcp_tools import (
    oauth_url as oauth_url,  # noqa: PLC0414 — re-export fixture.
)

if TYPE_CHECKING:
    from pathlib import Path

PUBLIC = "https://brain.example.ts.net/oauth/callback"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


def _approve_on_this_device(url: str, port: int) -> None:
    """What the browser and `tailscale serve` do: take the redirect, deliver it to the listener."""
    try:
        urllib.request.build_opener(_NoRedirect).open(url, timeout=10)
    except urllib.error.HTTPError as redirected:
        back = urlparse(redirected.headers["Location"])
        urllib.request.urlopen(
            f"http://127.0.0.1:{port}/callback?{back.query}", timeout=10
        ).read()


@pytest.mark.parametrize("configured", [PUBLIC, None])
def test_the_registered_and_requested_redirect_is_the_configured_one(
    tmp_path: Path, oauth_url: str, configured: str | None
) -> None:
    """Configured, the public URI; unset, the loopback URI exactly as before."""
    port = _free_port()
    expected = configured or f"http://127.0.0.1:{port}/callback"
    opened: list[str] = []

    def browser(url: str) -> None:
        opened.append(url)
        threading.Thread(target=_approve_on_this_device, args=(url, port), daemon=True).start()

    login = McpServers(
        timeout_s=20,
        token_dir=tmp_path / "mcp",
        callback_port=port,
        redirect_uri=configured,
        open_url=browser,
    )
    try:
        assert login.connect({"svc": {"url": oauth_url, "auth": "oauth"}})
    finally:
        login.stop()
    assert parse_qs(urlparse(opened[0]).query)["redirect_uri"] == [expected]
    stored = json.loads((tmp_path / "mcp" / "svc.json").read_text())
    assert [str(u) for u in stored["client_info"]["redirect_uris"]] == [expected]
    assert stored["tokens"]["access_token"]


def _logged_in(path: Path, redirect: str) -> FileTokenStorage:
    storage = FileTokenStorage(path)
    asyncio.run(storage.set_tokens(OAuthToken(access_token="at", expires_in=3600)))  # noqa: S106
    asyncio.run(
        storage.set_client_info(
            OAuthClientInformationFull(client_id="old", redirect_uris=[redirect])  # type: ignore[list-item]
        )
    )
    return storage


def test_a_registration_for_another_redirect_is_dropped_only_where_a_login_may_happen(
    tmp_path: Path,
) -> None:
    """Tokens stay; the daemon keeps the registration so refreshing still has its client id."""
    path = tmp_path / "svc.json"
    old = "http://127.0.0.1:8789/callback"
    storage = _logged_in(path, old)

    def build(*, interactive: bool, redirect: str | None) -> None:
        build_oauth(
            "svc",
            "http://127.0.0.1:1/mcp",
            path,
            callback_port=8789,
            open_url=(lambda _url: None) if interactive else None,
            redirect_uri=redirect,
        )

    def on_file() -> dict[str, object]:
        data: dict[str, object] = json.loads(path.read_text())
        return data

    build(interactive=False, redirect=PUBLIC)
    assert on_file()["client_info"]
    build(interactive=True, redirect=None)  # the same redirect it was registered for
    assert on_file()["client_info"]
    build(interactive=True, redirect=PUBLIC)
    assert not on_file()["client_info"]
    assert asyncio.run(storage.get_client_info()) is None
    assert asyncio.run(storage.get_tokens()) is not None
    assert time.time() < (storage.expires_at() or 0)
