"""ADR-0008 D10 — gated action cancellation (docs/goals/gated-action-cancel.md).

The ``action:`` EntityRegistry kind and its admission lookup (L2), the
Pre-action Gate's ``cancel_action`` arm (L3), the ``cancel_action`` tool
(L4), and the resolved / ambiguous / none answers — the latter driven
through the real ``decide()`` with a scripted LLM and a real ActionRunner.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from jarvis.state.decision_snapshot import read_decision_snapshot
from jarvis.state.event_log import emit_event, open_event_log

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from jarvis.shared import Event
    from jarvis.state.projections import ProjectionSet

_ACTION_TERMINALS: tuple[str, ...] = (
    "action.result_observed",
    "action.failed",
    "action.timeout_assumed",
    "action.cancelled",
)
"""The four canonical action terminals; each must evict the `action:` entry."""


# --- helpers -----------------------------------------------------------------


def _admit(
    conn: sqlite3.Connection,
    action_id: str,
    *,
    lease_id: str | None = None,
    outcome: str = "pass",
) -> tuple[Event, Event]:
    """Emit the pre-action verdict and the dispatch that open one action."""
    gate_payload: dict[str, object] = {
        "gate": "pre_action",
        "outcome": outcome,
        "reasons": [],
        "action_id": action_id,
    }
    if lease_id is not None:
        gate_payload["lease_id"] = lease_id
    gate = emit_event(conn, type="gate.evaluated", payload=gate_payload)
    dispatched = emit_event(
        conn,
        type="action.dispatched",
        payload={"action_id": action_id},
        correlation={"action_id": action_id},
    )
    return gate, dispatched


def _terminal_payload(event_type: str, action_id: str) -> dict[str, object]:
    """The minimal registry-valid payload for one action terminal."""
    payload: dict[str, object] = {"action_id": action_id}
    if event_type == "action.result_observed":
        payload["semantics"] = "ack"
    return payload


def _projections(conn: sqlite3.Connection) -> ProjectionSet:
    """Fold the whole log the way `assemble_packet` does."""
    return read_decision_snapshot(conn).projections


# --- (8) the `action:` fold and its admission lookup --------------------------


def test_action_dispatched_registers_the_entity_and_its_admission(tmp_path: Path) -> None:
    """`action.dispatched` opens an exact `action:` entry keyed to its passing gate."""
    conn = open_event_log(tmp_path / "events.db")
    try:
        gate, dispatched = _admit(conn, "A1")
        projections = _projections(conn)

        entry = projections.entity_registry.get("action:A1")
        assert entry is not None
        assert entry.entity_type == "action"
        assert entry.canonical == "A1"
        assert entry.aliases == ()
        assert entry.confidence == "exact"
        assert entry.source_event_id == dispatched.event_uid
        assert entry.last_seen_ms == dispatched.ts_epoch_ms

        admission = projections.action_admissions.get("A1")
        assert admission is not None
        assert admission.dispatched_event_uid == dispatched.event_uid
        assert admission.admission_gate_uid == gate.event_uid
        assert admission.lease_id is None
        assert admission.run_id is None
    finally:
        conn.close()


def test_a_leased_admission_carries_its_lease_id(tmp_path: Path) -> None:
    """D10's leased case: the `lease_id` on the passing verdict rides along."""
    conn = open_event_log(tmp_path / "events.db")
    try:
        _admit(conn, "A1", lease_id="lease-1")
        admission = _projections(conn).action_admissions.get("A1")
        assert admission is not None
        assert admission.lease_id == "lease-1"
    finally:
        conn.close()


def test_only_a_passing_pre_action_verdict_counts_as_the_admission(tmp_path: Path) -> None:
    """A dispatch whose last verdict refused (or that had none) has no gate uid."""
    conn = open_event_log(tmp_path / "events.db")
    try:
        _admit(conn, "A-refused", outcome="refuse")
        emit_event(conn, type="action.dispatched", payload={"action_id": "A-ungated"})
        admissions = _projections(conn).action_admissions
        refused = admissions.get("A-refused")
        ungated = admissions.get("A-ungated")
        assert refused is not None
        assert refused.admission_gate_uid is None
        assert ungated is not None
        assert ungated.admission_gate_uid is None
    finally:
        conn.close()


def test_run_id_joins_only_through_run_started(tmp_path: Path) -> None:
    """`run.started` joins by its source (the `action.running` uid) or correlation.

    `action.running` carries no `run_id` and is never read for one.
    """
    conn = open_event_log(tmp_path / "events.db")
    try:
        _, dispatched = _admit(conn, "A1")
        running = emit_event(
            conn,
            type="action.running",
            payload={"action_id": "A1"},
            source_event_id=dispatched.event_uid,
        )
        assert _projections(conn).action_admissions.get("A1").run_id is None  # type: ignore[union-attr]
        emit_event(
            conn,
            type="run.started",
            payload={"run_id": "R1", "task_id": "T1", "runner": "fixture"},
            source_event_id=running.event_uid,
        )
        _admit(conn, "A2")
        emit_event(
            conn,
            type="run.started",
            payload={"run_id": "R2", "task_id": "T2", "runner": "fixture"},
            correlation={"action_id": "A2"},
        )
        admissions = _projections(conn).action_admissions
        assert admissions.get("A1").run_id == "R1"  # type: ignore[union-attr]
        assert admissions.get("A2").run_id == "R2"  # type: ignore[union-attr]
    finally:
        conn.close()


@pytest.mark.parametrize("terminal", _ACTION_TERMINALS)
def test_each_action_terminal_evicts_the_entry(tmp_path: Path, terminal: str) -> None:
    """The `action:` universe is exactly the non-terminal set."""
    conn = open_event_log(tmp_path / "events.db")
    try:
        _admit(conn, "A1")
        _admit(conn, "A2")
        assert "action:A1" in _projections(conn).entity_registry
        emit_event(conn, type=terminal, payload=_terminal_payload(terminal, "A1"))
        projections = _projections(conn)
        assert "action:A1" not in projections.entity_registry
        assert projections.action_admissions.get("A1") is None
        # The sibling is untouched.
        assert "action:A2" in projections.entity_registry
        assert projections.action_admissions.get("A2") is not None
    finally:
        conn.close()
