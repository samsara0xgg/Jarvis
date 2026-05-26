"""Unit tests for ``jarvis.surface.inherent_output.InherentBroadcaster``.

LLM-free pure-asyncio tests. Uses a duck-typed ``_FakeWebSocket`` spy
instead of the FastAPI ``TestClient`` — the WS endpoint wiring is
covered by Step 7's tests, not this one.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from jarvis.shared import Event
from jarvis.surface.inherent_output import InherentBroadcaster

if TYPE_CHECKING:
    import pytest

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


def _make_event(*, text: str = "hi", turn_id: str = "T-test-001") -> Event:
    """Build a minimal ``surface.response_emitted``-shaped Event."""
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
# Registry
# ---------------------------------------------------------------------------


def test_broadcaster_starts_empty() -> None:
    """A fresh broadcaster has no registered clients."""
    bc = InherentBroadcaster()
    # Broadcasting with no clients must not raise; the no-clients path is
    # the canonical "empty registry" probe (no peek accessor by design).
    asyncio.run(bc.broadcast(_make_event()))


def test_register_adds_client() -> None:
    """After ``register``, a subsequent broadcast reaches the client."""
    bc = InherentBroadcaster()
    ws = _FakeWebSocket(name="ws1")

    asyncio.run(bc.register(ws))
    asyncio.run(bc.broadcast(_make_event(text="hello")))

    assert len(ws.sent_messages) == 2  # open + done


def test_register_is_idempotent() -> None:
    """Registering the same WS twice still yields one membership."""
    bc = InherentBroadcaster()
    ws = _FakeWebSocket(name="ws1")

    asyncio.run(bc.register(ws))
    asyncio.run(bc.register(ws))
    asyncio.run(bc.broadcast(_make_event(text="hi")))

    # Two messages total (open + done) — NOT four. Duplicate registration
    # collapsed because the registry is a set.
    assert len(ws.sent_messages) == 2


def test_unregister_removes_client() -> None:
    """After ``unregister``, the client receives nothing on broadcast."""
    bc = InherentBroadcaster()
    ws = _FakeWebSocket(name="ws1")

    asyncio.run(bc.register(ws))
    asyncio.run(bc.unregister(ws))
    asyncio.run(bc.broadcast(_make_event(text="hi")))

    assert ws.sent_messages == []


def test_unregister_unknown_client_is_idempotent() -> None:
    """Unregistering a never-registered WS is a no-op (set.discard semantics)."""
    bc = InherentBroadcaster()
    ws = _FakeWebSocket(name="never-registered")

    # Must not raise; discard() is idempotent by design.
    asyncio.run(bc.unregister(ws))


# ---------------------------------------------------------------------------
# Broadcast — empty paths
# ---------------------------------------------------------------------------


def test_broadcast_with_no_clients_logs_warning_and_returns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No registered clients -> warning mentioning the turn_id, no crash.

    ADR-0003 F5: Step-1 explicit deviation from spec §3.6.11
    ``surface.failed``. The audit event already landed in the L2 log;
    the daemon just has nothing to physically deliver, so the broadcaster
    logs a warning instead of escalating.
    """
    bc = InherentBroadcaster()

    with caplog.at_level(logging.WARNING, logger="jarvis.surface.inherent_output"):
        asyncio.run(bc.broadcast(_make_event(text="abc", turn_id="T-no-clients-001")))

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    # The warning must carry the turn_id so log scrapers can correlate it
    # back to the audit event.
    assert "T-no-clients-001" in warnings[0].getMessage()


def test_broadcast_with_empty_text_returns_silently() -> None:
    """Empty ``text`` -> no messages, no warning.

    The audit ``surface.response_emitted`` event already exists in the L2
    Event Log (emitted by ``render_response`` with ``delivered_via=[]``);
    the broadcaster has nothing meaningful to push so it returns early.
    """
    bc = InherentBroadcaster()
    ws = _FakeWebSocket(name="ws1")
    asyncio.run(bc.register(ws))

    asyncio.run(bc.broadcast(_make_event(text="")))

    assert ws.sent_messages == []


def test_broadcast_with_missing_text_key_returns_silently() -> None:
    """Payload without a ``text`` key behaves the same as empty text."""
    bc = InherentBroadcaster()
    ws = _FakeWebSocket(name="ws1")
    asyncio.run(bc.register(ws))

    event = Event(
        event_uid="evt-no-text",
        type="surface.response_emitted",
        schema_version=1,
        ts_epoch_ms=1_700_000_000_000,
        payload={"turn_id": "T-no-text-001"},  # no "text" key
        source_event_id=None,
        correlation={"turn_id": "T-no-text-001"},
    )
    asyncio.run(bc.broadcast(event))

    assert ws.sent_messages == []


# ---------------------------------------------------------------------------
# Broadcast — wire contract
# ---------------------------------------------------------------------------


def test_broadcast_pushes_open_then_done_to_single_client() -> None:
    """One client -> exactly ``[open, done]`` envelopes in that order."""
    bc = InherentBroadcaster()
    ws = _FakeWebSocket(name="ws1")
    asyncio.run(bc.register(ws))

    asyncio.run(bc.broadcast(_make_event(text="hi")))

    assert ws.sent_messages == [
        {"op": "open", "payload": {"streaming": False, "content": "hi"}},
        {"op": "done", "payload": {}},
    ]


def test_broadcast_pushes_to_multiple_clients() -> None:
    """Three clients each receive both envelopes."""
    bc = InherentBroadcaster()
    clients = [_FakeWebSocket(name=f"ws{i}") for i in range(3)]
    for ws in clients:
        asyncio.run(bc.register(ws))

    asyncio.run(bc.broadcast(_make_event(text="hello world")))

    expected = [
        {"op": "open", "payload": {"streaming": False, "content": "hello world"}},
        {"op": "done", "payload": {}},
    ]
    for ws in clients:
        assert ws.sent_messages == expected


# ---------------------------------------------------------------------------
# Broadcast — dead-client cleanup (ADR-0003 F4)
# ---------------------------------------------------------------------------


def test_broadcast_dead_client_removed_others_succeed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Dead client gets removed; healthy clients still receive; a 2nd broadcast confirms removal.

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

    asyncio.run(bc.register(ws_a))
    asyncio.run(bc.register(ws_dead))
    asyncio.run(bc.register(ws_b))

    with caplog.at_level(logging.WARNING, logger="jarvis.surface.inherent_output"):
        asyncio.run(bc.broadcast(_make_event(text="first")))

    # The two healthy clients received both envelopes.
    expected_first = [
        {"op": "open", "payload": {"streaming": False, "content": "first"}},
        {"op": "done", "payload": {}},
    ]
    assert ws_a.sent_messages == expected_first
    assert ws_b.sent_messages == expected_first

    # A warning was logged for the failure.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("ws_dead" in w.getMessage() or "disconnected" in w.getMessage().lower()
               for w in warnings), (
        f"expected a warning mentioning the dead client; got: "
        f"{[w.getMessage() for w in warnings]}"
    )

    # Second broadcast — the dead client should have been removed, so it
    # must not be invoked again. Flip ``raise_on_send`` off; if the
    # broadcaster still holds a reference, the new message would now land
    # in ``sent_messages``.
    ws_dead.raise_on_send = None
    asyncio.run(bc.broadcast(_make_event(text="second")))

    expected_second_for_alive = [
        {"op": "open", "payload": {"streaming": False, "content": "second"}},
        {"op": "done", "payload": {}},
    ]
    assert ws_a.sent_messages == expected_first + expected_second_for_alive
    assert ws_b.sent_messages == expected_first + expected_second_for_alive
    assert ws_dead.sent_messages == [], (
        "dead client should have been removed after the first broadcast"
    )
