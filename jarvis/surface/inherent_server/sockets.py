"""The desktop surface's socket and a paired terminal's socket.

- ``WS /inherent/ws`` — outbound-only; the client receives ``{"op", "payload"}`` envelopes the
  runtime's response watcher dispatches through the shared
  :class:`~jarvis.surface.inherent_output.InherentBroadcaster`; this route only registers and
  unregisters the connecting clients
- ``WS /terminal/ws`` — ADR 0170; a paired terminal, opened only by its own device token
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING

from fastapi import WebSocket, WebSocketDisconnect

from jarvis.surface.inherent_server.guards import bearer_token, reader_of
from jarvis.surface.terminal_link import TERMINAL_PATH, serve_terminal

if TYPE_CHECKING:
    from fastapi import FastAPI

    from jarvis.surface.inherent_server.deps import InherentDeps


LOGGER = logging.getLogger(__name__)


def register_ws_route(app: FastAPI, deps: InherentDeps) -> None:
    """``WS /inherent/ws``: register the client with the broadcaster until it goes."""

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
        # ADR 0222: the device this socket is (the terminal's upstream carries its token), so
        # another device's turns stay off it.
        await deps.broadcaster.register(ws, reader_of(deps, ws))
        # The `live` op is the client's only writer of live state (resonance
        # src/runtime.ts), so a fresh client needs one snapshot; sending it
        # through the broadcaster keeps it in the same order as the pushes.
        if deps.live is not None:
            with contextlib.suppress(Exception):
                await deps.broadcaster.broadcast_op("live", phase="state", **deps.live.status())
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            await deps.broadcaster.unregister(ws)
            # ADR 0041: with no surface left to leave wave mode, stop
            # listening without a wake word.
            if deps.controls is not None and deps.controls.conversation and not (
                deps.broadcaster.has_clients
            ):
                deps.controls.update(conversation=False)
                LOGGER.info("controls: conversation=False (last surface disconnected)")


def register_terminal_route(app: FastAPI, deps: InherentDeps) -> None:
    """``WS /terminal/ws``, when the brain has terminals and device tokens."""
    if deps.terminals is None or deps.device_name is None:
        return
    terminals, device_name = deps.terminals, deps.device_name

    @app.websocket(TERMINAL_PATH)
    async def ws_terminal(ws: WebSocket) -> None:
        """ADR 0170: a paired terminal's socket; only its own device token opens it.

        The middleware has already required a device token from a remote peer. A peer
        on loopback presents the local key there, which is not a device token, so it
        is refused here: a terminal always names itself by its token.
        """
        token = bearer_token(ws.headers.get("authorization"))
        name = None if token is None else device_name(token)
        if name is None:
            await ws.close(code=1008)
            return
        await serve_terminal(terminals, ws, name)
