"""L3 ResponseRun lifecycle: FSM, policies, terminal owner, cancel entry.

ADR-0008 §3 D1 makes L3 the owner of a response's generation truth: one
durable ``response.started``, exactly one terminal, and an in-memory finite
state machine in between.  This module is the only place in ``jarvis/`` that
calls :func:`jarvis.state.lifecycle_terminal.terminalize_response` — a
structural pin enforced by
``tests/canary/test_canary_response_terminal_only_through_cas.py``.

Cancellation is deliberately terminal-first: the cancel path wins the CAS
*before* it sets the run's token.  That ordering is what makes "a cancel can
never unspeak an answer that was already delivered" a property of the code
rather than a timing hope — a cancel arriving after ``response.completed``
gets ``AlreadyTerminal`` back and changes nothing.  It also never touches an
action: :attr:`~jarvis.shared.realtime.ResponseInterruptPolicy.action_action`
is the literal ``"never"``, and no code path here reads linked action ids,
calls a lifecycle transition, or names an ``action.*`` type.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Final, Literal

from jarvis.shared.realtime import (
    AlreadyTerminal,
    LegacyPresentationBinding,
    ResponseInterruptPolicy,
    TerminalCommitted,
    stable_response_group_id,
)
from jarvis.shared.realtime_trace import record_realtime_trace
from jarvis.state.event_log import emit_event
from jarvis.state.lifecycle_terminal import terminalize_response
from jarvis.state.response_runs import append_response_started, open_response_runs

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from jarvis.decision.llm_session import LLMRequestClient
    from jarvis.shared import Event
    from jarvis.shared.realtime import ResponseCancelScope, TerminalOutcome
    from jarvis.state.committed_event_bus import CommittedEventBus
    from jarvis.state.lifecycle_terminal import FailureInjector

type ResponsePhase = Literal["commentary", "final"]
type ResponseChannel = Literal["speech", "document", "both"]
type ResponseEmissionMode = Literal[
    "deterministic",
    "routine_stream",
    "progress_only",
    "full_text",
    "structured",
]
type ResponseRunState = Literal[
    "idle",
    "generating",
    "waiting_action",
    "finalizing",
    "completed",
    "cancelled",
    "failed",
]

_LEGAL_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    "idle": frozenset({"generating"}),
    "generating": frozenset({"waiting_action", "finalizing", "cancelled", "failed"}),
    "waiting_action": frozenset({"generating", "finalizing", "cancelled", "failed"}),
    "finalizing": frozenset({"completed", "cancelled", "failed"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
    "failed": frozenset(),
}
_OPEN_STATES: Final[frozenset[str]] = frozenset(
    {"idle", "generating", "waiting_action", "finalizing"},
)


class IllegalResponseTransitionError(RuntimeError):
    """``ResponseRun.mark`` rejected a transition ADR-0008 D1 does not allow."""


class ResponseCancelledError(RuntimeError):
    """Raised out of ``drive_turn``'s loop when the run's token is observed set."""


def _canonical_hash(fields: object) -> str:
    """Return a stable sha256 over the canonical JSON form of ``fields``."""
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ResponseEmissionPolicy:
    """Pre-route emission policy fixed for the life of one ResponseRun."""

    emission_mode: ResponseEmissionMode
    output_risk_class: str
    required_gate_mode: str
    allowed_phases: tuple[ResponsePhase, ...]
    allowed_channels: tuple[ResponseChannel, ...]
    active_subject_ref: str
    evidence_snapshot_hash: str
    preset_snapshot_hash: str
    risk_context_hash: str = "unknown"
    classifier_rule_version: str = "unknown"

    @property
    def policy_hash(self) -> str:
        """Return the sha256 identity of this policy's field values."""
        return _canonical_hash(asdict(self))


def legacy_full_text_policy(
    *,
    evidence_snapshot_hash: str,
    preset_snapshot_hash: str,
) -> ResponseEmissionPolicy:
    """Return the only policy Wave 4A constructs.

    ADR-0008 D2 rule 1 says ambiguity defaults to ``full_text``, and requires
    an explicit ``unknown`` rather than an omitted value for the risk class
    and active subject.  Because no permit machinery exists yet, a
    ``full_text`` policy also means nothing can emit early by construction;
    the genuinely derived risk class and gate mode still land on the existing
    ``gate.evaluated(pre_emit)`` event, so no fact is duplicated.
    """
    return ResponseEmissionPolicy(
        emission_mode="full_text",
        output_risk_class="unknown",
        required_gate_mode="full_text",
        allowed_phases=("final",),
        allowed_channels=("both",),
        active_subject_ref="unknown",
        evidence_snapshot_hash=evidence_snapshot_hash,
        preset_snapshot_hash=preset_snapshot_hash,
    )


def evidence_snapshot_hash(conn: sqlite3.Connection) -> str:
    """Hash the log position this run started from.

    Honest description of a placeholder: it is the current ``MAX(events.id)``,
    i.e. "everything the run could have seen".  ADR-0008 Step 7 replaces the
    value with a real SituationPacket hash; the field, the producer and the
    payload key stay exactly as they are, only what the hash is over changes.
    """
    row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()
    position = 0 if row is None else int(row[0])
    return hashlib.sha256(str(position).encode("utf-8")).hexdigest()


class ResponseCancellationToken:
    """A ``threading.Event``-backed cancel flag a foreign thread may set.

    Backed by an Event rather than the run's own lock so the cancel thread
    never has to acquire anything the owning worker holds.
    """

    def __init__(self) -> None:
        """Create an unset token."""
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason: str | None = None
        self._scope: ResponseCancelScope | None = None

    def cancel(self, *, reason: str, scope: ResponseCancelScope) -> bool:
        """Set the token once; return ``False`` on any repeat request."""
        with self._lock:
            if self._event.is_set():
                return False
            self._reason = reason
            self._scope = scope
            self._event.set()
            return True

    @property
    def is_cancelled(self) -> bool:
        """Return whether this run has been cancelled."""
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        """Return the cancel reason, or ``None`` while uncancelled."""
        with self._lock:
            return self._reason

    @property
    def scope(self) -> ResponseCancelScope | None:
        """Return the cancel scope, or ``None`` while uncancelled."""
        with self._lock:
            return self._scope


@dataclass(frozen=True)
class ResponseRunFacts:
    """The four durable identities a terminalizer needs, with no live state."""

    response_id: str
    response_group_id: str
    turn_id: str
    started_event_uid: str


class ResponseRun:
    """In-memory L3 finite state machine for one durable ResponseRun."""

    def __init__(  # noqa: PLR0913 — the six collaborators ARE the ADR-0008 D1 contract.
        self,
        *,
        facts: ResponseRunFacts,
        phase: ResponsePhase,
        channel: ResponseChannel,
        emission_policy: ResponseEmissionPolicy,
        interrupt_policy: ResponseInterruptPolicy,
        request_client: LLMRequestClient,
    ) -> None:
        """Bind one run's durable identities to its live collaborators."""
        self._facts = facts
        self._phase = phase
        self._channel = channel
        self._emission_policy = emission_policy
        self._interrupt_policy = interrupt_policy
        self._request_client = request_client
        self._cancellation_token = ResponseCancellationToken()
        self.admission_lock = threading.Lock()
        self._lock = threading.Lock()
        self._state: ResponseRunState = "idle"
        self._linked_action_ids: list[str] = []

    @contextlib.contextmanager
    def admission_guard(self) -> Iterator[None]:
        """Linearize new work admission against response cancellation."""
        with self.admission_lock:
            self.check_cancelled("before work admission")
            yield

    def check_cancelled(self, where: str) -> None:
        """Reject work proposed by a response whose cancellation won."""
        if self.cancellation_token.is_cancelled or self.state == "cancelled":
            message = f"response {self.response_id} cancelled {where}"
            raise ResponseCancelledError(message)

    def admit_request(self, conn: sqlite3.Connection, kind: str) -> None:
        """Commit request admission against cancellation; release before network I/O.

        A cancelled response may still receive an already admitted request's
        result. The admission row distinguishes that case from new work that
        cancellation must prevent; it is not evidence of provider execution.
        """
        with self.admission_guard():
            emit_event(
                conn,
                type="response.request_admitted",
                payload={
                    "response_id": self.response_id,
                    "admission_id": "REQADM" + uuid.uuid4().hex,
                    "kind": kind,
                },
                source_event_id=self.facts.started_event_uid,
                correlation={"turn_id": self.facts.turn_id},
            )

    @property
    def facts(self) -> ResponseRunFacts:
        """Return the run's durable identities."""
        return self._facts

    @property
    def response_id(self) -> str:
        """Return this run's response id."""
        return self._facts.response_id

    @property
    def response_group_id(self) -> str:
        """Return this run's response group id."""
        return self._facts.response_group_id

    @property
    def turn_id(self) -> str:
        """Return the turn this run belongs to."""
        return self._facts.turn_id

    @property
    def phase(self) -> ResponsePhase:
        """Return the run's fixed phase."""
        return self._phase

    @property
    def channel(self) -> ResponseChannel:
        """Return the run's fixed channel."""
        return self._channel

    @property
    def emission_policy(self) -> ResponseEmissionPolicy:
        """Return the pre-route emission policy."""
        return self._emission_policy

    @property
    def interrupt_policy(self) -> ResponseInterruptPolicy:
        """Return the frozen ADR-0008 D10 interrupt contract."""
        return self._interrupt_policy

    @property
    def request_client(self) -> LLMRequestClient:
        """Return the immutable per-run provider client."""
        return self._request_client

    @property
    def cancellation_token(self) -> ResponseCancellationToken:
        """Return the run's cancellation token."""
        return self._cancellation_token

    @property
    def linked_action_ids(self) -> tuple[str, ...]:
        """Return the actions this run dispatched, for narration only."""
        with self._lock:
            return tuple(self._linked_action_ids)

    @property
    def state(self) -> ResponseRunState:
        """Return the current FSM state."""
        with self._lock:
            return self._state

    @property
    def is_open(self) -> bool:
        """Return whether the run has not yet reached a terminal state."""
        return self.state in _OPEN_STATES

    def mark(self, state: ResponseRunState) -> None:
        """Advance the FSM, refusing any transition ADR-0008 D1 disallows.

        A cancelled run reports :class:`ResponseCancelledError` rather than
        the generic refusal.  That is what closes the one race the
        terminal-first cancel ordering leaves open: the cancel path wins the
        CAS, calls ``mark("cancelled")``, and only then sets the token, so an
        owning worker can pass its token check and *still* arrive here with
        the run already cancelled.  Reporting it as a cancellation makes that
        interleaving end the turn the same way every other one does, instead
        of surfacing an illegal-transition crash the daemon would log as
        ``turn.failed``.

        Raises:
            ResponseCancelledError: The run was already cancelled.
            IllegalResponseTransitionError: The edge is not in the table, or
                the run already reached a non-cancelled terminal state.
        """
        with self._lock:
            if self._state == "cancelled":
                msg = (
                    f"response run {self._facts.response_id!r} was cancelled; "
                    f"refusing to move to {state!r}"
                )
                raise ResponseCancelledError(msg)
            allowed = _LEGAL_TRANSITIONS[self._state]
            if state not in allowed:
                msg = (
                    f"response run {self._facts.response_id!r} cannot move "
                    f"{self._state!r} -> {state!r}"
                )
                raise IllegalResponseTransitionError(msg)
            self._state = state

    def link_action(self, action_id: str) -> None:
        """Record an action this run dispatched (never used to cancel it)."""
        with self._lock:
            if action_id not in self._linked_action_ids:
                self._linked_action_ids.append(action_id)

    def presentation_binding(self) -> LegacyPresentationBinding:
        """Return the L5 binding naming this run's ids."""
        return LegacyPresentationBinding(
            response_id=self._facts.response_id,
            response_group_id=self._facts.response_group_id,
        )


class ResponseRunRegistry:
    """Lock-guarded live-run index; the cancel entry point's only lookup."""

    def __init__(self) -> None:
        """Create an empty registry."""
        self._lock = threading.Lock()
        self._runs: dict[str, ResponseRun] = {}

    def register(self, run: ResponseRun) -> None:
        """Add ``run`` under its response id."""
        with self._lock:
            self._runs[run.response_id] = run

    def get(self, response_id: str) -> ResponseRun | None:
        """Return the live run for ``response_id``, or ``None``."""
        with self._lock:
            return self._runs.get(response_id)

    def unregister(self, response_id: str) -> None:
        """Drop ``response_id`` if present; never raises on a miss."""
        with self._lock:
            self._runs.pop(response_id, None)

    def open_runs(self) -> tuple[ResponseRun, ...]:
        """Return every registered run whose FSM has not terminated."""
        with self._lock:
            runs = tuple(self._runs.values())
        return tuple(run for run in runs if run.is_open)


@dataclass(frozen=True)
class CancelAccepted:
    """The terminal CAS was won and the run's token is now set."""

    response_id: str
    event: Event


@dataclass(frozen=True)
class CancelAlreadyTerminal:
    """The run already had a terminal; nothing was written, no token set."""

    response_id: str
    event: Event


@dataclass(frozen=True)
class CancelRejected:
    """The request was refused before any write."""

    response_id: str | None
    reason: Literal["unknown_response", "unsupported_scope", "policy_hash_mismatch"]


@dataclass(frozen=True)
class CancelTimedOut:
    """``BEGIN IMMEDIATE`` could not be acquired within ``cancel_timeout_ms``.

    The transaction never opened, so no terminal was written and the caller
    may retry.  This is deliberately distinct from ``CancelRejected``: a
    rejection knows the request was wrong, a timeout knows nothing at all.
    """

    response_id: str


type CancelOutcome = CancelAccepted | CancelAlreadyTerminal | CancelRejected | CancelTimedOut


@dataclass(frozen=True)
class ResponseCancelRequest:
    """ADR-0008 D10 request shape; both scopes carried, one implemented."""

    request_id: str
    response_id: str
    scope: ResponseCancelScope
    reason: str
    policy_hash: str | None = None
    expected_playback_generation_id: int | None = None
    source_utterance_id: str | None = None
    source_event_uid: str | None = None


class ResponseTerminalizer:
    """L3's single terminal owner and the sole caller of the response CAS."""

    def __init__(
        self,
        connect: Callable[[], sqlite3.Connection],
        *,
        close_after: bool,
        committed_event_bus: CommittedEventBus | None = None,
        failure_injector: FailureInjector | None = None,
    ) -> None:
        """Bind a connection factory and the optional L2 collaborators."""
        self._connect = connect
        self._close_after = close_after
        self._committed_event_bus = committed_event_bus
        self._failure_injector = failure_injector

    def _write(
        self,
        facts: ResponseRunFacts,
        *,
        event_type: str,
        payload: dict[str, object],
        source_event_id: str | None,
    ) -> TerminalOutcome:
        """Run one terminal CAS on a connection this terminalizer owns."""
        conn = self._connect()
        try:
            outcome = terminalize_response(
                conn,
                event_type=event_type,
                payload=payload,
                source_event_id=source_event_id,
                correlation={"turn_id": facts.turn_id},
                committed_event_bus=self._committed_event_bus,
                failure_injector=self._failure_injector,
            )
        finally:
            if self._close_after:
                with contextlib.suppress(sqlite3.Error):
                    conn.close()
        record_realtime_trace(
            "response_run_terminalized",
            response_id=facts.response_id,
            terminal_type=event_type,
            won_cas=isinstance(outcome, TerminalCommitted),
        )
        return outcome

    def complete(
        self,
        facts: ResponseRunFacts,
        *,
        response_hash: str,
        generated_text_hash: str | None = None,
        segment_count: int | None = None,
    ) -> TerminalOutcome:
        """Write ``response.completed`` for ``facts`` through the CAS."""
        payload: dict[str, object] = {
            "response_id": facts.response_id,
            "response_group_id": facts.response_group_id,
            "turn_id": facts.turn_id,
            "response_hash": response_hash,
        }
        if generated_text_hash is not None:
            payload["generated_text_hash"] = generated_text_hash
        if segment_count is not None:
            payload["segment_count"] = segment_count
        return self._write(
            facts,
            event_type="response.completed",
            payload=payload,
            source_event_id=facts.started_event_uid,
        )

    def cancel(
        self,
        facts: ResponseRunFacts,
        *,
        reason: str,
        cancel_scope: ResponseCancelScope,
        interrupted_by_utterance_id: str | None = None,
        source_event_id: str | None = None,
    ) -> TerminalOutcome:
        """Write ``response.cancelled`` for ``facts`` through the CAS."""
        payload: dict[str, object] = {
            "response_id": facts.response_id,
            "response_group_id": facts.response_group_id,
            "turn_id": facts.turn_id,
            "reason": reason,
            "cancel_scope": cancel_scope,
        }
        if interrupted_by_utterance_id is not None:
            payload["interrupted_by_utterance_id"] = interrupted_by_utterance_id
        return self._write(
            facts,
            event_type="response.cancelled",
            payload=payload,
            source_event_id=source_event_id or facts.started_event_uid,
        )

    def fail(
        self,
        facts: ResponseRunFacts,
        *,
        reason: str,
        retryable: bool | None = None,
    ) -> TerminalOutcome:
        """Write ``response.failed`` for ``facts`` through the CAS."""
        payload: dict[str, object] = {
            "response_id": facts.response_id,
            "response_group_id": facts.response_group_id,
            "turn_id": facts.turn_id,
            "reason": reason,
        }
        if retryable is not None:
            payload["retryable"] = retryable
        return self._write(
            facts,
            event_type="response.failed",
            payload=payload,
            source_event_id=facts.started_event_uid,
        )


def start_response_run(  # noqa: PLR0913 — the ADR-0008 §4.2 response.started payload shape.
    conn: sqlite3.Connection,
    *,
    turn_id: str,
    trigger_event_uid: str,
    request_client: LLMRequestClient,
    policy: ResponseEmissionPolicy,
    response_id: str,
    phase: ResponsePhase = "final",
    channel: ResponseChannel = "both",
    committed_event_bus: CommittedEventBus | None = None,
) -> ResponseRun:
    """Open one durable ResponseRun and return it already generating.

    ``response_id`` is passed in rather than minted here so the caller can
    build the per-run request client under the same id before the durable row
    exists.
    """
    response_group_id = stable_response_group_id(turn_id)
    snapshot = request_client.preset_snapshot
    payload: dict[str, object] = {
        "response_id": response_id,
        "response_group_id": response_group_id,
        "turn_id": turn_id,
        "phase": phase,
        "channel": channel,
        "emission_mode": policy.emission_mode,
        "output_risk_class": policy.output_risk_class,
        "required_gate_mode": policy.required_gate_mode,
        "policy_hash": policy.policy_hash,
        "active_subject_ref": policy.active_subject_ref,
        "evidence_snapshot_hash": policy.evidence_snapshot_hash,
        "risk_context_hash": policy.risk_context_hash,
        "classifier_rule_version": policy.classifier_rule_version,
        "provider": snapshot.provider,
        "model": snapshot.model,
    }
    started = append_response_started(
        conn,
        payload=payload,
        source_event_id=trigger_event_uid,
        correlation={"turn_id": turn_id},
        committed_event_bus=committed_event_bus,
    )
    run = ResponseRun(
        facts=ResponseRunFacts(
            response_id=response_id,
            response_group_id=response_group_id,
            turn_id=turn_id,
            started_event_uid=started.event_uid,
        ),
        phase=phase,
        channel=channel,
        emission_policy=policy,
        interrupt_policy=ResponseInterruptPolicy(
            response_id=response_id,
            policy_hash=policy.policy_hash,
        ),
        request_client=request_client,
    )
    run.mark("generating")
    return run


def request_response_cancel(  # noqa: PLR0911 — policy, timeout, and CAS outcomes remain distinct.
    registry: ResponseRunRegistry,
    terminalizer: ResponseTerminalizer,
    request: ResponseCancelRequest,
    *,
    deadline: float | None = None,
) -> CancelOutcome:
    """Cancel one live ResponseRun's generation, terminal-first.

    The terminal CAS runs before the token is set, so a cancel that arrives
    after ``response.completed`` reports ``CancelAlreadyTerminal`` and leaves
    the delivered answer alone (ADR-0008 D10).  ``scope="foreground_output"``
    is rejected until a playback lease exists to make its
    ``expected_playback_generation_id`` meaningful.
    """
    record_realtime_trace(
        "response_cancel_requested",
        response_id=request.response_id,
        scope=request.scope,
        reason=request.reason,
    )
    if request.scope != "generation":
        return CancelRejected(response_id=request.response_id, reason="unsupported_scope")
    run = registry.get(request.response_id)
    if run is None:
        return CancelRejected(response_id=request.response_id, reason="unknown_response")
    if (
        request.policy_hash is not None
        and request.policy_hash != run.interrupt_policy.policy_hash
    ):
        return CancelRejected(
            response_id=request.response_id,
            reason="policy_hash_mismatch",
        )

    acquired = run.admission_lock.acquire(
        timeout=-1 if deadline is None else max(0.0, deadline - time.monotonic()),
    )
    if not acquired:
        return CancelTimedOut(response_id=request.response_id)
    try:
        try:
            outcome = terminalizer.cancel(
                run.facts,
                reason=request.reason,
                cancel_scope=request.scope,
                interrupted_by_utterance_id=request.source_utterance_id,
                source_event_id=request.source_event_uid,
            )
        except sqlite3.OperationalError:
            return CancelTimedOut(response_id=request.response_id)

        if isinstance(outcome, AlreadyTerminal):
            return CancelAlreadyTerminal(
                response_id=request.response_id,
                event=outcome.event,
            )
        run.mark("cancelled")
        run.cancellation_token.cancel(reason=request.reason, scope=request.scope)
        return CancelAccepted(response_id=request.response_id, event=outcome.event)
    finally:
        run.admission_lock.release()


def reconcile_open_responses(
    conn: sqlite3.Connection,
    terminalizer: ResponseTerminalizer,
) -> tuple[Event, ...]:
    """Close every open ResponseRun once as ``daemon_restart`` (ADR F14).

    Idempotent by construction: the fold only returns runs with no terminal,
    and the CAS refuses a second one, so a repeated boot adds nothing.
    """
    closed: list[Event] = []
    for open_run in open_response_runs(conn):
        facts = ResponseRunFacts(
            response_id=open_run.response_id,
            response_group_id=open_run.response_group_id,
            turn_id=open_run.turn_id,
            started_event_uid=open_run.started_event_uid,
        )
        outcome = terminalizer.fail(facts, reason="daemon_restart", retryable=False)
        if isinstance(outcome, TerminalCommitted):
            closed.append(outcome.event)
    return tuple(closed)


__all__ = [
    "CancelAccepted",
    "CancelAlreadyTerminal",
    "CancelOutcome",
    "CancelRejected",
    "CancelTimedOut",
    "IllegalResponseTransitionError",
    "ResponseCancelRequest",
    "ResponseCancellationToken",
    "ResponseCancelledError",
    "ResponseChannel",
    "ResponseEmissionMode",
    "ResponseEmissionPolicy",
    "ResponsePhase",
    "ResponseRun",
    "ResponseRunFacts",
    "ResponseRunRegistry",
    "ResponseRunState",
    "ResponseTerminalizer",
    "evidence_snapshot_hash",
    "legacy_full_text_policy",
    "reconcile_open_responses",
    "request_response_cancel",
    "start_response_run",
]
