"""``POST /inherent/imports/{source}`` and ``PUT /inherent/imports/{source}/files/{path}``.

ADR 0294; a paired terminal's scheduled copy of TimeSink and Claude Code sessions, kept raw. Only
a paired device's own token opens them.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from jarvis.state.shipments import MAX_FILE_BYTES, MAX_SHIPMENT_BYTES, ShipRefused, answer
from jarvis.surface.inherent_server.guards import bearer_token, capped_body

if TYPE_CHECKING:
    from fastapi import FastAPI

    from jarvis.surface.inherent_server.deps import InherentDeps


LOGGER = logging.getLogger(__name__)


def register_import_routes(app: FastAPI, deps: InherentDeps) -> None:
    """ADR 0294: a paired terminal's imports, when ``deps.imports_intake`` is wired."""
    if deps.imports_intake is None or deps.device_name is None:
        return
    ship_name, intake = deps.device_name, deps.imports_intake

    def _shipper(request: Request) -> str:
        token = bearer_token(request.headers.get("authorization"))
        name = None if token is None else ship_name(token)
        if name is None:
            raise HTTPException(
                status_code=403, detail="a paired device's token is required",
            )
        if not request.headers.get("content-length", "").isdigit():
            raise HTTPException(status_code=411, detail="send a Content-Length")
        return name

    @app.post("/inherent/imports/{source}")
    async def post_imports(source: str, request: Request) -> JSONResponse:
        """ADR 0294: one JSONL body of a paired terminal's entries, kept raw and read in.

        Only a paired device's own token opens it, as on the lifelog route. 201 when the
        body added entries or was new, 200 when the same bytes were already held; a body with
        a bad line is refused whole (422) and nothing is kept.
        """
        name = _shipper(request)
        body = await capped_body(request, MAX_SHIPMENT_BYTES)
        try:
            got = await asyncio.to_thread(intake.receive_entries, source, body)
        except ShipRefused as exc:
            LOGGER.warning(
                "imports: refused %s from %s with %d: %s", source, name, exc.status, exc,
            )
            raise HTTPException(status_code=exc.status, detail=str(exc)) from None
        LOGGER.info(
            "imports: %s from %s: %d added, %d held (%d bytes)",
            source, name, got.added, got.held, got.size,
        )
        return JSONResponse(answer(got), status_code=200 if got.already_held else 201)

    @app.put("/inherent/imports/{source}/files/{relpath:path}")
    async def put_imports_file(source: str, relpath: str, request: Request) -> JSONResponse:
        """ADR 0294: one picture of a paired terminal, kept whole or not at all.

        201 when new, 200 when the path already holds the same bytes; the terminal advances
        only once the answer's ``sha256`` matches its own.
        """
        name = _shipper(request)
        data = await capped_body(request, MAX_FILE_BYTES)
        try:
            kept = await asyncio.to_thread(intake.receive_file, source, relpath, data)
        except ShipRefused as exc:
            LOGGER.warning(
                "imports: refused a file from %s with %d: %s", name, exc.status, exc,
            )
            raise HTTPException(status_code=exc.status, detail=str(exc)) from None
        return JSONResponse(
            {"sha256": kept.sha256, "bytes": kept.size},
            status_code=201 if kept.created else 200,
        )
