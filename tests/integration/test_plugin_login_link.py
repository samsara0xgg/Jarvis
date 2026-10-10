"""ADR 0202: the login link is part of the request and the brain opens no browser.

Real plugin connections against the local OAuth test server, no outside network. What is asserted
is what a paired device would read.
"""

from __future__ import annotations

import json
import urllib.request
import webbrowser
from typing import TYPE_CHECKING

from tests.integration.test_mcp_tools import (
    oauth_url as oauth_url,  # noqa: PLC0414 — re-export fixture.
)
from tests.integration.test_plugin_connections import (
    _open,
    _package,
    _service,
    _wait,
)
from tests.integration.test_plugin_connections import (
    fixture as fixture,  # noqa: PLC0414 — re-export fixture.
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from tests.integration.test_flat_tool_dispatch import _Fixture


def test_the_login_link_is_in_the_request_only_while_it_waits_and_the_brain_opens_nothing(
    tmp_path: Path, fixture: _Fixture, oauth_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A paired device reads the link; no browser opens here; no token or credential is read."""
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url, *_a, **_k: opened.append(url))
    _package(tmp_path, "oauth", {"oauth": {"url": oauth_url, "auth": "oauth"}})
    service = _service(tmp_path, fixture, open_url=None)
    try:
        request_id = _open(service, "oauth")
        assert "auth_url" not in service.read()["request"]
        service.action("connect", {"request_id": request_id})
        waiting = _wait(service, "authorizing")
        link = waiting["auth_url"]
        assert "/authorize" in link
        assert service.action("reopen", {"request_id": request_id})["request"]["auth_url"] == link
        assert opened == []
        with urllib.request.urlopen(link, timeout=8) as response:  # noqa: S310 — local test authorization server
            assert response.status == 200
        done = _wait(service, "ready")
        assert "auth_url" not in done
        shown = json.dumps(service.read())
        assert "access_token" not in shown
        assert "refresh_token" not in shown
        assert "code_challenge" not in json.dumps(done)
    finally:
        service.stop()


def test_a_cancelled_login_takes_the_link_away(
    tmp_path: Path, fixture: _Fixture, oauth_url: str
) -> None:
    """After cancel there is no link to open."""
    _package(tmp_path, "oauth", {"oauth": {"url": oauth_url, "auth": "oauth"}})
    service = _service(tmp_path, fixture, open_url=None)
    try:
        request_id = _open(service, "oauth")
        service.action("connect", {"request_id": request_id})
        _wait(service, "authorizing")
        service.action("cancel", {"request_id": request_id})
        assert "auth_url" not in service.read()["request"]
    finally:
        service.stop()
