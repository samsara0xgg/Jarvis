"""The desktop management routes: plugins and the language switch (ADR 0038, 0202).

They take the local key or a paired device's token (``guards.manager_authorize``), unlike
ordinary text submission. Secrets never travel on the public websocket.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request

from jarvis.shared.lang import language, t

if TYPE_CHECKING:
    from fastapi import FastAPI

    from jarvis.surface.inherent_server.deps import InherentDeps


_MAX_PLUGIN_COMMAND_BYTES = 32_768


def register_manager_routes(app: FastAPI, deps: InherentDeps) -> None:  # noqa: C901 — one closed route table.
    """ADR 0038, 0202: the plugin routes and the language switch, each only when wired."""
    if deps.plugin_read and deps.plugin_action and deps.plugin_authorize:
        plugin_read, plugin_action, plugin_authorize = (
            deps.plugin_read,
            deps.plugin_action,
            deps.plugin_authorize,
        )

        @app.get("/inherent/plugins")
        async def plugins(request: Request) -> dict[str, Any]:
            if not plugin_authorize(request.headers.get("authorization")):
                raise HTTPException(status_code=401, detail="desktop authorization required")
            return await asyncio.to_thread(plugin_read)

        if deps.plugin_icon is not None:
            plugin_icon = deps.plugin_icon

            @app.get("/inherent/plugins/{plugin_id}/icon")
            async def plugin_logo(plugin_id: str, request: Request) -> dict[str, str | None]:
                if not plugin_authorize(request.headers.get("authorization")):
                    raise HTTPException(status_code=401, detail="desktop authorization required")
                return {"icon": await asyncio.to_thread(plugin_icon, plugin_id)}

        @app.post("/inherent/plugins/action")
        async def plugin_command(request: Request) -> dict[str, Any]:
            if not plugin_authorize(request.headers.get("authorization")):
                raise HTTPException(status_code=401, detail="desktop authorization required")
            body = await request.body()
            if len(body) > _MAX_PLUGIN_COMMAND_BYTES:
                raise HTTPException(status_code=413, detail="plugin command too large")
            try:
                payload = json.loads(body)
                if not isinstance(payload, dict) or not isinstance(payload.get("data", {}), dict):
                    raise HTTPException(status_code=400, detail="invalid plugin request")
                return await asyncio.to_thread(
                    plugin_action, str(payload.get("operation", "")), payload.get("data", {})
                )
            except ValueError as exc:
                # No Pydantic error echo: request bodies can contain credentials.
                bad_json = isinstance(exc, json.JSONDecodeError)
                detail = t("plugin.bad_request") if bad_json else str(exc)
                raise HTTPException(status_code=400, detail=detail) from None

    if deps.language_save is not None and deps.plugin_authorize is not None:
        language_save, language_authorize = deps.language_save, deps.plugin_authorize

        @app.get("/inherent/language")
        async def language_read() -> dict[str, str]:
            """The language Jarvis speaks and writes its fixed text in."""
            return {"language": language()}

        @app.post("/inherent/language", status_code=200)
        async def language_write(request: Request) -> dict[str, str]:
            """Switch to ``{"language": "zh" | "en"}`` now and keep it in settings.yaml."""
            if not language_authorize(request.headers.get("authorization")):
                raise HTTPException(status_code=401, detail="desktop authorization required")
            try:
                code = json.loads(await request.body()).get("language")
                chosen = await asyncio.to_thread(language_save, str(code))
            except (ValueError, AttributeError) as exc:
                raise HTTPException(status_code=400, detail="language must be zh or en") from exc
            return {"language": chosen}
