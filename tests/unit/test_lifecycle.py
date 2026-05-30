"""Unit tests for `jarvis.execution.tools.ActionLifecycle` (ADR § Acceptance B).

Covers: every valid transition succeeds, every illegal transition raises,
terminal states reject any further transition, and `is_terminal` returns
True for each terminal state.
"""

from __future__ import annotations

import threading

import pytest

from jarvis.execution.tools import (
    ActionLifecycle,
    IllegalLifecycleTransition,
    LifecycleState,
)


def _register(action_id: str = "A1") -> tuple[ActionLifecycle, str]:
    """Build a lifecycle with `action_id` registered at `proposed`."""
    lc = ActionLifecycle()
    lc.register(action_id)
    return lc, action_id


# --- Initial state ----------------------------------------------------------


def test_register_sets_initial_state_proposed() -> None:
    """A freshly registered action is at `proposed`."""
    lc, action_id = _register()
    assert lc.state_of(action_id) == "proposed"


def test_state_of_unknown_action_returns_none() -> None:
    """Unknown action_ids return None (not an error)."""
    lc = ActionLifecycle()
    assert lc.state_of("never_registered") is None


def test_is_terminal_unknown_action_returns_false() -> None:
    """`is_terminal` for an unknown action_id is False, not an error."""
    lc = ActionLifecycle()
    assert lc.is_terminal("never_registered") is False


def test_register_twice_raises() -> None:
    """Re-registering an action_id raises `IllegalLifecycleTransition`."""
    lc, action_id = _register()
    with pytest.raises(IllegalLifecycleTransition):
        lc.register(action_id)


# --- Valid transitions (canonical happy path & every branch) ----------------


def test_proposed_to_authorized_to_dispatched_to_running_to_result_observed() -> None:
    """Canonical happy-path sequence (ADR § Acceptance B2)."""
    lc, action_id = _register()
    lc.transition(action_id, "authorized")
    assert lc.state_of(action_id) == "authorized"
    lc.transition(action_id, "dispatched")
    assert lc.state_of(action_id) == "dispatched"
    lc.transition(action_id, "running")
    assert lc.state_of(action_id) == "running"
    lc.transition(action_id, "result_observed")
    assert lc.state_of(action_id) == "result_observed"
    assert lc.is_terminal(action_id) is True


def test_proposed_to_cancelled() -> None:
    """Proposed → cancelled is the early-cancel path."""
    lc, action_id = _register()
    lc.transition(action_id, "cancelled")
    assert lc.is_terminal(action_id) is True


def test_authorized_to_cancelled() -> None:
    """Authorized → cancelled (allen revokes between gate and dispatch)."""
    lc, action_id = _register()
    lc.transition(action_id, "authorized")
    lc.transition(action_id, "cancelled")
    assert lc.is_terminal(action_id) is True


def test_dispatched_to_failed() -> None:
    """Dispatched → failed (handler invocation raised before running)."""
    lc, action_id = _register()
    lc.transition(action_id, "authorized")
    lc.transition(action_id, "dispatched")
    lc.transition(action_id, "failed")
    assert lc.is_terminal(action_id) is True


def test_dispatched_to_cancelled() -> None:
    """Dispatched → cancelled."""
    lc, action_id = _register()
    lc.transition(action_id, "authorized")
    lc.transition(action_id, "dispatched")
    lc.transition(action_id, "cancelled")
    assert lc.is_terminal(action_id) is True


def test_running_to_failed() -> None:
    """Running → failed (handler raised after async dispatch started)."""
    lc, action_id = _register()
    lc.transition(action_id, "authorized")
    lc.transition(action_id, "dispatched")
    lc.transition(action_id, "running")
    lc.transition(action_id, "failed")
    assert lc.is_terminal(action_id) is True


def test_running_to_timeout_assumed() -> None:
    """Running → timeout_assumed (no worker.reported within deadline)."""
    lc, action_id = _register()
    lc.transition(action_id, "authorized")
    lc.transition(action_id, "dispatched")
    lc.transition(action_id, "running")
    lc.transition(action_id, "timeout_assumed")
    assert lc.is_terminal(action_id) is True


def test_running_to_cancelled() -> None:
    """Running → cancelled (allen cancels in-flight)."""
    lc, action_id = _register()
    lc.transition(action_id, "authorized")
    lc.transition(action_id, "dispatched")
    lc.transition(action_id, "running")
    lc.transition(action_id, "cancelled")
    assert lc.is_terminal(action_id) is True


# --- Illegal transitions (the contract the dispatcher relies on) ------------


def test_proposed_to_dispatched_raises() -> None:
    """`proposed → dispatched` skips the authorized state — illegal."""
    lc, action_id = _register()
    with pytest.raises(IllegalLifecycleTransition):
        lc.transition(action_id, "dispatched")


def test_proposed_to_running_raises() -> None:
    """`proposed → running` skips two states — illegal."""
    lc, action_id = _register()
    with pytest.raises(IllegalLifecycleTransition):
        lc.transition(action_id, "running")


def test_proposed_to_result_observed_raises() -> None:
    """`proposed → result_observed` skips everything — illegal."""
    lc, action_id = _register()
    with pytest.raises(IllegalLifecycleTransition):
        lc.transition(action_id, "result_observed")


def test_authorized_to_running_raises() -> None:
    """`authorized → running` skips dispatched — illegal."""
    lc, action_id = _register()
    lc.transition(action_id, "authorized")
    with pytest.raises(IllegalLifecycleTransition):
        lc.transition(action_id, "running")


def test_dispatched_to_result_observed_raises() -> None:
    """`dispatched → result_observed` skips running — illegal (ADR B2)."""
    lc, action_id = _register()
    lc.transition(action_id, "authorized")
    lc.transition(action_id, "dispatched")
    with pytest.raises(IllegalLifecycleTransition):
        lc.transition(action_id, "result_observed")


def test_unknown_action_transition_raises() -> None:
    """Transition on an unregistered action_id raises."""
    lc = ActionLifecycle()
    with pytest.raises(IllegalLifecycleTransition):
        lc.transition("ghost", "authorized")


# --- Terminal states reject all further transitions -------------------------


_TERMINAL_TARGETS: list[LifecycleState] = [
    "proposed",
    "authorized",
    "dispatched",
    "running",
    "result_observed",
    "failed",
    "timeout_assumed",
    "cancelled",
]


def _drive_to(lc: ActionLifecycle, action_id: str, terminal: LifecycleState) -> None:
    """Drive `action_id` to a given terminal state via canonical path."""
    if terminal == "cancelled":
        lc.transition(action_id, "cancelled")
        return
    lc.transition(action_id, "authorized")
    lc.transition(action_id, "dispatched")
    lc.transition(action_id, "running")
    lc.transition(action_id, terminal)


@pytest.mark.parametrize(
    "terminal",
    ["result_observed", "failed", "timeout_assumed", "cancelled"],
)
def test_terminal_is_terminal_returns_true(terminal: LifecycleState) -> None:
    """Each of the 4 terminal states reports `is_terminal == True`."""
    lc, action_id = _register()
    _drive_to(lc, action_id, terminal)
    assert lc.is_terminal(action_id) is True
    assert lc.state_of(action_id) == terminal


@pytest.mark.parametrize(
    "terminal",
    ["result_observed", "failed", "timeout_assumed", "cancelled"],
)
@pytest.mark.parametrize("target", _TERMINAL_TARGETS)
def test_terminal_rejects_all_outgoing_transitions(
    terminal: LifecycleState, target: LifecycleState
) -> None:
    """Once terminal, no transition is allowed (covers each target x terminal)."""
    lc, action_id = _register()
    _drive_to(lc, action_id, terminal)
    with pytest.raises(IllegalLifecycleTransition):
        lc.transition(action_id, target)


# --- Thread-safety smoke ----------------------------------------------------


def test_concurrent_transitions_on_distinct_action_ids() -> None:
    """Two threads transitioning different action_ids must both succeed.

    The RLock serializes access to the dict; the test is a smoke check
    that the lock doesn't deadlock when used from multiple threads.
    """
    lc = ActionLifecycle()
    n_per_thread = 50
    barrier = threading.Barrier(2)

    def worker(prefix: str) -> None:
        barrier.wait()
        for i in range(n_per_thread):
            aid = f"{prefix}_{i}"
            lc.register(aid)
            lc.transition(aid, "authorized")
            lc.transition(aid, "dispatched")
            lc.transition(aid, "running")
            lc.transition(aid, "result_observed")

    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("b",))
    t1.start()
    t2.start()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)
    assert not t1.is_alive()
    assert not t2.is_alive()

    for prefix in ("a", "b"):
        for i in range(n_per_thread):
            assert lc.state_of(f"{prefix}_{i}") == "result_observed"
            assert lc.is_terminal(f"{prefix}_{i}") is True
