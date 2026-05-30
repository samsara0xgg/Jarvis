"""Unit tests for `jarvis.state.projections` (ADR § Tier 1, Step 5).

Covers ADR § Acceptance D + E + F's projection asserts:

- Seed events → expected Task Ledger state.
- `derive_status(task_id)` returns `open` / `reported_complete` /
  `verified_complete` for the three fold patterns.
- `rebuild_projections(conn)` is idempotent: re-fold from the same
  event log yields a deep-equal `ProjectionSet`.
- `RecentTrace` ring buffer bounds size and preserves event order.
- `ClaimEvidenceProjection.strongest_level_for` returns the ladder max.
- `TaskLedgerSnapshot.open_tasks()` is read-only (mutating the returned
  tuple does not affect the projection).

Each test seeds inputs through `emit_event` against a `tmp_path` SQLite
database — exactly the same surface L3 sees Day-1, so the tests double
as a small integration check between Step 4 and Step 5.
"""

from __future__ import annotations

from contextlib import closing
from typing import TYPE_CHECKING

import pytest

from jarvis.state.event_log import emit_event, iter_events, open_event_log
from jarvis.state.projections import (
    ClaimEvidenceProjection,
    ProjectionSet,
    RecentTrace,
    TaskLedger,
    TaskLedgerRecord,
    TaskLedgerSnapshot,
    rebuild_projections,
)

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


# --- Helpers ----------------------------------------------------------------


def _open(tmp_path: Path) -> sqlite3.Connection:
    """Open a fresh Event Log at `tmp_path/mac_events.db`."""
    return open_event_log(tmp_path / "mac_events.db")


def _seed_open_task(conn: sqlite3.Connection, ts: int = 1) -> None:
    """Seed a single `task.created` event for `task_X`."""
    emit_event(
        conn,
        type="task.created",
        payload={"task_id": "task_X", "goal": "ship Day-1"},
        ts_epoch_ms=ts,
        event_uid=f"task-created-{ts}",
    )


def _seed_reported_task(conn: sqlite3.Connection, base_ts: int = 1) -> None:
    """Seed `task.created` + `run.started` + `worker.reported` for `task_X`."""
    _seed_open_task(conn, ts=base_ts)
    emit_event(
        conn,
        type="run.started",
        payload={"run_id": "R1", "task_id": "task_X"},
        ts_epoch_ms=base_ts + 1,
        event_uid=f"run-started-{base_ts}",
    )
    emit_event(
        conn,
        type="worker.reported",
        payload={
            "run_id": "R1",
            "action_id": "A1",
            "status": "reported_complete",
            "summary": "stub",
        },
        ts_epoch_ms=base_ts + 2,
        event_uid=f"worker-reported-{base_ts}",
    )


def _seed_verified_task(conn: sqlite3.Connection, base_ts: int = 1) -> None:
    """Seed full happy-path trace for `task_X` ending in `task.verified`.

    Mirrors the canonical trace evt 09 → evt 22 minimum subset needed to
    derive `verified_complete`: run.started + worker.reported + claim
    (Postcondition) + evidence (verified) + task.verified.
    """
    _seed_reported_task(conn, base_ts=base_ts)
    # Claim of type Postcondition with subject_ref = task_X.
    emit_event(
        conn,
        type="claim.created",
        payload={
            "claim_id": "C2",
            "type": "Postcondition",
            "statement": "diff predicate satisfied",
            "subject_ref": "task_X",
            "produced_by_event_id": f"worker-reported-{base_ts}",
        },
        ts_epoch_ms=base_ts + 3,
        event_uid=f"claim-{base_ts}",
    )
    emit_event(
        conn,
        type="evidence.attached",
        payload={
            "evidence_id": "E2",
            "claim_id": "C2",
            "relation": "supports",
            "level": "verified",
            "artifact_path": "/tmp/diff.json",
        },
        ts_epoch_ms=base_ts + 4,
        event_uid=f"evidence-{base_ts}",
    )
    emit_event(
        conn,
        type="task.verified",
        payload={"task_id": "task_X", "by": "jarvis"},
        ts_epoch_ms=base_ts + 5,
        event_uid=f"task-verified-{base_ts}",
    )


# --- Task Ledger fold tests --------------------------------------------------


def test_task_ledger_from_events_seeds_record_from_task_created(tmp_path: Path) -> None:
    """A single `task.created` event creates one TaskLedgerRecord."""
    with closing(_open(tmp_path)) as conn:
        _seed_open_task(conn)
        events = list(iter_events(conn))

    ledger = TaskLedger.from_events(events)
    record = ledger.get("task_X")

    assert record is not None
    assert isinstance(record, TaskLedgerRecord)
    assert record.task_id == "task_X"
    assert record.goal == "ship Day-1"
    assert record.created_ts_epoch_ms == 1
    assert record.run_ids == ()
    assert record.action_ids == ()
    assert record.worker_reported_statuses == ()
    assert record.task_verified_event_uids == ()


def test_derive_status_open_for_freshly_created_task(tmp_path: Path) -> None:
    """`derive_status` returns `open` for a task with no progress events."""
    with closing(_open(tmp_path)) as conn:
        _seed_open_task(conn)
        ps = rebuild_projections(conn)

    assert ps.task_ledger.derive_status("task_X") == "open"


def test_derive_status_reported_complete_after_worker_report(tmp_path: Path) -> None:
    """`derive_status` returns `reported_complete` after `worker.reported`.

    Per ADR § Acceptance D1 (I8) + F: agent self-report is not
    verification; the negative-path scenario uses `reported_complete` as
    the terminal status when no Postcondition Claim with
    `evidence.level=verified` exists.
    """
    with closing(_open(tmp_path)) as conn:
        _seed_reported_task(conn)
        ps = rebuild_projections(conn)

    assert ps.task_ledger.derive_status("task_X") == "reported_complete"
    record = ps.task_ledger.get("task_X")
    assert record is not None
    assert record.run_ids == ("R1",)
    assert record.action_ids == ("A1",)
    assert record.worker_reported_statuses == ("reported_complete",)


def test_derive_status_verified_complete_full_trace(tmp_path: Path) -> None:
    """`derive_status` returns `verified_complete` for the full happy path.

    Requires BOTH `task.verified` event AND a Postcondition Claim with
    `evidence.level=verified` for the same `subject_ref` — neither alone
    is sufficient (ADR § Acceptance D1 + D3).
    """
    with closing(_open(tmp_path)) as conn:
        _seed_verified_task(conn)
        ps = rebuild_projections(conn)

    assert ps.task_ledger.derive_status("task_X") == "verified_complete"


def test_derive_status_not_verified_without_postcondition_evidence(tmp_path: Path) -> None:
    """`task.verified` alone does not promote to `verified_complete`.

    Catches a regression where the fold treats `task.verified` as the
    sole signal — that would let an agent self-promote past the gate
    (violates I10). The required pair is `task.verified` event AND a
    Postcondition Claim with `evidence.level=verified`.
    """
    with closing(_open(tmp_path)) as conn:
        _seed_reported_task(conn)
        # `task.verified` is appended with NO Postcondition Claim
        # supporting it. Per ADR § Acceptance D3 the projection must
        # still downgrade to `reported_complete`.
        emit_event(
            conn,
            type="task.verified",
            payload={"task_id": "task_X", "by": "jarvis"},
            ts_epoch_ms=99,
            event_uid="task-verified-unsupported",
        )
        ps = rebuild_projections(conn)

    assert ps.task_ledger.derive_status("task_X") == "reported_complete"


def test_derive_status_not_verified_without_task_verified_event(tmp_path: Path) -> None:
    """A Postcondition Claim with verified evidence alone is not enough.

    Catches the symmetric regression to the prior test: a Result
    Interpreter that emits a Postcondition Claim but a downstream bug
    fails to append `task.verified` must surface as `reported_complete`,
    not `verified_complete`.
    """
    with closing(_open(tmp_path)) as conn:
        _seed_reported_task(conn)
        emit_event(
            conn,
            type="claim.created",
            payload={
                "claim_id": "C2",
                "type": "Postcondition",
                "statement": "diff predicate satisfied",
                "subject_ref": "task_X",
                "produced_by_event_id": "worker-reported-1",
            },
            ts_epoch_ms=10,
            event_uid="claim-postcondition",
        )
        emit_event(
            conn,
            type="evidence.attached",
            payload={
                "evidence_id": "E2",
                "claim_id": "C2",
                "relation": "supports",
                "level": "verified",
            },
            ts_epoch_ms=11,
            event_uid="evidence-verified",
        )
        ps = rebuild_projections(conn)

    assert ps.task_ledger.derive_status("task_X") == "reported_complete"


def test_derive_status_unknown_task_id_is_open(tmp_path: Path) -> None:
    """Unknown `task_id` returns `open` — fold has no record to consult."""
    with closing(_open(tmp_path)) as conn:
        _seed_open_task(conn)
        ps = rebuild_projections(conn)

    assert ps.task_ledger.derive_status("task_does_not_exist") == "open"


def test_task_ledger_record_has_no_status_field() -> None:
    """`TaskLedgerRecord` carries no `status` attribute (ADR § D4).

    Status is computed via `derive_status`, never stored. Catches a
    regression where someone adds a `status: TaskStatus` field — canary
    H11 (Step 11) does the AST scan; this test does the structural
    check.
    """
    fields = TaskLedgerRecord.__dataclass_fields__
    assert "status" not in fields
    assert "derived_status" not in fields


# --- Open-task projection / Resolver-facing surface --------------------------


def test_open_tasks_excludes_reported_and_verified(tmp_path: Path) -> None:
    """`open_tasks()` filters out tasks past `open`."""
    with closing(_open(tmp_path)) as conn:
        # task_A: just open.
        emit_event(
            conn,
            type="task.created",
            payload={"task_id": "task_A", "goal": "G_A"},
            ts_epoch_ms=1,
            event_uid="open-A",
        )
        # task_B: reported_complete.
        emit_event(
            conn,
            type="task.created",
            payload={"task_id": "task_B", "goal": "G_B"},
            ts_epoch_ms=2,
            event_uid="open-B",
        )
        emit_event(
            conn,
            type="run.started",
            payload={"run_id": "R_B", "task_id": "task_B"},
            ts_epoch_ms=3,
            event_uid="run-B",
        )
        emit_event(
            conn,
            type="worker.reported",
            payload={"run_id": "R_B", "action_id": "A_B", "status": "reported_complete"},
            ts_epoch_ms=4,
            event_uid="rep-B",
        )
        ps = rebuild_projections(conn)

    open_tasks = ps.task_ledger.open_tasks()
    open_task_ids = {t.task_id for t in open_tasks}
    assert open_task_ids == {"task_A"}


def test_snapshot_open_tasks_is_read_only(tmp_path: Path) -> None:
    """Mutating `snapshot.open_tasks()` result does not mutate projection.

    Asserts the returned tuple is detached: even if a caller binds a
    list or rebinds the tuple, the snapshot's internal record dict is
    untouched.
    """
    with closing(_open(tmp_path)) as conn:
        _seed_open_task(conn)
        ps = rebuild_projections(conn)

    snapshot = ps.task_ledger.snapshot()
    assert isinstance(snapshot, TaskLedgerSnapshot)

    open_first = snapshot.open_tasks()
    assert isinstance(open_first, tuple)
    assert len(open_first) == 1

    # Tuples are immutable in Python — assert that by attempting and
    # catching, instead of mutating in place.
    with pytest.raises(TypeError):
        open_first[0] = None  # type: ignore[index]

    # Snapshot is unchanged.
    open_after = snapshot.open_tasks()
    assert open_after == open_first
    record = snapshot.get("task_X")
    assert record is not None
    assert record.task_id == "task_X"


def test_snapshot_derive_status_matches_ledger(tmp_path: Path) -> None:
    """`snapshot.derive_status` and `ledger.derive_status` agree."""
    with closing(_open(tmp_path)) as conn:
        _seed_verified_task(conn)
        ps = rebuild_projections(conn)

    snapshot = ps.task_ledger.snapshot()
    assert snapshot.derive_status("task_X") == ps.task_ledger.derive_status("task_X")
    assert snapshot.derive_status("task_X") == "verified_complete"


# --- Recent Trace tests -----------------------------------------------------


def test_recent_trace_preserves_order_within_bound(tmp_path: Path) -> None:
    """RecentTrace yields events in append order when under the limit."""
    with closing(_open(tmp_path)) as conn:
        _seed_reported_task(conn)
        events = list(iter_events(conn))

    trace = RecentTrace.from_events(events, max_size=10)
    seen = list(trace.iter())

    # Same length, same order.
    assert seen == events
    assert trace.max_size == 10


def test_recent_trace_bounds_size_drops_oldest(tmp_path: Path) -> None:
    """RecentTrace truncates to `max_size`, dropping the oldest events.

    Ring-buffer semantics: when input length > max_size, the result
    contains the most recent `max_size` events in append order.
    """
    with closing(_open(tmp_path)) as conn:
        # 5 events.
        for i in range(5):
            emit_event(
                conn,
                type="turn.started",
                payload={"turn_id": f"T{i}"},
                ts_epoch_ms=i,
                event_uid=f"turn-{i}",
            )
        events = list(iter_events(conn))

    trace = RecentTrace.from_events(events, max_size=3)
    seen = list(trace.iter())

    assert len(seen) == 3
    # Most-recent 3, oldest-first within the window.
    assert [e.event_uid for e in seen] == ["turn-2", "turn-3", "turn-4"]


def test_recent_trace_default_size_is_200(tmp_path: Path) -> None:
    """Default RecentTrace size is 200 per ADR § Module map / spec §6."""
    with closing(_open(tmp_path)) as conn:
        emit_event(
            conn,
            type="turn.started",
            payload={"turn_id": "T1"},
            ts_epoch_ms=1,
            event_uid="t1",
        )
        ps = rebuild_projections(conn)

    assert ps.recent_trace.max_size == 200


def test_recent_trace_zero_max_size_yields_empty(tmp_path: Path) -> None:
    """`max_size=0` produces an empty trace regardless of input."""
    with closing(_open(tmp_path)) as conn:
        _seed_reported_task(conn)
        events = list(iter_events(conn))

    trace = RecentTrace.from_events(events, max_size=0)
    assert list(trace.iter()) == []
    assert trace.max_size == 0


def test_recent_trace_negative_max_size_raises() -> None:
    """`max_size < 0` is a caller error and raises ValueError."""
    with pytest.raises(ValueError, match="max_size must be non-negative"):
        RecentTrace.from_events([], max_size=-1)


# --- Claim/Evidence projection tests ----------------------------------------


def test_claim_evidence_projection_indexes_by_subject_ref(tmp_path: Path) -> None:
    """`claims_for(subject_ref)` returns claims keyed to that subject."""
    with closing(_open(tmp_path)) as conn:
        _seed_verified_task(conn)
        ps = rebuild_projections(conn)

    claims = ps.claim_evidence.claims_for("task_X")
    assert len(claims) == 1
    assert claims[0].claim_id == "C2"
    assert claims[0].type == "Postcondition"
    assert claims[0].subject_ref == "task_X"


def test_evidence_for_returns_attached_evidence(tmp_path: Path) -> None:
    """`evidence_for(claim_id)` returns evidence attached to the claim."""
    with closing(_open(tmp_path)) as conn:
        _seed_verified_task(conn)
        ps = rebuild_projections(conn)

    evidence = ps.claim_evidence.evidence_for("C2")
    assert len(evidence) == 1
    assert evidence[0].evidence_id == "E2"
    assert evidence[0].level == "verified"
    assert evidence[0].claim_id == "C2"


def test_strongest_level_for_returns_ladder_max(tmp_path: Path) -> None:
    """`strongest_level_for` returns the max evidence level for a subject.

    Seeds two claims on the same subject — one with `reported`
    evidence, one with `verified` evidence — and asserts the projection
    returns `verified` (the ladder max).
    """
    with closing(_open(tmp_path)) as conn:
        _seed_open_task(conn)
        # Report Claim (low evidence level).
        emit_event(
            conn,
            type="claim.created",
            payload={
                "claim_id": "C1",
                "type": "Report",
                "statement": "agent reported done",
                "subject_ref": "task_X",
                "produced_by_event_id": "task-created-1",
            },
            ts_epoch_ms=2,
            event_uid="claim-report",
        )
        emit_event(
            conn,
            type="evidence.attached",
            payload={
                "evidence_id": "E1",
                "claim_id": "C1",
                "relation": "supports",
                "level": "reported",
            },
            ts_epoch_ms=3,
            event_uid="evidence-reported",
        )
        # Postcondition Claim (higher evidence level).
        emit_event(
            conn,
            type="claim.created",
            payload={
                "claim_id": "C2",
                "type": "Postcondition",
                "statement": "predicate satisfied",
                "subject_ref": "task_X",
                "produced_by_event_id": "task-created-1",
            },
            ts_epoch_ms=4,
            event_uid="claim-postcondition-2",
        )
        emit_event(
            conn,
            type="evidence.attached",
            payload={
                "evidence_id": "E2",
                "claim_id": "C2",
                "relation": "supports",
                "level": "verified",
            },
            ts_epoch_ms=5,
            event_uid="evidence-verified-2",
        )
        ps = rebuild_projections(conn)

    assert ps.claim_evidence.strongest_level_for("task_X") == "verified"


def test_strongest_level_for_unknown_subject_returns_none(tmp_path: Path) -> None:
    """`strongest_level_for(unknown)` returns None when no evidence exists."""
    with closing(_open(tmp_path)) as conn:
        _seed_open_task(conn)
        ps = rebuild_projections(conn)

    assert ps.claim_evidence.strongest_level_for("task_does_not_exist") is None


def test_strongest_level_for_claim_without_evidence_returns_none(tmp_path: Path) -> None:
    """A claim with no `evidence.attached` returns None for its subject."""
    with closing(_open(tmp_path)) as conn:
        _seed_open_task(conn)
        emit_event(
            conn,
            type="claim.created",
            payload={
                "claim_id": "C1",
                "type": "Report",
                "statement": "stub",
                "subject_ref": "task_X",
                "produced_by_event_id": "task-created-1",
            },
            ts_epoch_ms=2,
            event_uid="claim-only",
        )
        ps = rebuild_projections(conn)

    assert ps.claim_evidence.strongest_level_for("task_X") is None


def test_claim_evidence_projection_carries_optional_payload(tmp_path: Path) -> None:
    """Optional evidence payload fields land in `Evidence.payload`."""
    with closing(_open(tmp_path)) as conn:
        _seed_open_task(conn)
        emit_event(
            conn,
            type="claim.created",
            payload={
                "claim_id": "C2",
                "type": "Postcondition",
                "statement": "stub",
                "subject_ref": "task_X",
                "produced_by_event_id": "task-created-1",
            },
            ts_epoch_ms=2,
            event_uid="claim-payload",
        )
        emit_event(
            conn,
            type="evidence.attached",
            payload={
                "evidence_id": "E2",
                "claim_id": "C2",
                "relation": "supports",
                "level": "verified",
                "artifact_path": "/tmp/diff.json",
                "content_hash": "deadbeef",
            },
            ts_epoch_ms=3,
            event_uid="evidence-payload",
        )
        ps = rebuild_projections(conn)

    evidence = ps.claim_evidence.evidence_for("C2")
    assert len(evidence) == 1
    assert evidence[0].payload["artifact_path"] == "/tmp/diff.json"
    assert evidence[0].payload["content_hash"] == "deadbeef"


# --- Rebuild idempotency tests ----------------------------------------------


def test_rebuild_projections_is_idempotent(tmp_path: Path) -> None:
    """Two `rebuild_projections` calls produce deep-equal results.

    ADR § Acceptance D5 + E4: dropping the projection cache and
    re-folding from events produces identical state.
    """
    with closing(_open(tmp_path)) as conn:
        _seed_verified_task(conn)
        ps1 = rebuild_projections(conn)
        ps2 = rebuild_projections(conn)

    assert isinstance(ps1, ProjectionSet)
    assert isinstance(ps2, ProjectionSet)
    # Frozen dataclass equality on each field.
    assert ps1.task_ledger.records_by_task_id == ps2.task_ledger.records_by_task_id
    assert ps1.recent_trace == ps2.recent_trace
    assert ps1.claim_evidence.claims_by_id == ps2.claim_evidence.claims_by_id
    assert ps1.claim_evidence.evidence_by_claim_id == ps2.claim_evidence.evidence_by_claim_id
    assert (
        ps1.claim_evidence.claim_ids_by_subject_ref
        == ps2.claim_evidence.claim_ids_by_subject_ref
    )
    # And derived status is stable across rebuilds.
    assert ps1.task_ledger.derive_status("task_X") == "verified_complete"
    assert ps2.task_ledger.derive_status("task_X") == "verified_complete"


def test_rebuild_projections_idempotent_after_drop(tmp_path: Path) -> None:
    """`rebuild_projections` is stable across drop-and-rebuild cycles.

    Models acceptance D5: drop projection cache → re-fold → deep equal.
    Day-1 has no cache, but we simulate by discarding the first result
    and rebuilding.
    """
    with closing(_open(tmp_path)) as conn:
        _seed_verified_task(conn)
        first = rebuild_projections(conn)
        # Discard 'first' completely (Python GC analog of dropping a
        # cache). Rebuild a fresh ProjectionSet from the same source.
        del first
        second = rebuild_projections(conn)
        third = rebuild_projections(conn)

    assert second.task_ledger.records_by_task_id == third.task_ledger.records_by_task_id
    assert second.recent_trace.events == third.recent_trace.events
    assert second.claim_evidence.claims_by_id == third.claim_evidence.claims_by_id


def test_rebuild_projections_on_empty_event_log(tmp_path: Path) -> None:
    """Rebuild on an empty log produces empty projections (no errors)."""
    with closing(_open(tmp_path)) as conn:
        ps = rebuild_projections(conn)

    assert ps.task_ledger.records_by_task_id == {}
    assert ps.recent_trace.events == ()
    assert ps.claim_evidence.claims_by_id == {}
    assert ps.task_ledger.open_tasks() == ()


# --- ClaimEvidenceProjection.from_events standalone -------------------------


def test_claim_evidence_from_events_standalone(tmp_path: Path) -> None:
    """`ClaimEvidenceProjection.from_events` is a public constructor.

    Callable without `rebuild_projections` — used by L3 in Step 9 to
    refresh just the claim/evidence view without touching the Task
    Ledger.
    """
    with closing(_open(tmp_path)) as conn:
        _seed_verified_task(conn)
        events = list(iter_events(conn))

    ce = ClaimEvidenceProjection.from_events(events)
    assert isinstance(ce, ClaimEvidenceProjection)
    assert "C2" in ce.claims_by_id
    assert ce.evidence_for("C2")[0].level == "verified"


# --- TaskLedger.from_events standalone --------------------------------------


def test_task_ledger_from_events_standalone(tmp_path: Path) -> None:
    """`TaskLedger.from_events` can be called without `rebuild_projections`."""
    with closing(_open(tmp_path)) as conn:
        _seed_reported_task(conn)
        events = list(iter_events(conn))

    ledger = TaskLedger.from_events(events)
    assert ledger.derive_status("task_X") == "reported_complete"
    assert ledger.get("task_X") is not None


def test_task_ledger_records_are_frozen() -> None:
    """`TaskLedgerRecord` is frozen — direct mutation raises."""
    record = TaskLedgerRecord(
        task_id="task_X",
        goal="G",
        created_event_uid="u",
        created_ts_epoch_ms=0,
    )
    # `dataclasses.FrozenInstanceError` is a subclass of AttributeError.
    with pytest.raises(AttributeError):
        record.task_id = "task_Y"  # type: ignore[misc]
