"""``POST /inherent/ask``: a one-shot question answered with the reply.

Body ``{"text": str}`` → ``{"turn_id", "spoken", "written"}`` once that turn's final answer is in
(504 / 502 otherwise), for a one-shot caller (Siri).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response

from jarvis.surface.inherent_server.guards import capped_body, paired_device
from jarvis.surface.inherent_server.submit import start_turn

if TYPE_CHECKING:
    from fastapi import FastAPI

    from jarvis.surface.inherent_server.deps import InherentDeps


# ``POST /inherent/ask``: the biggest body and question, how long the answer is waited for, and
# how often a wait re-reads the log without a wake-up (``response.failed`` never reaches the wire).
_ASK_BODY_BYTES: Final = 4096
_ASK_TEXT_CHARS: Final = 2000
_ASK_WAIT_S: Final[float] = 25.0
_ASK_RECHECK_S: Final = 1.0


@dataclass(frozen=True)
class AskOutcome:
    """How a turn ended for ``POST /inherent/ask``: its final answer, or the reason it has none."""

    spoken: str = ""
    written: str = ""
    failure: str | None = None


def register_ask_route(app: FastAPI, deps: InherentDeps) -> None:
    """``POST /inherent/ask``, when ``deps.ask_outcome`` is wired."""
    if deps.ask_outcome is None:
        return
    ask_outcome = deps.ask_outcome

    @app.post("/inherent/ask", status_code=200)
    async def ask(request: Request) -> Response:
        """A one-shot question: submit it like ``/inherent/submit``, answer with the reply.

        ``{"text"}`` (at most 2000 characters in a body of at most 4 KiB) is typed in, so the
        turn is never spoken on the Mac. Answers 200 ``{turn_id, spoken, written}`` from the
        turn's final answer (a wait line is not it), 502 ``{turn_id, detail}`` when the turn
        fails or is cancelled, and 504 ``{turn_id, detail}`` after 25 s; that turn keeps
        running and is in the conversation.
        """
        try:
            body = json.loads(await capped_body(request, _ASK_BODY_BYTES) or b"{}")
        except ValueError:  # no echo of the body in an error
            body = None
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(status_code=400, detail="text required")
        text = text.strip()
        if len(text) > _ASK_TEXT_CHARS:
            raise HTTPException(
                status_code=400, detail=f"text is over {_ASK_TEXT_CHARS} characters",
            )
        # Watch before submitting, so an answer sent at once still wakes the wait.
        device = paired_device(deps, request)
        with deps.broadcaster.watch_settled() as settled:
            turn_id = await start_turn(deps, device, text)
            deadline = time.monotonic() + _ASK_WAIT_S
            while True:
                settled.clear()  # before the read: an answer written meanwhile sets it again
                outcome = await asyncio.to_thread(ask_outcome, turn_id)
                if outcome is not None:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return JSONResponse(
                        {"turn_id": turn_id, "detail": "no answer yet; the turn goes on"},
                        status_code=504,
                    )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(settled.wait(), min(remaining, _ASK_RECHECK_S))
        if outcome.failure is not None:
            return JSONResponse(
                {"turn_id": turn_id, "detail": outcome.failure}, status_code=502,
            )
        return JSONResponse(
            {"turn_id": turn_id, "spoken": outcome.spoken, "written": outcome.written},
        )
