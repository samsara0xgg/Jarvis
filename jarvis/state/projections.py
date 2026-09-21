"""L2 Projections — Recent Trace, Status Board, admissions, confirmations (pure fold).

This module is **read-only** with respect to the Event Log. It SELECTs events
through `jarvis.state.event_log.iter_events` and folds them into in-memory
projection records in Python — it never INSERTs, UPDATEs, or DELETEs any row.
Canary H1 whitelists `event_log.py` as the only INSERT site; this module
participates by carrying zero SQL writes.

Projections produced:

1. **Recent Trace** — ring buffer of the most recent N events (Day-1: 200).

2. **Status Board** — ADR-0009 D6: latest observed state per watched
   repo (`repo.state_observed` + `project.commit_seen`), the last Mac
   power transition (`mac.sleeping` / `mac.awake`), and every action
   the log shows dispatched with no terminal event yet.

3. **ActionAdmissions** — ADR-0008 D10: the admitting gate uid of every
   non-terminal action.

4. **PendingConfirmations** — ADR-0012 D4: the live confirmation slot and
   the consumed lease ids.

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
from typing import TYPE_CHECKING, Any, Final, Literal

from jarvis.state.conversation import ConversationHistory, fold_conversation_history
from jarvis.state.event_log import iter_events

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable, Iterator, Mapping

    from jarvis.shared import Event


# --- Public types ------------------------------------------------------------

_RECENT_TRACE_DEFAULT_SIZE: Final[int] = 200


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


# --- ActionAdmissions fold (ADR-0008 D10) --------------------------------------


@dataclass
class _ActionFoldState:
    """Per-pass scratch for the admissions fold."""

    admissions: dict[str, ActionAdmission] = field(default_factory=dict)
    gate_uid_by_action: dict[str, str] = field(default_factory=dict)
    lease_by_action: dict[str, str] = field(default_factory=dict)

    def fold(self, evt: Event) -> None:
        """Fold one event's action-lifecycle contribution, if it has one."""
        if evt.type == "gate.evaluated":
            self._note_pre_action_pass(evt)
        elif evt.type == "action.dispatched":
            action_id = str(evt.payload["action_id"])
            self.admissions[action_id] = ActionAdmission(
                action_id=action_id,
                dispatched_event_uid=evt.event_uid,
                admission_gate_uid=self.gate_uid_by_action.get(action_id),
                lease_id=self.lease_by_action.get(action_id),
            )
        elif evt.type in _STATUS_BOARD_TERMINAL_ACTION_TYPES:
            self.admissions.pop(str(evt.payload["action_id"]), None)

    def _note_pre_action_pass(self, evt: Event) -> None:
        """Remember the latest passing pre-action verdict per `action_id`.

        ADR-0008 D10: the admission uid is read from `gate.evaluated` and
        not from `action.dispatched.source_event_id`, because the ordinary
        dispatch path sets no source there — only the confirmation-backed
        outbox path does.
        """
        if evt.payload.get("gate") != "pre_action" or evt.payload.get("outcome") != "pass":
            return
        action_id = evt.payload.get("action_id")
        if not isinstance(action_id, str):
            return
        self.gate_uid_by_action[action_id] = evt.event_uid
        lease_id = evt.payload.get("lease_id")
        if isinstance(lease_id, str):
            self.lease_by_action[action_id] = lease_id
        else:
            self.lease_by_action.pop(action_id, None)


def _fold_action_admissions(events: Iterable[Event]) -> ActionAdmissions:
    """Fold the admission record of every action still open on the log.

    An action is registered at `action.dispatched` — the same opener
    `StatusBoard.open_actions` uses — and evicted by any of
    `_STATUS_BOARD_TERMINAL_ACTION_TYPES`, so the set is exactly the
    non-terminal actions.
    """
    state = _ActionFoldState()
    for evt in events:
        state.fold(evt)
    return ActionAdmissions(by_action_id=state.admissions)


# --- ActionAdmissions (ADR-0008 D10) -------------------------------------------


@dataclass(frozen=True)
class ActionAdmission:
    """Durable provenance of one non-terminal action (ADR-0008 D10).

    Attributes:
        action_id: The action's stable id.
        dispatched_event_uid: `event_uid` of the `action.dispatched` row
            that opened the action.
        admission_gate_uid: `event_uid` of the last passing
            `gate.evaluated(gate="pre_action")` row for this action seen
            before its dispatch, or `None` when no passing verdict
            preceded it (a directly dispatched fixture action).
        lease_id: The `lease_id` that verdict carried, if any.
    """

    action_id: str
    dispatched_event_uid: str
    admission_gate_uid: str | None
    lease_id: str | None = None


@dataclass(frozen=True)
class ActionAdmissions:
    """Folded `action_id -> ActionAdmission` for every non-terminal action."""

    by_action_id: Mapping[str, ActionAdmission] = field(default_factory=dict)

    def get(self, action_id: str) -> ActionAdmission | None:
        """Return the admission record for `action_id`, or None if absent."""
        return self.by_action_id.get(action_id)

# --- PendingConfirmations ------------------------------------------------------

PendingConfirmationState = Literal[
    "pending",
    "accepted_unconsumed",
    "consumed",
    "rejected",
    "superseded",
    "expired",
]
"""Single-slot lifecycle state (ADR-0012 §3 D4, plus ADR-0014 D14).

`"expired"` is the one member D4's enum does not name: ADR-0014 D14
supersedes D4's "no expiry event, no timer" clause for a live
confirmation, and this fold moves the slot there when it folds the
durable `confirmation.expired` terminal that D14's terminalizer
appends. Read-time expiry is untouched and remains the truth in the
window between the deadline and the sweep tick that commits the row
(:meth:`PendingConfirmationSlot.is_live`, whose `state == "pending"`
test already returns False for this member without being edited).

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


def _pending_slot_from_requested(event: Event) -> PendingConfirmationSlot | None:
    payload = event.payload
    identity, snapshot = payload.get("confirmation_id"), payload.get("action_snapshot")
    template, expiry = payload.get("template_line"), payload.get("expires_at_ms")
    if (
        not isinstance(identity, str) or not identity
        or not isinstance(snapshot, dict) or not isinstance(template, str)
        or type(expiry) is not int
    ):
        return None  # A malformed successor cannot leave an older ask executable.
    return PendingConfirmationSlot(
        confirmation_id=identity, snapshot=snapshot, template_line=template,
        expires_at_ms=expiry, state="pending", accepted_event_uid=None,
    )


def _fold_pending_confirmations(events: Iterable[Event]) -> PendingConfirmations:  # noqa: C901 - a flat one-branch-per-event-type dispatch; merging the three id-matching terminals to satisfy the counter would hide that each has its own rule
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
    - `confirmation.expired` moves the slot to `expired` under exactly
      the accepted/rejected id-matching rule above: a durable expiry
      naming a superseded ask's id does not match the (already
      replaced) current slot and is ignored. It carries no analogous
      uid field — no lease is ever minted from an expiry (ADR-0014
      D14), so there is no join key to stamp.
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
            slot = _pending_slot_from_requested(evt)
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
        elif evt.type == "confirmation.expired":
            if (
                slot is not None
                and str(evt.payload["confirmation_id"]) == slot.confirmation_id
            ):
                slot = replace(slot, state="expired")
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
        recent_trace: Folded Recent Trace ring buffer.
        status_board: Folded Status Board (ADR-0009 D6).
        pending_confirmations: Folded PendingConfirmations (ADR-0012 D4).
        action_admissions: Folded ActionAdmissions (ADR-0008 D10) — the
            admitting gate uid and dispatched uid of every non-terminal
            action.
    """

    recent_trace: RecentTrace
    status_board: StatusBoard
    pending_confirmations: PendingConfirmations
    action_admissions: ActionAdmissions
    conversation_history: ConversationHistory = field(default_factory=ConversationHistory)


def rebuild_projections(
    conn: sqlite3.Connection,
    *,
    recent_trace_size: int = _RECENT_TRACE_DEFAULT_SIZE,
) -> ProjectionSet:
    """Read all events from `conn` and fold every projection.

    One SELECT-driven read of the live Event Log via `iter_events(conn)`
    — materialized once, then walked by separate in-memory fold passes.
    No SQL writes, no projection tables touched. Idempotent: calling
    twice on the same connection (with no intervening `emit_event`)
    returns deep-equal `ProjectionSet`s.

    Args:
        conn: Open Event Log connection from
            `jarvis.state.event_log.open_event_log`.
        recent_trace_size: Ring-buffer capacity for the Recent Trace
            projection. Default = 200 (Day-1 per ADR § Module map).
    """
    return fold_projections(list(iter_events(conn)), recent_trace_size=recent_trace_size)


def fold_projections(
    events: Iterable[Event],
    *,
    recent_trace_size: int = _RECENT_TRACE_DEFAULT_SIZE,
) -> ProjectionSet:
    """Fold a caller's transaction-pinned Event Log without any further SQL."""
    materialized = list(events)
    return ProjectionSet(
        recent_trace=RecentTrace.from_events(materialized, max_size=recent_trace_size),
        status_board=_fold_status_board(materialized),
        pending_confirmations=_fold_pending_confirmations(materialized),
        action_admissions=_fold_action_admissions(materialized),
        conversation_history=fold_conversation_history(materialized),
    )


def make_snapshot(conn: sqlite3.Connection) -> ProjectionSet:
    """L3-facing alias for `rebuild_projections` (Day-1 identical).

    Stage 2 may introduce snapshot-vs-rebuild differentiation (e.g.,
    high-water-mark caching per spec §3.3.6); Day-1 keeps the contract
    surface simple by aliasing to a fresh fold every call.
    """
    return rebuild_projections(conn)


__all__ = [
    "ActionAdmission",
    "ActionAdmissions",
    "CommitObservation",
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
    "make_snapshot",
    "rebuild_projections",
]
