"""``POST /inherent/commitments/{hex}`` — ADR 0274, 0296, 0299.

Body ``{"action": "confirm"|"cancel"}`` confirms the thing ``thing-<hex>`` in the ledger of his
things, or cancels it as not his; ``{"ok": true}``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from fastapi import HTTPException
from pydantic import BaseModel

if TYPE_CHECKING:
    from fastapi import FastAPI

    from jarvis.surface.inherent_server.deps import InherentDeps


LOGGER = logging.getLogger(__name__)


class CommitmentActRequest(BaseModel):
    """Body of ``POST /inherent/commitments/{hex}`` (ADR 0296): confirm it, or cancel it."""

    action: Literal["confirm", "cancel"]


def register_commitment_route(app: FastAPI, deps: InherentDeps) -> None:
    """ADR 0296, 0299: the day line's buttons on a thing: confirm it, or cancel it as not his."""
    if deps.commitment_act is None:
        return
    commitment_act = deps.commitment_act

    @app.post("/inherent/commitments/{commitment_hex}", status_code=200)
    async def commitment(commitment_hex: str, req: CommitmentActRequest) -> dict[str, bool]:
        """``{ok: true}``.

        404 for a thing that is not there or is closed, 409 for a confirm of one that is confirmed
        already, 422 for any other action.
        """
        try:
            changed = await commitment_act(commitment_hex, req.action)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)[:200]) from None
        except Exception as exc:  # noqa: BLE001 — a log that cannot be written is the page's 502.
            LOGGER.warning("commitment route failed: %s: %s", type(exc).__name__, exc)
            raise HTTPException(status_code=502, detail=str(exc)[:200]) from None
        if not changed:
            raise HTTPException(status_code=409, detail="it is confirmed already")
        return {"ok": True}
