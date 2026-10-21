"""The push registration route (ADR 0210): a paired phone's APNs device token."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request

from jarvis.state.push_tokens import RegistrationError
from jarvis.surface.apns import MAX_REGISTER_BYTES, REGISTER_PATH
from jarvis.surface.inherent_server.guards import bearer_token, capped_body

if TYPE_CHECKING:
    from fastapi import FastAPI

    from jarvis.surface.inherent_server.deps import InherentDeps


def register_push_route(app: FastAPI, deps: InherentDeps) -> None:
    """ADR 0210: ``POST`` of a paired phone's push tokens, when ``deps.push_register`` is wired."""
    if deps.push_register is None or deps.device_name is None:
        return
    push_name, push_register = deps.device_name, deps.push_register

    @app.post(REGISTER_PATH)
    async def post_push_registration(request: Request) -> dict[str, bool]:
        """ADR 0210: a paired phone registers its APNs device token (and Live Activity token).

        Only a paired device's own token opens it; the local key is refused here as it is on
        ``/inherent/device/events``. A refusal never repeats a value of the body.
        """
        token = bearer_token(request.headers.get("authorization"))
        name = None if token is None else push_name(token)
        if name is None:
            raise HTTPException(
                status_code=403, detail="a paired device's token is required",
            )
        try:
            body = json.loads(await capped_body(request, MAX_REGISTER_BYTES))
        except (ValueError, RecursionError):
            body = None
        try:
            await asyncio.to_thread(push_register, name, body)
        except RegistrationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"ok": True}
