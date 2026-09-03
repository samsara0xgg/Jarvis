"""L4 execution boundary: handles, execution contexts, resource leases.

ADR-0008 D9 turns `spawn_worker` from "a handler the dispatcher blocks on"
into a real ActionRun.  This module is the provider-neutral half of that
change: it owns the executor, the per-job :class:`ActionExecutionContext`,
the canonical resource leases, the cleanup state machine and the terminal
arbitration.  It knows nothing about any specific tool — `jarvis.execution.
tools` builds the job and resolves the resource keys, so the import edge runs
one way (``tools`` -> ``action_runner``) and this module never re-enters the
registry.

Three facts are deliberately separate here, exactly as D9 requires:

* the **canonical action terminal** (`action.result_observed` / `action.failed`
  / `action.timeout_assumed` / `action.cancelled`), arbitrated by the shared
  L2 CAS in :mod:`jarvis.state.lifecycle_terminal`;
* **worker quiescence** (`worker.quiesced`), which says the owned task stopped
  writing;
* **repository cleanup** (`action.cleanup_completed` / `action.cleanup_failed`),
  which is the only thing that releases a write-exclusive lease.

A supervisor may declare `action.timeout_assumed` while the worker is still
suspected alive; the lease stays quarantined until quiescence *and* cleanup
finish.  That is why the three are not collapsed into one event.

Wave 4B is the migration slice ADR-0008 D9 calls "the legacy driver still
awaits the handle": :meth:`ActionRunner.submit` returns after dispatch, but
`ToolRegistry.dispatch` immediately calls :meth:`ActionHandle.result`, and
`drive_turn` still owns verify-then-stash-restore and therefore still triggers
:meth:`ActionRunner.finalize_cleanup`.  Wave 5 moves that trigger in here.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import sqlite3
import threading
import time
import uuid
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal

from jarvis.shared import RawResult, RawResultBundle
from jarvis.shared.realtime import TerminalCommitted
from jarvis.state.event_log import emit_event, iter_events_of_types, open_event_log
from jarvis.state.lifecycle_terminal import terminalize_action

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from jarvis.shared import Event
    from jarvis.shared.realtime import TerminalOutcome
    from jarvis.state.committed_event_bus import CommittedEventBus

LOGGER = logging.getLogger("jarvis.execution.action_runner")

type ResourceMode = Literal["read_shared", "write_exclusive", "global_exclusive"]
type CancellationMode = Literal["unsupported", "cooperative", "terminate_process"]
type VerificationOutcome = Literal["verified", "verification_skipped", "conflict_surfaced"]
type ActionCleanupState = Literal[
    "worker_active",
    "quiescing",
    "worker_quiesced",
    "verifying",
    "verification_skipped",
    "restoring_stash",
    "conflict_surfaced",
    "cleanup_completed",
    "cleanup_failed",
]

GLOBAL_RESOURCE_KEY: Final[str] = "*"
"""The single key a `global_exclusive` request occupies.

A mutating tool that declares no resource semantics defaults to this mode
(ADR-0008 D9 `undeclared_mutation_mode`), so it conflicts with every other
lease rather than silently running unlocked next to one.
"""

_MODE_RANK: Final[dict[str, int]] = {
    "read_shared": 0,
    "write_exclusive": 1,
    "global_exclusive": 2,
}

_LATE_WRITE_CAP: Final[int] = 100
"""ADR-0008 D9 bound on per-action late-worker telemetry records."""

_LEASE_POLL_STEP_S: Final[float] = 0.05
"""Re-check cadence for a blocked lease waiter.

The wait is notified on every release, so this only bounds how quickly a
``cancelled`` predicate or an expired deadline is noticed.
"""

_DEFAULT_LEASE_TIMEOUT_S: Final[float] = 900.0
"""Ceiling on waiting for a contended resource lease.

Chosen above the 600 s Codex turn budget plus its cleanup so an honest
same-repo queue never gives up on a run that is still making progress; a
lease wait that exceeds it is a stuck holder, not a slow one.
"""


class ActionRunnerError(RuntimeError):
    """Base class for every refusal this module raises."""


class ResourceKeyResolutionError(ActionRunnerError):
    """Resource keys could not be resolved, so the action must not run.

    ADR-0008 D9: resolution failure emits one
    `action.failed(reason="resource_key_resolution")` and never runs unlocked.
    """


class ResourceScopeEscalationError(ActionRunnerError):
    """A child asked for more than its parent's resource scope allows."""


class UnknownResourceScopeError(ActionRunnerError):
    """A child named a parent that holds no live scope."""


@dataclass(frozen=True)
class ToolConcurrency:
    """Resolved resource semantics for exactly one ActionRequest.

    ``parent_action_id`` is what turns this into a *borrow* request: when it
    is set, the runner proves the keys are a subset of that parent's live
    scope instead of acquiring a second lease on a repository the parent
    already owns (ADR-0008 F26).
    """

    resource_keys: tuple[str, ...]
    mode: ResourceMode
    independence_declared: bool = False
    parent_action_id: str | None = None


@dataclass(frozen=True)
class ResourceScope:
    """One granted lease, or one validated borrow of a parent's lease."""

    scope_id: str
    action_id: str
    resource_keys: frozenset[str]
    mode: ResourceMode
    borrowed_from: str | None = None

    @property
    def is_borrowed(self) -> bool:
        """Return whether this scope holds no lease of its own."""
        return self.borrowed_from is not None


def _conflicts(
    *,
    held_keys: frozenset[str],
    held_mode: ResourceMode,
    want_keys: frozenset[str],
    want_mode: ResourceMode,
) -> bool:
    """Return whether a pending request must wait for a held scope.

    Two `read_shared` scopes coexist; anything else that touches a common key
    serializes. `global_exclusive` conflicts with every live scope regardless
    of keys — that is what makes it the fail-closed default for a mutating
    tool with no declared resource semantics.
    """
    if "global_exclusive" in (held_mode, want_mode):
        return True
    if held_mode == "read_shared" and want_mode == "read_shared":
        return False
    return bool(held_keys & want_keys)


class ResourceLeaseTable:
    """In-process canonical-resource leases (ADR-0008 D9 `ToolConcurrency`).

    Deliberately in-process, not a SQLite table: the lease exists to keep two
    workers *of this daemon* off one repository's working tree and stash, and
    the daemon already holds an exclusive process lock. A durable table would
    add a second source of truth for a fact that dies with the process.
    Cross-restart safety comes from the durable cleanup fold instead —
    :meth:`ActionRunner.reconcile_quarantine` re-establishes a lease for every
    canonically-terminal action that never reached a cleanup event.
    """

    def __init__(self) -> None:
        """Create an empty lease table."""
        self._condition = threading.Condition(threading.Lock())
        self._scopes: dict[str, ResourceScope] = {}

    def acquire(
        self,
        *,
        action_id: str,
        keys: frozenset[str],
        mode: ResourceMode,
        timeout_s: float,
        cancelled: Callable[[], bool] | None = None,
    ) -> ResourceScope:
        """Block until no live scope conflicts, then grant one.

        Raises:
            TimeoutError: ``timeout_s`` elapsed with the request still blocked.
            ActionRunnerError: ``cancelled`` reported the wait is pointless.
        """
        # A monotonic deadline, not a count of wait() calls: Condition.wait
        # returns early on every notify_all, so summing the poll step would
        # over-estimate elapsed time and time a waiter out while its holder is
        # still making progress.
        deadline = None if timeout_s <= 0 else time.monotonic() + timeout_s
        scope = ResourceScope(
            scope_id="RSCOPE" + uuid.uuid4().hex,
            action_id=action_id,
            resource_keys=keys,
            mode=mode,
        )
        with self._condition:
            while self._blocked(scope):
                if cancelled is not None and cancelled():
                    msg = f"resource lease wait cancelled for action {action_id!r}"
                    raise ActionRunnerError(msg)
                if deadline is not None and time.monotonic() >= deadline:
                    msg = (
                        f"resource lease for {sorted(keys)!r} ({mode}) was still held "
                        f"after {timeout_s}s; action {action_id!r} refuses to run unlocked"
                    )
                    raise TimeoutError(msg)
                remaining = (
                    _LEASE_POLL_STEP_S
                    if deadline is None
                    else min(_LEASE_POLL_STEP_S, max(0.0, deadline - time.monotonic()))
                )
                self._condition.wait(remaining)
            self._scopes[scope.scope_id] = scope
            return scope

    def _blocked(self, want: ResourceScope) -> bool:
        """Return whether any live scope conflicts with ``want``."""
        return any(
            _conflicts(
                held_keys=held.resource_keys,
                held_mode=held.mode,
                want_keys=want.resource_keys,
                want_mode=want.mode,
            )
            for held in self._scopes.values()
            if held.action_id != want.action_id
        )

    def borrow(
        self,
        *,
        action_id: str,
        keys: frozenset[str],
        mode: ResourceMode,
        parent_action_id: str,
    ) -> ResourceScope:
        """Validate and grant a child's borrow of its parent's live scope.

        The child receives no lease token of its own: releasing the returned
        scope is a no-op, so a verification child cannot free the repository
        its parent is still mutating (ADR-0008 F26).

        Raises:
            UnknownResourceScopeError: ``parent_action_id`` holds no scope.
            ResourceScopeEscalationError: The child's keys are not a subset of
                the parent's, or its mode outranks the parent's.
        """
        with self._condition:
            parent = next(
                (s for s in self._scopes.values() if s.action_id == parent_action_id),
                None,
            )
            if parent is None:
                msg = (
                    f"action {action_id!r} cannot borrow from {parent_action_id!r}: "
                    f"that action holds no live resource scope"
                )
                raise UnknownResourceScopeError(msg)
            if not keys <= parent.resource_keys:
                msg = (
                    f"action {action_id!r} requested {sorted(keys - parent.resource_keys)!r} "
                    f"outside parent scope {sorted(parent.resource_keys)!r}"
                )
                raise ResourceScopeEscalationError(msg)
            if _MODE_RANK[mode] > _MODE_RANK[parent.mode]:
                msg = (
                    f"action {action_id!r} requested mode {mode!r} under a "
                    f"{parent.mode!r} parent scope; a child may not escalate"
                )
                raise ResourceScopeEscalationError(msg)
            return ResourceScope(
                scope_id=parent.scope_id,
                action_id=action_id,
                resource_keys=keys,
                mode=mode,
                borrowed_from=parent_action_id,
            )

    def release(self, scope: ResourceScope) -> None:
        """Drop a granted scope; a borrowed scope releases nothing."""
        if scope.is_borrowed:
            return
        with self._condition:
            self._scopes.pop(scope.scope_id, None)
            self._condition.notify_all()

    def live_scopes(self) -> tuple[ResourceScope, ...]:
        """Return every currently granted scope."""
        with self._condition:
            return tuple(self._scopes.values())


_CURRENT_CONTEXT: contextvars.ContextVar[ActionExecutionContext | None] = (
    contextvars.ContextVar("jarvis_action_execution_context", default=None)
)


def current_execution_context() -> ActionExecutionContext | None:
    """Return the running job's context, or ``None`` outside a runner job.

    This is how a cooperative handler observes a cancel request without
    changing the frozen four-argument ``ToolHandler`` signature that every
    tool in the registry is written against.
    """
    return _CURRENT_CONTEXT.get()


@dataclass
class ActionExecutionContext:
    """Per-job execution state and the D9 cleanup state machine.

    The cleanup state is what keeps a repository quarantined after the
    canonical terminal: `worker_quiesced` says the worker stopped, and only
    `cleanup_completed` / `cleanup_failed` releases the lease.
    """

    action_id: str
    worker_epoch: int
    running_event_uid: str
    turn_id: str | None
    run_id: str | None
    resource_scope: ResourceScope
    cancellation_mode: CancellationMode
    correlation: Mapping[str, str]
    on_terminal: Callable[[str], None] | None = None
    """Called with the event type when the *runner* writes this action's terminal.

    A cancel or an assumed timeout is the one terminal the handler does not
    write itself, so without this hook the in-process ``ActionLifecycle``
    would keep reporting a cancelled action as ``running``.
    """
    _cancel_requested: threading.Event = field(default_factory=threading.Event)
    _quiesced: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _state: ActionCleanupState = "worker_active"
    _cancel_reason: str | None = None
    _stash_ref: str | None = None
    _late_writes: deque[str] = field(default_factory=lambda: deque(maxlen=_LATE_WRITE_CAP))

    @property
    def cleanup_state(self) -> ActionCleanupState:
        """Return the current cleanup state."""
        with self._lock:
            return self._state

    def advance(self, state: ActionCleanupState) -> None:
        """Move the cleanup state machine; terminal states never move again."""
        with self._lock:
            if self._state in ("cleanup_completed", "cleanup_failed"):
                return
            self._state = state

    def request_cancel(self, reason: str) -> None:
        """Raise the cooperative cancel flag a handler may poll."""
        with self._lock:
            if self._cancel_reason is None:
                self._cancel_reason = reason
        self._cancel_requested.set()

    @property
    def is_cancel_requested(self) -> bool:
        """Return whether a cancel has been requested for this job."""
        return self._cancel_requested.is_set()

    @property
    def cancel_reason(self) -> str | None:
        """Return the reason of the first cancel request, if any."""
        with self._lock:
            return self._cancel_reason

    def record_stash_ref(self, stash_ref: str | None) -> None:
        """Record the pre-task stash this job set aside before it started.

        The handler is the only code that knows the ref, and on a cancelled
        or timed-out action the handler does not write the terminal — the
        runner does. Carrying the ref here is what lets that terminal name
        the stash, so the one durable source the cleanup finalizer reads
        stays complete on every path.
        """
        with self._lock:
            self._stash_ref = stash_ref

    @property
    def stash_ref(self) -> str | None:
        """Return the recorded pre-task stash ref, if the handler set one."""
        with self._lock:
            return self._stash_ref

    def mark_quiesced(self) -> None:
        """Record that the owned worker stopped running."""
        self.advance("worker_quiesced")
        self._quiesced.set()

    def wait_quiesced(self, timeout_s: float | None) -> bool:
        """Block until the worker is confirmed quiescent, or time out."""
        return self._quiesced.wait(timeout_s)

    @property
    def is_quiesced(self) -> bool:
        """Return whether the worker is confirmed quiescent."""
        return self._quiesced.is_set()

    def record_late_write(self, detail: str) -> None:
        """Record one post-terminal worker write and poison the cleanup.

        ADR-0008 D9: late-worker telemetry never releases a handle or a
        resource. A detected late write forces `cleanup_failed`, so the
        repository stays quarantined for a human rather than being handed to
        the next same-repo action.
        """
        with self._lock:
            self._late_writes.append(detail)
            if self._state not in ("cleanup_completed", "cleanup_failed"):
                self._state = "cleanup_failed"

    @property
    def late_writes(self) -> tuple[str, ...]:
        """Return the bounded late-write telemetry for this job."""
        with self._lock:
            return tuple(self._late_writes)


@dataclass(frozen=True)
class CancelAccepted:
    """Cancellation won the terminal CAS after confirmed quiescence."""

    action_id: str
    event_uid: str


@dataclass(frozen=True)
class CancelAlreadyTerminal:
    """The action already had a canonical terminal; nothing was written."""

    action_id: str
    event_uid: str
    terminal_type: str


@dataclass(frozen=True)
class CancelUnsupported:
    """The tool declares no cancellation capability (ADR-0008 F9)."""

    action_id: str


@dataclass(frozen=True)
class CancelUnconfirmed:
    """Quiescence was not confirmed in time; the action keeps running."""

    action_id: str
    reason: str


type CancelOutcome = (
    CancelAccepted | CancelAlreadyTerminal | CancelUnsupported | CancelUnconfirmed
)


@dataclass(frozen=True)
class ActionJob:
    """Everything the runner needs to execute one action, tool-agnostically.

    ``run`` receives the worker's own Event Log connection and the job's
    context; ``on_running`` is called once, on the worker thread, right after
    `action.running` commits, so the caller can advance its own in-memory
    lifecycle without the runner importing it.
    """

    action_id: str
    turn_id: str | None
    run_id: str | None
    concurrency: ToolConcurrency
    cancellation_mode: CancellationMode
    dispatched_event_uid: str
    correlation: Mapping[str, str]
    run: Callable[[sqlite3.Connection, ActionExecutionContext], RawResult | RawResultBundle]
    on_running: Callable[[str], None]
    carries_cleanup_debt: bool
    on_terminal: Callable[[str], None] | None = None
    """Optional hook for the terminals the runner writes instead of the handler."""
    on_dispatch_failure: Callable[[sqlite3.Connection, BaseException], None] | None = None
    """Called on the worker's own connection when the job never reached its handler.

    Resource-key resolution already happened at submit time, so the only way
    to land here is a lease that could not be taken. Nothing downstream will
    write that action's terminal — and on the background path nobody is
    holding the handle to notice — so the owner is given the worker's
    connection and told to record one.
    """


class ActionHandle:
    """ADR-0008 D9 handle over one submitted action."""

    def __init__(
        self,
        *,
        action_id: str,
        future: Future[RawResultBundle],
        context_ready: threading.Event,
        runner: ActionRunner,
        cancellation_mode: CancellationMode,
    ) -> None:
        """Bind the handle to its future and its owning runner."""
        self._action_id = action_id
        self._future = future
        self._context_ready = context_ready
        self._runner = runner
        self._cancellation_mode = cancellation_mode

    @property
    def action_id(self) -> str:
        """Return the action this handle owns."""
        return self._action_id

    @property
    def cancellation_mode(self) -> CancellationMode:
        """Return the declared cancellation capability."""
        return self._cancellation_mode

    def is_done(self) -> bool:
        """Return whether the worker finished (successfully or not)."""
        return self._future.done()

    def result(self, timeout: float | None = None) -> RawResultBundle:
        """Block for the worker's bundle, re-raising its exception verbatim."""
        return self._future.result(timeout)

    def context(self, timeout: float | None = 5.0) -> ActionExecutionContext | None:
        """Return the job's context once the worker has built one."""
        self._context_ready.wait(timeout)
        return self._runner.context_of(self._action_id)

    def cancel(self, reason: str, *, timeout_s: float = 5.0) -> CancelOutcome:
        """Request cancellation; emit `action.cancelled` only once quiescent.

        ADR-0008 D9/F9: an unsupported or unconfirmed cancellation leaves the
        action running and writes no event. Only confirmed quiescence lets the
        request compete for the canonical terminal, and the resource stays
        quarantined until cleanup regardless of who wins.
        """
        return self._runner.cancel_action(
            self._action_id,
            reason=reason,
            timeout_s=timeout_s,
            cancellation_mode=self._cancellation_mode,
        )

    def assume_timeout(self, reason: str) -> CancelOutcome:
        """Claim `action.timeout_assumed` while the worker may still be alive."""
        return self._runner.assume_timeout(self._action_id, reason=reason)


@dataclass(frozen=True)
class ActionSubmission:
    """What `submit` returns: a live handle plus the events it already wrote."""

    handle: ActionHandle
    initial_events: tuple[str, ...]


@dataclass
class _TurnCleanupRequest:
    """A turn owner's standing request to close its actions' cleanup debt.

    ADR-0008 D9 Step 4 moves cleanup ownership off the driver: the driver
    only *asks*, and the runner runs the finalizer at the one moment it is
    legal — after every action of that turn has quiesced. A background
    `spawn_worker` outlives its turn, so a driver that finalized directly
    would release a repository while Codex was still writing to it.
    """

    verification_outcome: VerificationOutcome
    on_finalize: Callable[[], VerificationOutcome | None] | None


class ActionRunner:
    """Owns the executor, the leases, and terminal arbitration for L4 actions."""

    def __init__(
        self,
        *,
        event_log_path: Path,
        max_concurrent_runs: int = 1,
        lease_timeout_s: float = _DEFAULT_LEASE_TIMEOUT_S,
        committed_event_bus: CommittedEventBus | None = None,
        leases: ResourceLeaseTable | None = None,
    ) -> None:
        """Construct a runner bound to one Event Log file."""
        self._event_log_path = event_log_path
        self._lease_timeout_s = lease_timeout_s
        self._committed_event_bus = committed_event_bus
        self._leases = leases if leases is not None else ResourceLeaseTable()
        # One thread per submitted job, and a semaphore that bounds only the
        # jobs actually RUNNING. ADR-0008 D9 puts it exactly this way —
        # "max_concurrent_runs limits jobs only after resource leases are
        # respected" — and the ordering is load-bearing, not stylistic. A
        # fixed worker pool lets a job that is merely *waiting* for a
        # contended repository occupy a slot; with the shipped
        # max_concurrent_runs=1 that starves the very turn whose cleanup would
        # release the lease, and the two sides wait for each other until the
        # lease timeout expires. Waiting for a lease must therefore cost a
        # thread, never a run slot.
        self._run_slots = threading.BoundedSemaphore(max(1, max_concurrent_runs))
        self._lock = threading.Lock()
        self._contexts: dict[str, ActionExecutionContext] = {}
        self._epochs: dict[str, int] = {}
        self._threads: list[threading.Thread] = []
        self._shutdown = threading.Event()
        # action_id -> turn_id for every accepted job that has not finished.
        # A job that is still queued behind a contended lease has no context
        # yet, so `_contexts` alone cannot answer "is this turn quiet?".
        self._inflight: dict[str, str | None] = {}
        self._turn_cleanups: dict[str, _TurnCleanupRequest] = {}

    @property
    def leases(self) -> ResourceLeaseTable:
        """Return the lease table this runner arbitrates through."""
        return self._leases

    def context_of(self, action_id: str) -> ActionExecutionContext | None:
        """Return the live execution context for ``action_id``, if any."""
        with self._lock:
            return self._contexts.get(action_id)

    def submit(self, job: ActionJob) -> ActionSubmission:
        """Accept one job and return after dispatch, not after completion.

        The resource lease is acquired on the worker thread, so a queued
        same-repo action does not block its submitter — `action.dispatched`
        already committed before this returns, and `action.running` is written
        only once the lease is actually held (ADR-0008 D9).
        """
        if self._shutdown.is_set():
            msg = "ActionRunner is shut down and cannot accept new jobs"
            raise ActionRunnerError(msg)
        with self._lock:
            epoch = self._epochs.get(job.action_id, -1) + 1
            self._epochs[job.action_id] = epoch
            self._inflight[job.action_id] = job.turn_id
        context_ready = threading.Event()
        future: Future[RawResultBundle] = Future()
        thread = threading.Thread(
            target=self._run_job,
            args=(job, epoch, context_ready, future),
            name=f"jarvis-action-{job.action_id}",
            daemon=True,
        )
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]
            self._threads.append(thread)
        try:
            thread.start()
        except BaseException:
            # An unstartable thread will never reach `_finish_job`, so drop
            # the in-flight marker here or this turn's cleanup waits forever.
            self._finish_job(job)
            raise
        return ActionSubmission(
            handle=ActionHandle(
                action_id=job.action_id,
                future=future,
                context_ready=context_ready,
                runner=self,
                cancellation_mode=job.cancellation_mode,
            ),
            initial_events=(job.dispatched_event_uid,),
        )

    def _run_job(
        self,
        job: ActionJob,
        epoch: int,
        context_ready: threading.Event,
        future: Future[RawResultBundle],
    ) -> None:
        """Own one job's thread: settle its future exactly once, always."""
        if not future.set_running_or_notify_cancel():  # pragma: no cover - never cancelled
            context_ready.set()
            return
        try:
            future.set_result(self._execute(job, epoch, context_ready))
        except BaseException as exc:  # noqa: BLE001 — the future is the only reporting channel.
            context_ready.set()
            future.set_exception(exc)

    def _execute(
        self,
        job: ActionJob,
        epoch: int,
        context_ready: threading.Event,
    ) -> RawResultBundle:
        """Run one job on its own connection, lease, and context."""
        conn = open_event_log(self._event_log_path)
        scope: ResourceScope | None = None
        context: ActionExecutionContext | None = None
        slot_held = False
        try:
            try:
                scope = self._enter_scope(job)
            except BaseException as exc:
                if job.on_dispatch_failure is not None:
                    job.on_dispatch_failure(conn, exc)
                raise
            # The run slot is taken only now, with the lease already in hand,
            # so a blocked waiter never holds one.
            self._run_slots.acquire()
            slot_held = True
            # `resource_keys`/`resource_mode` are what a later boot reads to
            # re-establish quarantine for an action that terminated without a
            # cleanup event; the in-process lease table dies with the process.
            running_event = emit_event(
                conn,
                type="action.running",
                payload={
                    "action_id": job.action_id,
                    "resource_keys": ",".join(sorted(scope.resource_keys)),
                    "resource_mode": scope.mode,
                },
                source_event_id=job.dispatched_event_uid,
                correlation=dict(job.correlation),
                committed_event_bus=self._committed_event_bus,
            )
            context = ActionExecutionContext(
                action_id=job.action_id,
                worker_epoch=epoch,
                running_event_uid=running_event.event_uid,
                turn_id=job.turn_id,
                run_id=job.run_id,
                resource_scope=scope,
                cancellation_mode=job.cancellation_mode,
                correlation=dict(job.correlation),
                on_terminal=job.on_terminal,
            )
            with self._lock:
                self._contexts[job.action_id] = context
            context_ready.set()
            job.on_running(running_event.event_uid)
            token = _CURRENT_CONTEXT.set(context)
            try:
                handler_result = job.run(conn, context)
            finally:
                _CURRENT_CONTEXT.reset(token)
            return (
                handler_result
                if isinstance(handler_result, RawResultBundle)
                else RawResultBundle(slots=(handler_result,))
            )
        finally:
            context_ready.set()
            self._quiesce(conn, job, context, scope)
            if slot_held:
                self._run_slots.release()
            with contextlib.suppress(sqlite3.Error):
                conn.close()
            # Last: dropping the in-flight marker is what can make this
            # turn's armed cleanup runnable, and the finalizer opens its own
            # connection, so it must not run while this one is still open.
            self._finish_job(job)

    def _enter_scope(self, job: ActionJob) -> ResourceScope:
        """Acquire or borrow this job's resource scope before it runs.

        A parent is used when the resolver named one, and otherwise when this
        turn already holds a covering scope. That second rule is the
        no-self-wait guard from ADR-0008 F26: while the legacy driver awaits
        each handle, one turn runs one action at a time, so a turn can never
        be "unrelated same-repo work" against itself — a `verify_diff` whose
        parent lookup came up empty must still borrow rather than block on the
        `spawn_worker` lease its own turn is holding. The borrow is validated
        exactly like an explicit one, so a child that asks for more keys or a
        stronger mode than the parent holds is still refused.
        """
        keys = frozenset(job.concurrency.resource_keys)
        parent_action_id = job.concurrency.parent_action_id
        if parent_action_id is None:
            parent_action_id = self._same_turn_scope_holder(job, keys)
        if parent_action_id is not None:
            return self._leases.borrow(
                action_id=job.action_id,
                keys=keys,
                mode=job.concurrency.mode,
                parent_action_id=parent_action_id,
            )
        return self._leases.acquire(
            action_id=job.action_id,
            keys=keys,
            mode=job.concurrency.mode,
            timeout_s=self._lease_timeout_s,
        )

    def _same_turn_scope_holder(self, job: ActionJob, keys: frozenset[str]) -> str | None:
        """Return this turn's live scope holder that fully covers this job.

        "Fully covers" means both the keys and the mode: this implicit path
        may only ever propose a borrow that will validate. A same-turn job
        that wants a STRONGER mode than its sibling holds is not a self-wait
        to be dissolved — it is a genuine conflict, and it belongs in the
        normal acquire queue where it blocks visibly instead of being handed a
        scope it did not earn.
        """
        if job.turn_id is None or not keys:
            return None
        with self._lock:
            turn_actions = {
                action_id
                for action_id, context in self._contexts.items()
                if context.turn_id == job.turn_id and action_id != job.action_id
            }
        if not turn_actions:
            return None
        return next(
            (
                scope.action_id
                for scope in self._leases.live_scopes()
                if scope.action_id in turn_actions
                and keys <= scope.resource_keys
                and _MODE_RANK[job.concurrency.mode] <= _MODE_RANK[scope.mode]
            ),
            None,
        )

    def _quiesce(
        self,
        conn: sqlite3.Connection,
        job: ActionJob,
        context: ActionExecutionContext | None,
        scope: ResourceScope | None,
    ) -> None:
        """Emit `worker.quiesced`, then release or quarantine the lease.

        A read-shared or borrowed scope carries no cleanup debt and frees
        here. A write-exclusive scope stays held: only the cleanup terminal
        may release the repository an action was mutating, so an unrelated
        same-repo action remains blocked through verification and stash
        restore (ADR-0008 F26).
        """
        if context is not None:
            try:
                emit_event(
                    conn,
                    type="worker.quiesced",
                    payload={
                        "action_id": job.action_id,
                        "worker_epoch": context.worker_epoch,
                        **({"run_id": job.run_id} if job.run_id is not None else {}),
                    },
                    source_event_id=context.running_event_uid,
                    correlation=dict(job.correlation),
                    committed_event_bus=self._committed_event_bus,
                )
            except Exception:
                # Never mask the handler's own result or exception behind a
                # telemetry write; the lease decision below still runs.
                LOGGER.exception(
                    "action runner could not record worker.quiesced (action_id=%r)",
                    job.action_id,
                )
            # Set the flag only after the row is durable. A canceller that
            # is blocked in `wait_quiesced` writes `action.cancelled` the
            # instant this flips, and a cleanup terminal that overtook its
            # own `worker.quiesced` would reorder the D9 trio in the log.
            context.mark_quiesced()
        if scope is None:
            return
        if not job.carries_cleanup_debt or scope.is_borrowed:
            self._leases.release(scope)
            with self._lock:
                self._contexts.pop(job.action_id, None)

    # --- terminal arbitration -----------------------------------------------

    def _terminalize(
        self,
        *,
        event_type: str,
        payload: dict[str, object],
        source_event_id: str | None,
        correlation: Mapping[str, str] | None,
    ) -> TerminalOutcome:
        """Run one action terminal CAS on a connection this runner owns."""
        conn = open_event_log(self._event_log_path)
        try:
            return terminalize_action(
                conn,
                event_type=event_type,
                payload=payload,
                source_event_id=source_event_id,
                correlation=dict(correlation) if correlation else None,
                committed_event_bus=self._committed_event_bus,
            )
        finally:
            with contextlib.suppress(sqlite3.Error):
                conn.close()

    def cancel_action(
        self,
        action_id: str,
        *,
        reason: str,
        timeout_s: float,
        cancellation_mode: CancellationMode,
    ) -> CancelOutcome:
        """Cooperatively cancel one action, terminalizing only if it quiesces."""
        if cancellation_mode == "unsupported":
            return CancelUnsupported(action_id=action_id)
        context = self.context_of(action_id)
        if context is None:
            return self._answer_without_context(action_id)
        context.advance("quiescing")
        context.request_cancel(reason)
        if not context.wait_quiesced(timeout_s):
            return CancelUnconfirmed(action_id=action_id, reason="quiescence_timeout")
        payload: dict[str, object] = {
            "action_id": action_id,
            "reason": reason,
            "cancellation_mode": cancellation_mode,
        }
        if context.turn_id is not None:
            payload["requested_by_turn_id"] = context.turn_id
        if context.stash_ref is not None:
            payload["stash_ref"] = context.stash_ref
        outcome = self._terminalize(
            event_type="action.cancelled",
            payload=payload,
            source_event_id=context.running_event_uid,
            correlation=context.correlation,
        )
        return self._cancel_result(context, outcome, expected="action.cancelled")

    def assume_timeout(self, action_id: str, *, reason: str) -> CancelOutcome:
        """Claim `action.timeout_assumed` and keep the resource quarantined."""
        context = self.context_of(action_id)
        if context is None:
            return self._answer_without_context(action_id)
        payload: dict[str, object] = {"action_id": action_id, "reason": reason}
        if context.stash_ref is not None:
            payload["stash_ref"] = context.stash_ref
        outcome = self._terminalize(
            event_type="action.timeout_assumed",
            payload=payload,
            source_event_id=context.running_event_uid,
            correlation=context.correlation,
        )
        return self._cancel_result(context, outcome, expected="action.timeout_assumed")

    def _answer_without_context(self, action_id: str) -> CancelOutcome:
        """Answer a cancel/timeout request for an action with no live context.

        A reaped context is not the same fact as an unknown action. When the
        durable fold already holds a canonical terminal, the honest answer is
        `already_terminal` — the request arrived too late, which is exactly
        what ADR-0008 D9 says a completed action returns. Only a genuinely
        untracked action stays `no_live_context`.
        """
        conn = open_event_log(self._event_log_path)
        try:
            terminal = _canonical_terminal_of(conn, action_id)
        finally:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
        if terminal is None:
            return CancelUnconfirmed(action_id=action_id, reason="no_live_context")
        return CancelAlreadyTerminal(
            action_id=action_id,
            event_uid=terminal.event_uid,
            terminal_type=terminal.type,
        )

    def _cancel_result(
        self,
        context: ActionExecutionContext,
        outcome: TerminalOutcome,
        *,
        expected: str,
    ) -> CancelOutcome:
        """Translate a terminal CAS result and tell the caller's FSM about it."""
        result = _cancel_outcome(context.action_id, outcome, expected=expected)
        if isinstance(result, CancelAccepted) and context.on_terminal is not None:
            context.on_terminal(expected)
        return result

    # --- cleanup ------------------------------------------------------------

    def finalize_cleanup(
        self,
        action_id: str,
        *,
        verification_outcome: VerificationOutcome,
        stash_ref: str | None = None,
    ) -> str | None:
        """Close one action's cleanup debt and release its lease.

        Returns the emitted event's uid, or ``None`` when this action held no
        cleanup debt (already finalized, or a read-shared/borrowed scope that
        freed at quiescence).
        """
        context = self.context_of(action_id)
        if context is None:
            return None
        failed = context.cleanup_state == "cleanup_failed" or bool(context.late_writes)
        payload: dict[str, object] = {
            "action_id": action_id,
            "worker_epoch": context.worker_epoch,
        }
        if stash_ref is not None:
            payload["stash_ref"] = stash_ref
        payload["resource_keys"] = ",".join(sorted(context.resource_scope.resource_keys))
        if failed:
            payload["reason"] = "late_worker_write"
            payload["quarantine_reason"] = "late_worker_write"
        else:
            payload["verification_outcome"] = verification_outcome

        conn = open_event_log(self._event_log_path)
        try:
            event = emit_event(
                conn,
                type="action.cleanup_failed" if failed else "action.cleanup_completed",
                payload=payload,
                source_event_id=context.running_event_uid,
                correlation=dict(context.correlation),
                committed_event_bus=self._committed_event_bus,
            )
        finally:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
        context.advance("cleanup_failed" if failed else "cleanup_completed")
        if not failed:
            # A poisoned cleanup keeps the repository quarantined for a human:
            # the lease is deliberately not released (ADR-0008 D9, F23).
            self._leases.release(context.resource_scope)
        with self._lock:
            self._contexts.pop(action_id, None)
        return event.event_uid

    def finalize_turn_cleanup(
        self,
        turn_id: str,
        *,
        verification_outcome: VerificationOutcome,
        on_finalize: Callable[[], VerificationOutcome | None] | None = None,
    ) -> tuple[str, ...]:
        """Ask the runner to close this turn's cleanup debt.

        ADR-0008 D9 Step 4: the turn owner asks, the runner decides *when*.
        If every action of this turn has already finished — which is always
        true while the driver awaits each handle — the finalizer runs inline
        on the caller's thread and this returns the cleanup event uids, the
        exact Wave-4B behaviour. If a truly background worker is still
        running, the request is armed instead and the finalizer runs on that
        worker's own thread the moment it finishes. Releasing a repository
        earlier would hand a tree Codex is still writing to the next action.

        Args:
            turn_id: The turn whose actions should be cleaned up.
            verification_outcome: What to record when ``on_finalize`` does
                not derive one itself.
            on_finalize: Runtime-owned work that must happen before any
                cleanup terminal — stash restore and live-action release.
                It runs at most once per request, on whichever thread ends
                up owning the cleanup, and may return the outcome it just
                established (a stash conflict, say) to override the
                argument above.

        Returns:
            The cleanup event uids emitted synchronously; empty when the
            request was armed for a still-running worker.
        """
        with self._lock:
            self._turn_cleanups[turn_id] = _TurnCleanupRequest(
                verification_outcome=verification_outcome,
                on_finalize=on_finalize,
            )
        return self._run_turn_cleanup_if_ready(turn_id)

    def _finish_job(self, job: ActionJob) -> None:
        """Drop one job's in-flight marker and settle its turn if it is now quiet."""
        with self._lock:
            self._inflight.pop(job.action_id, None)
        if job.turn_id is not None:
            self._run_turn_cleanup_if_ready(job.turn_id)

    def _run_turn_cleanup_if_ready(self, turn_id: str) -> tuple[str, ...]:
        """Run an armed turn cleanup once no action of that turn is live.

        The claim of the request out of ``_turn_cleanups`` happens under the
        lock, so exactly one thread ever runs a given request even when the
        driver and the last worker arrive together.
        """
        with self._lock:
            request = self._turn_cleanups.get(turn_id)
            if request is None:
                return ()
            if any(owner == turn_id for owner in self._inflight.values()):
                return ()
            del self._turn_cleanups[turn_id]
            action_ids = [
                action_id
                for action_id, context in self._contexts.items()
                if context.turn_id == turn_id
            ]

        outcome = request.verification_outcome
        if request.on_finalize is not None:
            derived = request.on_finalize()
            if derived is not None:
                outcome = derived
        return tuple(
            uid
            for action_id in action_ids
            if (uid := self.finalize_cleanup(action_id, verification_outcome=outcome)) is not None
        )

    def reconcile_quarantine(self, conn: sqlite3.Connection) -> tuple[str, ...]:
        """Re-establish quarantine for terminated actions that never cleaned up.

        ADR-0008 D9: startup scans canonical-terminal actions lacking a
        cleanup terminal and re-establishes the resource quarantine before
        accepting new conflicting work. The lease is taken here with no
        context, so nothing in this process can release it — a human must.
        Returns the action ids re-quarantined.
        """
        quarantined: list[str] = []
        for action_id, keys in _actions_awaiting_cleanup(conn).items():
            if not keys:
                continue
            with contextlib.suppress(TimeoutError, ActionRunnerError):
                self._leases.acquire(
                    action_id=action_id,
                    keys=keys,
                    mode="write_exclusive",
                    timeout_s=0.001,
                )
                quarantined.append(action_id)
        return tuple(quarantined)

    def shutdown(
        self,
        *,
        wait: bool = True,
        timeout_s: float = 30.0,
        cancel: bool = False,
        reason: str = "daemon_shutdown",
    ) -> tuple[str, ...]:
        """Stop accepting jobs and drain the in-flight ones.

        With ``cancel=True`` — what the daemon passes — every live action
        whose tool declares a cancellation capability is asked to stop and
        then terminalized, so a background Codex worker is genuinely drained
        instead of abandoned mid-turn with its repository stashed. Actions
        that declare no capability are left to finish; F9 forbids writing a
        cancel terminal we cannot stand behind.

        Returns the action ids that reached a cancel terminal here.
        """
        self._shutdown.set()
        cancelled: list[str] = []
        if cancel:
            deadline = time.monotonic() + timeout_s
            with self._lock:
                live = [
                    context
                    for action_id, context in self._contexts.items()
                    if action_id in self._inflight
                ]
            for context in live:
                if context.cancellation_mode == "unsupported":
                    continue
                outcome = self.cancel_action(
                    context.action_id,
                    reason=reason,
                    timeout_s=max(0.0, deadline - time.monotonic()),
                    cancellation_mode=context.cancellation_mode,
                )
                if isinstance(outcome, CancelAccepted):
                    cancelled.append(context.action_id)
        if not wait:
            return tuple(cancelled)
        with self._lock:
            threads = list(self._threads)
        deadline = time.monotonic() + timeout_s
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        return tuple(cancelled)


def _cancel_outcome(
    action_id: str,
    outcome: TerminalOutcome,
    *,
    expected: str,
) -> CancelOutcome:
    """Translate a terminal CAS result into a cancel outcome."""
    if isinstance(outcome, TerminalCommitted) and outcome.event.type == expected:
        return CancelAccepted(action_id=action_id, event_uid=outcome.event.event_uid)
    return CancelAlreadyTerminal(
        action_id=action_id,
        event_uid=outcome.event.event_uid,
        terminal_type=outcome.event.type,
    )


_CANONICAL_ACTION_TERMINALS: Final[tuple[str, ...]] = (
    "action.result_observed",
    "action.failed",
    "action.timeout_assumed",
    "action.cancelled",
)
_CLEANUP_TERMINALS: Final[tuple[str, ...]] = (
    "action.cleanup_completed",
    "action.cleanup_failed",
)


def _canonical_terminal_of(conn: sqlite3.Connection, action_id: str) -> Event | None:
    """Return this action's canonical terminal from the durable fold, if any."""
    return next(
        (
            event
            for event in iter_events_of_types(conn, _CANONICAL_ACTION_TERMINALS)
            if event.payload.get("action_id") == action_id
        ),
        None,
    )


def _actions_awaiting_cleanup(conn: sqlite3.Connection) -> dict[str, frozenset[str]]:
    """Fold write-exclusive actions that terminated with no cleanup event.

    `action.running` carries the keys the runner actually leased, so this is
    the only durable record of what a crashed process was holding. An action
    whose `action.running` declared no write-exclusive keys is not
    re-quarantined: it held nothing that a later action could corrupt.
    """
    leased: dict[str, frozenset[str]] = {}
    terminated: set[str] = set()
    cleaned: set[str] = set()
    for event in iter_events_of_types(
        conn,
        ("action.running", *_CANONICAL_ACTION_TERMINALS, *_CLEANUP_TERMINALS),
    ):
        action_id = event.payload.get("action_id")
        if not isinstance(action_id, str) or not action_id:
            continue
        if event.type in _CLEANUP_TERMINALS:
            cleaned.add(action_id)
        elif event.type in _CANONICAL_ACTION_TERMINALS:
            terminated.add(action_id)
        elif event.payload.get("resource_mode") == "read_shared":
            continue
        else:
            raw_keys = event.payload.get("resource_keys")
            if isinstance(raw_keys, str):
                leased[action_id] = frozenset(part for part in raw_keys.split(",") if part)
    return {
        action_id: keys
        for action_id, keys in leased.items()
        if action_id in terminated and action_id not in cleaned
    }


__all__ = [
    "GLOBAL_RESOURCE_KEY",
    "ActionCleanupState",
    "ActionExecutionContext",
    "ActionHandle",
    "ActionJob",
    "ActionRunner",
    "ActionRunnerError",
    "ActionSubmission",
    "CancelAccepted",
    "CancelAlreadyTerminal",
    "CancelOutcome",
    "CancelUnconfirmed",
    "CancelUnsupported",
    "CancellationMode",
    "ResourceKeyResolutionError",
    "ResourceLeaseTable",
    "ResourceMode",
    "ResourceScope",
    "ResourceScopeEscalationError",
    "ToolConcurrency",
    "UnknownResourceScopeError",
    "VerificationOutcome",
    "current_execution_context",
]
