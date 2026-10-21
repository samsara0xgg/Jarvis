"""The phone's recording of his day: ``PUT /inherent/lifelog/{audio,motion}/{id}`` and coverage.

- ``PUT /inherent/lifelog/audio/{id}`` — ADR 0238; one piece of the phone's day recording, kept
- ``PUT /inherent/lifelog/motion/{id}`` — ADR 0312; one piece of the phone's device motion
  while he rode, kept in the same archive
- ``GET /inherent/lifelog/coverage`` — ADR 0292; whether a connected terminal is recording his
  room now, and his saved home, for the phone to decide by

Only a paired device's own token opens them.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import HTTPException, Query, Request
from fastapi.responses import JSONResponse

from jarvis.state.lifelog import MAX_PIECE_BYTES, PieceRefused
from jarvis.surface.inherent_server.guards import bearer_token, capped_body

if TYPE_CHECKING:
    from fastapi import FastAPI

    from jarvis.surface.inherent_server.deps import InherentDeps


LOGGER = logging.getLogger(__name__)


def register_lifelog_routes(app: FastAPI, deps: InherentDeps) -> None:  # noqa: C901 — one closed route table.
    """ADR 0238, 0312, 0292: the recording pieces a paired phone keeps, and the coverage facts."""
    if deps.lifelog is not None and deps.device_name is not None:
        lifelog_name, lifelog = deps.device_name, deps.lifelog
        savers = {"audio": lifelog.save_audio, "motion": lifelog.save_motion}

        async def _keep_piece(
            kind: str, piece_id: str, request: Request, started_ms: int, duration_ms: int,
        ) -> JSONResponse:
            """ADR 0238, 0312: one closed piece of a paired phone's recording, kept for good.

            Only a paired device's own token opens it, as on ``/inherent/device/events``. 201 when
            the piece is new, 200 when it was already kept with the same bytes; the phone deletes
            its copy only once the answer's ``sha256`` matches its own.
            """
            token = bearer_token(request.headers.get("authorization"))
            name = None if token is None else lifelog_name(token)
            if name is None:
                raise HTTPException(
                    status_code=403, detail="a paired device's token is required",
                )
            if not request.headers.get("content-length", "").isdigit():
                raise HTTPException(status_code=411, detail="send a Content-Length")
            data = await capped_body(request, MAX_PIECE_BYTES)
            try:
                kept = await asyncio.to_thread(
                    savers[kind], name, piece_id, started_ms, duration_ms, data,
                )
            except PieceRefused as exc:
                LOGGER.warning(
                    "lifelog: refused %s from %s with %d: %s", kind, name, exc.status, exc,
                )
                raise HTTPException(status_code=exc.status, detail=str(exc)) from None
            LOGGER.info(
                "lifelog: %s %d s of %s from %s (%d bytes)",
                "kept" if kept.created else "already had", duration_ms // 1000, kind, name,
                kept.size,
            )
            return JSONResponse(
                {"id": kept.id, "sha256": kept.sha256, "bytes": kept.size},
                status_code=201 if kept.created else 200,
            )

        @app.put("/inherent/lifelog/audio/{piece_id}")
        async def put_lifelog_audio(
            piece_id: str,
            request: Request,
            started_ms: Annotated[int, Query()],
            duration_ms: Annotated[int, Query()],
        ) -> JSONResponse:
            """ADR 0238: one closed piece of a paired phone's day recording."""
            return await _keep_piece("audio", piece_id, request, started_ms, duration_ms)

        @app.put("/inherent/lifelog/motion/{piece_id}")
        async def put_lifelog_motion(
            piece_id: str,
            request: Request,
            started_ms: Annotated[int, Query()],
            duration_ms: Annotated[int, Query()],
        ) -> JSONResponse:
            """ADR 0312: one closed piece of the phone's device motion while he rode."""
            return await _keep_piece("motion", piece_id, request, started_ms, duration_ms)

    if deps.lifelog_coverage is not None and deps.device_name is not None:
        coverage_name, coverage_read = deps.device_name, deps.lifelog_coverage

        @app.get("/inherent/lifelog/coverage")
        async def get_lifelog_coverage(request: Request) -> dict[str, Any]:
            """ADR 0292: what the phone needs to decide whether to keep its own audio.

            Only a paired device's own token opens it, as on ``/inherent/lifelog/audio/{id}``.
            Facts only: ``{now_ms, mac_recording, terminal, newest_piece_end_ms, home}``.
            """
            token = bearer_token(request.headers.get("authorization"))
            if token is None or coverage_name(token) is None:
                raise HTTPException(
                    status_code=403, detail="a paired device's token is required",
                )
            return await asyncio.to_thread(coverage_read)
