"""Shared contracts for the opt-in realtime concurrency-safety foundation.

This module contains identity and outcome values only.  It deliberately owns
no SQLite connection, gate decision, provider client, or surface behavior.
Those responsibilities remain with their numbered layers.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping

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
    cancel seam.  Both stay off in the shipped configuration, so the
    default path remains the complete legacy batch turn.
    """

    response_run_lifecycle: bool = False
    independent_response_cancel: bool = False

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object] | None) -> Wave4ResponseFlags:
        """Parse exact booleans, treating absent values as disabled.

        Same fail-closed rule as :meth:`Wave1FeatureFlags.from_mapping`: a
        non-boolean truthy value never enables a lifecycle-sensitive path.
        """
        values = {} if raw is None else raw
        return cls(
            response_run_lifecycle=values.get("response_run_lifecycle") is True,
            independent_response_cancel=values.get("independent_response_cancel") is True,
        )

    @property
    def all_disabled(self) -> bool:
        """Return whether the runtime must retain the legacy batch path."""
        return not any((self.response_run_lifecycle, self.independent_response_cancel))


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

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object] | None) -> Wave4ActionFlags:
        """Parse exact booleans, treating absent values as disabled."""
        values = {} if raw is None else raw
        return cls(action_runner=values.get("action_runner") is True)

    @property
    def all_disabled(self) -> bool:
        """Return whether the runtime must retain the inline dispatch path."""
        return not self.action_runner


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
    "ResponseCancelScope",
    "ResponseInterruptPolicy",
    "StableAuthorizationIdentity",
    "TerminalCommitted",
    "TerminalOutcome",
    "Wave1FeatureFlags",
    "Wave4ActionFlags",
    "Wave4ResponseFlags",
    "new_response_id",
    "stable_authorization_identity",
    "stable_legacy_presentation_binding",
    "stable_response_group_id",
]
