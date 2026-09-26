"""ADR 0048 — the Usage page spends a Codex limit reset.

Each check asserts what a caller observes: the route's status and answer, or the
request the collector sent to chatgpt.com.
"""

from __future__ import annotations

import json
import urllib.error
from typing import TYPE_CHECKING, Any

from fastapi.testclient import TestClient

from jarvis.surface import usage_observer
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    import pytest

REQUEST_ID = "0199a3c2-1111-4222-8333-444455556666"
DESKTOP = {"Authorization": "Bearer desktop"}


def _app(redeem: Callable[[str], dict[str, Any]]) -> TestClient:
    return TestClient(
        create_app(
            InherentDeps(
                submit_callable=lambda _text: None,
                broadcaster=InherentBroadcaster(),
                plugin_authorize=lambda header: header == DESKTOP["Authorization"],
                usage_codex_reset=redeem,
            )
        )
    )


def test_reset_route_needs_the_desktop_credential_and_a_request_id() -> None:
    """No credential 401, no UUID 400, and neither reaches Codex; a valid call answers its code."""
    spent: list[str] = []

    def redeem(request_id: str) -> dict[str, Any]:
        spent.append(request_id)
        return {"code": "reset", "windows_reset": 2}

    with _app(redeem) as client:
        url = "/inherent/usage/codex/reset"
        assert client.post(url, json={"request_id": REQUEST_ID}).status_code == 401
        assert client.post(url, headers=DESKTOP, json={"request_id": "x"}).status_code == 400
        assert client.post(url, headers=DESKTOP, content=b"not json").status_code == 400
        assert spent == []
        answer = client.post(url, headers=DESKTOP, json={"request_id": REQUEST_ID})
        assert answer.status_code == 200
        assert answer.json() == {"code": "reset", "windows_reset": 2}
        assert spent == [REQUEST_ID]


def test_reset_route_reports_a_failed_call_as_502() -> None:
    """A network failure is a 502 with the reason, never a 200 the page would call a reset."""

    def redeem(_request_id: str) -> dict[str, Any]:
        reason = "timed out"
        raise urllib.error.URLError(reason)

    with _app(redeem) as client:
        answer = client.post(
            "/inherent/usage/codex/reset", headers=DESKTOP, json={"request_id": REQUEST_ID}
        )
        assert answer.status_code == 502
        assert "timed out" in answer.json()["detail"]


def test_redeem_sends_codexs_own_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """POST to Codex's consume route with the id as its idempotency key and the Codex login."""
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "tok", "account_id": "acct"}})
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    sent: list[tuple[str, dict[str, str], Mapping[str, Any]]] = []

    def answer(
        url: str, headers: Mapping[str, str], body: Mapping[str, Any], *, timeout_s: float
    ) -> dict[str, Any]:
        del timeout_s
        sent.append((url, dict(headers), body))
        return {"code": "nothing_to_reset"}

    monkeypatch.setattr(usage_observer, "_post_json", answer)
    assert usage_observer.redeem_codex_reset(REQUEST_ID) == {
        "code": "nothing_to_reset",
        "windows_reset": 0,
    }
    url, headers, body = sent[0]
    assert url == "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume"
    assert body == {"redeem_request_id": REQUEST_ID}
    assert headers["Authorization"] == "Bearer tok"
    assert headers["ChatGPT-Account-Id"] == "acct"
