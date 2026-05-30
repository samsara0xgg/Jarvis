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
# present in ``action.running`` but absent from every type in this set is
# considered "in-flight" by ``_in_progress_actions`` and a candidate for
# fail-closed reconciliation on wake.
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

    Day-1 in-flight actions always carry a non-None ``run_id`` (the L4
    dispatcher emits ``action.running`` with ``correlation={run_id,
    action_id, ...}`` per :mod:`jarvis.execution.tools`). The fold
    silently skips action.running rows that lack a run_id so the
    fail-closed reconciliation path only emits worker.* events for
    actions a worker actually owns.
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


def _in_progress_actions(event_log: sqlite3.Connection) -> list[_InProgressAction]:
    """Return one record per action that has ``action.running`` but no terminal.

    Walks the event log once. For each ``action.running`` event, record
    its ``action_id`` and the ``run_id`` from the correlation field. Any
    later event of a terminal type for that ``action_id`` removes the
    entry. The returned list preserves first-seen order.
    """
    pending: dict[str, dict[str, str | None]] = {}
    for evt in iter_events(event_log):
        if evt.type == "action.running":
            action_id = str(evt.payload["action_id"])
            if action_id not in pending:
                pending[action_id] = {
                    "run_id": _correlation_run_id(evt),
                    "last_heartbeat_ts": None,
                }
            continue
        # Heartbeats refresh last_heartbeat_ts; terminal events evict.
        raw_action_id = evt.payload.get("action_id")
        if raw_action_id is None:
            continue
        action_id = str(raw_action_id)
        if evt.type == "worker.heartbeat":
            record = pending.get(action_id)
            if record is not None:
                record["last_heartbeat_ts"] = str(evt.ts_epoch_ms)
        elif evt.type in _TERMINAL_ACTION_EVENT_TYPES:
            pending.pop(action_id, None)
    out: list[_InProgressAction] = []
    for action_id, record in pending.items():
        run_id = record["run_id"]
        if run_id is None:
            # No worker correlation — skip; this action is not a
            # spawn_worker run and reconciliation has no run_id to emit.
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
