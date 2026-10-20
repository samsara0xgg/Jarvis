"""ADR 0312: the background of the doing lane's writer, read for one day.

The writer is shown what happened; it also needs to know what things are: which project a
document belongs to, who a name is, what he usually does on this weekday, and how he has
corrected the wording before. :func:`gather` reads that from what is already stored, in four parts:

- projects: the items of core memory's ``GOALS_SECTION``, as stored;
- routines: what held on this weekday over past weeks (:func:`jarvis.state.routines.for_day`),
  as ``12:00-13:00 meal at Corner Cafe, seen 4 of 5 weeks``;
- people: the items of core memory's ``PEOPLE_SECTION`` whose leading name the day's clues or
  facts mention;
- corrections: his answers about the :data:`CORRECTION_DAYS` days before the day, each as the
  dated instance it was, with how the run had worded the stretch, as ``10-02 12:00-13:00: written
  as "lunch"; he said "ate with Ming"``. They are never merged into a rule.

None of it shows what he did that day, and none of it is written by a model. A part that cannot
be read is left empty, logged.

Layer rules: ``jarvis.state`` and ``jarvis.shared`` only (L2).
"""

from __future__ import annotations

import logging
from contextlib import closing
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final, NamedTuple

from jarvis.state import core_memory, places, routines
from jarvis.state.memory_db import (
    current_core_memory,
    doing_answers_between,
    latest_doing_run,
    open_memory_db,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Sequence
    from datetime import date, tzinfo
    from pathlib import Path

    from jarvis.state.memory_db import DoingAnswer, DoingRun

LOGGER = logging.getLogger(__name__)

ABOUT_SECTION_MAX_CHARS: Final = 3000
"""The most characters of any part; whole items are left off the end (corrections: the oldest)."""
CORRECTION_DAYS: Final = 30
"""How many days before the day his answers are read for corrections."""
MAX_CORRECTIONS: Final = 20
"""The most corrections shown, the newest."""
_NAME_MAX_CHARS: Final = 16
_NAME_MIN_CHARS: Final = 2
_NAME_ENDS: Final = ("\uff1a", ":", "\uff0c", ",", "\uff08", "(", " - ")
"""Where the name at the front of a person's item ends: the colon, comma and opening bracket of
either language, or a dash."""


class About(NamedTuple):
    """The background lines of a day, each part a tuple of one-line items."""

    projects: tuple[str, ...]
    routines: tuple[str, ...]
    people: tuple[str, ...]
    corrections: tuple[str, ...]


def _fitted(lines: Sequence[str], *, newest_last: bool = False) -> tuple[str, ...]:
    """The lines that fit :data:`ABOUT_SECTION_MAX_CHARS` as a list, the last (or first) dropped."""
    kept = list(lines)
    while kept and len("\n".join(f"- {line}" for line in kept)) > ABOUT_SECTION_MAX_CHARS:
        kept.pop(0 if newest_last else -1)
    return tuple(kept)


def _guarded[T](name: str, read: Callable[[], T], empty: T) -> T:
    try:
        return read()
    except Exception:
        LOGGER.exception("doing about: the %s source could not be read", name)
        return empty


def _items(memory_db: Path | None, section: str) -> list[str]:
    if memory_db is None:
        return []
    doc = current_core_memory(memory_db).doc
    return [" ".join(str(item["text"]).split()) for item in doc.get(section, [])]


def _name(text: str) -> str:
    """The name at the front of an item: up to the first stop, at most 16 characters."""
    ends = [at for stop in _NAME_ENDS if (at := text.find(stop)) >= 0]
    return text[: min(ends, default=len(text))][:_NAME_MAX_CHARS].strip()


def _people(memory_db: Path | None, mentioned: str) -> list[str]:
    said = mentioned.casefold()
    return [
        text for text in _items(memory_db, core_memory.PEOPLE_SECTION)
        if len(name := _name(text)) >= _NAME_MIN_CHARS and name.casefold() in said
    ]


def _routine(routine: routines.Routine) -> str:
    when = f"{routine.start}-{routine.end}" if routine.end else routine.start
    line = " ".join(part for part in (when, routine.what) if part)
    if routine.place and routine.place != routine.what:
        line += f" at {routine.place}"
    return f"{line}, seen {routine.seen} of {routine.of} weeks"


def _routines(
    event_log: sqlite3.Connection, memory_db: Path | None, day: date, zone: tzinfo,
) -> list[str]:
    kept = [] if memory_db is None else places.read_kept(memory_db)
    return [_routine(r) for r in routines.for_day(event_log, day, zone=zone, places=kept)]


def _worded(run: DoingRun | None, answer: DoingAnswer) -> str:
    """The title of the run's span that covers the start of the answer, else empty."""
    if run is None:
        return ""
    span = next(
        (s for s in run.spans if s["start_ms"] <= answer.start_ms < s["end_ms"]), None,
    )
    return " ".join(str(span["title"] or "").split()) if span is not None else ""


def _corrections(memory_db: Path | None, day: date, zone: tzinfo) -> list[str]:
    if memory_db is None:
        return []
    first = (day - timedelta(days=CORRECTION_DAYS)).isoformat()
    lines: list[str] = []
    with closing(open_memory_db(memory_db)) as conn:
        answers = doing_answers_between(conn, first, day.isoformat())[-MAX_CORRECTIONS:]
        runs = {a.day: latest_doing_run(conn, a.day) for a in answers}
    for answer in answers:
        said = " ".join((answer.text or answer.choice or "").split())
        if not said:
            continue
        written = _worded(runs[answer.day], answer)
        start, end = (
            datetime.fromtimestamp(ms / 1000, zone) for ms in (answer.start_ms, answer.end_ms)
        )
        when = f"{answer.day[5:]} {start:%H:%M}-{end:%H:%M}"
        was = f'written as "{written}"; ' if written and written != said else ""
        lines.append(f'{when}: {was}he said "{said}"')
    return lines


def gather(
    memory_db: Path | None, event_log: sqlite3.Connection, day: date, zone: tzinfo,
    mentioned: str,
) -> About:
    """The background of ``day``'s writer.

    ``mentioned`` is the day's clue texts and fact titles joined: a person is named only when
    the day names them. Each part is at most :data:`ABOUT_SECTION_MAX_CHARS` characters.
    """
    return About(
        projects=_fitted(_guarded(
            "projects", lambda: _items(memory_db, core_memory.GOALS_SECTION), [],
        )),
        routines=_fitted(_guarded(
            "routines", lambda: _routines(event_log, memory_db, day, zone), [],
        )),
        people=_fitted(_guarded("people", lambda: _people(memory_db, mentioned), [])),
        corrections=_fitted(
            _guarded("corrections", lambda: _corrections(memory_db, day, zone), []),
            newest_last=True,
        ),
    )


__all__ = [
    "ABOUT_SECTION_MAX_CHARS",
    "CORRECTION_DAYS",
    "MAX_CORRECTIONS",
    "About",
    "gather",
]
