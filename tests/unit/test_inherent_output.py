"""Unit tests for ``jarvis.surface.inherent_output.InherentBroadcaster``.

LLM-free pure-asyncio tests. Uses a duck-typed ``_FakeWebSocket`` spy
instead of the FastAPI ``TestClient`` — the WS endpoint wiring is
covered by ``test_inherent_server.py``, not this one.

Covers the ADR-0003 Step 2 three-method API:
``broadcast_open`` / ``broadcast_chunk`` / ``broadcast_done``. Each
method translates the corresponding ``surface.response_{open,chunk,emitted}``
event into the legacy Inherent wire envelope (mirrored verbatim from
``jarvis-legacy/ui/web/server.py:357-414``).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from jarvis.shared import Event
from jarvis.surface.inherent_output import InherentBroadcaster

if TYPE_CHECKING:
    import pytest
    from starlette.websockets import WebSocket

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _FakeWebSocket:
    """Minimal duck-typed stand-in for ``starlette.websockets.WebSocket``.

    Spies on ``send_json``. When ``raise_on_send`` is set, every call to
    ``send_json`` raises that exception instead of recording the payload
    (used by the dead-client removal tests).
    """

    name: str = "fake"
    raise_on_send: BaseException | None = None
    sent_messages: list[dict[str, Any]] = field(default_factory=list)

    async def send_json(self, payload: dict[str, Any]) -> None:
        if self.raise_on_send is not None:
            raise self.raise_on_send
        self.sent_messages.append(payload)

    # Identity-based hashing so a ``set[_FakeWebSocket]`` works even
    # though dataclass equality would otherwise make two empty fakes
    # collapse to one set member.
    def __hash__(self) -> int:
        return id(self)

    def __eq__(self, other: object) -> bool:
        return self is other


def _as_ws(fake: _FakeWebSocket) -> WebSocket:
    """Present the duck-typed fake as a starlette ``WebSocket`` for mypy.

    ``WebSocket`` is a concrete class (not a Protocol), so the fake
    cannot satisfy it structurally; the cast is confined to this one
    seam so the ``register`` / ``unregister`` call sites type-check.
    """
    return cast("WebSocket", fake)


def _make_open_event(*, query: str = "hello", turn_id: str = "T-open-001") -> Event:
    """Build a ``surface.response_open``-shaped Event."""
    return Event(
        event_uid="evt-open-1",
        type="surface.response_open",
        schema_version=1,
        ts_epoch_ms=1_700_000_000_000,
        payload={"turn_id": turn_id, "query": query, "kind": "text"},
        source_event_id=None,
        correlation={"turn_id": turn_id},
    )


def _make_chunk_event(*, text: str = "world", turn_id: str = "T-chunk-001") -> Event:
    """Build a ``surface.response_chunk``-shaped Event."""
    return Event(
        event_uid="evt-chunk-1",
        type="surface.response_chunk",
        schema_version=1,
        ts_epoch_ms=1_700_000_000_001,
        payload={"turn_id": turn_id, "text": text},
        source_event_id=None,
        correlation={"turn_id": turn_id},
    )


def _make_done_event(*, turn_id: str = "T-done-001") -> Event:
    """Build a ``surface.response_emitted``-shaped Event."""
    return Event(
        event_uid="evt-done-1",
        type="surface.response_emitted",
        schema_version=1,
        ts_epoch_ms=1_700_000_000_002,
        payload={"turn_id": turn_id, "text": "ignored-by-done"},
        source_event_id=None,
        correlation={"turn_id": turn_id},
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_broadcaster_starts_empty() -> None:
    """A fresh broadcaster has no registered clients.

    Probed via the no-clients path on ``broadcast_done`` — by design the
    broadcaster exposes no peek accessor; ``broadcast_done`` always
    fires the F5 path when empty (no payload guard) so it's the
    cleanest empty-registry probe.
    """
    bc = InherentBroadcaster()
    asyncio.run(bc.broadcast_done(_make_done_event()))


def test_register_adds_client() -> None:
    """After ``register``, a subsequent broadcast reaches the client."""
    bc = InherentBroadcaster()
    ws = _FakeWebSocket(name="ws1")

    asyncio.run(bc.register(_as_ws(ws)))
    asyncio.run(bc.broadcast_done(_make_done_event()))

    assert len(ws.sent_messages) == 1
    assert ws.sent_messages[0]["op"] == "done"


def test_register_is_idempotent() -> None:
    """Registering the same WS twice still yields one membership."""
    bc = InherentBroadcaster()
    ws = _FakeWebSocket(name="ws1")

    asyncio.run(bc.register(_as_ws(ws)))
    asyncio.run(bc.register(_as_ws(ws)))
    asyncio.run(bc.broadcast_done(_make_done_event()))

    # One message total — NOT two. Duplicate registration collapsed
    # because the registry is a set.
    assert len(ws.sent_messages) == 1


def test_unregister_removes_client() -> None:
    """After ``unregister``, the client receives nothing on broadcast."""
    bc = InherentBroadcaster()
    ws = _FakeWebSocket(name="ws1")

    asyncio.run(bc.register(_as_ws(ws)))
    asyncio.run(bc.unregister(_as_ws(ws)))
    asyncio.run(bc.broadcast_done(_make_done_event()))

    assert ws.sent_messages == []


def test_unregister_unknown_client_is_idempotent() -> None:
    """Unregistering a never-registered WS is a no-op (set.discard semantics)."""
    bc = InherentBroadcaster()
    ws = _FakeWebSocket(name="never-registered")

    # Must not raise; discard() is idempotent by design.
    asyncio.run(bc.unregister(_as_ws(ws)))


# ---------------------------------------------------------------------------
# broadcast_open — envelope + payload semantics
# ---------------------------------------------------------------------------


def test_broadcast_open_pushes_legacy_envelope() -> None:
    """``broadcast_open`` -> legacy open envelope with content/streaming/kind/q.

    Mirrors legacy ``_on_response_start`` at
    ``jarvis-legacy/ui/web/server.py:381-389`` — except that Step 2
    ALWAYS includes ``q`` (even empty) so the wire is predictable.
    """
    bc = InherentBroadcaster()
    ws = _FakeWebSocket(name="ws1")
    asyncio.run(bc.register(_as_ws(ws)))

    asyncio.run(bc.broadcast_open(_make_open_event(query="what time is it")))

    assert ws.sent_messages == [
        {
            "op": "open",
            "payload": {
                "content": "",
                "streaming": True,
                "kind": "text",
                "q": "what time is it",
                "turn_id": "T-open-001",
            },
        },
    ]


def test_broadcast_open_with_empty_query_sets_q_to_empty_string() -> None:
    """Empty ``query`` -> ``q=""`` (not skipped).

    Legacy server.py:386-389 SKIPPED the q key on empty; Step 2 sets
    ``q=""`` so the wire is predictable. The swift card tolerates
    empty ``q`` just fine.
    """
    bc = InherentBroadcaster()
    ws = _FakeWebSocket(name="ws1")
    asyncio.run(bc.register(_as_ws(ws)))

    asyncio.run(bc.broadcast_open(_make_open_event(query="")))

    assert len(ws.sent_messages) == 1
    assert ws.sent_messages[0]["payload"]["q"] == ""


def test_broadcast_open_with_missing_query_key_sets_q_to_empty_string() -> None:
    """Payload lacking a ``query`` key -> ``q=""`` (same as empty)."""
    bc = InherentBroadcaster()
    ws = _FakeWebSocket(name="ws1")
    asyncio.run(bc.register(_as_ws(ws)))

    event = Event(
        event_uid="evt-open-no-q",
        type="surface.response_open",
        schema_version=1,
        ts_epoch_ms=1_700_000_000_000,
        payload={"turn_id": "T-no-q", "kind": "text"},  # no "query"
        source_event_id=None,
        correlation={"turn_id": "T-no-q"},
    )
    asyncio.run(bc.broadcast_open(event))

    assert ws.sent_messages == [
        {
            "op": "open",
            "payload": {
                "content": "",
                "streaming": True,
                "kind": "text",
                "q": "",
                "turn_id": "T-no-q",
            },
        },
    ]


def test_broadcast_open_with_no_clients_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No clients -> F5 warning mentioning the turn_id; no crash."""
    bc = InherentBroadcaster()

    with caplog.at_level(logging.WARNING, logger="jarvis.surface.inherent_output"):
        asyncio.run(
            bc.broadcast_open(_make_open_event(query="abc", turn_id="T-open-noc-001")),
        )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "T-open-noc-001" in warnings[0].getMessage()


# ---------------------------------------------------------------------------
# broadcast_chunk — envelope + empty-text suppression
# ---------------------------------------------------------------------------


def test_broadcast_chunk_pushes_legacy_envelope() -> None:
    """``broadcast_chunk`` -> ``{op:append, payload:{token:<text>}}``.

    Mirrors legacy ``_on_response_chunk`` at
    ``jarvis-legacy/ui/web/server.py:392-400``.
    """
    bc = InherentBroadcaster()
    ws = _FakeWebSocket(name="ws1")
    asyncio.run(bc.register(_as_ws(ws)))

    asyncio.run(bc.broadcast_chunk(_make_chunk_event(text="hello ")))

    assert ws.sent_messages == [
        {"op": "append", "payload": {"token": "hello ", "turn_id": "T-chunk-001"}},
    ]


def test_broadcast_chunk_with_empty_text_returns_silently(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Empty ``text`` -> no envelope, NO F5 warning (legacy line 398-399).

    Empty-text suppression happens BEFORE the no-clients check so a
    spurious empty chunk doesn't trigger the F5 log.
    """
    bc = InherentBroadcaster()
    ws = _FakeWebSocket(name="ws1")
    asyncio.run(bc.register(_as_ws(ws)))

    with caplog.at_level(logging.WARNING, logger="jarvis.surface.inherent_output"):
        asyncio.run(bc.broadcast_chunk(_make_chunk_event(text="")))

    assert ws.sent_messages == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == []


def test_broadcast_chunk_with_missing_text_key_returns_silently() -> None:
    """Payload without a ``text`` key behaves the same as empty text."""
    bc = InherentBroadcaster()
    ws = _FakeWebSocket(name="ws1")
    asyncio.run(bc.register(_as_ws(ws)))

    event = Event(
        event_uid="evt-no-text",
        type="surface.response_chunk",
        schema_version=1,
        ts_epoch_ms=1_700_000_000_000,
        payload={"turn_id": "T-no-text"},  # no "text" key
        source_event_id=None,
        correlation={"turn_id": "T-no-text"},
    )
    asyncio.run(bc.broadcast_chunk(event))

    assert ws.sent_messages == []


def test_broadcast_chunk_with_no_clients_logs_warning_when_text_nonempty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Non-empty text + no clients -> F5 warning mentioning the turn_id."""
    bc = InherentBroadcaster()

    with caplog.at_level(logging.WARNING, logger="jarvis.surface.inherent_output"):
        asyncio.run(
            bc.broadcast_chunk(_make_chunk_event(text="hi", turn_id="T-chunk-noc-001")),
        )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "T-chunk-noc-001" in warnings[0].getMessage()


# ---------------------------------------------------------------------------
# broadcast_done — envelope (always sends, no payload guard)
# ---------------------------------------------------------------------------


def test_broadcast_done_pushes_legacy_envelope() -> None:
    """``broadcast_done`` -> ``{op:done, payload:{fadeMs:5000}}``.

    Mirrors legacy ``_on_response_final`` at
    ``jarvis-legacy/ui/web/server.py:402-404``. ``fadeMs:5000`` is the
    legacy default — the swift card's ``FadeController`` uses this to
    drive the alpha animation timer.
    """
    bc = InherentBroadcaster()
    ws = _FakeWebSocket(name="ws1")
    asyncio.run(bc.register(_as_ws(ws)))

    asyncio.run(bc.broadcast_done(_make_done_event()))

    assert ws.sent_messages == [
        {"op": "done", "payload": {"fadeMs": 5000, "turn_id": "T-done-001"}},
    ]


def test_broadcast_done_always_sends_no_payload_guard() -> None:
    """``done`` ignores the event payload — empty payload still fires the envelope."""
    bc = InherentBroadcaster()
    ws = _FakeWebSocket(name="ws1")
    asyncio.run(bc.register(_as_ws(ws)))

    event = Event(
        event_uid="evt-done-empty",
        type="surface.response_emitted",
        schema_version=1,
        ts_epoch_ms=1_700_000_000_000,
        payload={"turn_id": "T-done-empty"},  # no extra fields
        source_event_id=None,
        correlation={"turn_id": "T-done-empty"},
    )
    asyncio.run(bc.broadcast_done(event))

    assert ws.sent_messages == [
        {"op": "done", "payload": {"fadeMs": 5000, "turn_id": "T-done-empty"}},
    ]


def test_broadcast_done_with_no_clients_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No clients -> F5 warning mentioning the turn_id; no crash."""
    bc = InherentBroadcaster()

    with caplog.at_level(logging.WARNING, logger="jarvis.surface.inherent_output"):
        asyncio.run(bc.broadcast_done(_make_done_event(turn_id="T-done-noc-001")))

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "T-done-noc-001" in warnings[0].getMessage()


# ---------------------------------------------------------------------------
# Multiple clients
# ---------------------------------------------------------------------------


def test_broadcast_open_pushes_to_multiple_clients() -> None:
    """Three clients each receive the open envelope."""
    bc = InherentBroadcaster()
    clients = [_FakeWebSocket(name=f"ws{i}") for i in range(3)]
    for ws in clients:
        asyncio.run(bc.register(_as_ws(ws)))

    asyncio.run(bc.broadcast_open(_make_open_event(query="multi")))

    expected = [
        {
            "op": "open",
            "payload": {
                "content": "",
                "streaming": True,
                "kind": "text",
                "q": "multi",
                "turn_id": "T-open-001",
            },
        },
    ]
    for ws in clients:
        assert ws.sent_messages == expected


def test_full_open_chunk_done_sequence_to_single_client() -> None:
    """Three sequential broadcasts produce the legacy open/append/done sequence."""
    bc = InherentBroadcaster()
    ws = _FakeWebSocket(name="ws1")
    asyncio.run(bc.register(_as_ws(ws)))

    asyncio.run(bc.broadcast_open(_make_open_event(query="hi", turn_id="T-seq")))
    asyncio.run(bc.broadcast_chunk(_make_chunk_event(text="hello ", turn_id="T-seq")))
    asyncio.run(bc.broadcast_chunk(_make_chunk_event(text="world", turn_id="T-seq")))
    asyncio.run(bc.broadcast_done(_make_done_event(turn_id="T-seq")))

    assert ws.sent_messages == [
        {
            "op": "open",
            "payload": {
                "content": "",
                "streaming": True,
                "kind": "text",
                "q": "hi",
                "turn_id": "T-seq",
            },
        },
        {"op": "append", "payload": {"token": "hello ", "turn_id": "T-seq"}},
        {"op": "append", "payload": {"token": "world", "turn_id": "T-seq"}},
        {"op": "done", "payload": {"fadeMs": 5000, "turn_id": "T-seq"}},
    ]


# ---------------------------------------------------------------------------
# Dead-client cleanup (ADR-0003 F4)
# ---------------------------------------------------------------------------


def test_broadcast_open_dead_client_removed_others_succeed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Dead client gets removed; healthy clients still receive; 2nd broadcast confirms removal.

    ADR-0003 F4: per-client ``send_json`` failure is isolated — log the
    failure, mark for removal, continue serving the others.
    """
    bc = InherentBroadcaster()
    ws_a = _FakeWebSocket(name="ws_a")
    ws_dead = _FakeWebSocket(
        name="ws_dead",
        raise_on_send=RuntimeError("WebSocket disconnected"),
    )
    ws_b = _FakeWebSocket(name="ws_b")

    asyncio.run(bc.register(_as_ws(ws_a)))
    asyncio.run(bc.register(_as_ws(ws_dead)))
    asyncio.run(bc.register(_as_ws(ws_b)))

    with caplog.at_level(logging.WARNING, logger="jarvis.surface.inherent_output"):
        asyncio.run(bc.broadcast_open(_make_open_event(query="first")))

    # The two healthy clients each received the open envelope.
    expected_first = [
        {
            "op": "open",
            "payload": {
                "content": "",
                "streaming": True,
                "kind": "text",
                "q": "first",
                "turn_id": "T-open-001",
            },
        },
    ]
    assert ws_a.sent_messages == expected_first
    assert ws_b.sent_messages == expected_first

    # A warning was logged for the failure.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "ws_dead" in w.getMessage() or "disconnected" in w.getMessage().lower() for w in warnings
    ), f"expected a warning mentioning the dead client; got: {[w.getMessage() for w in warnings]}"

    # Second broadcast — the dead client should have been removed, so it
    # must not be invoked again. Flip ``raise_on_send`` off; if the
    # broadcaster still holds a reference, the new message would now
    # land in ``sent_messages``.
    ws_dead.raise_on_send = None
    asyncio.run(bc.broadcast_done(_make_done_event(turn_id="T-second")))

    expected_done = [{"op": "done", "payload": {"fadeMs": 5000, "turn_id": "T-second"}}]
    assert ws_a.sent_messages == expected_first + expected_done
    assert ws_b.sent_messages == expected_first + expected_done
    assert ws_dead.sent_messages == [], (
        "dead client should have been removed after the first broadcast"
    )


def test_broadcast_chunk_dead_client_removed_others_succeed() -> None:
    """F4 isolation also applies to ``broadcast_chunk``."""
    bc = InherentBroadcaster()
    ws_dead = _FakeWebSocket(
        name="ws_dead",
        raise_on_send=RuntimeError("boom"),
    )
    ws_healthy = _FakeWebSocket(name="ws_healthy")

    asyncio.run(bc.register(_as_ws(ws_dead)))
    asyncio.run(bc.register(_as_ws(ws_healthy)))

    asyncio.run(bc.broadcast_chunk(_make_chunk_event(text="tok")))

    assert ws_healthy.sent_messages == [
        {"op": "append", "payload": {"token": "tok", "turn_id": "T-chunk-001"}},
    ]

    # Confirm the dead client was dropped: a second broadcast lands only
    # on the healthy client.
    ws_dead.raise_on_send = None
    asyncio.run(bc.broadcast_chunk(_make_chunk_event(text="tok2")))
    assert ws_dead.sent_messages == []
    assert ws_healthy.sent_messages == [
        {"op": "append", "payload": {"token": "tok", "turn_id": "T-chunk-001"}},
        {"op": "append", "payload": {"token": "tok2", "turn_id": "T-chunk-001"}},
    ]


def test_broadcast_done_dead_client_removed_others_succeed() -> None:
    """F4 isolation also applies to ``broadcast_done``."""
    bc = InherentBroadcaster()
    ws_dead = _FakeWebSocket(name="ws_dead", raise_on_send=RuntimeError("boom"))
    ws_healthy = _FakeWebSocket(name="ws_healthy")

    asyncio.run(bc.register(_as_ws(ws_dead)))
    asyncio.run(bc.register(_as_ws(ws_healthy)))

    asyncio.run(bc.broadcast_done(_make_done_event()))

    assert ws_healthy.sent_messages == [
        {"op": "done", "payload": {"fadeMs": 5000, "turn_id": "T-done-001"}},
    ]

    ws_dead.raise_on_send = None
    asyncio.run(bc.broadcast_done(_make_done_event()))
    assert ws_dead.sent_messages == []
    assert len(ws_healthy.sent_messages) == 2
