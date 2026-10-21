"""``POST /inherent/reactions`` (ADR 0257): his emoji on a message, or none; never starts a turn."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from pydantic import BaseModel

from jarvis.shared.emoji import is_single_emoji

if TYPE_CHECKING:
    from fastapi import FastAPI

    from jarvis.surface.inherent_server.deps import InherentDeps


class ReactionRequest(BaseModel):
    """Body of ``POST /inherent/reactions`` (ADR 0257): his emoji on one message, or none (null)."""

    target: str
    emoji: str | None


def register_reaction_route(app: FastAPI, deps: InherentDeps) -> None:
    """``POST /inherent/reactions``, when ``deps.reaction_set`` is wired."""
    if deps.reaction_set is None:
        return
    reaction_set = deps.reaction_set

    @app.post("/inherent/reactions", status_code=200)
    async def react(req: ReactionRequest) -> dict[str, Any]:
        """ADR 0257: his emoji on a message, or null to take it off; 400 for anything else.

        The emoji is exactly one grapheme cluster with the emoji property. The target is the
        id of a message record of his or hers: a row of ``GET /inherent/conversation`` that
        is not mail or a picture note. The answer repeats what is on the message now.
        """
        if req.emoji is not None and not is_single_emoji(req.emoji):
            raise HTTPException(
                status_code=400, detail="emoji must be exactly one emoji, or null",
            )
        try:
            await asyncio.to_thread(reaction_set, req.target, req.emoji)
        except LookupError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"target": req.target, "by": "allen", "emoji": req.emoji}
