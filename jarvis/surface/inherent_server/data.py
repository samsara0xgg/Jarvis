"""ADR 0067, Privacy & data: take the data out as a zip, clear the recordings, erase all."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

if TYPE_CHECKING:
    from fastapi import FastAPI

    from jarvis.surface.inherent_server.deps import InherentDeps


def register_data_routes(app: FastAPI, deps: InherentDeps) -> None:
    """ADR 0067: take the data out as a zip, clear the recordings, erase everything."""
    if deps.data_export is not None:
        data_export = deps.data_export

        @app.get("/inherent/data/export")
        async def data_export_zip() -> FileResponse:
            """One zip of the user's data, as a download; the temp file goes once it is sent."""
            path = await data_export()
            return FileResponse(
                path, media_type="application/zip",
                filename=f"Jarvis-export-{time.strftime('%Y-%m-%d')}.zip",
                background=BackgroundTask(path.unlink, missing_ok=True),
            )

    if deps.data_clear is not None:
        data_clear = deps.data_clear

        @app.post("/inherent/data/clear-recordings", status_code=200)
        async def data_clear_recordings() -> dict[str, int]:
            """Delete every recording and screenshot now; the conversations stay."""
            return {"deleted": await data_clear()}

    if deps.data_erase is not None:
        data_erase = deps.data_erase

        @app.post("/inherent/data/erase", status_code=202)
        async def data_erase_all() -> dict[str, bool]:
            """Everything but the speech models goes at the restart this starts."""
            data_erase()
            return {"ok": True}
