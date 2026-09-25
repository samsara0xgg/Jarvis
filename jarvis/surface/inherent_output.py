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

Step 2 wire schema (three envelopes per turn, mirrored from
``jarvis-legacy/ui/web/server.py:357-414``), each payload carrying the
``turn_id`` added by ADR-0009 D2. A turn that ends with no answer gets
``{"op": "failed" | "cancelled", "payload": {"turn_id": <id>}}`` instead
(from ``turn.failed`` / ``response.cancelled``, sent via :meth:`broadcast_op`):

1. ``{"op": "open",   "payload": {"content": "", "streaming": True,
   "kind": "text", "q": <query>, "turn_id": <id>}}``
                                        ← from ``surface.response_open``
2. ``{"op": "append", "payload": {"token": <text>, "turn_id": <id>}}`` (x N)
                                        ← from ``surface.response_chunk``
3. ``{"op": "done",   "payload": {"fadeMs": 5000, "turn_id": <id>}}``
                                        ← from ``surface.response_emitted``

``turn_id`` is additive (the legacy swift card ignores unknown payload
keys) and it is what makes the ADR-0009 D2 CLI client correct rather
than merely usually-right: without it a client can only match the
``open`` envelope's ``q`` against the text it submitted, so two
concurrent turns carrying byte-identical utterances are
indistinguishable and the wrong answer gets printed.

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

    Holds one ``set[WebSocket]`` plus a global sender lock. Registration
    snapshot handoff and every live envelope share that sender sequence, so a
    client never observes a stale capability after a newer version and never
    receives overlapping ``send_json`` calls from response/voice producers.
    """

    def __init__(self) -> None:
        """Create an empty registry."""
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        # Daemon's event loop, set by ``attach_loop`` at composition root
        # (``runtime/inherent_loop.serve_inherent``). Used by the
        # ``broadcast_voice_sync`` worker-thread bridge. ``None`` until
        # attached → ``broadcast_voice_sync`` becomes a WARN + no-op so
        # unit tests of ``voice_pipeline`` that pass a bare broadcaster
        # stay loop-free.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._latest_voice_capability: dict[str, object] | None = None

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Store the daemon's event loop for the worker-thread → broadcaster bridge.

        Composition-root call (``runtime/inherent_loop.serve_inherent``).
        Idempotent: calling twice replaces the reference. The loop is
        only consulted by :meth:`broadcast_voice_sync`; the async
        ``broadcast_*`` methods run on whichever loop is current and
        ignore this attribute.
        """
        self._loop = loop

    async def register(self, ws: WebSocket) -> None:
        """Add a connected WS client to the registry. Idempotent."""
        async with self._send_lock:
            async with self._lock:
                self._clients.add(ws)
                capability = (
                    dict(self._latest_voice_capability)
                    if self._latest_voice_capability is not None
                    else None
                )
            if capability is not None:
                try:
                    await ws.send_json(
                        {"op": "voice_capability", "payload": capability},
                    )
                except Exception:  # noqa: BLE001 - reconnect snapshot uses F4 removal
                    async with self._lock:
                        self._clients.discard(ws)

    @property
    def has_clients(self) -> bool:
        """Whether any v1 client is still connected."""
        return bool(self._clients)

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
        turn_id = str(event.payload.get("turn_id", "<unknown>"))
        payload: dict[str, object] = {
            "content": "",
            "streaming": True,
            "kind": "text",
            "q": query,
            "turn_id": turn_id,
        }
        # Additive: lets a v1 client target ``POST /inherent/cancel-response``
        # (ADR-0008 D10) without the v2 wire. Absent on legacy events.
        response_id = event.payload.get("response_id")
        if isinstance(response_id, str):
            payload["response_id"] = response_id
        msg: dict[str, object] = {"op": "open", "payload": payload}
        await self._send_all(msg, turn_id=turn_id)

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
        turn_id = str(event.payload.get("turn_id", "<unknown>"))
        msg: dict[str, object] = {
            "op": "append",
            "payload": {"token": text, "turn_id": turn_id},
        }
        await self._send_all(msg, turn_id=turn_id)

    async def broadcast_done(self, event: Event) -> None:
        """Translate ``surface.response_emitted`` into the ``done`` wire envelope.

        Mirrors legacy ``_on_response_final`` at
        ``jarvis-legacy/ui/web/server.py:402-404``. ``fadeMs: 5000`` is
        the legacy default — the swift card's ``FadeController`` uses
        this value to drive its alpha animation timer. No payload
        guard: the ``done`` envelope always sends.

        Args:
            event: The ``surface.response_emitted`` event. Only
                ``payload["turn_id"]`` is read — for the envelope's
                correlation field and the F5 log via
                :meth:`_send_all`.
        """
        turn_id = str(event.payload.get("turn_id", "<unknown>"))
        msg: dict[str, object] = {
            "op": "done",
            "payload": {"fadeMs": 5000, "turn_id": turn_id},
        }
        await self._send_all(msg, turn_id=turn_id)

    async def broadcast_voice(
        self,
        phase: str,
        *,
        turn_id: str,
        **payload: object,
    ) -> None:
        """Translate an ADR-0005 §6 voice phase into the ``voice`` wire envelope.

        ADR-0005 §6 wire schema::

            {"op": "voice",
             "payload": {"phase": <str>, "turn_id": <str>, ...extras}}

        ``phase`` is one of the inherent-voice phases (e.g.,
        ``"listening"``, ``"accepted"``, ``"speaking"``, ``"idle"``);
        the exact set is defined by the daemon's voice watchers (Tasks
        17/18). Extra kwargs (``transcript``, ``emotion``, ...) flow
        into the payload verbatim so the surface can extend the wire
        without bumping this method.

        Mirrors the F4/F5 behavior of :meth:`broadcast_open` /
        :meth:`broadcast_chunk` / :meth:`broadcast_done` via the shared
        :meth:`_send_all` snapshot path.

        Args:
            phase: The voice phase label sent verbatim on the wire.
            turn_id: Correlation id for the F5 log path.
            **payload: Optional extras merged into ``payload`` (e.g.,
                ``transcript``, ``emotion``).
        """
        msg_payload: dict[str, object] = {"phase": phase, "turn_id": turn_id}
        msg_payload.update(payload)
        msg: dict[str, object] = {"op": "voice", "payload": msg_payload}
        await self._send_all(msg, turn_id=turn_id)

    def broadcast_voice_sync(
        self,
        phase: str,
        *,
        turn_id: str,
        **payload: object,
    ) -> None:
        """Worker-thread → broadcaster bridge for ADR-0005 §6 voice envelopes.

        The voice pipeline (Task 9) runs ASR / VAD on a daemon worker
        thread that does NOT have a running event loop. It calls this
        method to schedule :meth:`broadcast_voice` on the daemon's
        event loop via :func:`asyncio.run_coroutine_threadsafe`.

        If :meth:`attach_loop` has not been called (composition root
        wiring is the daemon's job in
        ``runtime/inherent_loop.serve_inherent``), the call is a no-op
        + WARN log. This keeps the ``voice_pipeline`` unit tests
        loop-free: they construct a bare broadcaster and let each
        ``broadcast_voice_sync`` call emit a single WARN apiece without
        needing an asyncio loop.

        Args:
            phase: Voice phase label, forwarded to
                :meth:`broadcast_voice`.
            turn_id: Correlation id, forwarded to
                :meth:`broadcast_voice`.
            **payload: Optional extras, forwarded verbatim.
        """
        loop = self._loop
        if loop is None:
            LOGGER.warning(
                "InherentBroadcaster.broadcast_voice_sync called before "
                "attach_loop; envelope dropped (phase=%s, turn_id=%s).",
                phase,
                turn_id,
            )
            return
        asyncio.run_coroutine_threadsafe(
            self.broadcast_voice(phase, turn_id=turn_id, **payload),
            loop,
        )

    async def broadcast_voice_capability(  # noqa: PLR0913 - explicit wire schema
        self,
        *,
        version: int,
        state: str,
        stream_epoch: int | None,
        reason: str,
        wake_available: bool,
        local_capture_available: bool,
        ptt_upload_available: bool,
        text_available: bool,
        route_kind: str = "unknown",
        allowed_barge_mode: str = "ptt",
    ) -> None:
        """Publish and retain one versioned ephemeral input-capability snapshot."""
        payload: dict[str, object] = {
            "version": version,
            "state": state,
            "stream_epoch": stream_epoch,
            "reason": reason,
            "wake_available": wake_available,
            "local_capture_available": local_capture_available,
            "ptt_upload_available": ptt_upload_available,
            "text_available": text_available,
            "route_kind": route_kind,
            "allowed_barge_mode": allowed_barge_mode,
        }
        async with self._send_lock:
            async with self._lock:
                prior = self._latest_voice_capability
                if prior is not None:
                    prior_version = prior.get("version")
                    if isinstance(prior_version, int) and prior_version >= version:
                        return
                self._latest_voice_capability = payload
            await self._send_all_locked(
                {"op": "voice_capability", "payload": payload},
                turn_id=f"input-capability-v{version}",
            )

    def broadcast_voice_capability_sync(  # noqa: PLR0913 - explicit wire schema
        self,
        *,
        version: int,
        state: str,
        stream_epoch: int | None,
        reason: str,
        wake_available: bool,
        local_capture_available: bool,
        ptt_upload_available: bool,
        text_available: bool,
        route_kind: str = "unknown",
        allowed_barge_mode: str = "ptt",
    ) -> None:
        """Schedule a capability snapshot from the ingress worker thread."""
        loop = self._loop
        if loop is None:
            LOGGER.warning(
                "voice capability v%s dropped before broadcaster loop attach",
                version,
            )
            return
        asyncio.run_coroutine_threadsafe(
            self.broadcast_voice_capability(
                version=version,
                state=state,
                stream_epoch=stream_epoch,
                reason=reason,
                wake_available=wake_available,
                local_capture_available=local_capture_available,
                ptt_upload_available=ptt_upload_available,
                text_available=text_available,
                route_kind=route_kind,
                allowed_barge_mode=allowed_barge_mode,
            ),
            loop,
        )

    async def broadcast_op(self, op: str, **payload: object) -> None:
        """Push one ``{"op": op, "payload": payload}`` envelope with no turn correlation.

        GPT-Live phase A uses ``live`` (session status) and ``subtitle``
        (transcript deltas); the payload is sent verbatim.
        """
        await self._send_all({"op": op, "payload": dict(payload)}, turn_id="")

    async def _send_all(self, msg: dict[str, object], *, turn_id: str) -> None:
        """Serialize one envelope with registration snapshots and live sends."""
        async with self._send_lock:
            await self._send_all_locked(msg, turn_id=turn_id)

    async def _send_all_locked(
        self,
        msg: dict[str, object],
        *,
        turn_id: str,
    ) -> None:
        """Send one globally sequenced envelope; caller owns ``_send_lock``.

        Snapshot pattern: acquire the lock just long enough to copy the
        client set, then iterate the snapshot OUTSIDE the lock so a slow
        client's ``send_json`` doesn't block ``register`` /
        ``unregister`` calls from new connections. Dead clients are
        re-collected and dropped under the lock at the end.

        ``turn_id`` is passed explicitly (vs. the Step-1 shape that
        pulled it off an ``Event``) so callers that don't already have
        an ``Event`` in hand — most notably
        :meth:`broadcast_voice` — can still produce the F5 log line.

        Response, voice, and capability producers may be concurrent. The
        sender lock is therefore the ordering boundary; ``_lock`` only
        protects membership and latest-snapshot state.
        """
        async with self._lock:
            if not self._clients:
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
