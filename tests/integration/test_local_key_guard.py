"""Every daemon route answers only local requests that carry the local key.

Builds the app with every optional route registered, guards it exactly as the
daemon does, and walks the whole route table: no key, a wrong key, and a
rebound Host are each refused on every route; the key on a local Host gets
through. Only the liveness probe is open. The runtime root that holds the
key, the conversations and the audio is closed to other accounts.
"""

from __future__ import annotations

import functools
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from jarvis.deployment import bootstrap_runtime
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app, require_local_key
from jarvis.surface.voice_controls import VoiceControls

KEYLESS = {"/api/health"}


async def _empty() -> dict[str, Any]:
    return {}


async def _set_todo(_todo_id: str, _done: bool) -> None:  # noqa: FBT001 — the deps signature
    return None


async def _archive_mail(_ids: list[str], _archive: bool) -> None:  # noqa: FBT001 — the deps signature
    return None


async def _empty_for(_message_id: str) -> dict[str, Any]:
    return {}


async def _save_draft(_message_id: str, _subject: str, _body: str) -> dict[str, Any]:
    return {}


async def _send_draft(_message_id: str, _subject: str, _body: str) -> None:
    return None


async def _discard_draft(_message_id: str) -> None:
    return None


async def _tap_mail(_ids: list[str], _do: bool) -> None:  # noqa: FBT001 — the deps signature
    return None


async def _save_settings(_changes: dict[str, Any]) -> dict[str, Any]:
    return {}


class _NullMemory:
    """ADR 0154's page with every answer empty: the walk only needs the routes to exist."""

    def __getattr__(self, _name: str) -> Any:  # noqa: ANN401 — any page method
        return lambda *_args: {}


def _client(
    tmp_path: Any,  # noqa: ANN401 — pytest tmp_path
    *,
    peer: str = "testclient",
    **guard: Any,  # noqa: ANN401 — require_local_key's keyword arguments
) -> tuple[TestClient, str, list[str]]:
    key = local_key(tmp_path)
    matches = functools.partial(local_key_matches, key)
    app = create_app(
        InherentDeps(
            submit_callable=lambda _text: "T1",
            broadcaster=InherentBroadcaster(),
            cancel_response_callable=lambda *_: "unknown_response",
            controls=VoiceControls(),
            usage_read=dict,
            usage_refresh=_empty,
            usage_codex_reset=lambda _request_id: {},
            usage_record_balance=lambda _provider, _usd: None,
            work_state_read=dict,
            work_state_refresh=_empty,
            projects_read=_empty,
            projects_refresh=_empty,
            conversation_read=lambda _after, _limit: {"since": None, "rows": []},
            plugin_read=dict,
            plugin_action=lambda _operation, _data: {},
            plugin_authorize=matches,
            plugin_icon=lambda _plugin_id: None,
            language_save=lambda _code: "zh",
            today_read=_empty,
            todo_set=_set_todo,
            mail_read=_empty,
            mail_archive=_archive_mail,
            mail_letter=_empty_for,
            mail_summary=_empty_for,
            mail_mark_read=_tap_mail,
            mail_trash=_tap_mail,
            view_set=lambda *_: None,
            mail_draft_read=_empty_for,
            mail_draft_save=_save_draft,
            mail_draft_send=_send_draft,
            mail_draft_discard=_discard_draft,
            memory_page=_NullMemory(),
            brief_read=dict,
            settings_read=_empty,
            settings_update=_save_settings,
            restart=lambda: None,
            agent_marks_path=tmp_path / "agent-marks.json",
        )
    )
    require_local_key(app, matches, **guard)
    routes = [
        f"{method} {route.path}"
        for route in app.routes
        if isinstance(route, Route)
        for method in sorted(route.methods or ())
    ] + [f"WS {route.path}" for route in app.routes if isinstance(route, WebSocketRoute)]
    return TestClient(app, base_url="http://127.0.0.1:8006", client=(peer, 50000)), key, routes


def _call(client: TestClient, route: str, headers: dict[str, str]) -> int:
    method, path = route.split(" ", 1)
    for name in (
        "{plugin_id}",
        "{request_id}",
        "{session_id}",
        "{message_id}",
        "{item_id}",
        "{day}",
    ):
        path = path.replace(name, "x")
    if method != "WS":
        return client.request(method, path, headers=headers).status_code
    try:
        with client.websocket_connect(f"ws://127.0.0.1:8006{path}", headers=headers):
            return 101
    except WebSocketDenialResponse as exc:  # an HTTP answer to the upgrade
        return exc.status_code
    except WebSocketDisconnect as exc:  # closed before accept
        return exc.code


def test_the_route_table_is_the_one_this_test_walks(tmp_path: Any) -> None:  # noqa: ANN401
    """Pin the count, so a route added later is walked, not silently skipped.

    64 HTTP method/path pairs, the four FastAPI docs pairs (GET and HEAD of
    ``/openapi.json``, ``/docs``, ``/docs/oauth2-redirect``, ``/redoc``) and
    the ``/inherent/ws`` socket.
    """
    _, _, routes = _client(tmp_path)
    assert len(routes) == 73, routes


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "wrong"}],
    ids=["no-key", "wrong-key", "not-bearer"],
)
def test_every_route_refuses_a_caller_without_the_key(
    tmp_path: Any,  # noqa: ANN401
    headers: dict[str, str],
) -> None:
    """401 on every HTTP route, a refused handshake on every socket; health stays open."""
    client, _, routes = _client(tmp_path)
    for route in routes:
        method, path = route.split(" ", 1)
        expected = 200 if path in KEYLESS else (1008 if method == "WS" else 401)
        assert _call(client, route, headers) == expected, route


def test_every_route_refuses_a_rebound_host_even_with_the_key(tmp_path: Any) -> None:  # noqa: ANN401
    """A page whose own domain resolves to 127.0.0.1 sends that domain as Host."""
    client, key, routes = _client(tmp_path)
    for host in ("attacker.example:8006", "attacker.example", "127.0.0.1.attacker.example"):
        headers = {"Authorization": f"Bearer {key}", "Host": host}
        for route in routes:
            assert _call(client, route, headers) == 400, (host, route)


def test_the_key_on_a_local_host_gets_through(tmp_path: Any) -> None:  # noqa: ANN401
    """The same routes answer the desktop, the CLI and the hooks, which carry the key."""
    client, key, routes = _client(tmp_path)
    auth = {"Authorization": f"Bearer {key}"}
    for route in routes:
        assert _call(client, route, auth) not in {401, 1008}, route
    assert client.get("/inherent/conversation", headers=auth).json() == {"since": None, "rows": []}
    local = {**auth, "Host": "localhost"}
    assert client.get("/inherent/conversation", headers=local).status_code == 200
    with client.websocket_connect("ws://127.0.0.1:8006/inherent/ws", headers=auth) as ws:
        ws.close()


def test_other_accounts_cannot_read_the_runtime_root(tmp_path: Any) -> None:  # noqa: ANN401
    """An existing world-readable root is closed at boot; the key file is owner-only."""
    root = tmp_path / "jarvis"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    paths = bootstrap_runtime(root)
    local_key(paths.root)
    assert paths.root.stat().st_mode & 0o777 == 0o700
    assert (paths.root / "plugin-access.json").stat().st_mode & 0o777 == 0o600
