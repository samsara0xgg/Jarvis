"""What he says he did or is doing, as the post-turn pass read it out of his words (ADR 0300).

A set is one ``doings.stated`` event: the doings one record of his holds, written by one version of
the extractor, each with the words that back it, when it was or is (or none), whether it is past or
going on, and where and with whom only if he said so. The log is append-only: a record read again
by another extractor version writes another set, and the latest set per record stands. An empty set
is written only over a standing one, to say the newer reading found nothing.

:func:`standing` is the one reader: the doings of the standing sets that fall in a time window. The
day's spans (ADR 0302) take them as clues; nothing here cuts or names a span.

Layer rules: ``jarvis.state`` and ``jarvis.shared`` only (L2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from jarvis.state.event_log import emit_event, iter_events_of_types

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence
    from datetime import datetime

TYPE: Final[str] = "doings.stated"
TENSES: Final[tuple[str, ...]] = ("past", "ongoing")
QUOTE_CHARS: Final[int] = 200
TEXT_CHARS: Final[int] = 120


@dataclass(frozen=True)
class Doing:
    """One thing he says he did (``past``) or is doing (``ongoing``).

    ``start_ms`` and ``end_ms`` say when, or ``None`` when he did not say; ``said_ms`` is when he
    said it (his record's time). ``place`` and ``who`` are empty unless he named them.
    """

    record_id: str
    extractor: str
    said_ms: int
    quote: str
    tense: str
    start_ms: int | None = None
    end_ms: int | None = None
    place: str = ""
    who: str = ""

    def entry(self) -> dict[str, Any]:
        """The doing as the event holds it, without the set's own fields."""
        return {
            "quote": self.quote, "tense": self.tense, "start_ms": self.start_ms,
            "end_ms": self.end_ms, "place": self.place, "who": self.who,
        }

    def span(self) -> tuple[int, int]:
        """Where the doing sits on the clock: its own times, else the time he said it."""
        begin = self.start_ms if self.start_ms is not None else (
            self.end_ms if self.end_ms is not None else self.said_ms
        )
        if self.end_ms is not None:
            return begin, max(begin, self.end_ms)
        return begin, max(begin, self.said_ms) if self.tense == "ongoing" else begin


def _check(doing: Doing) -> None:
    if doing.tense not in TENSES:
        msg = f"tense must be one of {TENSES}, not {doing.tense!r}"
        raise ValueError(msg)
    if not doing.quote.strip() or len(doing.quote) > QUOTE_CHARS:
        msg = f"a doing needs its quote, at most {QUOTE_CHARS} characters"
        raise ValueError(msg)
    if len(doing.place) > TEXT_CHARS or len(doing.who) > TEXT_CHARS:
        msg = f"place and who are at most {TEXT_CHARS} characters"
        raise ValueError(msg)
    if (
        doing.start_ms is not None and doing.end_ms is not None and doing.end_ms < doing.start_ms
    ):
        msg = "a doing does not end before it starts"
        raise ValueError(msg)


def _sets(conn: sqlite3.Connection) -> tuple[dict[str, tuple[Doing, ...]], set[tuple[str, str]]]:
    """The standing set per record, and every ``(record, extractor)`` that has written one."""
    standing: dict[str, tuple[Doing, ...]] = {}
    seen: set[tuple[str, str]] = set()
    for event in iter_events_of_types(conn, (TYPE,)):
        payload = event.payload
        record_id, extractor = str(payload["record_id"]), str(payload["extractor"])
        said = int(payload["said_ms"])
        standing[record_id] = tuple(
            Doing(
                record_id, extractor, said, str(one["quote"]), str(one["tense"]),
                None if one.get("start_ms") is None else int(one["start_ms"]),
                None if one.get("end_ms") is None else int(one["end_ms"]),
                str(one.get("place") or ""), str(one.get("who") or ""),
            )
            for one in payload["doings"]
        )
        seen.add((record_id, extractor))
    return standing, seen


def write(  # noqa: PLR0913 - the set: which record, which reader, when, what, and the clock
    conn: sqlite3.Connection, record_id: str, extractor: str, said_ms: int,
    doings: Sequence[Doing], *, now: datetime,
) -> bool:
    """Write the set ``extractor`` read from ``record_id``; False when nothing was written.

    One set per record and extractor version: a second one from the same version is not written.
    An empty set is written only when the record has a standing set to replace. ``ValueError`` for
    a doing that could not be read back.
    """
    for one in doings:
        _check(one)
    standing, seen = _sets(conn)
    if (record_id, extractor) in seen or not (doings or standing.get(record_id)):
        return False
    emit_event(
        conn, type=TYPE, ts_epoch_ms=int(now.timestamp() * 1000),
        payload={
            "record_id": record_id, "extractor": extractor, "said_ms": said_ms,
            "doings": [one.entry() for one in doings],
        },
    )
    return True


def standing(conn: sqlite3.Connection, start_ms: int, end_ms: int) -> list[Doing]:
    """The doings of the standing sets that fall in ``[start_ms, end_ms)``, earliest first.

    A doing falls in the window when its own times do, else when the time he said it does.
    """
    found = [
        (one.span(), one) for doings in _sets(conn)[0].values() for one in doings
    ]
    return [
        one for (begin, finish), one in sorted(found, key=lambda row: (row[0], row[1].record_id))
        if begin < end_ms and finish >= start_ms
    ]
