"""ADR 0299: the day line draws what he goes to or does from the ledger of his things.

Data-driven over a real event log (no network, no model): things are added and changed the way
the writers do, and ``fold_day`` gives the day.

- an occurrence is an ``event`` in his lane, its id ``commitment-<hex>@<day>`` (the phone's shape,
  ``<hex>`` being the hex of the thing's id), with ``source``, ``confirmed``, ``private`` and
  ``place``; a week's repeat keeps the series date in its id, a moved date too;
- an all-day one is a label of the day, and a thing to do by a date or an hour is neither;
- a done thing stays on the line, a cancelled one (or a cancelled date of a week) is gone;
- a thing's rings are not drawn as reminders: the thing stands for them, his own reminder stays.
"""

from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from jarvis.state import reminders, things
from jarvis.state.day_line import DaySources, day_window, fold_day
from jarvis.state.event_log import emit_event, open_event_log

if TYPE_CHECKING:
    from pathlib import Path

ZONE = ZoneInfo("America/Vancouver")
DAY = date(2026, 10, 16)  # a Friday
NOW = datetime(2026, 10, 16, 9, 0, tzinfo=ZONE)


def _on(days: int, clock: str) -> str:
    moment = datetime.fromisoformat(f"{DAY.isoformat()}T{clock}:00").replace(tzinfo=ZONE)
    return (moment + timedelta(days=days)).isoformat()


def _ms(clock: str) -> int:
    return int(datetime.fromisoformat(_on(0, clock)).timestamp() * 1000)


def _id(thing_id: str, day: str = DAY.isoformat()) -> str:
    """The id the phone sees for ``thing-<hex>`` on ``day``."""
    return f"commitment-{thing_id.removeprefix('thing-')}@{day}"


def test_the_day_draws_each_occurrence_of_a_thing_with_the_phones_id(tmp_path: Path) -> None:
    """Events, a label, a week's repeat and its moved date, a done one; not what is cancelled."""
    stamp: dict[str, Any] = {"by": "her", "evidence": "said so", "zone": ZONE, "now": NOW}
    now_ms = int(NOW.timestamp() * 1000)
    with closing(open_event_log(tmp_path / "events.db")) as conn:
        def add(title: str, kind: str = "go", **fields: Any) -> str:  # noqa: ANN401
            one = things.Thing(things.new_id(), title, kind, **fields)
            return things.add(conn, one, **stamp)[0].thing_id

        dentist = add(
            "Dentist", start_at=_on(0, "14:00"), end_at=_on(0, "15:30"), place="Main St",
            source="mail", confirmed=False, rings=("-30m",),
        )
        holiday = add("Holiday", on_day=DAY.isoformat())
        pottery = add("Pottery", start_at=_on(-14, "19:00"), weekly_until="2026-11-20")
        klass = add("Class", start_at=_on(-14, "10:00"), weekly_until="2026-11-20")
        gym = add("Gym", start_at=_on(-14, "07:00"), weekly_until="2026-11-20")
        standup = add("Stand-up", start_at=_on(0, "08:00"), private=True)
        form = add("Send form", "do", start_at=_on(0, "16:00"))
        cancelled = add("Cancelled", start_at=_on(0, "10:30"))
        add("Pay rent", "do", due=DAY.isoformat())
        add("Tomorrow", start_at=_on(1, "10:00"))
        # Next week's class comes to 11:00 today; this week's gym is off; the stand-up is over.
        things.move(conn, klass, occurrence=_on(7, "10:00")[:10], start_at=_on(0, "11:00"), **stamp)
        things.close(conn, gym, "cancelled", occurrence=DAY.isoformat(), **stamp)
        things.close(conn, standup, "done", **stamp)
        things.close(conn, cancelled, "cancelled", **stamp)
        # His own reminder, besides the dentist's ring (13:30) that the ledger armed.
        emit_event(
            conn, type="reminder.scheduled", ts_epoch_ms=now_ms,
            payload={
                "reminder_id": "reminder-own", "due_at_epoch_ms": now_ms + 3_600_000,
                "due_at_local": "-", "text": "stretch", "action_id": "a1",
            },
        )
        assert any(
            r.reminder_id.startswith(things.RING_PREFIX) for r in reminders.fold(conn).values()
        )
        first, last = day_window(DAY, ZONE)
        line = fold_day(conn, first, last, now_ms, DaySources(ZONE))
    assert line.missing == ["mac", "talks"]  # the stores this test has none of
    rows = {i["id"]: i for i in line.items}
    moved = _id(klass, "2026-10-23")  # it keeps the date of the series it belongs to
    assert [i["id"] for i in line.items] == [
        _id(standup), _id(klass), "reminder-own", moved, _id(dentist), _id(form), _id(pottery),
    ]
    assert rows[_id(dentist)] == {
        "id": _id(dentist), "lane": "you", "kind": "event", "state": "next", "title": "Dentist",
        "start_ms": _ms("14:00"), "end_ms": _ms("15:30"), "source": "mail", "confirmed": False,
        "private": False, "place": "Main St",
    }
    assert (rows[moved]["start_ms"], rows[moved]["end_ms"]) == (_ms("11:00"), _ms("12:00"))
    assert rows[_id(standup)]["state"] == "done"  # closed, and still where it was
    assert (rows[_id(standup)]["private"], rows[_id(form)]["title"]) == (True, "Send form")
    assert rows["reminder-own"]["text"] == "stretch"  # the dentist's ring is not a second item
    assert line.labels == [{
        "title": "Holiday", "source": "said", "confirmed": True, "private": False, "place": "",
        "id": _id(holiday),
    }]
    assert _id(pottery) in rows
