"""The cards waiting for Allen's button: the confirmation card and the ask card.

- ``GET`` and ``POST /inherent/confirmation`` — ADR 0062; send or dismiss the card on screen
- ``GET`` and ``POST /inherent/clarification`` — ADR 0066; fill in or dismiss the ask card

A card's ``device`` is the one whose turn asked it (ADR 0218): only that device's reads are
served it.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from jarvis.surface.inherent_server.guards import for_reader, reader_of

if TYPE_CHECKING:
    from fastapi import FastAPI

    from jarvis.surface.inherent_server.deps import InherentDeps


class QuestionAnswerRequest(BaseModel):
    """ADR 0066: the ask card's answers by field label, or ``dismiss`` for its close button."""

    clarification_id: str
    answers: dict[str, str] = Field(default_factory=dict)
    dismiss: bool = False


class CardDecisionRequest(BaseModel):
    """ADR 0062: Allen's answer to the card he sees, by its id; edits are the card's text fields."""

    confirmation_id: str
    decision: Literal["accept", "reject"]
    edits: dict[str, str] = Field(default_factory=dict)


def register_card_routes(app: FastAPI, deps: InherentDeps) -> None:
    """ADR 0062, 0066: the pending confirmation card and ask card, and the answers to them."""
    if deps.card_read is not None and deps.card_decide is not None:
        card_read, card_decide = deps.card_read, deps.card_decide

        @app.get("/inherent/confirmation")
        async def confirmation_card(request: Request) -> dict[str, Any]:
            """ADR 0062: the pending card, or ``{"card": null}``; another device's is null."""
            card = (await asyncio.to_thread(card_read)).get("card")
            return {"card": for_reader(card, reader_of(deps, request))}

        @app.post("/inherent/confirmation", status_code=200)
        async def confirmation_answer(req: CardDecisionRequest, request: Request) -> dict[str, str]:
            """ADR 0062: send or dismiss the card on screen; 409 once it is not the pending one."""
            card = (await asyncio.to_thread(card_read)).get("card")
            if not card or card.get("id") != req.confirmation_id:
                raise HTTPException(status_code=409, detail="that card is no longer waiting")
            turn_id = await asyncio.to_thread(
                card_decide, req.confirmation_id, req.decision, dict(req.edits),
                reader_of(deps, request),
            )
            return {"status": "accepted", "turn_id": turn_id}

    if deps.question_read is not None and deps.question_answer is not None:
        question_read, question_answer = deps.question_read, deps.question_answer

        @app.get("/inherent/clarification")
        async def clarification_card(request: Request) -> dict[str, Any]:
            """ADR 0066: the ask card to fill in, or ``{"card": null}``; another device's: null."""
            card = (await asyncio.to_thread(question_read)).get("card")
            return {"card": for_reader(card, reader_of(deps, request))}

        @app.post("/inherent/clarification", status_code=200)
        async def clarification_answer(
            req: QuestionAnswerRequest, request: Request,
        ) -> dict[str, Any]:
            """ADR 0066: fill in or dismiss the ask card; 409 once it is not the one waiting."""
            if not req.dismiss and not any(v.strip() for v in req.answers.values()):
                raise HTTPException(status_code=400, detail="nothing was filled in")
            try:
                turn_id = await asyncio.to_thread(
                    question_answer,
                    req.clarification_id,
                    None if req.dismiss else dict(req.answers),
                    reader_of(deps, request),
                )
            except LookupError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return {"status": "dismissed" if turn_id is None else "accepted", "turn_id": turn_id}
