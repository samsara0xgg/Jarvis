"""L5 in-memory WebSocket broadcaster for the Inherent text surface — ADR-0003.

``InherentBroadcaster`` is the daemon's only physical surface for the
Inherent client. Single instance per daemon process:

- The FastAPI WebSocket endpoint (``/inherent/ws``) registers each
  connecting client with the broadcaster and unregisters on disconnect.
- The runtime watcher ``_response_watcher`` calls one of three
  per-event-type methods (:meth:`broadcast_open`,
  :meth:`broadcast_chunk`, :meth:`broadcast_done`) for every new
  ``surface.response_{open,chunk,emitted}`` row observed in the L2
  Event Log, translating each into the legacy Inherent wire envelope.

Step 2 wire schema (three envelopes per turn, mirrored verbatim from
``jarvis-legacy/ui/web/server.py:357-414``):

1. ``{"op": "open",   "payload": {"content": "", "streaming": True,
   "kind": "text", "q": <query>}}``     ← from ``surface.response_open``
2. ``{"op": "append", "payload": {"token": <text>}}`` (x N)
                                        ← from ``surface.response_chunk``
3. ``{"op": "done",   "payload": {"fadeMs": 5000}}``
                                        ← from ``surface.response_emitted``

The single-watcher caller (``_response_watcher``) dispatches by
``event.type`` from one cursor ordered by SQLite row id, so within-broadcast
serialization is implicit (one caller, one in-flight method at a time).
The internal ``asyncio.Lock`` still guards the ``_clients`` set against
concurrent ``register`` / ``unregister`` calls from the FastAPI WS
endpoint tasks; ``_send_all`` snapshots the set inside the lock and
iterates outside so a slow client can't hold off a new connection.

Failure modes (ADR-0003 § Failure modes):

- **F4 — per-client send failure.** Log a warning, mark the offending
  client for removal, continue iterating the remaining clients. Dead
  clients are dropped from the registry under the lock so the set
  stays consistent.
- **F5 — no clients registered.** Log a warning carrying the event's
  ``turn_id`` and return. Explicit Step-1/Step-2 deviation from spec
  §3.6.11 ``surface.failed``; deferred to ADR-0007. F4/F5 are
  unchanged from Step 1.

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
    """In-memory WS client registry + per-event-type wire-envelope translator.

    Holds one ``set[WebSocket]`` of connected clients guarded by a single
    ``asyncio.Lock``. ``register`` / ``unregister`` acquire the lock for
    the set mutation; the three ``broadcast_*`` methods snapshot the set
    inside the lock and iterate outside so per-client ``send_json``
    latency does not block new connections. Within-broadcast
    serialization is implicit because the runtime watcher is the sole
    caller and dispatches one envelope at a time.
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

    async def broadcast_open(self, event: Event) -> None:
        """Translate ``surface.response_open`` into the ``open`` wire envelope.

        Envelope shape mirrors legacy ``_on_response_start`` at
        ``jarvis-legacy/ui/web/server.py:381-389`` byte-for-byte except
        that ``q`` is ALWAYS included (legacy omitted ``q`` when empty;
        Step 2 sets ``q=""`` so the wire is predictable — the swift
        card tolerates an empty string just fine).

        Args:
            event: The ``surface.response_open`` event. Only
                ``payload["query"]`` and ``payload["turn_id"]`` are
                read; other keys are ignored.
        """
        query = event.payload.get("query", "") or ""
        msg: dict[str, object] = {
            "op": "open",
            "payload": {
                "content": "",
                "streaming": True,
                "kind": "text",
                "q": query,
            },
        }
        await self._send_all(msg, event)

    async def broadcast_chunk(self, event: Event) -> None:
        """Translate ``surface.response_chunk`` into the ``append`` wire envelope.

        Mirrors legacy ``_on_response_chunk`` at
        ``jarvis-legacy/ui/web/server.py:392-400``. Empty / missing
        ``text`` returns silently (the audit row already exists in the
        L2 log; an empty token would surface as a no-op on the swift
        card's typing-animation buffer).

        Args:
            event: The ``surface.response_chunk`` event. Only
                ``payload["text"]`` is read.
        """
        text = event.payload.get("text", "") or ""
        if not text:
            return
        msg: dict[str, object] = {
            "op": "append",
            "payload": {"token": text},
        }
        await self._send_all(msg, event)

    async def broadcast_done(self, event: Event) -> None:
        """Translate ``surface.response_emitted`` into the ``done`` wire envelope.

        Mirrors legacy ``_on_response_final`` at
        ``jarvis-legacy/ui/web/server.py:402-404``. ``fadeMs: 5000`` is
        the legacy default — the swift card's ``FadeController`` uses
        this value to drive its alpha animation timer. No payload
        guard: the ``done`` envelope always sends.

        Args:
            event: The ``surface.response_emitted`` event. No payload
                fields are read (only ``turn_id`` is used for the F5
                log via :meth:`_send_all`).
        """
        msg: dict[str, object] = {
            "op": "done",
            "payload": {"fadeMs": 5000},
        }
        await self._send_all(msg, event)

    async def _send_all(self, msg: dict[str, object], event: Event) -> None:
        """Send ``msg`` to every registered WS; handle F4 + F5 failure modes.

        Snapshot pattern: acquire the lock just long enough to copy the
        client set, then iterate the snapshot OUTSIDE the lock so a slow
        client's ``send_json`` doesn't block ``register`` /
        ``unregister`` calls from new connections. Dead clients are
        re-collected and dropped under the lock at the end.

        Single-caller invariant (``_response_watcher``) means no two
        ``_send_all`` invocations can overlap on this broadcaster
        instance, so within-broadcast ordering is implicit. The lock
        protects ONLY the ``_clients`` set membership against concurrent
        WS-endpoint tasks.
        """
        async with self._lock:
            if not self._clients:
                turn_id = event.payload.get("turn_id", "<unknown>")
                LOGGER.warning(
                    "InherentBroadcaster: no clients connected for turn_id=%s "
                    "op=%s; envelope dropped (ADR-0003 F5, deferred to ADR-0007).",
                    turn_id,
                    msg.get("op"),
                )
                return
            clients_snapshot = list(self._clients)

        dead: list[WebSocket] = []
        for ws in clients_snapshot:
            try:
                await ws.send_json(msg)
            except Exception as exc:  # noqa: BLE001 — WS transport raises many errno/type variants; logging + drop is the right posture per ADR-0003 F4.
                LOGGER.warning(
                    "InherentBroadcaster: send failed for client %r (%s); removing from registry.",
                    ws,
                    exc,
                )
                dead.append(ws)

        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)


__all__ = [
    "InherentBroadcaster",
]
