"""Acceptance for the authenticated ``/inherent/ws/v2`` hello (ADR-0014 D5-D7).

The app is built in-process with :class:`fastapi.testclient.TestClient` over a
real Event Log, because two of the values the handshake promises — the
``log_epoch`` and the ``server_high_water_cursor`` — are only meaningful when
they come from an actual database rather than a stub.

Everything else is injected: the token comes from ``rotate_inherent_v2_token``
so the header the client sends is the one L6 would have written, and the
capability set is a fixture so a change in the daemon's voice wiring cannot
silently rewrite what these tests assert.
"""

from __future__ import annotations

import functools
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jarvis.deployment import inherent_v2_token_matches, rotate_inherent_v2_token
from jarvis.shared.realtime import new_connection_id
from jarvis.state.event_log import emit_event, open_event_log, read_log_epoch
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_protocol import (
    HELLO_TIMEOUT_S,
    INITIAL_MAX_FRAMES_PER_S,
    MAX_CLIENT_FRAME_BYTES,
    RuntimeCapabilities,
)
from jarvis.surface.inherent_server import InherentDeps, InherentV2Deps, create_app

if TYPE_CHECKING:
    from starlette.testclient import WebSocketTestSession

_V2_PATH = "/inherent/ws/v2"
_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "inherent_v2"
_BOOT_ID = "Btest00000000000000000000000001"

# The daemon computes these from live wiring (``_v2_runtime_capabilities``);
# the tests pin a distinct set so an accidental hard-coding in the surface
# layer would show up as a mismatch rather than a coincidence.
_CAPABILITIES = RuntimeCapabilities(
    text_input=True,
    image_input=False,
    voice_input=True,
    response_interrupt=True,
    action_cancel=False,
    confirmation_actions=False,
    natural_barge_in=False,
    aec_profile="headphones_only",
)


class _Rig(NamedTuple):
    """One built app together with the facts its tests assert against."""

    client: TestClient
    token: str
    conn: sqlite3.Connection


def _noop_submit(text: str) -> None:
    """Stand in for the v1 text submit callable; never invoked here."""
    _ = text


def _max_event_id(conn: sqlite3.Connection) -> int:
    """Return ``MAX(events.id)``, the cursor the hello advertises."""
    row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()
    return int(row[0])


def _client_hello() -> dict[str, Any]:
    """Load the golden ``client.hello`` fixture as a fresh mutable dict."""
    frame: dict[str, Any] = json.loads((_FIXTURES / "client.hello.json").read_text())
    return frame


def _build_rig(
    tmp_path: Path,
    *,
    hello_timeout_s: float = HELLO_TIMEOUT_S,
    max_frames_per_s: int = INITIAL_MAX_FRAMES_PER_S,
) -> _Rig:
    """Bootstrap a real Event Log, rotate a token, and wire the v2 app.

    Two events are emitted so ``MAX(events.id)`` is non-zero — a hello that
    advertised 0 against an empty log would pass a weaker assertion than the
    one this card owes.

    The high-water reader gets its own ``check_same_thread=False`` connection:
    ``TestClient`` runs the app on a portal thread, while the daemon calls the
    same query from the loop thread that opened its connection.
    """
    db_path = tmp_path / "mac_events.db"
    conn = open_event_log(db_path)
    for turn in ("t1", "t2"):
        emit_event(
            conn,
            type="utterance.received",
            payload={"turn_id": turn, "transcript": "hello " + turn, "channel": "voice"},
        )
    reader = sqlite3.connect(db_path, check_same_thread=False)
    token = rotate_inherent_v2_token(tmp_path / "inherent-v2.token")
    deps = InherentDeps(
        submit_callable=_noop_submit,
        broadcaster=InherentBroadcaster(),
        v2=InherentV2Deps(
            token_matches=functools.partial(inherent_v2_token_matches, token),
            mint_connection_id=new_connection_id,
            boot_id=_BOOT_ID,
            log_epoch=read_log_epoch(conn),
            high_water_cursor=functools.partial(_max_event_id, reader),
            runtime_capabilities=lambda: _CAPABILITIES,
            hello_timeout_s=hello_timeout_s,
            max_frames_per_s=max_frames_per_s,
        ),
    )
    return _Rig(client=TestClient(create_app(deps)), token=token, conn=conn)


def _auth(token: str) -> dict[str, str]:
    """Build the D5 upgrade header."""
    return {"Authorization": f"Bearer {token}"}


def _expect_close(session: WebSocketTestSession, code: int, reason: str) -> None:
    """Assert the next server message is exactly this close frame."""
    message = session.receive()
    assert message["type"] == "websocket.close", message
    assert message["code"] == code, message
    assert message["reason"] == reason, message


def _noop_envelope(connection_id: str, message_id: str) -> dict[str, Any]:
    """A valid post-hello client frame this card deliberately ignores."""
    return {
        "protocol_version": 2,
        "message_type": "client.noop",
        "message_id": message_id,
        "client_instance_id": "Ifixture0001",
        "connection_id": connection_id,
        "sent_at_ms": 1788200000000,
        "payload": {},
    }


def _complete_hello(session: WebSocketTestSession) -> dict[str, Any]:
    """Send the golden hello and return the decoded ``server.hello``."""
    session.send_json(_client_hello())
    frame: dict[str, Any] = session.receive_json()
    return frame


def test_upgrade_without_a_token_is_refused_before_accept(tmp_path: Path) -> None:
    """No ``Authorization`` header: the upgrade is denied, nothing is served."""
    rig = _build_rig(tmp_path)

    with pytest.raises(WebSocketDisconnect) as caught, rig.client.websocket_connect(_V2_PATH):
        pass  # pragma: no cover - the connect above never yields a session

    assert caught.value.code == 1008


def test_upgrade_with_a_wrong_token_is_refused(tmp_path: Path) -> None:
    """A well-formed Bearer header carrying the wrong secret is denied too."""
    rig = _build_rig(tmp_path)
    wrong = "0" * len(rig.token)
    assert wrong != rig.token

    with (
        pytest.raises(WebSocketDisconnect) as caught,
        rig.client.websocket_connect(_V2_PATH, headers=_auth(wrong)),
    ):
        pass  # pragma: no cover - the connect above never yields a session

    assert caught.value.code == 1008


def test_correct_token_and_golden_hello_yield_the_d7_server_hello(tmp_path: Path) -> None:
    """Every field of the answer matches the D7 contract and the live log."""
    rig = _build_rig(tmp_path)

    with rig.client.websocket_connect(_V2_PATH, headers=_auth(rig.token)) as session:
        frame = _complete_hello(session)

    assert frame["protocol_version"] == 2
    assert frame["message_type"] == "server.hello"
    assert frame["delivery_class"] == "protocol"
    assert frame["message_id"] == "Rfixture0001"
    assert frame["connection_id"].startswith("C")
    assert frame["log_epoch"] == read_log_epoch(rig.conn)
    assert frame["boot_id"] == _BOOT_ID
    assert frame["event_cursor"] is None
    assert frame["ephemeral_sequence"] is None
    assert frame["sent_at_ms"] > 0
    assert frame["payload"] == {
        "selected_version": 2,
        "view_schema_version": 1,
        "resume_mode": "snapshot",
        "server_high_water_cursor": _max_event_id(rig.conn),
        "required_client_capabilities": ["paged_snapshot", "transport_ack"],
        "runtime_capabilities": _CAPABILITIES.model_dump(),
    }
    assert _max_event_id(rig.conn) > 0


def test_hello_after_the_deadline_closes_with_hello_timeout(tmp_path: Path) -> None:
    """A client that dawdles past ``hello_timeout_s`` loses the socket."""
    rig = _build_rig(tmp_path, hello_timeout_s=0.2)

    with rig.client.websocket_connect(_V2_PATH, headers=_auth(rig.token)) as session:
        time.sleep(0.5)
        session.send_json(_client_hello())
        _expect_close(session, 1008, "hello_timeout")


def test_oversized_text_frame_closes_with_frame_too_large(tmp_path: Path) -> None:
    """64 KiB + 1 bytes is refused on size, before any decode is attempted."""
    rig = _build_rig(tmp_path)

    with rig.client.websocket_connect(_V2_PATH, headers=_auth(rig.token)) as session:
        session.send_text("a" * (MAX_CLIENT_FRAME_BYTES + 1))
        _expect_close(session, 1009, "frame_too_large")


def test_malformed_hello_closes_with_protocol_error(tmp_path: Path) -> None:
    """A hello missing a required identity never becomes a handshake."""
    rig = _build_rig(tmp_path)
    malformed = _client_hello()
    del malformed["message_id"]

    with rig.client.websocket_connect(_V2_PATH, headers=_auth(rig.token)) as session:
        session.send_json(malformed)
        _expect_close(session, 1002, "protocol_error")


def test_unsupported_version_closes_with_upgrade_required(tmp_path: Path) -> None:
    """A client that will only accept v3 back is told to upgrade, not rejected."""
    rig = _build_rig(tmp_path)
    hello = _client_hello()
    hello["payload"] = {**hello["payload"], "supported_versions": [3]}

    with rig.client.websocket_connect(_V2_PATH, headers=_auth(rig.token)) as session:
        session.send_json(hello)
        _expect_close(session, 1008, "upgrade_required")


def test_second_hello_after_a_successful_one_closes_with_protocol_error(tmp_path: Path) -> None:
    """The handshake happens once per socket; a repeat is a protocol fault."""
    rig = _build_rig(tmp_path)

    with rig.client.websocket_connect(_V2_PATH, headers=_auth(rig.token)) as session:
        _complete_hello(session)
        session.send_json(_client_hello())
        _expect_close(session, 1002, "protocol_error")


def test_frame_burst_over_the_budget_closes_with_protocol_error(tmp_path: Path) -> None:
    """``max_frames_per_s`` + 1 valid frames in one window breach the limit."""
    rig = _build_rig(tmp_path, max_frames_per_s=3)

    with rig.client.websocket_connect(_V2_PATH, headers=_auth(rig.token)) as session:
        connection_id = _complete_hello(session)["connection_id"]
        for index in range(4):
            session.send_json(_noop_envelope(connection_id, f"Rnoop{index}"))
        _expect_close(session, 1002, "protocol_error")


def test_frame_burst_within_the_budget_keeps_the_socket_open(tmp_path: Path) -> None:
    """Exactly ``max_frames_per_s`` frames are admitted.

    The oversized frame at the end is the probe: reaching a
    ``frame_too_large`` close proves the three before it were admitted, since
    a breach would have closed with ``protocol_error`` first.
    """
    rig = _build_rig(tmp_path, max_frames_per_s=3)

    with rig.client.websocket_connect(_V2_PATH, headers=_auth(rig.token)) as session:
        connection_id = _complete_hello(session)["connection_id"]
        for index in range(3):
            session.send_json(_noop_envelope(connection_id, f"Rnoop{index}"))
        session.send_text("a" * (MAX_CLIENT_FRAME_BYTES + 1))
        _expect_close(session, 1009, "frame_too_large")


def test_nothing_is_sent_after_the_hello_and_the_socket_stays_open(tmp_path: Path) -> None:
    """This card's socket says one thing and then listens.

    After the handshake the server must send no snapshot, no delta, and no
    keep-alive. The probe close is provoked deliberately: because it is the
    very next message the client sees, nothing was sent in between.
    """
    rig = _build_rig(tmp_path)

    with rig.client.websocket_connect(_V2_PATH, headers=_auth(rig.token)) as session:
        connection_id = _complete_hello(session)["connection_id"]
        session.send_json(_noop_envelope(connection_id, "Rnoop0"))
        time.sleep(0.3)
        session.send_text("a" * (MAX_CLIENT_FRAME_BYTES + 1))
        _expect_close(session, 1009, "frame_too_large")


def test_app_without_v2_deps_registers_no_v2_route() -> None:
    """A v1-only deployment's route table is exactly what it was."""
    app = create_app(InherentDeps(submit_callable=_noop_submit, broadcaster=InherentBroadcaster()))

    paths = {getattr(route, "path", "") for route in app.routes}

    assert _V2_PATH not in paths
    assert "/inherent/ws" in paths


def test_the_token_never_reaches_a_log_record(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Neither a refused nor an accepted connection may log the secret."""
    rig = _build_rig(tmp_path)

    with caplog.at_level(logging.DEBUG):
        with (
            pytest.raises(WebSocketDisconnect),
            rig.client.websocket_connect(_V2_PATH, headers=_auth("wrong-token")),
        ):
            pass  # pragma: no cover - the connect above never yields a session
        with rig.client.websocket_connect(_V2_PATH, headers=_auth(rig.token)) as session:
            _complete_hello(session)

    assert rig.token not in caplog.text
    assert not [record for record in caplog.records if rig.token in str(record.args)]
