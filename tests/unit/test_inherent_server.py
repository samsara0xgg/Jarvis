"""Unit tests for ``jarvis.surface.inherent_server.create_app`` — ADR-0003.

LLM-free; uses FastAPI's ``TestClient`` (and its embedded
``WebSocketTestSession``) to exercise every endpoint. The async
``InherentBroadcaster.broadcast_*`` calls for the WS round-trip tests
are dispatched onto the server's event loop via the WS session's
blocking portal so the registered WS client actually receives the
envelopes.

Step 2 wire schema: the broadcaster exposes three per-event-type
methods (``broadcast_open`` / ``broadcast_chunk`` / ``broadcast_done``).
The round-trip tests exercise the ``done`` envelope (``fadeMs:5000``)
because it has no payload-read guard, which keeps the test fixtures
minimal while still proving the WS endpoint is wired to the registry.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from jarvis.shared import Event
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import (
    InherentDeps,
    SubmitRequest,
    create_app,
)

# ---------------------------------------------------------------------------
# Test doubles + fixtures
# ---------------------------------------------------------------------------


@dataclass
class _SubmitSpy:
    """Captures every text submitted via /inherent/submit.

    Stand-in for the runtime's ``emit_surface_user_intent(...)`` binding
    that Step 8's ``serve_inherent`` injects. ``__call__`` is sync to
    match the production callable shape — the FastAPI handler offloads
    it via ``asyncio.to_thread`` so the loop stays unblocked.
    """

    received: list[str] = field(default_factory=list)

    def __call__(self, text: str) -> None:
        self.received.append(text)


@pytest.fixture
def spy_and_broadcaster() -> tuple[_SubmitSpy, InherentBroadcaster]:
    """Fresh spy + broadcaster per test (no state leak between tests)."""
    return _SubmitSpy(), InherentBroadcaster()


@pytest.fixture
def client(spy_and_broadcaster: tuple[_SubmitSpy, InherentBroadcaster]) -> TestClient:
    """A FastAPI ``TestClient`` wrapping ``create_app(deps)``."""
    spy, broadcaster = spy_and_broadcaster
    deps = InherentDeps(submit_callable=spy, broadcaster=broadcaster)
    return TestClient(create_app(deps))


def _make_event(*, text: str = "hi", turn_id: str = "T-test-001") -> Event:
    """Build a minimal ``surface.response_emitted``-shaped Event.

    Used by ``broadcast_done`` calls in the round-trip tests below.
    ``broadcast_done`` ignores the payload, so the fields are stub
    values; only the event's existence matters.
    """
    return Event(
        event_uid="evt-test-1",
        type="surface.response_emitted",
        schema_version=1,
        ts_epoch_ms=1_700_000_000_000,
        payload={"turn_id": turn_id, "text": text},
        source_event_id=None,
        correlation={"turn_id": turn_id},
    )


# ---------------------------------------------------------------------------
# /api/health
# ---------------------------------------------------------------------------


def test_health_endpoint_returns_ok(client: TestClient) -> None:
    """GET /api/health -> 200 + {'status': 'ok'} — used by ops liveness probes."""
    resp = client.get("/api/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# /inherent/submit — happy paths
# ---------------------------------------------------------------------------


def test_submit_with_text_returns_accepted(
    client: TestClient,
    spy_and_broadcaster: tuple[_SubmitSpy, InherentBroadcaster],
) -> None:
    """POST {'text': 'hi'} -> 200 + {'status': 'accepted'}; spy sees ['hi']."""
    spy, _ = spy_and_broadcaster

    resp = client.post("/inherent/submit", json={"text": "hi"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "accepted"}
    assert spy.received == ["hi"]


def test_submit_strips_whitespace(
    client: TestClient,
    spy_and_broadcaster: tuple[_SubmitSpy, InherentBroadcaster],
) -> None:
    """Leading/trailing whitespace is stripped before the submit_callable fires."""
    spy, _ = spy_and_broadcaster

    resp = client.post("/inherent/submit", json={"text": "  hi  "})

    assert resp.status_code == 200
    assert resp.json() == {"status": "accepted"}
    assert spy.received == ["hi"]


# ---------------------------------------------------------------------------
# /inherent/submit — validation / error paths
# ---------------------------------------------------------------------------


def test_submit_with_empty_text_returns_400(
    client: TestClient,
    spy_and_broadcaster: tuple[_SubmitSpy, InherentBroadcaster],
) -> None:
    """Empty text -> 400; the submit_callable is never invoked."""
    spy, _ = spy_and_broadcaster

    resp = client.post("/inherent/submit", json={"text": ""})

    assert resp.status_code == 400
    assert resp.json()["detail"] == "text required"
    assert spy.received == []


def test_submit_with_whitespace_only_returns_400(
    client: TestClient,
    spy_and_broadcaster: tuple[_SubmitSpy, InherentBroadcaster],
) -> None:
    """Whitespace-only text -> 400 (after .strip()); submit_callable not invoked."""
    spy, _ = spy_and_broadcaster

    resp = client.post("/inherent/submit", json={"text": "   "})

    assert resp.status_code == 400
    assert resp.json()["detail"] == "text required"
    assert spy.received == []


def test_submit_without_text_field_returns_422(
    client: TestClient,
    spy_and_broadcaster: tuple[_SubmitSpy, InherentBroadcaster],
) -> None:
    """Missing 'text' field -> 422 from pydantic; submit_callable not invoked."""
    spy, _ = spy_and_broadcaster

    resp = client.post("/inherent/submit", json={})

    assert resp.status_code == 422
    assert spy.received == []


def test_submit_with_wrong_field_type_returns_422(
    client: TestClient,
    spy_and_broadcaster: tuple[_SubmitSpy, InherentBroadcaster],
) -> None:
    """Non-string 'text' (e.g. int 123) -> 422 from pydantic; spy untouched."""
    spy, _ = spy_and_broadcaster

    resp = client.post("/inherent/submit", json={"text": 123})

    assert resp.status_code == 422
    assert spy.received == []


# ---------------------------------------------------------------------------
# Step-1 stubs — image / asr
# ---------------------------------------------------------------------------


def test_image_submit_returns_501_with_adr_reference(client: TestClient) -> None:
    """POST /inherent/image-submit -> 501; detail mentions 'image' and 'ADR-0004'.

    The legacy inherent-swift ``BridgeBackend.classify`` may match on these
    substrings to render a "deferred" UI hint instead of a generic error,
    so the exact detail string is part of the wire contract.
    """
    resp = client.post("/inherent/image-submit")

    assert resp.status_code == 501
    detail = resp.json()["detail"]
    assert "image" in detail
    assert "ADR-0004" in detail


def test_asr_submit_returns_501_with_adr_reference(client: TestClient) -> None:
    """POST /inherent/asr-submit -> 501; detail mentions 'asr' and 'ADR-0005'."""
    resp = client.post("/inherent/asr-submit")

    assert resp.status_code == 501
    detail = resp.json()["detail"]
    assert "asr" in detail
    assert "ADR-0005" in detail


# ---------------------------------------------------------------------------
# /inherent/ws — connect/disconnect registry mutation + round-trip
# ---------------------------------------------------------------------------


def test_ws_endpoint_registers_then_unregisters_on_disconnect(
    client: TestClient,
    spy_and_broadcaster: tuple[_SubmitSpy, InherentBroadcaster],
) -> None:
    """Connecting registers the client; disconnecting removes it.

    Observed indirectly via the broadcaster's behaviour: a broadcast made
    while connected lands on the WS client, and a broadcast made AFTER
    disconnect goes to no-one (which the broadcaster handles by logging
    and returning — the test would otherwise hang on ``receive_json``).

    Uses ``broadcast_done`` (no payload-read guard, always fires the
    envelope) so this test is decoupled from the per-type payload
    contract — that's covered exhaustively by ``test_inherent_output``.
    """
    _, broadcaster = spy_and_broadcaster

    # Phase 1: connect, broadcast lands, then drop the connection.
    with client.websocket_connect("/inherent/ws") as ws:
        # The WS endpoint runs ``await broadcaster.register(ws)`` inside
        # the server's event loop. Drive a broadcast on that same loop
        # via the session's portal so the registered ws receives it.
        ws.portal.call(broadcaster.broadcast_done, _make_event(text="round-trip"))

        # The ``done`` envelope confirms the client is in the registry.
        done_msg = ws.receive_json()
        assert done_msg["op"] == "done"

    # Phase 2: after the context manager exits, the WS endpoint's
    # ``finally`` ran and unregistered the client. A broadcast on an
    # empty registry must NOT raise — it logs F5 and returns. We can
    # re-enter the broadcaster from the test thread directly (no server
    # loop needed) since no clients are connected.
    import asyncio  # noqa: PLC0415 — local import keeps the module surface lean.

    asyncio.run(broadcaster.broadcast_done(_make_event(text="post-disconnect")))


def test_ws_endpoint_broadcast_round_trip(
    client: TestClient,
    spy_and_broadcaster: tuple[_SubmitSpy, InherentBroadcaster],
) -> None:
    """End-to-end: connect WS, trigger broadcast, receive the legacy ``done`` envelope.

    Exercises the full wiring contract — the WS endpoint accepts,
    registers with the shared broadcaster, and the broadcaster's exact
    legacy wire envelope ({'op':'done','payload':{'fadeMs':5000}})
    reaches the connected client unchanged. The per-type envelope
    contract for ``open`` / ``append`` is covered by
    ``test_inherent_output`` against ``_FakeWebSocket``.
    """
    _, broadcaster = spy_and_broadcaster

    with client.websocket_connect("/inherent/ws") as ws:
        ws.portal.call(broadcaster.broadcast_done, _make_event(text="hello world"))

        done_msg = ws.receive_json()

    assert done_msg == {"op": "done", "payload": {"fadeMs": 5000}}


def test_ws_endpoint_multiple_clients_each_receive_envelopes(
    client: TestClient,
    spy_and_broadcaster: tuple[_SubmitSpy, InherentBroadcaster],
) -> None:
    """Two concurrent WS clients both receive the same broadcast envelope."""
    _, broadcaster = spy_and_broadcaster

    with (
        client.websocket_connect("/inherent/ws") as ws_a,
        client.websocket_connect("/inherent/ws") as ws_b,
    ):
        ws_a.portal.call(broadcaster.broadcast_done, _make_event(text="fanout"))

        a_done = ws_a.receive_json()
        b_done = ws_b.receive_json()

    expected_done: dict[str, object] = {"op": "done", "payload": {"fadeMs": 5000}}
    assert a_done == expected_done
    assert b_done == expected_done


# ---------------------------------------------------------------------------
# Pydantic model — direct surface check
# ---------------------------------------------------------------------------


def test_submit_request_model_accepts_text_field() -> None:
    """``SubmitRequest`` is a pydantic model with exactly one ``text: str`` field."""
    req = SubmitRequest(text="hi")

    assert req.text == "hi"
