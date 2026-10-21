"""One day as the phone and Dashboard read it: the timeline, track, footprint and his marks.

- ``GET /inherent/day?date=`` — ADR 0199; one day as timed items, open to every admitted caller
- ``GET /inherent/track?from_ms=&to_ms=`` — ADR 0312; every fix the phones kept in a window of at
  most 12 hours, per device, as rows of :data:`jarvis.state.phone_track.COLUMNS`
- ``GET /inherent/footprint?date=`` — ADR 0323; his footprint of a day: the long stays and the
  outings between them, each with its route, stops, distance, speeds and heart rate
- ``POST /inherent/day/highlight`` — ADR 0296; body ``{"day", "id", "on", "label"?}`` marks or
  unmarks one item of a day as a highlight for him; ``{"ok", "highlights"}``
- ``POST /inherent/day/answer`` — ADR 0302; body ``{"day", "span_id", "choice"?, "text"?}`` is
  his answer about a span of what he was doing; ``{"ok", "span"}``
- ``POST /inherent/day/tidy`` — ADR 0310; no body: today's newest part of the doing lane is
  written now; ``{ok, written, outcome}``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Any, Final

from fastapi import HTTPException, Query
from pydantic import BaseModel, Field, StrictBool, StrictInt, field_validator, model_validator

from jarvis.state.day_highlights import LABEL_MAX_CHARS, HighlightCapReached
from jarvis.state.day_line import parse_day
from jarvis.state.doing import ANSWER_TEXT_MAX_CHARS
from jarvis.surface.inherent_server.calls import home_call

if TYPE_CHECKING:
    from fastapi import FastAPI

    from jarvis.surface.inherent_server.deps import InherentDeps


LOGGER = logging.getLogger(__name__)


def register_day_route(app: FastAPI, deps: InherentDeps) -> None:
    """ADR 0199: ``GET /inherent/day``, one day as timed items; open to every admitted caller."""
    if deps.day_read is None:
        return
    day_read = deps.day_read

    @app.get("/inherent/day")
    async def day(on: Annotated[str | None, Query(alias="date")] = None) -> dict[str, Any]:
        """``{date, tz, start_ms, end_ms, now_ms, items, labels, missing, line, highlights, ...}``.

        ``spans`` and ``coverage`` (ADR 0302) are the day's fact spans and what he was doing.
        """
        try:
            wanted = None if on is None else parse_day(on)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return await home_call(day_read(wanted))


def register_track_route(app: FastAPI, deps: InherentDeps) -> None:
    """ADR 0312: ``GET /inherent/track``, the dense track over a window; open as the day is."""
    if deps.track_read is None:
        return
    track_read = deps.track_read

    @app.get("/inherent/track")
    async def track(
        from_ms: Annotated[int, Query(ge=0)], to_ms: Annotated[int, Query(ge=0)],
    ) -> dict[str, Any]:
        """``{from_ms, to_ms, columns, tracks: [{device, fixes}]}``, fixes oldest first."""
        try:
            return await track_read(from_ms, to_ms)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None


def register_footprint_route(app: FastAPI, deps: InherentDeps) -> None:
    """ADR 0323: ``GET /inherent/footprint``, his footprint of a day; open as the day is."""
    if deps.footprint_read is None:
        return
    footprint_read = deps.footprint_read

    @app.get("/inherent/footprint")
    async def footprint(on: Annotated[str | None, Query(alias="date")] = None) -> dict[str, Any]:
        """``{date, tz, start_ms, end_ms, now_ms, stays, outings}`` for the day asked, or today."""
        try:
            wanted = None if on is None else parse_day(on)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return await home_call(footprint_read(wanted))


_ITEM_ID: Final = r"^[A-Za-z0-9_\-=+/.|:@]{1,256}$"
"""The id of a day-line item: ``jarvis.shared.about``'s id set."""


class HighlightRequest(BaseModel):
    """Body of ``POST /inherent/day/highlight`` (ADR 0296): his mark on one item of a day."""

    day: str = Field(min_length=10, max_length=10, pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    id: str = Field(pattern=_ITEM_ID)
    on: StrictBool
    label: str | None = Field(default=None, max_length=200)

    @field_validator("label")
    @classmethod
    def _label_fits(cls, value: str | None) -> str | None:
        label = (value or "").strip() or None
        if label is not None and len(label) > LABEL_MAX_CHARS:
            msg = f"label is at most {LABEL_MAX_CHARS} characters"
            raise ValueError(msg)
        return label


class DoingAnswerRequest(BaseModel):
    """Body of ``POST /inherent/day/answer`` (ADR 0302): his answer about a stretch of a day.

    Either a served span (``span_id`` with ``choice`` or ``text``) or a stretch he names
    himself (``start_ms`` and ``end_ms`` with ``text``).
    """

    day: str = Field(min_length=10, max_length=10, pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    span_id: str | None = Field(default=None, pattern=_ITEM_ID)
    start_ms: StrictInt | None = None
    end_ms: StrictInt | None = None
    choice: str | None = Field(default=None, min_length=1, max_length=ANSWER_TEXT_MAX_CHARS)
    text: str | None = None

    @field_validator("text")
    @classmethod
    def _text_fits(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not 1 <= len(text) <= ANSWER_TEXT_MAX_CHARS:
            msg = f"text is 1 to {ANSWER_TEXT_MAX_CHARS} characters"
            raise ValueError(msg)
        return text

    @model_validator(mode="after")
    def _one_answer(self) -> DoingAnswerRequest:
        ranged = self.start_ms is not None or self.end_ms is not None
        if ranged:
            if self.span_id is not None or self.choice is not None:
                msg = "a stretch by start_ms and end_ms takes text, not span_id or choice"
                raise ValueError(msg)
            if self.start_ms is None or self.end_ms is None or self.end_ms <= self.start_ms:
                msg = "give start_ms and end_ms, the end after the start"
                raise ValueError(msg)
            if self.text is None:
                msg = "a stretch by start_ms and end_ms takes text"
                raise ValueError(msg)
            return self
        if self.span_id is None:
            msg = "give span_id, or start_ms and end_ms"
            raise ValueError(msg)
        if (self.choice is None) == (self.text is None):
            msg = "give exactly one of choice and text"
            raise ValueError(msg)
        return self


def register_highlight_route(app: FastAPI, deps: InherentDeps) -> None:
    """ADR 0296: he marks or unmarks an item of a day as one of its highlights."""
    if deps.highlight_set is None:
        return
    highlight_set = deps.highlight_set

    @app.post("/inherent/day/highlight", status_code=200)
    async def day_highlight(req: HighlightRequest) -> dict[str, Any]:
        """``{ok, highlights}``: the day's highlights after his mark.

        422 for a day that is not one or an id or label of the wrong shape; 404 when no item has
        the id on that day; 409 when five of his marks are standing and this would be a sixth
        (unmark one first; her picks give way to his, his are never dropped). Marking what is
        marked, or unmarking what is not, changes nothing and answers 200.
        """
        try:
            on_day = parse_day(req.day)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)[:200]) from None
        try:
            highlights = await highlight_set(on_day, req.id, req.on, req.label)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)[:200]) from None
        except HighlightCapReached as exc:
            raise HTTPException(status_code=409, detail=str(exc)[:200]) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)[:200]) from None
        except Exception as exc:  # noqa: BLE001 — a store that cannot be written is the page's 502.
            LOGGER.warning("highlight route failed: %s: %s", type(exc).__name__, exc)
            raise HTTPException(status_code=502, detail=str(exc)[:200]) from None
        return {"ok": True, "highlights": highlights}


def register_doing_answer_route(app: FastAPI, deps: InherentDeps) -> None:
    """ADR 0302: he says what a stretch of a day was, or picks from the span's question."""
    if deps.doing_answer is None:
        return
    doing_answer = deps.doing_answer

    @app.post("/inherent/day/answer", status_code=200)
    async def day_answer(req: DoingAnswerRequest) -> dict[str, Any]:
        """``{ok, span}``: the span as now served, with his words and ``confirmed``.

        422 for a day that is not one, an id of the wrong shape, neither or both of ``choice``
        and ``text``, a ``text`` that is empty or over 200 characters once trimmed, or a
        ``choice`` the span's question does not offer (or a span with no question); 404 when the
        id is not a served, unplanned span of what he was doing on that day. A stretch he names
        (``start_ms``, ``end_ms``, ``text``) is 422 when it mixes with ``span_id`` or ``choice``,
        ends before it starts, leaves the day or touches none of the lane; it is never 404.
        """
        try:
            on_day = parse_day(req.day)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)[:200]) from None
        try:
            stretch = (
                None if req.start_ms is None or req.end_ms is None else (req.start_ms, req.end_ms)
            )
            span = await doing_answer(on_day, req.span_id, req.choice, req.text, stretch)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)[:200]) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)[:200]) from None
        except Exception as exc:  # noqa: BLE001 — a store that cannot be written is the page's 502.
            LOGGER.warning("answer route failed: %s: %s", type(exc).__name__, exc)
            raise HTTPException(status_code=502, detail=str(exc)[:200]) from None
        return {"ok": True, "span": span}


def register_doing_tidy_route(app: FastAPI, deps: InherentDeps) -> None:
    """ADR 0310: he asks to have today's timeline tidied now, by a tap."""
    if deps.doing_tidy is None:
        return
    doing_tidy = deps.doing_tidy

    @app.post("/inherent/day/tidy", status_code=200)
    async def day_tidy() -> dict[str, Any]:
        """``{ok, written, outcome}`` once the writer is done with today.

        ``written`` is true when a new run of today was stored, which ``GET /inherent/day``
        serves from then on; ``outcome`` says what happened otherwise (``unchanged``, ``no
        waking window``, ``rejected (...)``, ``failed``). Waits for the model, up to a minute.
        """
        outcome = await doing_tidy()
        return {"ok": True, "written": outcome.startswith("landed"), "outcome": outcome}
