"""First-run setup: status, names, a key to test and keep, a voice preview, done."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import HTTPException, Request
from fastapi.responses import Response

from jarvis.surface.inherent_server.calls import home_call

if TYPE_CHECKING:
    from fastapi import FastAPI

    from jarvis.surface.inherent_server.deps import InherentDeps


class SetupRoutes(Protocol):
    """What first-run setup serves (``jarvis.runtime.setup.Setup``)."""

    def read(self) -> dict[str, Any]:
        """``GET /inherent/setup``."""

    def save_names(self, body: dict[str, Any]) -> dict[str, Any]:
        """``POST /inherent/setup/name``."""

    def check_key(self, body: dict[str, Any]) -> dict[str, Any]:
        """``POST /inherent/setup/key``."""

    def preview(self, body: dict[str, Any]) -> bytes:
        """``POST /inherent/setup/voice-preview``."""

    def done(self) -> dict[str, Any]:
        """``POST /inherent/setup/done``."""


_MAX_SETUP_BODY_BYTES = 4096


async def _setup_body(request: Request) -> dict[str, Any]:
    """A setup request's JSON object; no echo of it in errors (it can hold an API key)."""
    raw = await request.body()
    if len(raw) > _MAX_SETUP_BODY_BYTES:
        raise HTTPException(status_code=413, detail="setup request too large")
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="send a JSON object")
    return body


def register_setup_routes(app: FastAPI, deps: InherentDeps) -> None:
    """First-run setup: status, names, a key to test and keep, a voice preview, done."""
    if deps.setup is None:
        return
    setup = deps.setup

    @app.get("/inherent/setup")
    async def setup_status() -> dict[str, Any]:
        """Whether setup has run, what it saved, which keys work, what the user can use."""
        return await asyncio.to_thread(setup.read)

    @app.post("/inherent/setup/name")
    async def setup_names(request: Request) -> dict[str, Any]:
        """``{name?, assistant_name?}``; answers like ``GET``."""
        body = await _setup_body(request)
        return await home_call(asyncio.to_thread(setup.save_names, body))

    @app.post("/inherent/setup/key")
    async def setup_key(request: Request) -> dict[str, Any]:
        """``{provider, key}``: run its checks, keep the key in the Keychain only when it works."""
        body = await _setup_body(request)
        return await home_call(asyncio.to_thread(setup.check_key, body))

    @app.post("/inherent/setup/voice-preview")
    async def setup_preview(request: Request) -> Response:
        """``{voice_id, text?}``: that voice saying the line, as MP3."""
        body = await _setup_body(request)
        audio = await home_call(asyncio.to_thread(setup.preview, body))
        return Response(content=audio, media_type="audio/mpeg")

    @app.post("/inherent/setup/done")
    async def setup_done() -> dict[str, Any]:
        """Setup will not show again; the daemon restarts to take in keys and names."""
        return setup.done()
