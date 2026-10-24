"""Reads of the conversation: its window, whether a turn is thinking, and where the time went.

- ``GET /inherent/conversation`` — spec §18.3; the rows the backend's own history starts from,
  or with ``around`` (ADR 0390) the rows around one message
- ``GET /inherent/think`` — ADR 0108; whether a turn that thinks is under way
- ``GET /inherent/turn-timings`` — ADR 0276; each answered turn's timing facts
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import HTTPException, Query

if TYPE_CHECKING:
    from fastapi import FastAPI

    from jarvis.surface.inherent_server.deps import InherentDeps


_CONVERSATION_MAX_ROWS = 500
"""The most rows one ``GET /inherent/conversation`` page may ask for."""

_TURN_TIMINGS_MAX = 500
"""The most facts one ``GET /inherent/turn-timings`` may ask for."""


def register_conversation_route(app: FastAPI, deps: InherentDeps) -> None:
    """``GET /inherent/conversation``, when ``deps.conversation_read`` is wired."""
    if deps.conversation_read is None:
        return
    conversation_read, around_read = deps.conversation_read, deps.conversation_around

    @app.get("/inherent/conversation")
    async def conversation(
        after: int = 0,
        before: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=_CONVERSATION_MAX_ROWS)] = 200,
        around: Annotated[str, Query(max_length=128)] = "",
    ) -> dict[str, Any]:
        """Spec §18.3: the conversation of record, a page of at most ``limit`` rows.

        ``after`` asks for the rows past that ``seq``, ``before`` for the newest rows older
        than it (ADR 0215); with neither, the newest rows. ``has_more`` says rows exist beyond
        the page in the direction asked. ``after`` with ``before`` is a 400.

        ``around`` (ADR 0390) is a message's record id: the page holds it in its middle, past
        the history floor; ``has_more`` says older rows exist and ``has_newer`` newer ones. A
        record id that names no message is a 404; ``around`` with a cursor is a 400.
        """
        if after and before:
            raise HTTPException(status_code=400, detail="send after or before, not both")
        if not around:
            return conversation_read(after, limit, before)
        if after or before:
            raise HTTPException(status_code=400, detail="send around alone, without a cursor")
        if around_read is None:
            raise HTTPException(status_code=501, detail="around is not served here")
        page = await asyncio.to_thread(around_read, around, limit)
        if page is None:
            raise HTTPException(status_code=404, detail="no message has that id")
        return page


def register_think_route(app: FastAPI, deps: InherentDeps) -> None:
    """``GET /inherent/think``, when ``deps.think_read`` is wired."""
    if deps.think_read is None:
        return
    think_read = deps.think_read

    @app.get("/inherent/think")
    async def think() -> dict[str, Any]:
        """ADR 0108: ``{on, on_words, turn_id}`` for the companion's deep look."""
        return think_read()


def register_turn_timings_route(app: FastAPI, deps: InherentDeps) -> None:
    """ADR 0276: ``GET /inherent/turn-timings``, where each answered turn's time went."""
    if deps.turn_timings_read is None:
        return
    timings_read = deps.turn_timings_read

    @app.get("/inherent/turn-timings")
    async def turn_timings(
        since: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=_TURN_TIMINGS_MAX)] = 50,
    ) -> dict[str, Any]:
        """``{now_ms, turns}``: the newest ``limit`` timing facts from ``since`` (epoch ms)."""
        return await timings_read(since, limit)
