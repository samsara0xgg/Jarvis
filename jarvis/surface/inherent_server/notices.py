"""The notch's notices and cards (ADR 0155, 0160, 0163): the alerts, the hold, his taps on them."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from fastapi import Request  # noqa: TC002 — FastAPI reads the route's annotations at runtime.
from pydantic import BaseModel, Field

from jarvis.surface.inherent_server.calls import home_call
from jarvis.surface.inherent_server.guards import for_reader, reader_of

if TYPE_CHECKING:
    from fastapi import FastAPI

    from jarvis.surface.inherent_server.deps import InherentDeps


class NoticeActionRequest(BaseModel):
    """Body of ``POST /inherent/notices/{id}`` (ADR 0155): ``seen``, or feedback with a reaction."""

    action: Literal["seen", "feedback", "dismissed"]
    reaction: str | None = Field(default=None, max_length=50)


class CardActionRequest(BaseModel):
    """Body of ``POST /inherent/cards/{id}`` (ADR 0160): ``seen`` with a snapshot, or feedback."""

    action: Literal["seen", "feedback", "dismissed"]
    reaction: str | None = Field(default=None, max_length=50)
    kind: str | None = Field(default=None, max_length=24)
    level: str | None = Field(default=None, max_length=24)
    facts: dict[str, Any] = Field(default_factory=dict)
    situation: dict[str, Any] = Field(default_factory=dict)


def notices_for(body: dict[str, Any], reader: str) -> dict[str, Any]:
    """``body`` with only the notices ``reader`` may be shown: its own and those of no device."""
    shown = [one for one in body.get("notices", []) if for_reader(one, reader) is not None]
    return {**body, "notices": shown}


def no_notices() -> dict[str, Any]:
    """The ``GET /inherent/notices`` body of a daemon with nothing to show."""
    return {"notices": [], "audio_private": False, "hold": None, "departure": None}


def register_notice_routes(app: FastAPI, deps: InherentDeps) -> None:
    """ADR 0155, 0160, 0163: the notices, the moment hold and the taps on notices and cards."""
    if deps.notices_read is not None:
        notices_read = deps.notices_read

        @app.get("/inherent/notices")
        async def notices(request: Request) -> dict[str, Any]:
            """``{notices, audio_private, hold}``: the job-mail alerts a client may show now."""
            return notices_for(await home_call(notices_read()), reader_of(deps, request))

    if deps.moment_read is not None:
        moment_read = deps.moment_read

        @app.get("/inherent/moment")
        async def moment() -> dict[str, Any]:
            """``{hold}``: ``call``, ``away`` or None; the client's own cards and sounds wait."""
            return await home_call(moment_read())

    if deps.notice_act is not None:
        notice_act = deps.notice_act

        @app.post("/inherent/notices/{notice_id}", status_code=200)
        async def notice_action(
            notice_id: str, req: NoticeActionRequest,
        ) -> dict[str, bool | str]:
            """Mark a notice seen or record Allen's reaction; a digest id covers its alerts.

            A settings notice (ADR 0227) that took the terminal's values answers ``restart``:
            ``idle`` (the daemon restarts once voice is quiet) or ``manual`` (restart it).
            """
            restart = await home_call(notice_act(notice_id, req.action, req.reaction))
            return {"ok": True, **({"restart": restart} if restart else {})}

    if deps.card_act is not None:
        card_act = deps.card_act

        @app.post("/inherent/cards/{card_id}", status_code=200)
        async def card_action(card_id: str, req: CardActionRequest) -> dict[str, bool]:
            """Store a shown card's snapshot (``seen``) or Allen's reaction to it."""
            await home_call(card_act(card_id, req.model_dump()))
            return {"ok": True}
