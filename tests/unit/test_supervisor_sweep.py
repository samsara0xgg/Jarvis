"""Unit tests for the ADR-0009 Step 6 supervisor sweep (D4, spec §3.4.8).

``sweep_overdue_actions`` closes actions the event log shows as open past
their deadline. It shares one low-level fold with
:func:`~jarvis.deployment.sleep_wake.reconcile_after_wake`, parameterized
where the two paths differ:

- the wake path only closes actions that reached ``run.started`` (a
  worker exists to declare terminated); the sweep also closes the
  ``run.started``-less window a daemon crash during preflight leaves
  behind, and for those emits **only** ``action.timeout_assumed`` —
  ``run_id`` is born at ``run.started``, so there is no run to terminate
  and no ``worker.*`` event that would be true.
- the sweep reads deadlines; the wake path has none (a sleep closes
  everything in flight regardless of age).

Deadline ladder under test (D4 bullet 1), all three rungs:

1. ``action.dispatched.result_expected_by_ms`` — the Step-5 stamp.
2. legacy rows lacking it: that action's ``action.dispatched`` ts +
   ``supervisor.default_budget_s``.
3. pre-dispatch-era rows: first-seen ``action.running`` ts + the same
   budget.

Rungs 2 and 3 are exercised with ``default_budget_s=0.0`` rather than by
sleeping 700s — the anchor is what is under test, not the arithmetic.

F7 (at most one ``action.timeout_assumed`` per action_id under every
interleaving) is covered from both directions: the active-set skip, and
the terminal re-check performed immediately before each emit.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.deployment import sleep_wake
from jarvis.deployment.sleep_wake import reconcile_after_wake, sweep_overdue_actions
from jarvis.state.event_log import emit_event, iter_events, open_event_log

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from jarvis.shared import Event


_TURN_ID = "T_sweep"


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    """Fresh Event Log under ``tmp_path``."""
    return open_event_log(tmp_path / "mac_events.db")


def _seed_dispatched(
    conn: sqlite3.Connection,
    *,
    action_id: str,
    result_expected_by_ms: int | None = None,
) -> None:
    """Seed just the ``action.dispatched`` row (optionally stamped)."""
    payload: dict[str, Any] = {"action_id": action_id}
    if result_expected_by_ms is not None:
        payload["result_expected_by_ms"] = result_expected_by_ms
    emit_event(
        conn,
        type="action.dispatched",
        payload=payload,
        correlation={"action_id": action_id, "turn_id": _TURN_ID},
    )


def _seed_open_action(
    conn: sqlite3.Connection,
    *,
    action_id: str,
    result_expected_by_ms: int | None = None,
    with_dispatched: bool = True,
    run_id: str | None = None,
) -> None:
    """Seed the L4 dispatcher event shape for one open action.

    Mirrors ``ToolRegistry.dispatch`` exactly: ``action.dispatched``
    (optionally carrying the Step-5 stamp) then ``action.running`` with
    correlation ``{action_id, turn_id}`` and NO ``run_id``. When
    ``run_id`` is given, a ``run.started`` follows — the only event that
    ever carries a run_id for a real worker run.
    """
    correlation = {"action_id": action_id, "turn_id": _TURN_ID}
    if with_dispatched:
        _seed_dispatched(
            conn,
            action_id=action_id,
            result_expected_by_ms=result_expected_by_ms,
        )
    emit_event(
        conn,
        type="action.running",
        payload={"action_id": action_id},
        correlation=correlation,
    )
    if run_id is not None:
        emit_event(
            conn,
            type="run.started",
            payload={"run_id": run_id, "task_id": "task_1", "runner": "codex"},
            correlation={**correlation, "run_id": run_id},
        )


def _past_ms() -> int:
    """A ``result_expected_by_ms`` one second in the past."""
    return int(time.time() * 1000) - 1_000


def _future_ms() -> int:
    """A ``result_expected_by_ms`` an hour out."""
    return int(time.time() * 1000) + 3_600_000


def _timeout_events(conn: sqlite3.Connection) -> list[Event]:
    """Every ``action.timeout_assumed`` row, in log order."""
    return [evt for evt in iter_events(conn) if evt.type == "action.timeout_assumed"]


def _event_types(conn: sqlite3.Connection) -> list[str]:
    return [evt.type for evt in iter_events(conn)]


# --- the run.started-less window (the blind spot the wake fold skips) ------


def test_sweep_closes_an_action_that_never_reached_run_started(
    conn: sqlite3.Connection,
) -> None:
    """A daemon that died during preflight leaves an action nobody closes."""
    _seed_open_action(conn, action_id="A_norun", result_expected_by_ms=_past_ms())

    closed = sweep_overdue_actions(conn)

    assert closed == 1
    timeouts = _timeout_events(conn)
    assert len(timeouts) == 1
    assert timeouts[0].payload["action_id"] == "A_norun"
    assert timeouts[0].payload["reason"] == "supervisor_sweep"


def test_sweep_emits_no_worker_event_for_a_run_less_orphan(
    conn: sqlite3.Connection,
) -> None:
    """No run exists, so no ``worker.*`` claim about it can be true (D4)."""
    _seed_open_action(conn, action_id="A_norun", result_expected_by_ms=_past_ms())

    sweep_overdue_actions(conn)

    assert not [t for t in _event_types(conn) if t.startswith("worker.")], (
        "run_id is born at run.started; a worker.* event for an action that "
        "never reached it would assert a run that does not exist."
    )


def test_sweep_closes_an_action_with_a_started_run_and_correlates_the_run_id(
    conn: sqlite3.Connection,
) -> None:
    """A run-ful orphan is closed too, with its run_id on the correlation."""
    _seed_open_action(
        conn,
        action_id="A_run",
        result_expected_by_ms=_past_ms(),
        run_id="run_42",
    )

    closed = sweep_overdue_actions(conn)

    assert closed == 1
    timeouts = _timeout_events(conn)
    assert len(timeouts) == 1
    assert timeouts[0].correlation is not None
    assert timeouts[0].correlation["run_id"] == "run_42"
    assert timeouts[0].correlation["action_id"] == "A_run"


def test_sweep_emits_no_worker_event_for_a_run_ful_orphan_either(
    conn: sqlite3.Connection,
) -> None:
    """The sweep is not the sleep path; nothing here was terminated by sleep.

    ADR-0009 §4 registers no new ``worker.*`` type for the supervisor, and
    reusing ``worker.terminated_by_sleep`` would put a false cause on the
    trace. The sweep emits exactly one event per closure.
    """
    _seed_open_action(
        conn,
        action_id="A_run",
        result_expected_by_ms=_past_ms(),
        run_id="run_42",
    )

    sweep_overdue_actions(conn)

    assert not [t for t in _event_types(conn) if t.startswith("worker.")]


# --- deadline ladder --------------------------------------------------------


def test_sweep_leaves_an_action_alone_before_its_deadline(
    conn: sqlite3.Connection,
) -> None:
    """A long-running Codex turn inside its budget MUST NOT be closed."""
    _seed_open_action(conn, action_id="A_fresh", result_expected_by_ms=_future_ms())

    assert sweep_overdue_actions(conn) == 0
    assert _timeout_events(conn) == []


def test_sweep_falls_back_to_dispatched_ts_for_rows_without_the_stamp(
    conn: sqlite3.Connection,
) -> None:
    """Legacy rung: ``action.dispatched`` ts + ``supervisor.default_budget_s``."""
    _seed_open_action(conn, action_id="A_legacy", result_expected_by_ms=None)

    # A zero budget makes "dispatched ts + budget" already past, so the
    # anchor is what the assertion is about — not a 700s wait.
    assert sweep_overdue_actions(conn, default_budget_s=0.0) == 1
    assert _timeout_events(conn)[0].payload["action_id"] == "A_legacy"


def test_sweep_honors_the_default_budget_for_rows_without_the_stamp(
    conn: sqlite3.Connection,
) -> None:
    """The same legacy row is NOT overdue while the budget still covers it."""
    _seed_open_action(conn, action_id="A_legacy", result_expected_by_ms=None)

    assert sweep_overdue_actions(conn, default_budget_s=3_600.0) == 0


def test_sweep_falls_back_to_action_running_ts_for_pre_dispatch_era_rows(
    conn: sqlite3.Connection,
) -> None:
    """Bottom rung: no ``action.dispatched`` row exists at all."""
    _seed_open_action(conn, action_id="A_ancient", with_dispatched=False)

    assert sweep_overdue_actions(conn, default_budget_s=0.0) == 1
    assert _timeout_events(conn)[0].payload["action_id"] == "A_ancient"


def test_sweep_closes_an_action_dispatched_but_never_running(
    conn: sqlite3.Connection,
) -> None:
    """The crash window between ``action.dispatched`` and ``action.running``."""
    _seed_dispatched(
        conn,
        action_id="A_predispatch",
        result_expected_by_ms=_past_ms(),
    )

    assert sweep_overdue_actions(conn) == 1


# --- F7: at most one action.timeout_assumed per action_id ------------------


def test_sweep_is_idempotent(conn: sqlite3.Connection) -> None:
    """A second tick MUST NOT re-close what the first tick closed."""
    _seed_open_action(conn, action_id="A_once", result_expected_by_ms=_past_ms())

    assert sweep_overdue_actions(conn) == 1
    assert sweep_overdue_actions(conn) == 0
    assert len(_timeout_events(conn)) == 1


def test_sweep_skips_actions_already_carrying_a_terminal_event(
    conn: sqlite3.Connection,
) -> None:
    """An action the in-turn driver already closed is not the sweep's business."""
    _seed_open_action(conn, action_id="A_done", result_expected_by_ms=_past_ms())
    emit_event(
        conn,
        type="action.result_observed",
        payload={"action_id": "A_done", "semantics": "ack"},
        correlation={"action_id": "A_done"},
    )

    assert sweep_overdue_actions(conn) == 0
    assert _timeout_events(conn) == []


def test_sweep_skips_action_ids_in_the_active_set(conn: sqlite3.Connection) -> None:
    """An action a live turn is still driving MUST survive the sweep (D4)."""
    _seed_open_action(conn, action_id="A_live", result_expected_by_ms=_past_ms())

    closed = sweep_overdue_actions(conn, active_action_ids=frozenset({"A_live"}))

    assert closed == 0
    assert _timeout_events(conn) == []


def test_sweep_still_closes_actions_outside_the_active_set(
    conn: sqlite3.Connection,
) -> None:
    """The active-set skip is per action_id, not a global off switch."""
    _seed_open_action(conn, action_id="A_live", result_expected_by_ms=_past_ms())
    _seed_open_action(conn, action_id="A_orphan", result_expected_by_ms=_past_ms())

    closed = sweep_overdue_actions(conn, active_action_ids=frozenset({"A_live"}))

    assert closed == 1
    assert _timeout_events(conn)[0].payload["action_id"] == "A_orphan"


def test_sweep_rechecks_for_a_terminal_event_immediately_before_each_emit(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal event landing mid-sweep MUST cancel the pending emit.

    Cross-process safety: a detached child from the pre-install era, or
    the in-turn driver, can close an action between the sweep's fold and
    its emit. A fold-once/emit-all sweep would double-emit. Simulated by
    closing ``A_second`` from inside the ``A_first`` emit.
    """
    _seed_open_action(conn, action_id="A_first", result_expected_by_ms=_past_ms())
    _seed_open_action(conn, action_id="A_second", result_expected_by_ms=_past_ms())

    # The same function object ``sleep_wake`` holds; monkeypatching the
    # module attribute below swaps only the sweep's view of it.
    real_emit = emit_event

    def _emit_and_race(
        target: sqlite3.Connection,
        *,
        type: str,  # noqa: A002 — mirrors emit_event's own keyword name.
        payload: dict[str, Any],
        correlation: dict[str, str] | None = None,
    ) -> Event:
        event = real_emit(target, type=type, payload=payload, correlation=correlation)
        if payload.get("action_id") == "A_first":
            real_emit(
                target,
                type="action.result_observed",
                payload={"action_id": "A_second", "semantics": "ack"},
                correlation={"action_id": "A_second"},
            )
        return event

    monkeypatch.setattr(sleep_wake, "emit_event", _emit_and_race)

    closed = sweep_overdue_actions(conn)

    assert closed == 1
    timeouts = _timeout_events(conn)
    assert [evt.payload["action_id"] for evt in timeouts] == ["A_first"], (
        "F7: at most one action.timeout_assumed per action_id under every "
        "interleaving — the pre-emit terminal re-check is what holds here."
    )


# --- the wake path must not change (K7 / K8 are live-certified on it) ------


def test_reconcile_after_wake_still_skips_run_less_actions(
    conn: sqlite3.Connection,
) -> None:
    """Sharing the fold MUST NOT give the wake path the sweep's wider reach.

    ``reconcile_after_wake`` emits ``worker.terminated_by_sleep`` per
    action; doing that for an action with no run would assert a run that
    never started. Its blind spot is deliberate.
    """
    _seed_open_action(conn, action_id="A_norun", result_expected_by_ms=_past_ms())

    assert reconcile_after_wake(conn) == 0
    assert _event_types(conn).count("worker.terminated_by_sleep") == 0
    assert _timeout_events(conn) == []


def test_reconcile_after_wake_still_closes_run_ful_actions_ignoring_deadlines(
    conn: sqlite3.Connection,
) -> None:
    """A sleep kills the subprocess whatever the deadline said."""
    _seed_open_action(
        conn,
        action_id="A_run",
        result_expected_by_ms=_future_ms(),
        run_id="run_7",
    )

    assert reconcile_after_wake(conn) == 1
    types = _event_types(conn)
    assert types.count("worker.terminated_by_sleep") == 1
    assert types.count("action.timeout_assumed") == 1


def test_sweep_input_validation_rejects_a_none_connection() -> None:
    """Same contract as the other sleep_wake entry points."""
    with pytest.raises(ValueError, match="event_log"):
        sweep_overdue_actions(None)
