"""L2 Projections — Task Ledger, Recent Trace, Claim/Evidence (pure fold).

This module is **read-only** with respect to the Event Log. It SELECTs events
through `jarvis.state.event_log.iter_events` and folds them into in-memory
projection records in Python — it never INSERTs, UPDATEs, or DELETEs any row.
Step 11 canary H1 will whitelist `event_log.py` as the only INSERT site;
this module participates by carrying zero SQL writes.

Per spec.html §3.3.2 + §6 + §7 + §8 and ADR 0001 § Stub strategy (L2 row),
§ Acceptance criterion D + E + F (status derivation + integrity), and
§ Resolver contract (mentions `TaskLedgerSnapshot.open_tasks()`).

Three projections are produced:

1. **Task Ledger** — one record per `task_id` mentioned anywhere on the
   trace. Status is **DERIVED** (computed at read time), never stored.
   The structure carries no `status` field; canary H11 (Step 11) AST
   scans this file and rejects any `task["status"] = ...`,
   `task.status = ...`, or `UPDATE ... SET status = ...` outside
   `derive_status()`.

2. **Recent Trace** — ring buffer of the most recent N events (Day-1: 200).

3. **Claim / Evidence projection** — records every `claim.created` /
   `evidence.attached` event, indexed by `claim_id` and `subject_ref`,
   used by Pre-emit Gate via `strongest_level_for(subject_ref)`.

`rebuild_projections(conn)` is the single L3-facing entry point — it reads
all events from the connection and returns a frozen `ProjectionSet`.

Layer boundary (`.importlinter` + canary H13 Step 11): stdlib only plus
`jarvis.shared` and `jarvis.state.event_log`. No imports from
`jarvis.constitution`, `jarvis.decision`, `jarvis.execution`,
`jarvis.surface`, `jarvis.deployment`, `jarvis.runtime`, `jarvis.cli`.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal, cast

from jarvis.shared import Claim, ClaimType, Evidence, EvidenceLevel
from jarvis.state.event_log import iter_events

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable, Iterator

    from jarvis.shared import Event


# --- Public types ------------------------------------------------------------

TaskStatus = Literal[
    "open",
    "reported_complete",
    "verified_complete",
    "failed",
    "cancelled",
    "unverifiable",
]
"""Derived Task Ledger status (spec §7.11 subset).

Day-1 fold rules produce `open` / `reported_complete` / `verified_complete`.
The remaining values exist in the literal so Stage 2 fold rules can grow
without changing this module's public surface.
"""


# Default Recent Trace ring-buffer capacity (ADR § Module map / spec §6).
_RECENT_TRACE_DEFAULT_SIZE: Final[int] = 200


# Evidence ladder (spec §8.4). Lowest → highest. Index = rank.
_EVIDENCE_LADDER: Final[tuple[EvidenceLevel, ...]] = (
    "reported",
    "observed",
    "executed",
    "verified",
    "accepted",
)
_EVIDENCE_RANK: Final[dict[EvidenceLevel, int]] = {
    level: rank for rank, level in enumerate(_EVIDENCE_LADDER)
}


# --- TaskLedgerRecord --------------------------------------------------------


@dataclass(frozen=True)
class TaskLedgerRecord:
    """One folded Task Ledger row (spec §7.3, ADR § Acceptance D).

    Status is **not** a field on this record per ADR § Acceptance D4 — it is
    computed at read time via `TaskLedger.derive_status(task_id)` /
    `TaskLedgerSnapshot.derive_status(task_id)`. Canary H11 enforces this
    structural invariant by AST scan.

    Attributes:
        task_id: Canonical task identifier (from `task.created.payload.task_id`).
        goal: Goal text (from `task.created.payload.goal`).
        created_event_uid: `event_uid` of the originating `task.created` event.
        created_ts_epoch_ms: `ts_epoch_ms` of the originating `task.created`.
        run_ids: Run IDs (from `run.started`) that target this task. Frozen
            tuple, ordered by first-seen.
        action_ids: Action IDs that worker-reported for any run of this
            task (via `run_id → task_id` correlation). Frozen tuple,
            ordered by first-seen.
        worker_reported_statuses: Worker-report status strings for any
            action under any run of this task. Frozen tuple, in event
            order.
        task_verified_event_uids: `event_uid` of every `task.verified`
            event targeting this `task_id`. Empty when no verification
            event has fired.
    """

    task_id: str
    goal: str
    created_event_uid: str
    created_ts_epoch_ms: int
    run_ids: tuple[str, ...] = field(default_factory=tuple)
    action_ids: tuple[str, ...] = field(default_factory=tuple)
    worker_reported_statuses: tuple[str, ...] = field(default_factory=tuple)
    task_verified_event_uids: tuple[str, ...] = field(default_factory=tuple)


# --- ClaimEvidenceProjection -------------------------------------------------


@dataclass(frozen=True)
class ClaimEvidenceProjection:
    """Folded Claim/Evidence projection (spec §8, ADR § Acceptance E).

    Indexes every `claim.created` and `evidence.attached` event by
    `claim_id` plus a secondary `subject_ref → claim_id` map. Rebuild is
    idempotent (acceptance E4).

    Attributes:
        claims_by_id: Frozen mapping of `claim_id → Claim`. Insertion
            order = event order.
        evidence_by_claim_id: Frozen mapping of `claim_id → tuple of
            Evidence`. Each tuple preserves event order; multiple evidence
            entries per claim are allowed (spec §8.6).
        claim_ids_by_subject_ref: Frozen mapping of `subject_ref → tuple
            of claim_id`. Used by `claims_for(subject_ref)` and
            `strongest_level_for(subject_ref)`.
    """

    claims_by_id: dict[str, Claim]
    evidence_by_claim_id: dict[str, tuple[Evidence, ...]]
    claim_ids_by_subject_ref: dict[str, tuple[str, ...]]

    def claims_for(self, subject_ref: str) -> tuple[Claim, ...]:
        """Return claims for `subject_ref`, ordered by `ts_epoch_ms` asc."""
        claim_ids = self.claim_ids_by_subject_ref.get(subject_ref, ())
        claims = tuple(self.claims_by_id[cid] for cid in claim_ids)
        return tuple(sorted(claims, key=lambda c: c.ts_epoch_ms))

    def evidence_for(self, claim_id: str) -> tuple[Evidence, ...]:
        """Return all evidence for `claim_id` in event order."""
        return self.evidence_by_claim_id.get(claim_id, ())

    def strongest_level_for(self, subject_ref: str) -> EvidenceLevel | None:
        """Strongest evidence level across all claims for `subject_ref`.

        Walks every claim whose `subject_ref` equals the argument, gathers
        every `Evidence.level` attached to those claims, and returns the
        maximum per the ladder `accepted > verified > executed > observed >
        reported`. Returns None when no evidence exists.

        Used by the Pre-emit Gate (Step 9) to decide whether the draft
        response may carry completion-class language.
        """
        levels: list[EvidenceLevel] = [
            ev.level
            for claim_id in self.claim_ids_by_subject_ref.get(subject_ref, ())
            for ev in self.evidence_by_claim_id.get(claim_id, ())
        ]
        if not levels:
            return None
        return max(levels, key=lambda level: _EVIDENCE_RANK[level])

    @classmethod
    def from_events(cls, events: Iterable[Event]) -> ClaimEvidenceProjection:
        """Fold `events` into a Claim/Evidence projection."""
        return _fold_claim_evidence(events)


# --- TaskLedgerSnapshot ------------------------------------------------------


@dataclass(frozen=True)
class TaskLedgerSnapshot:
    """Frozen immutable view of the Task Ledger (ADR § Resolver contract).

    Returned by `TaskLedger.snapshot()` and by the top-level
    `rebuild_projections(conn)` indirectly via `ProjectionSet.task_ledger`.
    The Resolver (Step 9) consults `open_tasks()`. Derived status is
    computed on demand by replaying the same fold rule against the
    snapshot's frozen record set + claim/evidence projection.

    Attributes:
        records_by_task_id: Frozen mapping of `task_id → TaskLedgerRecord`.
        claim_evidence: Companion projection used for `verified_complete`
            derivation (spec §13 I10 — claim must not exceed evidence).
    """

    records_by_task_id: dict[str, TaskLedgerRecord]
    claim_evidence: ClaimEvidenceProjection

    def get(self, task_id: str) -> TaskLedgerRecord | None:
        """Return the record for `task_id`, or None if absent."""
        return self.records_by_task_id.get(task_id)

    def derive_status(self, task_id: str) -> TaskStatus:
        """Compute the status for `task_id` from folded events + claims.

        Same rules as `TaskLedger.derive_status` — duplicated here so
        Resolver / Situation Packet assembler can derive status from a
        snapshot without needing the live ledger.
        """
        return _derive_status(self.records_by_task_id, self.claim_evidence, task_id)

    def open_tasks(self) -> tuple[TaskLedgerRecord, ...]:
        """Return all task records whose derived status is `open`.

        Used by the Resolver per ADR § Resolver contract. The returned
        tuple is read-only — mutating the result does not mutate the
        snapshot.
        """
        return tuple(
            record
            for task_id, record in self.records_by_task_id.items()
            if self.derive_status(task_id) == "open"
        )


# --- TaskLedger --------------------------------------------------------------


def _derive_status(
    records_by_task_id: dict[str, TaskLedgerRecord],
    claim_evidence: ClaimEvidenceProjection,
    task_id: str,
) -> TaskStatus:
    """Status fold rule (spec §13 I8/I10 + ADR § Acceptance D + F).

    Single implementation shared by `TaskLedger.derive_status` and
    `TaskLedgerSnapshot.derive_status` — guarantees the live ledger and
    its snapshot agree byte-for-byte (acceptance D5).

    Rules:

    - `open` — `task.created` exists, no `worker.reported` has fired for
      any of its actions, and no `task.verified` has fired.
    - `verified_complete` — at least one `task.verified(task_id=...)`
      event exists for this task AND at least one Postcondition Claim
      with `evidence.level=verified` (or `accepted`) exists for the
      subject_ref equal to `task_id`. Per ADR § Acceptance D1 + D3:
      agent self-report does not verify, and a claim must not exceed
      its supporting evidence.
    - `reported_complete` — at least one `worker.reported` references
      this task (via the run_id chain) AND the verified condition above
      is NOT met. The negative-path scenario F asserts this is the
      terminal status when `verify_diff` fails.

    Returns `open` when no relevant fold pattern matches (e.g., the task
    record exists but no events progressed it).
    """
    record = records_by_task_id.get(task_id)
    if record is None:
        return "open"

    # verified_complete requires BOTH (a) a task.verified event for this
    # task_id AND (b) at least one Postcondition Claim with level in
    # {verified, accepted} for subject_ref == task_id. Either alone is
    # insufficient per ADR § Acceptance D1 + D3.
    if record.task_verified_event_uids:
        for claim in claim_evidence.claims_for(task_id):
            if claim.type != "Postcondition":
                continue
            for ev in claim_evidence.evidence_for(claim.claim_id):
                if ev.level in ("verified", "accepted"):
                    return "verified_complete"

    # reported_complete — any worker.reported for an action under this task.
    if record.worker_reported_statuses:
        return "reported_complete"

    return "open"


@dataclass(frozen=True)
class TaskLedger:
    """Folded Task Ledger projection (spec §7, ADR § Acceptance D).

    Built by `TaskLedger.from_events(events)`. The `records_by_task_id`
    dict is owned by the instance — `snapshot()` returns a frozen
    `TaskLedgerSnapshot` that shares this dict by reference (it is
    treated as immutable by all consumers).

    Attributes:
        records_by_task_id: Mapping of `task_id → TaskLedgerRecord`.
            Treated as immutable; not mutated after `from_events`.
        claim_evidence: Companion Claim/Evidence projection used for
            `verified_complete` derivation.
    """

    records_by_task_id: dict[str, TaskLedgerRecord]
    claim_evidence: ClaimEvidenceProjection

    @classmethod
    def from_events(
        cls,
        events: Iterable[Event],
        *,
        claim_evidence: ClaimEvidenceProjection | None = None,
    ) -> TaskLedger:
        """Fold an event sequence into a Task Ledger.

        Two-pass-friendly: when `claim_evidence` is provided (already
        folded from the same event sequence), it is reused; otherwise a
        fresh `ClaimEvidenceProjection.from_events` is built first. The
        `rebuild_projections` entry point provides the joint projection
        so this re-fold cost is paid at most once.

        Args:
            events: Iterable of `Event`s in append order (use
                `iter_events(conn)` to get the canonical ordering).
            claim_evidence: Optional pre-built Claim/Evidence projection
                folded from the same event sequence. When None, this
                method folds the events twice — once to build a
                `ClaimEvidenceProjection`, once to build the Task Ledger
                rows. Tests and `rebuild_projections` should pre-build
                the projection to avoid the double pass.
        """
        materialized = list(events)
        if claim_evidence is None:
            claim_evidence = ClaimEvidenceProjection.from_events(materialized)
        records = _fold_task_ledger_records(materialized)
        return cls(records_by_task_id=records, claim_evidence=claim_evidence)

    def get(self, task_id: str) -> TaskLedgerRecord | None:
        """Return the record for `task_id`, or None if absent."""
        return self.records_by_task_id.get(task_id)

    def derive_status(self, task_id: str) -> TaskStatus:
        """Compute the status for `task_id` from folded events + claims."""
        return _derive_status(self.records_by_task_id, self.claim_evidence, task_id)

    def open_tasks(self) -> tuple[TaskLedgerRecord, ...]:
        """Return all task records whose derived status is `open`."""
        return tuple(
            record
            for task_id, record in self.records_by_task_id.items()
            if self.derive_status(task_id) == "open"
        )

    def snapshot(self) -> TaskLedgerSnapshot:
        """Return a frozen immutable view of this ledger.

        The Resolver (Step 9) and Situation Packet assembler consult the
        snapshot per ADR § Resolver contract — never the live ledger
        directly.
        """
        return TaskLedgerSnapshot(
            records_by_task_id=self.records_by_task_id,
            claim_evidence=self.claim_evidence,
        )


def _fold_task_ledger_records(events: Iterable[Event]) -> dict[str, TaskLedgerRecord]:
    """Single-pass fold producing Task Ledger records.

    Correlation challenges this fold handles explicitly:

    - `worker.reported` payload does NOT carry `task_id` (it carries
      `run_id` + `action_id` + `status`). We bridge through the
      `run.started` event, which DOES carry both `run_id` and `task_id`,
      to bind worker reports back to their task.
    - `task.verified(task_id, by)` event directly references the task
      and is appended at the end of a happy-path run (canonical trace
      evt 22).
    - `claim.created` / `evidence.attached` are folded by
      `ClaimEvidenceProjection`; this fold does not touch them.
    """
    # task_id → (goal, created_event_uid, created_ts_epoch_ms)
    seed: dict[str, tuple[str, str, int]] = {}
    # task_id → list of run_id (ordered, deduped via membership check)
    run_ids_by_task: dict[str, list[str]] = {}
    # task_id → list of action_id (from worker.reported)
    action_ids_by_task: dict[str, list[str]] = {}
    # task_id → list of worker.reported status strings (in event order)
    statuses_by_task: dict[str, list[str]] = {}
    # task_id → list of task.verified event_uid
    verified_event_uids_by_task: dict[str, list[str]] = {}
    # run_id → task_id (built from run.started)
    run_to_task: dict[str, str] = {}

    for evt in events:
        if evt.type == "task.created":
            task_id = str(evt.payload["task_id"])
            goal = str(evt.payload["goal"])
            seed.setdefault(task_id, (goal, evt.event_uid, evt.ts_epoch_ms))
            run_ids_by_task.setdefault(task_id, [])
            action_ids_by_task.setdefault(task_id, [])
            statuses_by_task.setdefault(task_id, [])
            verified_event_uids_by_task.setdefault(task_id, [])
        elif evt.type == "run.started":
            run_id = str(evt.payload["run_id"])
            task_id = str(evt.payload["task_id"])
            run_to_task[run_id] = task_id
            run_ids_by_task.setdefault(task_id, [])
            if run_id not in run_ids_by_task[task_id]:
                run_ids_by_task[task_id].append(run_id)
        elif evt.type == "worker.reported":
            run_id = str(evt.payload["run_id"])
            action_id = str(evt.payload["action_id"])
            status = str(evt.payload["status"])
            bridged_task_id = run_to_task.get(run_id)
            if bridged_task_id is None:
                # worker.reported with no matching run.started — Day-1
                # rule: skip; the ADR § Acceptance D path requires
                # run.started before worker.reported.
                continue
            action_ids_by_task.setdefault(bridged_task_id, [])
            if action_id not in action_ids_by_task[bridged_task_id]:
                action_ids_by_task[bridged_task_id].append(action_id)
            statuses_by_task.setdefault(bridged_task_id, []).append(status)
        elif evt.type == "task.verified":
            task_id = str(evt.payload["task_id"])
            verified_event_uids_by_task.setdefault(task_id, []).append(evt.event_uid)

    records: dict[str, TaskLedgerRecord] = {}
    for task_id, (goal, created_uid, created_ts) in seed.items():
        records[task_id] = TaskLedgerRecord(
            task_id=task_id,
            goal=goal,
            created_event_uid=created_uid,
            created_ts_epoch_ms=created_ts,
            run_ids=tuple(run_ids_by_task.get(task_id, [])),
            action_ids=tuple(action_ids_by_task.get(task_id, [])),
            worker_reported_statuses=tuple(statuses_by_task.get(task_id, [])),
            task_verified_event_uids=tuple(verified_event_uids_by_task.get(task_id, [])),
        )
    return records


# --- RecentTrace -------------------------------------------------------------


@dataclass(frozen=True)
class RecentTrace:
    """Bounded ring buffer of the most recent N events (spec §6).

    `iter()` yields oldest-first (== append-order). The internal store
    is a frozen tuple so the contract surface forbids mutation; rebuild
    is idempotent.

    Attributes:
        events: Frozen tuple of at most `max_size` events, oldest first.
        max_size: Configured ring-buffer capacity (default 200, Day-1).
    """

    events: tuple[Event, ...]
    max_size: int

    @classmethod
    def from_events(
        cls,
        events: Iterable[Event],
        *,
        max_size: int = _RECENT_TRACE_DEFAULT_SIZE,
    ) -> RecentTrace:
        """Fold `events` into a ring buffer of capacity `max_size`.

        Implementation uses `collections.deque(maxlen=...)` so the fold
        is O(n) regardless of the input length. The result is frozen to
        a tuple — the deque itself does not escape.
        """
        if max_size < 0:
            msg = f"max_size must be non-negative, got {max_size!r}"
            raise ValueError(msg)
        buf: deque[Event] = deque(maxlen=max_size) if max_size > 0 else deque(maxlen=0)
        for evt in events:
            buf.append(evt)
        return cls(events=tuple(buf), max_size=max_size)

    def iter(self) -> Iterator[Event]:
        """Yield events oldest-first (append-order).

        Pick documented in ADR § Acceptance: oldest-first lets a consumer
        process the trace in causal order without an explicit reverse.
        """
        return iter(self.events)


# --- ClaimEvidenceProjection.from_events -------------------------------------


def _fold_claim_evidence(events: Iterable[Event]) -> ClaimEvidenceProjection:
    """Single-pass fold producing the Claim/Evidence projection."""
    claims_by_id: dict[str, Claim] = {}
    evidence_by_claim_id: dict[str, list[Evidence]] = {}
    claim_ids_by_subject_ref: dict[str, list[str]] = {}

    for evt in events:
        if evt.type == "claim.created":
            claim_id = str(evt.payload["claim_id"])
            # `type` / `level` arrive as JSON strings from the event log;
            # validation that they are members of the `ClaimType` /
            # `EvidenceLevel` literal sets is the Result Interpreter's
            # responsibility (Step 9). The projection fold trusts the
            # producer per spec §5.4 EventTypeRegistry contract.
            claim_type = cast("ClaimType", evt.payload["type"])
            statement = str(evt.payload["statement"])
            subject_ref = str(evt.payload["subject_ref"])
            produced_by_event_id = str(evt.payload["produced_by_event_id"])
            claims_by_id[claim_id] = Claim(
                claim_id=claim_id,
                type=claim_type,
                statement=statement,
                subject_ref=subject_ref,
                produced_by_event_id=produced_by_event_id,
                ts_epoch_ms=evt.ts_epoch_ms,
            )
            claim_ids_by_subject_ref.setdefault(subject_ref, []).append(claim_id)
        elif evt.type == "evidence.attached":
            evidence_id = str(evt.payload["evidence_id"])
            claim_id = str(evt.payload["claim_id"])
            level = cast("EvidenceLevel", evt.payload["level"])
            # Carry every optional payload field through to Evidence.payload
            # so Stage 2 consumers (artifact_path / content_hash / scope /
            # freshness_ms) can read them without re-fetching the event.
            extras = {
                k: v
                for k, v in evt.payload.items()
                if k not in ("evidence_id", "claim_id", "level")
            }
            evidence_by_claim_id.setdefault(claim_id, []).append(
                Evidence(
                    evidence_id=evidence_id,
                    claim_id=claim_id,
                    level=level,
                    source_event_id=evt.event_uid,
                    payload=extras,
                    ts_epoch_ms=evt.ts_epoch_ms,
                ),
            )

    return ClaimEvidenceProjection(
        claims_by_id=claims_by_id,
        evidence_by_claim_id={cid: tuple(evs) for cid, evs in evidence_by_claim_id.items()},
        claim_ids_by_subject_ref={
            ref: tuple(cids) for ref, cids in claim_ids_by_subject_ref.items()
        },
    )


# --- ProjectionSet -----------------------------------------------------------


@dataclass(frozen=True)
class ProjectionSet:
    """Bundle of the three Day-1 projections produced by `rebuild_projections`.

    Frozen so that comparison / equality semantics work for the
    rebuild-idempotency test (acceptance D5 / E4).

    Attributes:
        task_ledger: Folded Task Ledger.
        recent_trace: Folded Recent Trace ring buffer.
        claim_evidence: Folded Claim/Evidence projection.
    """

    task_ledger: TaskLedger
    recent_trace: RecentTrace
    claim_evidence: ClaimEvidenceProjection


def rebuild_projections(
    conn: sqlite3.Connection,
    *,
    recent_trace_size: int = _RECENT_TRACE_DEFAULT_SIZE,
) -> ProjectionSet:
    """Read all events from `conn` and fold all three projections.

    Single SELECT-driven pass over the live Event Log via
    `iter_events(conn)` — no SQL writes, no projection tables touched.
    Idempotent: calling twice on the same connection (with no
    intervening `emit_event`) returns deep-equal `ProjectionSet`s.

    Args:
        conn: Open Event Log connection from
            `jarvis.state.event_log.open_event_log`.
        recent_trace_size: Ring-buffer capacity for the Recent Trace
            projection. Default = 200 (Day-1 per ADR § Module map).

    Returns:
        `ProjectionSet` carrying frozen `task_ledger`, `recent_trace`,
        and `claim_evidence` projections.
    """
    materialized = list(iter_events(conn))
    claim_evidence = _fold_claim_evidence(materialized)
    task_ledger = TaskLedger(
        records_by_task_id=_fold_task_ledger_records(materialized),
        claim_evidence=claim_evidence,
    )
    recent_trace = RecentTrace.from_events(materialized, max_size=recent_trace_size)
    return ProjectionSet(
        task_ledger=task_ledger,
        recent_trace=recent_trace,
        claim_evidence=claim_evidence,
    )


def make_snapshot(conn: sqlite3.Connection) -> ProjectionSet:
    """L3-facing alias for `rebuild_projections` (Day-1 identical).

    Stage 2 may introduce snapshot-vs-rebuild differentiation (e.g.,
    high-water-mark caching per spec §3.3.6); Day-1 keeps the contract
    surface simple by aliasing to a fresh fold every call.
    """
    return rebuild_projections(conn)


__all__ = [
    "ClaimEvidenceProjection",
    "ProjectionSet",
    "RecentTrace",
    "TaskLedger",
    "TaskLedgerRecord",
    "TaskLedgerSnapshot",
    "TaskStatus",
    "make_snapshot",
    "rebuild_projections",
]
