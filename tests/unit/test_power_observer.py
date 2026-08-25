"""Unit tests for the real ctypes-IOKit power observer (ADR-0009 Step 3 / D3).

Hermetic by construction. Every test drives
:class:`jarvis.deployment.sleep_wake._IOKitPowerObserver` through
:class:`_FakeIOKit` — a recording stand-in for the IOKit /
CoreFoundation entry points that :class:`_PowerBinding` carries. No
framework is dlopened, nothing registers with the kernel, and the
machine never sleeps. The real path was already proven end-to-end by the
Step-0 spike (``scripts/spike_power_observer.py``) against a live
``pmset sleepnow``; these tests pin the *contract* D3 specifies around
it:

- the ``CFUNCTYPE`` wrapper handed to IOKit is held on the instance for
  the observer's lifetime (GC while registered = crash)
- ``kIOMessageSystemWillSleep`` acks with ``IOAllowPowerChange``
  **unconditionally** — even when the ``before_sleep`` callback raises
- ``kIOMessageCanSystemSleep`` is allowed immediately, without invoking
  any user callback
- ``kIOMessageSystemHasPoweredOn`` invokes ``on_wake`` guarded, with no
  ack
- the closed flag is consulted at callback entry: a notification landing
  after ``shutdown()`` invokes nothing and acks nothing
- the teardown order D3 pins (deregister -> service close -> runloop stop
  -> join -> port destroy) and its idempotence

The stub-factory seam tests for :func:`install_power_observer` live in
``tests/unit/test_sleep_wake.py`` and are untouched by this step.
"""

from __future__ import annotations

import sys
import threading
from typing import TYPE_CHECKING

import pytest

from jarvis.deployment.sleep_wake import (
    _CALLBACK_TYPE,
    _MSG_CAN_SYSTEM_SLEEP,
    _MSG_HAS_POWERED_ON,
    _MSG_WILL_SLEEP,
    _IOKitPowerObserver,
    _PowerBinding,
    _real_observer_factory,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

# Notification id the kernel passes as ``messageArgument``; the ack must
# hand exactly this value back to ``IOAllowPowerChange``.
_NOTIFICATION_ID = 777

# Non-zero io_connect_t the fake registration hands back. Zero means
# "IORegisterForSystemPower failed" and is exercised separately.
_FAKE_ROOT_PORT = 4242

_RUNLOOP_PARK_TIMEOUT_S = 5.0


# --- Fake IOKit / CoreFoundation binding -----------------------------------


class _FakeIOKit:
    """Recording stand-in for the IOKit / CoreFoundation entry points.

    Every call appends its name to :attr:`calls`, so a test can assert
    the teardown ORDER that ADR-0009 D3 pins without touching a real
    framework. :meth:`_runloop_run` parks the observer's runloop thread
    on an event exactly as ``CFRunLoopRun`` parks the real one, and
    :meth:`_runloop_stop` releases it exactly as ``CFRunLoopStop`` does.
    """

    _RUNLOOP_SOURCE = 0x1111
    _RUNLOOP_REF = 0x2222

    def __init__(self) -> None:
        """Initialize an unregistered fake with an empty call log."""
        self.calls: list[str] = []
        self.ack_args: list[int] = []
        self.ack_ports: list[int] = []
        self.registered_callback: object | None = None
        self.register_result: int = _FAKE_ROOT_PORT
        self.runloop_entered = threading.Event()
        self.runloop_stopped = threading.Event()

    def binding(self) -> _PowerBinding:
        """Bundle the fake entry points into the struct the observer takes."""
        return _PowerBinding(
            register_for_system_power=self._register_for_system_power,
            notification_port_get_runloop_source=self._get_runloop_source,
            allow_power_change=self._allow_power_change,
            deregister_for_system_power=self._deregister_for_system_power,
            service_close=self._service_close,
            notification_port_destroy=self._notification_port_destroy,
            runloop_get_current=self._runloop_get_current,
            runloop_add_source=self._runloop_add_source,
            runloop_run=self._runloop_run,
            runloop_stop=self._runloop_stop,
            runloop_default_mode=object(),
        )

    # -- IOKit ------------------------------------------------------------

    def _register_for_system_power(
        self,
        _refcon: object,
        _port_ref: object,
        callback: object,
        _notifier_ref: object,
    ) -> int:
        """IORegisterForSystemPower — record the callback, return a root port."""
        self.calls.append("register_for_system_power")
        self.registered_callback = callback
        return self.register_result

    def _get_runloop_source(self, _notify_port: object) -> int:
        """IONotificationPortGetRunLoopSource."""
        self.calls.append("notification_port_get_runloop_source")
        return self._RUNLOOP_SOURCE

    def _allow_power_change(self, root_port: int, message_argument: int) -> int:
        """IOAllowPowerChange — the never-veto ack."""
        self.calls.append("allow_power_change")
        self.ack_ports.append(root_port)
        self.ack_args.append(message_argument)
        return 0

    def _deregister_for_system_power(self, _notifier_ref: object) -> int:
        """IODeregisterForSystemPower."""
        self.calls.append("deregister_for_system_power")
        return 0

    def _service_close(self, _root_port: int) -> int:
        """IOServiceClose."""
        self.calls.append("service_close")
        return 0

    def _notification_port_destroy(self, _notify_port: object) -> None:
        """IONotificationPortDestroy."""
        self.calls.append("notification_port_destroy")

    # -- CoreFoundation ---------------------------------------------------

    def _runloop_get_current(self) -> int:
        """CFRunLoopGetCurrent — called on the observer's own thread."""
        self.calls.append("runloop_get_current")
        return self._RUNLOOP_REF

    def _runloop_add_source(
        self,
        _runloop: object,
        _source: object,
        _mode: object,
    ) -> None:
        """CFRunLoopAddSource."""
        self.calls.append("runloop_add_source")

    def _runloop_run(self) -> None:
        """CFRunLoopRun — parks the thread until :meth:`_runloop_stop`."""
        self.calls.append("runloop_run")
        self.runloop_entered.set()
        self.runloop_stopped.wait(timeout=_RUNLOOP_PARK_TIMEOUT_S)

    def _runloop_stop(self, _runloop: object) -> None:
        """CFRunLoopStop — releases the parked runloop thread."""
        self.calls.append("runloop_stop")
        self.runloop_stopped.set()


# --- Fixtures + helpers ----------------------------------------------------


@pytest.fixture
def fake_iokit() -> Iterator[_FakeIOKit]:
    """Yield a fresh fake binding; release any parked runloop thread after."""
    fake = _FakeIOKit()
    yield fake
    # Safety net: a test that skips shutdown() would otherwise leave the
    # daemon thread parked for the full park timeout.
    fake.runloop_stopped.set()


class _Recorder:
    """Callable that records invocations and optionally raises."""

    def __init__(self, *, raises: bool = False) -> None:
        """Create a recorder; ``raises=True`` makes every call blow up."""
        self.calls = 0
        self._raises = raises

    def __call__(self) -> None:
        """Record one invocation, then raise if configured to."""
        self.calls += 1
        if self._raises:
            msg = "callback exploded on purpose"
            raise RuntimeError(msg)


def _registered(
    fake: _FakeIOKit,
    *,
    before_sleep: _Recorder | None = None,
    on_wake: _Recorder | None = None,
) -> tuple[_IOKitPowerObserver, _Recorder, _Recorder]:
    """Build + register an observer over ``fake``; wait for its runloop thread."""
    sleep_cb = before_sleep if before_sleep is not None else _Recorder()
    wake_cb = on_wake if on_wake is not None else _Recorder()
    observer = _IOKitPowerObserver(binding=fake.binding())
    observer.register(before_sleep=sleep_cb, on_wake=wake_cb)
    assert fake.runloop_entered.wait(timeout=_RUNLOOP_PARK_TIMEOUT_S), (
        "runloop thread never entered CFRunLoopRun"
    )
    return observer, sleep_cb, wake_cb


# --- Callback lifetime -----------------------------------------------------


def test_register_holds_the_callback_wrapper_for_the_observer_lifetime(
    fake_iokit: _FakeIOKit,
) -> None:
    """The CFUNCTYPE wrapper handed to IOKit stays referenced on the instance.

    ctypes does NOT own the trampoline it builds: if the only reference
    dies while IOKit still holds the pointer, the next notification
    jumps into freed memory and takes the runloop thread with it (D3).
    """
    observer, _, _ = _registered(fake_iokit)
    held = observer._callback_ref  # noqa: SLF001 — holding the ref IS the contract.
    assert isinstance(held, _CALLBACK_TYPE)
    assert fake_iokit.registered_callback is held, (
        "the object passed to IORegisterForSystemPower must be the very "
        "object the observer keeps alive"
    )
    observer.shutdown()


def test_register_spins_the_runloop_thread_and_adds_the_source(
    fake_iokit: _FakeIOKit,
) -> None:
    """register() wires the notification port onto a dedicated CFRunLoop thread."""
    observer, _, _ = _registered(fake_iokit)
    assert fake_iokit.calls[0] == "register_for_system_power"
    assert "notification_port_get_runloop_source" in fake_iokit.calls
    assert "runloop_add_source" in fake_iokit.calls
    assert "runloop_run" in fake_iokit.calls
    observer.shutdown()


def test_register_raises_when_iokit_registration_fails(
    fake_iokit: _FakeIOKit,
) -> None:
    """A zero root port means IORegisterForSystemPower failed — fail loudly."""
    fake_iokit.register_result = 0
    observer = _IOKitPowerObserver(binding=fake_iokit.binding())
    with pytest.raises(RuntimeError, match="IORegisterForSystemPower"):
        observer.register(before_sleep=_Recorder(), on_wake=_Recorder())


# --- Before-sleep: guarded invoke + unconditional ack ----------------------


def test_will_sleep_invokes_before_sleep_then_acks(fake_iokit: _FakeIOKit) -> None:
    """kIOMessageSystemWillSleep: run before_sleep, then IOAllowPowerChange."""
    observer, sleep_cb, wake_cb = _registered(fake_iokit)
    fake_iokit.calls.clear()
    observer._on_power_message(None, 0, _MSG_WILL_SLEEP, _NOTIFICATION_ID)  # noqa: SLF001
    assert sleep_cb.calls == 1
    assert wake_cb.calls == 0
    assert fake_iokit.calls == ["allow_power_change"]
    assert fake_iokit.ack_args == [_NOTIFICATION_ID]
    assert fake_iokit.ack_ports == [_FAKE_ROOT_PORT]
    observer.shutdown()


def test_will_sleep_acks_even_when_before_sleep_raises(
    fake_iokit: _FakeIOKit,
) -> None:
    """A raising before_sleep must never cost the ack — never veto a sleep.

    The emit path runs against a ``check_same_thread=True`` sqlite
    connection from this (CFRunLoop) thread, so a directly-wired emit
    WILL raise here. The ack must survive that.
    """
    boom = _Recorder(raises=True)
    observer, sleep_cb, _ = _registered(fake_iokit, before_sleep=boom)
    fake_iokit.calls.clear()
    observer._on_power_message(None, 0, _MSG_WILL_SLEEP, _NOTIFICATION_ID)  # noqa: SLF001
    assert sleep_cb.calls == 1
    assert fake_iokit.calls == ["allow_power_change"]
    assert fake_iokit.ack_args == [_NOTIFICATION_ID]
    observer.shutdown()


def test_can_system_sleep_acks_without_invoking_callbacks(
    fake_iokit: _FakeIOKit,
) -> None:
    """Can-system-sleep is allowed immediately; no user callback runs.

    Forced sleeps skip this message entirely (spike-proven), so nothing
    may depend on it — it is an ack and nothing else.
    """
    observer, sleep_cb, wake_cb = _registered(fake_iokit)
    fake_iokit.calls.clear()
    observer._on_power_message(None, 0, _MSG_CAN_SYSTEM_SLEEP, _NOTIFICATION_ID)  # noqa: SLF001
    assert sleep_cb.calls == 0
    assert wake_cb.calls == 0
    assert fake_iokit.calls == ["allow_power_change"]
    observer.shutdown()


# --- Wake side -------------------------------------------------------------


def test_has_powered_on_invokes_on_wake_without_ack(
    fake_iokit: _FakeIOKit,
) -> None:
    """Has-powered-on runs on_wake; that message takes no ack."""
    observer, sleep_cb, wake_cb = _registered(fake_iokit)
    fake_iokit.calls.clear()
    observer._on_power_message(None, 0, _MSG_HAS_POWERED_ON, 0)  # noqa: SLF001
    assert wake_cb.calls == 1
    assert sleep_cb.calls == 0
    assert fake_iokit.calls == []
    observer.shutdown()


def test_on_wake_exception_is_swallowed(fake_iokit: _FakeIOKit) -> None:
    """A raising on_wake must not escape into the CFRunLoop thread."""
    boom = _Recorder(raises=True)
    observer, _, wake_cb = _registered(fake_iokit, on_wake=boom)
    observer._on_power_message(None, 0, _MSG_HAS_POWERED_ON, 0)  # noqa: SLF001
    assert wake_cb.calls == 1
    observer.shutdown()


def test_unknown_message_is_ignored(fake_iokit: _FakeIOKit) -> None:
    """Messages outside the three D3 cares about invoke nothing and ack nothing."""
    observer, sleep_cb, wake_cb = _registered(fake_iokit)
    fake_iokit.calls.clear()
    observer._on_power_message(None, 0, 0xE0000290, 0)  # noqa: SLF001 — will_not_sleep
    assert sleep_cb.calls == 0
    assert wake_cb.calls == 0
    assert fake_iokit.calls == []
    observer.shutdown()


# --- Closed flag -----------------------------------------------------------


def test_notification_after_shutdown_invokes_nothing_and_acks_nothing(
    fake_iokit: _FakeIOKit,
) -> None:
    """The closed flag is consulted at callback ENTRY (D3 teardown).

    A notification can land between ``IODeregisterForSystemPower`` and
    process exit. By then the root port is closed and the asyncio loop
    may be gone, so both the emit and the ack would be use-after-close:
    the callback must find the flag and return.
    """
    observer, sleep_cb, wake_cb = _registered(fake_iokit)
    observer.shutdown()
    fake_iokit.calls.clear()

    observer._on_power_message(None, 0, _MSG_WILL_SLEEP, _NOTIFICATION_ID)  # noqa: SLF001
    observer._on_power_message(None, 0, _MSG_HAS_POWERED_ON, 0)  # noqa: SLF001

    assert sleep_cb.calls == 0
    assert wake_cb.calls == 0
    assert fake_iokit.calls == [], "no IOKit call may follow shutdown()"


# --- Teardown --------------------------------------------------------------


def test_shutdown_follows_the_pinned_teardown_order(fake_iokit: _FakeIOKit) -> None:
    """D3 order: deregister -> service close -> runloop stop -> join -> destroy.

    ``IONotificationPortDestroy`` runs LAST, after the runloop thread has
    been joined — destroying the port while the thread still owns its
    runloop source is what makes the spike's teardown crash instead of
    exiting cleanly.
    """
    observer, _, _ = _registered(fake_iokit)
    fake_iokit.calls.clear()
    observer.shutdown()
    assert fake_iokit.calls == [
        "deregister_for_system_power",
        "service_close",
        "runloop_stop",
        "notification_port_destroy",
    ]
    assert observer._thread is None  # noqa: SLF001 — joined thread must be released.


def test_shutdown_is_idempotent(fake_iokit: _FakeIOKit) -> None:
    """A second shutdown() is a no-op — no double deregister / double destroy."""
    observer, _, _ = _registered(fake_iokit)
    observer.shutdown()
    fake_iokit.calls.clear()
    observer.shutdown()
    assert fake_iokit.calls == []


def test_shutdown_without_register_touches_no_iokit_call(
    fake_iokit: _FakeIOKit,
) -> None:
    """Tearing down a never-registered observer unwinds nothing."""
    observer = _IOKitPowerObserver(binding=fake_iokit.binding())
    observer.shutdown()
    assert fake_iokit.calls == []


# --- Factory ---------------------------------------------------------------


def test_real_observer_factory_rejects_non_darwin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off macOS the factory refuses; tests must inject a stub factory."""
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="requires macOS"):
        _real_observer_factory()


@pytest.mark.skipif(sys.platform != "darwin", reason="IOKit is macOS-only")
def test_real_observer_factory_returns_the_iokit_observer() -> None:
    """On macOS the factory returns the real ctypes observer, unregistered.

    This constructs the observer (which dlopens IOKit + CoreFoundation
    and resolves every symbol — a genuine smoke of the ctypes recipe)
    but never calls ``register()``, so nothing subscribes to the kernel
    and the machine is never asked to sleep.
    """
    observer = _real_observer_factory()
    assert isinstance(observer, _IOKitPowerObserver)
    assert observer._thread is None  # noqa: SLF001 — construction must not spin a thread.
