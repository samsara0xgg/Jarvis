"""ADR-0005 §6: broadcaster gains ``broadcast_voice`` + ``broadcast_voice_sync``.

ADR-0005 §6 defines the ``op:"voice"`` WS envelope to the Inherent client:

    {"op": "voice", "payload": {"phase": <str>, "turn_id": <str>, ...}}

The async method :meth:`broadcast_voice` mirrors the existing
:meth:`broadcast_open` / :meth:`broadcast_chunk` / :meth:`broadcast_done`
shape — it runs on the broadcaster's owning event loop and fans out to
all registered clients via the same ``_send_all`` snapshot path.

The sync method :meth:`broadcast_voice_sync` is the worker-thread → event
loop bridge: the voice pipeline (Task 9) runs ASR / VAD on a daemon
worker thread that does NOT have an attached running loop. It calls
``broadcast_voice_sync`` to schedule the envelope onto the daemon's
event loop via ``asyncio.run_coroutine_threadsafe``. If no loop has been
attached (e.g., unit tests of ``voice_pipeline`` that pass a bare
broadcaster), the call is a no-op + WARN log so the pipeline tests stay
LLM-free / loop-free.

Tests follow the existing ``test_inherent_output.py`` style:
``asyncio.run`` for the async paths, a duck-typed ``_FakeWebSocket``
spy instead of the FastAPI test client, and a dedicated event-loop
thread for the worker-bridge test (genuinely exercises
``run_coroutine_threadsafe`` across thread boundaries).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from jarvis.surface.inherent_output import InherentBroadcaster

if TYPE_CHECKING:
    import pytest
    from starlette.websockets import WebSocket

# ---------------------------------------------------------------------------
# Test doubles — mirror test_inherent_output.py
# ---------------------------------------------------------------------------


@dataclass
class _FakeWebSocket:
    """Minimal duck-typed stand-in for ``starlette.websockets.WebSocket``."""

    name: str = "fake"
    sent_messages: list[dict[str, Any]] = field(default_factory=list)

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent_messages.append(payload)

    # Identity-based hashing so a ``set[_FakeWebSocket]`` works.
    def __hash__(self) -> int:
        return id(self)

    def __eq__(self, other: object) -> bool:
        return self is other


def _as_ws(fake: _FakeWebSocket) -> WebSocket:
    """Present the duck-typed fake as a starlette ``WebSocket`` for mypy.

    ``WebSocket`` is a concrete class (not a Protocol), so the fake
    cannot satisfy it structurally; the cast is confined to this one
    seam so the ``register`` call sites type-check.
    """
    return cast("WebSocket", fake)


# ---------------------------------------------------------------------------
# broadcast_voice — async envelope
# ---------------------------------------------------------------------------


def test_broadcast_voice_sends_envelope_to_clients() -> None:
    """``broadcast_voice("listening", turn_id="T1")`` -> ``op:"voice"`` envelope.

    The payload carries ``phase`` and ``turn_id`` at minimum; mirrors
    ADR-0005 §6.
    """
    bc = InherentBroadcaster()
    ws = _FakeWebSocket(name="ws1")
    asyncio.run(bc.register(_as_ws(ws)))

    asyncio.run(bc.broadcast_voice("listening", turn_id="T1"))

    assert len(ws.sent_messages) == 1
    msg = ws.sent_messages[0]
    assert msg["op"] == "voice"
    assert msg["payload"]["phase"] == "listening"
    assert msg["payload"]["turn_id"] == "T1"


def test_broadcast_voice_passes_optional_fields() -> None:
    """Optional kwargs (transcript, emotion, ...) flow into payload verbatim."""
    bc = InherentBroadcaster()
    ws = _FakeWebSocket(name="ws1")
    asyncio.run(bc.register(_as_ws(ws)))

    asyncio.run(
        bc.broadcast_voice("accepted", turn_id="T2", transcript="你好", emotion="HAPPY"),
    )

    msg = ws.sent_messages[0]
    assert msg["payload"]["transcript"] == "你好"
    assert msg["payload"]["emotion"] == "HAPPY"


def test_broadcast_voice_no_clients_logs_f5_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No clients -> ADR-0003 F5 warning carrying the turn_id; no raise."""
    bc = InherentBroadcaster()

    with caplog.at_level(logging.WARNING, logger="jarvis.surface.inherent_output"):
        asyncio.run(bc.broadcast_voice("listening", turn_id="T-empty"))

    assert any("T-empty" in rec.getMessage() for rec in caplog.records)


# ---------------------------------------------------------------------------
# broadcast_voice_sync — worker-thread bridge
# ---------------------------------------------------------------------------


def test_broadcast_voice_sync_without_attached_loop_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No ``attach_loop`` call yet -> drop the envelope + WARN log; no raise.

    This keeps the voice_pipeline unit tests loop-free: they construct
    a bare broadcaster, never call ``attach_loop``, and the pipeline's
    ``broadcast_voice_sync`` calls become silent (a single WARN apiece).
    """
    bc = InherentBroadcaster()

    with caplog.at_level(logging.WARNING, logger="jarvis.surface.inherent_output"):
        bc.broadcast_voice_sync("listening", turn_id="T-no-loop")

    assert any(
        "attach_loop" in rec.getMessage() and "T-no-loop" in rec.getMessage()
        for rec in caplog.records
    )


def test_broadcast_voice_sync_schedules_onto_attached_loop() -> None:
    """From a non-loop thread, ``broadcast_voice_sync`` lands on the attached loop.

    Spins up a dedicated thread running an event loop (mimics the
    daemon), attaches it to the broadcaster, registers a client on the
    loop, then calls ``broadcast_voice_sync`` from the main test thread
    (which has no running loop). The envelope must reach the client.
    """
    loop = asyncio.new_event_loop()

    def _run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    thread = threading.Thread(target=_run_loop, daemon=True)
    thread.start()

    try:
        bc = InherentBroadcaster()
        bc.attach_loop(loop)
        ws = _FakeWebSocket(name="ws1")

        # Register on the attached loop (the WS endpoint path normally
        # calls ``await broadcaster.register(ws)`` from a loop-resident
        # coroutine; we simulate that via ``run_coroutine_threadsafe``).
        asyncio.run_coroutine_threadsafe(bc.register(_as_ws(ws)), loop).result(timeout=1.0)

        # Main thread has no running loop here; this is the worker-bridge path.
        bc.broadcast_voice_sync("listening", turn_id="T3")

        # Wait for the scheduled coroutine to drain.
        async def _drain() -> None:
            await asyncio.sleep(0.05)

        asyncio.run_coroutine_threadsafe(_drain(), loop).result(timeout=1.0)

        assert len(ws.sent_messages) == 1
        assert ws.sent_messages[0]["op"] == "voice"
        assert ws.sent_messages[0]["payload"]["turn_id"] == "T3"
        assert ws.sent_messages[0]["payload"]["phase"] == "listening"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=1.0)
        loop.close()
