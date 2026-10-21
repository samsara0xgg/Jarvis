"""The ledger of his things, folded from the event log (ADR 0299).

There is no things table: a thing is its ``thing.added`` event with every later ``changed``,
``moved``, ``closed`` and ``reopened`` of it applied in the order they were written. Everything he
has to go to or do is one item here, whether it came from his words, a mail, a picture, Outlook,
To Do or the plan; her tool, the mail poller, the mirror, the plan and the buttons all write
through the functions below and every reader reads :func:`fold`, :func:`occurrences` or
:func:`open_things`.

Each write names who made it (``by``) and the words, mail line or record that proved it
(``evidence``, ``ref``), refuses what could not be read back, and ends by arming or cancelling the
rings of the thing it touched (:func:`sync_rings`). A thing and its rings go in one transaction.
Nothing is deleted: a wrong item is cancelled with its reason.

A ring is a string: ``-30m`` (30 minutes before the thing's time), ``-0m`` (at it), ``-1d@20:00``
(the day before, at 20:00 local) or ``@09:00`` (that day, at 09:00 local). Rings are ADR 0179
reminders whose ids start with :data:`RING_PREFIX`.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Final, NamedTuple

from jarvis.shared import lang
from jarvis.state import commitments, departures
from jarvis.state import reminders as reminder_state
from jarvis.state.event_log import append_event_in_transaction, iter_events_of_types

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
    from datetime import tzinfo

LOGGER = logging.getLogger(__name__)

TYPES: Final[tuple[str, ...]] = (
    "thing.added",
    "thing.changed",
    "thing.moved",
    "thing.closed",
    "thing.reopened",
    "thing.carried_in",
)
ID_PREFIX: Final[str] = "thing-"
RING_PREFIX: Final[str] = "reminder-th"
"""Every ring the ledger arms has this reminder-id prefix; :func:`ring_owner` names its thing."""
KINDS: Final[tuple[str, ...]] = ("go", "do")
STATES: Final[tuple[str, ...]] = ("open", "done", "paid", "cancelled")
SOURCES: Final[tuple[str, ...]] = ("said", "picture", "mail", "outlook", "todo", "plan")
BY: Final[tuple[str, ...]] = ("her", "pass", "mail", "mirror", "plan", "review", "him", "carry_in")
CHANGEABLE: Final[tuple[str, ...]] = (
    "title", "kind", "place", "link", "note", "rings", "private", "life_line", "confirmed",
    "sender", "outlook_id", "skip_dates", "weekly_until",
)
TITLE_CHARS: Final[int] = 300
NOTE_CHARS: Final[int] = 2000
LIFE_LINE_CHARS: Final[int] = 40
EVIDENCE_CHARS: Final[int] = 200
DEFAULT_MINUTES: Final[int] = 60
RING_HORIZON: Final[timedelta] = timedelta(days=7)

_WEEK: Final[timedelta] = timedelta(days=7)
_MAX_WEEKS: Final[int] = 10_000  # a series is weeks long; the bound only stops a corrupt date
_MAX_LEAD_DAYS: Final[int] = 31
_PLACE_SHORT: Final[int] = 40
_RING_ACTION: Final[str] = "thing-rings"
_CARRY_ACTION: Final[str] = "thing-carry-in"
# The action id runtime/interview_reminders.py gives its two reminders (ADR 0186); L2 cannot
# import it. The carry-in leaves them to the interview's own pass.
_INTERVIEW_ACTION: Final[str] = "job-interview-reminders"
_CARRY_VERSION: Final[int] = 1
_CLOSING: Final[tuple[str, ...]] = ("done", "paid", "cancelled")
_TIME_FIELDS: Final[tuple[str, ...]] = ("start_at", "end_at", "on_day", "last_day", "due")
_LISTS: Final[tuple[str, ...]] = ("rings", "skip_dates")
_DAY: Final[re.Pattern[str]] = re.compile(r"\d{4}-\d{2}-\d{2}")
_ID: Final[re.Pattern[str]] = re.compile(r"thing-[0-9a-z]{1,64}")
_RING_BEFORE: Final[re.Pattern[str]] = re.compile(r"-(\d{1,5})m")
_RING_AT: Final[re.Pattern[str]] = re.compile(r"(?:-(\d{1,2})d)?@(\d{2}):(\d{2})")
_CLOCK: Final[str] = "%H:%M"


@dataclass(frozen=True)
class Move:
    """A time a thing had until ``at_ms``, when ``by`` moved it because of ``evidence``.

    ``date`` is set when one occurrence of a weekly thing was moved: ``start_at`` and ``end_at``
    are then that occurrence's start and end before the move.
    """

    at_ms: int
    start_at: str
    end_at: str
    on_day: str
    due: str
    by: str
    evidence: str
    date: str = ""
    last_day: str = ""


@dataclass(frozen=True)
class Step:
    """One write to a thing; ``what`` is added, changed, moved, done, paid, cancelled, reopened."""

    at_ms: int
    what: str
    by: str
    evidence: str
    ref: str
    date: str = ""
    why: str = ""


@dataclass
class Thing:
    """One thing he goes to or does: its words, its time, and what the log says became of it."""

    thing_id: str
    title: str
    kind: str
    start_at: str = ""
    end_at: str = ""
    on_day: str = ""
    last_day: str = ""  # an ``on_day`` thing over several days: its last one
    due: str = ""
    weekly_until: str = ""
    skip_dates: tuple[str, ...] = ()
    place: str = ""
    link: str = ""
    note: str = ""
    rings: tuple[str, ...] = ()
    private: bool = False
    life_line: str = ""
    confirmed: bool = True
    source: str = "said"
    source_ref: str = ""
    sender: str = ""
    outlook_id: str = ""
    # filled by the fold, never by a writer
    added_ms: int = 0
    added_by: str = ""
    state: str = "open"
    closed_ms: int | None = None
    date_states: dict[str, str] = field(default_factory=dict)
    date_moves: dict[str, tuple[str, str]] = field(default_factory=dict)
    moves: tuple[Move, ...] = ()
    history: tuple[Step, ...] = ()

    def payload(self) -> dict[str, Any]:
        """The ``thing.added`` payload that says this thing again (without by, evidence, ref)."""
        return {
            "thing_id": self.thing_id, "title": self.title, "kind": self.kind,
            "start_at": self.start_at, "end_at": self.end_at, "on_day": self.on_day,
            "last_day": self.last_day, "due": self.due, "weekly_until": self.weekly_until,
            "skip_dates": list(self.skip_dates), "place": self.place, "link": self.link,
            "note": self.note, "rings": list(self.rings), "private": self.private,
            "life_line": self.life_line, "confirmed": self.confirmed, "source": self.source,
            "source_ref": self.source_ref, "sender": self.sender, "outlook_id": self.outlook_id,
        }


@dataclass(frozen=True)
class Occurrence:
    """One time a timed thing happens, in epoch milliseconds.

    ``day`` is the series date the occurrence belongs to (the date ``close``, ``move`` and
    ``reopen`` take as ``occurrence``); a moved occurrence keeps it. ``state`` is open, done or
    paid.
    """

    thing: Thing
    start_ms: int
    end_ms: int
    day: str
    all_day: bool
    state: str


class _Ring(NamedTuple):
    before_min: int | None  # ``-Nm``
    days: int  # ``-Nd@HH:MM``; 0 for ``@HH:MM``
    at: time | None


@dataclass(frozen=True)
class _Slot:
    """One time a thing happens, with the state of that time."""

    key: str  # the series date (the key of ``date_states`` and ``date_moves``)
    day: date  # the local date of the anchor
    anchor_ms: int  # the instant rings are measured from
    start_ms: int
    end_ms: int
    all_day: bool  # no hour: an ``on_day``, or a date-only ``due``
    state: str


def new_id() -> str:
    """A fresh ``thing-<uuid hex>`` id."""
    return ID_PREFIX + uuid.uuid4().hex


def short_id(thing: Thing) -> str:
    """The first 6 hex of the id: what she says and what ``find`` takes back."""
    return thing.thing_id.removeprefix(ID_PREFIX)[:6]


def _ms(moment: datetime) -> int:
    return int(moment.timestamp()) * 1000 + moment.microsecond // 1000


def _words(text: str) -> tuple[str, str]:
    """``(title, note)`` for words he said: one line up to 300 chars, the rest in the note."""
    text = text.strip()
    if len(text) <= TITLE_CHARS:
        return text, ""
    return text.splitlines()[0].strip()[:TITLE_CHARS], text[:NOTE_CHARS]


# --- the fold ------------------------------------------------------------------------------


def _from_payload(payload: Mapping[str, Any]) -> Thing:
    return Thing(
        thing_id=str(payload["thing_id"]),
        title=str(payload["title"]),
        kind=str(payload["kind"]),
        start_at=str(payload.get("start_at") or ""),
        end_at=str(payload.get("end_at") or ""),
        on_day=str(payload.get("on_day") or ""),
        last_day=str(payload.get("last_day") or ""),
        due=str(payload.get("due") or ""),
        weekly_until=str(payload.get("weekly_until") or ""),
        skip_dates=tuple(str(d) for d in payload.get("skip_dates") or ()),
        place=str(payload.get("place") or ""),
        link=str(payload.get("link") or ""),
        note=str(payload.get("note") or ""),
        rings=tuple(str(r) for r in payload.get("rings") or ()),
        private=bool(payload.get("private")),
        life_line=str(payload.get("life_line") or ""),
        confirmed=payload.get("confirmed") is not False,
        source=str(payload.get("source") or "said"),
        source_ref=str(payload.get("source_ref") or ""),
        sender=str(payload.get("sender") or ""),
        outlook_id=str(payload.get("outlook_id") or ""),
    )


def fold(conn: sqlite3.Connection) -> dict[str, Thing]:
    """Every thing ever added, by id, in the order it was added."""
    found: dict[str, Thing] = {}
    for event in iter_events_of_types(conn, TYPES):
        payload = event.payload
        if event.type == "thing.added":
            one = _from_payload(payload)
            one.added_ms, one.added_by = event.ts_epoch_ms, str(payload["by"])
            one.history = (_step(event.ts_epoch_ms, "added", payload),)
            found[one.thing_id] = one
        elif (held := found.get(str(payload.get("thing_id")))) is not None:
            _apply(held, event.type, payload, event.ts_epoch_ms)
    return found


def _apply(one: Thing, event_type: str, payload: Mapping[str, Any], at: int) -> None:
    """One write after ``added``, applied to the thing it is about."""
    date_key = str(payload.get("date") or "")
    what = "changed"
    if event_type == "thing.changed":
        for name, value in payload["fields"].items():
            setattr(one, name, tuple(str(v) for v in value) if name in _LISTS else value)
    elif event_type == "thing.moved":
        what = "moved"
        by, evidence = str(payload["by"]), str(payload["evidence"])
        if date_key:
            was = (str(payload.get("was_start_at") or ""), str(payload.get("was_end_at") or ""))
            one.moves += (Move(at, *was, "", "", by, evidence, date_key),)
            one.date_moves[date_key] = (str(payload["start_at"]), str(payload["end_at"]))
        else:
            one.moves += (
                Move(
                    at, one.start_at, one.end_at, one.on_day, one.due, by, evidence,
                    last_day=one.last_day,
                ),
            )
            for name in _TIME_FIELDS:
                setattr(one, name, str(payload.get(name) or ""))
    elif event_type == "thing.closed":
        what = str(payload["state"])
        if date_key:
            one.date_states[date_key] = what
        else:
            one.state, one.closed_ms = what, at
    else:  # thing.reopened
        what = "reopened"
        if date_key:
            one.date_states.pop(date_key, None)
        else:
            one.state, one.closed_ms = "open", None
    one.history += (_step(at, what, payload),)


def _step(at: int, what: str, payload: Mapping[str, Any]) -> Step:
    return Step(
        at, what, str(payload["by"]), str(payload["evidence"]), str(payload.get("ref") or ""),
        str(payload.get("date") or ""), str(payload.get("why") or ""),
    )


def get(conn: sqlite3.Connection, thing_id: str) -> Thing:
    """The thing ``thing_id`` names; ``LookupError`` when there is none."""
    one = fold(conn).get(thing_id)
    if one is None:
        msg = f"no thing {thing_id!r}"
        raise LookupError(msg)
    return one


def find(conn: sqlite3.Connection, short: str) -> Thing:
    """The one thing whose id starts with ``short`` (4 chars or more); ``LookupError`` if none."""
    key = short.strip().lower().removeprefix(ID_PREFIX)
    if len(key) < 4:  # noqa: PLR2004 - the shortest id start that is not a guess
        msg = f"{short!r} is too short: give at least 4 characters of the id"
        raise LookupError(msg)
    hits = [t for t in fold(conn).values() if t.thing_id.removeprefix(ID_PREFIX).startswith(key)]
    if not hits:
        msg = f"no thing starts with {short!r}"
        raise LookupError(msg)
    if len(hits) > 1:
        msg = f"{short!r} starts {len(hits)} things; give more of the id"
        raise LookupError(msg)
    return hits[0]


def by_source(conn: sqlite3.Connection, source: str, source_ref: str) -> Thing | None:
    """The newest thing that came from ``source`` with ``source_ref``, whatever became of it."""
    if not source_ref:
        return None
    for one in reversed(list(fold(conn).values())):
        if one.source == source and one.source_ref == source_ref:
            return one
    return None


def _wall(one: Thing) -> datetime | None:
    """Where the thing sits on a clock, to order by: its own wall time, else the end of its day."""
    if one.start_at:
        return datetime.fromisoformat(one.start_at).replace(tzinfo=None)
    if one.on_day:
        return datetime.combine(date.fromisoformat(one.on_day), time.min)
    if one.due:
        if _DAY.fullmatch(one.due):
            return datetime.combine(date.fromisoformat(one.due), time.max)
        return datetime.fromisoformat(one.due).replace(tzinfo=None)
    return None


def open_things(conn: sqlite3.Connection) -> list[Thing]:
    """The things still open: timed ones soonest first, then untimed ones oldest first.

    A timed thing is placed by its (first) time as written, on the wall clock of its own offset; a
    weekly one by its first. ``occurrences`` has the real instants.
    """
    timed: list[tuple[datetime, int, Thing]] = []
    untimed: list[tuple[int, int, Thing]] = []
    for order, one in enumerate(fold(conn).values()):
        if one.state != "open":
            continue
        at = _wall(one)
        if at is None:
            untimed.append((one.added_ms, order, one))
        else:
            timed.append((at, order, one))
    timed.sort(key=lambda row: row[:2])
    untimed.sort(key=lambda row: row[:2])
    return [row[2] for row in timed] + [row[2] for row in untimed]


def due_ms(one: Thing, zone: tzinfo) -> int:
    """The instant ``one`` is due; a date means 23:59:59.999 local that day."""
    if not one.due:
        msg = f"{one.title!r} has no due time"
        raise ValueError(msg)
    if _DAY.fullmatch(one.due):
        end = datetime.combine(date.fromisoformat(one.due), time.max, tzinfo=zone)
        return _ms(end.replace(microsecond=999_000))
    return _ms(datetime.fromisoformat(one.due))


# --- the times a thing happens -------------------------------------------------------------


def _first_day(one: Thing, zone: tzinfo) -> date:
    if one.start_at:
        return datetime.fromisoformat(one.start_at).astimezone(zone).date()
    return date.fromisoformat(one.on_day)


def _extra_days(one: Thing) -> int:
    """The days an ``on_day`` thing covers after its first one."""
    if not (one.on_day and one.last_day):
        return 0
    return (date.fromisoformat(one.last_day) - date.fromisoformat(one.on_day)).days


def _series(one: Thing, zone: tzinfo) -> list[date]:
    """The series dates of a timed thing: one, or every 7 days to ``weekly_until``, less skips."""
    first = _first_day(one, zone)
    if not one.weekly_until:
        return [first]
    until = date.fromisoformat(one.weekly_until)
    found: list[date] = []
    for week in range(_MAX_WEEKS):
        at = first + week * _WEEK
        if at > until:
            break
        found.append(at)
    return found


def _span_ms(one: Thing) -> int:
    """How long a timed thing lasts: ``end_at`` less ``start_at``, else an hour."""
    if one.start_at and one.end_at:
        span = _ms(datetime.fromisoformat(one.end_at)) - _ms(datetime.fromisoformat(one.start_at))
        if span > 0:
            return span
    return DEFAULT_MINUTES * 60_000


def _state_of(one: Thing, key: str) -> str:
    return one.state if one.state != "open" else one.date_states.get(key, "open")


def _midnight(day: date, zone: tzinfo) -> int:
    return _ms(datetime.combine(day, time.min, tzinfo=zone))


def _slots(one: Thing, zone: tzinfo) -> list[_Slot]:
    """Every time ``one`` happens, closed ones included, per-date moves applied."""
    if one.due:
        at = due_ms(one, zone)
        day = datetime.fromtimestamp(at / 1000, zone).date()
        return [_Slot(day.isoformat(), day, at, at, at, bool(_DAY.fullmatch(one.due)), one.state)]
    if not (one.start_at or one.on_day):
        return []
    span = _span_ms(one)
    first_at = datetime.fromisoformat(one.start_at) if one.start_at else None
    wall = None if first_at is None else first_at.astimezone(zone).time().replace(fold=0)
    slots: list[_Slot] = []
    for week, day in enumerate(_series(one, zone)):
        key = day.isoformat()
        if key in one.skip_dates:
            continue
        if (moved := one.date_moves.get(key)) is not None:
            start = _ms(datetime.fromisoformat(moved[0]))
            end = _ms(datetime.fromisoformat(moved[1])) if moved[1] else start + span
            all_day = False
        elif first_at is not None and wall is not None:
            # the same wall-clock time each week, so a clock change does not move it
            start = _ms(first_at) if week == 0 else _ms(datetime.combine(day, wall, tzinfo=zone))
            end, all_day = start + span, False
        else:
            start, all_day = _midnight(day, zone), True
            end = _midnight(day + timedelta(days=1 + _extra_days(one)), zone)
        anchor_day = datetime.fromtimestamp(start / 1000, zone).date()
        slots.append(_Slot(key, anchor_day, start, start, end, all_day, _state_of(one, key)))
    return slots


def occurrences(
    conn: sqlite3.Connection, start_ms: int, end_ms: int, *, zone: tzinfo, closed: bool = False,
) -> list[Occurrence]:
    """Every occurrence of a timed thing (``start_at`` or ``on_day``) in ``[start_ms, end_ms)``.

    A weekly one repeats every 7 days from its first date to ``weekly_until`` (inclusive) less
    ``skip_dates``, at the same wall-clock time in ``zone`` across a clock change, with per-date
    moves applied. An ``on_day`` occurrence is that local day (through ``last_day``), midnight to
    midnight, ``all_day``; a ``start_at`` without ``end_at`` lasts an hour. Cancelled things and
    dates are left out; done and paid ones only with ``closed``. A ``due`` is not an occurrence:
    see :func:`open_things` and :func:`due_ms`. Soonest first.
    """
    found: list[Occurrence] = []
    for one in fold(conn).values():
        if one.state == "cancelled" or one.due:
            continue
        for slot in _slots(one, zone):
            if slot.state == "cancelled" or (slot.state != "open" and not closed):
                continue
            if slot.end_ms > start_ms and slot.start_ms < end_ms:
                found.append(Occurrence(
                    one, slot.start_ms, slot.end_ms, slot.key, slot.all_day, slot.state,
                ))
    return sorted(found, key=lambda o: (o.start_ms, o.thing.thing_id))


# --- checking what is written --------------------------------------------------------------


def _day(text: str, what: str) -> date:
    try:
        if _DAY.fullmatch(text):
            return date.fromisoformat(text)
    except ValueError:
        pass
    msg = f"{what} {text!r} is not a date YYYY-MM-DD"
    raise ValueError(msg)


def _aware(text: str, what: str) -> datetime:
    try:
        at = datetime.fromisoformat(text)
    except ValueError as exc:
        msg = f"{what} {text!r} is not an ISO time"
        raise ValueError(msg) from exc
    if at.tzinfo is None:
        msg = f"{what} {text!r} needs a UTC offset"
        raise ValueError(msg)
    return at


def _ring(text: str) -> _Ring:
    """Parse a ring; ``ValueError`` when it is none of ``-Nm``, ``-Nd@HH:MM``, ``@HH:MM``."""
    if (before := _RING_BEFORE.fullmatch(text)) and int(before[1]) <= _MAX_LEAD_DAYS * 1440:
        return _Ring(int(before[1]), 0, None)
    if (at := _RING_AT.fullmatch(text)) and int(at[1] or 0) <= _MAX_LEAD_DAYS:
        try:
            return _Ring(None, int(at[1] or 0), time(int(at[2]), int(at[3])))
        except ValueError:
            pass
    msg = (
        f"ring {text!r} is not -Nm, -Nd@HH:MM or @HH:MM (at most {_MAX_LEAD_DAYS} days ahead)"
    )
    raise ValueError(msg)


def _timed(one: Thing) -> bool:
    return bool(one.start_at or one.on_day or one.due)


def _check(one: Thing, zone: tzinfo) -> None:  # noqa: C901, PLR0912 - one rule per field
    """``ValueError`` when ``one`` could not be read back."""
    if not _ID.fullmatch(one.thing_id):
        msg = f"thing_id {one.thing_id!r} is not thing-<id>"
        raise ValueError(msg)
    if not one.title:
        msg = "a thing needs its words (title)"
        raise ValueError(msg)
    for name, value, limit in (
        ("title", one.title, TITLE_CHARS), ("note", one.note, NOTE_CHARS),
        ("life_line", one.life_line, LIFE_LINE_CHARS),
    ):
        if len(value) > limit:
            msg = f"{name} is {len(value)} characters; at most {limit}"
            raise ValueError(msg)
    for name, allowed, value in (
        ("kind", KINDS, one.kind), ("source", SOURCES, one.source),
    ):
        if value not in allowed:
            msg = f"{name} must be one of {allowed}, not {value!r}"
            raise ValueError(msg)
    flags: tuple[tuple[str, object], ...] = (("private", one.private), ("confirmed", one.confirmed))
    for name, flag in flags:
        if not isinstance(flag, bool):
            msg = f"{name} must be true or false"
            raise ValueError(msg)  # noqa: TRY004 - a value error, as the others
    if sum(map(bool, (one.start_at, one.on_day, one.due))) > 1:
        msg = "a thing has one kind of time: start_at, on_day or due"
        raise ValueError(msg)
    if one.end_at and not one.start_at:
        msg = "end_at needs start_at"
        raise ValueError(msg)
    if one.start_at:
        start = _aware(one.start_at, "start_at")
        if one.end_at and _aware(one.end_at, "end_at") < start:
            msg = f"end_at {one.end_at!r} is before start_at {one.start_at!r}"
            raise ValueError(msg)
    if one.on_day:
        _day(one.on_day, "on_day")
    if one.last_day and not one.on_day:
        msg = "last_day needs on_day"
        raise ValueError(msg)
    if one.last_day and _day(one.last_day, "last_day") < _day(one.on_day, "on_day"):
        msg = f"last_day {one.last_day!r} is before on_day {one.on_day!r}"
        raise ValueError(msg)
    if one.due:
        _day(one.due, "due") if _DAY.fullmatch(one.due) else _aware(one.due, "due")
    if (one.weekly_until or one.skip_dates) and not (one.start_at or one.on_day):
        msg = "weekly_until and skip_dates need a start_at or an on_day"
        raise ValueError(msg)
    for skipped in one.skip_dates:
        _day(skipped, "skip date")
    if one.weekly_until and _day(one.weekly_until, "weekly_until") < _first_day(one, zone):
        msg = f"weekly_until {one.weekly_until!r} is before the first day"
        raise ValueError(msg)
    for text in one.rings:
        _ring(text)
    if one.rings and not _timed(one):
        msg = "a thing with no time has no rings"
        raise ValueError(msg)


def _clean(one: Thing) -> Thing:
    """``one`` with its strings trimmed and its title on one line."""
    return replace(
        one,
        title=" ".join(str(one.title).split()),
        place=str(one.place).strip(), link=str(one.link).strip(), note=str(one.note).strip(),
        life_line=str(one.life_line).strip(), sender=str(one.sender).strip(),
        source_ref=str(one.source_ref).strip(), outlook_id=str(one.outlook_id).strip(),
        start_at=str(one.start_at).strip(), end_at=str(one.end_at).strip(),
        on_day=str(one.on_day).strip(), last_day=str(one.last_day).strip(),
        due=str(one.due).strip(),
        weekly_until=str(one.weekly_until).strip(),
        skip_dates=tuple(str(d).strip() for d in one.skip_dates),
        rings=tuple(str(r).strip() for r in one.rings),
    )


def _check_by(by: str) -> None:
    if by not in BY:
        msg = f"by must be one of {BY}, not {by!r}"
        raise ValueError(msg)


def _open(one: Thing) -> None:
    if one.state != "open":
        msg = f"{one.title!r} is {one.state}; only reopen touches it"
        raise ValueError(msg)


def _occurrence(one: Thing, key: str, zone: tzinfo) -> None:
    """``ValueError`` unless ``key`` is a date of ``one``'s weekly series."""
    if not one.weekly_until:
        msg = f"{one.title!r} is not weekly: it has no occurrence {key!r}"
        raise ValueError(msg)
    _day(key, "occurrence")
    if key not in {d.isoformat() for d in _series(one, zone)} - set(one.skip_dates):
        msg = f"{key!r} is not one of the occurrences of {one.title!r}"
        raise ValueError(msg)


# --- the one write path --------------------------------------------------------------------


@contextmanager
def _atomic(conn: sqlite3.Connection) -> Iterator[None]:
    """``BEGIN IMMEDIATE`` .. ``COMMIT``, or join the transaction the caller already holds."""
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


def _append(  # noqa: PLR0913 - the event, and who stamps it
    conn: sqlite3.Connection, event_type: str, payload: Mapping[str, Any], *, actor: str,
    stamp: int | None, correlation: Mapping[str, str] | None = None,
) -> None:
    append_event_in_transaction(
        conn, type=event_type, payload=payload, actor=actor, ts_epoch_ms=stamp,
        correlation=correlation,
    )


def _write(  # noqa: PLR0913 - the event, and who wrote it and why
    conn: sqlite3.Connection, event_type: str, thing_id: str, extra: Mapping[str, Any], *,
    by: str, evidence: str, ref: str, actor: str, stamp: int | None,
) -> None:
    _append(
        conn, event_type,
        {
            "thing_id": thing_id, "by": by, "evidence": evidence.strip()[:EVIDENCE_CHARS],
            "ref": ref.strip(), **extra,
        },
        actor=actor, stamp=stamp,
    )


def _stamp(now: datetime | None) -> int | None:
    return None if now is None else _ms(now)


def _same_time(a: Thing, b: Thing) -> bool:
    def when(one: Thing) -> tuple[str, int | str]:
        if one.start_at:
            return "start", _ms(datetime.fromisoformat(one.start_at))
        if one.on_day:
            return "day", f"{one.on_day}/{one.last_day}"
        if one.due:
            if _DAY.fullmatch(one.due):
                return "due", one.due
            return "due", _ms(datetime.fromisoformat(one.due))
        return "none", 0

    return when(a) == when(b)


def _add(  # noqa: PLR0913 - the thing, who wrote it and why, and the clock
    conn: sqlite3.Connection, thing: Thing, *, by: str, evidence: str, ref: str, zone: tzinfo,
    actor: str, stamp: int | None,
) -> tuple[Thing, bool]:
    """``add`` inside the caller's transaction, without arming rings."""
    _check_by(by)
    one = _clean(thing)
    _check(one, zone)
    things = fold(conn)
    if one.thing_id in things:
        msg = f"thing {one.thing_id!r} already exists"
        raise ValueError(msg)
    key = " ".join(one.title.split()).casefold()
    for other in things.values():
        if (
            other.state == "open"
            and " ".join(other.title.split()).casefold() == key
            and _same_time(other, one)
        ):
            return other, False
    body = {k: v for k, v in one.payload().items() if k != "thing_id"}
    _write(
        conn, "thing.added", one.thing_id, body, by=by, evidence=evidence, ref=ref, actor=actor,
        stamp=stamp,
    )
    return one, True


def add(  # noqa: PLR0913 - the thing, who wrote it and why, and the clock
    conn: sqlite3.Connection, thing: Thing, *, by: str, evidence: str, ref: str = "",
    zone: tzinfo, now: datetime | None = None, actor: str = "jarvis_runtime",
) -> tuple[Thing, bool]:
    """Write a new thing; ``(thing, created)``.

    An open thing with the same title (whitespace collapsed, case folded) and the same time is the
    same thing: nothing is written, that thing comes back and ``created`` is False. ``ValueError``
    for a write that could not be read back (a time without a UTC offset, more than one kind of
    time, ``end_at`` before ``start_at``, a bad date, weekly fields without a start, a kind,
    source or ``by`` outside its set, an empty title, a ring that does not parse, rings on a thing
    with no time). ``now`` stamps the events and is where rings are measured from.
    """
    with _atomic(conn):
        one, created = _add(
            conn, thing, by=by, evidence=evidence, ref=ref, zone=zone, actor=actor,
            stamp=_stamp(now),
        )
        if created:
            _sync(conn, zone=zone, now=now, thing_ids=(one.thing_id,), horizon=RING_HORIZON)
            return get(conn, one.thing_id), True
        return one, False


def _fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    """The values of a ``change`` as they go in the event: strings trimmed, lists as lists."""
    unknown = sorted(set(fields) - set(CHANGEABLE))
    if unknown:
        times = [name for name in unknown if name in _TIME_FIELDS]
        msg = (
            f"{times} are the time: use move, not change" if times
            else f"{unknown} cannot be changed; the fields are {CHANGEABLE}"
        )
        raise ValueError(msg)
    if not fields:
        msg = "nothing to change"
        raise ValueError(msg)
    clean: dict[str, Any] = {}
    for name, value in fields.items():
        if name in _LISTS:
            if not isinstance(value, list | tuple):
                msg = f"{name} must be a list"
                raise ValueError(msg)
            clean[name] = [str(v).strip() for v in value]
        elif name in ("private", "confirmed"):
            clean[name] = value
        else:
            clean[name] = " ".join(str(value).split()) if name == "title" else str(value).strip()
    return clean


def change(  # noqa: PLR0913 - the fields, who wrote them and why, and the clock
    conn: sqlite3.Connection, thing_id: str, fields: Mapping[str, Any], *, by: str,
    evidence: str, ref: str = "", zone: tzinfo, now: datetime | None = None,
    actor: str = "jarvis_runtime",
) -> Thing:
    """Change words, place, link, note, rings, flags or the weekly fields of an open thing.

    ``fields`` is a subset of :data:`CHANGEABLE`; a time is moved, not changed. ``ValueError`` for
    a field outside the set, a value that could not be read back, or a closed thing;
    ``LookupError`` when there is no such thing.
    """
    _check_by(by)
    clean = _fields(fields)
    with _atomic(conn):
        one = get(conn, thing_id)
        _open(one)
        candidate = replace(one)
        for name, value in clean.items():
            setattr(candidate, name, tuple(value) if name in _LISTS else value)
        _check(_clean(candidate), zone)
        _write(
            conn, "thing.changed", thing_id, {"fields": clean}, by=by, evidence=evidence, ref=ref,
            actor=actor, stamp=_stamp(now),
        )
        _sync(conn, zone=zone, now=now, thing_ids=(thing_id,), horizon=RING_HORIZON)
        return get(conn, thing_id)


def move(  # noqa: PLR0913 - the new time, who moved it and why, and the clock
    conn: sqlite3.Connection, thing_id: str, *, start_at: str = "", end_at: str = "",
    on_day: str = "", last_day: str = "", due: str = "", occurrence: str = "", by: str,
    evidence: str,
    ref: str = "", zone: tzinfo, now: datetime | None = None, actor: str = "jarvis_runtime",
) -> Thing:
    """Give an open thing a new time; the time it had stays in ``moves``.

    With ``occurrence`` (a date of a weekly thing) only that date moves, to ``start_at`` and
    ``end_at``. ``ValueError`` when no time is given, or the result could not be read back, or the
    thing (or that date) is closed, or the date is not one of its occurrences.
    """
    _check_by(by)
    with _atomic(conn):
        one = get(conn, thing_id)
        _open(one)
        if occurrence:
            _occurrence(one, occurrence, zone)
            if one.date_states.get(occurrence):
                msg = f"{occurrence} of {one.title!r} is {one.date_states[occurrence]}; reopen it"
                raise ValueError(msg)
            if on_day or last_day or due or not start_at:
                msg = "one occurrence moves to a new start_at (and end_at) only"
                raise ValueError(msg)
            begin = _aware(start_at.strip(), "start_at")
            if end_at.strip() and _aware(end_at.strip(), "end_at") < begin:
                msg = f"end_at {end_at!r} is before start_at {start_at!r}"
                raise ValueError(msg)
            was = next(s for s in _slots(one, zone) if s.key == occurrence)
            extra: dict[str, Any] = {
                "date": occurrence, "start_at": start_at.strip(), "end_at": end_at.strip(),
                "on_day": "", "last_day": "", "due": "",
                "was_start_at": datetime.fromtimestamp(was.start_ms / 1000, zone).isoformat(
                    timespec="seconds",
                ),
                "was_end_at": datetime.fromtimestamp(was.end_ms / 1000, zone).isoformat(
                    timespec="seconds",
                ),
            }
        else:
            if not (start_at.strip() or on_day.strip() or due.strip()):
                msg = "a move needs the new time: start_at, on_day or due"
                raise ValueError(msg)
            moved = _clean(replace(
                one, start_at=start_at, end_at=end_at, on_day=on_day, last_day=last_day, due=due,
            ))
            _check(moved, zone)
            extra = {name: getattr(moved, name) for name in _TIME_FIELDS}
        _write(
            conn, "thing.moved", thing_id, extra, by=by, evidence=evidence, ref=ref, actor=actor,
            stamp=_stamp(now),
        )
        _sync(conn, zone=zone, now=now, thing_ids=(thing_id,), horizon=RING_HORIZON)
        return get(conn, thing_id)


def close(  # noqa: PLR0913 - the state, which date, who closed it and why, and the clock
    conn: sqlite3.Connection, thing_id: str, state: str, *, occurrence: str = "",
    why: str = "", by: str, evidence: str, ref: str = "", zone: tzinfo,
    now: datetime | None = None, actor: str = "jarvis_runtime",
) -> Thing:
    """Close an open thing as ``done``, ``paid`` or ``cancelled``; ``why`` is a cancel's reason.

    A weekly thing is done or paid per date (``occurrence``); the series is cancelled as a whole,
    or a date at a time. ``ValueError`` for another state, a closed thing or date, or a date that
    is not one of its occurrences; ``LookupError`` when there is no such thing.
    """
    _check_by(by)
    if state not in _CLOSING:
        msg = f"a thing is closed as one of {_CLOSING}, not {state!r}; reopen makes it open"
        raise ValueError(msg)
    with _atomic(conn):
        one = get(conn, thing_id)
        _open(one)
        extra: dict[str, Any] = {"state": state, "why": why.strip()[:EVIDENCE_CHARS]}
        if occurrence:
            _occurrence(one, occurrence, zone)
            if one.date_states.get(occurrence):
                msg = f"{occurrence} of {one.title!r} is {one.date_states[occurrence]} already"
                raise ValueError(msg)
            extra["date"] = occurrence
        elif one.weekly_until and state != "cancelled":
            msg = f"{one.title!r} is weekly: it is {state} one date at a time (occurrence=)"
            raise ValueError(msg)
        _write(
            conn, "thing.closed", thing_id, extra, by=by, evidence=evidence, ref=ref, actor=actor,
            stamp=_stamp(now),
        )
        _sync(conn, zone=zone, now=now, thing_ids=(thing_id,), horizon=RING_HORIZON)
        return get(conn, thing_id)


def reopen(  # noqa: PLR0913 - which date, who reopened it and why, and the clock
    conn: sqlite3.Connection, thing_id: str, *, occurrence: str = "", by: str, evidence: str,
    ref: str = "", zone: tzinfo, now: datetime | None = None, actor: str = "jarvis_runtime",
) -> Thing:
    """Make a closed thing (or one closed date of a weekly thing) open again.

    ``ValueError`` when it is open already.
    """
    _check_by(by)
    with _atomic(conn):
        one = get(conn, thing_id)
        extra: dict[str, Any] = {}
        if occurrence:
            _occurrence(one, occurrence, zone)
            _open(one)
            if occurrence not in one.date_states:
                msg = f"{occurrence} of {one.title!r} is open already"
                raise ValueError(msg)
            extra["date"] = occurrence
        elif one.state == "open":
            msg = f"{one.title!r} is open already"
            raise ValueError(msg)
        _write(
            conn, "thing.reopened", thing_id, extra, by=by, evidence=evidence, ref=ref,
            actor=actor, stamp=_stamp(now),
        )
        _sync(conn, zone=zone, now=now, thing_ids=(thing_id,), horizon=RING_HORIZON)
        return get(conn, thing_id)


def confirm(  # noqa: PLR0913 - who confirmed it and why, and the clock
    conn: sqlite3.Connection, thing_id: str, *, by: str, evidence: str, ref: str = "",
    zone: tzinfo, now: datetime | None = None, actor: str = "jarvis_runtime",
) -> bool:
    """He says a thing a mail or an unsure sentence made is his; False when it was already.

    ``ValueError`` when the thing is closed; ``LookupError`` when there is no such thing.
    """
    with _atomic(conn):
        if get(conn, thing_id).confirmed:
            return False
        change(
            conn, thing_id, {"confirmed": True}, by=by, evidence=evidence, ref=ref, zone=zone,
            now=now, actor=actor,
        )
        return True


# --- rings ---------------------------------------------------------------------------------


def _hex12(thing_id: str) -> str:
    return thing_id.removeprefix(ID_PREFIX)[:12]


def _ring_due(slot: _Slot, ring: _Ring, zone: tzinfo) -> int:
    """When ``ring`` rings for ``slot``: minutes before its instant, or a local clock time."""
    if ring.before_min is not None:
        return slot.anchor_ms - ring.before_min * 60_000
    assert ring.at is not None  # noqa: S101 - a ring is minutes or a clock time
    return _ms(datetime.combine(slot.day - timedelta(days=ring.days), ring.at, tzinfo=zone))


def _ring_line(one: Thing, slot: _Slot, due: int, zone: tzinfo) -> str:
    """The ring's words: time, title and a short place; a private thing says only its time."""
    ahead = (slot.day - datetime.fromtimestamp(due / 1000, zone).date()).days
    place = f" · {one.place}" if one.place and len(one.place) <= _PLACE_SHORT else ""
    day, title = f"{slot.day:%m-%d}", one.title
    if slot.all_day:
        key = "today" if ahead <= 0 else "day" if ahead == 1 else "on_day"
        key = f"private_{key}" if one.private else key
        return lang.t(f"thing.rem.{key}", date=day, title=title, place=place)
    at = f"{datetime.fromtimestamp(slot.start_ms / 1000, zone):{_CLOCK}}"
    when = "" if ahead <= 0 else "_tomorrow" if ahead == 1 else "_on"
    key = ("private" if one.private else "line") + when
    return lang.t(f"thing.rem.{key}", date=day, time=at, title=title, place=place)


def default_rings(kind: str, *, start_at: str, on_day: str, before_min: int) -> tuple[str, ...]:
    """The rings a new thing gets when nobody named any, whoever writes it.

    A thing he goes to rings ``before_min`` before its hour, or the evening before its day; a
    thing he does rings at its hour; a thing with only a due date, or no time, does not ring.
    """
    if kind == "do":
        return ("-0m",) if start_at else ()
    if start_at:
        return (f"-{before_min}m",)
    return ("-1d@20:00",) if on_day else ()


def _ring_slots(
    things: Iterable[Thing], zone: tzinfo,
) -> Iterator[tuple[str, Thing, _Slot, int]]:
    """``(reminder id, thing, slot, due ms)`` of every ring the open things want, past ones too."""
    for one in things:
        if one.state != "open" or not one.rings:
            continue
        rings = [_ring(text) for text in one.rings]
        for slot in _slots(one, zone):
            if slot.state != "open":
                continue
            for k, ring in enumerate(rings):
                rid = f"{RING_PREFIX}{_hex12(one.thing_id)}-{slot.anchor_ms}-{k}"
                yield rid, one, slot, _ring_due(slot, ring, zone)


def ring_dues(one: Thing, zone: tzinfo) -> list[int]:
    """When each ring of ``one`` goes off, in ms, for every time it happens that is not closed."""
    return [due for *_, due in _ring_slots((one,), zone)]


def _wanted(
    things: Iterable[Thing], zone: tzinfo,
) -> dict[str, tuple[int, str]]:
    """``reminder id -> (due ms, words)`` of every ring the open things want, past ones too."""
    return {
        rid: (due, _ring_line(one, slot, due, zone))
        for rid, one, slot, due in _ring_slots(things, zone)
    }


@dataclass(frozen=True)
class Ring:
    """One ring of one time of an open thing: when it goes off, and whether it went off."""

    reminder_id: str
    thing: Thing
    start_ms: int  # the time of the thing it rings for: an occurrence's start, or the due
    all_day: bool
    due_ms: int
    rang: bool


def ring_times(
    conn: sqlite3.Connection, start_ms: int, end_ms: int, *, zone: tzinfo,
) -> list[Ring]:
    """Every ring the open things want that goes off in ``[start_ms, end_ms)``, soonest first.

    What the ledger holds, not only what is armed now: a ring beyond :data:`RING_HORIZON` is
    listed though :func:`sync_rings` arms it only when it comes near. ``rang`` says its reminder
    went off. This is the one account of a thing's rings that every reader shows.
    """
    known = reminder_state.fold(conn)
    found = [
        Ring(rid, one, slot.start_ms, slot.all_day, due,
             (held := known.get(rid)) is not None and held.fired_at_ms is not None)
        for rid, one, slot, due in _ring_slots(fold(conn).values(), zone)
        if start_ms <= due < end_ms
    ]
    return sorted(found, key=lambda r: (r.due_ms, r.thing.thing_id))


def ring_owner(reminder_id: str, things: Iterable[Thing]) -> Thing | None:
    """The thing a ring reminder belongs to, closed ones included; None when it is no ring."""
    if not reminder_id.startswith(RING_PREFIX):
        return None
    hex12 = reminder_id.removeprefix(RING_PREFIX).partition("-")[0]
    return next((one for one in things if _hex12(one.thing_id) == hex12), None)


def _sync(
    conn: sqlite3.Connection, *, zone: tzinfo, now: datetime | None,
    thing_ids: Collection[str] | None, horizon: timedelta,
) -> None:
    """``sync_rings`` inside the caller's transaction."""
    moment = now or datetime.now(UTC)
    now_ms, until_ms = _ms(moment), _ms(moment + horizon)
    scope = None if thing_ids is None else {_hex12(i) for i in thing_ids}
    wanted = _wanted(
        (t for t in fold(conn).values() if thing_ids is None or t.thing_id in thing_ids), zone,
    )
    known = reminder_state.fold(conn)
    stamp = _stamp(now)
    for rid, pending in known.items():
        if not (rid.startswith(RING_PREFIX) and pending.pending) or rid in wanted:
            continue
        if scope is None or rid.removeprefix(RING_PREFIX).partition("-")[0] in scope:
            _append(
                conn, "reminder.cancelled", {"reminder_id": rid, "action_id": _RING_ACTION},
                actor="jarvis_runtime", stamp=stamp, correlation={"action_id": _RING_ACTION},
            )
    for rid, (due, text) in wanted.items():
        held = known.get(rid)
        ahead = now_ms <= due < until_ms
        if held is None or held.cancelled:
            if not ahead:
                continue  # a ring already past is skipped, never made late; one far off waits
        elif not held.pending or (held.text, held.due_at_ms) == (text, due):
            continue  # it stands, or it rang already
        elif due != held.due_at_ms and due < now_ms:
            _append(  # its time moved into the past: not made late
                conn, "reminder.cancelled", {"reminder_id": rid, "action_id": _RING_ACTION},
                actor="jarvis_runtime", stamp=stamp, correlation={"action_id": _RING_ACTION},
            )
            continue
        _append(
            conn, "reminder.scheduled",
            {
                "reminder_id": rid, "due_at_epoch_ms": due,
                "due_at_local": datetime.fromtimestamp(due / 1000, zone).isoformat(
                    timespec="seconds",
                ),
                "text": text, "action_id": _RING_ACTION,
            },
            actor="jarvis_runtime", stamp=stamp, correlation={"action_id": _RING_ACTION},
        )


def sync_rings(
    conn: sqlite3.Connection, *, zone: tzinfo, now: datetime | None = None,
    thing_ids: Collection[str] | None = None, horizon: timedelta = RING_HORIZON,
) -> None:
    """Make the pending ring reminders match the rings of the open things; twice changes nothing.

    A ring of an open thing, for each occurrence not closed, moved away or skipped, is a
    ``reminder.scheduled`` with id ``reminder-th<hex12>-<anchor ms>-<k>`` when it falls in
    ``[now, now + horizon)``; one already past is skipped, never made late. A pending ring nothing
    wants any more (the thing closed, moved or cancelled, the ring removed) is cancelled, and one
    whose words or time changed is scheduled again. With ``thing_ids`` only those things' rings are
    looked at. The words are the ``thing.rem.*`` lines of :mod:`jarvis.shared.lang`.
    """
    with _atomic(conn):
        _sync(conn, zone=zone, now=now, thing_ids=thing_ids, horizon=horizon)


# --- carry-in ------------------------------------------------------------------------------


def _carried(conn: sqlite3.Connection) -> bool:
    return any(True for _ in iter_events_of_types(conn, ("thing.carried_in",)))


def _passed(one: Thing, now_ms: int, zone: tzinfo) -> bool:
    """Whether the last time ``one`` happens has ended."""
    slots = _slots(one, zone)
    return bool(slots) and max(s.end_ms for s in slots) <= now_ms


def carry_in(  # noqa: C901, PLR0915 - four old stores, each its own few lines
    conn: sqlite3.Connection, *, zone: tzinfo, now: datetime,
    core_items: Sequence[Mapping[str, Any]], before_min: int,
) -> dict[str, int] | None:
    """Carry his memos, reminders, commitments and core-memory to-dos into the ledger, once.

    ``None`` and nothing written when a ``thing.carried_in`` exists. Otherwise, in one transaction:
    every memo (exact repeats merged) is an untimed ``do``; every pending reminder of his own is a
    ``do`` at its time ringing at it, and the old reminder is cancelled (one already due stays, to
    ring late as ADR 0179 says); every live commitment is a ``go`` keeping its hex, with its ring
    ``before_min`` ahead (``-1d@20:00`` for a whole day), the pending ``reminder-cm*`` reminders
    cancelled, and one he never confirmed whose time has passed cancelled; every item of core
    memory's 承诺和待办 (``core_items``, read by the runtime from memory.db) is an untimed ``do``.
    Returns the counts the ``thing.carried_in`` event holds.
    """
    if _carried(conn):
        return None
    stamp = _ms(now)
    counts = dict.fromkeys((
        "memos", "reminders", "commitments", "core_items", "cancelled", "reminders_cancelled",
        "skipped",
    ), 0)
    made: list[str] = []

    def carry(
        group: str, one: Thing, evidence: str, ref: str, *, ts: int | None = None,
    ) -> Thing | None:
        try:
            carried, created = _add(
                conn, one, by="carry_in", evidence=evidence, ref=ref, zone=zone,
                actor="jarvis_runtime", stamp=stamp if ts is None else ts,
            )
        except ValueError:
            LOGGER.warning("things: %s %r cannot be carried in", group, ref, exc_info=True)
            counts["skipped"] += 1
            return None
        if created:
            counts[group] += 1
            made.append(carried.thing_id)
        return carried if created else None

    def cancel(rid: str) -> None:
        _append(
            conn, "reminder.cancelled", {"reminder_id": rid, "action_id": _CARRY_ACTION},
            actor="jarvis_runtime", stamp=stamp, correlation={"action_id": _CARRY_ACTION},
        )

    with _atomic(conn):
        if _carried(conn):
            return None
        seen: set[str] = set()
        for memo in iter_events_of_types(conn, ("memo.captured",)):
            text = str(memo.payload.get("text") or "").strip()
            sameness = " ".join(text.split()).casefold()
            if not sameness or sameness in seen:
                continue
            seen.add(sameness)
            title, note = _words(text)
            carry(
                "memos", Thing(new_id(), title, "do", note=note), text[:EVIDENCE_CHARS],
                memo.event_uid, ts=memo.ts_epoch_ms,
            )

        actions = {
            str(e.payload["reminder_id"]): str(e.payload.get("action_id") or "")
            for e in iter_events_of_types(conn, ("reminder.scheduled",))
        }
        for old in reminder_state.pending(conn):
            action = actions.get(old.reminder_id, "")
            if old.reminder_id.startswith(commitments.REMINDER_PREFIX):
                cancel(old.reminder_id)
                counts["reminders_cancelled"] += 1
                continue
            if (
                old.reminder_id.startswith(RING_PREFIX)
                or action in (_INTERVIEW_ACTION, _RING_ACTION)
                or action.startswith(departures.ID_PREFIX)
                or not old.text.strip()
            ):
                continue
            title, note = _words(old.text)
            at = datetime.fromtimestamp(old.due_at_ms / 1000, zone).isoformat(timespec="seconds")
            carry(
                "reminders",
                Thing(new_id(), title, "do", start_at=at, note=note, rings=("-0m",)),
                old.text[:EVIDENCE_CHARS], old.reminder_id,
            )
            if old.due_at_ms > stamp:  # one already due is left to ring late
                cancel(old.reminder_id)
                counts["reminders_cancelled"] += 1

        for held in commitments.live(conn):
            hex_id = held.commitment_id.removeprefix(commitments.ID_PREFIX)
            whole_day = held.all_day
            one = Thing(
                ID_PREFIX + hex_id, held.title[:TITLE_CHARS], "go",
                start_at="" if whole_day else held.start_at,
                end_at=""
                if whole_day or (held.end_at and datetime.fromisoformat(held.end_at) < held.start)
                else held.end_at,
                on_day=held.start.astimezone(zone).date().isoformat() if whole_day else "",
                weekly_until=held.weekly_until, skip_dates=held.skip_dates, place=held.place,
                link=held.link, rings=("-1d@20:00",) if whole_day else (f"-{before_min}m",),
                private=held.private, confirmed=held.confirmed,
                source="mail" if held.source == "mail" else "said", source_ref=held.source_ref,
                sender=held.sender_domain,
            )
            made_one = carry("commitments", one, held.evidence, held.commitment_id)
            if made_one is not None and not held.confirmed and _passed(made_one, stamp, zone):
                _write(
                    conn, "thing.closed", made_one.thing_id,
                    {"state": "cancelled", "why": "time passed, never confirmed"},
                    by="carry_in", evidence=held.evidence, ref=held.commitment_id,
                    actor="jarvis_runtime", stamp=stamp,
                )
                counts["cancelled"] += 1

        for item in core_items:
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            ref = str(
                item.get("id") or item.get("topic")
                or "t" + hashlib.sha256(text.encode()).hexdigest()[:10],
            )
            title, note = _words(text)
            carry("core_items", Thing(new_id(), title, "do", note=note), text, f"core:{ref}")

        _sync(conn, zone=zone, now=now, thing_ids=made, horizon=RING_HORIZON)
        _append(
            conn, "thing.carried_in", {"version": _CARRY_VERSION, "counts": counts},
            actor="jarvis_runtime", stamp=stamp,
        )
    return counts
