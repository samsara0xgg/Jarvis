"""A paired phone's two doors: its conversation socket and its batch of sensed events.

- ``WS /phone/ws`` — ADR 0209; the phone's conversation, on every host that has ``device_name``
- ``POST /inherent/device/events`` — ADR 0197; a batch of sensed events, each appended once

Only a paired device's own token opens either; the local key is refused here.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request, WebSocket

from jarvis.surface.inherent_server.guards import bearer_token, capped_body
from jarvis.surface.phone_events import MAX_BATCH_BYTES, MAX_BATCH_FRAMES, PHONE_EVENTS_PATH
from jarvis.surface.phone_link import PHONE_PATH, serve_phone

if TYPE_CHECKING:
    from fastapi import FastAPI

    from jarvis.surface.inherent_server.deps import InherentDeps


def register_phone_routes(app: FastAPI, deps: InherentDeps) -> None:
    """ADR 0209, 0197: the phone's socket and its event batches."""
    if deps.phone is not None and deps.device_name is not None:
        phone_hub, phone_socket_name = deps.phone, deps.device_name

        @app.websocket(PHONE_PATH)
        async def ws_phone(ws: WebSocket) -> None:
            """ADR 0209: a paired phone's conversation; only its own device token opens it.

            As on ``/terminal/ws`` and the phone-events route, the local key is not a device
            token and is refused here: a phone always names itself by its token.
            """
            token = bearer_token(ws.headers.get("authorization"))
            name = None if token is None else phone_socket_name(token)
            if name is None:
                await ws.close(code=1008)
                return
            await serve_phone(phone_hub, ws, name)

    if deps.phone_events is not None and deps.device_name is not None:
        phone_name, brain_events = deps.device_name, deps.phone_events

        @app.post(PHONE_EVENTS_PATH)
        async def post_phone_events(request: Request) -> dict[str, Any]:
            """ADR 0197: a paired phone's batch of sensed events, each appended once.

            Only a paired device's own token opens it; the local key is refused here as it
            is on ``/terminal/ws``. ``async`` on purpose: the append runs on the loop
            thread that owns the runtime's log connection, as the socket's does, and a
            plain ``def`` would run it on a worker thread the connection is closed to.
            """
            token = bearer_token(request.headers.get("authorization"))
            name = None if token is None else phone_name(token)
            if name is None:
                raise HTTPException(
                    status_code=403, detail="a paired device's token is required",
                )
            try:
                body = json.loads(await capped_body(request, MAX_BATCH_BYTES))
            except (ValueError, RecursionError):
                body = None
            frames = body.get("events") if isinstance(body, dict) else None
            if not isinstance(frames, list):
                raise HTTPException(status_code=400, detail='send {"events": [...]}')
            if len(frames) > MAX_BATCH_FRAMES:
                raise HTTPException(status_code=413, detail="too many events in one batch")
            return {"acks": brain_events.record_phone_batch(name, frames)}
