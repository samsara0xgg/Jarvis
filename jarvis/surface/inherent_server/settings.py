"""The Settings page (ADR 0052): its file, the reSpeaker board's status and Restart."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from jarvis.surface.inherent_server.calls import home_call

if TYPE_CHECKING:
    from fastapi import FastAPI

    from jarvis.surface.inherent_server.deps import InherentDeps


class SettingsRequest(BaseModel):
    """Body of ``POST /inherent/settings`` (ADR 0052): the page's changed values by key."""

    changes: dict[str, Any]


def register_settings_routes(app: FastAPI, deps: InherentDeps) -> None:
    """ADR 0052, 0147: the Settings page's file, the board and the restart, each only when wired."""
    if deps.settings_read is not None and deps.settings_update is not None:
        settings_read, settings_update = deps.settings_read, deps.settings_update

        @app.get("/inherent/settings")
        async def settings() -> dict[str, Any]:
            """``{values, options, restart_pending}`` for the Settings page."""
            return await settings_read()

        @app.post("/inherent/settings", status_code=200)
        async def settings_save(req: SettingsRequest) -> dict[str, Any]:
            """Save the changed values for the next boot; answers like ``GET``."""
            return await home_call(settings_update(req.changes))

    if deps.board_status is not None:
        board_status = deps.board_status

        @app.get("/inherent/board")
        async def board() -> dict[str, Any]:
            """The reSpeaker board as the Settings page shows it (ADR 0147)."""
            return await board_status()

    if deps.restart is not None:
        restart = deps.restart

        @app.post("/inherent/restart", status_code=202)
        async def restart_now() -> dict[str, bool]:
            """Settings > Restart: the answer leaves first, then launchd brings Jarvis back."""
            restart()
            return {"ok": True}
