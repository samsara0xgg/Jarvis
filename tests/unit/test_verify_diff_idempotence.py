"""Tier-1 unit: the decision loop's redundant-verify_diff idempotence signal.

Root cause (live-burned 2026-05-30): on the reviewer-fail / no-verify_command
path the untrusted Tier-2 LLM (spec §3.4.6) re-proposed ``verify_diff`` for the
same task within one turn, producing two ``observation`` slots and — on a
``verify_command`` path — risking a duplicate ``task.verified``. Spec
§3.4.3/§3.4.4 say the decision engine chooses the next action from current
state (``open_actions`` / ``missing_evidence`` are in the Situation Packet), so
a second verify_diff for an already-verify-proposed task this turn is redundant
and must be refused deterministically.

These tests pin the pure detection helper that backs that gate refusal. They
are LLM-free and Codex-free (Tier 1): they seed ``action.proposed`` rows
directly and assert the signal, so the guard logic is verifiable without a live
burn.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from jarvis.decision import _verify_diff_already_proposed_this_turn
from jarvis.state.event_log import emit_event, open_event_log

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


def _propose(
    conn: sqlite3.Connection,
    *,
    action_id: str,
    tool_name: str,
    task_id: str,
    turn_id: str,
) -> None:
    """Emit a minimal ``action.proposed`` mirroring the real decide() payload."""
    emit_event(
        conn,
        type="action.proposed",
        payload={
            "action_id": action_id,
            "tool_name": tool_name,
            "caller_principal": "jarvis_llm",
            "risk_level": "L0",
            "target_entity_ref": task_id,
            "turn_id": turn_id,
            "arguments": {},
        },
        correlation={"turn_id": turn_id},
    )


def test_no_prior_proposal_returns_false(tmp_path: Path) -> None:
    """A fresh log has no prior verify_diff → not redundant."""
    conn = open_event_log(tmp_path / "events.db")
    try:
        assert not _verify_diff_already_proposed_this_turn(
            conn, turn_id="T1", task_id="task_X",
        )
    finally:
        conn.close()


def test_prior_verify_diff_same_turn_same_task_returns_true(tmp_path: Path) -> None:
    """A verify_diff already proposed for (task, turn) → redundant."""
    conn = open_event_log(tmp_path / "events.db")
    try:
        _propose(conn, action_id="A1", tool_name="verify_diff", task_id="task_X", turn_id="T1")
        assert _verify_diff_already_proposed_this_turn(
            conn, turn_id="T1", task_id="task_X",
        )
    finally:
        conn.close()


def test_prior_verify_diff_different_turn_returns_false(tmp_path: Path) -> None:
    """The signal is turn-scoped — a verify_diff in another turn does not count."""
    conn = open_event_log(tmp_path / "events.db")
    try:
        _propose(conn, action_id="A1", tool_name="verify_diff", task_id="task_X", turn_id="T1")
        assert not _verify_diff_already_proposed_this_turn(
            conn, turn_id="T2", task_id="task_X",
        )
    finally:
        conn.close()


def test_prior_verify_diff_different_task_returns_false(tmp_path: Path) -> None:
    """The signal is task-scoped — a verify_diff for another task does not count."""
    conn = open_event_log(tmp_path / "events.db")
    try:
        _propose(conn, action_id="A1", tool_name="verify_diff", task_id="task_X", turn_id="T1")
        assert not _verify_diff_already_proposed_this_turn(
            conn, turn_id="T1", task_id="task_Y",
        )
    finally:
        conn.close()


def test_prior_spawn_worker_does_not_count(tmp_path: Path) -> None:
    """Only verify_diff proposals count — a spawn_worker for the task does not."""
    conn = open_event_log(tmp_path / "events.db")
    try:
        _propose(conn, action_id="A1", tool_name="spawn_worker", task_id="task_X", turn_id="T1")
        assert not _verify_diff_already_proposed_this_turn(
            conn, turn_id="T1", task_id="task_X",
        )
    finally:
        conn.close()


def test_none_turn_or_task_returns_false(tmp_path: Path) -> None:
    """Missing turn_id / task_id is treated as not-redundant (fail open to one run)."""
    conn = open_event_log(tmp_path / "events.db")
    try:
        _propose(conn, action_id="A1", tool_name="verify_diff", task_id="task_X", turn_id="T1")
        assert not _verify_diff_already_proposed_this_turn(conn, turn_id=None, task_id="task_X")
        assert not _verify_diff_already_proposed_this_turn(conn, turn_id="T1", task_id=None)
    finally:
        conn.close()
