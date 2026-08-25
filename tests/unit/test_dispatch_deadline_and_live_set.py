"""Unit tests for ADR-0009 Step 5 — what ``ToolRegistry.dispatch`` newly does.

Two additions, both consumed by the Step-6 supervisor sweep (D4):

1. **Deadline persistence.** ``action.dispatched`` gains the optional
   ``result_expected_by_ms`` payload field, stamped from the per-tool
   budget. Codex (``spawn_worker``) is the only tool with a budget
   Day-1: ``_resolve_codex_turn_timeout_s()`` (600s default,
   ``JARVIS_CODEX_TURN_TIMEOUT_S``-overridable) plus a fixed grace. The
   grace is 100s so the stamped deadline equals the sweep's own legacy
   fallback anchor (``dispatched ts + supervisor.default_budget_s`` =
   700s) at default settings — stamped and unstamped rows then behave
   identically, which is what makes the fallback ladder honest.
2. **Live action-id registration.** The dispatch path registers the
   action into the process-wide live set so the sweep never closes an
   action a turn is still driving (D4 "active-turn exclusion"). The
   *release* side lives in ``drive_turn`` and is covered by
   ``tests/unit/test_drive_turn.py``.

Tier 1: no LLM, no Codex — ``spawn_worker``'s handler is replaced with a
stub BEFORE ``build_default_registry()`` binds it, so the real Codex
subprocess never starts while the real ``spawn_worker`` ToolDefinition
(budget included) is still the one under test.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.deployment import bootstrap_runtime
from jarvis.execution import tools as tools_mod
from jarvis.execution.tools import (
    ActionLifecycle,
    ToolRegistry,
    build_default_registry,
    live_action_ids,
    release_turn_actions,
)
from jarvis.shared import ActionRequest, CallerPrincipal, RawResult
from jarvis.state.event_log import open_event_log

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from jarvis.deployment import RuntimePaths


_STUB_RUN_ID = "run_stub"

# The pinned numbers, written out rather than imported from the module
# under test: 600s Codex turn budget + 100s dispatcher grace = 700s, which
# must equal `supervisor.default_budget_s` so the sweep's fallback anchor
# reconstructs the stamp exactly.
_EXPECTED_GRACE_MS = 100_000
_EXPECTED_SPAN_MS = 700_000


def _stub_spawn_worker_handler(
    action_request: ActionRequest,
    _conn: sqlite3.Connection,
    _runtime_paths: Any,  # noqa: ANN401 — RuntimePathsLike is a structural Protocol.
    _lifecycle: ActionLifecycle,
) -> RawResult:
    """Stand in for ``spawn_worker_handler``; spawns nothing.

    Async-tool contract: leave the lifecycle at ``running`` (the
    dispatcher put it there) and return an ``ack`` slot.
    """
    return RawResult(
        action_id=action_request.action_id,
        semantics="ack",
        payload={"run_id": _STUB_RUN_ID},
        tool_output=None,
        error=None,
    )


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> ToolRegistry:
    """Default registry whose ``spawn_worker`` handler is inert."""
    monkeypatch.setattr(tools_mod, "spawn_worker_handler", _stub_spawn_worker_handler)
    return build_default_registry()


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    """Fresh Event Log under ``tmp_path``."""
    return open_event_log(tmp_path / "mac_events.db")


@pytest.fixture
def paths(tmp_path: Path) -> RuntimePaths:
    """Bootstrapped L6 layout under ``tmp_path``."""
    return bootstrap_runtime(tmp_path / "root")


def _spawn_request(*, action_id: str, turn_id: str | None = "T_deadline") -> ActionRequest:
    """Build the canonical L3 ``spawn_worker`` ActionRequest."""
    return ActionRequest(
        action_id=action_id,
        tool_name="spawn_worker",
        target_entity_ref="task_1",
        caller_principal=CallerPrincipal.JARVIS_LLM,
        risk_level="L2",
        arguments={"task_id": "task_1"},
        authorization_lease=None,
        run_id=None,
        turn_id=turn_id,
    )


def _dispatch(
    registry: ToolRegistry,
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    request: ActionRequest,
) -> ActionLifecycle:
    """Register + authorize ``request``, then dispatch it."""
    lifecycle = ActionLifecycle()
    lifecycle.register(request.action_id)
    lifecycle.transition(request.action_id, "authorized")
    registry.dispatch(request, conn, paths, lifecycle)
    return lifecycle


def _dispatched_payload(conn: sqlite3.Connection, action_id: str) -> dict[str, Any]:
    """Return the ``action.dispatched`` payload for ``action_id``."""
    cursor = conn.execute(
        "SELECT payload_json FROM events WHERE type = 'action.dispatched' ORDER BY id ASC",
    )
    for (payload_json,) in cursor.fetchall():
        payload: dict[str, Any] = json.loads(payload_json)
        if payload.get("action_id") == action_id:
            return payload
    msg = f"no action.dispatched row for action_id={action_id!r}"
    raise AssertionError(msg)


# --- result_expected_by_ms stamp -------------------------------------------


def test_dispatch_stamps_result_expected_by_ms_on_the_codex_path(
    registry: ToolRegistry,
    conn: sqlite3.Connection,
    paths: RuntimePaths,
) -> None:
    """``action.dispatched`` MUST carry ``result_expected_by_ms`` for spawn_worker."""
    request = _spawn_request(action_id="A_stamp")
    try:
        _dispatch(registry, conn, paths, request)
        payload = _dispatched_payload(conn, "A_stamp")
    finally:
        release_turn_actions("T_deadline")

    assert "result_expected_by_ms" in payload, (
        "ADR-0009 D4: the dispatcher must persist the per-tool deadline so the "
        f"supervisor sweep can decide overdue-ness from the log alone: {payload!r}"
    )
    assert isinstance(payload["result_expected_by_ms"], int)


def test_stamped_deadline_is_codex_budget_plus_grace(
    registry: ToolRegistry,
    conn: sqlite3.Connection,
    paths: RuntimePaths,
) -> None:
    """The stamp MUST be ``now + codex budget + grace`` (700s at defaults)."""
    before_ms = int(time.time() * 1000)
    request = _spawn_request(action_id="A_budget")
    try:
        _dispatch(registry, conn, paths, request)
        payload = _dispatched_payload(conn, "A_budget")
    finally:
        release_turn_actions("T_deadline")
    after_ms = int(time.time() * 1000)

    assert before_ms + _EXPECTED_SPAN_MS <= payload["result_expected_by_ms"]
    assert payload["result_expected_by_ms"] <= after_ms + _EXPECTED_SPAN_MS


def test_stamped_deadline_follows_the_codex_turn_timeout_override(
    registry: ToolRegistry,
    conn: sqlite3.Connection,
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``JARVIS_CODEX_TURN_TIMEOUT_S`` MUST move the stamp, not just the driver.

    The in-process driver deadline and the persisted supervisor deadline
    have to stay coherent; a 5s operator override that left a 700s stamp
    behind would keep the sweep waiting 11 minutes on an action the
    driver already gave up on.
    """
    monkeypatch.setenv("JARVIS_CODEX_TURN_TIMEOUT_S", "5")
    before_ms = int(time.time() * 1000)
    request = _spawn_request(action_id="A_override")
    try:
        _dispatch(registry, conn, paths, request)
        payload = _dispatched_payload(conn, "A_override")
    finally:
        release_turn_actions("T_deadline")

    assert payload["result_expected_by_ms"] < before_ms + _EXPECTED_GRACE_MS + 60_000


def test_dispatch_omits_result_expected_by_ms_for_budgetless_tools(
    registry: ToolRegistry,
    conn: sqlite3.Connection,
    paths: RuntimePaths,
) -> None:
    """A tool with no declared budget MUST NOT be stamped.

    Sync tools terminate inside ``dispatch``; only a process death can
    orphan them, and the sweep's ``dispatched ts + default budget``
    fallback covers that case without the dispatcher inventing a number.
    """
    request = ActionRequest(
        action_id="A_nobudget",
        tool_name="get_current_time",
        target_entity_ref=None,
        caller_principal=CallerPrincipal.JARVIS_LLM,
        risk_level="L0",
        arguments={},
        authorization_lease=None,
        run_id=None,
        turn_id="T_deadline",
    )
    try:
        _dispatch(registry, conn, paths, request)
        payload = _dispatched_payload(conn, "A_nobudget")
    finally:
        release_turn_actions("T_deadline")

    assert "result_expected_by_ms" not in payload, (
        f"only tools declaring a budget get a stamp: {payload!r}"
    )


# --- live action-id registration -------------------------------------------


def test_dispatch_registers_the_action_in_the_live_set(
    registry: ToolRegistry,
    conn: sqlite3.Connection,
    paths: RuntimePaths,
) -> None:
    """The dispatch path MUST publish the action_id as live (D4)."""
    request = _spawn_request(action_id="A_live")
    try:
        _dispatch(registry, conn, paths, request)
        assert "A_live" in live_action_ids(), (
            "ADR-0009 D4: the supervisor sweep skips active-set action_ids; "
            "an action nobody registers would be closed out from under a live turn."
        )
    finally:
        release_turn_actions("T_deadline")
    assert "A_live" not in live_action_ids()


def test_dispatch_without_a_turn_id_does_not_leak_into_the_live_set(
    registry: ToolRegistry,
    conn: sqlite3.Connection,
    paths: RuntimePaths,
) -> None:
    """A turn-less dispatch MUST NOT register — nothing would ever release it.

    ``drive_turn`` releases by ``turn_id``; an entry keyed to no turn
    would pin its action_id "active" for the life of the process, which
    is exactly the ghost the sweep exists to close.
    """
    request = _spawn_request(action_id="A_turnless", turn_id=None)
    _dispatch(registry, conn, paths, request)
    assert "A_turnless" not in live_action_ids()
