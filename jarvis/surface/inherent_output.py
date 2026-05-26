"""L5 in-memory WebSocket broadcaster for the Inherent text surface — Step 6 of ADR-0003.

``InherentBroadcaster`` is the daemon's only physical surface for the new
(text-only Step-1) Inherent client. Single instance per daemon process:

- The FastAPI WebSocket endpoint from Step 7 (``/inherent/ws``) registers
  each connecting client with the broadcaster and unregisters on
  disconnect.
- The runtime watcher from Step 8 (``_response_broadcaster``) calls
  :meth:`InherentBroadcaster.broadcast` for every
  ``surface.response_emitted`` event observed in the L2 Event Log,
  translating it into the legacy Inherent wire envelope.

The wire envelope shape is preserved from the legacy
``inherent-swift/BridgeBackend.swift`` client (ADR-0003 D8):
``{"op": str, "payload": dict}``. Step 1 emits exactly two envelopes per
turn:

1. ``{"op": "open", "payload": {"streaming": False, "content": <text>}}``
2. ``{"op": "done", "payload": {}}``

Failure modes (ADR-0003 § Failure modes):

- **F4 — per-client send failure.** Log a warning, mark the offending
  client for removal, continue iterating the remaining clients. The
  dead client is dropped from the registry inside the same lock so the
  set stays consistent.
- **F5 — no clients registered.** Log a warning carrying the event's
  ``turn_id`` and return. Step-1 explicit deviation from spec §3.6.11
  ``surface.failed``; deferred to ADR-0007.

Layer rules (L5): may import from :mod:`jarvis.shared` and (transitively
via ``Event``) ``jarvis.state`` types, but never names
:mod:`jarvis.decision`, :mod:`jarvis.execution`, or
:mod:`jarvis.deployment`. The ``starlette.websockets.WebSocket`` type is
referenced only in type annotations and so is imported under
``TYPE_CHECKING`` — fastapi re-exports the same class, but the canonical
home is starlette.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.websockets import WebSocket

    from jarvis.shared import Event


LOGGER = logging.getLogger("jarvis.surface.inherent_output")


class InherentBroadcaster:
    """In-memory WS client registry + ``surface.response_emitted`` translator.

    Holds one ``set[WebSocket]`` of connected clients guarded by a single
    ``asyncio.Lock`` — register/unregister/broadcast all acquire the lock
    so the registry stays consistent under concurrent connection churn
    and concurrent broadcasts. Broadcasts are short (two ``send_json``
    calls per client) so the lock contention is acceptable.
    """

    def __init__(self) -> None:
        """Create an empty registry."""
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def register(self, ws: WebSocket) -> None:
        """Add a connected WS client to the registry. Idempotent."""
        async with self._lock:
            self._clients.add(ws)

    async def unregister(self, ws: WebSocket) -> None:
        """Remove a disconnected WS client from the registry. Idempotent."""
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, event: Event) -> None:
        """Translate one ``surface.response_emitted`` event into open+done envelopes.

        Behaviour:

        - Reads ``event.payload["text"]``. If absent or empty, returns
          silently (the audit event already exists in the L2 log; the
          daemon simply has nothing to deliver).
        - Constructs ``{"op": "open", "payload": {"streaming": False,
          "content": text}}`` and ``{"op": "done", "payload": {}}``.
        - If no clients are registered: logs a warning that includes the
          event's ``turn_id`` and returns (ADR-0003 F5).
        - For each registered client: ``send_json(open)`` then
          ``send_json(done)``. A failure for one client is logged as a
          warning and marks that client for removal; iteration continues
          for the others (ADR-0003 F4).
        - Dead clients are removed from the registry before the method
          returns, inside the same lock so concurrent register/unregister
          calls see a consistent set.

        Args:
            event: The ``surface.response_emitted`` event from the L2
                Event Log. Only ``payload["text"]`` and
                ``payload["turn_id"]`` are read; other keys are ignored.
        """
        text = event.payload.get("text", "") or ""
        if not text:
            # Empty / missing text — the audit event already landed in
            # the log; nothing to physically deliver.
            return

        open_envelope = {
            "op": "open",
            "payload": {"streaming": False, "content": text},
        }
        done_envelope: dict[str, object] = {"op": "done", "payload": {}}

        async with self._lock:
            if not self._clients:
                turn_id = event.payload.get("turn_id", "<unknown>")
                LOGGER.warning(
                    "InherentBroadcaster.broadcast: no clients connected for "
                    "turn_id=%s; surface.response_emitted dropped (ADR-0003 F5, "
                    "deferred to ADR-0007).",
                    turn_id,
                )
                return

            dead: list[WebSocket] = []
            for ws in self._clients:
                try:
                    await ws.send_json(open_envelope)
                    await ws.send_json(done_envelope)
                except Exception as exc:  # noqa: BLE001 — WS transport raises many errno/type variants; logging + drop is the right posture per ADR-0003 F4.
                    LOGGER.warning(
                        "InherentBroadcaster.broadcast: send failed for client "
                        "%r (%s); removing from registry.",
                        ws,
                        exc,
                    )
                    dead.append(ws)

            for ws in dead:
                self._clients.discard(ws)


__all__ = [
    "InherentBroadcaster",
]
