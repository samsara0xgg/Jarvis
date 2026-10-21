"""``POST /inherent/cancel-response`` (ADR-0008 D10): stop a response run, or a turn's open runs."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from fastapi import HTTPException
from pydantic import BaseModel

if TYPE_CHECKING:
    from fastapi import FastAPI

    from jarvis.surface.inherent_server.deps import InherentDeps


class CancelResponseRequest(BaseModel):
    """Body of ``POST /inherent/cancel-response`` (ADR-0008 D10).

    ``scope`` defaults to ``"generation"``, which cancels the run itself.
    ``"foreground_output"`` stops only the speech that is audible now and
    leaves the run to finish on its own; under that scope ``reason`` is
    ignored, because such a stop is always recorded as ``user_stop``. Any
    unrecognized string is answered with ``{"outcome": "unsupported_scope"}``.

    A turn still being thought about has no answer on the wire to name yet
    (its ``open`` leaves once the answer is whole, ADR 0108): ``turn_id`` in
    place of ``response_id`` cancels every run of that turn still open, as
    ``generation``, and answers ``no_open_run`` when none is. A ``user_stop``
    for a turn whose run has not opened yet is kept and applied when it does
    (``stopped_before_start``): the model is never asked.
    """

    response_id: str = ""
    turn_id: str = ""
    scope: str = "generation"
    reason: str = "operator_request"


def register_cancel_route(app: FastAPI, deps: InherentDeps) -> None:
    """``POST /inherent/cancel-response``, when ``deps.cancel_response_callable`` is wired."""
    if deps.cancel_response_callable is None:
        return
    cancel_response_callable = deps.cancel_response_callable

    @app.post("/inherent/cancel-response", status_code=200)
    async def cancel_response(req: CancelResponseRequest) -> dict[str, str]:
        """Request generation cancellation for one ResponseRun.

        Always 200 with an outcome body — an unknown id is
        ``{"outcome": "unknown_response"}``, never a 500, because the
        caller cannot distinguish "already finished" from "never
        existed" and neither is a server fault. The callable is sync
        (it owns a SQLite CAS) and is offloaded via
        ``asyncio.to_thread`` exactly like ``/inherent/submit``, which
        is what keeps the ``BEGIN IMMEDIATE`` off the event loop.
        """
        if req.response_id:
            outcome = await asyncio.to_thread(
                cancel_response_callable,
                req.response_id,
                req.scope,
                req.reason,
            )
        elif req.turn_id and deps.cancel_turn_callable is not None:
            outcome = await asyncio.to_thread(
                deps.cancel_turn_callable, req.turn_id, req.reason,
            )
        else:
            raise HTTPException(status_code=422, detail="response_id or turn_id required")
        return {"outcome": outcome}
