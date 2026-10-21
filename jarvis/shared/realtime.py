"""Shared contracts for the opt-in realtime concurrency-safety foundation.

This module contains identity and outcome values only.  It deliberately owns
no SQLite connection, gate decision, provider client, or surface behavior.
Those responsibilities remain with their numbered layers.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from jarvis.shared import Event

LifecycleOwner = Literal["playback", "response", "action", "confirmation"]
LLMRequestOutcome = Literal["completed", "cancelled", "error"]
LLMUsageStatus = Literal["provider_final", "partial", "unavailable"]
ResponseCancelScope = Literal["foreground_output", "generation"]

RESPONSE_CANCEL_REASONS: Final[frozenset[str]] = frozenset(
    {"user_stop", "superseded", "shutdown", "operator_request", "barge_in"},
)
"""Closed ``response.cancelled`` reason vocabulary (ADR-0008 D10, Q7).

A caller-supplied reason outside this set is normalized to
``"operator_request"`` at the cancel entry point, so a later presentation
wave can switch on the value without string archaeology.
"""


@dataclass(frozen=True)
class Wave1FeatureFlags:
    """Production adoption switch for ADR-0006/0008 Wave 1.

    The primitives themselves are always importable so deterministic
    integration and recovery checks can exercise them.  Runtime callers must
    opt into the behavior explicitly.  Since 2026-09-07 the shipped
    configuration turns it on (the tier-A adoption decision recorded in
    ADR-0006 §13); the dataclass default stays off so an absent or malformed
    block still degrades to the serial Wave-0 path.  The transactional
    append, the lifecycle terminal CAS and the exactly-once cost accounting
    are not switches any more (ADR 0332).
    """

    confirmation_dispatch_outbox: bool = False

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object] | None) -> Wave1FeatureFlags:
        """Parse exact booleans, treating absent values as disabled.

        Non-boolean truthy values do not enable a safety-sensitive path.
        Configuration validation can become stricter when a rollout wants a
        loud error; Wave 1 needs the compatibility-safe fail-closed default.
        """
        values = {} if raw is None else raw
        return cls(
            confirmation_dispatch_outbox=values.get("confirmation_dispatch_outbox") is True,
        )


def _structured(spoken: object) -> bool:
    if not isinstance(spoken, Mapping):
        return False
    return spoken.get("enabled") is True and spoken.get("structured") is True


def _first_clause_chars(spoken: object) -> int:
    value = spoken.get("first_clause_chars") if isinstance(spoken, Mapping) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


@dataclass(frozen=True)
class Wave4ResponseFlags:
    """Production adoption switches for ADR-0008 Step 2 (Wave 4A).

    Every turn runs in a durable ResponseRun with an immutable per-run request
    client, and the generation cancel seam exists (ADR 0332): neither is a
    switch. ``routine_streaming`` (ADR-0008 Step 8, ``routine_streaming.enabled``)
    streams permitted sentences of a pre-routed casual answer while the model
    is still generating. ``spoken_streaming`` (``spoken_streaming.enabled``,
    docs/plans/speak-as-written-proposal.md) streams every request of a turn
    Allen spoke, tools included, and speaks its answer as it is written. The
    shipped configuration turns both off, so a turn takes the batch path with
    the complete legacy prompt.
    """

    routine_streaming: bool = False
    spoken_streaming: bool = False
    spoken_first_clause_chars: int = 0
    """``spoken_streaming.first_clause_chars``: a spoken answer's first
    sentence is said from its first clause end at or past this many
    characters, not from its end (0: off)."""
    spoken_structured: bool = False
    """``spoken_streaming.structured`` (ADR 0114): the spoken route's requests
    carry a strict schema with ``spoken`` and ``written``. Only with
    ``spoken_streaming`` on."""
    prefix_warm: bool = False
    """``prefix_warm.enabled``: after each turn, one capped request with the
    next turn's prompt up to its new message, so OpenAI's cache covers the
    history (``jarvis.decision.open_prefix_warm``). Uses the run's per-run
    client."""
    slow_results: bool = False
    """``slow_results.enabled`` (ADR 0107): a turn
    is told which earlier ones are still being answered, a turn Allen spoke
    past opens by pointing back at its question, and another turn's answer
    waits for the speech lane instead of cutting in or being dropped."""
    lifecycle_commentary: bool = False
    """ADR-0008 D6 (Step 5, ``realtime.commentary.enabled``).

    A committed action lifecycle row on a user-originated turn opens one
    short deterministic ``phase="commentary"`` ResponseRun.  Its config
    block is a sibling of ``realtime.response`` rather than a member of it —
    the switch is about a second response *phase*, not about how the answer
    itself is produced — so :meth:`from_mapping` takes it separately.
    """

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, object] | None,
        *,
        commentary: Mapping[str, object] | None = None,
    ) -> Wave4ResponseFlags:
        """Parse exact booleans, treating absent values as disabled.

        Same fail-closed rule as :meth:`Wave1FeatureFlags.from_mapping`: a
        non-boolean truthy value never enables a lifecycle-sensitive path.
        ``commentary`` is the ``realtime.commentary`` block; omitted, the
        commentary switch reads as off exactly like an absent key.
        """
        values = {} if raw is None else raw
        routine = values.get("routine_streaming")
        spoken = values.get("spoken_streaming")
        warm = values.get("prefix_warm")
        slow = values.get("slow_results")
        return cls(
            routine_streaming=isinstance(routine, Mapping) and routine.get("enabled") is True,
            spoken_streaming=isinstance(spoken, Mapping) and spoken.get("enabled") is True,
            spoken_first_clause_chars=_first_clause_chars(spoken),
            spoken_structured=_structured(spoken),
            prefix_warm=isinstance(warm, Mapping) and warm.get("enabled") is True,
            slow_results=isinstance(slow, Mapping) and slow.get("enabled") is True,
            lifecycle_commentary=commentary is not None and commentary.get("enabled") is True,
        )

    @property
    def all_disabled(self) -> bool:
        """Return whether no streaming or commentary switch is requested."""
        return not any(
            (
                self.routine_streaming,
                self.spoken_streaming,
                self.lifecycle_commentary,
            )
        )


@dataclass(frozen=True)
class ResponseInterruptPolicy:
    """ADR-0008 D10 frozen interrupt contract, issued when a run starts.

    ``action_action`` is deliberately the literal ``"never"`` and not a
    wider union: ADR §13.5 requires that cancelling a response can never
    imply cancelling a linked action, and a type that cannot express any
    other value makes that a compile-time property rather than a review
    convention.
    """

    response_id: str
    policy_hash: str
    candidate_playback: Literal["duck", "ignore"] = "ignore"
    confirmed_playback: Literal[
        "interrupt_expected_playback_generation",
        "ignore",
    ] = "interrupt_expected_playback_generation"
    generation_action: Literal["cancel", "continue"] = "cancel"
    action_action: Literal["never"] = "never"


PresentationIntentType = Literal[
    "emphasize",
    "acknowledge",
    "progress",
    "stale_warn",
    "confirm_request",
    "review_needed",
    "error",
]
"""Spec §3.6.3's closed ``PresentationIntent.intent_type`` vocabulary."""


@dataclass(frozen=True)
class PresentationIntent:
    """Spec §3.6.3 L3→L5 render contract, finer-grained than the channel.

    Ephemeral by construction: spec §3.6.3's Contract-vs-Event note makes
    this a message between the layers, never an Event Log row.  The durable
    trace of a delivered intent is the ResponseRun it opens and that run's
    ``surface.response_*`` rows — nothing here is appended.

    It lives beside :class:`ResponseInterruptPolicy` and
    :class:`LegacyPresentationBinding` for the same reason they do: it is an
    L3→L5 contract object, and ``shared`` is the only module both layers may
    import under the layer DAG.
    """

    intent_type: PresentationIntentType
    surface_hint: str
    subject_ref: str
    content_hint: str
    freshness_required: bool


@dataclass(frozen=True)
class LegacyPresentationBinding:
    """Stable compatibility identity minted at the existing L3/L5 seam.

    This is not a second ResponseRun lifecycle.  It only gives legacy batch
    rendering the immutable IDs required by L5 delivery and is replaced by
    explicit ADR-0008 ResponseRun IDs when that producer is adopted.
    """

    response_id: str
    response_group_id: str


def stable_response_group_id(turn_id: str) -> str:
    """Derive the migration-stable response-group id for one turn.

    Extracted verbatim from :func:`stable_legacy_presentation_binding`'s own
    group derivation — same namespace, same input string, same prefix — so
    the L3 ResponseRun producer and the legacy batch binding name the same
    group for the same turn.
    """
    derived = uuid.uuid5(uuid.NAMESPACE_URL, f"jarvis:surface:group:{turn_id}")
    return "RGRP" + derived.hex


def new_response_id() -> str:
    """Mint a fresh ResponseRun id.

    ADR-0014 requires that a ``response_id`` is never reused: a correction or
    retry that needs a new semantic response gets a new one.  The legacy
    binding derives its id from ``turn_id`` + ``response_hash``, so two
    renders of one turn with identical text collide; a ResponseRun does not.
    """
    return "RESP" + uuid.uuid4().hex


def new_log_epoch() -> str:
    """Mint the identity of one Event Log lineage (ADR-0014 D6).

    Assigned exactly once per log file and never re-minted afterwards: a
    client that presents a different epoch is holding state from another
    log and must resynchronize from a snapshot.
    """
    return "L" + uuid.uuid4().hex


def stable_legacy_presentation_binding(
    *,
    turn_id: str,
    response_hash: str,
) -> LegacyPresentationBinding:
    """Derive one migration-safe response/group binding for a batch render."""
    derived = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"jarvis:surface:response:{turn_id}:{response_hash}",
    )
    return LegacyPresentationBinding(
        response_id="RESP" + derived.hex,
        response_group_id=stable_response_group_id(turn_id),
    )


@dataclass(frozen=True)
class TerminalCommitted:
    """The caller won one lifecycle's canonical terminal CAS."""

    owner: LifecycleOwner
    identity: str
    event: Event


@dataclass(frozen=True)
class AlreadyTerminal:
    """A canonical terminal already exists for the requested lifecycle."""

    owner: LifecycleOwner
    identity: str
    event: Event


@dataclass(frozen=True)
class StaleConfirmation:
    """The confirmation slot the caller folded is no longer the newest ask.

    Deliberately outside :data:`TerminalOutcome`: only the confirmation
    terminalizer can produce it, and widening the shared union would force
    every L3/L4 consumer of a playback, response or action terminal to
    handle a case it can never receive.

    Attributes:
        confirmation_id: The ask the caller meant to terminalize.
        expected_revision: ``events.id`` of the ``confirmation.requested``
            row the caller folded.
        actual_revision: ``events.id`` of the newest ``confirmation.requested``
            row seen inside the CAS transaction, or None when the log holds
            no such row at all.
    """

    confirmation_id: str
    expected_revision: int
    actual_revision: int | None


TerminalOutcome = TerminalCommitted | AlreadyTerminal


@dataclass(frozen=True)
class StableAuthorizationIdentity:
    """IDs derived solely from one canonical confirmation acceptance."""

    source_confirmation_event_id: str
    authorization_id: str
    lease_id: str
    action_id: str
    dispatch_id: str


def authorization_source(accepted_event_uid: str, call_index: int) -> str:
    """The key one call of an accepted card is authorized under (ADR 0321).

    A card with several calls has one ``confirmation.accepted`` and one lease per call: the
    first call keeps the acceptance's own uid, so a card of one call is keyed as it always was.
    """
    return accepted_event_uid if call_index == 0 else f"{accepted_event_uid}#{call_index}"


def split_authorization_source(source: str) -> tuple[str, int]:
    """``authorization_source``'s inverse: the acceptance's uid and the call's index."""
    uid, _, index = source.partition("#")
    return uid, int(index) if index else 0


def stable_authorization_identity(
    source_confirmation_event_id: str,
) -> StableAuthorizationIdentity:
    """Derive replay-stable authorization/action/dispatch IDs.

    A duplicate runner may propose arbitrary random lease or action IDs, but
    none of them become the claim key.  The accepted Event UID is the only
    input, so live races and boot recovery resolve to the same identities.
    """

    def _derive(label: str, prefix: str) -> str:
        value = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"jarvis:wave1:{label}:{source_confirmation_event_id}",
        )
        return prefix + value.hex

    return StableAuthorizationIdentity(
        source_confirmation_event_id=source_confirmation_event_id,
        authorization_id=_derive("authorization", "AUTH"),
        lease_id=_derive("lease", "LEASE"),
        action_id=_derive("action", "A"),
        dispatch_id=_derive("dispatch", "DISP"),
    )


@dataclass(frozen=True)
class AuthorizedDispatch:
    """Committed confirmation claim, passing gate, and dispatch debt."""

    identity: StableAuthorizationIdentity
    confirmation_id: str
    gate_event: Event
    tool_name: str
    target_entity_ref: str | None
    request_payload: Mapping[str, object]


@dataclass(frozen=True)
class AlreadyConsumed:
    """The acceptance already produced one stable authorized dispatch."""

    dispatch: AuthorizedDispatch


ConfirmationConsumptionOutcome = AuthorizedDispatch | AlreadyConsumed


@dataclass(frozen=True)
class CostAccountingDisposition:
    """One L3 accounting conclusion for one provider request."""

    llm_request_id: str
    kind: str
    provider: str
    model: str
    outcome: LLMRequestOutcome
    usage_status: LLMUsageStatus
    provider_response_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_usd: float | None = None
    error_code: str | None = None
    service_tier: str | None = None
    """The tier the provider said it served, else the one the request asked for."""


@dataclass(frozen=True)
class CostRecorded:
    """The caller committed the request's single cost disposition."""

    disposition: CostAccountingDisposition
    event: Event


@dataclass(frozen=True)
class CostAlreadyRecorded:
    """Replay found the request's already-committed cost disposition."""

    disposition: CostAccountingDisposition
    event: Event


CostAccountingOutcome = CostRecorded | CostAlreadyRecorded


__all__ = [
    "RESPONSE_CANCEL_REASONS",
    "AlreadyConsumed",
    "AlreadyTerminal",
    "AuthorizedDispatch",
    "ConfirmationConsumptionOutcome",
    "CostAccountingDisposition",
    "CostAccountingOutcome",
    "CostAlreadyRecorded",
    "CostRecorded",
    "LLMRequestOutcome",
    "LLMUsageStatus",
    "LegacyPresentationBinding",
    "LifecycleOwner",
    "PresentationIntent",
    "PresentationIntentType",
    "ResponseCancelScope",
    "ResponseInterruptPolicy",
    "StableAuthorizationIdentity",
    "StaleConfirmation",
    "TerminalCommitted",
    "TerminalOutcome",
    "Wave1FeatureFlags",
    "Wave4ResponseFlags",
    "authorization_source",
    "new_log_epoch",
    "new_response_id",
    "split_authorization_source",
    "stable_authorization_identity",
    "stable_legacy_presentation_binding",
    "stable_response_group_id",
]
