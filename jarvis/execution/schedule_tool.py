"""ADR 0299: all he has to go to or do is one ledger, and ``schedule`` is her one tool for it.

It reads the ledger (:mod:`jarvis.state.things`: a day, a span, a search, what is overdue, what has
no time) and adds, moves, marks done or paid, cancels, confirms and reopens things in it, as
``her``, with his words as the evidence and the running event as the ref. Times are his local
time: the tool resolves them in his zone, never the model. The pinned bus (ADR 0246) is a row of
the read. Facts about him are ``keep`` (ADR 0245).
"""

from __future__ import annotations

import calendar
import math
import re
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution import speakable
from jarvis.execution.tools import Tool, ToolError, _get_running_event_uid
from jarvis.shared import CallerPrincipal
from jarvis.state import commitments, departures, things
from jarvis.state.daily_contract import encoded
from jarvis.state.daily_report import resolve_zone

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import tzinfo

    from jarvis.execution.tools import ToolContext

_DESCRIPTION: Final = (
    "Use for anything he has to go to or do, with or without a time: appointments, plans with"
    " someone, reminders, to-dos, things to fix or buy, bills; to see what is on, add one, move"
    " it, mark it done or paid, cancel it, confirm one a mail made, or reopen it. Not for lasting"
    " facts about him (keep) or past days (my_day). Returns one sentence to say and the things"
    " with their short ids; a write returns what happened."
)
_ACTIONS: Final = ("read", "add", "move", "done", "paid", "cancel", "confirm", "reopen")
_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(_ACTIONS),
            "description": (
                "read (default): what is on and what is open. add: a new thing, needs what."
                " move: a new time, place or ring, needs id. done, paid, cancel, reopen: needs"
                " id. confirm: he says a thing a mail made is his, needs id."
            ),
        },
        "what": {
            "type": "string",
            "description": (
                "add: his words for the thing, in the language he used. read: words to find in"
                " titles, places and notes, over everything open and the next 60 days. Other"
                " actions: his reason, optional."
            ),
        },
        "id": {
            "type": "string",
            "description": (
                "The short id a read or add gave, e.g. 3fa9c1; every action but read and add."
            ),
        },
        "at": {
            "type": "string",
            "description": (
                "add, move: when it starts, his local time, e.g. 2026-10-20 15:00; a date alone,"
                " 2026-10-20, is a thing on that day with no hour. read: a day (today, tomorrow,"
                " Mon, 2026-10-20); leave out for the rest of today and tomorrow."
            ),
        },
        "ends": {
            "type": "string",
            "description": "add, move: when it ends, his local time; only with a clock time in at.",
        },
        "due": {
            "type": "string",
            "description": (
                "add, move: by when, a local date or time (a bill, a deadline); not with at."
            ),
        },
        "to": {
            "type": "string",
            "description": "read: the last day (included) of a span of at most 14 days from at.",
        },
        "kind": {
            "type": "string",
            "enum": list(things.KINDS),
            "description": (
                "add: go (he goes somewhere or meets someone) or do (he does it); default do."
            ),
        },
        "remind": {
            "type": "array",
            "items": {"type": "integer"},
            "description": (
                "add, move: minutes before to ring, e.g. [30] or [60, 10]; 0 rings at the time;"
                " [] for no ring. Left out: a do thing with at rings at the time, a go thing with"
                " a clock time rings the usual lead before, a go thing on a day rings the evening"
                " before; due and untimed things do not ring."
            ),
        },
        "place": {
            "type": "string",
            "description": "add, move: where it is, or a link (https://...).",
        },
        "weekly_until": {
            "type": "string",
            "description": "add: the last date, 2026-12-15, of a thing that repeats every week.",
        },
        "occurrence": {
            "type": "string",
            "description": (
                "move, done, paid, cancel, reopen: the date, 2026-10-27, of one time of a weekly"
                " thing; a read gives it. Leave out for the whole thing."
            ),
        },
        "private": {
            "type": "boolean",
            "description": "add: true for a thing whose ring and lines say only its time.",
        },
        "whole_sender": {
            "type": "boolean",
            "description": "confirm, cancel: that sender's later mail too.",
        },
        "page": {"type": "integer", "description": "read: 2 gives items 9-16 of the same request."},
    },
    "additionalProperties": False,
}
_ARGS: Final = {
    "read": ("what", "at", "to", "page"),
    "add": ("what", "at", "ends", "due", "kind", "remind", "place", "weekly_until", "private"),
    "move": ("id", "what", "at", "ends", "due", "remind", "place", "occurrence"),
    "done": ("id", "what", "occurrence"),
    "paid": ("id", "what", "occurrence"),
    "cancel": ("id", "what", "occurrence", "whole_sender"),
    "confirm": ("id", "what", "whole_sender"),
    "reopen": ("id", "what", "occurrence"),
}
_CLOSES: Final = {"done": "done", "paid": "paid", "cancel": "cancelled"}
_SPAN_DAYS: Final = 14
_FIND_DAYS: Final = 60
_AHEAD_MS: Final = 366 * 86_400_000
_PAGE: Final = 8
_BUDGET: Final = 4000
_SOON: Final = timedelta(hours=6)
_PAST_GRACE: Final = timedelta(minutes=1)
_HORIZON: Final = timedelta(days=366)
_HOUR: Final = 60
_NOON: Final = 12
_WHAT_CHARS: Final = 120
_WHERE_CHARS: Final = 80
_LINK_CHARS: Final = 300
_NEXT_CHARS: Final = 60
_NO_TIME_NAMED: Final = 4
_PRIVATE: Final = "something private"
_UNCONFIRMED: Final = " (from a mail, not confirmed yet)"
_EVENING: Final = "-1d@20:00"
_RELATIVE: Final = {-1: "yesterday", 0: "today", 1: "tomorrow"}
_ISO_DAY: Final = re.compile(r"\d{4}-\d{2}-\d{2}(?:[t ]|$)")
_DAY: Final = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class _Window:
    first: date
    last: date
    start: datetime
    end: datetime
    label: str


@dataclass(frozen=True)
class _Row:
    """One item of a read: when it sorts, when it is over, and the fields the model reads."""

    at: int
    over: int
    fields: dict[str, Any]


@dataclass(frozen=True)
class _Time:
    """When a thing is: a start (and an end), a day, a due, or none of them."""

    start_at: str = ""
    end_at: str = ""
    on_day: str = ""
    due: str = ""
    weekly_until: str = ""

    def __bool__(self) -> bool:
        return bool(self.start_at or self.on_day or self.due)


def _text(args: Mapping[str, Any], name: str) -> str:
    value = args.get(name)
    return value.strip() if isinstance(value, str) else ""


def _given(args: Mapping[str, Any], name: str) -> bool:
    """Whether the model passed ``name`` with something in it (an empty string is nothing)."""
    value = args.get(name)
    return value is not None and value != "" and value is not False


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _named(day: date, today: date) -> str:
    """``tomorrow (Tue Oct 13)``, else ``Wed Oct 14``."""
    if (rel := _RELATIVE.get((day - today).days)) is not None:
        return f"{rel} ({day:%a %b} {day.day})"
    return speakable.day_name(day, today)


def _midnight(day: date, zone: tzinfo) -> datetime:
    """Local midnight of ``day``: its own offset, so a 23 or 25 hour day keeps its bounds."""
    return datetime.combine(day, time(), zone)


def _weekday(word: str) -> int | None:
    return next(
        (i for i, name in enumerate(calendar.day_name)
         if len(word) >= 3 and name.lower().startswith(word)),  # noqa: PLR2004 - mon, tue, ...
        None,
    )


def _day(value: str, today: date, after: date, *, inclusive: bool = False) -> date:
    """A local day from today, tomorrow, yesterday, an ISO date or a weekday name.

    A weekday is the next one after ``after`` (``after`` itself too when ``inclusive``).
    """
    word = value.strip().lower().removeprefix("next ").removeprefix("this ").rstrip(".")
    if _ISO_DAY.match(word):
        word = word[:10]  # a full timestamp names its day
    if word in ("today", "tomorrow", "yesterday"):
        return today + timedelta(days={"today": 0, "tomorrow": 1, "yesterday": -1}[word])
    if (index := _weekday(word)) is not None:
        ahead = (index - after.weekday()) % 7
        return after + timedelta(days=ahead if ahead or inclusive else 7)
    try:
        return date.fromisoformat(word)
    except ValueError:
        msg = f"schedule: a day looks like today, tomorrow, Mon or 2026-10-15, not {value!r}"
        raise ToolError(msg, code="invalid_arguments") from None


def _clock_span(a: datetime, b: datetime) -> str:
    """``1:30-2:30 pm``; ``11:30 am-1:00 pm`` when it crosses noon."""
    if (a.hour < _NOON) == (b.hour < _NOON):
        return f"{speakable.clock(a)[:-3]}-{speakable.clock(b)}"
    return f"{speakable.clock(a)}-{speakable.clock(b)}"


def _in(delta: timedelta) -> str:
    minutes = max(1, math.ceil(delta.total_seconds() / _HOUR))
    if minutes < _HOUR:
        return f"in {minutes} min"
    hours, rest = divmod(minutes, _HOUR)
    return f"in {hours} h {rest} min" if rest else f"in {hours} h"


def _ahead(at: datetime, now: datetime) -> str:
    """``, in 45 min`` for a moment within the next hours, else nothing."""
    return f", {_in(at - now)}" if now < at <= now + _SOON else ""


def _said(at: datetime, now: datetime, zone: tzinfo) -> str:
    """``at`` as he would say it: ``tomorrow (Mon) 9:00 am``, else ``Mon Oct 12, 3:00 pm``."""
    local, today = at.astimezone(zone), now.astimezone(zone).date()
    if (local.date() - today).days in _RELATIVE:
        return f"{speakable.day_name(local.date(), today)} {speakable.clock(local)}"
    return f"{speakable.day_name(local.date(), today)}, {speakable.clock(local)}"


def _name(one: things.Thing) -> str:
    """The thing as the sentences name it: quoted, and marked when a mail made it."""
    unsure = " (from a mail, not confirmed)" if one.source == "mail" and not one.confirmed else ""
    return f'"{speakable.cut(one.title, _WHAT_CHARS)}"{unsure}'


def _when_of(one: things.Thing) -> _Time:
    return _Time(one.start_at, one.end_at, one.on_day, one.due, one.weekly_until)


def _shape(when: _Time) -> str:
    """Which kind of time: a clock time, a whole day, a due, or none."""
    return "clock" if when.start_at else "day" if when.on_day else "due" if when.due else ""


def _place(raw: str) -> tuple[str, str]:
    """``(place, link)``: a link goes to the link, words to the place."""
    return ("", raw) if raw.startswith("http") else (speakable.cut(raw, _WHERE_CHARS), "")


def _fit(answer: dict[str, Any]) -> dict[str, Any]:
    """The answer within 4000 characters: links go first, from the last row."""
    for row in reversed(answer["items"]):
        if len(encoded(answer)) <= _BUDGET:
            break
        row.pop("link", None)
    return answer


def _holds(words: list[str]) -> Callable[[things.Thing], bool]:
    """Whether a thing's title, place or note holds every word; a private one is only its name."""

    def holds(one: things.Thing) -> bool:
        text = _PRIVATE if one.private else f"{one.title} {one.place} {one.note}"
        return all(word in text.casefold() for word in words)

    return holds


class _Schedule:
    """The tool's handler: a read of the ledger, and a write for each thing he asks for."""

    def __init__(self, zone: tzinfo, before_min: int, clock: Callable[[], datetime]) -> None:
        self._zone = zone
        self._before_min = before_min
        self._clock = clock

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
        """Do what ``action`` says (reading when it is left out) and answer in one sentence."""
        action = self._checked(args)
        now = self._clock()
        if action == "read":
            return self._read(args, ctx, now)
        if action == "add":
            return self._add(args, ctx, now)
        one = self._find(ctx, _text(args, "id"), action)
        args = self._dated(args, one, action)
        if action == "move":
            return self._move(one, args, ctx, now)
        if action == "confirm":
            return self._confirm(one, args, ctx, now)
        if action == "reopen":
            return self._reopen(one, args, ctx, now)
        return self._close(action, one, args, ctx, now)

    @staticmethod
    def _checked(args: Mapping[str, Any]) -> str:
        """The action asked for, once every argument is one the tool has and the action takes."""
        if unknown := sorted(set(args) - set(_SCHEMA["properties"])):
            msg = f"schedule: no argument {unknown[0]!r}; use {', '.join(_SCHEMA['properties'])}"
            raise ToolError(msg, code="invalid_arguments")
        action = _text(args, "action") or "read"
        if action not in _ACTIONS:
            msg = f"schedule: action must be one of {', '.join(_ACTIONS)}, not {action!r}"
            raise ToolError(msg, code="invalid_arguments")
        for name in args:
            if name != "action" and _given(args, name) and name not in _ARGS[action]:
                goes = " or ".join(a for a, names in _ARGS.items() if name in names)
                msg = (
                    f"schedule: {name} does not go with action={action}; it goes with {goes}."
                    " Leave it out or change action"
                )
                raise ToolError(msg, code="invalid_arguments")
        for name in ("private", "whole_sender"):  # a word is not read as true or false
            if args.get(name) is not None and not isinstance(args[name], bool):
                msg = f"schedule: {name} is true or false, not {args[name]!r}"
                raise ToolError(msg, code="invalid_arguments")
        return action

    @staticmethod
    def _dated(args: Mapping[str, Any], one: things.Thing, action: str) -> Mapping[str, Any]:
        """The day line's ``commitment-<hex>@<day>`` names one date of a weekly series."""
        day = _text(args, "id").partition("@")[2]
        if day and one.weekly_until and "occurrence" in _ARGS[action] and not _given(
            args, "occurrence",
        ):
            return {**args, "occurrence": day}
        return args

    # --- the words a write is given -------------------------------------------------------

    def _by(self, ctx: ToolContext, evidence: str, now: datetime) -> dict[str, Any]:
        """Who wrote, on what proof and when: what every write to the ledger is given."""
        return {
            "by": "her", "evidence": evidence,
            "ref": _get_running_event_uid(ctx.conn, ctx.action_id),
            "zone": self._zone, "now": now,
        }

    @staticmethod
    def _find(ctx: ToolContext, raw: str, action: str) -> things.Thing:
        """The thing a short id names; the day line's ``commitment-<hex>@<day>`` is read too."""
        key = raw.lower().partition("@")[0].removeprefix(commitments.ID_PREFIX)
        if not key:
            msg = (
                f"schedule: {action} needs id, the short id a read or add gave;"
                " schedule(what=<words>) finds it"
            )
            raise ToolError(msg, code="invalid_arguments")
        try:
            return things.find(ctx.conn, key)
        except LookupError as exc:
            msg = f"schedule: {exc}; schedule() lists what is open with their ids"
            raise ToolError(msg, code="not_found") from None

    @staticmethod
    def _refused(exc: ValueError) -> ToolError:
        return ToolError(f"schedule: {exc}; nothing was written", code="invalid_arguments")

    def _local_day(self, raw: str, name: str, now: datetime, *, upcoming: bool) -> str:
        """``raw`` (``2026-10-20``) as a date; ``upcoming`` refuses a past day or one a year off."""
        today = now.astimezone(self._zone).date()
        example = f"{today + timedelta(days=1)}"
        try:
            day = date.fromisoformat(raw)
        except ValueError:
            msg = f'schedule: {name} "{raw}" is not a date like {example}'
            raise ToolError(msg, code="invalid_time") from None
        if upcoming and day < today:
            msg = (
                f"schedule: {name} {raw} is before today ({today}); give today or a later date,"
                f" e.g. {example}"
            )
            raise ToolError(msg, code="time_in_past")
        if upcoming and day > today + _HORIZON:
            msg = f"schedule: {name} {raw} is more than a year ahead; give a nearer date"
            raise ToolError(msg, code="too_far_ahead")
        return raw

    def _local_time(self, raw: str, name: str, now: datetime, *, upcoming: bool) -> str:
        """``raw`` (``2026-10-20 15:00``, his zone unless it says its own) as an aware time."""
        example = f"{now.astimezone(self._zone) + timedelta(days=1):%Y-%m-%d} 09:00"
        try:
            at = datetime.fromisoformat(raw)
        except ValueError:
            msg = (
                f'schedule: {name} "{raw}" is not a local date and time like {example}; words such'
                " as tomorrow or 9am are not understood, so work the date out from the time in"
                " your state line"
            )
            raise ToolError(msg, code="invalid_time") from None
        at = at.replace(tzinfo=self._zone) if at.tzinfo is None else at.astimezone(self._zone)
        if upcoming and at < now - _PAST_GRACE:
            msg = (
                f"schedule: {_said(at, now, self._zone)} is in the past (it is"
                f" {_said(now, now, self._zone)} now); give a later local time, e.g. {example}"
            )
            raise ToolError(msg, code="time_in_past")
        if upcoming and at > now + _HORIZON:
            msg = f"schedule: {name} {raw} is more than a year ahead; give a nearer local time"
            raise ToolError(msg, code="too_far_ahead")
        return at.isoformat(timespec="seconds")

    def _times(self, args: Mapping[str, Any], now: datetime) -> _Time:
        """The time ``at`` (with ``ends``) or ``due`` give: a start, a day or a due, else none."""
        at, ends, due = _text(args, "at"), _text(args, "ends"), _text(args, "due")
        if at and due:
            msg = "schedule: a thing has either at (when it starts) or due (by when), not both"
            raise ToolError(msg, code="invalid_arguments")
        if due:
            if _DAY.fullmatch(due):
                return _Time(due=self._local_day(due, "due", now, upcoming=True))
            return _Time(due=self._local_time(due, "due", now, upcoming=True))
        if ends and not (at and not _DAY.fullmatch(at)):
            msg = "schedule: ends goes with a clock time in at, e.g. at=2026-10-20 15:00"
            raise ToolError(msg, code="invalid_arguments")
        if not at:
            return _Time()
        if _DAY.fullmatch(at):
            return _Time(on_day=self._local_day(at, "at", now, upcoming=True))
        end = self._clock_end(ends, now) if ends else ""
        return _Time(start_at=self._local_time(at, "at", now, upcoming=True), end_at=end)

    def _clock_end(self, ends: str, now: datetime) -> str:
        if _DAY.fullmatch(ends):
            msg = "schedule: ends is a clock time, e.g. 2026-10-20 16:30"
            raise ToolError(msg, code="invalid_time")
        return self._local_time(ends, "ends", now, upcoming=False)

    @staticmethod
    def _ring_list(raw: object) -> tuple[str, ...]:
        """The rings ``remind`` asks for: whole minutes before the time, as the ledger says them."""
        if not isinstance(raw, list) or any(
            isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in raw
        ):
            msg = "schedule: remind is a list of whole minutes before, e.g. [30]; [] for none"
            raise ToolError(msg, code="invalid_arguments")
        return tuple(dict.fromkeys(f"-{n}m" for n in raw))

    def _default_rings(self, kind: str, when: _Time) -> tuple[str, ...]:
        """What rings when ``remind`` is left out: the ledger's rule for every writer."""
        return things.default_rings(
            kind, start_at=when.start_at, on_day=when.on_day, before_min=self._before_min,
        )

    def _at_words(self, when: _Time, now: datetime) -> str:
        """`` for tomorrow (Tue) 9:00 am``, `` for Thu Oct 22``, ``, due Fri Oct 16``, or none."""
        today = now.astimezone(self._zone).date()
        weekly = f", every week until {when.weekly_until}" if when.weekly_until else ""
        if when.start_at:
            return f" for {_said(datetime.fromisoformat(when.start_at), now, self._zone)}{weekly}"
        if when.on_day:
            return f" for {speakable.day_name(date.fromisoformat(when.on_day), today)}{weekly}"
        if not when.due:
            return " with no time"
        if _DAY.fullmatch(when.due):
            return f", due {speakable.day_name(date.fromisoformat(when.due), today)}"
        return f", due {_said(datetime.fromisoformat(when.due), now, self._zone)}"

    def _rung(self, one: things.Thing, now: datetime) -> str:
        """``; rings 60 min before`` for the rings still ahead; one already past is said."""
        if not one.rings:
            return ""
        if one.start_at:
            anchor = datetime.fromisoformat(one.start_at)
        elif one.on_day:
            anchor = _midnight(date.fromisoformat(one.on_day), self._zone)
        else:
            anchor = datetime.fromtimestamp(things.due_ms(one, self._zone) / 1000, UTC)
        said: list[str] = []
        for ring in one.rings:
            if ring == _EVENING:
                evening = anchor.astimezone(self._zone).date() - timedelta(days=1)
                at, words = datetime.combine(evening, time(20), self._zone), "the evening before"
            else:
                minutes = int(ring[1:-1])
                at = anchor - timedelta(minutes=minutes)
                words = "at the time" if minutes == 0 else f"{minutes} min before"
            if at >= now - _PAST_GRACE:
                said.append(words)
        if not said:
            return "; its ring time has passed, so nothing will ring"
        return f"; rings {' and '.join(said)}"

    # --- writes ---------------------------------------------------------------------------

    def _add(self, args: Mapping[str, Any], ctx: ToolContext, now: datetime) -> dict[str, Any]:
        """Keep a new thing; the same words at the same time again write nothing."""
        text = _text(args, "what")
        if not text:
            msg = 'schedule: add needs what, his words, e.g. schedule(action="add", what="fix it")'
            raise ToolError(msg, code="empty_text")
        kind = _text(args, "kind") or "do"
        if kind not in things.KINDS:
            msg = f"schedule: kind must be go or do, not {kind!r}"
            raise ToolError(msg, code="invalid_arguments")
        when = replace(self._times(args, now), weekly_until=_text(args, "weekly_until"))
        rings = (
            self._ring_list(args["remind"]) if args.get("remind") is not None
            else self._default_rings(kind, when)
        )
        if rings and not when:
            msg = "schedule: remind needs a time to ring from: give at or due, or leave remind out"
            raise ToolError(msg, code="invalid_arguments")
        place, link = _place(_text(args, "place"))
        title, note = (text, "") if len(text) <= things.TITLE_CHARS else (
            text.splitlines()[0].strip()[: things.TITLE_CHARS], text[: things.NOTE_CHARS],
        )
        one = things.Thing(
            things.new_id(), title, kind, start_at=when.start_at, end_at=when.end_at,
            on_day=when.on_day, due=when.due, weekly_until=when.weekly_until, place=place,
            link=link, note=note, rings=rings, private=args.get("private") is True,
        )
        try:
            kept, created = things.add(ctx.conn, one, **self._by(ctx, text, now))
        except ValueError as exc:
            raise self._refused(exc) from None
        head = f"Kept {_name(kept)}" if created else f"Already kept, nothing new: {_name(kept)}"
        said = self._at_words(_when_of(kept), now) + (self._rung(kept, now) if created else "")
        return {
            "summary": f"{head}{said} (id {things.short_id(kept)}).",
            "id": things.short_id(kept), "state": kept.state,
        }

    def _later(
        self, one: things.Thing, args: Mapping[str, Any], now: datetime, occurrence: str,
    ) -> _Time:
        """The time a move gives: ``at`` or ``due``, or a new end for the start it has; or none."""
        if _given(args, "ends") and not (_given(args, "at") or _given(args, "due")):
            if not one.start_at:
                msg = f"schedule: {_name(one)} has no clock time; give at as well as ends"
                raise ToolError(msg, code="invalid_arguments")
            return _Time(start_at=one.start_at, end_at=self._clock_end(_text(args, "ends"), now))
        later = self._times(args, now)
        if later.start_at and not later.end_at and one.end_at and one.start_at and not occurrence:
            span = datetime.fromisoformat(one.end_at) - datetime.fromisoformat(one.start_at)
            end = datetime.fromisoformat(later.start_at) + span  # it keeps its length
            return replace(later, end_at=end.isoformat(timespec="seconds"))
        return later

    def _changes(
        self, one: things.Thing, args: Mapping[str, Any], later: _Time, occurrence: str,
    ) -> dict[str, Any]:
        """The place and rings a move changes besides the time."""
        fields: dict[str, Any] = {}
        timed = bool(later or one.start_at or one.on_day or one.due)
        if _given(args, "place"):
            fields["place"], fields["link"] = _place(_text(args, "place"))
        if args.get("remind") is not None:
            fields["rings"] = list(self._ring_list(args["remind"]))
            if fields["rings"] and not timed:
                msg = "schedule: remind needs a time to ring from: give at or due as well"
                raise ToolError(msg, code="invalid_arguments")
        elif later and not occurrence and _shape(later) != _shape(_when_of(one)):
            # a clock time, a day, a due and no time ring differently: the usual ring of the new one
            if (rings := list(self._default_rings(one.kind, later))) != list(one.rings):
                fields["rings"] = rings
        return fields

    def _move(
        self, one: things.Thing, args: Mapping[str, Any], ctx: ToolContext, now: datetime,
    ) -> dict[str, Any]:
        """Give an open thing a new time, place or rings; the time it had stays in its record."""
        self._open(one, "move")
        occurrence = _text(args, "occurrence")
        later = self._later(one, args, now, occurrence)
        fields = self._changes(one, args, later, occurrence)
        if not later and not fields:
            msg = (
                "schedule: move needs the new time (at, ends or due), or a place, or remind;"
                " nothing was changed"
            )
            raise ToolError(msg, code="invalid_arguments")
        by = self._by(ctx, _text(args, "what") or one.title, now)
        try:
            moved = one
            if later:
                moved = things.move(
                    ctx.conn, one.thing_id, start_at=later.start_at, end_at=later.end_at,
                    on_day=later.on_day, due=later.due, occurrence=occurrence, **by,
                )
            if fields:
                moved = things.change(ctx.conn, one.thing_id, fields, **by)
        except ValueError as exc:
            raise self._refused(exc) from None
        summary = self._moved(one, moved, later, occurrence, now)
        if "rings" in fields:
            summary += self._rung(moved, now)
        return {"summary": f"{summary}.", "id": things.short_id(moved), "state": moved.state}

    def _moved(
        self, one: things.Thing, moved: things.Thing, later: _Time, occurrence: str,
        now: datetime,
    ) -> str:
        """What a move did, in words: a new time and the one it had, or just a new end."""
        name = _name(moved)
        if later.start_at and later.end_at and later.start_at == one.start_at and not occurrence:
            end = datetime.fromisoformat(later.end_at)
            return f"Changed {name}; it now ends {_said(end, now, self._zone)}"
        if not later:
            return f"Changed {name}"
        if occurrence:
            return f"Moved {name} on {occurrence}{self._at_words(later, now)}"
        was = self._at_words(_when_of(one), now)
        return f"Moved {name}{self._at_words(later, now)} (it was{was})"

    def _open(self, one: things.Thing, verb: str) -> None:
        if one.state != "open":
            msg = (
                f"schedule: {_name(one)} is {one.state}, so it cannot be {verb}d;"
                ' schedule(action="reopen", id=...) makes it open again first'
            )
            raise ToolError(msg, code="closed")

    def _close(
        self, action: str, one: things.Thing, args: Mapping[str, Any], ctx: ToolContext,
        now: datetime,
    ) -> dict[str, Any]:
        """Mark a thing (or one date of a weekly one) done, paid or cancelled."""
        wanted, occurrence = _CLOSES[action], _text(args, "occurrence")
        whole, why = args.get("whole_sender") is True, _text(args, "what")
        self._sender_of(one, whole=whole)
        held = one.state if one.state != "open" else one.date_states.get(occurrence, "open")
        name = _name(one) + (f" on {occurrence}" if occurrence else "")
        if held != "open":
            summary = (
                f"Already {held}, nothing changed: {name}." if held == wanted
                else f"{name} is {held} already, nothing changed; reopen it first to mark it"
                f" {wanted}."
            )
        else:
            if one.weekly_until and not occurrence and wanted != "cancelled":
                msg = (
                    f"schedule: {name} repeats every week: say which date with occurrence,"
                    f" e.g. {self._next_date(ctx, one, now)}"
                )
                raise ToolError(msg, code="invalid_arguments")
            try:
                things.close(
                    ctx.conn, one.thing_id, wanted, occurrence=occurrence,
                    why=why or ("not his" if whole else "he cancelled it"),
                    **self._by(ctx, why or one.title, now),
                )
            except ValueError as exc:
                raise self._refused(exc) from None
            every = ", the whole series" if one.weekly_until and not occurrence else ""
            summary = (
                f"Cancelled {name}{every}." if wanted == "cancelled"
                else f"Marked {wanted}: {name}."
            )
        if whole:
            summary += self._sender_rule(one, "never", ctx)
        state = held if held != "open" else wanted
        return {"summary": summary, "id": things.short_id(one), "state": state}

    def _next_date(self, ctx: ToolContext, one: things.Thing, now: datetime) -> str:
        """The next date of a weekly thing, as an example of ``occurrence``."""
        first = _ms(now)
        found = things.occurrences(ctx.conn, first, first + 8 * 86_400_000, zone=self._zone)
        return next((o.day for o in found if o.thing.thing_id == one.thing_id), "2026-10-27")

    def _confirm(
        self, one: things.Thing, args: Mapping[str, Any], ctx: ToolContext, now: datetime,
    ) -> dict[str, Any]:
        """He says a thing a mail made is his; optionally every later mail of its sender."""
        whole = args.get("whole_sender") is True
        self._sender_of(one, whole=whole)
        try:
            changed = things.confirm(
                ctx.conn, one.thing_id, **self._by(ctx, _text(args, "what") or one.title, now),
            )
        except ValueError as exc:
            raise self._refused(exc) from None
        name = _name(things.get(ctx.conn, one.thing_id))
        summary = (
            f"Confirmed {name}." if changed else f"Already confirmed, nothing changed: {name}."
        )
        if whole:
            summary += self._sender_rule(one, "always", ctx)
        return {"summary": summary, "id": things.short_id(one), "state": one.state}

    def _reopen(
        self, one: things.Thing, args: Mapping[str, Any], ctx: ToolContext, now: datetime,
    ) -> dict[str, Any]:
        """Make a closed thing (or one closed date of a weekly one) open again."""
        occurrence = _text(args, "occurrence")
        held = one.state if one.state != "open" else one.date_states.get(occurrence, "open")
        name = _name(one) + (f" on {occurrence}" if occurrence else "")
        if held == "open":
            summary = f"Already open, nothing changed: {name}."
        else:
            try:
                things.reopen(
                    ctx.conn, one.thing_id, occurrence=occurrence,
                    **self._by(ctx, _text(args, "what") or one.title, now),
                )
            except ValueError as exc:
                raise self._refused(exc) from None
            summary = f"Reopened {name}, it was {held}."
        return {"summary": summary, "id": things.short_id(one), "state": "open"}

    @staticmethod
    def _sender_of(one: things.Thing, *, whole: bool) -> None:
        if whole and not one.sender:
            msg = "schedule: that thing did not come from a mail, so it has no sender to remember"
            raise ToolError(msg, code="invalid_arguments")

    @staticmethod
    def _sender_rule(one: things.Thing, rule: str, ctx: ToolContext) -> str:
        if commitments.sender_rules(ctx.conn).get(one.sender) != rule:
            commitments.set_sender_rule(ctx.conn, one.sender, rule, actor="jarvis_llm")
        return f" Later mail from {one.sender} will {rule} count."

    # --- the read -------------------------------------------------------------------------

    def _read(self, args: Mapping[str, Any], ctx: ToolContext, now: datetime) -> dict[str, Any]:
        """The ledger for a window (or a search): what is on, overdue, due and without a time."""
        query = _text(args, "what")
        page = speakable.page_number(args.get("page"), "schedule", "items 9-16")
        if query and (_text(args, "at") or _text(args, "to")):
            msg = (
                f"schedule: what searches everything open and the next {_FIND_DAYS} days; pass it"
                ' without at or to. To keep a new thing, pass action="add"'
            )
            raise ToolError(msg, code="invalid_arguments")
        window = self._window(args, query, now)
        groups = self._gather(ctx, window, now, query.casefold().split())
        rows = [row for group in groups for row in group]
        shown = rows[(page - 1) * _PAGE : page * _PAGE]
        if page > 1 and not shown:
            end = max(1, (len(rows) - 1) // _PAGE + 1)
            summary = f"There is no page {page} of {window.label}; its items end at page {end}."
            return {"summary": summary, "items": [], "more": False}
        answer = {
            "summary": self._summary(window, query, groups, page, shown, now),
            "items": [row.fields for row in shown], "more": page * _PAGE < len(rows),
        }
        return _fit(answer)

    def _window(self, args: Mapping[str, Any], query: str, now: datetime) -> _Window:
        today = now.astimezone(self._zone).date()
        if query:
            end = today + timedelta(days=_FIND_DAYS)
            label = f'"{query}" among everything open and the next {_FIND_DAYS} days'
            return _Window(
                today, end - timedelta(days=1), _midnight(today, self._zone),
                _midnight(end, self._zone), label,
            )
        first_text, last_text = _text(args, "at"), _text(args, "to")
        if not first_text and not last_text:
            tomorrow = today + timedelta(days=1)
            label = (
                f"{_named(today, today)} from {speakable.clock(now.astimezone(self._zone))}"
                f" and {_named(tomorrow, today)}"
            )
            return _Window(
                today, tomorrow, now.astimezone(self._zone).replace(microsecond=0),
                _midnight(tomorrow + timedelta(days=1), self._zone), label,
            )
        first = _day(first_text, today, today) if first_text else today
        last = _day(last_text, today, first, inclusive=True) if last_text else first
        if last < first:
            msg = f"schedule: to={last_text} is before at={first_text}; to is the last day"
            raise ToolError(msg, code="invalid_arguments")
        if (last - first).days + 1 > _SPAN_DAYS:
            msg = f"schedule: a span is at most {_SPAN_DAYS} days; ask in parts for longer"
            raise ToolError(msg, code="invalid_arguments")
        label = _named(first, today) if first == last else (
            f"{_named(first, today)} to {_named(last, today)}"
        )
        return _Window(
            first, last, _midnight(first, self._zone),
            _midnight(last + timedelta(days=1), self._zone), label,
        )

    def _gather(
        self, ctx: ToolContext, window: _Window, now: datetime, words: list[str],
    ) -> tuple[list[_Row], list[_Row], list[_Row], list[_Row]]:
        """The rows to read, in four groups, each soonest first.

        The window's occurrences and the pinned bus; open things past their time; open things due
        in the window; open things with no time, oldest first. A search (``words``) reads on past
        the window to everything open.
        """
        holds = _holds(words)
        lo, hi = _ms(window.start), _ms(window.end)
        timed = [
            self._occurrence(occ, now)
            for occ in things.occurrences(ctx.conn, lo, hi, zone=self._zone, closed=True)
            if holds(occ.thing)
        ]
        seen = {row.fields["id"] for row in timed}
        if words:
            ahead = things.occurrences(ctx.conn, hi, hi + _AHEAD_MS, zone=self._zone)
            for occ in ahead:  # the first time of what is still to come
                if holds(occ.thing) and things.short_id(occ.thing) not in seen:
                    timed.append(self._occurrence(occ, now))
                    seen.add(things.short_id(occ.thing))
        timed += [
            row for row in self._bus(ctx, window, now)
            if all(word in str(row.fields["what"]).casefold() for word in words)
        ]
        overdue = [
            self._occurrence(occ, now, overdue=True)
            for occ in things.occurrences(ctx.conn, 0, _ms(now), zone=self._zone)
            if occ.end_ms <= _ms(now) and not occ.thing.weekly_until and holds(occ.thing)
            and things.short_id(occ.thing) not in seen
        ]
        late, due, untimed = self._open_rows(ctx, (lo, hi), now, holds, search=bool(words))
        overdue += late
        for group in (timed, overdue, due):
            group.sort(key=lambda row: row.at)
        return timed, overdue, due, untimed

    def _open_rows(
        self, ctx: ToolContext, bounds: tuple[int, int], now: datetime,
        holds: Callable[[things.Thing], bool], *, search: bool,
    ) -> tuple[list[_Row], list[_Row], list[_Row]]:
        """The open things past their due, those due in the window, and those with no time."""
        late: list[_Row] = []
        due: list[_Row] = []
        untimed: list[_Row] = []
        for one in things.open_things(ctx.conn):
            if not holds(one):
                continue
            if one.due:
                at = things.due_ms(one, self._zone)
                if at < _ms(now):
                    late.append(self._due(one, at, now, overdue=True))
                elif bounds[0] <= at < bounds[1] or search:
                    due.append(self._due(one, at, now, overdue=False))
            elif not (one.start_at or one.on_day):
                untimed.append(_Row(one.added_ms, one.added_ms, self._fields(one, "")))
        return late, due, untimed

    @staticmethod
    def _fields(one: things.Thing, when: str) -> dict[str, Any]:
        """The fewest fields the next step needs; a private thing is only its time."""
        fields: dict[str, Any] = {
            "id": things.short_id(one),
            "what": _PRIVATE if one.private else speakable.cut(one.title, _WHAT_CHARS),
        }
        if not one.confirmed:
            fields["what"] += _UNCONFIRMED
        if when:
            fields["when"] = when
        fields["kind"] = one.kind
        if not one.confirmed:
            fields["confirmed"] = False
        if not one.private:
            if place := speakable.cut(one.place, _WHERE_CHARS):
                fields["place"] = place
            if one.link and len(one.link) <= _LINK_CHARS:
                fields["link"] = one.link
        return fields

    def _occurrence(
        self, occ: things.Occurrence, now: datetime, *, overdue: bool = False,
    ) -> _Row:
        start = datetime.fromtimestamp(occ.start_ms / 1000, UTC)
        end = datetime.fromtimestamp(occ.end_ms / 1000, UTC)
        today = now.astimezone(self._zone).date()
        if occ.all_day:
            day = start.astimezone(self._zone).date()
            when = f"{speakable.day_name(day, today)}, all day"
        else:  # without an end of its own it lasts "an hour" only in the ledger: say its start
            when = self._clock_when(start, end if occ.thing.end_at else start, now)
        fields = self._fields(occ.thing, when)
        if occ.state != "open":
            fields["state"] = occ.state
        if occ.thing.weekly_until:
            fields["occurrence"] = occ.day
        if overdue:
            fields["overdue"] = True
        return _Row(occ.start_ms, occ.end_ms, fields)

    def _due(self, one: things.Thing, at: int, now: datetime, *, overdue: bool) -> _Row:
        today = now.astimezone(self._zone).date()
        due = datetime.fromtimestamp(at / 1000, self._zone)
        when = f"due {speakable.day_name(due.date(), today)}"
        if not _DAY.fullmatch(one.due):
            when += f" {speakable.clock(due)}"
        fields = self._fields(one, when)
        if overdue:
            fields["overdue"] = True
        return _Row(at, at, fields)

    def _clock_when(self, start: datetime, end: datetime, now: datetime) -> str:
        a, b = start.astimezone(self._zone), end.astimezone(self._zone)
        today = now.astimezone(self._zone).date()
        if b.date() == a.date():
            span = speakable.clock(a) if end == start else _clock_span(a, b)
        else:
            until = f"{speakable.day_name(b.date(), today)} {speakable.clock(b)}"
            span = f"{speakable.clock(a)} to {until}"
        state = ", now" if start <= now < end else _ahead(start, now)
        return f"{speakable.day_name(a.date(), today)} {span}{state}"

    def _bus(self, ctx: ToolContext, window: _Window, now: datetime) -> list[_Row]:
        """The pinned bus's trips that leave in the window: one row each, on the leave time."""
        now_ms = _ms(now)
        pin = departures.active(ctx.conn, now_ms)
        today = now.astimezone(self._zone).date()
        rows = []
        for trip in () if pin is None else pin.catchable(now_ms):
            leave = datetime.fromtimestamp(trip.leave_at_ms / 1000, UTC)
            if window.start <= leave < window.end:
                local = leave.astimezone(self._zone)
                bus = f"bus {trip.route} from {trip.board_stop} to {trip.to}"
                when = (
                    f"leave {speakable.day_name(local.date(), today)} {speakable.clock(local)}"
                    f"{_ahead(leave, now)}"
                )
                rows.append(_Row(trip.leave_at_ms, trip.leave_at_ms, {
                    "what": speakable.cut(bus, _WHAT_CHARS), "when": when, "kind": "bus",
                }))
        return rows

    def _summary(  # noqa: PLR0913 - the request, what was found, what is shown and the clock
        self, window: _Window, query: str, groups: tuple[list[_Row], ...], page: int,
        shown: list[_Row], now: datetime,
    ) -> str:
        timed, overdue, due, untimed = groups
        total = sum(len(group) for group in groups)
        if not total and query:
            return (
                f"Nothing matches {window.label}. Try fewer words, or ask for a day with at (and"
                " to for up to 14 days)."
            )
        if not total:
            return (
                f"Nothing on for {window.label}, nothing overdue and nothing waiting without a"
                " time. Ask for another day with at (and to for a span of up to 14 days), or"
                " search words with what."
            )
        counts = ", ".join(
            f"{n} {word}" for n, word in (
                (len(timed), "scheduled"), (len(overdue), "overdue"), (len(due), "due"),
                (len(untimed), "with no time"),
            ) if n
        )
        if not timed and not query:
            counts = f"nothing scheduled, {counts}"
        if page > 1:
            first, last = (page - 1) * _PAGE + 1, min(page * _PAGE, total)
            return f"{window.label}: items {first}-{last} of {total} ({counts})."
        lead = ""
        if upcoming := next(
            (r for r in timed if r.at > _ms(now) and "state" not in r.fields), None,
        ):
            what = speakable.cut(str(upcoming.fields["what"]), _NEXT_CHARS)
            lead = f" Next: {what}, {upcoming.fields['when']}."
        named = [f'"{r.fields["what"]}"' for r in shown if "when" not in r.fields][:_NO_TIME_NAMED]
        no_time = f" No time set: {', '.join(named)}." if named else ""
        unsure = sum(r.fields.get("confirmed") is False for g in groups for r in g)
        unsure_words = f" {unsure} from a mail, not confirmed yet." if unsure else ""
        paged = f" Showing 1-{_PAGE}; page=2 for the rest." if total > _PAGE else ""
        return f"{window.label}: {counts}.{lead}{no_time}{unsure_words}{paged}"


def build_schedule_tool(
    zone: tzinfo | None, before_min: int, clock: Callable[[], datetime] = speakable.now,
) -> tuple[Tool, ...]:
    """``schedule`` over the ledger of his things and his zone (``None``: the machine's).

    ``before_min`` is how many minutes before it a thing he goes to rings
    (``commitments.remind_before_min``, the lead the mail poller uses).
    """
    return (
        Tool(
            name="schedule",
            description=_DESCRIPTION,
            input_schema=_SCHEMA,
            handler=_Schedule(resolve_zone(None, zone)[1], before_min, clock).run,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM, CallerPrincipal.REGEX_ROUTER}),
            risk_level="L1",
            read_only=False,
            max_result_chars=_BUDGET,
            # The names it took over, and the Microsoft reads behind tool search (the mirror
            # brings Outlook and To Do into the ledger): a search that names one is told which
            # call took its place.
            supersedes={
                "create_memo": 'schedule(action="add", what=...)',
                "set_reminder": 'schedule(action="add", what=..., at=...)',
                "cancel_reminder": 'schedule(action="cancel", id=...)',
                "list_memos": "schedule()",
                "list_reminders": "schedule()",
                "my_schedule": "schedule(at=..., to=...)",
                "mcp__microsoft__get-calendar-view": "schedule(at=..., to=...)",
                "mcp__microsoft__list-todo-task-lists": "schedule()",
                "mcp__microsoft__list-todo-tasks": "schedule(at=..., to=...)",
            },
        ),
    )


__all__ = ["build_schedule_tool"]
