"""An app whose routes are all stubbed, to test the local-key guard on each."""

from __future__ import annotations

import functools
from typing import Any

from fastapi.testclient import TestClient
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from jarvis.state.attachments import Attachments
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import (
    InherentDeps,
    create_app,
    manager_authorize,
    require_local_key,
)
from jarvis.surface.voice_controls import VoiceControls

KEYLESS = {"/api/health"}


async def _empty() -> dict[str, Any]:
    return {}


async def _empty_day(_day: object) -> dict[str, Any]:
    return {}


async def _no_highlights(
    _day: object,
    _item_id: str,
    _on: bool,  # noqa: FBT001 — the deps signature
    _label: str | None,
) -> list[dict[str, str]]:
    return []


async def _no_commitment(_commitment_hex: str, _action: str) -> bool:
    return True


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


def guard_client(
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
            day_read=_empty_day,
            highlight_set=_no_highlights,
            commitment_act=_no_commitment,
            projects_read=_empty,
            projects_refresh=_empty,
            conversation_read=lambda _after, _limit, _before: {"since": None, "rows": []},
            ask_outcome=lambda _turn_id: None,
            plugin_read=dict,
            plugin_action=lambda _operation, _data: {},
            plugin_authorize=manager_authorize(matches, guard.get("device_token_matches")),
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
            attachments=Attachments(tmp_path / "attachments"),
            submit_attachments=lambda _text, _ids: "T1",
            share_callable=lambda _fields: "S1",
            reaction_set=lambda _target, _emoji: True,
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


def status_of(client: TestClient, route: str, headers: dict[str, str]) -> int:
    method, path = route.split(" ", 1)
    for name in (
        "{plugin_id}",
        "{request_id}",
        "{session_id}",
        "{message_id}",
        "{item_id}",
        "{day}",
        "{commitment_hex}",
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
