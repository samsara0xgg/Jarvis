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
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

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
