"""L2 Projections — Task Ledger, Recent Trace, Claim/Evidence (pure fold).

This module is **read-only** with respect to the Event Log. It SELECTs events
through `jarvis.state.event_log.iter_events` and folds them into in-memory
projection records in Python — it never INSERTs, UPDATEs, or DELETEs any row.
Step 11 canary H1 will whitelist `event_log.py` as the only INSERT site;
this module participates by carrying zero SQL writes.

Per spec.html §3.3.2 + §6 + §7 + §8 and ADR 0001 § Stub strategy (L2 row),
§ Acceptance criterion D + E + F (status derivation + integrity), and
§ Resolver contract (mentions `TaskLedgerSnapshot.open_tasks()`).

Four projections are produced:

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

4. **Status Board** — ADR-0009 D6: latest observed state per watched
   repo (`repo.state_observed` + `project.commit_seen`), the last Mac
   power transition (`mac.sleeping` / `mac.awake`), and every action
   the log shows dispatched with no terminal event yet.

`rebuild_projections(conn)` is the single L3-facing entry point — it reads
all events from the connection and returns a frozen `ProjectionSet`.

Layer boundary (`.importlinter` + canary H13 Step 11): stdlib only plus
`jarvis.shared` and `jarvis.state.event_log`. No imports from
`jarvis.constitution`, `jarvis.decision`, `jarvis.execution`,
`jarvis.surface`, `jarvis.deployment`, `jarvis.runtime`, `jarvis.cli`.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from jarvis.shared import Claim, ClaimType, Evidence, EvidenceLevel
from jarvis.state.event_log import iter_events

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable, Iterator, Mapping, Sequence

    from jarvis.shared import Event


# --- Public types ------------------------------------------------------------

TaskId = str
"""Canonical task identifier (alias kept open so Stage 2 can swap to NewType)."""


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
        verify_command: Verbatim `verify_command` string stored on
            `task.created.optional_payload["verify_command"]` (ADR-0002
            Step 4 — `create_task_handler` writes it). Step 12 L3 reads
            this off the projection and plumbs it onto
            `ActionRequest.payload["verify_command"]` when proposing the
            `verify_diff` action. `None` when the D-1 detection returned
            no match.
        repo_path: Verbatim `repo_path` string stored on
            `task.created.optional_payload["repo_path"]`. Step 12 L3
            plumbs it onto `ActionRequest.payload["repo_path"]` so the
            L4 `verify_command` subprocess runs with the right cwd.
    """

    task_id: str
    goal: str
    created_event_uid: str
    created_ts_epoch_ms: int
    run_ids: tuple[str, ...] = field(default_factory=tuple)
    action_ids: tuple[str, ...] = field(default_factory=tuple)
    worker_reported_statuses: tuple[str, ...] = field(default_factory=tuple)
    task_verified_event_uids: tuple[str, ...] = field(default_factory=tuple)
    verify_command: str | None = None
    repo_path: str | None = None


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

    def active_claims_for(self, subject_ref: str) -> tuple[Claim, ...]:
        """Claims for `subject_ref` excluding `refuted` / `superseded`.

        The correction-aware view every completion-adjacent reader must
        use (spec §3.8 invariant 2): a refuted or superseded claim is
        history, not support. `limited` claims remain active — a
        limitation qualifies, it does not veto (ADR-0002).
        """
        return tuple(
            claim
            for claim in self.claims_for(subject_ref)
            if claim.status not in ("refuted", "superseded")
        )

    def strongest_level_for(self, subject_ref: str) -> EvidenceLevel | None:
        """Strongest SUPPORTING evidence level for `subject_ref`.

        Walks every ACTIVE claim (not refuted/superseded) for the
        subject, gathers every supporting `Evidence.level` (payload
        `relation` == "supports"; absent relation counts as supporting
        for pre-relation rows), and returns the maximum per the ladder
        `accepted > verified > executed > observed > reported`. Returns
        None when no supporting evidence exists.

        Relation-awareness fix (2026-08-25): refuting / limiting
        evidence previously counted toward the strongest level, so a
        verified-level REFUTING row would strengthen a claim.

        Used by the Pre-emit Gate (Step 9) to decide whether the draft
        response may carry completion-class language.
        """
        levels: list[EvidenceLevel] = [
            ev.level
            for claim in self.active_claims_for(subject_ref)
            for ev in self.evidence_by_claim_id.get(claim.claim_id, ())
            if ev.payload.get("relation", "supports") == "supports"
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

    def tasks_in_window(self, since_ts: int, until_ts: int) -> list[TaskId]:
        """Return task_ids whose `task.created.ts_epoch_ms` falls in the window.

        Per ADR-0002 § Module Map (`projections.py` edits) and spec
        §3.4.3: L3's time-window resolver consults this method instead of
        running a SELECT against the events table directly. Day-2 returns
        bare task_ids; richer fields (goal, repo_path, verify_command)
        remain accessible via :meth:`get`.

        Args:
            since_ts: Lower bound (inclusive), epoch milliseconds.
            until_ts: Upper bound (inclusive), epoch milliseconds.

        Returns:
            List of `task_id`s whose ``created_ts_epoch_ms`` satisfies
            ``since_ts <= ts <= until_ts``. Sorted ascending by ts
            (oldest first); ties broken by task_id for determinism.
        """
        return _tasks_in_window(self.records_by_task_id, since_ts, until_ts)


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
    # Correction-aware (spec §3.8 invariant 2 + §8.8, 2026-08-25):
    # refuted/superseded Postcondition claims no longer count; evidence
    # must SUPPORT (a verified-level refuting row is a block, not a
    # proof); and any verified/accepted-level refuting row on the claim
    # blocks it (§8.8 "no blocking refute evidence", minimal reading).
    if record.task_verified_event_uids:
        for claim in claim_evidence.active_claims_for(task_id):
            if claim.type != "Postcondition":
                continue
            rows = claim_evidence.evidence_for(claim.claim_id)
            supported = any(
                ev.level in ("verified", "accepted")
                and ev.payload.get("relation", "supports") == "supports"
                for ev in rows
            )
            blocked = any(
                ev.level in ("verified", "accepted")
                and ev.payload.get("relation") == "refutes"
                for ev in rows
            )
            if supported and not blocked:
                return "verified_complete"

    # reported_complete — any worker.reported for an action under this task.
    if record.worker_reported_statuses:
        return "reported_complete"

    return "open"


def _tasks_in_window(
    records_by_task_id: dict[str, TaskLedgerRecord],
    since_ts: int,
    until_ts: int,
) -> list[TaskId]:
    """Filter folded records by `created_ts_epoch_ms` window (inclusive).

    Shared between :meth:`TaskLedger.tasks_in_window` and
    :meth:`TaskLedgerSnapshot.tasks_in_window` so the live ledger and a
    frozen snapshot agree byte-for-byte (mirror of `_derive_status`).
    Ordering: ascending by ``created_ts_epoch_ms``; ties broken by
    ``task_id`` so the resolver's "single match" path is deterministic
    even when two tasks share a clock tick.
    """
    matched = [
        record
        for record in records_by_task_id.values()
        if since_ts <= record.created_ts_epoch_ms <= until_ts
    ]
    matched.sort(key=lambda r: (r.created_ts_epoch_ms, r.task_id))
    return [record.task_id for record in matched]


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

    def tasks_in_window(self, since_ts: int, until_ts: int) -> list[TaskId]:
        """Return task_ids whose creation ts falls in ``[since_ts, until_ts]``.

        Mirror of :meth:`TaskLedgerSnapshot.tasks_in_window` (shared
        helper :func:`_tasks_in_window`). See that docstring for the
        contract.
        """
        return _tasks_in_window(self.records_by_task_id, since_ts, until_ts)

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
    # task_id → optional verify_command + repo_path lifted from
    # task.created.optional_payload (ADR-0002 § Verify_command plumbing).
    verify_command_by_task: dict[str, str | None] = {}
    repo_path_by_task: dict[str, str | None] = {}
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
            # Day-2 (ADR-0002 Step 12) — surface optional verify_command
            # / repo_path so the L3 Result Interpreter can plumb them
            # onto ActionRequest.payload for verify_diff. Read verbatim
            # off the task.created payload; no transformation.
            verify_command_raw = evt.payload.get("verify_command")
            verify_command_by_task[task_id] = (
                verify_command_raw if isinstance(verify_command_raw, str) else None
            )
            repo_path_raw = evt.payload.get("repo_path")
            repo_path_by_task[task_id] = (
                repo_path_raw if isinstance(repo_path_raw, str) else None
            )
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
            verify_command=verify_command_by_task.get(task_id),
            repo_path=repo_path_by_task.get(task_id),
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


def _fold_claim_evidence(  # noqa: C901 — one branch per §3.3.3 event family; splitting hides the fold table.
    events: Iterable[Event],
) -> ClaimEvidenceProjection:
    """Single-pass fold producing the Claim/Evidence projection.

    Correction mechanism (spec §3.3.3 / §3.8 invariant 2, landed
    2026-08-25): a claim's `status` is derived here from the four
    correction events — the claim record itself is never edited.
    Transition rules, applied in event order:

    - ``claim.created`` → ``open``.
    - ``evidence.attached`` with ``relation="supports"`` → ``supported``
      (only from ``open`` — corrections are not undone by later
      supporting evidence).
    - ``claim.refuted`` → ``refuted``.
    - ``claim.limited`` → ``limited`` (only from ``open``/``supported``;
      a limitation qualifies a live claim, it does not resurrect a
      refuted or superseded one).
    - ``claim.superseded`` → ``superseded``.
    - ``claim.accepted`` → ``supported`` unconditionally (Allen's
      override outranks prior corrections) AND synthesizes one
      accepted-level supporting Evidence row (spec §8.4 ladder top —
      "accepted" is an evidence level, not a §8.7 status).

    A correction referencing an unknown ``claim_id`` is skipped — a
    partial replay must not crash the rebuild.

    Note: spec §6's projection table lists only refuted/accepted as fold
    sources; §3.3.3 and §5.2 list all four correction events. Treated as
    a §6 typo — all four fold here.
    """
    claims_by_id: dict[str, Claim] = {}
    evidence_by_claim_id: dict[str, list[Evidence]] = {}
    claim_ids_by_subject_ref: dict[str, list[str]] = {}

    def _transition(evt: Event) -> None:
        claim_id = str(evt.payload["claim_id"])
        claim = claims_by_id.get(claim_id)
        if claim is None:
            return
        if evt.type == "claim.refuted":
            claims_by_id[claim_id] = replace(claim, status="refuted")
        elif evt.type == "claim.limited":
            if claim.status in ("open", "supported"):
                claims_by_id[claim_id] = replace(claim, status="limited")
        elif evt.type == "claim.superseded":
            claims_by_id[claim_id] = replace(claim, status="superseded")
        elif evt.type == "claim.accepted":
            claims_by_id[claim_id] = replace(claim, status="supported")
            note = evt.payload.get("note")
            payload: dict[str, Any] = {"relation": "supports", "source_type": "user"}
            if isinstance(note, str):
                payload["note"] = note
            evidence_by_claim_id.setdefault(claim_id, []).append(
                Evidence(
                    evidence_id="E" + evt.event_uid[:8],
                    claim_id=claim_id,
                    level="accepted",
                    source_event_id=evt.event_uid,
                    payload=payload,
                    ts_epoch_ms=evt.ts_epoch_ms,
                ),
            )

    for evt in events:
        if evt.type in ("claim.refuted", "claim.limited", "claim.superseded", "claim.accepted"):
            _transition(evt)
        elif evt.type == "claim.created":
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
            claim = claims_by_id.get(claim_id)
            if (
                claim is not None
                and claim.status == "open"
                and extras.get("relation", "supports") == "supports"
            ):
                claims_by_id[claim_id] = replace(claim, status="supported")

    return ClaimEvidenceProjection(
        claims_by_id=claims_by_id,
        evidence_by_claim_id={cid: tuple(evs) for cid, evs in evidence_by_claim_id.items()},
        claim_ids_by_subject_ref={
            ref: tuple(cids) for ref, cids in claim_ids_by_subject_ref.items()
        },
    )


# --- StatusBoard --------------------------------------------------------------


PowerState = Literal["awake", "sleeping"]
"""Last observed Mac power state (spec §3.7.8 `mac.sleeping` / `mac.awake`)."""


# Terminal action-lifecycle types that close an open action. Same set as the
# supervisor sweep's `_TERMINAL_ACTION_EVENT_TYPES` in
# `jarvis/deployment/sleep_wake.py` — `action.cancelled` is terminal in both
# the L4 lifecycle FSM and the sleep/wake fold, so leaving it out here would
# strand cancelled actions on the board as phantom open work forever
# (ADR-0009 D6 calls this out by name).
_STATUS_BOARD_TERMINAL_ACTION_TYPES: Final[frozenset[str]] = frozenset(
    {
        "action.result_observed",
        "action.failed",
        "action.timeout_assumed",
        "action.cancelled",
    },
)


@dataclass(frozen=True)
class RepoObservation:
    """Latest `repo.state_observed` for one watched repo (ADR-0009 D5 payload).

    Attributes:
        repo_path: Absolute path of the watched repo (the fold key).
        branch: Current branch, or the literal `"HEAD"` on a detached
            checkout — the producer keeps the contract total.
        head_sha: Full HEAD sha as observed.
        dirty_file_count: `git status --porcelain` line count.
        last_commit_subject: Subject of HEAD's commit (producer-capped at
            200 chars per spec §3.3.9 bounded payloads).
        observed_at_ms: Producer-stamped observation time. This — not the
            event's `ts_epoch_ms` — is what freshness wording is computed
            against, because it is when the world was actually looked at.
    """

    repo_path: str
    branch: str
    head_sha: str
    dirty_file_count: int
    last_commit_subject: str
    observed_at_ms: int


@dataclass(frozen=True)
class CommitObservation:
    """Latest `project.commit_seen` for one watched repo.

    Attributes:
        repo_path: Absolute path of the watched repo (the fold key).
        commit_sha: Commit sha of the newest commit seen for this repo.
        subject: Commit subject (producer-capped at 200 chars).
        committed_at_ms: Commit timestamp, epoch ms.
        truncated: True when the producer burst-capped the poll that
            emitted this event — the commit window is gapped, not
            contiguous (ADR-0010 reads this).
        skipped_count: How many commits the burst cap dropped, when the
            producer reported it.
    """

    repo_path: str
    commit_sha: str
    subject: str
    committed_at_ms: int
    truncated: bool
    skipped_count: int | None


@dataclass(frozen=True)
class PowerTransition:
    """The last `mac.sleeping` / `mac.awake` transition on the log.

    Attributes:
        state: `"sleeping"` or `"awake"`.
        ts_epoch_ms: Transition time as stamped in the payload (kernel
            time for the real power-notification path).
        slept_for_ms: Sleep duration carried by `mac.awake`; None for a
            `mac.sleeping` transition.
    """

    state: PowerState
    ts_epoch_ms: int
    slept_for_ms: int | None


@dataclass(frozen=True)
class OpenAction:
    """One action the log shows dispatched with no terminal event yet.

    Attributes:
        action_id: Canonical action identifier.
        dispatched_ts_ms: `ts_epoch_ms` of its `action.dispatched` row.
    """

    action_id: str
    dispatched_ts_ms: int


@dataclass(frozen=True)
class StatusBoard:
    """Folded Status Board projection (spec §6, ADR-0009 D6).

    "What is true about the machine right now" as distinct from "what is
    true about the tasks" (Task Ledger). Full-refold like every other
    projection here; Stage-2 incrementality stays deferred.

    Fold sources are exactly the §6 canonical ones minus the two
    domain-availability types that Mac-only scope does not yet register
    (`domain_availability.changed` / `domain_projection.stale` — recorded
    as ADR-0009 deviation V2, deliberately absent rather than forgotten).

    Attributes:
        repos: `repo_path → RepoObservation`, latest per repo.
        latest_commits: `repo_path → CommitObservation`, latest per repo.
        last_power_transition: Last `mac.*` transition, or None when the
            log carries none.
        open_actions: Actions dispatched with no terminal event, in
            first-dispatched order.
    """

    repos: dict[str, RepoObservation] = field(default_factory=dict)
    latest_commits: dict[str, CommitObservation] = field(default_factory=dict)
    last_power_transition: PowerTransition | None = None
    open_actions: tuple[OpenAction, ...] = ()

    @classmethod
    def from_events(cls, events: Iterable[Event]) -> StatusBoard:
        """Fold `events` into a Status Board."""
        return _fold_status_board(events)

    def repos_by_recency(self) -> tuple[RepoObservation, ...]:
        """Return observations newest-observed first; ties broken by path."""
        return tuple(
            sorted(
                self.repos.values(),
                key=lambda observation: (-observation.observed_at_ms, observation.repo_path),
            ),
        )

    def last_wake_ts_ms(self) -> int | None:
        """Epoch-ms of the most recent wake, or None if the last transition was a sleep.

        Consumers use this to tell "observed before the machine woke" from
        "merely old": an observation older than the wake cannot have seen
        anything that happened during the sleep window.
        """
        transition = self.last_power_transition
        if transition is None or transition.state != "awake":
            return None
        return transition.ts_epoch_ms


def _payload_int(evt: Event, key: str) -> int | None:
    """Return `evt.payload[key]` as an int, or None when absent / not an int.

    Payloads round-trip through JSON, so the registry's "required field"
    guarantee covers presence but not type. `bool` is rejected explicitly
    (it is an `int` subclass and would silently fold to 0/1).
    """
    value = evt.payload.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _fold_repo_observation(repos: dict[str, RepoObservation], evt: Event) -> None:
    """Fold one `repo.state_observed` row; last row in log order wins."""
    repo_path = str(evt.payload["repo_path"])
    observed_at_ms = _payload_int(evt, "observed_at_ms")
    dirty_file_count = _payload_int(evt, "dirty_file_count")
    repos[repo_path] = RepoObservation(
        repo_path=repo_path,
        branch=str(evt.payload["branch"]),
        head_sha=str(evt.payload["head_sha"]),
        dirty_file_count=0 if dirty_file_count is None else dirty_file_count,
        last_commit_subject=str(evt.payload["last_commit_subject"]),
        observed_at_ms=evt.ts_epoch_ms if observed_at_ms is None else observed_at_ms,
    )


def _fold_commit_observation(commits: dict[str, CommitObservation], evt: Event) -> None:
    """Fold one `project.commit_seen` row; last row in log order wins."""
    repo_path = str(evt.payload["repo_path"])
    committed_at_ms = _payload_int(evt, "committed_at_ms")
    commits[repo_path] = CommitObservation(
        repo_path=repo_path,
        commit_sha=str(evt.payload["commit_sha"]),
        subject=str(evt.payload["subject"]),
        committed_at_ms=evt.ts_epoch_ms if committed_at_ms is None else committed_at_ms,
        truncated=bool(evt.payload.get("truncated", False)),
        skipped_count=_payload_int(evt, "skipped_count"),
    )


def _fold_power_transition(evt: Event) -> PowerTransition:
    """Build the transition record for one `mac.sleeping` / `mac.awake` row."""
    state: PowerState = "sleeping" if evt.type == "mac.sleeping" else "awake"
    ts_epoch_ms = _payload_int(evt, "ts_epoch_ms")
    return PowerTransition(
        state=state,
        ts_epoch_ms=evt.ts_epoch_ms if ts_epoch_ms is None else ts_epoch_ms,
        slept_for_ms=_payload_int(evt, "slept_for_ms"),
    )


def _fold_status_board(events: Iterable[Event]) -> StatusBoard:
    """Single-pass fold producing the Status Board projection.

    Open-action rule: `action.dispatched` opens an action, and any of the
    four terminal types closes it. `action.cancelled` is one of the four —
    an action closed by cancellation leaves the board, exactly as it
    leaves the supervisor sweep's pending map.
    """
    repos: dict[str, RepoObservation] = {}
    latest_commits: dict[str, CommitObservation] = {}
    power_transition: PowerTransition | None = None
    dispatched_ts_by_action: dict[str, int] = {}

    for evt in events:
        if evt.type == "repo.state_observed":
            _fold_repo_observation(repos, evt)
        elif evt.type == "project.commit_seen":
            _fold_commit_observation(latest_commits, evt)
        elif evt.type in ("mac.sleeping", "mac.awake"):
            power_transition = _fold_power_transition(evt)
        elif evt.type == "action.dispatched":
            dispatched_ts_by_action.setdefault(
                str(evt.payload["action_id"]), evt.ts_epoch_ms,
            )
        elif evt.type in _STATUS_BOARD_TERMINAL_ACTION_TYPES:
            dispatched_ts_by_action.pop(str(evt.payload["action_id"]), None)

    return StatusBoard(
        repos=repos,
        latest_commits=latest_commits,
        last_power_transition=power_transition,
        open_actions=tuple(
            OpenAction(action_id=action_id, dispatched_ts_ms=dispatched_ts)
            for action_id, dispatched_ts in dispatched_ts_by_action.items()
        ),
    )


# --- EntityRegistry -----------------------------------------------------------

# Alias cap per ADR-0011 D4 entry shape: "last 8, dedup". Bounds the
# payload the packet carries per entity — a file opened/resolved dozens
# of times under different phrasings must not grow the projection
# unboundedly.
_ALIAS_CAP: Final[int] = 8


@dataclass(frozen=True)
class EntityRegistryEntry:
    """One folded EntityRegistry row (ADR-0011 §3 D4).

    Attributes:
        entity_id: Deterministic natural key — ``"file:<abs-path>"`` |
            ``"repo:<abs-path>"`` | ``"task:<task-id>"``.
        entity_type: ``"file"`` | ``"repo"`` | ``"task"`` (open enum;
            ``"device"`` joins with the smart_home ADR).
        canonical: The resolved absolute path / task id.
        aliases: Raw refs that resolved to this entity, in first-seen
            order, deduped, bounded to the last `_ALIAS_CAP` (8).
        confidence: ``"exact"`` | ``"fuzzy"`` | ``"bookmark"`` |
            ``"config"``.
        source_event_id: `event_uid` of the event that (re)registered
            this entry, or `None` for a config-seeded row that no event
            has yet confirmed.
        last_seen_ms: `ts_epoch_ms` of the row that produced this entry
            (`0` for a config seed that has never been confirmed by an
            event).
    """

    entity_id: str
    entity_type: str
    canonical: str
    aliases: tuple[str, ...]
    confidence: str
    source_event_id: str | None
    last_seen_ms: int


def _add_alias(aliases: tuple[str, ...], new_ref: str) -> tuple[str, ...]:
    """Append `new_ref` to `aliases`, deduped, capped at the last 8.

    A ref already present is not re-appended — its original position is
    kept (order-stable). Once more than `_ALIAS_CAP` distinct refs have
    accumulated, the oldest is dropped so the tuple never exceeds the
    cap. An empty `new_ref` (no natural-language ref on the resolving
    event) is a no-op.
    """
    if not new_ref or new_ref in aliases:
        return aliases
    updated = (*aliases, new_ref)
    if len(updated) > _ALIAS_CAP:
        updated = updated[-_ALIAS_CAP:]
    return updated


@dataclass(frozen=True)
class EntityRegistry:
    """Folded EntityRegistry projection (ADR-0011 §3 D4).

    The Pre-action Gate's entity arm (ADR-0011 §12.2 Reconciliation J)
    consults this alongside the Task Ledger: a `target_entity_ref` that
    is not a known task id may still be trusted if it is a known
    `entity_id` here — the registry is precisely the projection of ids
    the trusted resolver produced (spec §3.3.7's ID-source allowlist).

    Attributes:
        entries_by_id: Frozen mapping of `entity_id -> EntityRegistryEntry`.
    """

    entries_by_id: Mapping[str, EntityRegistryEntry] = field(default_factory=dict)

    def __contains__(self, entity_id: object) -> bool:
        """Return True iff `entity_id` is a known entry key."""
        return entity_id in self.entries_by_id

    def get(self, entity_id: str) -> EntityRegistryEntry | None:
        """Return the entry for `entity_id`, or None if absent."""
        return self.entries_by_id.get(entity_id)

    def with_resolved_event(self, event: Event) -> EntityRegistry:
        """Return a new registry with one `entity.resolved` event folded in.

        ADR-0011 §12.2 MUST-FIX 1: `_dispatch_one_tool_call` emits the
        `entity.resolved` event into the log and then must gate on a
        registry that already reflects it — the packet snapshot it holds
        was assembled BEFORE that event existed, so a first-time file
        resolution would otherwise be refused by the gate even though it
        just succeeded. This overlays exactly that one event via
        `_entity_registry_entry_for_resolved_event` — the SAME helper
        `_fold_entity_registry`'s route 3 uses — so the result is
        byte-identical to what the next full `rebuild_projections` would
        produce. The event is already durable in the log; this only
        catches the projection up to it. It is not a widening of trust.

        A non-file / non-resolved / malformed event is a no-op: returns
        an equivalent (here, the same) registry.
        """
        update = _entity_registry_entry_for_resolved_event(self.entries_by_id, event)
        if update is None:
            return self
        entity_id, entry = update
        return EntityRegistry(entries_by_id={**self.entries_by_id, entity_id: entry})


def _entity_registry_entry_for_resolved_event(
    entries_by_id: Mapping[str, EntityRegistryEntry],
    evt: Event,
) -> tuple[str, EntityRegistryEntry] | None:
    """Compute the `(entity_id, entry)` update one `entity.resolved` event produces.

    Single source of truth for "how an `entity.resolved` event becomes an
    EntityRegistry entry" — shared by :func:`_fold_entity_registry`'s
    route 3 and :meth:`EntityRegistry.with_resolved_event`'s single-event
    overlay, so the two paths can never disagree.

    Returns `None` for a non-`entity.resolved` / non-`file` /
    non-`resolved` / malformed event — the caller then leaves its
    registry untouched.

    NIT-FIX 4(b): a config-seeded entry (`confidence="config"`) keeps
    that confidence when a later event re-registers the same
    `entity_id` — config is the most trusted provenance, and losing it
    on the first real resolution would make "was this a trusted config
    entry" unrecoverable from the projection. Everything else about the
    override (alias merge, `last_seen_ms`, `source_event_id`) proceeds
    normally.
    """
    if (
        evt.type != "entity.resolved"
        or evt.payload.get("entity_type") != "file"
        or evt.payload.get("outcome") != "resolved"
    ):
        return None
    resolved_to = evt.payload.get("resolved_to")
    if not isinstance(resolved_to, str):
        return None
    canonical = resolved_to.removeprefix("file:")
    natural_ref_raw = evt.payload.get("natural_ref", "")
    natural_ref = natural_ref_raw if isinstance(natural_ref_raw, str) else ""
    confidence_raw = evt.payload.get("confidence", "exact")
    confidence = confidence_raw if isinstance(confidence_raw, str) else "exact"
    existing = entries_by_id.get(resolved_to)
    if existing is not None and existing.confidence == "config":
        confidence = "config"
    aliases = _add_alias(existing.aliases if existing is not None else (), natural_ref)
    entry = EntityRegistryEntry(
        entity_id=resolved_to,
        entity_type="file",
        canonical=canonical,
        aliases=aliases,
        confidence=confidence,
        source_event_id=evt.event_uid,
        last_seen_ms=evt.ts_epoch_ms,
    )
    return resolved_to, entry


def _fold_entity_registry(
    events: Iterable[Event],
    task_records: Mapping[str, TaskLedgerRecord],
    entity_bookmarks: Sequence[tuple[str, str]],
) -> dict[str, EntityRegistryEntry]:
    """Single-pass fold producing the EntityRegistry (ADR-0011 D4).

    Three routes plus one seed, folded in this order so a real
    resolution always supersedes a mere config declaration:

    1. **Seed** — `entity_bookmarks` (alias, absolute-path) pairs from
       `config/file_targets.yaml`, folded FIRST at `confidence="config"`.
       The seed arrives as plain data because `jarvis.state` may not
       import `jarvis.execution.path_resolver` (layer boundary); the
       runtime composition root loads the config and passes the pairs.
    2. **Task Ledger** — `task_records` (already folded by the caller,
       so this route costs no extra event walk) → `task:` entries,
       `confidence="exact"`.
    3. **`repo.state_observed`** events → `repo:` entries; last row in
       log order wins (mirrors `_fold_repo_observation`).
    4. **`entity.resolved`** events with `entity_type="file"` and
       `outcome="resolved"` → `file:` entries, via
       `_entity_registry_entry_for_resolved_event` (the same helper
       `EntityRegistry.with_resolved_event` uses to overlay a single
       just-emitted event onto a stale registry — ADR-0011 §12.2
       MUST-FIX 1). `aliases` accumulates `natural_ref` via
       `_add_alias`; `source_event_id` is the resolving event's uid,
       superseding any earlier seed/event row for the same `entity_id`
       (except a `confidence="config"` row, which the helper keeps).
    """
    entries: dict[str, EntityRegistryEntry] = {}

    for alias, abs_path in entity_bookmarks:
        entity_id = f"file:{abs_path}"
        entries[entity_id] = EntityRegistryEntry(
            entity_id=entity_id,
            entity_type="file",
            canonical=abs_path,
            aliases=(alias,),
            confidence="config",
            source_event_id=None,
            last_seen_ms=0,
        )

    for task_id, record in task_records.items():
        entity_id = f"task:{task_id}"
        entries[entity_id] = EntityRegistryEntry(
            entity_id=entity_id,
            entity_type="task",
            canonical=task_id,
            aliases=(),
            confidence="exact",
            source_event_id=record.created_event_uid,
            last_seen_ms=record.created_ts_epoch_ms,
        )

    for evt in events:
        if evt.type == "repo.state_observed":
            repo_path = str(evt.payload["repo_path"])
            entity_id = f"repo:{repo_path}"
            entries[entity_id] = EntityRegistryEntry(
                entity_id=entity_id,
                entity_type="repo",
                canonical=repo_path,
                aliases=(),
                confidence="exact",
                source_event_id=evt.event_uid,
                last_seen_ms=evt.ts_epoch_ms,
            )
        elif evt.type == "entity.resolved":
            update = _entity_registry_entry_for_resolved_event(entries, evt)
            if update is not None:
                entity_id, entry = update
                entries[entity_id] = entry

    return entries


# --- PendingConfirmations ------------------------------------------------------

PendingConfirmationState = Literal[
    "pending",
    "accepted_unconsumed",
    "consumed",
    "rejected",
    "superseded",
]
"""Single-slot lifecycle state (ADR-0012 §3 D4).

Day-1's single-slot fold (:func:`_fold_pending_confirmations`) never
returns a slot whose CURRENT state is `"superseded"` — a new
`confirmation.requested` discards the old slot outright rather than
retaining it in a superseded state, since only one slot is ever held
(supersession is observable indirectly: a later accepted/rejected event
naming the discarded slot's `confirmation_id` no longer matches the
current slot and is ignored). `"superseded"` stays in the Literal for
D4 spec fidelity and so a future multi-slot fold (ADR-0012's defer
list) can produce it without widening this module's public surface —
mirrors `TaskStatus` carrying literal members the Day-1 fold does not
yet produce.
"""


@dataclass(frozen=True)
class PendingConfirmationSlot:
    """The one live-or-recent confirmation ask (ADR-0012 §3 D4).

    Attributes:
        confirmation_id: `confirmation_id` from the originating
            `confirmation.requested` event. The answer-path grammar
            hook (Step 6) binds "是"/"不要" to this id; an
            accepted/rejected event naming a different id does not
            move this slot (see :func:`_fold_pending_confirmations`).
        snapshot: The `action_snapshot` payload dict, stored verbatim —
            no transformation. It already excludes the write content
            (staged to an artifact per §3 D3; `args_meta` carries
            `content_artifact`, the staged path), so there is nothing
            left to strip.
        template_line: The exact rendered action line from
            `confirmation.requested.payload["template_line"]` — durable
            so consent binds to recorded machine truth (§3 D3).
        expires_at_ms: TTL deadline stamped by the ask path (Step 5;
            this projection never computes or defaults it). Compared
            against a caller-supplied `now_ms` at read time via
            :meth:`is_live` — never against a hidden clock read.
        state: Current lifecycle state. See `PendingConfirmationState`.
        accepted_event_uid: `event_uid` of the `confirmation.accepted`
            event that moved this slot to `accepted_unconsumed`, or
            `None` if it never has been. This is the join key D2.4's
            first half needs: a minted `AuthorizationLease`'s
            `source_confirmation_event_id` is set (D6) to exactly this
            uid, and Step 6's gate check matches the lease against this
            field to confirm "references a confirmation this
            projection shows as accepted" — a distinct check from
            `consumed_lease_ids`, which only prevents replaying an
            already-spent `lease_id` and has no way to validate that a
            lease's `source_confirmation_event_id` was ever a real
            acceptance in the first place.
    """

    confirmation_id: str
    snapshot: Mapping[str, Any]
    template_line: str
    expires_at_ms: int
    state: PendingConfirmationState
    accepted_event_uid: str | None = None

    def is_live(self, now_ms: int) -> bool:
        """True iff this slot is a still-pending, unexpired ask at `now_ms`.

        §3 D4's state enum has no `expired` member — expiry is judged
        here, at read time, against the caller-supplied `now_ms`, never
        stored and never read from a hidden clock (a caller-supplied
        `now_ms` keeps the fold pure and this check testable). Any
        state other than `"pending"` returns False regardless of
        `expires_at_ms` — an accepted, rejected, consumed, or
        superseded slot is not awaiting an answer, live or not.
        """
        return self.state == "pending" and now_ms < self.expires_at_ms


@dataclass(frozen=True)
class PendingConfirmations:
    """Folded PendingConfirmations projection (ADR-0012 §3 D4).

    Single-slot: the answer-path grammar hook (Step 6) always binds the
    newest ask. Folded from `confirmation.requested` /
    `confirmation.accepted` / `confirmation.rejected` plus
    `gate.evaluated` (lease consumption).

    Attributes:
        slot: The current slot, or None when no `confirmation.requested`
            has ever fired.
        consumed_lease_ids: Every `lease_id` a `gate.evaluated(outcome=
            "pass")` event has carried. D2.4's single-use check
            (Step 6, `gates.py`) tests `lease["lease_id"] in
            consumed_lease_ids` directly — a set, not slot-matching,
            because `gate.evaluated` carries `lease_id` but never
            `confirmation_id`, and leases are never stored (D2), so
            there is no persistent `lease_id -> confirmation_id`
            mapping to fold through. Stays correct if multi-slot
            pending ever lands.
    """

    slot: PendingConfirmationSlot | None = None
    consumed_lease_ids: frozenset[str] = frozenset()

    @classmethod
    def from_events(cls, events: Iterable[Event]) -> PendingConfirmations:
        """Fold `events` into a PendingConfirmations projection."""
        return _fold_pending_confirmations(events)


def _fold_pending_confirmations(events: Iterable[Event]) -> PendingConfirmations:
    """Single-pass fold producing the PendingConfirmations projection.

    Fold rules (ADR-0012 §3 D4, decisions pinned 2026-08-26):

    - `confirmation.requested` unconditionally replaces the slot with a
      fresh `state="pending"` row — the old slot (in whatever state) is
      discarded, not retained (single-slot; see `PendingConfirmationState`
      for why `"superseded"` is never the CURRENT slot's state under
      this rule).
    - `confirmation.accepted` / `confirmation.rejected` move the slot to
      `accepted_unconsumed` / `rejected` ONLY when the event's
      `confirmation_id` matches the current slot's — a stale answer
      naming a superseded ask's id does not match the (already
      replaced) current slot and is ignored, so it cannot resurrect it.
      The accepted branch also stamps `accepted_event_uid` with the
      accepted event's own `event_uid` — the D2.4 join key a minted
      lease's `source_confirmation_event_id` is matched against (Step
      6). `confirmation.rejected` gets no analogous field: D6 never
      mints a lease on rejection, so no lease could ever carry a uid
      pointing at a `confirmation.rejected` event — there is no
      consumer for that join key.
    - `gate.evaluated` with `outcome == "pass"` and a string `lease_id`
      payload key adds that id to `consumed_lease_ids` unconditionally
      (every passing leased gate evaluation is a real consumption, not
      just ones tied to the current slot), AND, only when the current
      slot is `accepted_unconsumed`, moves it to `consumed`
      (`accepted_event_uid` is left untouched by this transition — it
      still and forever names the event that authorized the now-spent
      slot).
    A non-`"pass"` outcome never consumes, even carrying a `lease_id` —
    a refused gate did not spend the lease.
    """
    slot: PendingConfirmationSlot | None = None
    consumed_lease_ids: set[str] = set()

    for evt in events:
        if evt.type == "confirmation.requested":
            slot = PendingConfirmationSlot(
                confirmation_id=str(evt.payload["confirmation_id"]),
                snapshot=evt.payload["action_snapshot"],
                template_line=str(evt.payload["template_line"]),
                expires_at_ms=int(evt.payload["expires_at_ms"]),
                state="pending",
                accepted_event_uid=None,
            )
        elif evt.type == "confirmation.accepted":
            if (
                slot is not None
                and str(evt.payload["confirmation_id"]) == slot.confirmation_id
            ):
                slot = replace(
                    slot, state="accepted_unconsumed", accepted_event_uid=evt.event_uid,
                )
        elif evt.type == "confirmation.rejected":
            if (
                slot is not None
                and str(evt.payload["confirmation_id"]) == slot.confirmation_id
            ):
                slot = replace(slot, state="rejected")
        elif evt.type == "gate.evaluated" and evt.payload.get("outcome") == "pass":
            lease_id = evt.payload.get("lease_id")
            if isinstance(lease_id, str) and lease_id:
                consumed_lease_ids.add(lease_id)
                if slot is not None and slot.state == "accepted_unconsumed":
                    slot = replace(slot, state="consumed")

    return PendingConfirmations(slot=slot, consumed_lease_ids=frozenset(consumed_lease_ids))


# --- ProjectionSet -----------------------------------------------------------


@dataclass(frozen=True)
class ProjectionSet:
    """Bundle of the projections produced by `rebuild_projections`.

    Frozen so that comparison / equality semantics work for the
    rebuild-idempotency test (acceptance D5 / E4).

    Attributes:
        task_ledger: Folded Task Ledger.
        recent_trace: Folded Recent Trace ring buffer.
        claim_evidence: Folded Claim/Evidence projection.
        status_board: Folded Status Board (ADR-0009 D6).
        entity_registry: Folded EntityRegistry (ADR-0011 D4).
        pending_confirmations: Folded PendingConfirmations (ADR-0012 D4).
    """

    task_ledger: TaskLedger
    recent_trace: RecentTrace
    claim_evidence: ClaimEvidenceProjection
    status_board: StatusBoard
    entity_registry: EntityRegistry
    pending_confirmations: PendingConfirmations


def rebuild_projections(
    conn: sqlite3.Connection,
    *,
    recent_trace_size: int = _RECENT_TRACE_DEFAULT_SIZE,
    entity_bookmarks: Sequence[tuple[str, str]] = (),
) -> ProjectionSet:
    """Read all events from `conn` and fold all six projections.

    One SELECT-driven read of the live Event Log via `iter_events(conn)`
    — materialized once, then walked by six separate in-memory fold
    passes (Claim/Evidence, Task Ledger, Recent Trace, EntityRegistry,
    Status Board, PendingConfirmations; the Task Ledger route inside
    EntityRegistry's fold reuses the already-folded records rather than
    re-walking, but the other five each make their own pass). No SQL
    writes, no projection tables touched. Idempotent: calling twice on
    the same connection (with no intervening `emit_event`) returns
    deep-equal `ProjectionSet`s.

    Args:
        conn: Open Event Log connection from
            `jarvis.state.event_log.open_event_log`.
        recent_trace_size: Ring-buffer capacity for the Recent Trace
            projection. Default = 200 (Day-1 per ADR § Module map).
        entity_bookmarks: `(alias, absolute-path)` seed pairs for the
            EntityRegistry's config route (ADR-0011 D4). `jarvis.state`
            may not import `jarvis.execution.path_resolver`, so the
            runtime composition root loads `config/file_targets.yaml`
            and passes the pairs down as plain data. Default `()` —
            no seed, matching every pre-ADR-0011 caller.

    Returns:
        `ProjectionSet` carrying frozen `task_ledger`, `recent_trace`,
        `claim_evidence`, `status_board`, `entity_registry`, and
        `pending_confirmations` projections.
    """
    materialized = list(iter_events(conn))
    claim_evidence = _fold_claim_evidence(materialized)
    task_ledger = TaskLedger(
        records_by_task_id=_fold_task_ledger_records(materialized),
        claim_evidence=claim_evidence,
    )
    recent_trace = RecentTrace.from_events(materialized, max_size=recent_trace_size)
    entity_registry = EntityRegistry(
        entries_by_id=_fold_entity_registry(
            materialized, task_ledger.records_by_task_id, entity_bookmarks,
        ),
    )
    return ProjectionSet(
        task_ledger=task_ledger,
        recent_trace=recent_trace,
        claim_evidence=claim_evidence,
        status_board=_fold_status_board(materialized),
        entity_registry=entity_registry,
        pending_confirmations=_fold_pending_confirmations(materialized),
    )


def make_snapshot(
    conn: sqlite3.Connection,
    *,
    entity_bookmarks: Sequence[tuple[str, str]] = (),
) -> ProjectionSet:
    """L3-facing alias for `rebuild_projections` (Day-1 identical).

    Stage 2 may introduce snapshot-vs-rebuild differentiation (e.g.,
    high-water-mark caching per spec §3.3.6); Day-1 keeps the contract
    surface simple by aliasing to a fresh fold every call.
    """
    return rebuild_projections(conn, entity_bookmarks=entity_bookmarks)


__all__ = [
    "ClaimEvidenceProjection",
    "CommitObservation",
    "EntityRegistry",
    "EntityRegistryEntry",
    "OpenAction",
    "PendingConfirmationSlot",
    "PendingConfirmationState",
    "PendingConfirmations",
    "PowerState",
    "PowerTransition",
    "ProjectionSet",
    "RecentTrace",
    "RepoObservation",
    "StatusBoard",
    "TaskId",
    "TaskLedger",
    "TaskLedgerRecord",
    "TaskLedgerSnapshot",
    "TaskStatus",
    "make_snapshot",
    "rebuild_projections",
]
