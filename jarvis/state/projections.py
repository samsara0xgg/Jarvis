"""L2 Projections — Status Board, confirmations, clarification, conversation (pure fold).

This module is **read-only** with respect to the Event Log. It SELECTs events
through `jarvis.state.event_log.iter_events` and folds them into in-memory
projection records in Python — it never INSERTs, UPDATEs, or DELETEs any row.
Canary H1 whitelists `event_log.py` as the only INSERT site; this module
participates by carrying zero SQL writes.

Projections produced:

1. **Status Board** — ADR-0009 D6: latest observed state per watched
   repo (`repo.state_observed` + `project.commit_seen`), the last Mac
   power transition (`mac.sleeping` / `mac.awake`), and every action
   the log shows dispatched with no terminal event yet.

2. **PendingConfirmations** — ADR-0012 D4: the live confirmation slot and
   the consumed lease ids.

`rebuild_projections(conn)` is the single L3-facing entry point — it reads
all events from the connection and returns a frozen `ProjectionSet`.

Layer boundary (`.importlinter` + canary H13 Step 11): stdlib only plus
`jarvis.shared` and `jarvis.state.event_log`. No imports from
`jarvis.constitution`, `jarvis.decision`, `jarvis.execution`,
`jarvis.surface`, `jarvis.deployment`, `jarvis.runtime`, `jarvis.cli`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Final, Literal

from jarvis.shared.realtime import authorization_source, split_authorization_source
from jarvis.state.conversation import (
    ConversationFold,
    ConversationHistory,
    RefoldRequired,
    fold_conversation_history,
)
from jarvis.state.event_log import iter_events

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable

    from jarvis.shared import Event


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
    true about the tasks" (Task Ledger). Folds from the whole log or from a
    high-water mark (:class:`ProjectionFold`) with the same result.

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


@dataclass
class _BoardFoldState:
    """Per-pass scratch for the Status Board fold."""

    repos: dict[str, RepoObservation] = field(default_factory=dict)
    latest_commits: dict[str, CommitObservation] = field(default_factory=dict)
    power_transition: PowerTransition | None = None
    dispatched_ts_by_action: dict[str, int] = field(default_factory=dict)

    def copy(self) -> _BoardFoldState:
        """Independent containers; the observation records themselves are frozen."""
        return _BoardFoldState(
            dict(self.repos),
            dict(self.latest_commits),
            self.power_transition,
            dict(self.dispatched_ts_by_action),
        )

    def fold(self, evt: Event) -> None:
        """Open-action rule: `action.dispatched` opens an action, any terminal type closes it.

        `action.cancelled` is one of the four terminals, so an action closed
        by cancellation leaves the board exactly as it leaves the supervisor
        sweep's pending map.
        """
        if evt.type == "repo.state_observed":
            _fold_repo_observation(self.repos, evt)
        elif evt.type == "project.commit_seen":
            _fold_commit_observation(self.latest_commits, evt)
        elif evt.type in ("mac.sleeping", "mac.awake"):
            self.power_transition = _fold_power_transition(evt)
        elif evt.type == "action.dispatched":
            self.dispatched_ts_by_action.setdefault(
                str(evt.payload["action_id"]), evt.ts_epoch_ms,
            )
        elif evt.type in _STATUS_BOARD_TERMINAL_ACTION_TYPES:
            self.dispatched_ts_by_action.pop(str(evt.payload["action_id"]), None)

    def freeze(self) -> StatusBoard:
        """The board as an independent value (a later fold cannot reach into it)."""
        return StatusBoard(
            repos=dict(self.repos),
            latest_commits=dict(self.latest_commits),
            last_power_transition=self.power_transition,
            open_actions=tuple(
                OpenAction(action_id=action_id, dispatched_ts_ms=dispatched_ts)
                for action_id, dispatched_ts in self.dispatched_ts_by_action.items()
            ),
        )


def _fold_status_board(events: Iterable[Event]) -> StatusBoard:
    """Single-pass fold producing the Status Board projection."""
    state = _BoardFoldState()
    for evt in events:
        state.fold(evt)
    return state.freeze()


# --- PendingConfirmations ------------------------------------------------------

PendingConfirmationState = Literal[
    "pending",
    "accepted_unconsumed",
    "consumed",
    "rejected",
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

The single-slot fold (:func:`_fold_pending_confirmations`) holds one slot:
a new `confirmation.requested` discards the old slot outright (supersession
is observable indirectly: a later accepted/rejected event naming the
discarded slot's `confirmation_id` no longer matches the current slot and
is ignored).
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
        calls: The frozen calls of the card, stored verbatim from the
            `confirmation.requested` payload (ADR 0321): each is the
            six-key snapshot of ADR-0012 §3 D3 plus its own `question`.
            An event written before one card could hold several calls
            has the one `action_snapshot` instead, read here as a card
            of one call.
        template_line: The exact rendered action line from
            `confirmation.requested.payload["template_line"]` — durable
            so consent binds to recorded machine truth (§3 D3).
        expires_at_ms: TTL deadline stamped by the ask path (Step 5;
            this projection never computes or defaults it). ADR 0062: it
            bounds only a spoken or typed answer (:meth:`answers_by_words`);
            the card itself waits until it is answered or replaced.
            Compared against a caller-supplied `now_ms` at read time —
            never against a hidden clock read.
        state: Current lifecycle state. See `PendingConfirmationState`.
        accepted_event_uid: `event_uid` of the `confirmation.accepted`
            event that moved this slot to `accepted_unconsumed`, or
            `None` if it never has been. This is the join key D2.4's
            first half needs: a minted `AuthorizationLease`'s
            `source_confirmation_event_id` is set (D6) to this uid, or,
            for the card's later calls, to `uid#<index>`
            (:func:`jarvis.shared.realtime.authorization_source`), and
            Step 6's gate check matches the lease against
            :meth:`holds_lease_source` to confirm "references a
            confirmation this projection shows as accepted" — a distinct
            check from `consumed_lease_ids`, which only prevents
            replaying an already-spent `lease_id` and has no way to
            validate that a lease's `source_confirmation_event_id` was
            ever a real acceptance in the first place.
        leases_spent: How many passing leased gates the log holds since
            the acceptance. The slot is `consumed` when every call's
            lease is spent.
        utterances_since: How many user utterances (`surface.user_intent`
            / `utterance.received`) the log holds after the ask. The one
            being handled is already appended, so the first answer sees 1.
    """

    confirmation_id: str
    calls: tuple[Mapping[str, Any], ...]
    template_line: str
    expires_at_ms: int
    state: PendingConfirmationState
    accepted_event_uid: str | None = None
    utterances_since: int = 0
    leases_spent: int = 0

    def holds_lease_source(self, source: str) -> bool:
        """Whether ``source`` keys one call of this slot's acceptance (ADR 0321)."""
        if self.accepted_event_uid is None:
            return False
        uid, index = split_authorization_source(source)
        return uid == self.accepted_event_uid and index < len(self.calls) and (
            authorization_source(uid, index) == source
        )

    def is_live(self, now_ms: int) -> bool:  # noqa: ARG002 — callers pass their clock; ADR 0062 dropped the deadline here.
        """True iff this slot is a still-pending ask: its card waits (ADR 0062).

        Any state other than `"pending"` returns False — an accepted,
        rejected, consumed, or superseded slot is not awaiting an answer.
        A card never times out; see :meth:`answers_by_words` for the
        narrower window a spoken or typed yes/no has.
        """
        return self.state == "pending"

    def answers_by_words(self, now_ms: int) -> bool:
        """Whether a spoken or typed yes/no can answer this ask (ADR 0062, from ADR 0039).

        Only the first utterance after the ask, and only within its TTL:
        a later 「好」 meant for another question never fires the card.
        """
        return self.is_live(now_ms) and now_ms < self.expires_at_ms and self.utterances_since <= 1


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


def requested_calls(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """The frozen calls of a `confirmation.requested` payload; none when it is malformed.

    ADR 0321: `calls`, each with its own `question`. A row written before that holds the one
    `action_snapshot`, which is a card of one call.
    """
    calls = payload.get("calls")
    if isinstance(calls, list):
        return tuple(calls) if calls and all(isinstance(one, dict) for one in calls) else ()
    snapshot = payload.get("action_snapshot")
    return (snapshot,) if isinstance(snapshot, dict) else ()


def _pending_slot_from_requested(event: Event) -> PendingConfirmationSlot | None:
    payload = event.payload
    identity, calls = payload.get("confirmation_id"), requested_calls(payload)
    template, expiry = payload.get("template_line"), payload.get("expires_at_ms")
    if (
        not isinstance(identity, str) or not identity
        or not calls or not isinstance(template, str)
        or type(expiry) is not int
    ):
        return None  # A malformed successor cannot leave an older ask executable.
    return PendingConfirmationSlot(
        confirmation_id=identity, calls=calls, template_line=template,
        expires_at_ms=expiry, state="pending", accepted_event_uid=None,
    )


def _fold_pending_confirmations(  # noqa: C901, PLR0912 - a flat one-branch-per-event-type dispatch; merging the three id-matching terminals to satisfy the counter would hide that each has its own rule
    events: Iterable[Event],
    start: PendingConfirmations | None = None,
) -> PendingConfirmations:
    """Single-pass fold producing the PendingConfirmations projection.

    ``start`` is the projection folded from the log's earlier events: the
    result is the same as folding all of them in one pass, because this value
    is its own fold state.

    Fold rules (ADR-0012 §3 D4, decisions pinned 2026-08-26):

    - `confirmation.requested` unconditionally replaces the slot with a
      fresh `state="pending"` row — the old slot (in whatever state) is
      discarded, not retained (single-slot; see `PendingConfirmationState`
      for why `"superseded"` is never the CURRENT slot's state under
      this rule).
    - `surface.user_intent` / `utterance.received` count into a pending
      slot's `utterances_since` (ADR 0062: only the first answers by words).
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
      once every call of the card has spent its lease (ADR 0321: one
      lease per call) (`accepted_event_uid` is left untouched by this
      transition — it still and forever names the event that
      authorized the now-spent slot).
    A non-`"pass"` outcome never consumes, even carrying a `lease_id` —
    a refused gate did not spend the lease.
    """
    start = start or PendingConfirmations()
    slot = start.slot
    consumed_lease_ids = set(start.consumed_lease_ids)

    for evt in events:
        if evt.type == "confirmation.requested":
            slot = _pending_slot_from_requested(evt)
        elif evt.type in ("surface.user_intent", "utterance.received"):
            if slot is not None and slot.state == "pending":
                slot = replace(slot, utterances_since=slot.utterances_since + 1)
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
                    spent = slot.leases_spent + 1
                    slot = replace(
                        slot, leases_spent=spent,
                        state="consumed" if spent >= len(slot.calls) else slot.state,
                    )

    return PendingConfirmations(slot=slot, consumed_lease_ids=frozenset(consumed_lease_ids))


# --- PendingClarification (ADR 0066) ------------------------------------------

CLARIFICATION_EVENT_TYPES: Final[tuple[str, ...]] = (
    "clarification.requested",
    "clarification.withdrawn",
    "surface.clarified",
    "surface.dismissed",
    "surface.user_intent",
    "utterance.received",
)
"""Everything the ask-card fold reads, for readers that fold it without the full log."""


@dataclass(frozen=True)
class PendingClarification:
    """The newest ask card (ADR 0066): what the model asked, and whether it is still up.

    Attributes:
        clarification_id: Id from its `clarification.requested`.
        question: The one-sentence question on the card.
        fields: The card's fields as the tool stored them (label, choices,
            value, remember).
        asked_turn_id: The turn that put the card up.
        closed: A `surface.clarified`, `surface.dismissed` or
            `clarification.withdrawn` naming this id has landed.
        dismissed: It was closed with its close button, unanswered.
        answered_turn_id: The turn Allen's filled-in answers started, or
            ``None`` when he never submitted it.
        answered_labels: Labels of the fields he filled in; the rest he left
            blank.
        utterances_since: User utterances (`surface.user_intent` /
            `utterance.received`) after the ask while it was open, or after
            it was dismissed (counted anew from the dismissal). The one being
            handled is already appended, so the first one sees 1. They do not
            close the card (ADR 0206).
        trip: A `bus` plan's rows and modes (ADR 0205): the card is a bus
            card, not a question.
    """

    clarification_id: str
    question: str
    fields: tuple[Mapping[str, Any], ...]
    asked_turn_id: str = ""
    closed: bool = False
    dismissed: bool = False
    answered_turn_id: str | None = None
    answered_labels: tuple[str, ...] = ()
    utterances_since: int = 0
    trip: Mapping[str, Any] | None = None

    @property
    def waiting(self) -> bool:
        """The card is on screen: not answered, dismissed, replaced or taken down (ADR 0206)."""
        return not self.closed

    @property
    def spoken_over(self) -> bool:
        """The card is still up and he has spoken since it went up (ADR 0206)."""
        return not self.closed and self.utterances_since > 0

    @property
    def just_dismissed(self) -> bool:
        """The turn being handled is the first utterance after the card was dismissed."""
        return self.dismissed and self.utterances_since == 1

    @classmethod
    def from_events(cls, events: Iterable[Event]) -> PendingClarification | None:
        """Fold `events` (at least :data:`CLARIFICATION_EVENT_TYPES`) into the newest card."""
        return _fold_pending_clarification(events)


def _fold_pending_clarification(
    events: Iterable[Event],
    start: PendingClarification | None = None,
) -> PendingClarification | None:
    """A newer ask replaces the slot; an answer, dismissal or take-down closes it by its id.

    ``start`` is the card folded from the log's earlier events; the card is its own fold state.
    """
    slot = start
    for evt in events:
        if evt.type == "clarification.requested":
            fields = evt.payload.get("fields")
            slot = PendingClarification(
                clarification_id=str(evt.payload["clarification_id"]),
                question=str(evt.payload.get("question", "")),
                asked_turn_id=str(evt.payload.get("turn_id", "")),
                trip=evt.payload.get("trip"),
                fields=(
                    tuple(f for f in fields if isinstance(f, Mapping))
                    if isinstance(fields, list) else ()
                ),
            )
        elif slot is None:
            continue
        elif evt.type == "clarification.withdrawn":  # she took it down (ADR 0206), not his close
            if not slot.closed and evt.payload.get("clarification_id") == slot.clarification_id:
                slot = replace(slot, closed=True)
        elif evt.type in ("surface.clarified", "surface.dismissed"):
            if not slot.closed and evt.payload.get("clarification_id") == slot.clarification_id:
                dismissed = evt.type == "surface.dismissed"
                answered = None if dismissed else str(evt.payload["turn_id"])
                answers = evt.payload.get("answers")
                slot = replace(
                    slot, closed=True, dismissed=dismissed, answered_turn_id=answered,
                    answered_labels=tuple(answers) if isinstance(answers, Mapping) else (),
                    utterances_since=0 if dismissed else slot.utterances_since,  # count anew
                )
        elif evt.type in ("surface.user_intent", "utterance.received") and (
            not slot.closed or slot.dismissed
        ):
            slot = replace(slot, utterances_since=slot.utterances_since + 1)
    return slot


# --- ProjectionSet -----------------------------------------------------------


@dataclass(frozen=True)
class ProjectionSet:
    """Bundle of the projections produced by `rebuild_projections`.

    Frozen so that comparison / equality semantics work for the
    rebuild-idempotency test (acceptance D5 / E4).

    Attributes:
        status_board: Folded Status Board (ADR-0009 D6).
        pending_confirmations: Folded PendingConfirmations (ADR-0012 D4).
        conversation_history: Folded conversation window.
        pending_clarification: The newest ask card (ADR 0066), or None.
    """

    status_board: StatusBoard
    pending_confirmations: PendingConfirmations
    conversation_history: ConversationHistory = field(default_factory=ConversationHistory)
    pending_clarification: PendingClarification | None = None


class ProjectionFold:
    """Every projection folded up to some event, advanced by returning a new value.

    The incremental twin of :func:`fold_projections`: ``empty().advance(a)
    .advance(b)`` yields the same :class:`ProjectionSet` as folding ``a + b`` in
    one pass, wherever the log is split. A fold is never mutated once it is
    shared: :meth:`advance` copies the containers it will touch and shares the
    rest, so any number of threads can hold and advance one value.
    """

    __slots__ = ("_board", "_clarification", "_confirmations", "_conversation")

    def __init__(self) -> None:
        """The projections of an empty log."""
        self._board = _BoardFoldState()
        self._confirmations = PendingConfirmations()
        self._clarification: PendingClarification | None = None
        self._conversation = ConversationFold()

    def advance(self, events: Iterable[Event]) -> ProjectionFold:
        """The fold after ``events``, which must follow this fold's last event in log order.

        Raises :class:`RefoldRequired` when the conversation window cannot
        absorb them (see :class:`ConversationFold`); the caller folds the whole log.
        """
        fresh = tuple(events)
        if not fresh:
            return self
        twin = ProjectionFold()
        board = self._board.copy()
        conversation = self._conversation.copy()
        for evt in fresh:
            board.fold(evt)
            conversation.apply(evt)
        twin._board = board
        twin._conversation = conversation
        twin._confirmations = _fold_pending_confirmations(fresh, self._confirmations)
        twin._clarification = _fold_pending_clarification(fresh, self._clarification)
        return twin

    def projections(self) -> ProjectionSet:
        """The folded projections, as independent values."""
        return ProjectionSet(
            status_board=self._board.freeze(),
            pending_confirmations=self._confirmations,
            conversation_history=self._conversation.freeze(),
            pending_clarification=self._clarification,
        )


def rebuild_projections(conn: sqlite3.Connection) -> ProjectionSet:
    """Read all events from `conn` and fold every projection.

    One SELECT-driven read of the live Event Log via `iter_events(conn)`
    — materialized once, then walked by separate in-memory fold passes.
    No SQL writes, no projection tables touched. Idempotent: calling
    twice on the same connection (with no intervening `emit_event`)
    returns deep-equal `ProjectionSet`s.

    Args:
        conn: Open Event Log connection from
            `jarvis.state.event_log.open_event_log`.
    """
    return fold_projections(list(iter_events(conn)))


def fold_projections(events: Iterable[Event]) -> ProjectionSet:
    """Fold a caller's transaction-pinned Event Log without any further SQL."""
    materialized = list(events)
    return ProjectionSet(
        status_board=_fold_status_board(materialized),
        pending_confirmations=_fold_pending_confirmations(materialized),
        conversation_history=fold_conversation_history(materialized),
        pending_clarification=_fold_pending_clarification(materialized),
    )


__all__ = [
    "CLARIFICATION_EVENT_TYPES",
    "CommitObservation",
    "OpenAction",
    "PendingClarification",
    "PendingConfirmationSlot",
    "PendingConfirmationState",
    "PendingConfirmations",
    "PowerState",
    "PowerTransition",
    "ProjectionFold",
    "ProjectionSet",
    "RefoldRequired",
    "RepoObservation",
    "StatusBoard",
    "rebuild_projections",
    "requested_calls",
]
