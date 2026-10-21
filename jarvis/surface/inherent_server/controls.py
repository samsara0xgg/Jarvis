"""``POST /inherent/controls`` (ADR-0015): the mute switches, the quiet level and GPT-Live."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

if TYPE_CHECKING:
    from fastapi import FastAPI

    from jarvis.surface.inherent_server.deps import InherentDeps


LOGGER = logging.getLogger(__name__)


class ControlsRequest(BaseModel):
    """Body of ``POST /inherent/controls`` (ADR-0015).

    Each field is optional; ``None`` leaves that switch untouched, so ``{}``
    is a pure read the surface uses to sync on connect. The response is
    always the full current state ``{"mic_muted", "speech_muted", "conversation", "quiet"}``.
    """

    mic_muted: bool | None = None
    speech_muted: bool | None = None
    # ADR 0041: the surface's wave mode, listening without a wake word.
    conversation: bool | None = None
    # ADR 0153: the quiet level, the one controls field the daemon keeps across restarts.
    quiet: Literal["off", "quiet", "no-pop", "dnd"] | None = None
    # GPT-Live phase A: ``start`` opens a session (refused with a reason when
    # the ingress or the API key is missing), ``stop`` hangs up.  The response
    # then also carries ``"live": {...}`` (``LiveVoice.status``), on every request.
    live: Literal["start", "stop"] | None = None


def register_controls_route(app: FastAPI, deps: InherentDeps) -> None:  # noqa: C901 — one closed route table.
    """``POST /inherent/controls``, when ``deps.controls`` is wired."""
    if deps.controls is None:
        return
    controls = deps.controls

    @app.post("/inherent/controls", status_code=200)
    async def set_controls(req: ControlsRequest) -> dict[str, object]:
        """Flip the mute switches and drive GPT-Live; answer with the full state.

        Conversation going on to off is the surface's exit: she is stopped at
        once and says nothing back (``dismiss_callable``).
        """
        before = controls.conversation
        state: dict[str, object] = dict(
            controls.update(
                mic_muted=req.mic_muted,
                speech_muted=req.speech_muted,
                conversation=req.conversation,
                quiet=req.quiet,
            ),
        )
        if req.mic_muted is not None or req.speech_muted is not None:
            LOGGER.info(
                "controls: mic_muted=%s speech_muted=%s",
                state["mic_muted"], state["speech_muted"],
            )
        if state["conversation"] != before:
            LOGGER.info("controls: conversation=%s", state["conversation"])
            if before and deps.dismiss_callable is not None:
                try:
                    await asyncio.to_thread(deps.dismiss_callable)
                except Exception:  # noqa: BLE001 - the switch is already flipped
                    LOGGER.warning("controls: dismissal failed", exc_info=True)
        if deps.live is None:
            if req.live is not None:
                state["live"] = {"state": "unavailable", "reason": "gpt_live_disabled"}
            return state
        if req.live == "start":
            state["live"] = await deps.live.start()
        elif req.live == "stop":
            state["live"] = await deps.live.stop(reason="user")
        else:
            state["live"] = deps.live.status()
        return state
