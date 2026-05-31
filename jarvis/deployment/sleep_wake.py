"""macOS IOPM sleep/wake observer + reconciliation for in-flight workers.

Per ADR-0002 § Sleep/wake protocol (lines 1100-1156) + spec §3.7.8.

A 3-minute Codex turn easily spans a Mac sleep. The detached child must
observe sleep/wake and reconcile in-flight state. Day-2 implements the
Mac-only minimum (cross-domain publication deferred per deviation V4):
IOPM observer + ``reconcile_after_wake`` handle the case fail-closed.

The macOS IOPM bindings (via PyObjC ``Foundation.NSWorkspace``) are
stubbed behind a :class:`PowerObserver` protocol so unit tests inject a
fake observer that fires ``simulate_sleep`` / ``simulate_wake``
deterministically without touching system power events. K7 / K8 Tier-2
invariants reuse the same injection point against live Codex.

The Codex subprocess almost always dies through a sleep — macOS power
management does not preserve subprocess sockets/pipes across deep sleep.
Reconciliation is the spec's mandated fail-closed behavior: emit
``worker.terminated_by_sleep`` + ``action.timeout_assumed`` (which a
later Limitation Claim will reference); never silently mark the action
complete.

Layer boundary (``.importlinter``): jarvis.deployment may import
jarvis.state (state sits below deployment in the layer DAG). Lazy
PyObjC import keeps non-Mac CI / unit tests free of the binding.

References:
- ADR-0002 § Sleep/wake protocol (lines 1100-1156)
- spec.html §3.7.8 (sleep/wake protocol)
- ADR-0002 deviation V4 (Mac-only minimum; cross-domain publication deferred)
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

from jarvis.state.event_log import emit_event, iter_events

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

    from jarvis.shared import Event


# Terminal event types that close an action's lifecycle. An ``action_id``
# that reached ``run.started`` (so a worker run is registered) but carries
# no event of a type in this set is considered "in-flight" by
# ``_in_progress_actions`` and a candidate for fail-closed reconciliation
# on wake.
_TERMINAL_ACTION_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "action.result_observed",
        "action.failed",
        "action.timeout_assumed",
        "action.cancelled",
    },
)


class PowerObserver(Protocol):
    """Power observer contract used by :func:`install_power_observer`.

    The real macOS observer subscribes to ``NSWorkspaceWillSleepNotification``
    / ``NSWorkspaceDidWakeNotification`` (PyObjC). Unit tests inject a stub
    that fires ``simulate_sleep`` / ``simulate_wake`` manually.
    """

    def register(
        self,
        *,
        before_sleep: Callable[[], None],
        on_wake: Callable[[], None],
    ) -> None:
        """Wire the before-sleep and on-wake callbacks.

        ``before_sleep`` runs in the kernel-notified callback path (best
        effort — may not fire if power is yanked). ``on_wake`` runs after
        the kernel re-arms userspace.
        """
        ...

    def shutdown(self) -> None:
        """Best-effort de-registration of the kernel notification handlers."""
        ...


# --- Real observer (Mac-only, lazy PyObjC import) ---------------------------


class _NSWorkspacePowerObserver:
    """NSWorkspace-backed observer (real Mac code path).

    Day-2 minimum: holds the callbacks and records that registration
    happened. The full Cocoa wiring (NSNotificationCenter
    ``addObserver`` plumbing through an NSObject delegate) is deferred —
    Step 17 will exercise this against the live system; reliability of
    the sleep notification itself is best-effort by spec §3.7.8, with
    :func:`reconcile_after_wake` providing the fail-closed safety net.
    """

    def __init__(self) -> None:
        self._before_sleep: Callable[[], None] | None = None
        self._on_wake: Callable[[], None] | None = None
        self._registered: bool = False

    def register(
        self,
        *,
        before_sleep: Callable[[], None],
        on_wake: Callable[[], None],
    ) -> None:
        """Subscribe to NSWorkspaceWillSleep / NSWorkspaceDidWake."""
        self._before_sleep = before_sleep
        self._on_wake = on_wake
        # Lazy import: PyObjC is a Mac-only optional dep; non-Mac CI
        # never reaches this branch (factory raises on non-darwin), and
        # mypy on non-Mac runners cannot find the stub at all. F401 +
        # PLC0415 are silenced for the placeholder until Step 17 wires
        # NSNotificationCenter; type-ignore covers the missing stub.
        try:
            from Foundation import NSWorkspace  # type:ignore[import-not-found] # noqa:F401,PLC0415
        except ImportError as exc:
            msg = (
                "PyObjC required for real IOPM observer. "
                "Install with: pip install pyobjc-framework-Cocoa"
            )
            raise RuntimeError(msg) from exc
        # Day-2 minimum records that registration happened. The actual
        # NSNotificationCenter subscription (with an NSObject delegate)
        # is wired in Step 17 against live Codex; reliability of the
        # notification is best-effort by spec §3.7.8.
        self._registered = True

    def shutdown(self) -> None:
        """Mark the observer de-registered (best-effort)."""
        self._registered = False


def _real_observer_factory() -> PowerObserver:
    """Construct the real macOS IOPM-backed observer.

    Raises ``RuntimeError`` on non-macOS platforms — unit tests must
    inject ``observer_factory=<stub-factory>`` rather than relying on
    the real binding.
    """
    if sys.platform != "darwin":
        msg = (
            "Real IOPM observer requires macOS; "
            "pass observer_factory=<stub> in tests."
        )
        raise RuntimeError(msg)
    return _NSWorkspacePowerObserver()


# --- install_power_observer ------------------------------------------------


def install_power_observer(
    event_log: sqlite3.Connection | None,
    *,
    observer_factory: Callable[[], PowerObserver] | None = None,
) -> PowerObserver:
    """Register the macOS power observer; wire sleep/wake callbacks.

    ``observer_factory`` defaults to :func:`_real_observer_factory` (real
    macOS IOPM-backed observer). K7 / K8 Tier-2 invariants inject a stub
    factory whose ``simulate_sleep`` / ``simulate_wake`` methods fire the
    IOPM callbacks deterministically; unit tests do the same.

    The before-sleep callback emits ``mac.sleeping`` (with the list of
    in-progress ``action_id``s) plus one ``worker.suspended_by_sleep``
    per in-flight action. The on-wake callback emits ``mac.awake`` and
    invokes :func:`reconcile_after_wake` to fail-closed any orphan runs
    that the sleep killed.

    Args:
        event_log: Open Event Log connection (from
            :func:`jarvis.state.event_log.open_event_log`). The Day-1
            shape uses ``sqlite3.Connection`` directly as the event-log
            handle.
        observer_factory: Callable returning a :class:`PowerObserver`.
            Defaults to the real macOS observer. Tests inject a stub.

    Returns:
        The registered observer. The caller may later call
        ``observer.shutdown()`` to release the IOPM notification handler.

    Raises:
        ValueError: If ``event_log`` is None.
    """
    if event_log is None:
        msg = "install_power_observer requires a non-None event_log connection"
        raise ValueError(msg)
    conn: sqlite3.Connection = event_log

    factory = observer_factory if observer_factory is not None else _real_observer_factory
    observer = factory()

    def _before_sleep() -> None:
        """Emit mac.sleeping + per-action worker.suspended_by_sleep."""
        in_progress = _in_progress_actions(conn)
        emit_event(
            conn,
            type="mac.sleeping",
            payload={
                "ts_epoch_ms": int(time.time() * 1000),
                "reason": "system_sleep",
                "in_progress_action_ids": [a.action_id for a in in_progress],
            },
        )
        for action in in_progress:
            payload: dict[str, object] = {
                "run_id": action.run_id,
                "action_id": action.action_id,
            }
            if action.last_heartbeat_ts is not None:
                payload["last_heartbeat_ts"] = action.last_heartbeat_ts
            emit_event(
                conn,
                type="worker.suspended_by_sleep",
                payload=payload,
                correlation={
                    "run_id": action.run_id,
                    "action_id": action.action_id,
                },
            )

    def _on_wake() -> None:
        """Emit mac.awake + run reconcile_after_wake (fail-closed)."""
        slept_for_ms = _slept_for_ms(conn)
        emit_event(
            conn,
            type="mac.awake",
            payload={
                "ts_epoch_ms": int(time.time() * 1000),
                "slept_for_ms": slept_for_ms,
            },
        )
        reconcile_after_wake(conn)

    observer.register(before_sleep=_before_sleep, on_wake=_on_wake)
    return observer


# --- reconcile_after_wake --------------------------------------------------


def reconcile_after_wake(event_log: sqlite3.Connection | None) -> int:
    """Fail-closed reconciliation for in-flight actions. Returns closed count.

    For each ``action.running`` event with no terminal event yet, emit
    ``worker.terminated_by_sleep`` followed by ``action.timeout_assumed``
    so the Result Interpreter / projection can derive a Limitation
    outcome. Idempotent: dedup by checking whether
    ``action.timeout_assumed`` already exists for the ``action_id``
    (via :func:`_in_progress_actions`), so invoking twice in succession
    does not double-emit.

    The mac.* events are emitted by :func:`install_power_observer`'s
    callbacks; this function only handles the per-action fail-closed
    reconciliation, which is the safe-to-call-multiple-times path.

    Args:
        event_log: Open Event Log connection.

    Returns:
        Number of actions newly closed by this call (0 on idempotent
        re-invocation).

    Raises:
        ValueError: If ``event_log`` is None.
    """
    if event_log is None:
        msg = "reconcile_after_wake requires a non-None event_log connection"
        raise ValueError(msg)
    conn: sqlite3.Connection = event_log

    closed = 0
    for action in _in_progress_actions(conn):
        emit_event(
            conn,
            type="worker.terminated_by_sleep",
            payload={
                "run_id": action.run_id,
                "action_id": action.action_id,
                "reason": "subprocess_lost_to_sleep",
            },
            correlation={
                "run_id": action.run_id,
                "action_id": action.action_id,
            },
        )
        emit_event(
            conn,
            type="action.timeout_assumed",
            payload={
                "action_id": action.action_id,
                "reason": "lost_to_sleep",
            },
            correlation={"action_id": action.action_id},
        )
        closed += 1
    return closed


# --- Internal helpers ------------------------------------------------------


@dataclass(frozen=True)
class _InProgressAction:
    """Snapshot of one in-flight action used by the sleep/wake fold.

    A worker run's ``run_id`` is first written to the log by the codex
    handler's ``run.started`` event, NOT by ``action.running``. The
    generic L4 dispatcher emits ``action.running`` with
    ``correlation={action_id, turn_id}`` and no ``run_id`` (it is minted
    inside the handler, several steps after dispatch — see
    :mod:`jarvis.execution.tools`). The fold therefore associates each
    action's ``run_id`` from its ``run.started`` event and silently skips
    actions that never reached ``run.started`` (e.g. a failed
    ``codex --version`` preflight), so the fail-closed reconciliation
    path only emits worker.* events for actions a worker actually owns.
    """

    action_id: str
    run_id: str
    last_heartbeat_ts: str | None


def _correlation_run_id(evt: Event) -> str | None:
    """Pull ``run_id`` out of an event's correlation map; None if absent."""
    if evt.correlation is None:
        return None
    raw_run_id = evt.correlation.get("run_id")
    return None if raw_run_id is None else str(raw_run_id)


def _correlation_action_id(evt: Event) -> str | None:
    """Pull ``action_id`` out of an event's correlation map; None if absent."""
    if evt.correlation is None:
        return None
    raw_action_id = evt.correlation.get("action_id")
    return None if raw_action_id is None else str(raw_action_id)


def _note_run_id(
    pending: dict[str, dict[str, str | None]],
    action_id: str,
    run_id: str | None,
) -> None:
    """Register ``action_id`` if unseen; fill its ``run_id`` once known.

    ``action.running`` from the generic dispatcher registers the action
    with a ``None`` run_id; the later ``run.started`` (or a unit-test seed
    that bakes the run_id straight onto ``action.running``) fills it in.
    The first non-None run_id wins; a terminal event evicts the action
    before any re-run, so per-action run_id never needs overwriting.
    """
    record = pending.get(action_id)
    if record is None:
        pending[action_id] = {"run_id": run_id, "last_heartbeat_ts": None}
    elif run_id is not None and record["run_id"] is None:
        record["run_id"] = run_id


def _apply_lifecycle_event(
    pending: dict[str, dict[str, str | None]],
    evt: Event,
) -> None:
    """Refresh a heartbeat timestamp or evict on a terminal event."""
    raw_action_id = evt.payload.get("action_id")
    if raw_action_id is None:
        return
    action_id = str(raw_action_id)
    if evt.type == "worker.heartbeat":
        record = pending.get(action_id)
        if record is not None:
            record["last_heartbeat_ts"] = str(evt.ts_epoch_ms)
    elif evt.type in _TERMINAL_ACTION_EVENT_TYPES:
        pending.pop(action_id, None)


def _in_progress_actions(event_log: sqlite3.Connection) -> list[_InProgressAction]:
    """Return one record per action that has a started run but no terminal.

    Walks the event log once. ``action.running`` registers an action;
    ``run.started`` supplies its ``run_id`` (the first event that carries
    one for a real worker run — ``action.running`` from the generic
    dispatcher has only ``{action_id, turn_id}``). A later terminal event
    for that ``action_id`` evicts the entry. Actions that never reached
    ``run.started`` carry a ``None`` run_id and are skipped (no worker was
    ever registered). The returned list preserves first-seen order.
    """
    pending: dict[str, dict[str, str | None]] = {}
    for evt in iter_events(event_log):
        if evt.type == "action.running":
            _note_run_id(pending, str(evt.payload["action_id"]), _correlation_run_id(evt))
        elif evt.type == "run.started":
            started_action_id = _correlation_action_id(evt)
            if started_action_id is not None:
                _note_run_id(pending, started_action_id, _correlation_run_id(evt))
        else:
            _apply_lifecycle_event(pending, evt)
    out: list[_InProgressAction] = []
    for action_id, record in pending.items():
        run_id = record["run_id"]
        if run_id is None:
            # No run.started — the worker never registered (e.g. a failed
            # codex --version preflight); nothing to fail-closed.
            continue
        out.append(
            _InProgressAction(
                action_id=action_id,
                run_id=run_id,
                last_heartbeat_ts=record["last_heartbeat_ts"],
            ),
        )
    return out


def _slept_for_ms(event_log: sqlite3.Connection) -> int:
    """Compute milliseconds since the most recent ``mac.sleeping`` event.

    Returns ``0`` when no ``mac.sleeping`` event has been emitted (e.g.
    test paths that call :meth:`StubPowerObserver.simulate_wake` without
    a prior ``simulate_sleep``). The bound is non-negative.
    """
    last_sleep_ts: int | None = None
    for evt in iter_events(event_log):
        if evt.type == "mac.sleeping":
            last_sleep_ts = evt.ts_epoch_ms
    if last_sleep_ts is None:
        return 0
    delta = int(time.time() * 1000) - last_sleep_ts
    return max(delta, 0)


__all__ = [
    "PowerObserver",
    "install_power_observer",
    "reconcile_after_wake",
]
