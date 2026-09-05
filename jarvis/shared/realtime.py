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

LifecycleOwner = Literal["playback", "response", "action"]
LLMRequestOutcome = Literal["completed", "cancelled", "error"]
LLMUsageStatus = Literal["provider_final", "partial", "unavailable"]
ResponseCancelScope = Literal["foreground_output", "generation"]

RESPONSE_CANCEL_REASONS: Final[frozenset[str]] = frozenset(
    {"user_stop", "superseded", "shutdown", "operator_request"},
)
"""Closed ``response.cancelled`` reason vocabulary (ADR-0008 D10, Q7).

A caller-supplied reason outside this set is normalized to
``"operator_request"`` at the cancel entry point, so a later presentation
wave can switch on the value without string archaeology.
"""


@dataclass(frozen=True)
class Wave1FeatureFlags:
    """Production adoption switches for ADR-0006/0008 Wave 1.

    The primitives themselves are always importable so deterministic
    integration and recovery checks can exercise them.  Runtime callers must
    opt into each behavior explicitly; the shipped configuration leaves all
    switches off and therefore retains the serial Wave-0 path.
    """

    transactional_event_append: bool = False
    lifecycle_terminal_cas: bool = False
    confirmation_dispatch_outbox: bool = False
    exactly_once_cost_accounting: bool = False

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object] | None) -> Wave1FeatureFlags:
        """Parse exact booleans, treating absent values as disabled.

        Non-boolean truthy values do not enable a safety-sensitive path.
        Configuration validation can become stricter when a rollout wants a
        loud error; Wave 1 needs the compatibility-safe fail-closed default.
        """
        values = {} if raw is None else raw
        return cls(
            transactional_event_append=values.get("transactional_event_append") is True,
            lifecycle_terminal_cas=values.get("lifecycle_terminal_cas") is True,
            confirmation_dispatch_outbox=values.get("confirmation_dispatch_outbox") is True,
            exactly_once_cost_accounting=values.get("exactly_once_cost_accounting") is True,
        )

    @property
    def all_disabled(self) -> bool:
        """Return whether the runtime must retain the complete Wave-0 path."""
        return not any(
            (
                self.transactional_event_append,
                self.lifecycle_terminal_cas,
                self.confirmation_dispatch_outbox,
                self.exactly_once_cost_accounting,
            ),
        )


@dataclass(frozen=True)
class Wave4ResponseFlags:
    """Production adoption switches for ADR-0008 Step 2 (Wave 4A).

    ``response_run_lifecycle`` wraps ``drive_turn`` in a durable
    ResponseRun with an immutable per-run request client.
    ``independent_response_cancel`` additionally exposes the generation
    cancel seam. ``typed_conversation_history`` adds explicit heard/available
    history to L3 prompts under the same lifecycle parent.
    ``routine_streaming`` (ADR-0008 Step 8, ``routine_streaming.enabled``)
    streams permitted sentences of a pre-routed casual answer while the model
    is still generating. All stay off in the shipped configuration,
    preserving the complete legacy batch prompt.
    """

    response_run_lifecycle: bool = False
    independent_response_cancel: bool = False
    typed_conversation_history: bool = False
    routine_streaming: bool = False

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object] | None) -> Wave4ResponseFlags:
        """Parse exact booleans, treating absent values as disabled.

        Same fail-closed rule as :meth:`Wave1FeatureFlags.from_mapping`: a
        non-boolean truthy value never enables a lifecycle-sensitive path.
        """
        values = {} if raw is None else raw
        routine = values.get("routine_streaming")
        return cls(
            response_run_lifecycle=values.get("response_run_lifecycle") is True,
            independent_response_cancel=values.get("independent_response_cancel") is True,
            typed_conversation_history=values.get("typed_conversation_history") is True,
            routine_streaming=isinstance(routine, Mapping) and routine.get("enabled") is True,
        )

    @property
    def all_disabled(self) -> bool:
        """Return whether the runtime must retain the legacy batch path."""
        return not any(
            (
                self.response_run_lifecycle,
                self.independent_response_cancel,
                self.typed_conversation_history,
                self.routine_streaming,
            )
        )


@dataclass(frozen=True)
class Wave4ActionFlags:
    """Production adoption switch for ADR-0008 Step 3 (Wave 4B).

    ``action_runner`` routes ``ToolRegistry.dispatch`` through the L4
    ActionRunner: the handler runs on the runner's own thread and Event Log
    connection under a resolved resource lease, and the driver waits on the
    returned handle.  Off in the shipped configuration, so ``dispatch`` runs
    handlers inline exactly as it did before.
    """

    action_runner: bool = False
    true_async_workers: bool = False
    """ADR-0008 §6 / Step 4: an ``is_async`` tool is no longer awaited.

    ``dispatch`` returns an acknowledgement as soon as the ActionRun is
    accepted, and the result reaches L3 through the durable trigger the
    tool's async shape always promised. Requires ``action_runner``: without
    a runner there is nothing to own the work after ``dispatch`` returns.
    """

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object] | None) -> Wave4ActionFlags:
        """Parse exact booleans, treating absent values as disabled."""
        values = {} if raw is None else raw
        return cls(
            action_runner=values.get("action_runner") is True,
            true_async_workers=values.get("true_async_workers") is True,
        )

    @property
    def all_disabled(self) -> bool:
        """Return whether the runtime must retain the inline dispatch path."""
        return not (self.action_runner or self.true_async_workers)


@dataclass(frozen=True)
class Wave5InputFlags:
    """Production adoption switch for ADR-0008 D8's intent pump (Step 4).

    ``intent_pump`` replaces the daemon's "poll one event, await its whole
    turn" watcher with a durable-claim queue: the watcher claims each trigger
    with one idempotent ``turn.started``, advances its cursor only after that
    claim is accepted, and hands the turn to a bounded pool. Off, the daemon
    keeps the serial watcher and its startup ``MAX(events.id)`` anchor.
    """

    intent_pump: bool = False

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object] | None) -> Wave5InputFlags:
        """Parse exact booleans, treating absent values as disabled."""
        values = {} if raw is None else raw
        return cls(intent_pump=values.get("intent_pump") is True)

    @property
    def all_disabled(self) -> bool:
        """Return whether the runtime must retain the serial watcher."""
        return not self.intent_pump


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
    confirmed_playback: Literal["interrupt_expected_playback_generation", "ignore"] = "ignore"
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


def new_boot_id() -> str:
    """Mint one daemon process's boot identity (ADR-0014 D6).

    A change of ``boot_id`` under an unchanged ``log_epoch`` means the
    process restarted while the log survived, which is the case that
    invalidates in-flight ephemeral sequences but not durable cursors.
    """
    return "B" + uuid.uuid4().hex


def new_connection_id() -> str:
    """Mint one accepted realtime socket's identity (ADR-0014 D6)."""
    return "C" + uuid.uuid4().hex


def new_client_instance_id() -> str:
    """Mint one client instance's identity (ADR-0014 D6).

    Stable across reconnects of the same client process, unlike
    ``connection_id`` which is per socket.
    """
    return "I" + uuid.uuid4().hex


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


TerminalOutcome = TerminalCommitted | AlreadyTerminal


@dataclass(frozen=True)
class StableAuthorizationIdentity:
    """IDs derived solely from one canonical confirmation acceptance."""

    source_confirmation_event_id: str
    authorization_id: str
    lease_id: str
    action_id: str
    dispatch_id: str


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
    "TerminalCommitted",
    "TerminalOutcome",
    "Wave1FeatureFlags",
    "Wave4ActionFlags",
    "Wave4ResponseFlags",
    "Wave5InputFlags",
    "new_boot_id",
    "new_client_instance_id",
    "new_connection_id",
    "new_log_epoch",
    "new_response_id",
    "stable_authorization_identity",
    "stable_legacy_presentation_binding",
    "stable_response_group_id",
]
