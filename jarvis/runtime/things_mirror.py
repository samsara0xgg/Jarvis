"""Outlook and To Do mirrored into the ledger of his things (ADR 0299).

When the ``microsoft`` connection answers, at start and every ``PASS_S``: the calendar from
yesterday to 60 days ahead and every To Do list's tasks (open and completed) are read, and the
ledger is made to agree. Each event or task is one thing found by its own id (``things.by_source``,
``source`` ``outlook`` or ``todo``), written ``by="mirror"`` and confirmed; an event is a ``go``
(an all-day one on its ``on_day`` through ``last_day``), a task a ``do`` due on its date. A new
one is added, a changed time is a move, a changed subject or place a change, an event deleted or
cancelled within the window is cancelled, a task completed is done, and a completed task opened
again reopens its thing.
Only what the mirror closed is reopened by it: a thing closed by Allen or by her stays closed.

An event whose id is some thing's ``outlook_id`` is Jarvis's own (an interview's, ADR 0186) and is
not read in. A mirrored thing has no rings, by default: the plan may give it a leave-by ring
(ADR 0383). Nothing is written when the connection is absent or a read fails: one line is logged
for each kind of failure.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution.tools import ToolError
from jarvis.runtime.daily_report import PLAN_SERVER, _graph
from jarvis.runtime.home import _ID_SEP
from jarvis.shared import lang
from jarvis.state import things
from jarvis.state.event_log import open_runtime_event_log

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping, Sequence
    from datetime import tzinfo
    from pathlib import Path

    from jarvis.runtime.plugin_connections import PluginConnections

LOGGER = logging.getLogger(__name__)

PASS_S: Final[float] = 15 * 60
_BACK: Final[timedelta] = timedelta(days=1)
_AHEAD: Final[timedelta] = timedelta(days=60)
_EVENT_FIELDS: Final[str] = "id,subject,start,end,isAllDay,isCancelled,location"
_LOG_WAIT_S: Final[float] = 5.0

# A thing's ``(source, source_ref)``: where it is mirrored from.
type _Key = tuple[str, str]


class ThingsMirror:
    """The read of Outlook and To Do into the ledger, and the desktop checkbox's write."""

    def __init__(self, connections: PluginConnections, event_log: Path, zone: tzinfo) -> None:
        """``event_log`` is the daemon's log; ``zone`` is his, where all-day and local times sit."""
        self._connections, self._event_log, self._zone = connections, event_log, zone
        self.now: Callable[[], datetime] = lambda: datetime.now(UTC)
        self._warned: set[str] = set()

    async def run(self) -> None:
        """Sync at once and every ``PASS_S``; a fault is logged, never ends the loop."""
        LOGGER.info("things mirror started (every %.0f s)", PASS_S)
        try:
            while True:
                try:
                    await asyncio.to_thread(self.sync)
                except Exception:
                    LOGGER.exception("things mirror: the pass failed; trying again next time")
                await asyncio.sleep(PASS_S)
        except asyncio.CancelledError:
            LOGGER.info("things mirror cancelled")
            raise

    def sync(self) -> int:
        """One pass on the calling thread; returns how many writes it made to the ledger.

        Everything is read before anything is written, so a connection that is absent or fails
        part way leaves the ledger as it was.
        """
        try:
            servers = self._connections.client_for(PLAN_SERVER)
        except ToolError:
            self._warn_once("not_connected", "the microsoft server is not connected")
            return 0
        today = self.now().astimezone(self._zone).date()
        first = datetime.combine(today - _BACK, clock(), self._zone)
        last = datetime.combine(today, clock(), self._zone) + _AHEAD
        view = {
            "startDateTime": first.isoformat(), "endDateTime": last.isoformat(),
            "select": _EVENT_FIELDS, "fetchAllPages": True,
        }
        try:
            events = _graph(servers, "get-calendar-view", view)
            tasks = [
                (str(one["id"]), task)
                for one in _graph(servers, "list-todo-task-lists", {"fetchAllPages": True})
                # No $select here: Graph answers 400 (RequestBroker--ParseUri) for To Do tasks.
                for task in _graph(
                    servers, "list-todo-tasks",
                    {"todoTaskListId": one["id"], "fetchAllPages": True},
                )
            ]
        except Exception as exc:  # noqa: BLE001 - whatever the read raised, the ledger is left be
            kind = type(exc).__name__
            self._warn_once(f"read_{kind}", f"Outlook or To Do could not be read ({kind}: {exc})")
            return 0
        with contextlib.closing(self._log()) as conn:
            return self._agree(conn, events, tasks, first, last)

    def tick(self, todo_id: str, *, done: bool) -> None:
        """The desktop checkbox: close (done) or reopen the thing mirrored for this task.

        A fault is logged and the route is not failed: To Do holds the click, and the next pass
        makes the ledger agree with it.
        """
        try:
            with contextlib.closing(self._log()) as conn:
                one = things.by_source(conn, "todo", todo_id)
                if one is None:
                    return
                write = self._write("ticked on the desktop", todo_id, "him") | {"actor": "user"}
                if done and one.state == "open":
                    things.close(conn, one.thing_id, "done", **write)
                elif not done and one.state != "open":
                    things.reopen(conn, one.thing_id, **write)
        except Exception:
            LOGGER.exception("things mirror: the ledger did not follow the tick on %s", todo_id)

    # --- the pass ------------------------------------------------------------------------

    def _agree(
        self,
        conn: sqlite3.Connection,
        events: Sequence[Mapping[str, Any]],
        tasks: Sequence[tuple[str, Mapping[str, Any]]],
        first: datetime,
        last: datetime,
    ) -> int:
        """Write what makes the ledger say the events and tasks read; returns the writes."""
        held: dict[_Key, things.Thing] = {}
        owned: set[str] = set()
        for one in things.fold(conn).values():  # the newest of a key wins, as by_source finds it
            held[(one.source, one.source_ref)] = one
            if one.outlook_id:
                owned.add(one.outlook_id)
        wants, seen = self._wants(events, tasks)
        writes = 0
        for want, over in wants:
            try:
                writes += self._follow(conn, held, want, over, owned)
            except ValueError:
                LOGGER.warning("things mirror: cannot follow %r", want.title, exc_info=True)
        for key, one in held.items():  # what was read before and is not now
            if one.state == "open" and key not in seen and self._gone(one, first, last):
                why = "gone from Outlook" if one.source == "outlook" else "gone from To Do"
                things.close(
                    conn, one.thing_id, "cancelled", why=why,
                    **self._write(one.title, one.source_ref, "mirror"),
                )
                writes += 1
        if writes:
            LOGGER.info("things: %d writes mirrored from Outlook and To Do", writes)
        return writes

    def _wants(
        self, events: Sequence[Mapping[str, Any]], tasks: Sequence[tuple[str, Mapping[str, Any]]],
    ) -> tuple[list[tuple[things.Thing, bool]], set[_Key]]:
        """What the events and tasks say as things, each with whether it is over; and the keys read.

        A cancelled event is not among the keys: it is gone, like a deleted one. One that cannot be
        read is among them and not among the things, so it is left as the ledger has it.
        """
        wants: list[tuple[things.Thing, bool]] = []
        seen: set[_Key] = set()
        for event in events:
            if event.get("isCancelled") or not event.get("id"):
                continue
            ref = str(event["id"])
            seen.add(("outlook", ref))
            try:
                wants.append((self._event(event), False))
            except (KeyError, TypeError, ValueError):
                LOGGER.warning("things mirror: cannot read event %s", ref, exc_info=True)
        for list_id, task in tasks:
            if not task.get("id"):
                continue
            ref = f"{list_id}{_ID_SEP}{task['id']}"
            seen.add(("todo", ref))
            try:
                wants.append((_task(task, ref), task.get("status") == "completed"))
            except (KeyError, TypeError, ValueError):
                LOGGER.warning("things mirror: cannot read task %s", ref, exc_info=True)
        return wants, seen

    def _event(self, event: Mapping[str, Any]) -> things.Thing:
        """An Outlook event as the ``go`` thing it is; an all-day one has a day, not an hour."""
        title = _line(event.get("subject")) or lang.t("thing.untitled")
        place = _line((event.get("location") or {}).get("displayName"))
        ref = str(event["id"])
        if event.get("isAllDay"):
            # Graph ends an all-day event at the midnight after its last day
            first = date.fromisoformat(str(event["start"]["dateTime"])[:10])
            last = date.fromisoformat(str(event["end"]["dateTime"])[:10]) - timedelta(days=1)
            return things.Thing(
                things.new_id(), title, "go", on_day=first.isoformat(),
                last_day=last.isoformat() if last > first else "", place=place,
                rings_from="default", source="outlook", source_ref=ref,
            )
        return things.Thing(
            things.new_id(), title, "go", start_at=self._local(event["start"]),
            end_at=self._local(event["end"]), place=place, rings_from="default",
            source="outlook", source_ref=ref,
        )

    def _local(self, stamp: Mapping[str, Any]) -> str:
        """Graph's ``{dateTime, timeZone}``, which calendarView answers in UTC, in his zone."""
        at = datetime.fromisoformat(str(stamp["dateTime"])).replace(tzinfo=UTC)
        return at.astimezone(self._zone).isoformat(timespec="seconds")

    def _follow(
        self,
        conn: sqlite3.Connection,
        held: dict[_Key, things.Thing],
        want: things.Thing,
        over: bool,  # noqa: FBT001 - a task completed
        owned: set[str],
    ) -> int:
        """Make the ledger's thing say ``want``, which Outlook or To Do says now; its writes."""
        key = (want.source, want.source_ref)
        one = held.get(key)
        write = self._write(want.title, want.source_ref, "mirror")
        own = want.source == "outlook" and want.source_ref in owned  # Jarvis's own event
        if one is None:
            if over or own:
                return 0  # done before it was seen, or the event of a thing already written
            added, made = things.add(conn, want, **write)
            if made:
                held[key] = added
            return int(made)
        if one.state == "open" and (over or own):
            if over:
                held[key] = things.close(conn, one.thing_id, "done", **write)
            else:  # read in before the thing that owns it was written
                why = "Jarvis's own event for another thing"
                held[key] = things.close(conn, one.thing_id, "cancelled", why=why, **write)
            return 1
        writes = 0
        if one.state != "open":
            if over or own or one.history[-1].by != "mirror":
                return 0  # still done, or closed by someone who is not the mirror
            one = things.reopen(conn, one.thing_id, **write)
            writes += 1
        # a time taken away in Outlook or To Do cannot be followed: a move needs a new time
        if (want.start_at or want.on_day or want.due) and _when(one) != _when(want):
            one = things.move(
                conn, one.thing_id, start_at=want.start_at, end_at=want.end_at,
                on_day=want.on_day, last_day=want.last_day, due=want.due, **write,
            )
            writes += 1
        names = ("title", "place") if want.source == "outlook" else ("title",)
        fields = {
            name: getattr(want, name) for name in names if getattr(want, name) != getattr(one, name)
        }
        if fields:
            one = things.change(conn, one.thing_id, fields, **write)
            writes += 1
        held[key] = one
        return writes

    def _gone(self, one: things.Thing, first: datetime, last: datetime) -> bool:
        """Whether a thing not read should have been: a task always, an event within the window."""
        if one.source == "todo":
            return True
        if one.source != "outlook":
            return False
        if one.start_at:
            begin = datetime.fromisoformat(one.start_at)
        elif one.on_day:
            begin = datetime.combine(date.fromisoformat(one.on_day), clock(), self._zone)
        else:
            return False
        return first <= begin < last

    def _write(self, evidence: str, ref: str, by: str) -> dict[str, Any]:
        """The arguments every write to the ledger carries."""
        return {"by": by, "evidence": evidence, "ref": ref, "zone": self._zone, "now": self.now()}

    # --- plumbing ------------------------------------------------------------------------

    def _warn_once(self, kind: str, what: str) -> None:
        if kind not in self._warned:
            self._warned.add(kind)
            LOGGER.warning("things mirror: %s; nothing is mirrored (logged once)", what)

    def _log(self) -> sqlite3.Connection:
        return open_runtime_event_log(self._event_log, deadline=time.monotonic() + _LOG_WAIT_S)


def _line(text: object) -> str:
    """One trimmed line of at most a thing's title."""
    return " ".join(str(text or "").split())[: things.TITLE_CHARS]


def _task(task: Mapping[str, Any], ref: str) -> things.Thing:
    """A To Do task as the ``do`` thing it is, due on its date (To Do keeps a date, not an hour)."""
    due = str(task["dueDateTime"]["dateTime"])[:10] if task.get("dueDateTime") else ""
    title = _line(task.get("title")) or lang.t("thing.untitled")
    return things.Thing(things.new_id(), title, "do", due=due, source="todo", source_ref=ref)


def _when(one: things.Thing) -> tuple[datetime | None, datetime | None, str, str, str]:
    """A thing's time as instants, so two spellings of one moment are the same."""
    return (
        datetime.fromisoformat(one.start_at) if one.start_at else None,
        datetime.fromisoformat(one.end_at) if one.end_at else None,
        one.on_day,
        one.last_day,
        one.due,
    )
