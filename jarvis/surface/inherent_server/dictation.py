"""ADR 0058: dictation from the live mic, one at a time, streamed as NDJSON until its result."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

    from jarvis.surface.inherent_server.deps import InherentDeps


class DictationRoutes(Protocol):
    """ADR 0058 dictation (``jarvis.runtime.dictation.Dictation``)."""

    active: bool

    def begin(self, context: dict[str, str]) -> AsyncIterator[dict[str, Any]]:
        """Start recording; RuntimeError while one is running."""

    def stop(self) -> bool:
        """Finish recording; the running stream goes on to the result."""


class DictationRequest(BaseModel):
    """Body of ``POST /inherent/dictation``: where the words will land, for the polish."""

    app: str = Field(default="", max_length=200)
    window: str = Field(default="", max_length=500)
    selected: str = Field(default="", max_length=20000)
    before: str = Field(default="", max_length=1000)


def dictation_stream(dictation: DictationRoutes, req: DictationRequest) -> StreamingResponse:
    """Record now; stream ``{level}`` lines, ``{state: thinking}``, then the result (ADR 0058).

    A session already running is a 409. A terminal's dictation (ADR 0183) answers the same way.
    """
    try:
        lines = dictation.begin(req.model_dump())
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)[:200]) from None

    async def body() -> AsyncIterator[bytes]:
        async for line in lines:
            yield (json.dumps(line, ensure_ascii=False) + "\n").encode()

    return StreamingResponse(body(), media_type="application/x-ndjson")


def register_dictation_routes(app: FastAPI, deps: InherentDeps) -> None:
    """ADR 0058: one dictation at a time, streamed as NDJSON until its result."""
    if deps.dictation is None:
        return
    dictation = deps.dictation

    @app.post("/inherent/dictation")
    async def dictate(req: DictationRequest) -> StreamingResponse:
        """Record now; stream ``{level}`` lines, ``{state: thinking}``, then the result."""
        return dictation_stream(dictation, req)

    @app.post("/inherent/dictation/stop", status_code=200)
    async def dictate_stop() -> dict[str, bool]:
        """Finish recording; the open stream answers with the result."""
        return {"ok": dictation.stop()}
