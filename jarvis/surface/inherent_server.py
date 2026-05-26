"""Inherent FastAPI app factory — Step 7 of ADR-0003.

L5 surface module. Hosts the daemon's HTTP+WS endpoints the
inherent-swift client (legacy ``desktop/inherent-swift/``) speaks. Step
1 (ADR-0003) is text-only; image / ASR / streaming TTS endpoints are
stubs that return HTTP 501 with the successor-ADR reference so the
client can render a "deferred" hint instead of a generic error.

The runtime wires this app via ``runtime/inherent_loop.serve_inherent``
in Step 8, injecting an :class:`InherentDeps` with:

- ``submit_callable`` — bound to
  ``emit_surface_user_intent(runtime.conn, transcript=text, turn_id=<mint>)``
  (L5 input adapter per spec §3.6.1). The handler wraps the sync call
  in ``asyncio.to_thread`` so the SQLite write does not block the
  event loop.
- ``broadcaster`` — the shared :class:`InherentBroadcaster`. The
  runtime's background ``_response_watcher`` task polls the L2 Event
  Log for ``surface.response_{open,chunk,emitted}`` rows and
  dispatches to the broadcaster's per-type methods; the WS endpoint
  here registers / unregisters connecting clients.

Layer rules (L5): may import from stdlib, ``fastapi`` / ``pydantic`` /
``starlette``, and ``jarvis.surface.inherent_output`` (intra-layer
sibling). Never names :mod:`jarvis.runtime`, :mod:`jarvis.decision`,
:mod:`jarvis.execution`, or :mod:`jarvis.deployment`. The
``submit_callable`` is **injected** so the module never needs to import
:mod:`jarvis.state` either — runtime is the only place wiring across
layers.

Wire contract (preserved from legacy ``ui/web/server.py`` so the
inherent-swift client's ``BridgeBackend`` keeps working unchanged):

- ``POST /inherent/submit``      — body ``{"text": str}`` → ``{"status": "accepted"}``
- ``WS  /inherent/ws``           — outbound-only; client receives ``{"op", "payload"}`` envelopes
- ``GET /api/health``            — liveness; ``{"status": "ok"}``
- ``POST /inherent/image-submit`` — Step 2 / ADR-0004 stub (501)
- ``POST /inherent/asr-submit``   — Step 3 / ADR-0005 stub (501)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Callable

    from jarvis.surface.inherent_output import InherentBroadcaster


class SubmitRequest(BaseModel):
    """Body of ``POST /inherent/submit``.

    Matches the legacy wire shape ``{"text": str}`` exactly so the
    inherent-swift client needs no changes. Pydantic enforces the type;
    a missing field returns 422, a non-string field returns 422, an
    empty string is accepted at the schema layer and rejected at the
    handler layer with 400.
    """

    text: str


@dataclass(frozen=True)
class InherentDeps:
    """Injectable dependencies for the FastAPI app.

    Frozen so the runtime's wiring stays explicit — once
    ``serve_inherent`` builds the deps, neither the app factory nor
    the handlers mutate them.

    Attributes:
        submit_callable: Synchronous side effect of the ``/submit``
            handler. The daemon binds this to a partial of
            ``emit_surface_user_intent(runtime.conn, transcript=text,
            turn_id=<mint>)`` per spec §3.6.1. Sync (not async)
            because the underlying SQLite write is sync; the handler
            offloads the call via ``asyncio.to_thread`` so the event
            loop stays unblocked.
        broadcaster: Shared :class:`InherentBroadcaster` instance.
            The runtime's ``_response_watcher`` task pushes envelopes
            into it from the background (one cursor over the three
            ``surface.response_{open,chunk,emitted}`` types, dispatched
            by ``event.type``); the WS endpoint here registers /
            unregisters client sockets on connect / disconnect.
    """

    submit_callable: Callable[[str], None]
    broadcaster: InherentBroadcaster


def create_app(deps: InherentDeps) -> FastAPI:
    """Build the FastAPI app with all 5 endpoints registered.

    The factory takes the injected deps once and closes over them in
    the route handlers — Step 8's ``serve_inherent`` owns the uvicorn
    lifecycle, so this module does not register startup / shutdown
    hooks.

    Args:
        deps: Bound runtime dependencies (submit callback + shared
            broadcaster). Must outlive every request the app serves.

    Returns:
        A fully-configured :class:`fastapi.FastAPI` instance ready
        for uvicorn to serve.
    """
    app = FastAPI(
        title="Jarvis Inherent (ADR-0003 Step 1, text-only)",
        version="0.1.0",
    )

    @app.post("/inherent/submit", status_code=200)
    async def submit(req: SubmitRequest) -> dict[str, str]:
        """Accept user text and hand off to the runtime via ``submit_callable``.

        ``req.text`` is stripped; an empty post-strip string returns
        400 so the inherent-swift client surfaces the "empty input"
        case explicitly rather than silently no-op-ing. The
        ``submit_callable`` is sync (SQLite write) — offloaded via
        ``asyncio.to_thread`` so the event loop stays free.
        """
        text = req.text.strip()
        if not text:
            raise HTTPException(status_code=400, detail="text required")
        await asyncio.to_thread(deps.submit_callable, text)
        return {"status": "accepted"}

    @app.websocket("/inherent/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        """Outbound-only push channel for Inherent wire envelopes.

        Lifecycle:

        1. ``ws.accept()`` — completes the WS handshake BEFORE the
           registry mutation so the broadcaster never holds a half-
           connected client.
        2. ``broadcaster.register(ws)`` — add the client to the in-
           memory registry under the broadcaster's lock.
        3. ``await ws.receive_text()`` in a loop — the legacy
           inherent-swift client is push-only and never sends frames,
           so this just blocks until the client disconnects. The loop
           also absorbs any future-proofing keep-alive frames without
           interpreting them.
        4. ``WebSocketDisconnect`` ends the loop normally.
        5. ``finally:`` ``broadcaster.unregister(ws)`` — runs whether
           the loop ended via disconnect, cancellation, or an
           unexpected exception, so the registry never leaks dead
           sockets.
        """
        await ws.accept()
        await deps.broadcaster.register(ws)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            await deps.broadcaster.unregister(ws)

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        """Liveness probe — used by ops scripts to confirm the daemon is up."""
        return {"status": "ok"}

    @app.post("/inherent/image-submit", status_code=501)
    async def image_submit() -> None:
        """Step 1 stub — image input lands in Step 2 (ADR-0004)."""
        raise HTTPException(
            status_code=501,
            detail="image not implemented in step 1 (ADR-0004)",
        )

    @app.post("/inherent/asr-submit", status_code=501)
    async def asr_submit() -> None:
        """Step 1 stub — ASR input lands in Step 3 (ADR-0005)."""
        raise HTTPException(
            status_code=501,
            detail="asr not implemented in step 1 (ADR-0005)",
        )

    return app


__all__ = [
    "InherentDeps",
    "SubmitRequest",
    "create_app",
]
