"""ADR 0093: the night run as the companion reads and drives it."""

from __future__ import annotations

import asyncio
import functools
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from datetime import time as clock

    from fastapi import FastAPI


class NightRoutes(Protocol):
    """ADR 0093 night run (``jarvis.runtime.night_run.NightRun``); every call may block briefly."""

    def snapshot(self) -> dict[str, Any]:
        """``GET /inherent/night``."""

    def start(
        self, *, hours: float | None, until: clock | None, source: str, action_id: str | None,
    ) -> dict[str, Any]:
        """Start a run, or answer the one already running."""

    def darken_now(self) -> None:
        """Dim, mute and sleep the display now instead of after the bedtime card."""

    def stay(self) -> None:
        """The owner goes to answer a session: dark waits for a quiet minute."""

    def end(self, *, action_id: str | None) -> dict[str, Any]:
        """End the run: put back what it changed."""


class NightRequest(BaseModel):
    """Body of ``POST /inherent/night``: start one (for ``hours``), go dark now, stay, or end it."""

    action: Literal["start", "dark", "stay", "end"]
    hours: float | None = Field(default=None, ge=0.25, le=12)


def register_night_routes(app: FastAPI, run: NightRoutes | None) -> None:
    """ADR 0093: the night run as the companion reads and drives it; none without a run."""
    if run is None:
        return
    night = run

    @app.get("/inherent/night")
    async def night_read() -> dict[str, Any]:
        """The run now (or null), the last one that ended, the default length."""
        return await asyncio.to_thread(night.snapshot)

    @app.post("/inherent/night", status_code=200)
    async def night_act(req: NightRequest) -> dict[str, Any]:
        """Start, go dark now, stay lit while the owner answers a session, or end; like ``GET``."""
        if req.action == "start":
            await asyncio.to_thread(
                functools.partial(
                    night.start, hours=req.hours, until=None, source="companion", action_id=None,
                ),
            )
        elif req.action == "dark":
            await asyncio.to_thread(night.darken_now)
        elif req.action == "stay":
            await asyncio.to_thread(night.stay)
        else:
            await asyncio.to_thread(functools.partial(night.end, action_id=None))
        return await asyncio.to_thread(night.snapshot)
