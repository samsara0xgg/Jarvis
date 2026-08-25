"""Unit tests for :mod:`jarvis.deployment.sleep_wake` (Step 16 of ADR-0002).

Drives sleep/wake transitions through a stub :class:`PowerObserver` whose
``simulate_sleep`` / ``simulate_wake`` methods fire the IOPM callbacks
deterministically — no real Cocoa / IOKit binding is exercised. The same
stub injection point is reused by K7 / K8 Tier-2 invariants against live
Codex.

Covers per ADR-0002 § Sleep/wake protocol (lines 1100-1156) +
spec §3.7.8:

- register / shutdown round-trip on the injected observer
- ``simulate_sleep`` emits ``mac.sleeping`` (and per-action
  ``worker.suspended_by_sleep`` when an ``action.running`` event is
  in-flight)
- ``simulate_wake`` emits ``mac.awake`` followed by
  :func:`reconcile_after_wake`
- ``reconcile_after_wake`` is idempotent (second invocation is a no-op)
- K7 full cycle: induced sleep + wake -> suspended_by_sleep +
  terminated_by_sleep + action.timeout_assumed
- Input validation: a None event_log raises ValueError
- Fail-closed sees a None ``run_id`` in correlation as a string-None;
  the action is still closed
- Actions that already have a terminal event are NOT reconciled

ADR-0009 Step 4 additions (the ``loop=`` marshaling seam):

- ``loop=None`` (the default) keeps the callbacks inline on the firing
  thread — the byte-for-byte K7/K8 contract
- ``loop=<running loop>`` marshals the emit onto the loop thread, so a
  CFRunLoop-thread notification can write a ``check_same_thread=True``
  connection
- the marshaled wrapper returns within a 3s hard bound even when the
  loop never runs the callback (it must never delay ``IOAllowPowerChange``)
- a ``register()`` that raises after a partial registration is unwound
  by ``install_power_observer`` itself (the caller never gets a handle)
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import TYPE_CHECKING

import pytest

from jarvis.deployment import sleep_wake
from jarvis.deployment.sleep_wake import (
    PowerObserver,
    install_power_observer,
    reconcile_after_wake,
)
from jarvis.state.event_log import emit_event, iter_events, open_event_log

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from pathlib import Path


# --- Stub observer ---------------------------------------------------------


class StubPowerObserver:
    """Manual-trigger stub satisfying :class:`PowerObserver`.

    Tests call :meth:`simulate_sleep` / :meth:`simulate_wake` to fire the
    callbacks that :func:`install_power_observer` registered, exactly as
    the kernel would fire them.
    """

    def __init__(self) -> None:
        """Initialize unwired stub (callbacks set by :meth:`register`)."""
        self._before_sleep: Callable[[], None] | None = None
        self._on_wake: Callable[[], None] | None = None
        self.shutdown_called: bool = False

    def register(
        self,
        *,
        before_sleep: Callable[[], None],
        on_wake: Callable[[], None],
    ) -> None:
        """Record the callbacks; tests fire them via simulate_*."""
        self._before_sleep = before_sleep
        self._on_wake = on_wake

    def shutdown(self) -> None:
        """Mark shutdown so the test can assert clean teardown."""
        self.shutdown_called = True

    def simulate_sleep(self) -> None:
        """Fire the before-sleep callback exactly as the kernel would."""
        assert self._before_sleep is not None, "register() must precede simulate_sleep()"
        self._before_sleep()

    def simulate_wake(self) -> None:
        """Fire the on-wake callback exactly as the kernel would."""
        assert self._on_wake is not None, "register() must precede simulate_wake()"
        self._on_wake()


# --- Fixtures + helpers ----------------------------------------------------


def _make_event_log(tmp_path: Path) -> sqlite3.Connection:
    """Open a fresh Event Log under ``tmp_path``."""
    return open_event_log(tmp_path / "mac_events.db")


def _seed_action_running(
    conn: sqlite3.Connection,
    *,
    action_id: str,
    run_id: str,
) -> None:
    """Seed one ``action.running`` event with the (run_id, action_id) pair.

    The fail-closed reconciler only needs ``action.running`` + a
    ``run_id`` correlation to identify an in-flight action; we skip the
    full causal-chain seeding (turn.started / action.proposed / ...) to
    keep tests focused on the sleep/wake fold.
    """
    emit_event(
        conn,
        type="action.running",
        payload={"action_id": action_id},
        correlation={"run_id": run_id, "action_id": action_id},
    )


def _seed_real_dispatcher_run(
    conn: sqlite3.Connection,
    *,
    action_id: str,
    run_id: str,
    task_id: str = "task_X",
    turn_id: str = "T_demo",
) -> None:
    """Seed the REAL L4-dispatcher event shape for an in-flight worker run.

    Unlike :func:`_seed_action_running`, this mirrors production exactly.
    The generic dispatcher emits ``action.running`` with correlation
    ``{action_id, turn_id}`` and NO ``run_id`` (``_action_correlation`` in
    :mod:`jarvis.execution.tools` only adds a run_id when the request
    already carries one, which spawn_worker never does — the run_id is
    minted several steps later inside the codex handler). The codex
    handler then emits ``run.started`` carrying ``run_id`` in both payload
    and correlation. The sleep/wake fold must therefore derive the run_id
    from ``run.started``; ``action.running`` alone never has it for a real
    worker run.
    """
    emit_event(
        conn,
        type="action.running",
        payload={"action_id": action_id},
        correlation={"action_id": action_id, "turn_id": turn_id},
    )
    emit_event(
        conn,
        type="run.started",
        payload={"run_id": run_id, "task_id": task_id, "runner": "codex"},
        correlation={
            "action_id": action_id,
            "run_id": run_id,
            "task_id": task_id,
            "turn_id": turn_id,
        },
    )


def _event_types(conn: sqlite3.Connection) -> list[str]:
    return [evt.type for evt in iter_events(conn)]


# --- register / shutdown round-trip ---------------------------------------


def test_install_returns_registered_observer(tmp_path: Path) -> None:
    """install_power_observer wires the callbacks onto the injected observer."""
    stub = StubPowerObserver()
    conn = _make_event_log(tmp_path)
    observer = install_power_observer(conn, observer_factory=lambda: stub)
    assert observer is stub
    assert stub._before_sleep is not None  # noqa: SLF001 — asserting wiring is the test point.
    assert stub._on_wake is not None  # noqa: SLF001 — asserting wiring is the test point.


def test_install_rejects_none_event_log() -> None:
    """install_power_observer fails fast on None event_log."""
    with pytest.raises(ValueError, match="non-None event_log"):
        install_power_observer(None, observer_factory=StubPowerObserver)


def test_observer_factory_default_uses_protocol_only(tmp_path: Path) -> None:
    """Custom factories satisfying :class:`PowerObserver` are accepted."""
    stub = StubPowerObserver()
    conn = _make_event_log(tmp_path)
    observer: PowerObserver = install_power_observer(
        conn, observer_factory=lambda: stub,
    )
    observer.shutdown()
    assert stub.shutdown_called is True


# --- simulate_sleep --------------------------------------------------------


def test_simulated_sleep_emits_mac_sleeping_when_no_action_running(
    tmp_path: Path,
) -> None:
    """Sleep with no in-flight action emits mac.sleeping only."""
    stub = StubPowerObserver()
    conn = _make_event_log(tmp_path)
    install_power_observer(conn, observer_factory=lambda: stub)
    stub.simulate_sleep()
    types = _event_types(conn)
    assert "mac.sleeping" in types
    assert "worker.suspended_by_sleep" not in types


def test_simulated_sleep_emits_worker_suspended_per_in_progress_action(
    tmp_path: Path,
) -> None:
    """Sleep with an in-flight action emits mac.sleeping + worker.suspended_by_sleep."""
    conn = _make_event_log(tmp_path)
    _seed_action_running(conn, action_id="A1", run_id="R1")
    stub = StubPowerObserver()
    install_power_observer(conn, observer_factory=lambda: stub)
    stub.simulate_sleep()
    types = _event_types(conn)
    assert types.count("mac.sleeping") == 1
    assert types.count("worker.suspended_by_sleep") == 1


def test_simulated_sleep_omits_suspended_for_actions_already_terminated(
    tmp_path: Path,
) -> None:
    """Actions with a terminal event do NOT receive worker.suspended_by_sleep."""
    conn = _make_event_log(tmp_path)
    _seed_action_running(conn, action_id="A_DONE", run_id="R_DONE")
    # Close the action.
    emit_event(
        conn,
        type="action.failed",
        payload={"action_id": "A_DONE", "error": "intentional"},
        correlation={"action_id": "A_DONE"},
    )
    # A second action is still in-flight.
    _seed_action_running(conn, action_id="A_LIVE", run_id="R_LIVE")
    stub = StubPowerObserver()
    install_power_observer(conn, observer_factory=lambda: stub)
    stub.simulate_sleep()
    suspend_events = [
        evt for evt in iter_events(conn) if evt.type == "worker.suspended_by_sleep"
    ]
    assert len(suspend_events) == 1
    assert suspend_events[0].payload["action_id"] == "A_LIVE"


def test_simulated_sleep_payload_carries_in_progress_action_ids(
    tmp_path: Path,
) -> None:
    """mac.sleeping.payload.in_progress_action_ids lists in-flight actions."""
    conn = _make_event_log(tmp_path)
    _seed_action_running(conn, action_id="A1", run_id="R1")
    _seed_action_running(conn, action_id="A2", run_id="R2")
    stub = StubPowerObserver()
    install_power_observer(conn, observer_factory=lambda: stub)
    stub.simulate_sleep()
    [mac_sleeping] = [evt for evt in iter_events(conn) if evt.type == "mac.sleeping"]
    assert set(mac_sleeping.payload["in_progress_action_ids"]) == {"A1", "A2"}


def test_simulated_sleep_suspends_real_dispatcher_run_keyed_on_run_started(
    tmp_path: Path,
) -> None:
    """Real-shaped run (action.running w/o run_id + run.started) is suspended.

    Regression for the K7/K8 live-burn failure: the generic L4 dispatcher
    emits ``action.running`` WITHOUT a run_id (the run_id is minted later
    inside the codex handler and first appears on ``run.started``). A fold
    that reads run_id only from ``action.running`` silently skips every
    real worker. The fold must pick the run_id up from ``run.started``.
    """
    conn = _make_event_log(tmp_path)
    _seed_real_dispatcher_run(conn, action_id="A_real", run_id="R_real")
    stub = StubPowerObserver()
    install_power_observer(conn, observer_factory=lambda: stub)
    stub.simulate_sleep()
    suspended = [
        evt for evt in iter_events(conn) if evt.type == "worker.suspended_by_sleep"
    ]
    assert len(suspended) == 1
    assert suspended[0].payload["action_id"] == "A_real"
    assert suspended[0].payload["run_id"] == "R_real"


# --- simulate_wake + reconcile_after_wake ---------------------------------


def test_simulated_wake_emits_mac_awake_and_reconciles(tmp_path: Path) -> None:
    """Wake emits mac.awake + worker.terminated_by_sleep + action.timeout_assumed."""
    conn = _make_event_log(tmp_path)
    _seed_action_running(conn, action_id="A2", run_id="R2")
    stub = StubPowerObserver()
    install_power_observer(conn, observer_factory=lambda: stub)
    stub.simulate_wake()
    types = _event_types(conn)
    assert "mac.awake" in types
    assert "worker.terminated_by_sleep" in types
    assert "action.timeout_assumed" in types


def test_reconcile_after_wake_is_idempotent(tmp_path: Path) -> None:
    """Calling reconcile twice does not double-close any action."""
    conn = _make_event_log(tmp_path)
    _seed_action_running(conn, action_id="A3", run_id="R3")
    first = reconcile_after_wake(conn)
    second = reconcile_after_wake(conn)
    assert first == 1
    assert second == 0
    types = _event_types(conn)
    assert types.count("worker.terminated_by_sleep") == 1
    assert types.count("action.timeout_assumed") == 1


def test_reconcile_after_wake_skips_already_terminated_actions(
    tmp_path: Path,
) -> None:
    """Actions with action.result_observed are left alone."""
    conn = _make_event_log(tmp_path)
    _seed_action_running(conn, action_id="A_OK", run_id="R_OK")
    emit_event(
        conn,
        type="action.result_observed",
        payload={"action_id": "A_OK", "semantics": "observation"},
        correlation={"action_id": "A_OK"},
    )
    closed = reconcile_after_wake(conn)
    assert closed == 0
    types = _event_types(conn)
    assert "worker.terminated_by_sleep" not in types
    assert "action.timeout_assumed" not in types


def test_reconcile_after_wake_rejects_none_event_log() -> None:
    """reconcile_after_wake fails fast on None event_log."""
    with pytest.raises(ValueError, match="non-None event_log"):
        reconcile_after_wake(None)


def test_reconcile_closes_real_dispatcher_run_keyed_on_run_started(
    tmp_path: Path,
) -> None:
    """reconcile_after_wake closes a real-shaped orphan run via run.started run_id.

    The fail-closed wake path must terminate a worker run whose
    ``action.running`` carries no run_id (the real dispatcher shape),
    deriving the run_id from the ``run.started`` event instead.
    """
    conn = _make_event_log(tmp_path)
    _seed_real_dispatcher_run(conn, action_id="A_real", run_id="R_real")
    closed = reconcile_after_wake(conn)
    assert closed == 1
    terminated = [
        evt for evt in iter_events(conn) if evt.type == "worker.terminated_by_sleep"
    ]
    assert len(terminated) == 1
    assert terminated[0].payload["run_id"] == "R_real"


# --- K7 full cycle --------------------------------------------------------


def test_sleep_wake_full_cycle_k7(tmp_path: Path) -> None:
    """K7: induced sleep + wake -> suspended_by_sleep + terminated_by_sleep + timeout."""
    conn = _make_event_log(tmp_path)
    _seed_action_running(conn, action_id="A4", run_id="R4")
    stub = StubPowerObserver()
    install_power_observer(conn, observer_factory=lambda: stub)
    stub.simulate_sleep()
    stub.simulate_wake()
    types = _event_types(conn)
    assert types.count("mac.sleeping") == 1
    assert types.count("worker.suspended_by_sleep") == 1
    assert types.count("mac.awake") == 1
    assert types.count("worker.terminated_by_sleep") == 1
    assert types.count("action.timeout_assumed") == 1


# --- ADR-0009 Step 4: the loop= marshaling seam ----------------------------


class _RaisingRegisterObserver(StubPowerObserver):
    """Stub whose ``register`` fails the way a partial IOKit setup does.

    ``_IOKitPowerObserver.register`` can raise *after*
    ``IORegisterForSystemPower`` already handed back a live root port
    (root port 0 check, or the runloop thread never coming up). The
    caller never receives the observer on that path, so
    :func:`install_power_observer` itself must unwind it.
    """

    def __init__(self, *, shutdown_raises: bool = False) -> None:
        """Record whether the unwinding ``shutdown()`` should also fail."""
        super().__init__()
        self._shutdown_raises = shutdown_raises

    def register(
        self,
        *,
        before_sleep: Callable[[], None],
        on_wake: Callable[[], None],
    ) -> None:
        """Fail exactly as a half-registered IOKit observer does."""
        super().register(before_sleep=before_sleep, on_wake=on_wake)
        msg = "IORegisterForSystemPower failed (root port 0)"
        raise RuntimeError(msg)

    def shutdown(self) -> None:
        """Mark the unwind; optionally fail so suppression is exercised."""
        super().shutdown()
        if self._shutdown_raises:
            msg = "IODeregisterForSystemPower blew up during unwind"
            raise RuntimeError(msg)


def _capturing(fire: Callable[[], None], raised: list[BaseException]) -> Callable[[], None]:
    """Wrap ``fire`` so whatever it raises lands in ``raised`` instead."""

    def _target() -> None:
        try:
            fire()
        except BaseException as exc:  # noqa: BLE001 — the test asserts on it
            raised.append(exc)

    return _target


def _fire_from_worker_thread(fire: Callable[[], None]) -> list[BaseException]:
    """Run ``fire`` on a bare thread (no loop running); return what it raised.

    Stands in for the CFRunLoop thread: the real observer invokes the
    installed callbacks there, never on the asyncio loop thread.
    """
    raised: list[BaseException] = []
    thread = threading.Thread(target=_capturing(fire, raised), name="fake-cfrunloop")
    thread.start()
    thread.join(timeout=10.0)
    assert not thread.is_alive(), "callback never returned — the ack bound leaked"
    return raised


async def _fire_off_loop(fire: Callable[[], None]) -> list[BaseException]:
    """Run ``fire`` on a worker thread while the loop stays free to turn.

    ``asyncio.to_thread`` is what makes this a real marshaling test: the
    callback runs off-loop (as the CFRunLoop thread does) and the awaiting
    loop is simultaneously free to run the ``call_soon_threadsafe`` emit
    the callback is blocking on.
    """
    raised: list[BaseException] = []
    await asyncio.to_thread(_capturing(fire, raised))
    return raised


def test_marshaled_before_sleep_emits_on_the_loop_thread(tmp_path: Path) -> None:
    """A CFRunLoop-thread before-sleep lands its emit on the loop thread.

    The Event Log connection is opened with ``check_same_thread=True`` on
    the loop thread, so an un-marshaled emit from another thread raises
    ``sqlite3.ProgrammingError``. The row existing — with the callback
    having been fired from a different thread — is the proof that
    ``loop=`` moved the write onto the loop.
    """

    async def _body() -> None:
        conn = _make_event_log(tmp_path)
        try:
            stub = StubPowerObserver()
            install_power_observer(
                conn,
                observer_factory=lambda: stub,
                loop=asyncio.get_running_loop(),
            )
            raised = await _fire_off_loop(stub.simulate_sleep)

            assert raised == []
            assert "mac.sleeping" in _event_types(conn)
        finally:
            conn.close()

    asyncio.run(_body())


def test_marshaled_on_wake_emits_on_the_loop_thread(tmp_path: Path) -> None:
    """The on-wake callback is marshaled the same way (ADR-0009 D3)."""

    async def _body() -> None:
        conn = _make_event_log(tmp_path)
        try:
            _seed_action_running(conn, action_id="A_marshal", run_id="R_marshal")
            stub = StubPowerObserver()
            install_power_observer(
                conn,
                observer_factory=lambda: stub,
                loop=asyncio.get_running_loop(),
            )
            raised = await _fire_off_loop(stub.simulate_wake)

            assert raised == []
            types = _event_types(conn)
            # mac.awake AND the reconciliation it drives both ran on the loop.
            assert "mac.awake" in types
            assert "worker.terminated_by_sleep" in types
        finally:
            conn.close()

    asyncio.run(_body())


def test_install_without_loop_emits_inline_on_the_firing_thread(
    tmp_path: Path,
) -> None:
    """``loop=None`` keeps the Day-1 inline behavior — the K7/K8 pin.

    No hop, no wait: the ``mac.sleeping`` row is visible the instant
    ``simulate_sleep()`` returns, without the loop ever being given a
    chance to run a scheduled callback. (A marshaled callback fired from
    the loop thread would instead block until the ack bound and emit
    nothing.)
    """

    async def _body() -> None:
        conn = _make_event_log(tmp_path)
        try:
            stub = StubPowerObserver()
            install_power_observer(conn, observer_factory=lambda: stub)
            stub.simulate_sleep()
            assert "mac.sleeping" in _event_types(conn)
        finally:
            conn.close()

    asyncio.run(_body())


def test_marshal_ack_bound_is_three_seconds() -> None:
    """ADR-0009 D3 pins the before-sleep ack wait at a 3s hard bound."""
    assert sleep_wake._MARSHAL_ACK_BOUND_S == 3.0  # noqa: SLF001 — the constant IS the contract.


def test_marshaled_callback_returns_within_bound_when_loop_never_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A busy/stopped loop must not stall the kernel's ack window.

    The wrapper waits at most ``_MARSHAL_ACK_BOUND_S``, logs one
    warning, and returns normally — never raises. Correctness then falls
    to the wake-side reconciliation (ADR-0009 F3).
    """
    monkeypatch.setattr(sleep_wake, "_MARSHAL_ACK_BOUND_S", 0.05)
    conn = _make_event_log(tmp_path)
    idle_loop = asyncio.new_event_loop()
    try:
        stub = StubPowerObserver()
        install_power_observer(
            conn,
            observer_factory=lambda: stub,
            loop=idle_loop,
        )
        with caplog.at_level(logging.WARNING, logger="jarvis.deployment.sleep_wake"):
            started = time.monotonic()
            raised = _fire_from_worker_thread(stub.simulate_sleep)
            elapsed = time.monotonic() - started

        assert raised == []
        assert elapsed < 1.0, f"ack wait overran the bound: {elapsed:.3f}s"
        warnings = [rec.getMessage() for rec in caplog.records]
        assert any("before_sleep" in msg for msg in warnings), (
            f"expected one before_sleep timeout warning, got {warnings}"
        )
        # The loop never ran, so nothing was written.
        assert _event_types(conn) == []
    finally:
        idle_loop.close()
        conn.close()


def test_install_unwinds_the_observer_when_register_raises(tmp_path: Path) -> None:
    """A half-registered observer is shut down by install, then re-raised.

    ``install_power_observer`` is the only holder of the reference at
    that point — the caller structurally cannot unwind what it never
    received.
    """
    conn = _make_event_log(tmp_path)
    stub = _RaisingRegisterObserver()
    try:
        with pytest.raises(RuntimeError, match="root port 0"):
            install_power_observer(conn, observer_factory=lambda: stub)
        assert stub.shutdown_called is True
    finally:
        conn.close()


def test_install_reraises_register_error_even_if_unwind_fails(
    tmp_path: Path,
) -> None:
    """A failing unwind is suppressed; the ORIGINAL register error wins."""
    conn = _make_event_log(tmp_path)
    stub = _RaisingRegisterObserver(shutdown_raises=True)
    try:
        with pytest.raises(RuntimeError, match="root port 0"):
            install_power_observer(conn, observer_factory=lambda: stub)
        assert stub.shutdown_called is True
    finally:
        conn.close()
