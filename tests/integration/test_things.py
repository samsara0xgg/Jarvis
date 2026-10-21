"""ADR 0299: everything he has to go to or do is one ledger in the event log.

Data-driven checks over the real code and a real event log and memory.db (no key, no network, no
provider). The examples are invented.

- the fold and every write's effect, and every refusal;
- ``add`` finding the thing it already holds;
- the reads: ``find``, ``by_source``, ``open_things``, ``due_ms``;
- ``occurrences``: weekly, skipped, moved and closed dates, whole days, a clock change;
- the rings: each grammar, idempotent sync, cancelled with the thing, private words, no late ring;
- the carry-in of memos, reminders, commitments and core memory's to-dos, once, in one transaction;
- core memory with ten sections.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest
import yaml

from jarvis.decision.core_memory import check_core_memory
from jarvis.deployment import bootstrap_runtime
from jarvis.runtime import things_ledger as ledger_module
from jarvis.runtime.things_ledger import ThingsLedger
from jarvis.shared import lang
from jarvis.state import commitments, core_memory, memory_page, things
from jarvis.state import reminders as reminder_state
from jarvis.state.event_log import emit_event, iter_events, iter_events_of_types, open_event_log
from jarvis.state.memory_db import (
    current_core_memory,
    drop_due_section,
    due_items,
    open_memory_db,
    remember_fact,
    render_context,
)
from tests.support.runtime_config import boot_runtime
from tests.support.runtime_log import RuntimeLog

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

ZONE = ZoneInfo("America/Vancouver")  # UTC-8 until the clock change of Mar 8 2026, then UTC-7
NOW = datetime(2026, 10, 14, 9, 0, tzinfo=ZONE)  # a Wednesday
WRITE: dict[str, Any] = {"by": "her", "evidence": "he said so", "zone": ZONE, "now": NOW}
THU_2PM = "2026-10-15T14:00:00-07:00"
TUE_7PM = "2026-10-20T19:00:00-07:00"
WEEKLY: dict[str, Any] = {"start_at": TUE_7PM, "weekly_until": "2026-11-10"}


def _ms(moment: datetime) -> int:
    return int(moment.timestamp()) * 1000 + moment.microsecond // 1000


def _at(month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=ZONE)


def _o(
    day: str, start: datetime, end: datetime, state: str = "open", *, all_day: bool = False,
) -> tuple[Any, ...]:
    """What ``occurrences`` should give for one occurrence."""
    return (day, _ms(start), _ms(end), all_day, state)


class _Log(RuntimeLog):
    def actors(self, event_type: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT actor FROM events WHERE type = ? ORDER BY id", (event_type,),
        )
        return [str(actor) for (actor,) in rows]

    def events(self, *types: str) -> list[Any]:
        found = iter_events_of_types(self.conn, types) if types else iter_events(self.conn)
        return list(found)


@pytest.fixture
def log(tmp_path: Path) -> Iterator[_Log]:
    """A runtime root with a real event log and memory.db; the language is English."""
    before = lang.language()
    lang.set_language("en")
    one = _Log(tmp_path)
    yield one
    one.conn.close()
    lang.set_language(before)


def _thing(title: str = "Dentist", kind: str = "go", **fields: Any) -> things.Thing:  # noqa: ANN401
    return things.Thing(fields.pop("thing_id", things.new_id()), title, kind, **fields)


def _put(
    log: _Log, title: str = "Dentist", kind: str = "go", *, now: datetime = NOW,
    **fields: Any,  # noqa: ANN401 - the thing's fields
) -> things.Thing:
    made, created = things.add(log.conn, _thing(title, kind, **fields), **{**WRITE, "now": now})
    assert created
    return made


def _do(log: _Log, one: things.Thing, name: str, **kwargs: Any) -> Any:  # noqa: ANN401
    """Call the write ``name`` on ``one`` with the usual who, why and clock."""
    return getattr(things, name)(log.conn, one.thing_id, **{**WRITE, **kwargs})


def _rings(log: _Log) -> dict[str, reminder_state.Reminder]:
    """The pending rings the ledger armed, by reminder id."""
    return {
        r.reminder_id: r
        for r in reminder_state.pending(log.conn)
        if r.reminder_id.startswith(things.RING_PREFIX)
    }


def _ring_id(one: things.Thing, start: str, k: int = 0) -> str:
    anchor = _ms(datetime.fromisoformat(start))
    return f"{things.RING_PREFIX}{one.thing_id.removeprefix('thing-')[:12]}-{anchor}-{k}"


# --- the fold and every write ----------------------------------------------------------------

_BASE: dict[str, Any] = {"start_at": THU_2PM}
_SCENARIOS: dict[str, tuple[dict[str, Any], list[tuple[str, dict[str, Any]]], dict[str, Any]]] = {
    "done": (
        _BASE, [("close", {"state": "done"})],
        {"state": "done", "closed_ms": _ms(NOW), "whats": ["added", "done"]},
    ),
    "paid": (
        _BASE, [("close", {"state": "paid"})],
        {"state": "paid", "closed_ms": _ms(NOW), "whats": ["added", "paid"]},
    ),
    "cancelled with its reason": (
        _BASE, [("close", {"state": "cancelled", "why": "the clinic phoned it off"})],
        {
            "state": "cancelled", "whats": ["added", "cancelled"],
            "last_why": "the clinic phoned it off",
        },
    ),
    "reopened": (
        _BASE, [("close", {"state": "done"}), ("reopen", {})],
        {"state": "open", "closed_ms": None, "whats": ["added", "done", "reopened"]},
    ),
    "moved keeps the time it had": (
        _BASE,
        [("move", {
            "start_at": "2026-10-16T14:00:00-07:00", "end_at": "2026-10-16T15:00:00-07:00",
        })],
        {
            "start_at": "2026-10-16T14:00:00-07:00", "end_at": "2026-10-16T15:00:00-07:00",
            "moves": [(THU_2PM, "", "", "")], "whats": ["added", "moved"],
        },
    ),
    "moved twice, oldest first": (
        _BASE,
        [("move", {"start_at": "2026-10-16T14:00:00-07:00"}),
         ("move", {"start_at": "2026-10-17T09:00:00-07:00"})],
        {
            "start_at": "2026-10-17T09:00:00-07:00",
            "moves": [(THU_2PM, "", "", ""), ("2026-10-16T14:00:00-07:00", "", "", "")],
        },
    ),
    "moved from an hour to a whole day": (
        _BASE, [("move", {"on_day": "2026-10-16"})],
        {"start_at": "", "on_day": "2026-10-16", "moves": [(THU_2PM, "", "", "")]},
    ),
    "an untimed thing is moved to a due date": (
        {}, [("move", {"due": "2026-10-30"})],
        {"due": "2026-10-30", "moves": [("", "", "", "")]},
    ),
    "changed": (
        _BASE,
        [("change", {"fields": {
            "title": "  Dentist   (Dr Lee) ", "place": " Clinic ", "private": True,
            "life_line": "health", "rings": ["-30m"], "note": "bring the card",
            "sender": "clinic.example", "outlook_id": "AAMk123", "link": "https://x.example/a",
        }})],
        {
            "title": "Dentist (Dr Lee)", "place": "Clinic", "private": True,
            "life_line": "health", "rings": ("-30m",), "note": "bring the card",
            "sender": "clinic.example", "outlook_id": "AAMk123", "link": "https://x.example/a",
            "whats": ["added", "changed"],
        },
    ),
    "a kind and the weekly fields change": (
        WEEKLY,
        [("change", {"fields": {
            "kind": "do", "skip_dates": ["2026-10-27"], "weekly_until": "2026-11-03",
        }})],
        {"kind": "do", "skip_dates": ("2026-10-27",), "weekly_until": "2026-11-03"},
    ),
    "confirmed": (
        {**_BASE, "confirmed": False, "source": "mail"}, [("confirm", {})],
        {"confirmed": True, "whats": ["added", "changed"]},
    ),
    "one date of a weekly thing is done": (
        WEEKLY, [("close", {"state": "done", "occurrence": "2026-10-27"})],
        {"state": "open", "date_states": {"2026-10-27": "done"}, "whats": ["added", "done"]},
    ),
    "one date is paid, another cancelled": (
        WEEKLY,
        [("close", {"state": "paid", "occurrence": "2026-10-27"}),
         ("close", {"state": "cancelled", "occurrence": "2026-11-03", "why": "away"})],
        {"date_states": {"2026-10-27": "paid", "2026-11-03": "cancelled"}},
    ),
    "a closed date is reopened": (
        WEEKLY,
        [("close", {"state": "done", "occurrence": "2026-10-27"}),
         ("reopen", {"occurrence": "2026-10-27"})],
        {"date_states": {}, "whats": ["added", "done", "reopened"]},
    ),
    "one date of a weekly thing is moved": (
        WEEKLY,
        [("move", {"occurrence": "2026-10-27", "start_at": "2026-10-28T10:00:00-07:00"})],
        {
            "start_at": TUE_7PM, "date_moves": {"2026-10-27": ("2026-10-28T10:00:00-07:00", "")},
            "moves": [("2026-10-27T19:00:00-07:00", "2026-10-27T20:00:00-07:00", "", "")],
            "move_dates": ["2026-10-27"],
        },
    ),
    "the weekly series is cancelled as a whole": (
        WEEKLY, [("close", {"state": "cancelled", "why": "the class ended"})],
        {"state": "cancelled", "date_states": {}},
    ),
    "a cancelled series is reopened": (
        WEEKLY, [("close", {"state": "cancelled"}), ("reopen", {})],
        {"state": "open"},
    ),
}


@pytest.mark.parametrize("name", list(_SCENARIOS))
def test_each_write_changes_the_fold_as_it_says(log: _Log, name: str) -> None:
    """Every write's effect on the folded thing, and the history it leaves."""
    base, steps, expected = _SCENARIOS[name]
    one = _put(log, **base)
    for step, kwargs in steps:
        _do(log, one, step, **kwargs)
    folded = things.get(log.conn, one.thing_id)
    for attribute, want in expected.items():
        match attribute:
            case "whats":
                got: Any = [s.what for s in folded.history]
            case "last_why":
                got = folded.history[-1].why
            case "move_dates":
                got = [m.date for m in folded.moves]
            case "moves":
                got = [(m.start_at, m.end_at, m.on_day, m.due) for m in folded.moves]
            case _:
                got = getattr(folded, attribute)
        assert got == want, attribute
    assert folded.history[0].by == "her"
    assert folded.history[0].evidence == "he said so"


def test_a_new_thing_is_read_back_whole_with_who_wrote_it(log: _Log) -> None:
    """The fold gives back every field, the time it was added and the first history step."""
    one = _put(
        log, "Pottery", "go", start_at=TUE_7PM, end_at="2026-10-20T21:00:00-07:00",
        weekly_until="2026-11-10", skip_dates=("2026-10-27",), place="Studio 4",
        link="https://x.example/a", note="n", rings=("-60m", "-1d@20:00"), private=True,
        life_line="pottery", confirmed=False, source="mail", source_ref="m1",
        sender="studio.example", outlook_id="AAMk1",
    )
    got = things.get(log.conn, one.thing_id)
    assert got.payload() == one.payload()
    assert (got.added_ms, got.added_by, got.state, got.closed_ms) == (_ms(NOW), "her", "open", None)
    (first,) = got.history
    assert (first.at_ms, first.what, first.by, first.evidence, first.ref) == (
        _ms(NOW), "added", "her", "he said so", "",
    )
    assert things.get(log.conn, one.thing_id).moves == ()


def test_the_events_carry_who_and_why_and_a_title_is_one_line(log: _Log) -> None:
    """Every write event holds thing_id, by, evidence (cut to 200) and ref; no event is deleted."""
    one = _put(log, "  Fix\nthe   bike ", "do")
    assert one.title == "Fix the bike"
    _do(log, one, "close", state="done", evidence="y" * 500, ref="rec-1", why="z" * 500)
    added, closed = log.events("thing.added", "thing.closed")
    assert (added.payload["thing_id"], added.payload["by"], added.payload["ref"]) == (
        one.thing_id, "her", "",
    )
    assert (closed.payload["by"], closed.payload["ref"]) == ("her", "rec-1")
    assert len(closed.payload["evidence"]) == len(closed.payload["why"]) == 200
    assert {e.type for e in log.events()} >= {"thing.added", "thing.closed"}
    assert log.actors("thing.closed") == ["jarvis_runtime"]


def test_a_write_by_a_button_is_stamped_with_that_actor(log: _Log) -> None:
    """The ``actor`` argument is who the log says caused the event."""
    one = _put(log, **_BASE)
    _do(log, one, "close", state="done", by="him", actor="user")
    assert log.actors("thing.closed") == ["user"]
    assert things.get(log.conn, one.thing_id).history[-1].by == "him"


# --- refusals --------------------------------------------------------------------------------

_VALID_TIME: dict[str, Any] = {"start_at": THU_2PM}
_ADD_REFUSALS: dict[str, tuple[dict[str, Any], str]] = {
    "a time with no offset": ({"start_at": "2026-10-15T14:00:00"}, "needs a UTC offset"),
    "a start that is no time": ({"start_at": "soon"}, "not an ISO time"),
    "a start and a day": ({**_VALID_TIME, "on_day": "2026-10-15"}, "one kind of time"),
    "a start and a due": ({**_VALID_TIME, "due": "2026-10-16"}, "one kind of time"),
    "a day and a due": ({"on_day": "2026-10-15", "due": "2026-10-16"}, "one kind of time"),
    "an end with no start": (
        {"on_day": "2026-10-15", "end_at": "2026-10-15T15:00:00-07:00"}, "end_at needs start_at",
    ),
    "an end before the start": (
        {**_VALID_TIME, "end_at": "2026-10-15T13:00:00-07:00"}, "is before start_at",
    ),
    "an end with no offset": (
        {**_VALID_TIME, "end_at": "2026-10-15T15:00:00"}, "needs a UTC offset",
    ),
    "a day that is not one": ({"on_day": "2026-13-01"}, "not a date"),
    "a day with an hour": ({"on_day": "2026-10-15T00:00:00-07:00"}, "not a date"),
    "a due that is no time": ({"due": "friday"}, "not an ISO time"),
    "a due hour with no offset": ({"due": "2026-10-16T17:00:00"}, "needs a UTC offset"),
    "weekly with no time": ({"weekly_until": "2026-11-10"}, "need a start_at or an on_day"),
    "weekly on a due": (
        {"due": "2026-10-20", "weekly_until": "2026-11-10"}, "need a start_at or an on_day",
    ),
    "skip dates with no time": ({"skip_dates": ("2026-10-20",)}, "need a start_at or an on_day"),
    "a last day that is not one": ({**_VALID_TIME, "weekly_until": "soon"}, "not a date"),
    "a last day before the first": (
        {**_VALID_TIME, "weekly_until": "2026-10-01"}, "before the first day",
    ),
    "a skip date that is not one": ({**WEEKLY, "skip_dates": ("tuesday",)}, "not a date"),
    "a kind outside the set": ({"kind": "plan"}, "kind must be one of"),
    "a source outside the set": ({"source": "rumour"}, "source must be one of"),
    "no words": ({"title": "   "}, "needs its words"),
    "words over 300": ({"title": "x" * 301}, "title is 301"),
    "a note over 2000": ({"note": "x" * 2001}, "note is 2001"),
    "a life line over 40": ({"life_line": "x" * 41}, "life_line is 41"),
    "a ring with no sign": ({**_VALID_TIME, "rings": ("30m",)}, "ring '30m'"),
    "a ring in hours": ({**_VALID_TIME, "rings": ("-1h",)}, "ring '-1h'"),
    "a ring at no hour": ({**_VALID_TIME, "rings": ("@25:00",)}, "ring '@25:00'"),
    "a ring too far ahead": ({**_VALID_TIME, "rings": ("-40d@09:00",)}, "at most 31 days"),
    "rings on an untimed thing": ({"rings": ("-30m",)}, "no time has no rings"),
    "a private that is not true or false": ({"private": "yes"}, "private must be true or false"),
    "an id that is not one": ({"thing_id": "commitment-abc"}, "not thing-<id>"),
}


@pytest.mark.parametrize("name", list(_ADD_REFUSALS))
def test_add_refuses_what_could_not_be_read_back(log: _Log, name: str) -> None:
    """Each refusal says what is wrong and leaves the log as it was."""
    fields, why = _ADD_REFUSALS[name]
    before = len(log.events())
    with pytest.raises(ValueError, match=why):
        things.add(log.conn, _thing(**fields), **WRITE)
    assert len(log.events()) == before
    assert not log.conn.in_transaction


def test_add_refuses_a_who_outside_the_set_and_an_id_taken(log: _Log) -> None:
    """``by`` is one of eight; a thing id is added once."""
    with pytest.raises(ValueError, match="by must be one of"):
        things.add(log.conn, _thing(), **{**WRITE, "by": "robot"})
    one = _put(log)
    with pytest.raises(ValueError, match="already exists"):
        things.add(log.conn, _thing("Other", thing_id=one.thing_id), **WRITE)
    assert len(log.events("thing.added")) == 1


_Steps = list[tuple[str, dict[str, Any]]]
_CALL_REFUSALS: dict[str, tuple[dict[str, Any], _Steps, str, dict[str, Any], str]] = {
    "change a time": (
        _BASE, [], "change", {"fields": {"start_at": "2026-10-16T14:00:00-07:00"}}, "use move",
    ),
    "change a field outside the set": (
        _BASE, [], "change", {"fields": {"source": "mail"}}, "cannot be changed",
    ),
    "change nothing": (_BASE, [], "change", {"fields": {}}, "nothing to change"),
    "change to a ring that does not parse": (
        _BASE, [], "change", {"fields": {"rings": ["soon"]}}, "ring 'soon'",
    ),
    "change rings to a string": (
        _BASE, [], "change", {"fields": {"rings": "-30m"}}, "rings must be a list",
    ),
    "change rings of an untimed thing": (
        {}, [], "change", {"fields": {"rings": ["-30m"]}}, "no time has no rings",
    ),
    "change to no words": (_BASE, [], "change", {"fields": {"title": " "}}, "needs its words"),
    "change a flag to a word": (
        _BASE, [], "change", {"fields": {"private": "yes"}}, "private must be true or false",
    ),
    "change a last day before the first": (
        WEEKLY, [], "change", {"fields": {"weekly_until": "2026-10-01"}}, "before the first day",
    ),
    "change weekly fields of an untimed thing": (
        {}, [], "change", {"fields": {"skip_dates": ["2026-10-27"]}}, "need a start_at",
    ),
    "change a closed thing": (
        _BASE, [("close", {"state": "done"})], "change", {"fields": {"title": "x"}}, "is done",
    ),
    "move a closed thing": (
        _BASE, [("close", {"state": "cancelled"})], "move",
        {"start_at": "2026-10-16T14:00:00-07:00"}, "is cancelled",
    ),
    "move to a time with no offset": (
        _BASE, [], "move", {"start_at": "2026-10-16T14:00:00"}, "needs a UTC offset",
    ),
    "move to no time": (_BASE, [], "move", {}, "needs the new time"),
    "move to two kinds of time": (
        _BASE, [], "move", {"start_at": THU_2PM, "on_day": "2026-10-16"}, "one kind of time",
    ),
    "move to an end before the start": (
        _BASE, [], "move", {"start_at": THU_2PM, "end_at": "2026-10-15T13:00:00-07:00"},
        "is before start_at",
    ),
    "move a weekly thing to a due": (
        WEEKLY, [], "move", {"due": "2026-10-30"}, "need a start_at or an on_day",
    ),
    "move an occurrence of a thing that is not weekly": (
        _BASE, [], "move", {"occurrence": "2026-10-15", "start_at": THU_2PM}, "is not weekly",
    ),
    "move a date that is no occurrence": (
        WEEKLY, [], "move", {"occurrence": "2026-10-21", "start_at": TUE_7PM},
        "not one of the occurrences",
    ),
    "move a date past the last": (
        WEEKLY, [], "move", {"occurrence": "2026-11-17", "start_at": TUE_7PM},
        "not one of the occurrences",
    ),
    "move a skipped date": (
        {**WEEKLY, "skip_dates": ("2026-10-27",)}, [], "move",
        {"occurrence": "2026-10-27", "start_at": TUE_7PM}, "not one of the occurrences",
    ),
    "move a date that is not a date": (
        WEEKLY, [], "move", {"occurrence": "tuesday", "start_at": TUE_7PM}, "not a date",
    ),
    "move an occurrence to a day": (
        WEEKLY, [], "move", {"occurrence": "2026-10-27", "on_day": "2026-10-28"}, "new start_at",
    ),
    "move a closed date": (
        WEEKLY, [("close", {"state": "done", "occurrence": "2026-10-27"})], "move",
        {"occurrence": "2026-10-27", "start_at": TUE_7PM}, "is done",
    ),
    "close as open": (_BASE, [], "close", {"state": "open"}, "closed as one of"),
    "close as something else": (_BASE, [], "close", {"state": "finished"}, "closed as one of"),
    "close twice": (
        _BASE, [("close", {"state": "done"})], "close", {"state": "cancelled"}, "is done",
    ),
    "close a weekly thing as a whole done": (
        WEEKLY, [], "close", {"state": "done"}, "one date at a time",
    ),
    "close a weekly thing as a whole paid": (
        WEEKLY, [], "close", {"state": "paid"}, "one date at a time",
    ),
    "close a date that is no occurrence": (
        WEEKLY, [], "close", {"state": "done", "occurrence": "2026-10-28"},
        "not one of the occurrences",
    ),
    "close a date twice": (
        WEEKLY, [("close", {"state": "done", "occurrence": "2026-10-27"})], "close",
        {"state": "cancelled", "occurrence": "2026-10-27"}, "done already",
    ),
    "close a date of a thing that is not weekly": (
        _BASE, [], "close", {"state": "done", "occurrence": "2026-10-15"}, "is not weekly",
    ),
    "close a date of a cancelled series": (
        WEEKLY, [("close", {"state": "cancelled"})], "close",
        {"state": "done", "occurrence": "2026-10-27"}, "is cancelled",
    ),
    "reopen an open thing": (_BASE, [], "reopen", {}, "open already"),
    "reopen an open date": (
        WEEKLY, [], "reopen", {"occurrence": "2026-10-27"}, "open already",
    ),
    "reopen a date of a thing that is not weekly": (
        _BASE, [], "reopen", {"occurrence": "2026-10-15"}, "is not weekly",
    ),
    "confirm a closed thing": (
        {**_BASE, "confirmed": False, "source": "mail"}, [("close", {"state": "cancelled"})],
        "confirm", {}, "is cancelled",
    ),
}


@pytest.mark.parametrize("name", list(_CALL_REFUSALS))
def test_every_other_write_refuses_what_it_should(log: _Log, name: str) -> None:
    """A closed thing is only reopened; a time needs an offset; a date must be an occurrence."""
    base, steps, call, kwargs, why = _CALL_REFUSALS[name]
    one = _put(log, **base)
    for step, step_kwargs in steps:
        _do(log, one, step, **step_kwargs)
    before = (len(log.events()), things.get(log.conn, one.thing_id).payload())
    with pytest.raises(ValueError, match=why):
        _do(log, one, call, **kwargs)
    assert (len(log.events()), things.get(log.conn, one.thing_id).payload()) == before
    assert not log.conn.in_transaction


@pytest.mark.parametrize("call", ["change", "move", "close", "reopen", "confirm"])
def test_a_thing_that_does_not_exist_is_a_lookup_error(log: _Log, call: str) -> None:
    """Every write names its thing; none is made up."""
    calls: dict[str, dict[str, Any]] = {
        "change": {"fields": {"title": "x"}}, "move": {"on_day": "2026-10-16"},
        "close": {"state": "done"}, "reopen": {}, "confirm": {},
    }
    kwargs = calls[call]
    with pytest.raises(LookupError, match="no thing"):
        getattr(things, call)(log.conn, "thing-nope", **{**WRITE, **kwargs})
    with pytest.raises(LookupError):
        things.get(log.conn, "thing-nope")


@pytest.mark.parametrize("call", ["change", "move", "close", "reopen"])
def test_a_who_outside_the_set_is_refused_by_every_write(log: _Log, call: str) -> None:
    """``by`` is checked before anything is written."""
    one = _put(log, **_BASE)
    calls: dict[str, dict[str, Any]] = {
        "change": {"fields": {"title": "x"}}, "move": {"on_day": "2026-10-16"},
        "close": {"state": "done"}, "reopen": {},
    }
    kwargs = calls[call]
    with pytest.raises(ValueError, match="by must be one of"):
        _do(log, one, call, by="robot", **kwargs)


# --- add finds the thing it already holds ----------------------------------------------------


@pytest.mark.parametrize(
    ("second", "created"),
    [
        ({"title": "dentist", "start_at": THU_2PM}, False),
        ({"title": "  DENTIST  ", "start_at": THU_2PM}, False),
        ({"title": "Dentist", "start_at": "2026-10-15T21:00:00+00:00"}, False),  # the same instant
        ({"title": "Dentist", "start_at": THU_2PM, "place": "Elsewhere", "kind": "do"}, False),
        ({"title": "Dentist", "start_at": "2026-10-15T15:00:00-07:00"}, True),
        ({"title": "Dentist", "on_day": "2026-10-15"}, True),
        ({"title": "Dentist 2", "start_at": THU_2PM}, True),
    ],
)
def test_add_with_the_same_words_and_time_as_an_open_thing_writes_nothing(
    log: _Log, second: dict[str, Any], created: bool,  # noqa: FBT001
) -> None:
    """Same title (whitespace collapsed, case folded) and same time: the one already there."""
    first = _put(log, "Dentist", start_at=THU_2PM)
    got, made = things.add(log.conn, _thing(**second), **WRITE)
    assert made is created
    assert (got.thing_id == first.thing_id) is (not created)
    assert len(log.events("thing.added")) == (2 if created else 1)


def test_an_untimed_thing_and_a_dated_one_are_the_same_only_with_the_same_time(log: _Log) -> None:
    """Untimed and untimed are one thing; untimed and due are two; a closed one is not matched."""
    first = _put(log, "Buy milk", "do")
    again, made = things.add(log.conn, _thing("buy  milk", "do"), **WRITE)
    assert (again.thing_id, made) == (first.thing_id, False)
    _put(log, "Buy milk", "do", due="2026-10-30")
    assert len(log.events("thing.added")) == 2
    _do(log, first, "close", state="done")
    fresh, made = things.add(log.conn, _thing("Buy milk", "do"), **WRITE)
    assert made
    assert fresh.thing_id != first.thing_id


# --- the reads -------------------------------------------------------------------------------


def test_find_takes_the_start_of_the_hex_and_short_id_is_the_first_six(log: _Log) -> None:
    """Four characters or more, unique; with or without ``thing-``."""
    a = _put(log, "A", thing_id="thing-abcd10" + "0" * 26)
    b = _put(log, "B", thing_id="thing-abcd20" + "0" * 26)
    assert things.short_id(a) == "abcd10"
    assert things.find(log.conn, "abcd1").thing_id == a.thing_id
    assert things.find(log.conn, "thing-ABCD2").thing_id == b.thing_id
    with pytest.raises(LookupError, match="starts 2 things"):
        things.find(log.conn, "abcd")
    with pytest.raises(LookupError, match="no thing starts"):
        things.find(log.conn, "ffff")
    with pytest.raises(LookupError, match="too short"):
        things.find(log.conn, "abc")


def test_by_source_is_the_newest_thing_from_that_source_whatever_became_of_it(log: _Log) -> None:
    """Keyed by source and reference; an empty reference matches nothing."""
    old = _put(log, "Old", source="mail", source_ref="m1")
    _do(log, old, "close", state="cancelled")
    new = _put(log, "New", source="mail", source_ref="m1", start_at=THU_2PM)
    _put(log, "Other", source="outlook", source_ref="m1")
    assert things.by_source(log.conn, "mail", "m1") == things.get(log.conn, new.thing_id)
    assert things.by_source(log.conn, "outlook", "m1") is not None
    assert things.by_source(log.conn, "mail", "m2") is None
    assert things.by_source(log.conn, "todo", "m1") is None
    assert things.by_source(log.conn, "mail", "") is None


def test_open_things_are_timed_soonest_first_then_untimed_oldest_first(log: _Log) -> None:
    """Closed things are left out; a due date sits at the end of its day."""
    def put(title: str, minutes: int, **fields: Any) -> things.Thing:  # noqa: ANN401
        return things.add(
            log.conn, _thing(title, **fields), **{**WRITE, "now": NOW + timedelta(minutes=minutes)},
        )[0]

    put("untimed first", 0, kind="do")
    put("later start", 1, start_at="2026-10-20T10:00:00-07:00")
    put("sooner start", 2, start_at="2026-10-15T09:00:00-07:00")
    put("a due date", 3, kind="do", due="2026-10-16")
    put("a whole day", 4, on_day="2026-10-15")
    put("untimed second", 5, kind="do")
    done = put("finished", 6, start_at="2026-10-14T10:00:00-07:00")
    _do(log, done, "close", state="done")
    assert [t.title for t in things.open_things(log.conn)] == [
        "a whole day", "sooner start", "a due date", "later start", "untimed first",
        "untimed second",
    ]


@pytest.mark.parametrize(
    ("due", "expected"),
    [
        ("2026-10-16", datetime(2026, 10, 16, 23, 59, 59, 999000, tzinfo=ZONE)),
        ("2026-11-01", datetime(2026, 11, 1, 23, 59, 59, 999000, tzinfo=ZONE)),
        ("2026-10-16T17:00:00-07:00", datetime(2026, 10, 16, 17, 0, tzinfo=ZONE)),
        ("2026-10-16T17:00:00+00:00", datetime(2026, 10, 16, 10, 0, tzinfo=ZONE)),
    ],
)
def test_due_ms_is_the_instant_and_a_date_means_the_end_of_that_day(
    log: _Log, due: str, expected: datetime,
) -> None:
    """A date is 23:59:59.999 in his zone; an hour is that instant."""
    one = _put(log, "Pay", "do", due=due)
    assert things.due_ms(one, ZONE) == _ms(expected)
    assert things.due_ms(one, ZoneInfo("Asia/Tokyo")) == (
        _ms(expected) if "T" in due else _ms(expected.replace(tzinfo=ZoneInfo("Asia/Tokyo")))
    )


def test_due_ms_of_a_thing_with_no_due_is_refused(log: _Log) -> None:
    """Only a thing with a due has one."""
    with pytest.raises(ValueError, match="no due time"):
        things.due_ms(_put(log, **_BASE), ZONE)


# --- occurrences -----------------------------------------------------------------------------

_WIDE = (_at(1, 1), _at(12, 1))
_MARCH = "2026-03-03T19:00:00-08:00"  # a Tuesday; the clocks go forward on Sunday the 8th
_OCCURRENCES: dict[str, tuple[dict[str, Any], _Steps, bool, list[tuple[Any, ...]]]] = {
    "one hour by default": (
        _BASE, [], False, [_o("2026-10-15", _at(10, 15, 14), _at(10, 15, 15))],
    ),
    "until its end": (
        {**_BASE, "end_at": "2026-10-15T16:30:00-07:00"}, [], False,
        [_o("2026-10-15", _at(10, 15, 14), _at(10, 15, 16, 30))],
    ),
    "weekly, less a skipped day, across the clock change": (
        {"start_at": _MARCH, "weekly_until": "2026-03-24", "skip_dates": ("2026-03-10",)},
        [], False,
        [
            _o("2026-03-03", _at(3, 3, 19), _at(3, 3, 20)),
            _o("2026-03-17", _at(3, 17, 19), _at(3, 17, 20)),
            _o("2026-03-24", _at(3, 24, 19), _at(3, 24, 20)),
        ],
    ),
    "one date moved": (
        WEEKLY,
        [("move", {
            "occurrence": "2026-10-27", "start_at": "2026-10-28T10:00:00-07:00",
            "end_at": "2026-10-28T11:30:00-07:00",
        })],
        False,
        [
            _o("2026-10-20", _at(10, 20, 19), _at(10, 20, 20)),
            _o("2026-10-27", _at(10, 28, 10), _at(10, 28, 11, 30)),
            _o("2026-11-03", _at(11, 3, 19), _at(11, 3, 20)),
            _o("2026-11-10", _at(11, 10, 19), _at(11, 10, 20)),
        ],
    ),
    "one date moved without an end keeps the length": (
        {**WEEKLY, "end_at": "2026-10-20T21:30:00-07:00", "weekly_until": "2026-10-27"},
        [("move", {"occurrence": "2026-10-27", "start_at": "2026-10-28T10:00:00-07:00"})], False,
        [
            _o("2026-10-20", _at(10, 20, 19), _at(10, 20, 21, 30)),
            _o("2026-10-27", _at(10, 28, 10), _at(10, 28, 12, 30)),
        ],
    ),
    "a done date is left out unless closed ones are asked for": (
        {**WEEKLY, "weekly_until": "2026-10-27"},
        [("close", {"state": "done", "occurrence": "2026-10-20"})], False,
        [_o("2026-10-27", _at(10, 27, 19), _at(10, 27, 20))],
    ),
    "a done date with closed ones asked for": (
        {**WEEKLY, "weekly_until": "2026-10-27"},
        [("close", {"state": "done", "occurrence": "2026-10-20"})], True,
        [
            _o("2026-10-20", _at(10, 20, 19), _at(10, 20, 20), "done"),
            _o("2026-10-27", _at(10, 27, 19), _at(10, 27, 20)),
        ],
    ),
    "a cancelled date is never listed": (
        {**WEEKLY, "weekly_until": "2026-10-27"},
        [("close", {"state": "cancelled", "occurrence": "2026-10-20"})], True,
        [_o("2026-10-27", _at(10, 27, 19), _at(10, 27, 20))],
    ),
    "a thing that is done is left out unless closed ones are asked for": (
        _BASE, [("close", {"state": "paid"})], False, [],
    ),
    "a thing that is paid with closed ones asked for": (
        _BASE, [("close", {"state": "paid"})], True,
        [_o("2026-10-15", _at(10, 15, 14), _at(10, 15, 15), "paid")],
    ),
    "a cancelled thing is never listed": (
        _BASE, [("close", {"state": "cancelled"})], True, [],
    ),
    "a thing moved is at its new time": (
        _BASE, [("move", {"start_at": "2026-10-16T09:00:00-07:00"})], False,
        [_o("2026-10-16", _at(10, 16, 9), _at(10, 16, 10))],
    ),
    "a whole day": (
        {"on_day": "2026-10-16"}, [], False,
        [_o("2026-10-16", _at(10, 16), _at(10, 17), all_day=True)],
    ),
    "a weekly whole day across the day the clock goes forward": (
        {"on_day": "2026-03-01", "weekly_until": "2026-03-22", "skip_dates": ("2026-03-22",)},
        [], False,
        [
            _o("2026-03-01", _at(3, 1), _at(3, 2), all_day=True),
            _o("2026-03-08", _at(3, 8), _at(3, 9), all_day=True),  # 23 hours long
            _o("2026-03-15", _at(3, 15), _at(3, 16), all_day=True),
        ],
    ),
    "a date has no occurrence": ({"due": "2026-10-16"}, [], False, []),
    "an untimed thing has none": ({}, [], False, []),
}


@pytest.mark.parametrize("name", list(_OCCURRENCES))
def test_occurrences_expand_a_thing_into_its_times(log: _Log, name: str) -> None:
    """Weekly, skipped, moved and closed dates, whole days, and a clock change."""
    base, steps, closed, expected = _OCCURRENCES[name]
    one = _put(log, **base)
    for step, kwargs in steps:
        _do(log, one, step, **kwargs)
    found = things.occurrences(log.conn, _ms(_WIDE[0]), _ms(_WIDE[1]), zone=ZONE, closed=closed)
    assert [(o.day, o.start_ms, o.end_ms, o.all_day, o.state) for o in found] == expected
    assert all(o.thing.thing_id == one.thing_id for o in found)


@pytest.mark.parametrize(
    ("first", "last", "found"),
    [
        (_at(10, 15, 13), _at(10, 15, 14), False),  # ends as it starts
        (_at(10, 15, 13), _at(10, 15, 14, 1), True),
        (_at(10, 15, 14, 59), _at(10, 15, 16), True),
        (_at(10, 15, 15), _at(10, 15, 16), False),  # starts as it ends
        (_at(10, 15, 14, 30), _at(10, 15, 14, 31), True),  # inside it
        (_at(10, 15, 0), _at(10, 16, 0), True),
        (_at(10, 16, 0), _at(10, 17, 0), False),
    ],
)
def test_a_window_finds_what_overlaps_it(
    log: _Log, first: datetime, last: datetime, found: bool,  # noqa: FBT001
) -> None:
    """``[start, end)`` against an hour from 14:00 to 15:00."""
    _put(log, **_BASE)
    assert bool(things.occurrences(log.conn, _ms(first), _ms(last), zone=ZONE)) is found


def test_a_whole_day_is_in_a_window_inside_that_day(log: _Log) -> None:
    """An all-day occurrence overlaps any part of its local day."""
    _put(log, on_day="2026-10-16")
    (one,) = things.occurrences(log.conn, _ms(_at(10, 16, 12)), _ms(_at(10, 16, 13)), zone=ZONE)
    assert one.all_day
    assert not things.occurrences(log.conn, _ms(_at(10, 17, 0)), _ms(_at(10, 17, 1)), zone=ZONE)


def test_the_weekly_hour_keeps_its_wall_clock_in_a_week_with_a_clock_change(log: _Log) -> None:
    """Tuesday 19:00 stays 19:00 on both sides of Mar 8; the gap is a week less an hour."""
    _put(log, "Pottery", start_at=_MARCH, weekly_until="2026-03-24")
    found = things.occurrences(log.conn, _ms(_WIDE[0]), _ms(_WIDE[1]), zone=ZONE)
    local = [datetime.fromtimestamp(o.start_ms / 1000, ZONE) for o in found]
    assert [(t.hour, t.minute) for t in local] == [(19, 0)] * 4
    assert [t.utcoffset() for t in local] == [timedelta(hours=-8)] + [timedelta(hours=-7)] * 3
    assert [b.start_ms - a.start_ms for a, b in pairwise(found)] == [
        (7 * 24 - 1) * 3_600_000, 7 * 24 * 3_600_000, 7 * 24 * 3_600_000,
    ]
    assert {o.end_ms - o.start_ms for o in found} == {3_600_000}
    (day,) = things.occurrences(log.conn, _ms(_at(3, 10)), _ms(_at(3, 11)), zone=ZONE)
    assert day.day == "2026-03-10"


def test_a_whole_day_the_clocks_go_forward_is_23_hours(log: _Log) -> None:
    """An ``on_day`` runs from its local midnight to the next local midnight."""
    _put(log, on_day="2026-03-08")
    (one,) = things.occurrences(log.conn, _ms(_at(3, 8)), _ms(_at(3, 9)), zone=ZONE)
    assert one.end_ms - one.start_ms == 23 * 3_600_000


# --- rings -----------------------------------------------------------------------------------

_THU_7PM = "2026-10-15T19:00:00-07:00"
_RING_GRAMMAR: dict[str, tuple[dict[str, Any], str, datetime, str]] = {
    "minutes before": (
        {"start_at": _THU_7PM, "place": "Clinic"}, "-30m", _at(10, 15, 18, 30),
        "19:00 Dentist · Clinic",
    ),
    "at it": (
        {"start_at": _THU_7PM, "place": "Clinic"}, "-0m", _at(10, 15, 19), "19:00 Dentist · Clinic",
    ),
    "the evening before": (
        {"start_at": _THU_7PM}, "-1d@20:00", _at(10, 14, 20), "Tomorrow 19:00 Dentist",
    ),
    "two evenings before": (
        {"start_at": "2026-10-16T19:00:00-07:00"}, "-2d@20:00", _at(10, 14, 20),
        "10-16 19:00 Dentist",
    ),
    "that morning": ({"start_at": _THU_7PM}, "@09:00", _at(10, 15, 9), "19:00 Dentist"),
    "a day of minutes": (
        {"start_at": _THU_7PM}, "-1440m", _at(10, 14, 19), "Tomorrow 19:00 Dentist",
    ),
    "a place too long to say": (
        {"start_at": _THU_7PM, "place": "x" * 41}, "-30m", _at(10, 15, 18, 30), "19:00 Dentist",
    ),
    "a whole day, the evening before": (
        {"on_day": "2026-10-16", "place": "Main St"}, "-1d@20:00", _at(10, 15, 20),
        "Tomorrow: Dentist · Main St",
    ),
    "a whole day, that morning": (
        {"on_day": "2026-10-16"}, "@09:00", _at(10, 16, 9), "Today: Dentist",
    ),
    "a whole day, two days before": (
        {"on_day": "2026-10-16"}, "-2d@18:00", _at(10, 14, 18), "10-16: Dentist",
    ),
    "a whole day, at midnight": ({"on_day": "2026-10-16"}, "-0m", _at(10, 16), "Today: Dentist"),
    "a due hour": (
        {"kind": "do", "due": "2026-10-16T17:00:00-07:00"}, "-30m", _at(10, 16, 16, 30),
        "17:00 Dentist",
    ),
    "a due date counts from that day's start for a clock time": (
        {"kind": "do", "due": "2026-10-16"}, "@09:00", _at(10, 16, 9), "Today: Dentist",
    ),
    "a due date counts the evening before": (
        {"kind": "do", "due": "2026-10-16"}, "-1d@20:00", _at(10, 15, 20), "Tomorrow: Dentist",
    ),
    "a due date counts minutes from its end": (
        {"kind": "do", "due": "2026-10-16"}, "-30m",
        datetime(2026, 10, 16, 23, 29, 59, 999000, tzinfo=ZONE), "Today: Dentist",
    ),
    "private, timed": (
        {"start_at": _THU_7PM, "place": "Clinic", "private": True}, "-30m", _at(10, 15, 18, 30),
        "19:00 you have something on",
    ),
    "private, timed, the evening before": (
        {"start_at": _THU_7PM, "private": True}, "-1d@20:00", _at(10, 14, 20),
        "Tomorrow 19:00 you have something on",
    ),
    "private, a whole day": (
        {"on_day": "2026-10-16", "private": True}, "-1d@20:00", _at(10, 15, 20),
        "you have something to do tomorrow",
    ),
    "private, a whole day, that morning": (
        {"on_day": "2026-10-16", "private": True}, "@09:00", _at(10, 16, 9),
        "you have something to do today",
    ),
    "private, a whole day, two days before": (
        {"on_day": "2026-10-16", "private": True}, "-2d@18:00", _at(10, 14, 18),
        "you have something to do on 10-16",
    ),
}


@pytest.mark.parametrize("name", list(_RING_GRAMMAR))
def test_each_ring_grammar_arms_one_reminder_at_its_time_with_its_words(
    log: _Log, name: str,
) -> None:
    """The ring's id, time and words; a private thing says only its time."""
    fields, ring, due, text = _RING_GRAMMAR[name]
    one = _put(log, **fields, rings=(ring,))
    rings = _rings(log)
    (rid, held) = next(iter(rings.items()))
    assert len(rings) == 1
    assert (held.due_at_ms, held.text) == (_ms(due), text)
    assert rid.startswith(f"{things.RING_PREFIX}{one.thing_id.removeprefix('thing-')[:12]}-")
    assert rid.endswith("-0")
    scheduled = next(iter(log.events("reminder.scheduled")))
    assert scheduled.payload["action_id"] == "thing-rings"
    assert scheduled.payload["due_at_local"] == due.isoformat(timespec="seconds")


def test_the_same_rings_in_chinese(log: _Log) -> None:
    """The words come from the language table in the language of the day."""
    lang.set_language("zh")
    _put(log, "牙医", start_at=_THU_7PM, place="诊所", rings=("-30m",))
    _put(log, "护照", on_day="2026-10-16", rings=("-1d@20:00", "@09:00"))
    assert sorted(r.text for r in _rings(log).values()) == [
        "19:00 牙医 · 诊所", "今天 护照", "明天 护照",
    ]


def test_a_thing_with_two_rings_arms_one_reminder_for_each(log: _Log) -> None:
    """The ring's index is the end of its id; they ring at their own times."""
    one = _put(log, start_at=_THU_7PM, rings=("-1d@20:00", "-30m"))
    assert sorted(_rings(log)) == [_ring_id(one, _THU_7PM, 0), _ring_id(one, _THU_7PM, 1)]
    assert {r.due_at_ms for r in _rings(log).values()} == {
        _ms(_at(10, 14, 20)), _ms(_at(10, 15, 18, 30)),
    }


def test_a_weekly_thing_rings_for_each_occurrence_within_seven_days(log: _Log) -> None:
    """Armed as the horizon reaches each one, across the clock change; none twice."""
    one = _put(
        log, "Pottery", start_at=_MARCH, weekly_until="2026-03-24", rings=("-60m",),
        now=_at(3, 2, 9),
    )
    assert [r.due_at_ms for r in _rings(log).values()] == [_ms(_at(3, 3, 18))]
    for now, expected in (
        (_at(3, 9, 9), [_at(3, 3, 18), _at(3, 10, 18)]),
        (_at(3, 16, 9), [_at(3, 3, 18), _at(3, 10, 18), _at(3, 17, 18)]),
    ):
        things.sync_rings(log.conn, zone=ZONE, now=now)
        assert sorted(r.due_at_ms for r in _rings(log).values()) == [_ms(e) for e in expected]
    assert _ring_id(one, _at(3, 10, 19).isoformat()) in _rings(log)
    assert len(log.events("reminder.scheduled")) == 3
    things.sync_rings(log.conn, zone=ZONE, now=_at(3, 16, 9))
    assert len(log.events("reminder.scheduled")) == 3


def test_sync_twice_changes_nothing_and_the_second_write_arms_nothing_new(log: _Log) -> None:
    """Running the pass again, even with a later clock inside the same window, writes no event."""
    _put(log, start_at=_THU_7PM, rings=("-30m", "@09:00"))
    before = len(log.events())
    things.sync_rings(log.conn, zone=ZONE, now=NOW)
    things.sync_rings(log.conn, zone=ZONE, now=NOW + timedelta(minutes=10))
    assert len(log.events()) == before


def test_a_ring_is_cancelled_when_its_thing_closes(log: _Log) -> None:
    """Done, paid or cancelled: nothing rings for it; reopening arms it again."""
    one = _put(log, start_at=_THU_7PM, rings=("-30m",))
    assert len(_rings(log)) == 1
    _do(log, one, "close", state="done")
    assert _rings(log) == {}
    (cancelled,) = log.events("reminder.cancelled")
    assert cancelled.payload["action_id"] == "thing-rings"
    _do(log, one, "reopen")
    assert len(_rings(log)) == 1
    _do(log, one, "close", state="cancelled", why="off")
    assert _rings(log) == {}


def test_a_moved_thing_rings_at_its_new_time_only(log: _Log) -> None:
    """The old ring is cancelled; the new one has the new anchor in its id."""
    one = _put(log, start_at=_THU_7PM, rings=("-30m",))
    _do(log, one, "move", start_at="2026-10-16T10:00:00-07:00")
    (rid, held) = next(iter(_rings(log).items()))
    assert held.due_at_ms == _ms(_at(10, 16, 9, 30))
    assert rid == _ring_id(one, "2026-10-16T10:00:00-07:00")
    assert held.text == "10:00 Dentist"


def test_a_closed_date_or_a_moved_date_of_a_weekly_thing_changes_only_its_own_ring(
    log: _Log,
) -> None:
    """The series keeps ringing for the other dates."""
    one = _put(log, "Pottery", start_at=TUE_7PM, weekly_until="2026-11-10", rings=("-60m",))
    things.sync_rings(log.conn, zone=ZONE, now=_at(10, 26, 9))
    assert len(_rings(log)) == 2
    _do(log, one, "close", state="cancelled", occurrence="2026-10-20")
    assert sorted(r.due_at_ms for r in _rings(log).values()) == [_ms(_at(10, 27, 18))]
    _do(log, one, "move", occurrence="2026-10-27", start_at="2026-10-27T21:00:00-07:00",
        now=_at(10, 26, 9))
    assert [r.due_at_ms for r in _rings(log).values()] == [_ms(_at(10, 27, 20))]
    _do(log, one, "close", state="cancelled")
    assert _rings(log) == {}


def test_changing_the_words_or_the_rings_updates_the_pending_ring(log: _Log) -> None:
    """Changed words are scheduled again under the same id; a ring removed is cancelled."""
    one = _put(log, "Dentist", start_at=_THU_7PM, rings=("-30m", "-0m"))
    ring_id = _ring_id(one, _THU_7PM, 0)
    _do(log, one, "change", fields={"title": "Dentist (Dr Lee)"})
    assert _rings(log)[ring_id].text == "19:00 Dentist (Dr Lee)"
    assert len(log.events("reminder.scheduled")) == 4  # two, and both again
    _do(log, one, "change", fields={"private": True})
    assert _rings(log)[ring_id].text == "19:00 you have something on"
    _do(log, one, "change", fields={"rings": ["-0m"]})
    assert list(_rings(log)) == [_ring_id(one, _THU_7PM, 0)]
    assert _rings(log)[_ring_id(one, _THU_7PM, 0)].due_at_ms == _ms(_at(10, 15, 19))
    _do(log, one, "change", fields={"rings": []})
    assert _rings(log) == {}


def test_a_ring_whose_time_has_passed_is_skipped_not_made_late(log: _Log) -> None:
    """An hour from now arms the hour-before ring only if that moment is still ahead."""
    one = _put(log, start_at="2026-10-14T09:30:00-07:00", rings=("-60m", "-0m", "-10m"))
    assert sorted(r.due_at_ms for r in _rings(log).values()) == [
        _ms(_at(10, 14, 9, 20)), _ms(_at(10, 14, 9, 30)),
    ]
    assert _ring_id(one, "2026-10-14T09:30:00-07:00", 0) not in _rings(log)
    begun = _put(log, "Begun", start_at="2026-10-14T08:00:00-07:00", rings=("-30m", "-0m"))
    assert not [r for r in _rings(log) if begun.thing_id.removeprefix("thing-")[:12] in r]


def test_a_ring_a_week_or_more_ahead_waits_until_the_horizon_reaches_it(log: _Log) -> None:
    """Only rings in the next seven days are armed; a wider horizon arms more."""
    _put(log, start_at="2026-10-21T08:00:00-07:00", rings=("-0m",))
    far = _put(log, "Far", start_at="2026-10-30T10:00:00-07:00", rings=("-0m",))
    assert [r.due_at_ms for r in _rings(log).values()] == [_ms(_at(10, 21, 8))]  # 6 days 23 hours
    things.sync_rings(log.conn, zone=ZONE, now=NOW, horizon=timedelta(days=30))
    assert len(_rings(log)) == 2
    assert _ring_id(far, "2026-10-30T10:00:00-07:00") in _rings(log)
    later = _put(log, "Later", start_at="2026-12-15T10:00:00-08:00", rings=("-0m",))
    things.sync_rings(log.conn, zone=ZONE, now=_at(12, 10, 9))
    assert _ring_id(later, "2026-12-15T10:00:00-08:00") in _rings(log)


def test_sync_with_thing_ids_looks_only_at_those_things(log: _Log) -> None:
    """The other thing's ring stays unarmed, and its pending ring stays pending."""
    a = _put(log, "A", start_at="2026-10-23T10:00:00-07:00", rings=("-0m",))
    b = _put(log, "B", start_at="2026-10-23T11:00:00-07:00", rings=("-0m",))
    assert _rings(log) == {}  # a week and more ahead
    later = _at(10, 20, 9)
    things.sync_rings(log.conn, zone=ZONE, now=later, thing_ids=[a.thing_id])
    assert list(_rings(log)) == [_ring_id(a, "2026-10-23T10:00:00-07:00")]
    things.sync_rings(log.conn, zone=ZONE, now=later)
    assert sorted(_rings(log)) == sorted([
        _ring_id(a, "2026-10-23T10:00:00-07:00"), _ring_id(b, "2026-10-23T11:00:00-07:00"),
    ])


def test_a_ring_that_rang_is_not_armed_again_and_other_reminders_are_left_alone(log: _Log) -> None:
    """A fired ring stays fired; a reminder the owner set is not the ledger's to cancel."""
    one = _put(log, start_at=_THU_7PM, rings=("-30m",))
    mine = reminder_state.schedule(
        log.conn, due=NOW + timedelta(hours=1), text="call", action_id="a",
    )
    ring_id = _ring_id(one, _THU_7PM)
    emit_event(
        log.conn, type="reminder.fired",
        payload={"reminder_id": ring_id, "fired_at": _ms(_at(10, 15, 18, 30)), "late_ms": 0,
                 "delivered": "speak"},
    )
    things.sync_rings(log.conn, zone=ZONE, now=_at(10, 15, 18, 0))
    _do(log, one, "close", state="done", now=_at(10, 15, 20))
    assert len(log.events("reminder.scheduled")) == 2  # the ring once, and his own
    assert [r.reminder_id for r in reminder_state.pending(log.conn)] == [mine]


def test_a_thing_and_its_rings_are_written_in_one_transaction(
    log: _Log, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the rings cannot be armed, the thing is not written either."""
    def boom(*_args: Any, **_kwargs: Any) -> None:  # noqa: ANN401
        msg = "no rings today"
        raise RuntimeError(msg)

    monkeypatch.setattr(things, "_sync", boom)
    with pytest.raises(RuntimeError, match="no rings today"):
        things.add(log.conn, _thing(start_at=_THU_7PM, rings=("-30m",)), **WRITE)
    assert log.events() == []
    assert not log.conn.in_transaction


def test_writes_join_a_transaction_the_caller_holds(log: _Log) -> None:
    """Inside the caller's transaction nothing is committed until the caller commits."""
    log.conn.execute("BEGIN IMMEDIATE")
    one = _put(log, **_BASE)
    assert log.conn.in_transaction
    log.conn.rollback()
    with pytest.raises(LookupError):
        things.get(log.conn, one.thing_id)


# --- carry-in --------------------------------------------------------------------------------

_LONG = "x" * 200 + "\n" + "y" * 300  # 501 characters, first line 200
_CORE: list[dict[str, Any]] = [
    {"id": "abc123", "topic": "稿", "text": "周五交稿", "sources": []},
    {"topic": "报税", "text": "报税 2026-10-31"},
    {"text": "buy milk"},
    {"text": "   "},
]


def _seed(log: _Log) -> dict[str, str]:
    """The older stores: memos, his reminders and other kinds, commitments."""
    for n, text in enumerate(("buy milk", "Buy  Milk", "fix the bike", _LONG, "  ")):
        emit_event(
            log.conn, type="memo.captured", payload={"text": text, "action_id": f"memo-{n}"},
            ts_epoch_ms=1_000 + n, event_uid=f"memo-uid-{n}",
        )
    ids = {
        "mum": reminder_state.schedule(log.conn, due=NOW + timedelta(hours=2), text="call mum",
                                       action_id="act-1"),
        "late": reminder_state.schedule(log.conn, due=NOW - timedelta(hours=1), text="old one",
                                        action_id="act-2"),
        "bus": reminder_state.schedule(log.conn, due=NOW + timedelta(hours=3), text="bus",
                                       action_id="departure-abc12345"),
        "interview": reminder_state.schedule(log.conn, due=NOW + timedelta(hours=4),
                                             text="interview", action_id="job-interview-reminders"),
        "gone": reminder_state.schedule(log.conn, due=NOW + timedelta(hours=5), text="gone",
                                        action_id="act-3"),
    }
    reminder_state.cancel(log.conn, ids["gone"], action_id="act-3")
    ids["cm"] = f"{commitments.REMINDER_PREFIX}abc-1"
    emit_event(
        log.conn, type="reminder.scheduled",
        payload={"reminder_id": ids["cm"], "due_at_epoch_ms": _ms(NOW + timedelta(hours=6)),
                 "due_at_local": "x", "text": "pottery", "action_id": "commitment-reminders"},
    )
    return ids


def _commit(log: _Log, title: str, start: datetime, **extra: Any) -> str:  # noqa: ANN401
    fields: dict[str, Any] = {
        "commitment_id": commitments.new_id(), "title": title,
        "start_at": start.isoformat(timespec="seconds"), "source": "said", "confirmed": True,
        "private": False,
    }
    record = commitments.Commitment(**(fields | extra))
    commitments.note(log.conn, record)
    return record.commitment_id


def _carry(log: _Log, **kwargs: Any) -> dict[str, int] | None:  # noqa: ANN401
    args: dict[str, Any] = {
        "zone": ZONE, "now": NOW, "core_items": _CORE, "before_min": 60, **kwargs,
    }
    return things.carry_in(log.conn, **args)


def test_the_carry_in_merges_memos_and_keeps_a_long_one_in_its_note(log: _Log) -> None:
    """Each memo is an untimed ``do``; exact repeats are one; words over 300 go to the note."""
    _seed(log)
    _carry(log)
    memos = [t for t in things.fold(log.conn).values() if t.history[0].ref.startswith("memo-uid")]
    assert [(t.title[:12], t.kind, t.source, t.history[0].by) for t in memos] == [
        ("buy milk", "do", "said", "carry_in"),
        ("fix the bike", "do", "said", "carry_in"),
        ("x" * 12, "do", "said", "carry_in"),
    ]
    assert [t.history[0].ref for t in memos] == ["memo-uid-0", "memo-uid-2", "memo-uid-3"]
    long = memos[2]
    assert (long.title, long.note) == ("x" * 200, _LONG)
    assert long.history[0].evidence == _LONG[:200]
    assert [t.added_ms for t in memos] == [1_000, 1_002, 1_003]  # when he noted them
    assert all(not (t.start_at or t.on_day or t.due or t.rings) for t in memos)
    untimed = [t.title[:8] for t in things.open_things(log.conn) if not t.start_at]
    assert untimed[:3] == ["buy milk", "fix the ", "xxxxxxxx"]  # oldest first


def test_the_carry_in_makes_his_reminders_things_that_ring_and_cancels_the_old_ones(
    log: _Log,
) -> None:
    """His own pending reminders are carried; a bus pin's, the interview's, a cancelled not."""
    ids = _seed(log)
    _carry(log)
    mine = {t.title: t for t in things.fold(log.conn).values() if t.history[0].ref in ids.values()}
    assert sorted(mine) == ["call mum", "old one"]
    mum = mine["call mum"]
    assert (mum.kind, mum.start_at, mum.rings, mum.source, mum.history[0].by) == (
        "do", "2026-10-14T11:00:00-07:00", ("-0m",), "said", "carry_in",
    )
    assert mum.history[0].ref == ids["mum"]
    pending = {r.reminder_id: r for r in reminder_state.pending(log.conn)}
    assert ids["mum"] not in pending  # the ring under the thing took its place
    assert _ring_id(mum, "2026-10-14T11:00:00-07:00") in pending
    assert pending[_ring_id(mum, "2026-10-14T11:00:00-07:00")].text == "11:00 call mum"
    assert {ids["bus"], ids["interview"]} <= set(pending)  # not his: left to their own passes
    assert ids["late"] in pending  # already due: still rings, late, as ADR 0179 says
    assert ids["cm"] not in pending
    cancelled = [e.payload for e in log.events("reminder.cancelled")]
    assert {p["reminder_id"] for p in cancelled if p["action_id"] == "thing-carry-in"} == {
        ids["mum"], ids["cm"],
    }
    assert _ring_id(mine["old one"], "2026-10-14T08:00:00-07:00") not in pending  # past: no ring


def test_the_carry_in_keeps_the_hex_of_a_commitment_and_its_fields(log: _Log) -> None:
    """A live commitment is a ``go`` thing with the same hex, its fields and its ring."""
    _seed(log)
    said = _commit(
        log, "Dentist", _at(10, 15, 14), end_at=_at(10, 15, 15).isoformat(), place="Clinic",
        link="https://x.example/a", private=True, evidence="he said",
    )
    mail = _commit(
        log, "Pottery", _at(10, 22, 19), source="mail", confirmed=False, source_ref="m1",
        sender_domain="studio.example", weekly_until="2026-11-05", skip_dates=("2026-10-29",),
        evidence="Tuesdays 7 PM",
    )
    day = _commit(log, "Passport", _at(10, 20, 0), all_day=True, place="Main St")
    dropped = _commit(log, "Cancelled one", _at(10, 25, 10))
    commitments.drop(log.conn, dropped, "cancelled")
    counts = _carry(log, before_min=45)
    assert counts is not None
    assert counts["commitments"] == 3
    hexes = {c.removeprefix("commitment-"): c for c in (said, mail, day)}
    held = {c.removeprefix("thing-"): t for c, t in things.fold(log.conn).items()}
    assert set(hexes) <= set(held)
    assert dropped.removeprefix("commitment-") not in held
    a, b, c = (held[h] for h in hexes)
    assert (a.thing_id, a.kind, a.title, a.start_at, a.end_at, a.place, a.link, a.private) == (
        said.replace("commitment-", "thing-"), "go", "Dentist",
        _at(10, 15, 14).isoformat(timespec="seconds"), _at(10, 15, 15).isoformat(),
        "Clinic", "https://x.example/a", True,
    )
    assert (a.confirmed, a.source, a.rings, a.history[0].ref, a.history[0].evidence) == (
        True, "said", ("-45m",), said, "he said",
    )
    assert (b.confirmed, b.source, b.source_ref, b.sender, b.weekly_until, b.skip_dates) == (
        False, "mail", "m1", "studio.example", "2026-11-05", ("2026-10-29",),
    )
    assert (c.start_at, c.on_day, c.rings, c.place) == ("", "2026-10-20", ("-1d@20:00",), "Main St")
    assert (b.state, a.state) == ("open", "open")


def test_the_carry_in_cancels_every_commitment_reminder_and_arms_the_rings(log: _Log) -> None:
    """The ``reminder-cm`` ones go; the same moments ring again under ``reminder-th``."""
    ids = _seed(log)
    one = _commit(log, "Dentist", _at(10, 15, 14))
    all_day = _commit(log, "Passport", _at(10, 20, 0), all_day=True)
    _carry(log)
    pending = {r.reminder_id: r for r in reminder_state.pending(log.conn)}
    assert not [r for r in pending if r.startswith(commitments.REMINDER_PREFIX)]
    assert ids["cm"] not in pending
    rings = {r.text: r.due_at_ms for r in _rings(log).values()}
    assert rings["14:00 Dentist"] == _ms(_at(10, 15, 13))
    assert rings["Tomorrow: Passport"] == _ms(_at(10, 19, 20))
    assert one != all_day


def test_an_unconfirmed_commitment_whose_time_passed_is_cancelled_and_the_others_stay(
    log: _Log,
) -> None:
    """Confirmed past stays open; unconfirmed future stays so; unconfirmed past is cancelled."""
    past = _commit(log, "Old mail", _at(10, 10, 10), source="mail", confirmed=False)
    spoken = _commit(log, "Said and past", _at(10, 9, 10))
    future = _commit(log, "Mail ahead", _at(10, 22, 19), source="mail", confirmed=False)
    series_over = _commit(
        log, "Series over", _at(9, 29, 19), source="mail", confirmed=False,
        weekly_until="2026-10-06",
    )
    series_on = _commit(
        log, "Series on", _at(10, 6, 19), source="mail", confirmed=False, weekly_until="2026-10-27",
    )
    counts = _carry(log)
    assert counts is not None
    assert counts["cancelled"] == 2
    got = {c: things.get(log.conn, c.replace("commitment-", "thing-")) for c in
           (past, spoken, future, series_over, series_on)}
    assert [got[c].state for c in (past, spoken, future, series_over, series_on)] == [
        "cancelled", "open", "open", "cancelled", "open",
    ]
    assert got[future].confirmed is False
    last = got[past].history[-1]
    assert (last.what, last.why, last.by) == (
        "cancelled", "time passed, never confirmed", "carry_in",
    )
    assert got[past].closed_ms == _ms(NOW)


def test_the_carry_in_makes_a_to_do_of_each_core_memory_item_once(log: _Log) -> None:
    """Untimed ``do`` things; their reference is the item's id, else its topic; repeats merge."""
    _seed(log)
    counts = _carry(log)
    assert counts == {
        "memos": 3, "reminders": 2, "commitments": 0, "core_items": 2, "cancelled": 0,
        "reminders_cancelled": 2, "skipped": 0,
    }
    core = [t for t in things.fold(log.conn).values() if t.history[0].ref.startswith("core:")]
    first = [t.history[0] for t in core]
    got = [(t.title, t.kind, h.ref, h.evidence, h.by) for t, h in zip(core, first, strict=True)]
    assert got == [
        ("周五交稿", "do", "core:abc123", "周五交稿", "carry_in"),
        ("报税 2026-10-31", "do", "core:报税", "报税 2026-10-31", "carry_in"),
    ]  # "buy milk" was a memo already
    assert all(not (t.start_at or t.on_day or t.due) for t in core)


def test_the_carry_in_runs_once_and_says_what_it_did(log: _Log) -> None:
    """A second call returns None and writes nothing; the marker holds the counts."""
    _seed(log)
    counts = _carry(log)
    (marker,) = log.events("thing.carried_in")
    assert marker.payload == {"version": 1, "counts": counts}
    events = len(log.events())
    assert _carry(log) is None
    assert len(log.events()) == events


def test_a_carry_in_that_fails_leaves_nothing_behind_and_can_be_run_again(
    log: _Log, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One transaction: the things, the cancelled reminders and the marker go together."""
    ids = _seed(log)
    _commit(log, "Dentist", _at(10, 15, 14))
    events = len(log.events())
    real = things._sync  # noqa: SLF001

    def boom(*_args: Any, **_kwargs: Any) -> None:  # noqa: ANN401
        msg = "cut short"
        raise RuntimeError(msg)

    monkeypatch.setattr(things, "_sync", boom)
    with pytest.raises(RuntimeError, match="cut short"):
        _carry(log)
    assert len(log.events()) == events
    assert not log.events("thing.added", "thing.carried_in")
    assert ids["mum"] in {r.reminder_id for r in reminder_state.pending(log.conn)}
    monkeypatch.setattr(things, "_sync", real)
    assert _carry(log) is not None


def test_what_cannot_be_carried_in_is_left_out_and_counted(log: _Log) -> None:
    """A commitment that cannot be read back is skipped; the rest still goes in."""
    ok = _commit(log, "Fine", _at(10, 15, 14))
    commitments.note(log.conn, commitments.Commitment(
        commitment_id="commitment-" + "f" * 32, title="x" * 400,
        start_at=_at(10, 16, 9).isoformat(),
        source="mail", confirmed=False, private=False,
    ))
    counts = _carry(log, core_items=[])
    assert counts is not None
    assert (counts["commitments"], counts["skipped"]) == (2, 0)  # the title is cut, not refused
    assert things.get(log.conn, ok.replace("commitment-", "thing-")).title == "Fine"
    assert len(things.get(log.conn, "thing-" + "f" * 32).title) == 300


# --- the runtime pass and the daemon's start --------------------------------------------------


def _core_doc_with_to_dos(path: Path) -> None:
    old = {
        "经历": ["a"], "定下来的规矩": ["e"], "承诺和待办": ["周五交稿", "报税 2026-10-31"],
        "生活里的几件事": ["写论文"],
    }
    raw: dict[str, Any] = {
        "sections": {
            s: [{"text": t, "topic": None, "sources": [], "since": "2026-10-01"} for t in ts]
            for s, ts in old.items()
        },
    }
    with closing(open_memory_db(path)) as conn, conn:
        conn.execute(
            "INSERT INTO core_memory (id, ts, base_id, origin, upto_day, doc, changes, chars) "
            "VALUES ('core:old', '2026-10-01T09:00:00-07:00', NULL, 'nightly', '2026-10-01', ?, "
            "'[]', 0)",
            (json.dumps(raw, ensure_ascii=False),),
        )


def _versions(path: Path) -> list[tuple[str, str | None]]:
    with closing(open_memory_db(path)) as conn:
        return [
            (str(origin), upto)
            for origin, upto in conn.execute(
                "SELECT origin, upto_day FROM core_memory ORDER BY rowid",
            )
        ]


def test_the_runtime_carries_core_memorys_to_dos_in_and_writes_a_version_without_them(
    log: _Log,
) -> None:
    """The items are read raw, become things, and the section leaves the stored document once."""
    _core_doc_with_to_dos(log.memory)
    assert [i["text"] for i in due_items(log.memory)] == ["周五交稿", "报税 2026-10-31"]
    ledger = ThingsLedger(log.paths.event_log, log.memory, ZONE, 60)
    ledger.now = lambda: NOW
    counts = ledger.carry_in()
    assert counts is not None
    assert counts["core_items"] == 2
    assert [t.title for t in things.open_things(log.conn)] == ["周五交稿", "报税 2026-10-31"]
    assert due_items(log.memory) == []
    assert _versions(log.memory) == [("nightly", "2026-10-01"), ("migration", "2026-10-01")]
    kept = current_core_memory(log.memory).doc
    assert {s: [i["text"] for i in kept[s]] for s in kept if kept[s]} == {
        "经历": ["a"], "定下来的规矩": ["e"], "生活里的几件事": ["写论文"],
    }
    with closing(open_memory_db(log.memory)) as conn:
        (raw,) = conn.execute("SELECT doc FROM core_memory ORDER BY rowid DESC LIMIT 1").fetchone()
    assert "承诺和待办" not in json.loads(raw)["sections"]
    assert ledger.carry_in() is None
    assert len(_versions(log.memory)) == 2
    assert render_context(log.memory, exclude_id="").profile.count("###") == 3


def test_a_start_cut_between_the_carry_in_and_the_core_memory_write_finishes_the_next_time(
    log: _Log,
) -> None:
    """The ledger holds the items and the document still has the section: it is dropped, once."""
    _core_doc_with_to_dos(log.memory)
    things.carry_in(
        log.conn, zone=ZONE, now=NOW, core_items=due_items(log.memory), before_min=60,
    )
    ledger = ThingsLedger(log.paths.event_log, log.memory, ZONE, 60)
    assert ledger.carry_in() is None
    assert due_items(log.memory) == []
    assert len(_versions(log.memory)) == 2
    assert len(things.fold(log.conn)) == 2
    assert drop_due_section(log.memory) is False


def test_a_store_with_no_core_memory_is_left_alone(log: _Log) -> None:
    """Nothing to drop writes nothing, and reading the items writes no version."""
    assert due_items(log.memory) == []
    assert drop_due_section(log.memory) is False
    ledger = ThingsLedger(log.paths.event_log, log.memory, ZONE, 60)
    assert ledger.carry_in() == dict.fromkeys(
        ("memos", "reminders", "commitments", "core_items", "cancelled", "reminders_cancelled",
         "skipped"), 0,
    )
    assert _versions(log.memory) == []


def test_the_rings_pass_arms_the_next_seven_days_and_can_run_twice(log: _Log) -> None:
    """``sync`` is ``things.sync_rings`` with the runtime's clock and zone."""
    one = _put(log, start_at="2026-10-23T10:00:00-07:00", rings=("-0m",))
    ledger = ThingsLedger(log.paths.event_log, log.memory, ZONE, 60)
    ledger.now = lambda: NOW
    ledger.sync()
    assert _rings(log) == {}
    ledger.now = lambda: _at(10, 20, 9)
    ledger.sync()
    ledger.sync()
    assert list(_rings(log)) == [_ring_id(one, "2026-10-23T10:00:00-07:00")]
    assert len(log.events("reminder.scheduled")) == 1


def test_the_rings_pass_runs_at_once_and_again_every_interval(
    log: _Log, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run`` carries in if boot did not, syncs, sleeps, and syncs again."""
    monkeypatch.setattr(ledger_module, "PASS_S", 0.01)
    ledger = ThingsLedger(log.paths.event_log, log.memory, ZONE, 60)
    clock = [NOW]
    ledger.now = lambda: clock[0]
    _put(log, start_at="2026-10-23T10:00:00-07:00", rings=("-0m",))

    async def go() -> None:
        task = asyncio.create_task(ledger.run())
        await asyncio.sleep(0.2)
        assert _rings(log) == {}
        clock[0] = _at(10, 20, 9)
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert len(_rings(log)) == 1
    assert len(log.events("thing.carried_in")) == 1


def test_the_daemon_carries_in_once_at_boot_with_the_configured_lead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Built from the runtime root's own logs: a commitment rings ``remind_before_min`` ahead."""
    root = tmp_path / "root"
    paths = bootstrap_runtime(root)
    (root / "settings.yaml").write_text(
        "commitments:\n  enabled: false\n  remind_before_min: 45\n", encoding="utf-8",
    )
    start = datetime.now(UTC).astimezone() + timedelta(days=30)
    with closing(open_event_log(paths.event_log)) as conn:
        record = commitments.Commitment(
            commitment_id=commitments.new_id(), title="Dentist",
            start_at=start.replace(microsecond=0).isoformat(), source="said", confirmed=True,
            private=False,
        )
        commitments.note(conn, record)
        emit_event(conn, type="memo.captured", payload={"text": "buy milk", "action_id": "m"})
    open_memory_db(root / "memory.db").close()
    _core_doc_with_to_dos(root / "memory.db")
    monkeypatch.setenv("HOME", str(tmp_path))
    runtime = boot_runtime(root)
    try:
        assert isinstance(runtime.things, ThingsLedger)
        with closing(open_event_log(paths.event_log)) as conn:
            folded = things.fold(conn)
            assert {t.title for t in folded.values()} == {
                "Dentist", "buy milk", "报税 2026-10-31", "周五交稿",
            }
            assert folded[record.commitment_id.replace("commitment-", "thing-")].rings == ("-45m",)
            assert len(list(iter_events_of_types(conn, ("thing.carried_in",)))) == 1
        assert due_items(root / "memory.db") == []
    finally:
        runtime.conn.close()
    again = boot_runtime(root)
    try:
        with closing(open_event_log(paths.event_log)) as conn:
            assert len(list(iter_events_of_types(conn, ("thing.carried_in",)))) == 1
    finally:
        again.conn.close()


# --- core memory with ten sections -----------------------------------------------------------

_TEN = [
    "经历", "性格和价值观", "身体", "钱", "兴趣和品味", "你希望被怎么对待", "定下来的规矩",
    "证件和硬期限", "身边的人", "生活里的几件事",
]


def test_core_memory_has_ten_sections_and_the_to_dos_cap_went_to_the_goals() -> None:
    """承诺和待办 is no section; 生活里的几件事 holds 10,000 and the ceiling is still 40,000."""
    assert list(core_memory.SECTIONS) == _TEN
    assert list(core_memory.SECTION_CAPS.values()) == [
        4000, 3000, 2000, 2000, 2000, 3000, 3000, 1000, 10000, 10000,
    ]
    assert sum(core_memory.SECTION_CAPS.values()) == 40_000
    assert core_memory.SECTION_CAPS[core_memory.GOALS_SECTION] == 10_000
    assert core_memory.DUE_SECTION not in core_memory.SECTIONS
    assert list(core_memory.empty_doc()) == _TEN


def test_the_config_names_the_same_ten_sections_and_the_night_is_not_told_to_write_to_dos() -> None:
    """``config/jarvis.yaml`` agrees with the code on the sections, the caps and the prompt."""
    from tests.canary._helpers import repo_root  # noqa: PLC0415 - only this test reads the config

    config = yaml.safe_load((repo_root() / "config" / "jarvis.yaml").read_text("utf-8"))
    block = config["core_memory"]
    assert list(block["section_caps"]) == _TEN
    assert block["section_caps"] == core_memory.SECTION_CAPS
    assert sum(block["section_caps"].values()) == block["max_chars"]
    assert "承诺和待办" not in block["prompt"]
    assert all(f"{name}:" in block["prompt"] for name in _TEN)


def test_a_stored_document_that_still_has_the_section_loads_without_it(tmp_path: Path) -> None:
    """Until the carry-in drops it, the old section is left out of every read, not an error."""
    path = tmp_path / "memory.db"
    open_memory_db(path).close()
    _core_doc_with_to_dos(path)
    doc = current_core_memory(path).doc
    assert list(doc) == _TEN
    assert [i["text"] for i in doc["生活里的几件事"]] == ["写论文"]
    assert "承诺和待办" not in render_context(path, exclude_id="").profile
    assert core_memory.item_count(doc) == 3
    assert [i["text"] for i in due_items(path)] == ["周五交稿", "报税 2026-10-31"]
    remember_fact(path, "晨跑", "每天 6 点", "身体")  # a later write drops the section silently
    assert "承诺和待办" not in current_core_memory(path).doc


def test_the_memory_page_shows_ten_sections(tmp_path: Path) -> None:
    """The overview lists the ten, with the goals' new cap."""
    path = tmp_path / "memory.db"
    open_memory_db(path).close()
    _core_doc_with_to_dos(path)
    page = memory_page.overview(path, max_chars=40_000, booted_max_chars=40_000)
    assert [s["name"] for s in page["sections"]] == _TEN
    assert {s["name"]: s["cap"] for s in page["sections"]}["生活里的几件事"] == 10_000
    assert page["items"] == 3


@pytest.mark.parametrize("section", ["承诺和待办", "其他"])
def test_the_night_cannot_add_to_a_section_that_is_not_one(section: str) -> None:
    """An add to the retired section is rejected like any section outside the ten."""
    answer = json.dumps(
        {"changes": [{"op": "add", "section": section, "text": "周五交稿", "sources": ["r1"]}]},
    )
    changes, notes, why = check_core_memory(
        answer, "stop", doc=core_memory.empty_doc(), labels={"r1": "rec-1"}, max_stale=5,
        max_chars=40_000, day="2026-10-14",
    )
    assert (changes, notes) == (None, None)
    assert why == f"section {section!r} is not one of the ten"


def test_a_date_in_a_goal_is_not_swept_stale_by_code() -> None:
    """The date sweep belonged to 承诺和待办; nothing marks an item stale without the model."""
    doc = core_memory.empty_doc()
    doc["生活里的几件事"] = [
        {"text": "写论文,截至 2026-10-01", "topic": None, "sources": [], "since": "x"},
    ]
    changes, notes, why = check_core_memory(
        '{"changes": []}', "stop", doc=doc, labels={}, max_stale=5, max_chars=40_000,
        day=date(2026, 10, 14).isoformat(),
    )
    assert (changes, notes, why) == ([], [], None)

