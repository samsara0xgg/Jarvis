"""ADR 0215: the final answer row names the times the turn itself set or showed.

Acceptance checks over a real event log:

- ``_turn_times`` reads the turn's own rows, as a table of what the turn did to the entries it
  gets: reminders it scheduled (``ref`` is the reminder's id, the day line's id for it), Outlook
  events it created or updated without error, the first leave time of its transit card; a turn
  that did none of these, or only failed ones, gets nothing; another turn's rows are not read;
- entries come in log order and at most five; an Outlook time in a Windows zone name is skipped;
- the whole path: ``drive_turn`` puts them on the final ``surface.response_emitted`` row, and the
  key is absent when there are none.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from jarvis import runtime as runtime_module
from jarvis.runtime import _turn_times
from jarvis.state import reminders
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.surface.cli import emit_surface_user_intent
from tests.integration.test_spoken_streaming import _Peer, _spoken_runtime
from tests.integration.test_wire_routine_streaming import _drive, _payloads

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

ZONE = ZoneInfo("America/Vancouver")
CREATE = "mcp__microsoft__create-calendar-event"
UPDATE = "mcp__microsoft__update-calendar-event"
EVENT_ID = "AAMkAGI2TG93AAA="


@pytest.fixture(autouse=True)
def _fixture_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROUTINE_STREAM_FIXTURE_KEY", "synthetic")
    monkeypatch.setattr(runtime_module, "_labels_phases", lambda _base_url: True)


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _proposed(
    conn: sqlite3.Connection, turn: str, action_id: str, tool: str, arguments: dict[str, Any],
) -> None:
    emit_event(
        conn, type="action.proposed",
        payload={
            "action_id": action_id, "tool_name": tool, "caller_principal": "jarvis_llm",
            "risk_level": "L1", "turn_id": turn, "arguments": arguments,
        },
        correlation={"action_id": action_id, "turn_id": turn},
    )


def _result(
    conn: sqlite3.Connection, action_id: str, output: dict[str, Any] | None, *, error: bool = False,
) -> None:
    payload: dict[str, Any] = {
        "action_id": action_id, "semantics": "error" if error else "ack",
        "tool_output": json.dumps(output or {"error": "boom"}, ensure_ascii=False),
    }
    if error:
        payload["error"] = "mcp_tool_error"
    emit_event(
        conn, type="action.result_observed", payload=payload,
        correlation={"action_id": action_id},
    )


def _reminder(conn: sqlite3.Connection, turn: str, action_id: str, due: datetime, text: str) -> str:
    """What ``set_reminder`` writes: the proposal on the turn, and the reminder it schedules."""
    _proposed(conn, turn, action_id, "set_reminder", {"text": text, "at": due.isoformat()})
    return reminders.schedule(conn, due=due, text=text, action_id=action_id)


def _body(subject: str, at: str, zone: str) -> dict[str, Any]:
    return {"body": {"subject": subject, "start": {"dateTime": at, "timeZone": zone}}}


def _event(  # noqa: PLR0913 - one call is one Outlook write
    conn: sqlite3.Connection, turn: str, action_id: str, tool: str, arguments: dict[str, Any],
    *, error: bool = False, event_id: str = EVENT_ID,
) -> None:
    _proposed(conn, turn, action_id, tool, arguments)
    answer = {"text": json.dumps({"id": event_id, "subject": "x"})}
    _result(conn, action_id, None if error else answer, error=error)


def _card(conn: sqlite3.Connection, turn: str, leave_at: str, made_at: str) -> None:
    trip = {
        "offer_id": "offer-9", "to": "University of Victoria", "made_at": made_at,
        "options": [
            {"index": 0, "route": "28", "board_stop": "Hillside", "leave_at": leave_at,
             "departs": "08:25", "arrive_at": "08:55", "to": "UVic"},
            {"index": 1, "route": "4", "board_stop": "Hillside", "leave_at": "09:10",
             "departs": "09:15", "arrive_at": "09:50", "to": "UVic"},
        ],
    }
    emit_event(
        conn, type="clarification.requested",
        payload={
            "clarification_id": "offer-9", "question": "UVic", "fields": [], "turn_id": turn,
            "action_id": "A-card", "trip": trip,
        },
        correlation={"action_id": "A-card"},
    )


def test_each_thing_the_turn_set_or_showed_is_one_time(tmp_path: Path) -> None:
    """A reminder, an Outlook event made and one moved, and the bus card, in the order they ran."""
    conn = open_event_log(tmp_path / "events.db")
    due = datetime(2026, 10, 11, 9, 0, tzinfo=ZONE)
    rid = _reminder(conn, "T1", "A-rem", due, "给妈妈打电话")
    _event(
        conn, "T1", "A-new", CREATE,
        _body("Dentist", "2026-10-12T15:00:00.0000000", "America/Vancouver"),
    )
    _event(
        conn, "T1", "A-mv", UPDATE,
        {"eventId": "evt-77", **_body("Standup", "2026-10-13T09:30:00", "UTC")},
    )
    _card(conn, "T1", "08:20", "2026-10-10T08:00-07:00")

    assert _turn_times(conn, "T1") == [
        {"kind": "reminder", "at_ms": _ms(due), "label": "给妈妈打电话", "ref": rid},
        {"kind": "event", "at_ms": _ms(datetime(2026, 10, 12, 15, 0, tzinfo=ZONE)),
         "label": "Dentist", "ref": EVENT_ID},
        {"kind": "event", "at_ms": _ms(datetime(2026, 10, 13, 9, 30, tzinfo=ZoneInfo("UTC"))),
         "label": "Standup", "ref": "evt-77"},
        {"kind": "departure", "at_ms": _ms(datetime(2026, 10, 10, 8, 20, tzinfo=ZONE)),
         "label": "University of Victoria", "ref": "offer-9"},
    ]


def test_a_reminders_ref_is_its_reminder_id_the_day_lines_id_for_it(tmp_path: Path) -> None:
    """The phone matches a dial time to the reminder it shows by this id."""
    conn = open_event_log(tmp_path / "events.db")
    due = datetime(2026, 10, 11, 9, 0, tzinfo=ZONE)
    rid = _reminder(conn, "T1", "A-1", due, "x" * 100)
    [one] = _turn_times(conn, "T1")
    assert rid.startswith(reminders.ID_PREFIX)
    assert (one["kind"], one["ref"]) == ("reminder", rid)
    assert len(one["label"]) == 40, "labels are clipped to 40"
    assert reminders.fold(conn)[one["ref"]].due_at_ms == one["at_ms"]


def test_a_move_gives_only_the_new_time(tmp_path: Path) -> None:
    """Set the new reminder, cancel the old: the cancelled one was scheduled in an older turn."""
    conn = open_event_log(tmp_path / "events.db")
    old = _reminder(conn, "T-old", "A-old", datetime(2026, 10, 11, 9, 0, tzinfo=ZONE), "call mom")
    new_due = datetime(2026, 10, 12, 9, 0, tzinfo=ZONE)
    new = _reminder(conn, "T-move", "A-new", new_due, "call mom")
    _proposed(conn, "T-move", "A-cancel", "cancel_reminder", {"reminder_id": old})
    reminders.cancel(conn, old, action_id="A-cancel")
    assert _turn_times(conn, "T-move") == [
        {"kind": "reminder", "at_ms": _ms(new_due), "label": "call mom", "ref": new},
    ]


def test_what_failed_or_cannot_be_read_gives_no_time(tmp_path: Path) -> None:
    """An errored write, a Windows zone name, an update that moved no time, a card with no trip."""
    conn = open_event_log(tmp_path / "events.db")
    at = "2026-10-12T15:00:00"
    _event(conn, "T1", "A-err", CREATE, _body("Lost", at, "America/Vancouver"), error=True)
    _event(conn, "T1", "A-win", CREATE, _body("Windows", at, "Pacific Standard Time"))
    _event(conn, "T1", "A-title", UPDATE, {"eventId": "evt-1", "body": {"subject": "Renamed"}})
    _proposed(conn, "T1", "A-pending", CREATE, _body("Awaiting the card", at, "UTC"))
    emit_event(  # a card with no trip is a question, not a time
        conn, type="clarification.requested",
        payload={"clarification_id": "q", "question": "Which?", "fields": [], "turn_id": "T1"},
    )
    emit_event(  # a trip kept before it had a day is unreadable
        conn, type="clarification.requested",
        payload={"clarification_id": "old", "question": "x", "fields": [], "turn_id": "T1",
                 "trip": {"offer_id": "old", "to": "x", "options": [{"leave_at": "08:00"}]}},
    )
    assert _turn_times(conn, "T1") == []
    assert _turn_times(conn, "T-none") == []


def test_another_turns_rows_are_not_this_turns(tmp_path: Path) -> None:
    """A reminder and a card of another turn are not read."""
    conn = open_event_log(tmp_path / "events.db")
    _reminder(conn, "T-other", "A-other", datetime(2026, 10, 11, 9, 0, tzinfo=ZONE), "other")
    _card(conn, "T-other", "08:20", "2026-10-10T08:00-07:00")
    mine = datetime(2026, 10, 11, 10, 0, tzinfo=ZONE)
    rid = _reminder(conn, "T-mine", "A-mine", mine, "mine")
    assert [t["ref"] for t in _turn_times(conn, "T-mine")] == [rid]


def test_at_most_five_in_log_order(tmp_path: Path) -> None:
    """Seven reminders: the five that ran first, in that order."""
    conn = open_event_log(tmp_path / "events.db")
    first = datetime(2026, 10, 11, 9, 0, tzinfo=ZONE)
    ids = [
        _reminder(conn, "T1", f"A-{n}", first + timedelta(hours=7 - n), f"r{n}") for n in range(7)
    ]
    got = _turn_times(conn, "T1")
    assert [t["ref"] for t in got] == ids[:5], "the first five as they ran, not the earliest"
    assert [t["label"] for t in got] == ["r0", "r1", "r2", "r3", "r4"]


def test_the_final_row_carries_times_and_leaves_the_key_out_when_there_are_none(
    tmp_path: Path,
) -> None:
    """Through ``drive_turn``: the answer's ``surface.response_emitted`` is what the phone reads."""
    due = datetime.now(ZONE).replace(microsecond=0) + timedelta(days=1)
    with _Peer([[("final_answer", "好的, 明天提醒你。")], [("final_answer", "没问题。")]]) as peer:
        runtime = _spoken_runtime(tmp_path, peer.url)
        rid = _reminder(runtime.conn, "T-with", "A-with", due, "给妈妈打电话")
        for turn in ("T-with", "T-without"):
            intent = emit_surface_user_intent(
                runtime.conn, transcript="把它推到明天", turn_id=turn, channel="inherent_ptt",
                ingestion_node="iphone",
            )
            _drive(runtime, intent)
    with_times, without = (
        [p for p in _payloads(runtime.conn, "surface.response_emitted") if p["turn_id"] == turn]
        for turn in ("T-with", "T-without")
    )
    assert with_times[-1]["times"] == [
        {"kind": "reminder", "at_ms": _ms(due), "label": "给妈妈打电话", "ref": rid},
    ]
    assert "times" not in without[-1]
