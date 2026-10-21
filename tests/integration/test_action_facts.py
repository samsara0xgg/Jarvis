"""What the Event Log says about one action: its proposal, its result and the reminders it set.

ADR 0349. A real on-disk Event Log; the rows are the ones L4 writes for a tool call.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from jarvis.state import reminders
from jarvis.state.action_facts import ProposedCall, action_result, proposed_call
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
