"""What the Event Log says about one action: its proposal, its result and the reminders it set.

ADR 0349. A real on-disk Event Log; the rows are the ones L4 writes for a tool call.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from jarvis.state import reminders
from jarvis.state.action_facts import (
    ActionInput,
    ProposedCall,
    action_result,
    input_behind_action,
    proposed_call,
)
from jarvis.state.event_log import latest_id
from tests.support.turn_rows import TurnLog

if TYPE_CHECKING:
    from pathlib import Path


def _proposed(log: TurnLog, action_id: str, tool: str, **more: object) -> None:
    log.put(
        "action.proposed",
        {
            "action_id": action_id, "tool_name": tool, "caller_principal": "jarvis",
            "risk_level": "low", **more,
        },
    )


def test_the_call_an_action_proposed(tmp_path: Path) -> None:
    """The tool and its arguments as sent; an action never proposed has no call."""
    log = TurnLog(tmp_path)
    assert proposed_call(log.conn, "A1") is None
    _proposed(log, "A1", "weather", arguments={"place": "Vancouver"})
    _proposed(log, "A2", "my_day")
    _proposed(log, "A3", "mail", arguments="not an object")
    assert proposed_call(log.conn, "A1") == ProposedCall("weather", {"place": "Vancouver"})
    assert proposed_call(log.conn, "A2") == ProposedCall("my_day", None)
    assert proposed_call(log.conn, "A3") == ProposedCall("mail", "not an object")
    assert proposed_call(log.conn, "A9") is None


def test_the_result_an_action_came_back_with(tmp_path: Path) -> None:
    """The result row of that action with its log id; none while the action is still running."""
    log = TurnLog(tmp_path)
    log.put("action.result_observed", {"action_id": "A2", "semantics": "ok", "tool_output": "x"})
    assert action_result(log.conn, "A1") is None
    seen = log.put(
        "action.result_observed",
        {"action_id": "A1", "semantics": "ok", "tool_output": '{"temp": 12}'},
    )
    found = action_result(log.conn, "A1")
    assert found is not None
    row_id, event = found
    assert event.event_uid == seen.event_uid
    assert event.payload["tool_output"] == '{"temp": 12}'
    assert row_id == latest_id(log.conn)


def test_the_reminders_actions_scheduled(tmp_path: Path) -> None:
    """Each action's reminders come back as scheduled, in order, with their log ids."""
    log = TurnLog(tmp_path)
    nine = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
    ten = datetime(2026, 10, 22, 10, 0, tzinfo=UTC)
    first = reminders.schedule(log.conn, due=nine, text="stand up", action_id="A1")
    reminders.schedule(log.conn, due=ten, text="not this turn's", action_id="A9")
    second = reminders.schedule(log.conn, due=ten, text="call mum", action_id="A2")
    assert reminders.scheduled_by_actions(log.conn, []) == []
    found = reminders.scheduled_by_actions(log.conn, ["A2", "A1"])
    assert [(r.reminder_id, r.text, r.due_at_ms) for _row_id, r in found] == [
        (first, "stand up", int(nine.timestamp() * 1000)),
        (second, "call mum", int(ten.timestamp() * 1000)),
    ]
    assert found[0][0] < found[1][0]


def test_the_words_behind_an_action(tmp_path: Path) -> None:
    """The newest typed or heard row of the turn the action ran under, with its device."""
    log = TurnLog(tmp_path)
    log.heard("T0", "an earlier turn", node="iphone-2")
    assert input_behind_action(log.conn, "A1") is None  # never ran
    log.put("action.running", {"action_id": "A1"}, turn_id="T1")
    assert input_behind_action(log.conn, "A1") is None  # its turn holds no words of his
    log.put("action.running", {"action_id": "A9"}, turn_id="T9")
    log.heard("T9", "another turn")
    log.heard("T1", "connect my calendar", node="iphone-2", channel="phone_voice")
    assert input_behind_action(log.conn, "A1") == ActionInput(
        row_id=latest_id(log.conn),
        event_uid=log.conn.execute(
            "SELECT event_uid FROM events WHERE id = ?", (latest_id(log.conn),),
        ).fetchone()[0],
        payload={"transcript": "connect my calendar", "turn_id": "T1", "channel": "phone_voice"},
        ingestion_node="iphone-2",
    )
    typed = log.typed("T1", "gpt_live")
    log.put("action.running", {"action_id": "A2"}, turn_id="T1")
    log.heard("T2", "afterwards")
    found = input_behind_action(log.conn, "A2")
    assert found is not None
    assert (found.event_uid, found.ingestion_node) == (typed.event_uid, "mac")
    assert found.payload["channel"] == "gpt_live"
    assert found.row_id < latest_id(log.conn)


def test_an_action_that_ran_twice_is_read_under_its_newest_run(tmp_path: Path) -> None:
    """When a retry ran under another turn, that turn's words are the ones behind the action."""
    log = TurnLog(tmp_path)
    log.heard("T1", "first ask")
    second = log.heard("T2", "second ask")
    log.put("action.running", {"action_id": "A1"}, turn_id="T1")
    log.put("action.running", {"action_id": "A1"}, turn_id="T2")
    found = input_behind_action(log.conn, "A1")
    assert found is not None
    assert found.event_uid == second.event_uid
