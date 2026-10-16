"""ADR 0248: what is on for him is one read-only tool: calendar, to-dos, reminders, a pinned bus.

The calendar and the to-dos are read from Microsoft just now, through the two Graph reads the
home already makes (``graph``), over the local day's own bounds; the reminders, the pinned bus
and the things he has to go to (ADR 0274) are read from the event log, so they are there when
Microsoft is not. A source that cannot be read is named and never passed off as an empty one.
"""

from __future__ import annotations

import calendar
import math
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution import speakable
from jarvis.execution.tools import Tool, ToolError
from jarvis.shared import CallerPrincipal
from jarvis.state import commitments, departures, reminders
from jarvis.state.daily_contract import encoded

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import tzinfo

    from jarvis.execution.tools import ToolContext

_DESCRIPTION: Final = (
    "Use for anything on his schedule: calendar events, interviews, meetings, appointments, "
    "to-dos due, reminders waiting, a pinned bus; call it before saying he is free or his "
    "calendar is clear. It only reads (reminders are set with keep). No arguments: today from "
    "now on, and tomorrow. date: a day (today, tomorrow, Mon, 2026-10-15); to: the last day of a "
    "span of up to 14 days. query: words to find in the next 60 days, e.g. Reliable Controls "
    "interview. page=2 gives items 9-16. confirmed=false: a mail made it, he has not said it is "
    "his; say so. Says which source could not be read."
)
_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "date": {
            "type": "string",
            "description": (
                "A day: today, tomorrow, a weekday (Mon) or 2026-10-15; omit for the rest of"
                " today and tomorrow."
            ),
        },
        "to": {
            "type": "string",
            "description": "Last day (included) of a span of at most 14 days from date or today.",
        },
        "query": {
            "type": "string",
            "description": "Words an item's title or place must hold, over the next 60 days.",
        },
        "page": {"type": "integer", "description": "2 gives items 9-16 of the same request."},
    },
    "additionalProperties": False,
}
_EVENT_FIELDS: Final = "id,subject,start,end,isAllDay,isCancelled,location,isOnlineMeeting,webLink"
_SPAN_DAYS: Final = 14
_FIND_DAYS: Final = 60
_PAGE: Final = 8
_BUDGET: Final = 4000
_SOON: Final = timedelta(hours=6)
_HOUR: Final = 60
_NOON: Final = 12
_WHAT_CHARS: Final = 120
_UNCONFIRMED: Final = " (from a mail, not confirmed yet)"
_WHERE_CHARS: Final = 80
_LINK_CHARS: Final = 300
_REASON_CHARS: Final = 200
_RELATIVE: Final = {-1: "yesterday", 0: "today", 1: "tomorrow"}
_NAMES: Final = {
    "calendar": ("calendar event", "calendar events"),
    "to-do": ("to-do", "to-dos"),
    "reminder": ("reminder", "reminders"),
    "bus": ("bus to catch", "buses to catch"),
    "commitment": ("appointment", "appointments"),
}
_READ_AS: Final = {"calendar": "calendar", "to-do": "to-dos", "reminder": "reminders"}
_GONE_AS: Final = {
    "calendar": "his calendar", "to-do": "his to-dos", "reminder": "his reminders",
}
_ISO_DAY: Final = re.compile(r"\d{4}-\d{2}-\d{2}(?:[t ]|$)")
_SIGN_IN: Final = re.compile(
    r"auth|log ?in|sign ?in|token|credential|expired|permission|\b40[13]\b", re.IGNORECASE,
)


@dataclass(frozen=True)
class _Window:
    first: date
    last: date
    start: datetime
    end: datetime
    label: str


@dataclass(frozen=True)
class _Row:
    """One item: when it sorts, when it is over, and the fields the model reads."""

    at: datetime
    over: datetime
    fields: dict[str, Any]


def _kind(row: _Row) -> str:
    """The count a row belongs to: an appointment's ``source`` is where it came from."""
    source: str = row.fields["source"]
    return "commitment" if source in commitments.SOURCES else source


def _text(args: Mapping[str, Any], name: str) -> str:
    value = args.get(name)
    return value.strip() if isinstance(value, str) else ""


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
        msg = (
            "my_schedule: a day looks like today, tomorrow, Mon or 2026-10-15, "
            f"not {value!r}"
        )
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


def _graph_time(stamp: Mapping[str, Any]) -> datetime:
    """Graph's ``{dateTime, timeZone}``; calendarView answers in UTC unless asked otherwise."""
    return datetime.fromisoformat(str(stamp["dateTime"])).replace(tzinfo=UTC)


def _view(start: datetime, end: datetime) -> dict[str, Any]:
    """The ``get-calendar-view`` arguments for ``[start, end)``: bounds with their own offset."""
    return {
        "startDateTime": start.isoformat(),
        "endDateTime": end.isoformat(),
        "select": _EVENT_FIELDS,
        "fetchAllPages": True,
    }


def _trouble(exc: Exception, who: str) -> str:
    """Why a source was not read, as the model should relay it."""
    detail = speakable.cut(str(exc), _REASON_CHARS)
    if isinstance(exc, LookupError) and not isinstance(exc, KeyError):
        return f"{who} is not connected; tell him it needs connecting"
    if isinstance(exc, ToolError):
        if _SIGN_IN.search(detail):
            return f"{who} needs reconnecting ({detail}); tell him, it cannot be read until he does"
        return f"{who} did not answer ({detail}); tell him it could not be read just now"
    if isinstance(exc, sqlite3.Error):
        return f"{who} did not answer ({detail}); tell him it could not be read just now"
    return f"{who} answered something unreadable; tell him it could not be read just now"


def _fit(answer: dict[str, Any]) -> dict[str, Any]:
    """The answer within 4000 characters: links go first, from the last row."""
    rows = answer["items"]
    for row in reversed(rows):
        if len(encoded(answer)) <= _BUDGET:
            break
        row.pop("link", None)
    return answer


class _Schedule:
    """The tool's handler: a window of days, or a find, over four sources."""

    def __init__(
        self, graph: Callable[[str, Mapping[str, Any]], list[dict[str, Any]]], zone: tzinfo,
        clock: Callable[[], datetime],
    ) -> None:
        self._graph = graph
        self._zone = zone
        self._clock = clock

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
        """Dispatch on the arguments; each source that cannot be read is named in the answer."""
        if unknown := sorted(set(args) - set(_SCHEMA["properties"])):
            msg = f"my_schedule: no argument {unknown[0]!r}; use date, to, query or page"
            raise ToolError(msg, code="invalid_arguments")
        query = _text(args, "query")
        page = speakable.page_number(args.get("page"), "my_schedule", "items 9-16")
        if query and (_text(args, "date") or _text(args, "to")):
            msg = "my_schedule: query searches the next 60 days; pass it without date or to"
            raise ToolError(msg, code="invalid_arguments")
        now = self._clock()
        window = self._window(args, query, now)
        rows, read, missing = self._gather(window, ctx, now)
        if query:
            words = query.casefold().split()
            rows = [
                r for r in rows
                if all(w in f"{r.fields['what']} {r.fields.get('where', '')}".casefold()
                       for w in words)
            ]
        rows.sort(key=lambda r: r.at)
        shown = rows[(page - 1) * _PAGE : page * _PAGE]
        if page > 1 and not shown:
            end = max(1, (len(rows) - 1) // _PAGE + 1)
            summary = f"There is no page {page} of {window.label}; its items end at page {end}."
            return self._answer(summary, [], missing, read, more=False)
        more = page * _PAGE < len(rows)
        summary = self._summary(window, query, rows, page, read, missing, now)
        return _fit(self._answer(summary, [r.fields for r in shown], missing, read, more=more))

    @staticmethod
    def _answer(
        summary: str, items: list[dict[str, Any]], missing: Mapping[str, str], read: list[str],
        *, more: bool,
    ) -> dict[str, Any]:
        answer: dict[str, Any] = {"summary": summary, "items": items, "more": more, "read": read}
        if missing:
            answer["missing"] = [f"{_GONE_AS[k]}: {v}" for k, v in missing.items()]
        return answer

    def _window(self, args: Mapping[str, Any], query: str, now: datetime) -> _Window:
        today = now.astimezone(self._zone).date()
        if query:
            end = today + timedelta(days=_FIND_DAYS)
            label = f'"{query}" in the next {_FIND_DAYS} days'
            return _Window(
                today, end - timedelta(days=1), _midnight(today, self._zone),
                _midnight(end, self._zone), label,
            )
        first_text, last_text = _text(args, "date"), _text(args, "to")
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
            msg = f"my_schedule: to={last_text} is before date={first_text}; to is the last day"
            raise ToolError(msg, code="invalid_arguments")
        if (last - first).days + 1 > _SPAN_DAYS:
            msg = f"my_schedule: a span is at most {_SPAN_DAYS} days; ask in parts for longer"
            raise ToolError(msg, code="invalid_arguments")
        label = _named(first, today) if first == last else (
            f"{_named(first, today)} to {_named(last, today)}"
        )
        return _Window(
            first, last, _midnight(first, self._zone),
            _midnight(last + timedelta(days=1), self._zone), label,
        )

    def _gather(
        self, window: _Window, ctx: ToolContext, now: datetime,
    ) -> tuple[list[_Row], list[str], dict[str, str]]:
        """The rows of every source, the sources read and why the others were not.

        The two Microsoft reads run together on workers; the event log's connection belongs to
        this thread, so the reminders are read here.
        """
        rows: list[_Row] = []
        read: list[str] = []
        missing: dict[str, str] = {}
        with ThreadPoolExecutor(2, thread_name_prefix="schedule") as pool:
            started = {
                "calendar": pool.submit(self._events, window, now),
                "to-do": pool.submit(self._todos, window, now),
            }
            try:
                stored = self._stored(window, ctx, now)
            except sqlite3.Error as exc:
                stored, missing["reminder"] = [], _trouble(exc, "the event log")
        rows += stored
        for name, future in started.items():
            try:
                rows += future.result()
            except (LookupError, ToolError, ValueError, TypeError, AttributeError) as exc:
                missing[name] = _trouble(exc, "Microsoft")
            else:
                read.append(_READ_AS[name])
        if "reminder" not in missing:
            read.append(_READ_AS["reminder"])
        return rows, read, missing

    def _events(self, window: _Window, now: datetime) -> list[_Row]:
        today = now.astimezone(self._zone).date()
        rows = []
        for one in self._graph("get-calendar-view", _view(window.start, window.end)):
            if one.get("isCancelled"):
                continue
            what = speakable.cut(str(one.get("subject") or ""), _WHAT_CHARS) or "(no title)"
            if one.get("isAllDay"):
                first = date.fromisoformat(str(one["start"]["dateTime"])[:10])
                last = date.fromisoformat(str(one["end"]["dateTime"])[:10]) - timedelta(days=1)
                if last < window.first or first > window.last:
                    continue  # Graph's overlap is in instants; an all-day event is by date
                span = speakable.day_name(first, today)
                if last > first:
                    span += f" to {speakable.day_name(last, today)}"
                at = _midnight(first, self._zone)
                over = _midnight(last + timedelta(days=1), self._zone)
                when = f"{span}, all day"
            else:
                start, end = _graph_time(one["start"]), _graph_time(one["end"])
                at, over = start, max(start, end)
                when = self._timed(start, over, now)
            place = str((one.get("location") or {}).get("displayName") or "")
            fields: dict[str, Any] = {"when": when, "what": what}
            online = bool(one.get("isOnlineMeeting"))
            if place := speakable.cut(place, _WHERE_CHARS) or ("online" if online else ""):
                fields["where"] = place
            link = str(one.get("webLink") or "")
            if online and 0 < len(link) <= _LINK_CHARS:
                fields["link"] = link
            rows.append(_Row(at, over, {**fields, "source": "calendar"}))
        return rows

    def _timed(self, start: datetime, end: datetime, now: datetime) -> str:
        a, b = start.astimezone(self._zone), end.astimezone(self._zone)
        today = now.astimezone(self._zone).date()
        if b.date() == a.date():
            span = speakable.clock(a) if end == start else _clock_span(a, b)
        else:
            until = f"{speakable.day_name(b.date(), today)} {speakable.clock(b)}"
            span = f"{speakable.clock(a)} to {until}"
        if end <= now:
            state = ", done"
        elif start <= now:
            state = ", now"
        else:
            state = _ahead(start, now)
        return f"{speakable.day_name(a.date(), today)} {span}{state}"

    def _todos(self, window: _Window, now: datetime) -> list[_Row]:
        today = now.astimezone(self._zone).date()
        rows = []
        for one in self._graph("list-todo-task-lists", {"fetchAllPages": True}):
            tasks = {"todoTaskListId": one["id"], "fetchAllPages": True}
            for task in self._graph("list-todo-tasks", tasks):
                if task.get("status") == "completed" or not task.get("dueDateTime"):
                    continue
                due = date.fromisoformat(str(task["dueDateTime"]["dateTime"])[:10])
                if window.first <= due <= window.last:
                    at = datetime.combine(due, time(23, 59), self._zone)
                    what = speakable.cut(str(task.get("title") or ""), _WHAT_CHARS) or "(no title)"
                    when = f"due {speakable.day_name(due, today)}"
                    rows.append(_Row(at, at, {"when": when, "what": what, "source": "to-do"}))
        return rows

    def _stored(self, window: _Window, ctx: ToolContext, now: datetime) -> list[_Row]:
        """The waiting reminders, the pinned bus's trips and the appointments in the window."""
        today = now.astimezone(self._zone).date()
        pin = departures.active(ctx.conn, int(now.timestamp() * 1000))
        rows = []
        for one in reminders.pending(ctx.conn):
            due = datetime.fromtimestamp(one.due_at_ms / 1000, UTC)
            if pin is not None and one.reminder_id in pin.rings:
                continue  # the pinned bus's ring is the bus, listed once below
            if one.reminder_id.startswith(commitments.REMINDER_PREFIX):
                continue  # the appointment's ring: the appointment stands for it
            if window.start <= due < window.end:
                day = speakable.day_name(due.astimezone(self._zone).date(), today)
                clock = speakable.clock(due.astimezone(self._zone))
                rows.append(_Row(due, due, {
                    "when": f"{day} {clock}{_ahead(due, now)}",
                    "what": speakable.cut(one.text, _WHAT_CHARS), "source": "reminder",
                    "id": one.reminder_id,
                }))
        for trip in () if pin is None else pin.catchable(int(now.timestamp() * 1000)):
            leave = datetime.fromtimestamp(trip.leave_at_ms / 1000, UTC)
            if window.start <= leave < window.end:
                day = speakable.day_name(leave.astimezone(self._zone).date(), today)
                clock = speakable.clock(leave.astimezone(self._zone))
                rows.append(_Row(leave, leave, {
                    "when": f"leave {day} {clock}{_ahead(leave, now)}",
                    "what": speakable.cut(
                        f"bus {trip.route} from {trip.board_stop} to {trip.to}", _WHAT_CHARS,
                    ),
                    "source": "bus",
                }))
        found = commitments.occurrences(
            ctx.conn, int(window.start.timestamp() * 1000), int(window.end.timestamp() * 1000),
            zone=self._zone,
        )
        rows.extend(self._appointment(occ, now) for occ in found)
        return rows

    def _appointment(self, occ: commitments.Occurrence, now: datetime) -> _Row:
        one = occ.commitment
        start = datetime.fromtimestamp(occ.start_ms / 1000, UTC)
        end = datetime.fromtimestamp(occ.end_ms / 1000, UTC)
        what = speakable.cut(one.title, _WHAT_CHARS - len(_UNCONFIRMED))
        today = now.astimezone(self._zone).date()
        day = start.astimezone(self._zone).date()
        when = (
            f"due {speakable.day_name(day, today)}, all day" if one.all_day
            else self._timed(start, end, now)
        )
        fields: dict[str, Any] = {
            "when": when,
            "what": what if one.confirmed else what + _UNCONFIRMED,
        }
        if place := speakable.cut(one.place, _WHERE_CHARS):
            fields["where"] = place
        if one.link and len(one.link) <= _LINK_CHARS:
            fields["link"] = one.link
        fields |= {"source": one.source, "confirmed": one.confirmed, "id": one.commitment_id}
        if one.private:
            fields["private"] = True
        return _Row(start, end, fields)

    def _summary(  # noqa: PLR0913 - the request, what was found, what was read and not, the clock
        self, window: _Window, query: str, rows: list[_Row], page: int, read: list[str],
        missing: Mapping[str, str], now: datetime,
    ) -> str:
        gone = self._gone(missing)
        if not rows:
            if not read:
                return f"Could not read anything for {window.label}.{gone}"
            scope = read[0] if len(read) == 1 else f"{', '.join(read[:-1])} or {read[-1]}"
            where = f"matches {window.label}" if query else f"for {window.label}"
            return f"Nothing on your {scope} {where} (read just now).{gone}"
        counts = ", ".join(
            f"{n} {_NAMES[s][n != 1]}"
            for s in _NAMES if (n := sum(_kind(r) == s for r in rows))
        )
        if page > 1:
            first, last = (page - 1) * _PAGE + 1, min(page * _PAGE, len(rows))
            return (
                f"{window.label}: items {first}-{last} of {len(rows)} ({counts}),"
                f" read just now.{gone}"
            )
        total = f"{len(rows)} items: " if len(rows) > _PAGE else ""
        lead = ""
        if upcoming := next((r for r in rows if r.over > now), None):
            next_up = upcoming.fields
            lead = f" Next: {speakable.cut(next_up['what'], 60)}, {next_up['when']}."
        paged = f" Showing 1-{_PAGE}; page=2 for the rest." if len(rows) > _PAGE else ""
        return f"{window.label}: {total}{counts}, read just now.{lead}{paged}{gone}"

    @staticmethod
    def _gone(missing: Mapping[str, str]) -> str:
        """The sources not read, grouped by why: a sentence for the end of the summary."""
        by_reason: dict[str, list[str]] = {}
        for source, reason in missing.items():
            by_reason.setdefault(reason, []).append(_GONE_AS[source])
        return "".join(
            f" Could not read {' and '.join(names)} ({why})." for why, names in by_reason.items()
        )


def build_schedule_tool(
    graph: Callable[[str, Mapping[str, Any]], list[dict[str, Any]]],
    zone: tzinfo,
    clock: Callable[[], datetime] = speakable.now,
) -> tuple[Tool, ...]:
    """``my_schedule`` over ``Home.graph``: the calendar view, the To Do lists, the event log.

    Only ``get-calendar-view``, ``list-todo-task-lists`` and ``list-todo-tasks`` are ever called
    through ``graph``.
    """
    return (
        Tool(
            name="my_schedule",
            description=_DESCRIPTION,
            input_schema=_SCHEMA,
            handler=_Schedule(graph, zone, clock).run,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L0",
            read_only=True,
            supersedes={
                "mcp__microsoft__get-calendar-view": "my_schedule(date=..., to=...)",
                "mcp__microsoft__list-todo-task-lists": "my_schedule()",
                "mcp__microsoft__list-todo-tasks": "my_schedule(date=..., to=...)",
            },
        ),
    )


def upcoming_events(
    graph: Callable[[str, Mapping[str, Any]], list[dict[str, Any]]],
    zone: tzinfo,
    now: datetime,
    limit: int,
) -> list[tuple[datetime, str]]:
    """The next ``limit`` timed events from ``now`` to the end of tomorrow: ``(start, title)``.

    The calendar read ``my_schedule`` makes (ADR 0248), for the state block's calendar line
    (ADR 0249). An event that has begun, an all-day one and a cancelled one are not "next".

    Raises:
        LookupError: Microsoft is not connected.
        ToolError: the server did not answer.
        ValueError, TypeError, AttributeError: the answer is not Graph's JSON.
    """
    local = now.astimezone(zone)
    view = _view(local.replace(microsecond=0), _midnight(local.date() + timedelta(days=2), zone))
    found = sorted(
        (_graph_time(one["start"]), speakable.cut(str(one.get("subject") or ""), _WHAT_CHARS))
        for one in graph("get-calendar-view", view)
        if not one.get("isCancelled") and not one.get("isAllDay")
    )
    return [(start, title or "(no title)") for start, title in found if start > now][:limit]


__all__ = ["build_schedule_tool", "upcoming_events"]
