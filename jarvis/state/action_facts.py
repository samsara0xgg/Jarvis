"""What the log says about one action: the call that was proposed and the result that came back.

ADR 0349. An action is named by its ``action_id``; the proposal and the result are the rows
``action.proposed`` and ``action.result_observed`` that carry it in their payload.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jarvis.state.event_log import EVENT_COLUMNS, row_to_event

if TYPE_CHECKING:
    import sqlite3

    from jarvis.shared import Event


@dataclass(frozen=True)
class ProposedCall:
    """The call an action asked for: the tool, and the arguments as the proposer sent them."""

    tool_name: str
    arguments: Any


def proposed_call(conn: sqlite3.Connection, action_id: str) -> ProposedCall | None:
    """The tool call the action proposed, or ``None`` if no ``action.proposed`` names it."""
    row = conn.execute(
        "SELECT payload_json FROM events WHERE type = 'action.proposed' "
        "AND json_extract(payload_json, '$.action_id') = ? LIMIT 1",
        (action_id,),
    ).fetchone()
    if row is None:
        return None
    payload = json.loads(row[0])
    return ProposedCall(str(payload.get("tool_name") or ""), payload.get("arguments"))


def action_result(conn: sqlite3.Connection, action_id: str) -> tuple[int, Event] | None:
    """``(log row id, event)`` of the action's ``action.result_observed``, ``None`` if none yet."""
    row = conn.execute(
        f"SELECT {EVENT_COLUMNS} FROM events "  # noqa: S608 - the columns are a constant.
        "WHERE type = 'action.result_observed' "
        "AND json_extract(payload_json, '$.action_id') = ?",
        (action_id,),
    ).fetchone()
    return None if row is None else (int(row[0]), row_to_event(row))


@dataclass(frozen=True)
class ActionInput:
    """The row of his words behind an action: log id, uid, payload and the device that wrote it."""

    row_id: int
    event_uid: str
    payload: dict[str, Any]
    ingestion_node: str


def input_behind_action(conn: sqlite3.Connection, action_id: str) -> ActionInput | None:
    """The newest row of his words (typed or heard) in the turn the action ran under.

    The turn is the one the action's newest ``action.running`` is correlated to; an action that
    never ran is looked up under no turn at all. ``None`` when that turn holds no such row.
    """
    running = conn.execute(
        "SELECT correlation_id FROM events WHERE json_extract(payload_json, '$.action_id') = ? "
        "AND type = 'action.running' ORDER BY id DESC LIMIT 1",
        (action_id,),
    ).fetchone()
    row = conn.execute(
        "SELECT id, event_uid, payload_json, ingestion_node FROM events "
        "WHERE type IN ('surface.user_intent', 'utterance.received') "
        "AND correlation_id = ? ORDER BY id DESC LIMIT 1",
        (running[0] if running else "",),
    ).fetchone()
    if row is None:
        return None
    return ActionInput(int(row[0]), str(row[1]), json.loads(row[2]), str(row[3]))
