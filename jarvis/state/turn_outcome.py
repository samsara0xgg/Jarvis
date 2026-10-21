"""How one turn ended, read from the rows it left (ADR 0349).

A caller that waits on a turn it did not run, such as the Live delegation or ``POST
/inherent/ask``, asks one question of the log: did the turn answer, did it fail, or is it still
going. This module is the one place that decides, so the two callers cannot disagree on an input.

Only the turn's own rows count:

- The answer is its final ``surface.response_emitted``. A wait line (a ``commentary`` run,
  ADR 0116) renders under the same turn id and is not the answer.
- A failure is a ``response.failed``, ``response.cancelled`` or ``turn.failed`` row, except one
  that ends a wait line or a streamed run another run took over (``corrects_response_id``,
  ADR-0008 D3): the turn goes on, and its answer is still to come.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from jarvis.state.event_log import iter_events_for_turn

if TYPE_CHECKING:
    import sqlite3

_OUTCOME_ROWS: Final[tuple[str, ...]] = (
    "response.started",
    "surface.response_emitted",
    "response.failed",
    "response.cancelled",
    "turn.failed",
)
_FAILURE_ROWS: Final[frozenset[str]] = frozenset(
    {"response.failed", "response.cancelled", "turn.failed"},
)


@dataclass(frozen=True)
class Answered:
    """The turn's final answer, as the row carries it.

    ``text`` is the whole answer. ``voice_text`` is what the voice says and ``document_text``
    what is written, each ``None`` when the row has none; ``written_apart`` is the row's own
    mark that the written text carries more than the voice says (ADR 0114).
    """

    text: str
    voice_text: str | None
    document_text: str | None
    written_apart: bool


@dataclass(frozen=True)
class Failed:
    """The turn's own failure: the row that ended it, and what that row says about why.

    ``event_type`` is ``response.failed``, ``response.cancelled`` or ``turn.failed``. ``reason``
    is the row's ``reason`` and ``detail`` its ``exception_repr``; either is ``None`` when the
    row has none.
    """

    event_type: str
    reason: str | None
    detail: str | None


type TurnOutcome = Answered | Failed


def _text(value: object) -> str | None:
    """The field as text, ``None`` when it is absent or empty."""
    return str(value) if value else None


def turn_outcome(conn: sqlite3.Connection, turn_id: str) -> TurnOutcome | None:
    """How ``turn_id`` ended, or ``None`` while it is still going.

    An answer wins over a failure: a turn whose final answer is out has answered, whatever
    row followed it. Among failures the first of the turn's own wins.
    """
    events = list(iter_events_for_turn(conn, turn_id, _OUTCOME_ROWS))
    not_the_turn = {
        str(event.payload.get("response_id"))
        for event in events
        if event.type == "response.started" and event.payload.get("phase") == "commentary"
    } | {
        str(event.payload["corrects_response_id"])
        for event in events
        if event.type == "response.started" and event.payload.get("corrects_response_id")
    }
    for event in events:
        if (
            event.type == "surface.response_emitted"
            and event.payload.get("phase", "final") == "final"
        ):
            voice, document = event.payload.get("voice_text"), event.payload.get("document_text")
            text = event.payload.get("text")
            return Answered(
                text=text if isinstance(text, str) else "",
                voice_text=voice if isinstance(voice, str) and voice else None,
                document_text=document if isinstance(document, str) and document else None,
                written_apart=event.payload.get("written_apart") is True,
            )
    for event in events:
        if event.type in _FAILURE_ROWS and (
            str(event.payload.get("response_id")) not in not_the_turn
        ):
            return Failed(
                event_type=event.type,
                reason=_text(event.payload.get("reason")),
                detail=_text(event.payload.get("exception_repr")),
            )
    return None
