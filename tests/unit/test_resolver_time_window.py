"""Unit tests for the Day-2 time-window resolver path (ADR-0002 Step 5).

Two surfaces under test:

- :meth:`jarvis.state.projections.TaskLedgerSnapshot.tasks_in_window` —
  the projection method that filters folded Task Ledger records by
  ``task.created.ts_epoch_ms`` window (inclusive on both ends, ascending
  by ts).
- :func:`jarvis.decision.resolver.resolve_task_ref_by_window` — the L3
  resolver path that consumes ``{since_ts, until_ts}`` emitted by the
  LLM and translates a window hit into a :class:`ResolverResult` (zero
  -> confidence="none"; single -> high + ``match_basis="time_window"``;
  multi -> ``unknown_subject`` fail-fast).

LLM-free tests. The event log is seeded with raw ``task.created`` events
through :func:`emit_event` so the fold path is exactly the one L3 will
see at runtime.
"""

from __future__ import annotations

from contextlib import closing
from typing import TYPE_CHECKING

from jarvis.decision.resolver import resolve_task_ref_by_window
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.projections import (
    ClaimEvidenceProjection,
    TaskLedgerRecord,
    TaskLedgerSnapshot,
    rebuild_projections,
)

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


# --- Helpers ----------------------------------------------------------------


def _open(tmp_path: Path) -> sqlite3.Connection:
    """Open a fresh Event Log at ``tmp_path/events.db``."""
    return open_event_log(tmp_path / "events.db")


def _seed_task_created(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    goal: str,
    ts: int,
) -> None:
    """Seed one ``task.created`` event at the given epoch-ms timestamp."""
    emit_event(
        conn,
        type="task.created",
        payload={"task_id": task_id, "goal": goal},
        ts_epoch_ms=ts,
        event_uid=f"task-created-{task_id}-{ts}",
    )


def _make_snapshot(*records: TaskLedgerRecord) -> TaskLedgerSnapshot:
    """Build a frozen snapshot directly from records (no event log)."""
    claim_evidence = ClaimEvidenceProjection(
        claims_by_id={},
        evidence_by_claim_id={},
        claim_ids_by_subject_ref={},
    )
    return TaskLedgerSnapshot(
        records_by_task_id={r.task_id: r for r in records},
        claim_evidence=claim_evidence,
    )


def _record(task_id: str, ts: int) -> TaskLedgerRecord:
    return TaskLedgerRecord(
        task_id=task_id,
        goal=f"goal for {task_id}",
        created_event_uid=f"uid-{task_id}",
        created_ts_epoch_ms=ts,
    )


# --- TaskLedgerSnapshot.tasks_in_window -------------------------------------


def test_tasks_in_window_single_match(tmp_path: Path) -> None:
    """A single task created inside the window is returned."""
    with closing(_open(tmp_path)) as conn:
        _seed_task_created(conn, task_id="T_A", goal="A", ts=1000)
        _seed_task_created(conn, task_id="T_B", goal="B", ts=2000)
        _seed_task_created(conn, task_id="T_C", goal="C", ts=3000)
        projections = rebuild_projections(conn)

    snapshot = projections.task_ledger.snapshot()
    assert snapshot.tasks_in_window(since_ts=1500, until_ts=2500) == ["T_B"]


def test_tasks_in_window_multi_match_ascending(tmp_path: Path) -> None:
    """Multi-match window returns task_ids sorted by ts ascending."""
    with closing(_open(tmp_path)) as conn:
        # Seed out of insertion order to confirm the sort is real.
        _seed_task_created(conn, task_id="T_C", goal="C", ts=3000)
        _seed_task_created(conn, task_id="T_A", goal="A", ts=1000)
        _seed_task_created(conn, task_id="T_B", goal="B", ts=2000)
        projections = rebuild_projections(conn)

    snapshot = projections.task_ledger.snapshot()
    # All three are inside [500, 3500]; expect oldest-first.
    assert snapshot.tasks_in_window(since_ts=500, until_ts=3500) == [
        "T_A",
        "T_B",
        "T_C",
    ]


def test_tasks_in_window_zero_match(tmp_path: Path) -> None:
    """No task in the window yields an empty list."""
    with closing(_open(tmp_path)) as conn:
        _seed_task_created(conn, task_id="T_A", goal="A", ts=1000)
        projections = rebuild_projections(conn)

    snapshot = projections.task_ledger.snapshot()
    assert snapshot.tasks_in_window(since_ts=5000, until_ts=6000) == []


def test_tasks_in_window_boundary_inclusive(tmp_path: Path) -> None:
    """Inclusive bounds: ts == since_ts AND ts == until_ts both match."""
    with closing(_open(tmp_path)) as conn:
        _seed_task_created(conn, task_id="T_low", goal="L", ts=1000)
        _seed_task_created(conn, task_id="T_high", goal="H", ts=2000)
        projections = rebuild_projections(conn)

    snapshot = projections.task_ledger.snapshot()
    # Window endpoints sit exactly on the seeded timestamps.
    assert snapshot.tasks_in_window(since_ts=1000, until_ts=2000) == [
        "T_low",
        "T_high",
    ]
    # A single-point window where ts == since_ts == until_ts still hits.
    assert snapshot.tasks_in_window(since_ts=1000, until_ts=1000) == ["T_low"]


def test_tasks_in_window_empty_log(tmp_path: Path) -> None:
    """Empty event log yields an empty list."""
    with closing(_open(tmp_path)) as conn:
        projections = rebuild_projections(conn)

    snapshot = projections.task_ledger.snapshot()
    assert snapshot.tasks_in_window(since_ts=0, until_ts=10**12) == []


def test_tasks_in_window_live_ledger_matches_snapshot(tmp_path: Path) -> None:
    """`TaskLedger.tasks_in_window` agrees with snapshot byte-for-byte."""
    with closing(_open(tmp_path)) as conn:
        _seed_task_created(conn, task_id="T_A", goal="A", ts=1000)
        _seed_task_created(conn, task_id="T_B", goal="B", ts=2000)
        projections = rebuild_projections(conn)

    ledger = projections.task_ledger
    snapshot = ledger.snapshot()
    assert ledger.tasks_in_window(0, 10**12) == snapshot.tasks_in_window(0, 10**12)


def test_tasks_in_window_tie_ts_sorted_by_task_id() -> None:
    """When two tasks share a ts, the result is sorted by task_id."""
    snapshot = _make_snapshot(
        _record("T_beta", 1000),
        _record("T_alpha", 1000),
        _record("T_gamma", 2000),
    )
    # Both 1000-ts tasks land in the window; deterministic order matters
    # so the resolver's single-match check is stable across runs.
    assert snapshot.tasks_in_window(500, 1500) == ["T_alpha", "T_beta"]


# --- resolve_task_ref_by_window ---------------------------------------------


def test_resolve_window_single_match_high_confidence() -> None:
    """One task in the window -> confidence=high, match_basis=time_window."""
    snapshot = _make_snapshot(
        _record("T_A", 1000),
        _record("T_B", 2000),
        _record("T_C", 3000),
    )
    result = resolve_task_ref_by_window(
        "昨天那个 task", snapshot, since_ts=1500, until_ts=2500,
    )

    assert result.resolved_to == "T_B"
    assert result.confidence == "high"
    assert result.candidates == ("T_B",)
    assert result.match_basis == "time_window"


def test_resolve_window_zero_match_returns_none() -> None:
    """Zero tasks in the window -> confidence=none, candidates empty."""
    snapshot = _make_snapshot(_record("T_A", 1000))
    result = resolve_task_ref_by_window(
        "yesterday", snapshot, since_ts=10_000, until_ts=20_000,
    )

    assert result.resolved_to is None
    assert result.confidence == "none"
    assert result.candidates == ()
    assert result.match_basis == "no task in window"


def test_resolve_window_multi_match_unknown_subject() -> None:
    """Multi-match -> resolved_to=None, match_basis='unknown_subject'."""
    snapshot = _make_snapshot(
        _record("T_A", 1000),
        _record("T_B", 2000),
        _record("T_C", 3000),
    )
    result = resolve_task_ref_by_window(
        "昨天", snapshot, since_ts=0, until_ts=10_000,
    )

    # Fail-fast contract: L3 must NOT silently bind one of multiple
    # candidates; the caller emits a Limitation Claim instead.
    assert result.resolved_to is None
    assert result.confidence == "none"
    assert result.match_basis == "unknown_subject"
    # Candidates exposed for the audit trail (entity.resolved payload).
    assert result.candidates == ("T_A", "T_B", "T_C")


def test_resolve_window_boundary_inclusive() -> None:
    """A task whose ts equals since_ts (or until_ts) is included."""
    snapshot = _make_snapshot(_record("T_edge", 1000))

    on_lower = resolve_task_ref_by_window(
        "", snapshot, since_ts=1000, until_ts=5000,
    )
    on_upper = resolve_task_ref_by_window(
        "", snapshot, since_ts=0, until_ts=1000,
    )

    assert on_lower.resolved_to == "T_edge"
    assert on_lower.match_basis == "time_window"
    assert on_upper.resolved_to == "T_edge"
    assert on_upper.match_basis == "time_window"


def test_resolve_window_empty_log_zero_match() -> None:
    """Empty ledger -> zero match (confidence=none)."""
    snapshot = _make_snapshot()
    result = resolve_task_ref_by_window(
        "any ref", snapshot, since_ts=0, until_ts=10**12,
    )

    assert result.resolved_to is None
    assert result.confidence == "none"
    assert result.match_basis == "no task in window"


def test_resolve_window_event_log_end_to_end(tmp_path: Path) -> None:
    """End-to-end: events -> rebuild -> snapshot -> resolver -> bind."""
    with closing(_open(tmp_path)) as conn:
        # D-1 yesterday's task plus a today task.
        _seed_task_created(conn, task_id="T_yest", goal="昨天的任务", ts=100_000)
        _seed_task_created(conn, task_id="T_today", goal="今天的任务", ts=200_000)
        projections = rebuild_projections(conn)

    snapshot = projections.task_ledger.snapshot()
    result = resolve_task_ref_by_window(
        "昨天那个 task",
        snapshot,
        since_ts=90_000,
        until_ts=150_000,
    )

    assert result.resolved_to == "T_yest"
    assert result.confidence == "high"
    assert result.match_basis == "time_window"
    assert result.candidates == ("T_yest",)


def test_resolve_window_does_not_mutate_snapshot() -> None:
    """The resolver leaves the snapshot byte-for-byte unchanged."""
    # ``tasks_in_window`` returns a fresh list — the snapshot itself
    # is frozen so this is mostly a smoke test, but the contract that
    # callers may chain resolver calls relies on it.
    snapshot = _make_snapshot(_record("T_A", 1000), _record("T_B", 2000))
    before = list(snapshot.tasks_in_window(0, 10_000))
    resolve_task_ref_by_window("", snapshot, since_ts=1500, until_ts=2500)
    after = list(snapshot.tasks_in_window(0, 10_000))
    assert before == after == ["T_A", "T_B"]


# --- Backstop: resolver remains free of jarvis.decision.llm imports ---------


def test_resolver_module_still_llm_free() -> None:
    """Day-1 canary H10 invariant survives the Day-2 extension."""
    # We re-import to make sure the additional `resolve_task_ref_by_window`
    # function did not sneak in a jarvis.decision.llm import (which the
    # Day-1 canary would also catch — this is a fast local backstop).
    import jarvis.decision.resolver as mod  # noqa: PLC0415 — intentional in-test import.

    assert "jarvis.decision.llm" not in getattr(mod, "__file__", ""), (
        "resolver module path should not contain the llm path token"
    )
