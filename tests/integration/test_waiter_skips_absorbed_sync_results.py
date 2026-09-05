"""The in-turn waiter wakes on ``worker.reported``, not on a sync tool's old row.

Found live on 2026-09-04 (Wave 4 burn 4 at ``b298be8``): the model ran
``list_tasks`` and ``get_current_time`` before ``spawn_worker`` in one decide()
iteration. Both sync tools wrote ``action.result_observed`` rows the turn owns,
so the waiter returned the first of them as the trigger, the worker's
``worker.reported`` was never handed to L3, no ``action.result_observed`` was
written for the worker, and cleanup recorded ``verification_skipped``.
"""
from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from jarvis.execution.tools import ActionLifecycle
from jarvis.runtime import _wait_for_next_trigger
from jarvis.state.event_log import emit_event, open_event_log

if TYPE_CHECKING:
    from pathlib import Path

_SYNC = "A-sync-list-tasks"
_WORKER = "A-async-spawn-worker"


def _register_at(lifecycle: ActionLifecycle, action_id: str, state: str) -> None:
    lifecycle.register(action_id)
    for step in ("authorized", "dispatched", "running", "result_observed"):
        lifecycle.transition(action_id, step)
        if step == state:
            return


def test_waiter_skips_the_sync_tools_absorbed_row_and_returns_worker_reported(
    tmp_path: Path,
) -> None:
    """Both rows are owned by the turn; only the still-running worker may wake it."""
    lifecycle = ActionLifecycle()
    _register_at(lifecycle, _SYNC, "result_observed")
    _register_at(lifecycle, _WORKER, "running")

    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        emit_event(
            conn,
            type="action.result_observed",
            payload={"action_id": _SYNC, "semantics": "observation"},
            correlation={"action_id": _SYNC, "turn_id": "T1"},
        )
        emit_event(
            conn,
            type="worker.reported",
            payload={"action_id": _WORKER, "status": "ok", "run_id": "R1"},
            correlation={"action_id": _WORKER, "turn_id": "T1"},
        )

        event, _cursor = _wait_for_next_trigger(
            conn,
            after_id=0,
            action_ids=frozenset({_SYNC, _WORKER}),
            lifecycle=lifecycle,
            timeout=0.2,
        )

    assert event.type == "worker.reported"
    assert event.payload["action_id"] == _WORKER
