"""Unit tests for `list_tasks` L4 read-only tool (F6).

Seeds two `task.created` events (one left open, one walked through the
full verification chain `task.verified` + Postcondition `claim.created`
+ verified `evidence.attached`) and dispatches `list_tasks` twice via
the default registry — once filtered to `status="open"`, once with
`status="all"` — asserting the derived-status filter selects the
intended subset.

The verification-chain seed mirrors the fold rule in
`jarvis.state.projections._derive_status`: a Postcondition Claim on the
task subject_ref with an `evidence.attached(level="verified")` flips
the derived status to `verified_complete` once `task.verified` has
fired.
"""

from __future__ import annotations

import json
from contextlib import closing
from typing import TYPE_CHECKING, Any

from jarvis.deployment import bootstrap_runtime
from jarvis.execution.tools import (
    ActionLifecycle,
    RawResult,
    build_default_registry,
)
from jarvis.shared import ActionRequest, CallerPrincipal
from jarvis.state.event_log import emit_event, open_event_log

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from jarvis.deployment import RuntimePaths


# --- Helpers ----------------------------------------------------------------


def _build_action_request(
    *,
    action_id: str,
    arguments: dict[str, Any],
) -> ActionRequest:
    """Build an `ActionRequest` for the `list_tasks` tool."""
    return ActionRequest(
        action_id=action_id,
        tool_name="list_tasks",
        target_entity_ref=None,
        caller_principal=CallerPrincipal.JARVIS_LLM,
        risk_level="L0",
        arguments=arguments,
        authorization_lease=None,
        run_id=None,
        turn_id="T_lt1",
    )


def _open_runtime(tmp_path: Path) -> tuple[RuntimePaths, sqlite3.Connection]:
    """Bootstrap runtime + open the Event Log."""
    paths = bootstrap_runtime(root=tmp_path)
    conn = open_event_log(paths.event_log)
    return paths, conn


def _seed_lifecycle(lifecycle: ActionLifecycle, action_id: str) -> None:
    """Register + authorize an action (what L3 does pre-dispatch)."""
    lifecycle.register(action_id)
    lifecycle.transition(action_id, "authorized")


def _seed_open_task(conn: sqlite3.Connection, *, task_id: str, goal: str) -> None:
    """Emit a single `task.created` (no further events => derives `open`)."""
    emit_event(
        conn,
        type="task.created",
        payload={"task_id": task_id, "goal": goal, "source": "manual"},
        correlation={"task_id": task_id},
    )


def _seed_verified_task(conn: sqlite3.Connection, *, task_id: str, goal: str) -> None:
    """Walk a task all the way to `verified_complete` per `_derive_status`.

    Sequence:
        1. `task.created(task_id, goal)`
        2. `task.verified(task_id)` — populates
           `record.task_verified_event_uids`.
        3. `claim.created` with `type="Postcondition"` and
           `subject_ref=task_id` — gives the fold rule a claim to walk.
        4. `evidence.attached` for that claim with `level="verified"` —
           passes the ladder check `{verified, accepted}` in
           `_derive_status`.
    """
    created = emit_event(
        conn,
        type="task.created",
        payload={"task_id": task_id, "goal": goal, "source": "manual"},
        correlation={"task_id": task_id},
    )
    emit_event(
        conn,
        type="task.verified",
        payload={"task_id": task_id, "by": "test_seed"},
        source_event_id=created.event_uid,
        correlation={"task_id": task_id},
    )
    claim_id = f"C_{task_id}"
    claim = emit_event(
        conn,
        type="claim.created",
        payload={
            "claim_id": claim_id,
            "type": "Postcondition",
            "statement": f"task {task_id} postcondition met",
            "subject_ref": task_id,
            "produced_by_event_id": created.event_uid,
        },
        source_event_id=created.event_uid,
        correlation={"task_id": task_id, "claim_id": claim_id},
    )
    emit_event(
        conn,
        type="evidence.attached",
        payload={
            "evidence_id": f"E_{task_id}",
            "claim_id": claim_id,
            "relation": "supports",
            "level": "verified",
        },
        source_event_id=claim.event_uid,
        correlation={"task_id": task_id, "claim_id": claim_id},
    )


# --- The test ---------------------------------------------------------------


def test_list_tasks_returns_open_tasks(tmp_path: Path) -> None:
    """`status="open"` returns only the open task; `status="all"` returns both.

    Seeds two tasks: one left open (`task_OPEN_A`), one walked through
    `task.verified` + Postcondition Claim + verified Evidence so the
    Task Ledger fold rule classifies it as `verified_complete`
    (`task_VERIFIED_B`). Dispatches `list_tasks` twice through the
    default registry (one action_id per dispatch — lifecycle is
    one-shot) and asserts the filter selects the intended subset.
    """
    paths, conn = _open_runtime(tmp_path)
    try:
        _seed_open_task(conn, task_id="task_OPEN_A", goal="buy milk")
        _seed_verified_task(conn, task_id="task_VERIFIED_B", goal="ship feature")

        registry = build_default_registry()
        lifecycle = ActionLifecycle()

        # First dispatch: status="open" -> expect only task_OPEN_A.
        req_open = _build_action_request(
            action_id="A_lt_open",
            arguments={"status": "open"},
        )
        _seed_lifecycle(lifecycle, req_open.action_id)
        bundle_open = registry.dispatch(req_open, conn, paths, lifecycle)
        assert len(bundle_open.slots) == 1
        result_open = bundle_open.slots[0]
        assert isinstance(result_open, RawResult)
        assert result_open.semantics == "observation"
        assert result_open.error is None

        tasks_open = result_open.payload["tasks"]
        assert len(tasks_open) == 1
        assert tasks_open[0]["task_id"] == "task_OPEN_A"
        assert tasks_open[0]["goal"] == "buy milk"
        assert tasks_open[0]["status"] == "open"

        # tool_output is the JSON-encoded same payload.
        assert result_open.tool_output is not None
        decoded = json.loads(result_open.tool_output)
        assert decoded == {"tasks": tasks_open}

        # Second dispatch: status="all" -> expect both tasks.
        # Mint a fresh action_id since the lifecycle FSM is one-shot per id.
        req_all = _build_action_request(
            action_id="A_lt_all",
            arguments={"status": "all"},
        )
        _seed_lifecycle(lifecycle, req_all.action_id)
        bundle_all = registry.dispatch(req_all, conn, paths, lifecycle)
        result_all = bundle_all.slots[0]
        assert isinstance(result_all, RawResult)
        assert result_all.semantics == "observation"

        tasks_all = result_all.payload["tasks"]
        assert len(tasks_all) == 2
        seen_ids = {t["task_id"] for t in tasks_all}
        assert seen_ids == {"task_OPEN_A", "task_VERIFIED_B"}
        by_id = {t["task_id"]: t for t in tasks_all}
        assert by_id["task_OPEN_A"]["status"] == "open"
        assert by_id["task_VERIFIED_B"]["status"] == "verified"
    finally:
        conn.close()
        with closing(open_event_log(paths.event_log)):
            # Open + close to ensure no leaked write locks on tmp_path.
            pass
