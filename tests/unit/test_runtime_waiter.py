"""Unit tests for :func:`jarvis.runtime._wait_for_next_trigger` (B-0003b).

Covers the B-0003b fix that extends ``_RUNTIME_TRIGGER_TYPES`` to
include ``action.timeout_assumed`` and ``action.failed``. Without
these in the trigger set the runtime waiter never wakes after a
spawn_worker timeout / crash, so decide() is never re-entered for
the terminal-failure paths and the user sees silence.

The tests pre-emit the trigger row then call the waiter synchronously
— the SQLite poll finds the row on its first cursor pass, so no
threading or sleep timing is required.
"""

from __future__ import annotations

from contextlib import closing
from typing import TYPE_CHECKING

from jarvis.runtime import _wait_for_next_trigger
from jarvis.state.event_log import emit_event, open_event_log

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


def _seed_utterance(conn: sqlite3.Connection) -> int:
    """Append a surface.user_intent row + return its SQLite id.

    Used as the ``after_id`` anchor for the waiter so we only see
    rows that land AFTER this seed.
    """
    emit_event(
        conn,
        type="surface.user_intent",
        payload={"transcript": "seed", "turn_id": "T_seed"},
        correlation={"turn_id": "T_seed"},
    )
    cursor = conn.execute("SELECT MAX(id) FROM events")
    row = cursor.fetchone()
    return int(row[0])


def test_wait_for_next_trigger_returns_on_action_timeout_assumed(tmp_path: Path) -> None:
    """B-0003b: waiter must wake on ``action.timeout_assumed`` rows."""
    db_path = tmp_path / "events.db"
    with closing(open_event_log(db_path)) as conn:
        after_id = _seed_utterance(conn)

        # Pre-emit the timeout row so the waiter finds it on the first
        # poll pass (no threading needed — the row is already in the
        # log when the waiter executes its SELECT).
        parent = emit_event(
            conn,
            type="action.proposed",
            payload={
                "action_id": "A_to",
                "tool_name": "spawn_worker",
                "caller_principal": "jarvis_llm",
                "risk_level": "L2",
            },
        )
        emit_event(
            conn,
            type="action.timeout_assumed",
            payload={
                "action_id": "A_to",
                "error": "codex_turn_timeout",
                "reason": "codex turn timed out",
            },
            source_event_id=parent.event_uid,
            correlation={"action_id": "A_to"},
        )

        event, new_id = _wait_for_next_trigger(
            conn,
            after_id=after_id,
            action_ids=frozenset({"A_to"}),
            timeout=1.0,
            poll_interval_s=0.01,
        )

        assert event.type == "action.timeout_assumed"
        assert event.payload["action_id"] == "A_to"
        assert new_id > after_id


def test_wait_for_next_trigger_returns_on_action_failed(tmp_path: Path) -> None:
    """B-0003b: waiter must wake on ``action.failed`` rows."""
    db_path = tmp_path / "events.db"
    with closing(open_event_log(db_path)) as conn:
        after_id = _seed_utterance(conn)

        parent = emit_event(
            conn,
            type="action.proposed",
            payload={
                "action_id": "A_fail",
                "tool_name": "spawn_worker",
                "caller_principal": "jarvis_llm",
                "risk_level": "L2",
            },
        )
        emit_event(
            conn,
            type="action.failed",
            payload={
                "action_id": "A_fail",
                "error": "codex_subprocess_crashed",
                "reason": "codex subprocess exited non-zero",
            },
            source_event_id=parent.event_uid,
            correlation={"action_id": "A_fail"},
        )

        event, new_id = _wait_for_next_trigger(
            conn,
            after_id=after_id,
            action_ids=frozenset({"A_fail"}),
            timeout=1.0,
            poll_interval_s=0.01,
        )

        assert event.type == "action.failed"
        assert event.payload["action_id"] == "A_fail"
        assert new_id > after_id
