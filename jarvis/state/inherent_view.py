"""Inherent view fold — ADR-0014 D8/D9/D13/D14/D16 (L2).

The v2 panel's durable truth is derived here and nowhere else: one
:class:`InherentView` folds the Inherent-relevant Event Log rows in
``events.id`` order and answers two questions the runtime sequencer asks —
"what changed on this row?" (:class:`ViewTransition`) and "what does a new
client need to see?" (:class:`InherentViewCheckpoint`).  Both answers are
immutable values; the sequencer owns the scan watermark and the wake, the
presenter owns the wire shape, and neither re-derives what this fold holds
(D9: "L2 owns all fold logic and immutable projection snapshots").

Three sections of truth are folded:

- **response groups** from ``surface.response_{open,chunk,emitted}``, with
  each response's L3 lifecycle joined from the ``response.*`` rows that name
  it — reported from committed events, never assumed;
- the bounded **action projection** (D13) from the eight registered
  ``action.*`` lifecycle types plus ``worker.quiesced`` and the cleanup pair,
  carrying each action's canonical state, its cleanup state and — folded but
  never serialized, because the shipped ``ActionUpsert`` has no slot for it —
  the A5 :class:`CancelRequestView`;
- the single globally unique **confirmation slot** (D14) from
  ``confirmation.requested`` / ``.accepted`` / ``.rejected`` /
  ``.expired``.  Expiry clears the slot two ways, and both report reason
  ``expired``: lazily, against the timestamp of the rows folded past the
  deadline (``PendingConfirmationSlot.is_live`` evaluated at fold time),
  and durably, from the ``confirmation.expired`` row the D14 terminalizer
  appends — the one an idle panel with no further traffic depends on.

Three bounds live here because they bound the *truth kept*, not the bytes
sent (D16, and D13's "bounded ActionViewProjection"):

- at most :data:`RECENT_TERMINAL_GROUP_LIMIT` terminal groups are retained,
  oldest evicted first, so a snapshot carries every open group plus a bounded
  recent history and the fold never grows with the log;
- at most :data:`RECENT_TERMINAL_ACTION_LIMIT` actions in a canonical terminal
  are retained, oldest evicted first, for the same reason, and an action the
  Pre-action Gate refused is retired at its verdict — L3 dispatches nothing
  after a non-``pass`` outcome, so it would otherwise sit in every snapshot
  as ``proposed`` for the life of the log;
- a closed response whose segments exceed :data:`INLINE_DOCUMENT_BUDGET_BYTES`
  of UTF-8 keeps a whole-segment prefix as its preview plus a
  :class:`DocumentReference` naming the durable row that carries the complete
  body.  Open responses are never cut: a live client extends them by
  ``sequence`` and a trimmed prefix would only send it into a resync loop.

A row produces a transition — exactly one durable envelope — when it is a
response row carrying a ``response_id`` (even when nothing changed: a
duplicate open, a late or repeated segment) or when it moved something the
wire carries.  A row that moves only fold-internal truth — an action's
cleanup state, the cancel request's own state machine, a lifecycle for a
response this fold never opened, the D21 correlation — advances the cursor
and produces nothing: no wire field changed, and an empty durable envelope
would only cost the client an ACK.

Layer rules (L2): stdlib plus ``jarvis.shared`` for the one migration-stable
group-id derivation.  The fold takes plain row fields rather than an
``Event``, so it names no layer package at all.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final, Literal

from jarvis.shared.realtime import stable_response_group_id

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

RESPONSE_EVENT_TYPES: Final[tuple[str, ...]] = (
    "surface.response_open",
    "surface.response_chunk",
    "surface.response_emitted",
)
RESPONSE_LIFECYCLE_EVENT_TYPES: Final[tuple[str, ...]] = (
    "response.started",
    "response.completed",
    "response.cancelled",
    "response.failed",
)
ACTION_EVENT_TYPES: Final[tuple[str, ...]] = (
    "action.proposed",
    "action.authorized",
    "action.dispatched",
    "action.running",
    "action.result_observed",
    "action.timeout_assumed",
    "action.failed",
    "action.cancelled",
)
CLEANUP_EVENT_TYPES: Final[tuple[str, ...]] = (
    "worker.quiesced",
    "action.cleanup_completed",
    "action.cleanup_failed",
)
CONFIRMATION_EVENT_TYPES: Final[tuple[str, ...]] = (
    "confirmation.requested",
    "confirmation.accepted",
    "confirmation.rejected",
    "confirmation.expired",
)
INPUT_CORRELATION_TYPE: Final[str] = "surface.user_intent"
GATE_EVENT_TYPE: Final[str] = "gate.evaluated"
FOLD_EVENT_TYPES: Final[tuple[str, ...]] = (
    *RESPONSE_EVENT_TYPES,
    *RESPONSE_LIFECYCLE_EVENT_TYPES,
    *ACTION_EVENT_TYPES,
    *CLEANUP_EVENT_TYPES,
    *CONFIRMATION_EVENT_TYPES,
    INPUT_CORRELATION_TYPE,
    GATE_EVENT_TYPE,
)
"""Every type this fold reads; the sequencer's row query selects exactly these."""

INLINE_DOCUMENT_BUDGET_BYTES: Final[int] = 16384
RECENT_TERMINAL_GROUP_LIMIT: Final[int] = 20
RECENT_TERMINAL_ACTION_LIMIT: Final[int] = 20
PENDING_REQUEST_LIMIT: Final[int] = 64
CANCEL_TOOL_NAME: Final[str] = "cancel_action"

PanelStream = Literal["open", "closed"]
ResponseLifecycle = Literal["generating", "completed", "cancelled", "failed"]
ActionCanonicalState = Literal[
    "proposed",
    "authorized",
    "dispatched",
    "running",
    "result_observed",
    "timeout_assumed",
    "failed",
    "cancelled",
]
CleanupState = Literal["none", "quiesced", "completed", "quarantined"]
CancelRequestState = Literal[
    "received", "authorized", "quiescing", "rejected", "failed", "resolved",
]
ConfirmationClearReason = Literal["accepted", "rejected", "expired", "superseded"]
_CLEAR_REASON_OF_TYPE: Final[Mapping[str, ConfirmationClearReason]] = {
    "confirmation.accepted": "accepted",
    "confirmation.rejected": "rejected",
    "confirmation.expired": "expired",
}
ChangeKind = Literal[
    "response.opened",
    "response.segment",
    "response.delivery",
    "response.lifecycle",
    "action.upsert",
    "confirmation.upsert",
    "confirmation.cleared",
]

_PHASES: Final[frozenset[str]] = frozenset({"commentary", "final"})
_CHANNELS: Final[frozenset[str]] = frozenset({"speech", "document", "both"})
_DEFAULT_PHASE: Final[str] = "final"
_DEFAULT_CHANNEL: Final[str] = "document"
_DEFAULT_ACTION_TYPE: Final[str] = "unknown"
_CONFIRMATION_OPTIONS: Final[tuple[str, ...]] = ("accept", "reject")
# The row's own type is the canonical state, one-to-one with the shipped
# Swift ``ActionCanonicalState``; the four below are its terminals, which is
# the same open-set rule ``_ActionFoldState.fold`` applies in projections.py.
_ACTION_STATE_OF_TYPE: Final[dict[str, ActionCanonicalState]] = {
    "action.proposed": "proposed",
    "action.authorized": "authorized",
    "action.dispatched": "dispatched",
    "action.running": "running",
    "action.result_observed": "result_observed",
    "action.timeout_assumed": "timeout_assumed",
    "action.failed": "failed",
    "action.cancelled": "cancelled",
}
_TERMINAL_ACTION_STATES: Final[frozenset[str]] = frozenset(
    {"result_observed", "timeout_assumed", "failed", "cancelled"},
)
# Why the target stopped, as the CancelRequestView's ``reason_code`` (D13).
_CANCEL_RESOLUTION_OF_STATE: Final[dict[str, str]] = {
    "cancelled": "cancelled",
    "result_observed": "completed_before_cancel",
    "failed": "failed",
    "timeout_assumed": "failed",
}
_CANCEL_SETTLED_STATES: Final[frozenset[str]] = frozenset({"rejected", "resolved"})
_CANCEL_LIVE_STATES: Final[frozenset[str]] = frozenset({"received", "authorized", "quiescing"})
_DEFAULT_RISK: Final[str] = "unknown"
_CLEANUP_STATE_OF_TYPE: Final[dict[str, CleanupState]] = {
    "worker.quiesced": "quiesced",
    "action.cleanup_completed": "completed",
    "action.cleanup_failed": "quarantined",
}
_LIFECYCLE_OF_TYPE: Final[dict[str, ResponseLifecycle]] = {
    "response.started": "generating",
    "response.completed": "completed",
    "response.cancelled": "cancelled",
    "response.failed": "failed",
}


@dataclass(frozen=True)
class SegmentView:
    """One permitted panel segment of a response (D10 ``response.segment``)."""

    sequence: int
    phase: str
    channel: str
    text: str
    segment_hash: str
    truncated: bool = False


@dataclass(frozen=True)
class DocumentReference:
    """Durable pointer to a body the view does not inline (D16).

    ``event_uid`` names the ``surface.response_emitted`` row, which carries
    the complete text; a later card's fetch path resolves it.
    """

    response_id: str
    event_uid: str
    utf8_bytes: int


@dataclass(frozen=True)
class ResponseView:
    """One response's panel-stream truth (D3 ``ResponseState``, panel half)."""

    response_id: str
    response_group_id: str
    turn_id: str
    phase: str
    channel: str
    question: str | None
    created_at_ms: int
    revision: int
    panel_stream: PanelStream
    segments: tuple[SegmentView, ...] = ()
    document_reference: DocumentReference | None = None
    source_client_request_id: str | None = None
    #: The L3 ResponseRun lifecycle, from the ``response.*`` rows naming this
    #: response.  A response opened through the legacy uuid5 binding has no
    #: such row and stays ``generating``: ``surface.response_emitted`` is
    #: delivery production truth and never stands in for a terminal (§16.9).
    lifecycle: ResponseLifecycle = "generating"
    terminal_reason: str | None = None


@dataclass(frozen=True)
class ResponseGroupView:
    """One response group: sibling responses sharing a turn (D3, D26)."""

    response_group_id: str
    turn_id: str
    question: str | None
    created_at_ms: int
    opened_cursor: int
    last_cursor: int
    responses: tuple[ResponseView, ...]
    source_client_request_id: str | None = None

    @property
    def terminal(self) -> bool:
        """Report whether every sibling's panel stream is closed."""
        return all(response.panel_stream == "closed" for response in self.responses)


@dataclass(frozen=True)
class CancelRequestView:
    """One A5 cancel request's own state machine (D13).

    Keyed by the request's own ``action_id`` (``CancelActionRequest.request_id``)
    and held against its target.  "Cancel requested" never enters the canonical
    eight-state ``ActionLifecycle``: only ``action.cancelled`` moves the
    target's own state.  L5 sends four of these fields as the ``ActionUpsert``
    ``cancel_request`` object; ``target_action_id`` and ``proposed_event_uid``
    stay inside the fold.
    """

    request_id: str
    target_action_id: str
    state: CancelRequestState
    revision_cursor: int
    #: The request's ``action.proposed`` row, which its ``gate.evaluated``
    #: verdict names in ``events.source_event_id``.
    proposed_event_uid: str
    reason_code: str | None = None


@dataclass(frozen=True)
class ActionView:
    """One registered action's panel truth (D13 ``ActionView``).

    It holds no raw tool arguments, no secrets, no unbounded stdout, no lease
    and no unverified tool self-report: ``failure_code`` is the reason code of
    a terminal, never its free-text ``error``.
    """

    action_id: str
    action_type: str
    canonical_state: ActionCanonicalState
    state_revision_cursor: int
    updated_at_ms: int
    task_id: str | None = None
    response_group_id: str | None = None
    started_at_ms: int | None = None
    safe_target_ref: str | None = None
    result_available: bool = False
    failure_code: str | None = None
    cleanup_state: CleanupState = "none"
    #: D13 ``cancellable_hint``, computed here because L5 never derives it.
    cancellable: bool = False
    cancel_request: CancelRequestView | None = None


@dataclass(frozen=True)
class ConfirmationView:
    """The one live confirmation ask (D14), mirroring ``PendingConfirmations``.

    Nothing else from the ``action_snapshot`` reaches this value: no
    ``args_meta``, no ``content_artifact``, no lease.
    """

    confirmation_id: str
    summary: str
    risk: str
    expires_at_ms: int
    revision: int
    options: tuple[str, ...] = _CONFIRMATION_OPTIONS
    target: str | None = None
    action_id: str | None = None
    response_group_id: str | None = None


@dataclass(frozen=True)
class ConfirmationCleared:
    """Why and at which cursor the confirmation slot emptied (D14)."""

    confirmation_id: str
    reason: ConfirmationClearReason
    revision: int


@dataclass(frozen=True)
class InherentViewCheckpoint:
    """The bounded, immutable state a snapshot is built from (D8 step 1).

    ``pending_requests`` carries the D21 ``turn_id -> source_client_request_id``
    entries whose group has not opened yet, so a catch-up re-fold from this
    checkpoint stamps the same correlation a live client saw.
    """

    through_cursor: int
    groups: tuple[ResponseGroupView, ...]
    pending_requests: tuple[tuple[str, str], ...] = ()
    actions: tuple[ActionView, ...] = ()
    pending_confirmation: ConfirmationView | None = None


@dataclass(frozen=True)
class ViewChange:
    """One typed mutation; exactly one payload field is set per ``kind``."""

    kind: ChangeKind
    response: ResponseView | None = None
    segment: SegmentView | None = None
    action: ActionView | None = None
    confirmation: ConfirmationView | None = None
    cleared: ConfirmationCleared | None = None


@dataclass(frozen=True)
class ViewTransition:
    """What one relevant row changed: exactly one per relevant row (D6)."""

    cursor: int
    event_uid: str
    changes: tuple[ViewChange, ...]


def _string(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def _enum(payload: Mapping[str, Any], key: str, allowed: frozenset[str], default: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) and value in allowed else default


def _ack_status(payload: Mapping[str, Any]) -> str | None:
    """Read only ``status`` out of a ``cancel_action`` ack's ``tool_output``.

    Nothing else in that JSON — and no free text anywhere — reaches the view.
    """
    raw = payload.get("tool_output")
    if not isinstance(raw, str):
        return None
    try:
        decoded = json.loads(raw)
    except ValueError:
        return None
    return _string(decoded, "status") if isinstance(decoded, dict) else None


def _confirmation_of_request(
    cursor: int, payload: Mapping[str, Any], correlation: Mapping[str, Any] | None,
) -> ConfirmationView | None:
    """Build the slot from one ``confirmation.requested`` row, or None if malformed.

    ``summary`` is the runtime-rendered ``template_line`` (ADR-0012 D5's fixed
    vocabulary, never LLM text); the ask's identity, target and risk are the
    only things read out of ``action_snapshot``.
    """
    identity = _string(payload, "confirmation_id")
    snapshot = payload.get("action_snapshot")
    template = _string(payload, "template_line")
    expiry = payload.get("expires_at_ms")
    if identity is None or template is None or not isinstance(snapshot, dict):
        return None
    if type(expiry) is not int:
        return None
    turn_id = None if correlation is None else _string(correlation, "turn_id")
    return ConfirmationView(
        confirmation_id=identity,
        summary=template,
        risk=_string(snapshot, "risk_level") or _DEFAULT_RISK,
        expires_at_ms=expiry,
        revision=cursor,
        target=_string(snapshot, "canonical_target"),
        action_id=None if correlation is None else _string(correlation, "action_id"),
        response_group_id=None if turn_id is None else stable_response_group_id(turn_id),
    )


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _cut_utf8(text: str, limit: int) -> str:
    """Truncate ``text`` to at most ``limit`` UTF-8 bytes on a character boundary."""
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def bound_document(response: ResponseView, *, event_uid: str) -> ResponseView:
    """Apply the inline budget to a closed response (D16).

    A body at or under :data:`INLINE_DOCUMENT_BUDGET_BYTES` is kept whole.
    Above it, the longest whole-segment prefix that fits is the preview; when
    even the first segment does not fit, that segment is cut on a character
    boundary and flagged ``truncated`` while keeping its durable hash.

    Args:
        response: The response whose panel stream just closed.
        event_uid: The closing ``surface.response_emitted`` row, which carries
            the complete body the reference points at.

    Returns:
        The response unchanged, or with a bounded preview plus reference.
    """
    total = sum(_utf8_len(segment.text) for segment in response.segments)
    if total <= INLINE_DOCUMENT_BUDGET_BYTES:
        return response
    kept: list[SegmentView] = []
    used = 0
    for segment in response.segments:
        size = _utf8_len(segment.text)
        if used + size > INLINE_DOCUMENT_BUDGET_BYTES:
            break
        kept.append(segment)
        used += size
    if not kept:
        first = response.segments[0]
        preview = _cut_utf8(first.text, INLINE_DOCUMENT_BUDGET_BYTES)
        kept.append(replace(first, text=preview, truncated=True))
    reference = DocumentReference(
        response_id=response.response_id,
        event_uid=event_uid,
        utf8_bytes=total,
    )
    return replace(response, segments=tuple(kept), document_reference=reference)


class InherentView:
    """The mutable fold behind the immutable views.

    One instance lives inside the runtime sequencer for the daemon's lifetime;
    :meth:`from_checkpoint` builds a second, private one for a client's
    post-ACK catch-up (D8 step 5) so the same fold rules produce the same
    deltas the client would have seen live.
    """

    def __init__(
        self,
        *,
        through_cursor: int = 0,
        groups: Iterable[ResponseGroupView] = (),
        pending_requests: Iterable[tuple[str, str]] = (),
        actions: Iterable[ActionView] = (),
        pending_confirmation: ConfirmationView | None = None,
    ) -> None:
        """Start from ``groups`` and ``actions`` as truth folded through ``through_cursor``."""
        self._through_cursor = through_cursor
        self._request_of_turn: dict[str, str] = dict(pending_requests)
        self._groups: dict[str, ResponseGroupView] = {
            group.response_group_id: group for group in groups
        }
        self._group_of: dict[str, str] = {
            response.response_id: group.response_group_id
            for group in self._groups.values()
            for response in group.responses
        }
        self._actions: dict[str, ActionView] = {}
        # ``cancellable`` is exactly "dispatched seen, no terminal seen", so a
        # restored action that is still cancellable is one whose dispatch this
        # fold saw; a terminal one can never become cancellable again.
        self._dispatched: set[str] = set()
        self._cancels: dict[str, CancelRequestView] = {}
        self._cancel_of_target: dict[str, str] = {}
        for action in actions:
            self._actions[action.action_id] = replace(
                action, cancellable=False, cancel_request=None,
            )
            if action.cancellable:
                self._dispatched.add(action.action_id)
            if action.cancel_request is not None:
                self._cancels[action.cancel_request.request_id] = action.cancel_request
                self._cancel_of_target[action.action_id] = action.cancel_request.request_id
        self._confirmation = pending_confirmation

    @classmethod
    def from_checkpoint(cls, checkpoint: InherentViewCheckpoint) -> InherentView:
        """Clone a checkpoint into a fold that accepts rows past its cursor."""
        return cls(
            through_cursor=checkpoint.through_cursor,
            groups=checkpoint.groups,
            pending_requests=checkpoint.pending_requests,
            actions=checkpoint.actions,
            pending_confirmation=checkpoint.pending_confirmation,
        )

    @property
    def through_cursor(self) -> int:
        """The highest cursor this fold has been told about."""
        return self._through_cursor

    def checkpoint(self, *, through_cursor: int) -> InherentViewCheckpoint:
        """Freeze the retained groups, oldest first, as folded through ``through_cursor``.

        Args:
            through_cursor: The scan high-water the caller drained to; it may
                exceed the last relevant row's cursor because irrelevant rows
                advance the watermark without touching the view.

        Returns:
            An immutable checkpoint at that cursor.
        """
        if through_cursor < self._through_cursor:
            msg = (
                f"checkpoint cursor {through_cursor} regresses below the fold's "
                f"{self._through_cursor}"
            )
            raise ValueError(msg)
        self._through_cursor = through_cursor
        ordered = sorted(self._groups.values(), key=lambda group: group.opened_cursor)
        return InherentViewCheckpoint(
            through_cursor=through_cursor,
            groups=tuple(ordered),
            pending_requests=tuple(self._request_of_turn.items()),
            actions=tuple(self._action_view(action) for action in self._actions.values()),
            pending_confirmation=self._confirmation,
        )

    def fold(  # noqa: PLR0913 — one keyword per Event Log row column the fold reads.
        self,
        *,
        cursor: int,
        event_uid: str,
        event_type: str,
        ts_epoch_ms: int,
        payload: Mapping[str, Any],
        source_event_id: str | None = None,
        correlation: Mapping[str, Any] | None = None,
    ) -> ViewTransition | None:
        """Apply one Event Log row and report what it changed.

        Rows must arrive in strictly ascending ``cursor`` order (D6: cursor
        regression is not legal).  A response row carrying a ``response_id``
        always returns a transition, possibly with no changes (a duplicate
        open, a late or repeated segment).  Every other row returns a
        transition only when it moved something the wire carries; a row that
        moved only fold-internal truth, or nothing at all, advances the cursor
        and returns None.  So every returned transition is exactly one durable
        envelope.

        The confirmation deadline is judged here, against this row's
        ``ts_epoch_ms``: the fold has no clock, so the first row folded at or
        past a live slot's ``expires_at_ms`` clears it, before that row's own
        changes.

        Args:
            cursor: ``events.id`` of the row.
            event_uid: The row's ``event_uid``; becomes the delta's message id.
            event_type: The row's ``type``.
            ts_epoch_ms: The row's timestamp; a new response's creation time.
            payload: The decoded ``payload_json``.
            source_event_id: The row's ``events.source_event_id`` column; the
                cancel trail's gate verdict names its request's proposal here.
            correlation: The row's decoded ``correlation_json`` column; a
                confirmation ask carries its ``action_id`` / ``turn_id`` here.

        Returns:
            The transition, or None for a row that changed nothing on the wire.

        Raises:
            ValueError: ``cursor`` does not exceed the fold's cursor.
        """
        if cursor <= self._through_cursor:
            msg = f"cursor {cursor} does not advance past {self._through_cursor}"
            raise ValueError(msg)
        self._through_cursor = cursor
        changes: list[ViewChange] = []
        expiry = self._expire_confirmation(cursor, ts_epoch_ms)
        if expiry is not None:
            changes.append(expiry)
        folded = self._fold_row(
            cursor, event_uid, event_type, ts_epoch_ms, payload, source_event_id, correlation,
        )
        if folded is None and not changes:
            return None
        changes.extend(folded or ())
        return ViewTransition(cursor=cursor, event_uid=event_uid, changes=tuple(changes))

    # --- row dispatch -------------------------------------------------------

    def _fold_row(  # noqa: PLR0913 — one positional per row column, as `fold` takes them.
        self,
        cursor: int,
        event_uid: str,
        event_type: str,
        ts_epoch_ms: int,
        payload: Mapping[str, Any],
        source_event_id: str | None,
        correlation: Mapping[str, Any] | None,
    ) -> list[ViewChange] | None:
        """Route one row to its family; None when it earns no envelope."""
        if event_type in RESPONSE_EVENT_TYPES:
            return self._fold_response(cursor, event_uid, event_type, ts_epoch_ms, payload)
        if event_type in _LIFECYCLE_OF_TYPE:
            return self._fold_lifecycle(cursor, event_type, payload) or None
        if event_type in _ACTION_STATE_OF_TYPE:
            return (
                self._fold_action(cursor, event_uid, event_type, ts_epoch_ms, payload) or None
            )
        if event_type in _CLEANUP_STATE_OF_TYPE:
            self._fold_cleanup(event_type, ts_epoch_ms, payload)
        elif event_type in CONFIRMATION_EVENT_TYPES:
            return self._fold_confirmation(cursor, event_type, payload, correlation) or None
        elif event_type == GATE_EVENT_TYPE:
            self._fold_gate(cursor, payload, source_event_id)
        elif event_type == INPUT_CORRELATION_TYPE:
            self._remember_request(payload)
        return None

    # --- response groups ----------------------------------------------------

    def _fold_response(
        self,
        cursor: int,
        event_uid: str,
        event_type: str,
        ts_epoch_ms: int,
        payload: Mapping[str, Any],
    ) -> list[ViewChange] | None:
        """Fold one ``surface.response_*`` row; None when it names no response."""
        response_id = _string(payload, "response_id")
        if response_id is None:
            return None
        changes: list[ViewChange] = []
        response = self._response(response_id)
        if response is None:
            response = self._open(response_id, cursor, ts_epoch_ms, payload)
            changes.append(ViewChange("response.opened", response))
            changes.append(ViewChange("response.delivery", response))
        if event_type == "surface.response_chunk":
            segment = self._segment(response, payload)
            if segment is not None and response.panel_stream == "open":
                response = self._store(
                    replace(response, segments=(*response.segments, segment), revision=cursor),
                    cursor,
                )
                changes.append(ViewChange("response.segment", response, segment))
        elif event_type == "surface.response_emitted" and response.panel_stream == "open":
            response = self._store(
                bound_document(
                    replace(response, panel_stream="closed", revision=cursor),
                    event_uid=event_uid,
                ),
                cursor,
            )
            changes.append(ViewChange("response.delivery", response))
            self._evict_terminal_groups()
        return changes

    def _fold_lifecycle(
        self, cursor: int, event_type: str, payload: Mapping[str, Any],
    ) -> list[ViewChange]:
        """Join one L3 ``response.*`` row to the response it names.

        A response this fold never opened — the legacy uuid5 binding emits no
        ``response.*`` row at all, and an evicted group is gone — changes
        nothing: the client holds no such response and a lifecycle naming one
        would only send it into a resync.
        """
        response_id = _string(payload, "response_id")
        response = None if response_id is None else self._response(response_id)
        if response is None:
            return []
        lifecycle = _LIFECYCLE_OF_TYPE[event_type]
        reason = _string(payload, "reason")
        if response.lifecycle == lifecycle and response.terminal_reason == reason:
            return []
        updated = self._store(
            replace(response, lifecycle=lifecycle, terminal_reason=reason, revision=cursor),
            cursor,
        )
        return [ViewChange("response.lifecycle", updated)]

    # --- actions ------------------------------------------------------------

    def _fold_action(
        self,
        cursor: int,
        event_uid: str,
        event_type: str,
        ts_epoch_ms: int,
        payload: Mapping[str, Any],
    ) -> list[ViewChange]:
        """Fold one registered ``action.*`` row into its :class:`ActionView`."""
        action_id = _string(payload, "action_id")
        if action_id is None:
            return []
        state = _ACTION_STATE_OF_TYPE[event_type]
        known = self._actions.get(action_id)
        action = ActionView(
            action_id=action_id,
            # An action dispatched with no proposed row — the same gap
            # `ActionAdmission` documents — has no tool name to report.
            action_type=_DEFAULT_ACTION_TYPE if known is None else known.action_type,
            canonical_state=state,
            state_revision_cursor=cursor,
            updated_at_ms=ts_epoch_ms,
        ) if known is None else replace(
            known,
            canonical_state=state,
            state_revision_cursor=cursor,
            updated_at_ms=ts_epoch_ms,
        )
        if event_type == "action.proposed":
            turn_id = _string(payload, "turn_id")
            action = replace(
                action,
                action_type=_string(payload, "tool_name") or _DEFAULT_ACTION_TYPE,
                safe_target_ref=_string(payload, "target_entity_ref"),
                response_group_id=(
                    stable_response_group_id(turn_id)
                    if turn_id is not None
                    else action.response_group_id
                ),
            )
            self._open_cancel_request(cursor, event_uid, action_id, payload)
        elif event_type == "action.dispatched":
            self._dispatched.add(action_id)
            self._move_cancel(cursor, action_id, "quiescing")
        elif event_type == "action.running":
            action = replace(action, started_at_ms=ts_epoch_ms)
        elif event_type == "action.result_observed":
            action = replace(action, result_available=True)
            self._fold_cancel_ack(cursor, action_id, payload)
        if state in _TERMINAL_ACTION_STATES:
            action = self._terminalize(action, cursor, event_type, payload)
        self._actions[action_id] = action
        view = self._action_view(action)
        if state in _TERMINAL_ACTION_STATES:
            self._evict_terminal_actions()
        return [ViewChange("action.upsert", action=view)]

    def _terminalize(
        self, action: ActionView, cursor: int, event_type: str, payload: Mapping[str, Any],
    ) -> ActionView:
        """Stamp a terminal row's safe fields and settle any cancel request.

        ``failure_code`` is ``payload.reason`` and never ``payload.error``:
        D13 bars unbounded stdout and unverified self-report from this view.
        """
        if event_type != "action.result_observed":
            action = replace(action, failure_code=_string(payload, "reason"))
            self._move_cancel(cursor, action.action_id, "failed", _string(payload, "reason"))
        if event_type == "action.timeout_assumed":
            action = replace(action, task_id=_string(payload, "task_id") or action.task_id)
        self._resolve_cancel_of_target(cursor, action.action_id, action.canonical_state)
        return action

    def _fold_cleanup(
        self, event_type: str, ts_epoch_ms: int, payload: Mapping[str, Any],
    ) -> None:
        """Record the D9 cleanup trio on a known action; the wire carries none of it."""
        action_id = _string(payload, "action_id")
        action = None if action_id is None else self._actions.get(action_id)
        if action is None or action_id is None:
            return
        self._actions[action_id] = replace(
            action, cleanup_state=_CLEANUP_STATE_OF_TYPE[event_type], updated_at_ms=ts_epoch_ms,
        )

    def _action_view(self, action: ActionView) -> ActionView:
        """Materialize the two derived fields L5 is forbidden to compute."""
        request_id = self._cancel_of_target.get(action.action_id)
        return replace(
            action,
            cancellable=(
                action.action_id in self._dispatched
                and action.canonical_state not in _TERMINAL_ACTION_STATES
            ),
            cancel_request=None if request_id is None else self._cancels.get(request_id),
        )

    def _evict_terminal_actions(self) -> None:
        terminal = sorted(
            (
                action
                for action in self._actions.values()
                if action.canonical_state in _TERMINAL_ACTION_STATES
            ),
            key=lambda action: action.state_revision_cursor,
        )
        for action in terminal[: max(0, len(terminal) - RECENT_TERMINAL_ACTION_LIMIT)]:
            self._forget_action(action.action_id)

    def _forget_action(self, action_id: str) -> None:
        del self._actions[action_id]
        self._dispatched.discard(action_id)
        request_id = self._cancel_of_target.pop(action_id, None)
        if request_id is not None:  # it was a cancel target
            self._cancels.pop(request_id, None)
        cancel = self._cancels.pop(action_id, None)
        if cancel is not None:  # it was a cancel request
            self._cancel_of_target.pop(cancel.target_action_id, None)

    # --- the A5 cancel trail ------------------------------------------------

    def _open_cancel_request(
        self, cursor: int, event_uid: str, request_id: str, payload: Mapping[str, Any],
    ) -> None:
        """Start a :class:`CancelRequestView` on a ``cancel_action`` proposal."""
        if _string(payload, "tool_name") != CANCEL_TOOL_NAME:
            return
        arguments = payload.get("arguments")
        target = _string(arguments, "target_action_id") if isinstance(arguments, dict) else None
        if target is None or target not in self._actions:
            # The trail is only reachable through its target's view, so a
            # target this fold does not hold is nothing to attach it to; that
            # also keeps the trail bounded by the actions it hangs off.
            return
        live = self._cancels.get(self._cancel_of_target.get(target, ""))
        if live is not None and live.state in _CANCEL_LIVE_STATES:
            # D13: a second request while one is received / authorized /
            # quiescing resolves to the existing request rather than creating
            # a second cancel state.
            return
        self._cancels[request_id] = CancelRequestView(
            request_id=request_id,
            target_action_id=target,
            state="received",
            revision_cursor=cursor,
            proposed_event_uid=event_uid,
        )
        self._cancel_of_target[target] = request_id

    def _move_cancel(
        self, cursor: int, request_id: str, state: CancelRequestState, reason: str | None = None,
    ) -> None:
        cancel = self._cancels.get(request_id)
        if cancel is None or cancel.state in _CANCEL_SETTLED_STATES:
            return
        self._cancels[request_id] = replace(
            cancel, state=state, revision_cursor=cursor, reason_code=reason or cancel.reason_code,
        )

    def _fold_gate(
        self, cursor: int, payload: Mapping[str, Any], source_event_id: str | None,
    ) -> None:
        """Apply one Pre-action Gate verdict to the action it names.

        It settles a cancel request and, when it does not pass, retires the
        action it refused: L3 returns without dispatching on any non-``pass``
        outcome, so no terminal will ever arrive for that ``action_id`` and no
        wire state resolves a stuck ``proposed``.  An accepted confirmation re-proposes,
        which folds a fresh view.
        """
        if payload.get("gate") != "pre_action":
            return
        passed = payload.get("outcome") == "pass"
        self._settle_cancel_on_gate(cursor, payload, source_event_id, passed=passed)
        action_id = _string(payload, "action_id")
        if passed or action_id is None:
            return
        self._actions.pop(action_id, None)
        self._dispatched.discard(action_id)

    def _settle_cancel_on_gate(
        self,
        cursor: int,
        payload: Mapping[str, Any],
        source_event_id: str | None,
        *,
        passed: bool,
    ) -> None:
        """Move a received cancel request on its own verdict.

        Its :class:`CancelRequestView` outlives the retirement above: D13 keeps
        a rejected request visible against its target.
        """
        cancel = self._cancel_of_gate(payload, source_event_id)
        if cancel is None or cancel.state != "received":
            return
        if passed:
            self._move_cancel(cursor, cancel.request_id, "authorized")
            return
        reasons = payload.get("reasons")
        reason = reasons[0] if isinstance(reasons, list) and reasons else None
        self._move_cancel(
            cursor, cancel.request_id, "rejected", reason if isinstance(reason, str) else None,
        )

    def _cancel_of_gate(
        self, payload: Mapping[str, Any], source_event_id: str | None,
    ) -> CancelRequestView | None:
        """Resolve a verdict's request by ``action_id``, else by its source row.

        ``action_id`` is an optional payload key on ``gate.evaluated``, while
        the verdict's ``events.source_event_id`` always names the
        ``action.proposed`` row it answers.
        """
        request_id = _string(payload, "action_id")
        cancel = None if request_id is None else self._cancels.get(request_id)
        if cancel is not None or source_event_id is None:
            return cancel
        return next(
            (
                candidate
                for candidate in self._cancels.values()
                if candidate.proposed_event_uid == source_event_id
            ),
            None,
        )

    def _fold_cancel_ack(self, cursor: int, request_id: str, payload: Mapping[str, Any]) -> None:
        """Move the request on its handler's ack; ``accepted`` waits for the target."""
        if request_id not in self._cancels:
            return
        status = _ack_status(payload)
        if status == "unsupported":
            self._move_cancel(cursor, request_id, "rejected", "unsupported")
        elif status == "unconfirmed":
            self._move_cancel(cursor, request_id, "failed", "unconfirmed")
        elif status == "already_terminal":
            self._move_cancel(cursor, request_id, "resolved", "already_terminal")

    def _resolve_cancel_of_target(
        self, cursor: int, target_id: str, state: ActionCanonicalState,
    ) -> None:
        """Resolve the request when its target reaches any canonical terminal."""
        request_id = self._cancel_of_target.get(target_id)
        cancel = None if request_id is None else self._cancels.get(request_id)
        if cancel is None or request_id is None or cancel.state in _CANCEL_SETTLED_STATES:
            return
        self._cancels[request_id] = replace(
            cancel,
            state="resolved",
            revision_cursor=cursor,
            reason_code=_CANCEL_RESOLUTION_OF_STATE[state],
        )

    # --- the confirmation slot ----------------------------------------------

    def _fold_confirmation(
        self,
        cursor: int,
        event_type: str,
        payload: Mapping[str, Any],
        correlation: Mapping[str, Any] | None,
    ) -> list[ViewChange]:
        """Fold one confirmation row into the one globally unique slot (D14)."""
        if event_type == "confirmation.requested":
            changes: list[ViewChange] = []
            if self._confirmation is not None:
                changes.append(self._clear_confirmation(self._confirmation, "superseded", cursor))
            view = _confirmation_of_request(cursor, payload, correlation)
            if view is None:
                # A malformed successor cannot leave an older ask executable.
                return changes
            self._confirmation = view
            changes.append(ViewChange("confirmation.upsert", confirmation=view))
            return changes
        slot = self._confirmation
        if slot is None or _string(payload, "confirmation_id") != slot.confirmation_id:
            return []
        reason: ConfirmationClearReason = _CLEAR_REASON_OF_TYPE[event_type]
        return [self._clear_confirmation(slot, reason, cursor)]

    def _expire_confirmation(self, cursor: int, ts_epoch_ms: int) -> ViewChange | None:
        """Clear a slot whose deadline this row's timestamp has reached."""
        slot = self._confirmation
        if slot is None or ts_epoch_ms < slot.expires_at_ms:
            return None
        return self._clear_confirmation(slot, "expired", cursor)

    def _clear_confirmation(
        self, slot: ConfirmationView, reason: ConfirmationClearReason, cursor: int,
    ) -> ViewChange:
        self._confirmation = None
        return ViewChange(
            "confirmation.cleared",
            cleared=ConfirmationCleared(
                confirmation_id=slot.confirmation_id, reason=reason, revision=cursor,
            ),
        )

    # --- internals ---------------------------------------------------------

    def _response(self, response_id: str) -> ResponseView | None:
        group_id = self._group_of.get(response_id)
        if group_id is None:
            return None
        return next(
            response
            for response in self._groups[group_id].responses
            if response.response_id == response_id
        )

    def _open(
        self,
        response_id: str,
        cursor: int,
        ts_epoch_ms: int,
        payload: Mapping[str, Any],
    ) -> ResponseView:
        group_id = _string(payload, "response_group_id") or response_id
        turn_id = _string(payload, "turn_id") or ""
        question = _string(payload, "query")
        group = self._groups.get(group_id)
        # D21: the correlation is consumed when the group first opens; a
        # sibling response of an already-open group inherits what the group
        # carries, exactly as it shares the group's turn.
        request_id = (
            self._request_of_turn.pop(turn_id, None)
            if group is None
            else group.source_client_request_id
        )
        response = ResponseView(
            response_id=response_id,
            response_group_id=group_id,
            turn_id=turn_id,
            phase=_enum(payload, "phase", _PHASES, _DEFAULT_PHASE),
            channel=_enum(payload, "channel", _CHANNELS, _DEFAULT_CHANNEL),
            question=question,
            created_at_ms=ts_epoch_ms,
            revision=cursor,
            panel_stream="open",
            source_client_request_id=request_id,
        )
        if group is None:
            group = ResponseGroupView(
                response_group_id=group_id,
                turn_id=turn_id,
                question=question,
                created_at_ms=ts_epoch_ms,
                opened_cursor=cursor,
                last_cursor=cursor,
                responses=(),
                source_client_request_id=request_id,
            )
        self._groups[group_id] = replace(
            group,
            question=group.question or question,
            responses=(*group.responses, response),
            last_cursor=cursor,
        )
        self._group_of[response_id] = group_id
        return response

    def _remember_request(self, payload: Mapping[str, Any]) -> None:
        """Hold one D21 ``turn_id -> request_id`` until its group opens.

        The row itself stays irrelevant: it produces no transition and no
        envelope, because the next ``response.opened`` already carries the
        fact.  The map is bounded so an input row whose turn never opens a
        response — a refused turn, a crash — cannot grow the fold with the log.
        """
        turn_id = _string(payload, "turn_id")
        request_id = _string(payload, "source_client_request_id")
        if turn_id is None or request_id is None:
            return
        self._request_of_turn[turn_id] = request_id
        while len(self._request_of_turn) > PENDING_REQUEST_LIMIT:
            self._request_of_turn.pop(next(iter(self._request_of_turn)))

    def _store(self, response: ResponseView, cursor: int) -> ResponseView:
        group = self._groups[response.response_group_id]
        self._groups[response.response_group_id] = replace(
            group,
            responses=tuple(
                response if existing.response_id == response.response_id else existing
                for existing in group.responses
            ),
            last_cursor=cursor,
        )
        return response

    @staticmethod
    def _segment(response: ResponseView, payload: Mapping[str, Any]) -> SegmentView | None:
        text = payload.get("text")
        if not isinstance(text, str):
            return None
        sequence = payload.get("sequence")
        if type(sequence) is not int or sequence < 0:
            sequence = len(response.segments)
        if any(existing.sequence == sequence for existing in response.segments):
            return None
        segment_hash = _string(payload, "segment_hash") or hashlib.sha256(text.encode()).hexdigest()
        return SegmentView(
            sequence=sequence,
            phase=_enum(payload, "phase", _PHASES, response.phase),
            channel=_enum(payload, "channel", _CHANNELS, response.channel),
            text=text,
            segment_hash=segment_hash,
        )

    def _evict_terminal_groups(self) -> None:
        terminal = sorted(
            (group for group in self._groups.values() if group.terminal),
            key=lambda group: group.last_cursor,
        )
        for group in terminal[: max(0, len(terminal) - RECENT_TERMINAL_GROUP_LIMIT)]:
            del self._groups[group.response_group_id]
            for response in group.responses:
                del self._group_of[response.response_id]


__all__ = [
    "ACTION_EVENT_TYPES",
    "CANCEL_TOOL_NAME",
    "CLEANUP_EVENT_TYPES",
    "CONFIRMATION_EVENT_TYPES",
    "FOLD_EVENT_TYPES",
    "GATE_EVENT_TYPE",
    "INLINE_DOCUMENT_BUDGET_BYTES",
    "INPUT_CORRELATION_TYPE",
    "PENDING_REQUEST_LIMIT",
    "RECENT_TERMINAL_ACTION_LIMIT",
    "RECENT_TERMINAL_GROUP_LIMIT",
    "RESPONSE_EVENT_TYPES",
    "RESPONSE_LIFECYCLE_EVENT_TYPES",
    "ActionView",
    "CancelRequestView",
    "ConfirmationCleared",
    "ConfirmationView",
    "DocumentReference",
    "InherentView",
    "InherentViewCheckpoint",
    "ResponseGroupView",
    "ResponseView",
    "SegmentView",
    "ViewChange",
    "ViewTransition",
]
