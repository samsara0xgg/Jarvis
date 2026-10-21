"""``GET /inherent/waiting``: everything waiting on Allen in one read, for a paired phone."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from fastapi import Request  # noqa: TC002 — FastAPI reads the route's annotations at runtime.

from jarvis.surface.inherent_server.guards import for_reader, reader_of
from jarvis.surface.inherent_server.notices import no_notices, notices_for

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import FastAPI

    from jarvis.surface.inherent_server.deps import InherentDeps


LOGGER = logging.getLogger(__name__)


def register_waiting_route(  # noqa: C901 — one closed route table.
    app: FastAPI, deps: InherentDeps, read_claude_board: Callable[[], Awaitable[dict[str, Any]]],
) -> None:
    """ADR 0218 and the cards: one read of what waits, answered by each card's own route."""

    @app.get("/inherent/waiting")
    async def waiting(request: Request) -> dict[str, Any]:
        """Everything waiting on Allen, read once; answering stays on each card's own route.

        ``{confirmation, clarification, notices, plugin_request, claude_requests}``: the card of
        ``GET /inherent/confirmation`` and of ``/clarification`` (``card``), the body of
        ``/notices``, ``request`` of ``/plugins``, and each Claude Code session's held prompt
        (the ``request`` of its ``/claude-sessions`` row with the session's id, title and
        project). Nothing waiting is ``null`` or empty, and so is a source this daemon does not
        have. The board is read exactly as ``/claude-sessions`` reads it, so a phone that asks
        for this keeps Claude Code's prompts held.
        """

        async def card(read: Callable[[], dict[str, Any]] | None) -> dict[str, Any] | None:
            found = None if read is None else (await asyncio.to_thread(read)).get("card")
            return for_reader(found, reader_of(deps, request))

        async def notices() -> dict[str, Any]:
            if deps.notices_read is None:
                return no_notices()
            return notices_for(await deps.notices_read(), reader_of(deps, request))

        async def plugin_request() -> dict[str, Any] | None:
            if deps.plugin_read is None or deps.plugin_authorize is None:
                return None
            if not deps.plugin_authorize(request.headers.get("authorization")):
                return None
            found = (await asyncio.to_thread(deps.plugin_read)).get("request")
            return found if isinstance(found, dict) else None

        async def claude_requests() -> list[dict[str, Any]]:
            rows = (await read_claude_board())["sessions"]
            return [
                {
                    **row["request"],
                    "session_id": row["session_id"],
                    "title": row.get("title", ""),
                    "project": row.get("project", ""),
                }
                for row in rows
                if row.get("request")
            ]

        async def source[T](name: str, read: Awaitable[T], empty: T) -> T:
            # One source down (a mail or Microsoft call) leaves the rest of what waits readable.
            try:
                return await read
            except Exception as exc:  # noqa: BLE001 — a source failing is an empty one, logged.
                LOGGER.warning("waiting: %s unreadable: %s: %s", name, type(exc).__name__, exc)
                return empty

        confirmation, clarification, notice_body, plugin, claude = await asyncio.gather(
            source("confirmation", card(deps.card_read), None),
            source("clarification", card(deps.question_read), None),
            source("notices", notices(), no_notices()),
            source("plugin request", plugin_request(), None),
            claude_requests(),
        )
        return {
            "confirmation": confirmation,
            "clarification": clarification,
            "notices": notice_body,
            "plugin_request": plugin,
            "claude_requests": claude,
        }
