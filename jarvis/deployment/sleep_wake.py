"""macOS IOKit sleep/wake observer + reconciliation for in-flight workers.

Per ADR-0002 § Sleep/wake protocol (lines 1100-1156), ADR-0009 D3, and
spec §3.7.8.

A 3-minute Codex turn easily spans a Mac sleep. The daemon must observe
sleep/wake and reconcile in-flight state. Mac-only minimum
(cross-domain publication deferred per ADR-0002 deviation V4): power
observer + ``reconcile_after_wake`` handle the case fail-closed.

The real observer is :class:`_IOKitPowerObserver` —
``IORegisterForSystemPower`` driven over **ctypes** against IOKit +
CoreFoundation (ADR-0009 D3: PyObjC does not wrap IOKit at all, so
ctypes is the mechanism and costs no dependency). The exact signatures,
message codes, ack calls and teardown order come from the Step-0 spike
``scripts/spike_power_observer.py``, which proved them against a real
``pmset sleepnow``.

It sits behind a :class:`PowerObserver` protocol so unit tests inject a
fake observer that fires ``simulate_sleep`` / ``simulate_wake``
deterministically without touching system power events. K7 / K8 Tier-2
invariants reuse the same injection point against live Codex.

**Callbacks run on the CFRunLoop thread, never on the asyncio loop.**
The Event Log connection is opened ``check_same_thread=True``, so an
emit wired straight into ``before_sleep`` / ``on_wake`` raises here.
:func:`install_power_observer` therefore takes the loop that owns the
connection (``loop=``) and marshals both callbacks onto it with
``call_soon_threadsafe`` + a bounded wait (ADR-0009 Step 4 — see
:func:`_marshal_onto_loop`). Without ``loop=`` the callbacks stay inline
on the firing thread, which is what the one-shot CLI and the stub-driven
K7/K8 tests want. Either way the emit is survivable: both callbacks are
invoked guarded (the exception is logged and swallowed) and
``IOAllowPowerChange`` is then called unconditionally, so a failed or
timed-out emit never vetoes or delays a sleep — the fail-closed wake-side
reconciliation is the safety net (spec §3.7.8 — "sleep hook 不可靠").

The Codex subprocess almost always dies through a sleep — macOS power
management does not preserve subprocess sockets/pipes across deep sleep.
Reconciliation is the spec's mandated fail-closed behavior: emit
``worker.terminated_by_sleep`` + ``action.timeout_assumed`` (which a
later Limitation Claim will reference); never silently mark the action
complete.

Supervisor sweep (ADR-0009 D4, spec §3.4.8)
===========================================

:func:`sweep_overdue_actions` lives here rather than in a module of its
own because H13 scopes the ``deployment -> jarvis.state.event_log``
exception to this one file, and because it shares the open-action fold
with the wake path. There is exactly ONE fold
(:func:`_open_actions`); the two consumers differ only in what they
project out of it — the wake path keeps actions that reached
``run.started`` (it names a run in ``worker.terminated_by_sleep``), the
sweep keeps all of them and reads deadlines. Copying the fold instead of
parameterizing it is how the two paths would drift apart.

The sweep is an emitter, not a second brain: one
``action.timeout_assumed`` per overdue action, and the existing Result
Interpreter ladder turns that into the Limitation claim.

Layer boundary (``.importlinter``): jarvis.deployment may import
jarvis.state (state sits below deployment in the layer DAG). The
framework handles are dlopened lazily by :func:`_load_power_binding`,
so importing this module binds nothing — unit tests and non-Mac hosts
never touch IOKit. The live action-id set the sweep excludes is owned by
the composition root and arrives as a plain ``frozenset[str]`` argument;
``deployment`` must never import ``execution`` to learn it (D4).

References:
- ADR-0002 § Sleep/wake protocol (lines 1100-1156)
- ADR-0009 D3 (ctypes-IOKit observer, ack protocol, pinned teardown)
- spec.html §3.7.8 (sleep/wake protocol)
- ADR-0002 deviation V4 (Mac-only minimum; cross-domain publication deferred)
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import logging
import sys
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol

from jarvis.state.event_log import emit_event, iter_events
from jarvis.state.lifecycle_terminal import terminalize_action

if TYPE_CHECKING:
    import asyncio
    import sqlite3
    from collections.abc import Callable, Iterator

LOGGER = logging.getLogger("jarvis.deployment.sleep_wake")


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

# Sorted so the SQL placeholder tuples below bind deterministically.
_TERMINAL_ACTION_EVENT_TYPES_ORDERED: Final[tuple[str, ...]] = tuple(
    sorted(_TERMINAL_ACTION_EVENT_TYPES),
)

# The complete set of event types the shared open-action fold reads.
# ADR-0009 D4 pins the fold to a typed SELECT over exactly these rather
# than ``iter_events`` over the whole log: the supervisor sweep runs this
# every ``supervisor.sweep_interval_s`` forever, on a log this same ADR
# makes grow per minute (repo observer). Same IN-list shape as the
# runtime's ``_SELECT_NEXT_TRIGGER_SQL`` watcher query.
_OPEN_ACTION_EVENT_TYPES: Final[tuple[str, ...]] = (
    "action.dispatched",
    "action.running",
    "run.started",
    "worker.heartbeat",
    *_TERMINAL_ACTION_EVENT_TYPES_ORDERED,
)

_SELECT_OPEN_ACTION_ROWS_SQL: Final[str] = (  # noqa: S608 — placeholder interpolation is over a hard-coded type tuple, not user input.
    "SELECT type, ts_epoch_ms, payload_json, correlation_json FROM events "
    "WHERE type IN ({placeholders}) ORDER BY id ASC"
).format(placeholders=",".join("?" for _ in _OPEN_ACTION_EVENT_TYPES))

_SELECT_TERMINAL_FOR_ACTION_SQL: Final[str] = (  # noqa: S608 — placeholder interpolation is over a hard-coded type tuple, not user input.
    "SELECT 1 FROM events WHERE type IN ({placeholders}) "
    "AND json_extract(payload_json, '$.action_id') = ? LIMIT 1"
).format(
    placeholders=",".join("?" for _ in _TERMINAL_ACTION_EVENT_TYPES_ORDERED),
)

# ADR-0009 D4 fallback budget for actions whose ``action.dispatched`` row
# predates the Step-5 ``result_expected_by_ms`` stamp. Matches
# ``supervisor.default_budget_s`` in ``config/jarvis.yaml``, which is what
# the Step-7 periodic task passes in; the constant is the standalone
# default so the sweep is callable (and testable) without config.
_DEFAULT_SUPERVISOR_BUDGET_S: Final[float] = 700.0

# ``action.timeout_assumed.reason`` written by the sweep. Distinct from
# the wake path's ``lost_to_sleep`` so the Result Interpreter's Limitation
# claim — and any human reading the trace — can tell "the machine slept"
# from "nobody ever reported back".
_SWEEP_TIMEOUT_REASON: Final[str] = "supervisor_sweep"


class PowerObserver(Protocol):
    """Power observer contract used by :func:`install_power_observer`.

    The real macOS observer (:class:`_IOKitPowerObserver`) registers for
    IOKit system-power notifications on a dedicated CFRunLoop thread.
    Unit tests inject a stub that fires ``simulate_sleep`` /
    ``simulate_wake`` manually.
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


# --- Real observer: ctypes-IOKit on a dedicated CFRunLoop thread -----------

_IOKIT_FRAMEWORK: Final = "/System/Library/Frameworks/IOKit.framework/IOKit"
_COREFOUNDATION_FRAMEWORK: Final = (
    "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
)

# void (*IOServiceInterestCallback)(void *refcon, io_service_t service,
#                                   natural_t messageType, void *messageArgument)
_CALLBACK_TYPE = ctypes.CFUNCTYPE(
    None,
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.c_void_p,
)

# IOMessage.h power codes (iokit_common_msg base 0xE0000000).
_MSG_CAN_SYSTEM_SLEEP: Final = 0xE0000270
_MSG_WILL_SLEEP: Final = 0xE0000280
_MSG_HAS_POWERED_ON: Final = 0xE0000300

# The two messages the kernel waits on. Both are acknowledged with
# ``IOAllowPowerChange`` — Jarvis never vetoes a sleep (spec §3.7.8).
# Note ``kIOMessageCanSystemSleep`` is skipped entirely for forced
# sleeps (spike-proven), so nothing may depend on seeing it.
_ACK_MESSAGES: Final[frozenset[int]] = frozenset(
    {_MSG_CAN_SYSTEM_SLEEP, _MSG_WILL_SLEEP},
)

# The runloop thread publishes its CFRunLoop ref before parking; the
# join bound keeps ``shutdown()`` from blocking the serve teardown.
# MUST stay > _MARSHAL_ACK_BOUND_S — see the note on that constant.
_RUNLOOP_READY_TIMEOUT_S: Final = 5.0
_RUNLOOP_JOIN_TIMEOUT_S: Final = 5.0


@dataclass(frozen=True)
class _PowerBinding:
    """The IOKit / CoreFoundation entry points the observer calls.

    Bundled into one frozen struct so unit tests can inject a fake and
    drive the observer's whole control flow — ack protocol, closed flag,
    teardown order — without registering with the kernel. Resolved by
    :func:`_load_power_binding`, which is the only place a framework is
    dlopened.
    """

    register_for_system_power: Callable[..., int]
    notification_port_get_runloop_source: Callable[..., int | None]
    allow_power_change: Callable[..., int]
    deregister_for_system_power: Callable[..., int]
    service_close: Callable[..., int]
    notification_port_destroy: Callable[..., None]
    runloop_get_current: Callable[..., int | None]
    runloop_add_source: Callable[..., None]
    runloop_run: Callable[..., None]
    runloop_stop: Callable[..., None]
    runloop_default_mode: object


def _load_power_binding() -> _PowerBinding:
    """Load IOKit + CoreFoundation and pin every argtype / restype.

    Signatures are copied verbatim from ``scripts/spike_power_observer.py``
    (ADR-0009 Step 0), which proved them end-to-end against a real
    ``pmset sleepnow`` plus a scheduled wake. Called only from
    :func:`_real_observer_factory`, so importing this module never binds
    a framework.
    """
    iokit = ctypes.CDLL(_IOKIT_FRAMEWORK)
    corefoundation = ctypes.CDLL(_COREFOUNDATION_FRAMEWORK)

    # io_connect_t IORegisterForSystemPower(void *refcon,
    #     IONotificationPortRef *thePortRef,
    #     IOServiceInterestCallback callback, io_object_t *notifier)
    iokit.IORegisterForSystemPower.restype = ctypes.c_uint32
    iokit.IORegisterForSystemPower.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        _CALLBACK_TYPE,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    iokit.IONotificationPortGetRunLoopSource.restype = ctypes.c_void_p
    iokit.IONotificationPortGetRunLoopSource.argtypes = [ctypes.c_void_p]
    iokit.IOAllowPowerChange.restype = ctypes.c_int
    iokit.IOAllowPowerChange.argtypes = [ctypes.c_uint32, ctypes.c_ssize_t]
    iokit.IODeregisterForSystemPower.restype = ctypes.c_int
    iokit.IODeregisterForSystemPower.argtypes = [ctypes.POINTER(ctypes.c_uint32)]
    iokit.IOServiceClose.restype = ctypes.c_int
    iokit.IOServiceClose.argtypes = [ctypes.c_uint32]
    iokit.IONotificationPortDestroy.restype = None
    iokit.IONotificationPortDestroy.argtypes = [ctypes.c_void_p]

    corefoundation.CFRunLoopGetCurrent.restype = ctypes.c_void_p
    corefoundation.CFRunLoopGetCurrent.argtypes = []
    corefoundation.CFRunLoopAddSource.restype = None
    corefoundation.CFRunLoopAddSource.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    corefoundation.CFRunLoopRun.restype = None
    corefoundation.CFRunLoopRun.argtypes = []
    corefoundation.CFRunLoopStop.restype = None
    corefoundation.CFRunLoopStop.argtypes = [ctypes.c_void_p]

    return _PowerBinding(
        register_for_system_power=iokit.IORegisterForSystemPower,
        notification_port_get_runloop_source=(
            iokit.IONotificationPortGetRunLoopSource
        ),
        allow_power_change=iokit.IOAllowPowerChange,
        deregister_for_system_power=iokit.IODeregisterForSystemPower,
        service_close=iokit.IOServiceClose,
        notification_port_destroy=iokit.IONotificationPortDestroy,
        runloop_get_current=corefoundation.CFRunLoopGetCurrent,
        runloop_add_source=corefoundation.CFRunLoopAddSource,
        runloop_run=corefoundation.CFRunLoopRun,
        runloop_stop=corefoundation.CFRunLoopStop,
        runloop_default_mode=ctypes.c_void_p.in_dll(
            corefoundation, "kCFRunLoopDefaultMode",
        ),
    )


class _IOKitPowerObserver:
    """IOKit system-power observer on a dedicated CFRunLoop thread.

    ``IORegisterForSystemPower`` delivers notifications through a
    CFRunLoop source. The daemon has no CFRunLoop of its own (its main
    thread runs asyncio), so the observer owns one on a daemon thread —
    the architecture the Step-0 spike proved.

    Threading contract: ``before_sleep`` / ``on_wake`` are invoked **on
    that thread**, not on the asyncio loop, so the Event Log's
    ``check_same_thread=True`` connection rejects a directly-wired emit.
    Both callbacks are therefore invoked guarded (exception logged,
    swallowed) and ``IOAllowPowerChange`` is called unconditionally
    afterwards — a broken callback can never veto or stall a sleep.
    Marshaling the emit onto the loop is
    :func:`install_power_observer`'s job, via its ``loop=`` argument
    (ADR-0009 Step 4); ``serve_inherent`` is what supplies the loop.

    Lifetime hazard: ctypes does not own the trampoline it builds for a
    Python callback. :attr:`_callback_ref` keeps it alive for the whole
    object lifetime — dropping it while IOKit still holds the pointer
    crashes the runloop thread on the next notification.
    """

    def __init__(self, *, binding: _PowerBinding) -> None:
        """Build an unregistered observer over ``binding``.

        Constructing spins no thread and touches no kernel state;
        :meth:`register` does both.
        """
        self._binding = binding
        self._before_sleep: Callable[[], None] | None = None
        self._on_wake: Callable[[], None] | None = None
        self._root_port = ctypes.c_uint32(0)
        self._notify_port = ctypes.c_void_p(None)
        self._notifier = ctypes.c_uint32(0)
        self._runloop_ref = ctypes.c_void_p(None)
        self._runloop_ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False
        # Held for the observer's lifetime — see the class docstring.
        self._callback_ref = _CALLBACK_TYPE(self._on_power_message)

    def register(
        self,
        *,
        before_sleep: Callable[[], None],
        on_wake: Callable[[], None],
    ) -> None:
        """Subscribe to IOKit power notifications; start the CFRunLoop thread.

        Raises:
            RuntimeError: If ``IORegisterForSystemPower`` fails (root
                port 0) or the runloop thread never comes up. Either
                path can leave a *partial* registration behind, so
                :func:`install_power_observer` unwinds it with
                :meth:`shutdown` before re-raising — the caller of
                ``install_power_observer`` never receives the observer
                and structurally cannot do it itself. Callers degrade
                to running without a power observer (ADR-0009 F4); the
                bootstrap sweep still closes orphans.
        """
        self._before_sleep = before_sleep
        self._on_wake = on_wake
        self._root_port = ctypes.c_uint32(
            self._binding.register_for_system_power(
                None,
                ctypes.byref(self._notify_port),
                self._callback_ref,
                ctypes.byref(self._notifier),
            ),
        )
        if self._root_port.value == 0:
            msg = "IORegisterForSystemPower failed (root port 0)"
            raise RuntimeError(msg)
        thread = threading.Thread(
            target=self._runloop_thread,
            name="jarvis-power-observer",
            daemon=True,
        )
        self._thread = thread
        thread.start()
        if not self._runloop_ready.wait(timeout=_RUNLOOP_READY_TIMEOUT_S):
            msg = (
                "power observer runloop thread did not come up within "
                f"{_RUNLOOP_READY_TIMEOUT_S}s"
            )
            raise RuntimeError(msg)

    def shutdown(self) -> None:
        """Tear the registration down in the ADR-0009 D3 pinned order.

        Closed flag first (a notification racing this teardown must find
        it and return), then ``IODeregisterForSystemPower`` +
        ``IOServiceClose``, then ``CFRunLoopStop`` and the thread join,
        and only then ``IONotificationPortDestroy`` — destroying the
        port while the runloop thread still owns its source is what
        turns a clean exit into a crash. Idempotent.
        """
        if self._closed:
            return
        self._closed = True
        if self._root_port.value == 0:
            # register() never succeeded — nothing to unwind.
            return
        binding = self._binding
        binding.deregister_for_system_power(ctypes.byref(self._notifier))
        binding.service_close(self._root_port.value)
        if self._runloop_ref.value is not None:
            binding.runloop_stop(self._runloop_ref)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=_RUNLOOP_JOIN_TIMEOUT_S)
            self._thread = None
        binding.notification_port_destroy(self._notify_port)

    # -- CFRunLoop thread -------------------------------------------------

    def _runloop_thread(self) -> None:
        """Own a CFRunLoop, attach the notification source, and park in it."""
        binding = self._binding
        self._runloop_ref = ctypes.c_void_p(binding.runloop_get_current())
        source = binding.notification_port_get_runloop_source(self._notify_port)
        binding.runloop_add_source(
            self._runloop_ref, source, binding.runloop_default_mode,
        )
        self._runloop_ready.set()
        binding.runloop_run()

    def _on_power_message(
        self,
        _refcon: int | None,
        _service: int,
        message_type: int,
        message_argument: int | None,
    ) -> None:
        """IOKit callback. Runs on the CFRunLoop thread — never on the loop."""
        if self._closed:
            # Late notification after shutdown(): the root port is
            # already closed and the asyncio loop may be gone, so both
            # the callback and the ack would be use-after-close.
            return
        if message_type == _MSG_WILL_SLEEP:
            self._invoke_guarded(self._before_sleep, "before_sleep")
        elif message_type == _MSG_HAS_POWERED_ON:
            self._invoke_guarded(self._on_wake, "on_wake")
        if message_type in _ACK_MESSAGES:
            self._allow_power_change(message_argument)

    def _invoke_guarded(
        self,
        callback: Callable[[], None] | None,
        label: str,
    ) -> None:
        """Run ``callback``, swallowing any exception so the ack still happens."""
        if callback is None:
            return
        try:
            callback()
        except Exception:
            # A raising callback must never veto or stall a sleep: log it
            # and fall through to the unconditional ack.
            LOGGER.exception("power observer %s callback raised", label)

    def _allow_power_change(self, message_argument: int | None) -> None:
        """Acknowledge the pending power change. Never veto (spec §3.7.8)."""
        rc = self._binding.allow_power_change(
            self._root_port.value, message_argument or 0,
        )
        if rc != 0:
            LOGGER.warning("IOAllowPowerChange returned %d", rc)


def _real_observer_factory() -> PowerObserver:
    """Construct the real macOS IOKit-backed observer.

    Raises ``RuntimeError`` on non-macOS platforms — unit tests must
    inject ``observer_factory=<stub-factory>`` rather than relying on
    the real binding.
    """
    if sys.platform != "darwin":
        msg = (
            "Real IOKit power observer requires macOS; "
            "pass observer_factory=<stub> in tests."
        )
        raise RuntimeError(msg)
    return _IOKitPowerObserver(binding=_load_power_binding())


# --- install_power_observer ------------------------------------------------


# ADR-0009 D3: the before-sleep emit is scheduled onto the asyncio loop and
# waited on for at most this long, then ``IOAllowPowerChange`` fires
# regardless. The OS gives a bounded ack window and Jarvis never vetoes a
# sleep; a missed bound degrades to the F3 wake-side reconciliation, which
# is the mandatory half of spec §3.7.8 ("Sleep hook 不可靠").
#
# MUST stay < _RUNLOOP_JOIN_TIMEOUT_S. If a notification is in flight when
# ``shutdown()`` runs, the CFRunLoop thread is blocked in ``finished.wait``
# while the loop thread is blocked in ``thread.join`` — deadlocked on each
# other until one bound expires. The shorter bound here is what breaks the
# tie; inverting the two makes every such teardown pay the full join.
_MARSHAL_ACK_BOUND_S: Final = 3.0


def _marshal_onto_loop(
    callback: Callable[[], None],
    *,
    loop: asyncio.AbstractEventLoop,
    label: str,
) -> Callable[[], None]:
    """Wrap ``callback`` so it runs on ``loop``'s thread, bounded-wait.

    The IOKit observer fires its callbacks on the CFRunLoop thread while
    the Event Log connection is ``check_same_thread=True`` on the loop
    thread, so the emit must hop. ``call_soon_threadsafe`` does the hop;
    a :class:`threading.Event` gives the CFRunLoop thread a bounded wait
    so the emit is (usually) durable before the kernel is acked.

    The returned wrapper NEVER raises and never blocks past
    :data:`_MARSHAL_ACK_BOUND_S` — both would stall or veto a system
    sleep. It must only be fired from a thread that is not the loop's
    own (as the CFRunLoop thread always is); calling it from the loop
    thread would wait out the full bound and accomplish nothing.
    """

    def _marshaled() -> None:
        finished = threading.Event()

        def _run_on_loop() -> None:
            try:
                callback()
            except Exception:
                # Mirrors the observer's own guard: a raising emit must
                # not escape onto the loop's exception handler either.
                LOGGER.exception("power observer %s callback raised", label)
            finally:
                finished.set()

        try:
            loop.call_soon_threadsafe(_run_on_loop)
        except RuntimeError:
            # Loop already closed (teardown race). Nothing to emit onto.
            LOGGER.warning(
                "power observer %s: event loop is closed; emit skipped", label,
            )
            return
        if not finished.wait(timeout=_MARSHAL_ACK_BOUND_S):
            LOGGER.warning(
                "power observer %s: loop did not run the emit within %.1fs; "
                "acking the power change anyway (wake-side reconciliation "
                "covers it)",
                label,
                _MARSHAL_ACK_BOUND_S,
            )

    return _marshaled


def install_power_observer(
    event_log: sqlite3.Connection | None,
    *,
    observer_factory: Callable[[], PowerObserver] | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
    before_sleep_hook: Callable[[], object] | None = None,
    on_wake_hook: Callable[[], object] | None = None,
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
        loop: Asyncio loop that owns ``event_log``. When given (the
            ``serve_inherent`` daemon wiring, ADR-0009 D3), BOTH
            callbacks are marshaled onto it via
            :func:`_marshal_onto_loop` — mandatory for the real
            observer, whose callbacks arrive on the CFRunLoop thread
            while ``event_log`` is loop-thread-only. Left ``None``
            (default) the callbacks run inline on whatever thread fires
            them, which is the one-shot CLI's shape and the byte-for-byte
            K7/K8 stub contract.
        before_sleep_hook: Optional runtime-owned bounded media shutdown
            called before the durable sleep events are emitted.
        on_wake_hook: Optional runtime-owned bounded media reopen called after
            durable wake reconciliation.

    Returns:
        The registered observer. The caller may later call
        ``observer.shutdown()`` to release the IOPM notification handler.

    Raises:
        ValueError: If ``event_log`` is None.
        Exception: Whatever ``observer.register`` raises — re-raised
            after the half-registered observer has been shut down here,
            because the caller never receives a reference to unwind.
    """
    if event_log is None:
        msg = "install_power_observer requires a non-None event_log connection"
        raise ValueError(msg)
    conn: sqlite3.Connection = event_log

    factory = observer_factory if observer_factory is not None else _real_observer_factory
    observer = factory()

    def _before_sleep() -> None:
        """Emit mac.sleeping + per-action worker.suspended_by_sleep."""
        if before_sleep_hook is not None:
            before_sleep_hook()
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
        if on_wake_hook is not None:
            on_wake_hook()

    before_sleep_cb: Callable[[], None] = _before_sleep
    on_wake_cb: Callable[[], None] = _on_wake
    if loop is not None:
        before_sleep_cb = _marshal_onto_loop(
            _before_sleep, loop=loop, label="before_sleep",
        )
        on_wake_cb = _marshal_onto_loop(_on_wake, loop=loop, label="on_wake")

    try:
        observer.register(before_sleep=before_sleep_cb, on_wake=on_wake_cb)
    except BaseException:
        # register() can fail AFTER IORegisterForSystemPower handed back a
        # live root port (runloop thread never came up). We are the only
        # holder of the reference at this point, so the unwind is ours.
        with contextlib.suppress(Exception):
            observer.shutdown()
        raise
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
        # ADR-0008 F8 — a supervisor terminal races L4's own result. The CAS
        # picks one winner inside `BEGIN IMMEDIATE`; the loser gets
        # `AlreadyTerminal` and appends nothing, so an action can never carry
        # two canonical terminals.
        terminalize_action(
            conn,
            event_type="action.timeout_assumed",
            payload={
                "action_id": action.action_id,
                "reason": "lost_to_sleep",
            },
            correlation={"action_id": action.action_id},
        )
        closed += 1
    return closed


# --- sweep_overdue_actions -------------------------------------------------


def sweep_overdue_actions(
    event_log: sqlite3.Connection | None,
    *,
    active_action_ids: frozenset[str] = frozenset(),
    default_budget_s: float = _DEFAULT_SUPERVISOR_BUDGET_S,
) -> int:
    """Close every open action past its deadline. Returns the count closed.

    The spec §3.4.8 mandate ("Scheduler / supervisor 必须扫 open actions,
    超过 result_expected_by 且无 terminal event 时 emit timeout/limitation")
    realized as ADR-0009 D4. Runs both as a one-shot bootstrap sweep at
    serve start and as the periodic task (Step 7 wires both).

    Emits exactly ONE ``action.timeout_assumed`` per closure —
    ``reason="supervisor_sweep"``, ``run_id`` on the correlation when the
    action reached ``run.started``. Deliberately no ``worker.*`` event,
    for run-less and run-ful orphans alike: ``run_id`` is born at
    ``run.started``, so a run-less orphan has no run to declare
    terminated, and ADR-0009 §4 registers no supervisor-side worker type
    that would be true of the others (reusing
    ``worker.terminated_by_sleep`` would put a false cause on the trace).

    The sweep is an emitter, not a second brain: the Result Interpreter
    and ``_handle_action_terminal_failure`` already turn a timeout into
    the Limitation claim, and the ADR-0002 amendment routes it to
    ``queue_review``.

    Double-emission is excluded twice over (F7 — "at most one
    ``action.timeout_assumed`` per action_id under every interleaving"):
    ``active_action_ids`` skips actions a live turn is still driving, and
    :func:`_has_terminal_event` re-checks the log immediately before each
    emit, so a detached child or the in-turn driver closing an action
    mid-sweep cancels the pending emit.

    Args:
        event_log: Open Event Log connection.
        active_action_ids: Snapshot of the action_ids live turns are
            currently driving. A value, not a callable: the sweep is then
            a pure function of (log, active set, budget), and the guard
            that actually has to be race-tight is the pre-emit terminal
            re-check, which reads the log rather than this set. The
            composition root owns the set and passes the snapshot down,
            so ``deployment`` never imports ``execution``.
        default_budget_s: Fallback budget, in seconds, for rows lacking
            the dispatcher-stamped ``result_expected_by_ms`` (see
            :func:`_deadline_ms`). Defaults to
            ``supervisor.default_budget_s``.

    Returns:
        Number of actions closed by this call (0 on a quiet sweep).

    Raises:
        ValueError: If ``event_log`` is None.
    """
    if event_log is None:
        msg = "sweep_overdue_actions requires a non-None event_log connection"
        raise ValueError(msg)
    conn: sqlite3.Connection = event_log

    now_ms = int(time.time() * 1000)
    budget_ms = int(default_budget_s * 1000)
    closed = 0
    for action in _open_actions(conn):
        if action.action_id in active_action_ids:
            continue
        deadline_ms = _deadline_ms(action, budget_ms=budget_ms)
        if deadline_ms is None or now_ms < deadline_ms:
            continue
        if _has_terminal_event(conn, action.action_id):
            continue
        correlation = {"action_id": action.action_id}
        if action.run_id is not None:
            correlation["run_id"] = action.run_id
        # ADR-0008 F8 — `_has_terminal_event` above is a cheap pre-filter,
        # not the arbiter: it is a check-then-act that a concurrent L4 result
        # can slip through. The CAS is what actually decides.
        terminalize_action(
            conn,
            event_type="action.timeout_assumed",
            payload={
                "action_id": action.action_id,
                "reason": _SWEEP_TIMEOUT_REASON,
            },
            correlation=correlation,
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

@dataclass(frozen=True)
class _OpenAction:
    """One action the log shows as open — the shared fold's output row.

    Superset of :class:`_InProgressAction`: ``run_id`` may be ``None``
    (the action never reached ``run.started``) and the deadline anchors
    are carried for the supervisor sweep. The wake path narrows this
    back down in :func:`_in_progress_actions`.
    """

    action_id: str
    run_id: str | None
    last_heartbeat_ts: str | None
    dispatched_ts_ms: int | None
    running_ts_ms: int | None
    result_expected_by_ms: int | None


@dataclass
class _PendingAction:
    """Mutable accumulator for one action_id while the fold runs."""

    run_id: str | None = None
    last_heartbeat_ts: str | None = None
    dispatched_ts_ms: int | None = None
    running_ts_ms: int | None = None
    result_expected_by_ms: int | None = None


@dataclass(frozen=True)
class _LifecycleRow:
    """One row of the typed open-action SELECT, JSON already parsed."""

    type: str
    ts_epoch_ms: int
    payload: dict[str, Any]
    correlation: dict[str, Any]


def _iter_lifecycle_rows(event_log: sqlite3.Connection) -> Iterator[_LifecycleRow]:
    """Yield the action/run lifecycle rows in log order, and nothing else.

    ADR-0009 D4: type-scoped, not ``iter_events`` — see
    :data:`_OPEN_ACTION_EVENT_TYPES`. Only the four columns the fold
    reads are selected, so no full :class:`jarvis.shared.Event`
    hydration happens on the sweep's every-30s path.
    """
    cursor = event_log.execute(_SELECT_OPEN_ACTION_ROWS_SQL, _OPEN_ACTION_EVENT_TYPES)
    for type_, ts_epoch_ms, payload_json, correlation_json in cursor:
        yield _LifecycleRow(
            type=str(type_),
            ts_epoch_ms=int(ts_epoch_ms),
            payload=json.loads(payload_json),
            correlation={} if correlation_json is None else json.loads(correlation_json),
        )


def _correlation_str(row: _LifecycleRow, key: str) -> str | None:
    """Pull ``key`` out of a row's correlation map; None if absent."""
    raw = row.correlation.get(key)
    return None if raw is None else str(raw)


def _note_run_id(
    pending: dict[str, _PendingAction],
    action_id: str,
    run_id: str | None,
) -> _PendingAction:
    """Register ``action_id`` if unseen; fill its ``run_id`` once known.

    ``action.running`` from the generic dispatcher registers the action
    with a ``None`` run_id; the later ``run.started`` (or a unit-test seed
    that bakes the run_id straight onto ``action.running``) fills it in.
    The first non-None run_id wins; a terminal event evicts the action
    before any re-run, so per-action run_id never needs overwriting.
    """
    record = pending.get(action_id)
    if record is None:
        record = _PendingAction(run_id=run_id)
        pending[action_id] = record
    elif run_id is not None and record.run_id is None:
        record.run_id = run_id
    return record


def _note_dispatched(record: _PendingAction, row: _LifecycleRow) -> None:
    """Record the dispatch anchor and the Step-5 stamp; first row wins.

    Both are deadline inputs for :func:`_deadline_ms`. "First wins"
    because the lifecycle FSM rejects re-dispatch of an ``action_id``, so
    a second ``action.dispatched`` row for one action would be a bug —
    and the earlier, stricter deadline is the safe one to keep either way.
    """
    if record.dispatched_ts_ms is None:
        record.dispatched_ts_ms = row.ts_epoch_ms
    raw_deadline = row.payload.get("result_expected_by_ms")
    if isinstance(raw_deadline, int) and record.result_expected_by_ms is None:
        record.result_expected_by_ms = raw_deadline


def _apply_lifecycle_row(
    pending: dict[str, _PendingAction],
    row: _LifecycleRow,
) -> None:
    """Fold one lifecycle row into the pending map.

    ``action.dispatched`` / ``action.running`` register the action and
    record their timestamps (the sweep's fallback deadline anchors);
    ``run.started`` supplies the ``run_id``; ``worker.heartbeat``
    refreshes liveness; a terminal row evicts the action outright.
    """
    raw_action_id = row.payload.get("action_id")
    action_id = None if raw_action_id is None else str(raw_action_id)
    if row.type == "run.started":
        started_action_id = _correlation_str(row, "action_id")
        if started_action_id is not None:
            _note_run_id(pending, started_action_id, _correlation_str(row, "run_id"))
        return
    if action_id is None:
        return
    if row.type == "action.dispatched":
        _note_dispatched(
            _note_run_id(pending, action_id, _correlation_str(row, "run_id")),
            row,
        )
    elif row.type == "action.running":
        record = _note_run_id(pending, action_id, _correlation_str(row, "run_id"))
        if record.running_ts_ms is None:
            record.running_ts_ms = row.ts_epoch_ms
    elif row.type == "worker.heartbeat":
        heartbeat_record = pending.get(action_id)
        if heartbeat_record is not None:
            heartbeat_record.last_heartbeat_ts = str(row.ts_epoch_ms)
    elif row.type in _TERMINAL_ACTION_EVENT_TYPES:
        pending.pop(action_id, None)


def _open_actions(event_log: sqlite3.Connection) -> list[_OpenAction]:
    """Shared low-level fold: every action the log shows as open.

    ADR-0009 D4 pins ONE fold for both consumers, parameterized by what
    each projects out of it rather than duplicated:

    - :func:`reconcile_after_wake` (via :func:`_in_progress_actions`)
      keeps only actions that reached ``run.started``, because it emits
      ``worker.terminated_by_sleep`` and there must be a run to name.
    - :func:`sweep_overdue_actions` keeps all of them — including the
      daemon-died-during-preflight window — and reads the deadline
      anchors, because an action nobody ever started is exactly the
      ghost the sweep exists to close.

    The returned list preserves first-seen order.
    """
    pending: dict[str, _PendingAction] = {}
    for row in _iter_lifecycle_rows(event_log):
        _apply_lifecycle_row(pending, row)
    return [
        _OpenAction(
            action_id=action_id,
            run_id=record.run_id,
            last_heartbeat_ts=record.last_heartbeat_ts,
            dispatched_ts_ms=record.dispatched_ts_ms,
            running_ts_ms=record.running_ts_ms,
            result_expected_by_ms=record.result_expected_by_ms,
        )
        for action_id, record in pending.items()
    ]


def _in_progress_actions(event_log: sqlite3.Connection) -> list[_InProgressAction]:
    """Return one record per action that has a started run but no terminal.

    The wake path's projection of :func:`_open_actions`. ``action.running``
    registers an action; ``run.started`` supplies its ``run_id`` (the
    first event that carries one for a real worker run — ``action.running``
    from the generic dispatcher has only ``{action_id, turn_id}``). A
    later terminal event for that ``action_id`` evicts the entry. Actions
    that never reached ``run.started`` carry a ``None`` run_id and are
    skipped (no worker was ever registered) — the deliberate blind spot
    the supervisor sweep, and only the supervisor sweep, covers. The
    returned list preserves first-seen order.
    """
    return [
        _InProgressAction(
            action_id=action.action_id,
            run_id=action.run_id,
            last_heartbeat_ts=action.last_heartbeat_ts,
        )
        for action in _open_actions(event_log)
        if action.run_id is not None
    ]


def _has_terminal_event(event_log: sqlite3.Connection, action_id: str) -> bool:
    """Return True if any terminal event already names ``action_id``.

    The sweep's pre-emit re-check (ADR-0009 F7). Deliberately a fresh
    query rather than a lookup into the fold's own result: between the
    fold and this action's emit, a detached child from the pre-install
    era or the in-turn driver may have closed it.
    """
    cursor = event_log.execute(
        _SELECT_TERMINAL_FOR_ACTION_SQL,
        (*_TERMINAL_ACTION_EVENT_TYPES_ORDERED, action_id),
    )
    return cursor.fetchone() is not None


def _deadline_ms(action: _OpenAction, *, budget_ms: int) -> int | None:
    """Resolve ``action``'s deadline via the ADR-0009 D4 ladder.

    1. The dispatcher-stamped ``result_expected_by_ms``.
    2. Legacy rows lacking it: that action's ``action.dispatched`` ts
       plus the default budget.
    3. Pre-dispatch-era rows: first-seen ``action.running`` ts plus the
       same budget.

    ``None`` when no anchor exists at all (an action known only from a
    ``run.started`` row) — the sweep then leaves it alone rather than
    inventing an age for it.
    """
    if action.result_expected_by_ms is not None:
        return action.result_expected_by_ms
    if action.dispatched_ts_ms is not None:
        return action.dispatched_ts_ms + budget_ms
    if action.running_ts_ms is not None:
        return action.running_ts_ms + budget_ms
    return None


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
    "sweep_overdue_actions",
]
