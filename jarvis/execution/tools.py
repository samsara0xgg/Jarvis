"""L4 Capability Execution — ToolRegistry + ActionLifecycle + Day-1 stubs.

This module is the SINGLE place in `jarvis/` allowed to:

- dispatch L4 tool handlers,
- schedule `threading.Timer` worker reports,
- write artifact files into `${runtime_paths.artifacts_root}/run_<run_id>/`.

Per spec.html §3.4 (`ActionRequest` / lifecycle / ToolRegistry) +
spec.html §3.5 (CallerPrincipal scoping) + ADR 0001 § Stub strategy L4
rows + § Canonical event trace evt 04..21 + § Acceptance B (lifecycle).

Day-1 ships two tools:

- `spawn_worker` — ASYNC L2 task action. Synchronously emits
  `action.dispatched` → `action.running`, writes a real diff artifact
  (`${run_dir}/diff.json` with `{"status": _SPAWN_WORKER_ARTIFACT_STATUS}`),
  then schedules `worker.reported` via `threading.Timer` (true async
  re-entry per ADR § Async lifecycle pattern). Lifecycle is left at
  `running`; L3's Result Interpreter (Step 9) emits
  `action.result_observed(semantics=report)` later, in response to the
  `worker.reported` row.
- `verify_diff` — SYNC L0 read. Reads the artifact, checks predicate
  `data["status"] == "ok"`, emits `action.result_observed` with
  `semantics="verification"` (match) or `semantics="error"` (miss),
  transitioning lifecycle `running → result_observed`.

Controllable artifact knob:
    `_SPAWN_WORKER_ARTIFACT_STATUS` is a module-level constant. The Step 13
    negative scenario fixture monkeypatches it to `"fail"` so `verify_diff`
    fails the predicate. The LLM never sees this knob — `spawn_worker`'s
    `input_schema` only exposes `task_id`.

Thread safety:
    `threading.Timer` runs the callback on a worker thread. `sqlite3`
    connections default to `check_same_thread=True`, so the callback
    opens its OWN connection from the DB path (a `Path`, captured by
    value into the closure). `check_same_thread=False` is intentionally
    NOT used. The DB path, action_id, run_id, task_id,
    source_event_id, and the diff path string are the only values the
    closure captures; no `sqlite3.Connection` or `ActionLifecycle`
    instance crosses the thread boundary.

`tool_result` / `tool_error` JSON serializers are adapted verbatim from
`/Users/alllllenshi/Projects/jarvis-legacy/tools_v2/helpers.py` per
ADR § Reference sources.

`RuntimePaths` is consumed via a structural `RuntimePathsLike` Protocol
(see below) so this module does NOT import `jarvis.deployment` — that
would violate the `.importlinter` middle-layer sibling rule (execution
and deployment are independent siblings in `decision | execution |
surface | deployment`). `jarvis.runtime` (the composition root, Step 10)
passes a real `jarvis.deployment.RuntimePaths` into the registry; it
structurally satisfies the Protocol.

Layer boundary (`.importlinter` + canary H13 in Step 11): stdlib only
plus `jarvis.shared` and `jarvis.state`. No imports from
`jarvis.constitution`, `jarvis.decision`, `jarvis.surface`,
`jarvis.deployment`, `jarvis.runtime`, `jarvis.cli`.

ToolHandler signature
=====================

A `ToolHandler` is::

    Callable[
        [ActionRequest, sqlite3.Connection, RuntimePathsLike, ActionLifecycle],
        RawResult,
    ]

The handler MUST emit any async events itself; for sync handlers it
also transitions the lifecycle to a terminal state before returning.
For async handlers the lifecycle is left at `running` and a worker
thread / callback transitions it terminal once a result arrives.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

from jarvis.shared import ActionRequest, CallerPrincipal, RawResult, ResultSemantics, RiskLevel
from jarvis.state.event_log import emit_event, open_event_log

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping
    from pathlib import Path


# --- Controllable knobs ------------------------------------------------------

# Step 13 monkeypatches this to "fail" to drive the negative scenario.
# `spawn_worker_handler` writes this value into `diff.json["status"]`.
# Kept module-level (not an `ActionRequest.arguments` field) so the LLM's
# input_schema for `spawn_worker` stays schematically clean (only `task_id`).
_SPAWN_WORKER_ARTIFACT_STATUS: str = "ok"

# Delay before the spawn_worker Timer fires `worker.reported`. 10 ms is
# enough for the dispatcher to return and the test to start polling, but
# small enough to keep Tier 1 wall-clock well under 30 s. Module-level so
# tests can monkeypatch if needed (default works for current Tier 1).
_TIMER_DELAY_SECONDS: float = 0.01

# Test hook: when set to a list, `_emit_worker_reported` appends the
# `threading.current_thread()` of the callback. Used by acceptance B4 readiness
# to assert the worker callback runs on a thread distinct from the main flow.
# Module-level (not part of public API); tests set + clear.
_TEST_MODE_THREAD_CAPTURE: list[threading.Thread] | None = None


# --- Public type aliases -----------------------------------------------------

# `RawResult` and `ResultSemantics` live in `jarvis.shared` since Step 0b of
# ADR-0002 (so L3 can import them without crossing the layer DAG). They are
# re-exported via `__all__` below for backward compatibility with Day-1
# callers that imported them from `jarvis.execution.tools`.


class RuntimePathsLike(Protocol):
    """Structural view of `jarvis.deployment.RuntimePaths` used by L4.

    L4 cannot import L6 (sibling layers in `.importlinter`). The composition
    root (`jarvis.runtime`, Step 10) passes a real `RuntimePaths` instance;
    it satisfies this Protocol structurally. Day-1 only needs `event_log`
    (DB path for the Timer callback) and `artifact_dir_for_run` (per-run
    artifact directory).
    """

    @property
    def event_log(self) -> Path:
        """Path to the SQLite Event Log file (used by Timer callback)."""
        ...

    def artifact_dir_for_run(self, run_id: str) -> Path:
        """Return (and create) the per-run artifact directory."""
        ...


# --- Exceptions --------------------------------------------------------------


class ToolRegistryError(Exception):
    """Base class for ToolRegistry-level failures."""


class DuplicateToolError(ToolRegistryError):
    """Raised when `register` is called twice for the same tool name."""


class UnknownToolError(ToolRegistryError):
    """Raised when `dispatch` receives an `ActionRequest.tool_name` that is not registered."""


class CallerNotAllowedError(ToolRegistryError):
    """Raised when an ActionRequest's `caller_principal` is not in `allowed_callers`.

    Defense-in-depth: the L3 Pre-action Gate normally catches this before
    dispatch; this raise is the L4 belt-and-braces enforcement so a bug
    in L3 can never silently dispatch a forbidden caller.
    """


class IllegalLifecycleTransition(Exception):  # noqa: N818 — name follows spec § Acceptance B vocabulary.
    """Raised on any state transition not in the 8-state DAG (or post-terminal mutation).

    Also raised by `ToolRegistry.dispatch` as the "dispatch precondition"
    failure when the action is not at `authorized` at the moment of
    dispatch.
    """


# --- ActionLifecycle 8-state machine (ADR § Acceptance B) -------------------

LifecycleState = Literal[
    "proposed",
    "authorized",
    "dispatched",
    "running",
    "result_observed",
    "failed",
    "timeout_assumed",
    "cancelled",
]
"""The 8 ActionLifecycle states (ADR § Acceptance B).

States are typed as a `Literal` so mypy can spot typos and so callers
can document their expected pre/post conditions precisely.
"""

# Allowed transitions per ADR § Acceptance B + step 6 spec. Terminal states
# (`result_observed`, `failed`, `timeout_assumed`, `cancelled`) have empty
# outgoing sets, so any transition out of them is illegal.
_VALID_TRANSITIONS: Final[Mapping[LifecycleState, frozenset[LifecycleState]]] = {
    "proposed": frozenset({"authorized", "cancelled"}),
    "authorized": frozenset({"dispatched", "cancelled"}),
    "dispatched": frozenset({"running", "failed", "cancelled"}),
    "running": frozenset({"result_observed", "failed", "timeout_assumed", "cancelled"}),
    "result_observed": frozenset(),
    "failed": frozenset(),
    "timeout_assumed": frozenset(),
    "cancelled": frozenset(),
}

_TERMINAL_STATES: Final[frozenset[LifecycleState]] = frozenset(
    {"result_observed", "failed", "timeout_assumed", "cancelled"}
)


class ActionLifecycle:
    """Per-action lifecycle FSM. Does NOT emit events — callers do that.

    Thread-safe via `threading.RLock` around the internal `dict`. Day-1
    contention is low (only L3 / L4 / L3-Result-Interpreter mutate;
    Timer callback only reads via emit), but the lock is cheap insurance
    against an L3 reinvocation racing an L4 verify in tests that exercise
    concurrent action_ids.
    """

    def __init__(self) -> None:
        """Construct an empty FSM (no actions registered yet)."""
        self._state: dict[str, LifecycleState] = {}
        self._lock = threading.RLock()

    def register(self, action_id: str) -> None:
        """Register a new action at initial state `proposed`.

        Idempotency: re-registering raises `IllegalLifecycleTransition`
        (a re-register would lose any prior progress and is almost
        certainly a bug; explicit re-use of an action_id should go
        through `cancelled` first).
        """
        with self._lock:
            if action_id in self._state:
                msg = (
                    f"action_id={action_id!r} is already registered "
                    f"(current state={self._state[action_id]!r})"
                )
                raise IllegalLifecycleTransition(msg)
            self._state[action_id] = "proposed"

    def transition(self, action_id: str, new_state: LifecycleState) -> None:
        """Move `action_id` from its current state to `new_state`.

        Validates via the `_VALID_TRANSITIONS` map; raises
        `IllegalLifecycleTransition` if `action_id` is unknown, currently
        terminal, or `new_state` is not in the allowed outgoing set.
        """
        with self._lock:
            current = self._state.get(action_id)
            if current is None:
                msg = f"action_id={action_id!r} is not registered"
                raise IllegalLifecycleTransition(msg)
            allowed = _VALID_TRANSITIONS[current]
            if new_state not in allowed:
                msg = (
                    f"illegal transition for action_id={action_id!r}: "
                    f"{current!r} → {new_state!r} (allowed from {current!r}: "
                    f"{sorted(allowed)!r})"
                )
                raise IllegalLifecycleTransition(msg)
            self._state[action_id] = new_state

    def state_of(self, action_id: str) -> LifecycleState | None:
        """Return the current state, or None if `action_id` is unknown."""
        with self._lock:
            return self._state.get(action_id)

    def is_terminal(self, action_id: str) -> bool:
        """Return True if the action is in one of the 4 terminal states."""
        with self._lock:
            state = self._state.get(action_id)
            if state is None:
                return False
            return state in _TERMINAL_STATES


# --- ToolDefinition dataclass -----------------------------------------------
#
# `RawResult` (and `ResultSemantics`) live in `jarvis.shared` since Step 0b;
# they are imported at the top of this module and re-exported via `__all__`.


if TYPE_CHECKING:
    ToolHandler = Callable[
        [ActionRequest, "sqlite3.Connection", RuntimePathsLike, ActionLifecycle],
        RawResult,
    ]


@dataclass(frozen=True)
class ToolDefinition:
    """One registered tool (ADR § Module map L4 row).

    Attributes:
        name: Stable string the LLM uses to call this tool.
        description: One-line description (shown in the tool list the
            LLM receives via `for_caller(...)`).
        allowed_callers: Frozen set of `CallerPrincipal` enum values that
            may dispatch this tool. Defense-in-depth: L3 Pre-action Gate
            checks this first; L4 dispatch raises `CallerNotAllowedError`
            if anyone slips through.
        risk_level: One of L0..L4 (matches `RiskLevel` in shared types).
            Compared against `EffectivePolicy.autonomy_ceiling` at the
            Pre-action Gate.
        result_semantics: How the Result Interpreter maps this tool's
            result. See ADR § Gate contracts table.
        is_async: True if the handler schedules a follow-up event (e.g.
            `spawn_worker` schedules `worker.reported`). Used by the
            dispatcher's documentation and by tests to know not to
            expect `action.result_observed` immediately.
        input_schema: JSON schema describing the tool's arguments
            (Anthropic-style `{type, properties, required}` shape).
        handler: The callable that actually executes the tool.
    """

    name: str
    description: str
    allowed_callers: frozenset[CallerPrincipal]
    risk_level: RiskLevel
    result_semantics: ResultSemantics
    is_async: bool
    input_schema: Mapping[str, Any]
    handler: ToolHandler


# --- JSON serializers (adapted from legacy tools_v2/helpers.py) -------------


def tool_error(message: object, *, code: str | None = None, **extra: object) -> str:
    """Serialize a tool failure as JSON.

    Verbatim shape from `jarvis-legacy/tools_v2/helpers.py` (ADR §
    Reference sources). Day-1 adds an optional `code` kwarg so handlers
    can carry a short machine-readable tag alongside the human message.

    Args:
        message: Human-readable error string (coerced via `str`).
        code: Optional machine-readable error tag (e.g. "artifact_missing").
        **extra: Optional additional keys to merge into the payload.

    Returns:
        JSON string of the form ``{"error": "<msg>", ...}`` with
        `ensure_ascii=False` so CJK characters survive intact.
    """
    payload: dict[str, Any] = {"error": str(message)}
    if code is not None:
        payload["code"] = code
    if extra:
        payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def tool_result(data: Mapping[str, Any] | None = None, **kwargs: object) -> str:
    """Serialize a tool success result as JSON.

    Verbatim shape from `jarvis-legacy/tools_v2/helpers.py`. Accepts a
    `data` mapping positional argument OR keyword arguments — kwargs are
    ignored if `data` is provided. Normalizes `Mapping` to `dict` before
    `json.dumps` so `MappingProxyType` / read-only mappings serialize.
    """
    if data is not None:
        payload: dict[str, Any] = dict(data)
    else:
        payload = dict(kwargs)
    return json.dumps(payload, ensure_ascii=False)


# --- spawn_worker / verify_diff handlers ------------------------------------


def _emit_worker_reported(  # noqa: PLR0913 — Timer-closure-safe captures: each arg is an immutable id/path; bundling them would require capturing a stateful object.
    db_path: Path,
    action_id: str,
    run_id: str,
    task_id: str,
    source_event_id: str,
    diff_path_str: str,
    turn_id: str | None = None,
) -> None:
    """Timer callback — opens its OWN sqlite3 connection, emits `worker.reported`, closes.

    Captures only immutable values (paths as `Path` / `str`, ids as
    `str`). No `sqlite3.Connection` or `ActionLifecycle` instance
    crosses the thread boundary, so `check_same_thread=True` (default)
    stays honored.

    Top-level (not nested in `spawn_worker_handler`) so it is unit-testable
    in isolation and so the Timer closure stays minimal.
    """
    # Acceptance B4 readiness: capture the worker thread when the test
    # mode hook is set. The `is not None` check is the only side effect
    # in normal (non-test) operation; the list mutation only happens in
    # tests that explicitly opt in.
    if _TEST_MODE_THREAD_CAPTURE is not None:
        _TEST_MODE_THREAD_CAPTURE.append(threading.current_thread())

    correlation: dict[str, str] = {
        "action_id": action_id,
        "run_id": run_id,
        "task_id": task_id,
    }
    if turn_id is not None:
        correlation["turn_id"] = turn_id

    conn = open_event_log(db_path)
    try:
        emit_event(
            conn,
            type="worker.reported",
            payload={
                "run_id": run_id,
                "action_id": action_id,
                "status": "reported_complete",
                "summary": "codex stub wrote diff artifact",
                "artifact_path": diff_path_str,
            },
            source_event_id=source_event_id,
            correlation=correlation,
        )
    finally:
        conn.close()


def spawn_worker_handler(
    action_request: ActionRequest,
    conn: sqlite3.Connection,
    runtime_paths: RuntimePathsLike,
    lifecycle: ActionLifecycle,  # noqa: ARG001 — kept for signature uniformity; async handlers don't terminal-transition.
) -> RawResult:
    """L2 `spawn_worker` stub — async lifecycle (ADR § Async lifecycle pattern).

    Steps:
        1. Pull `task_id` from `action_request.arguments`.
        2. Mint `run_id = "R" + uuid.uuid4().hex[:8]`.
        3. Emit `run.started(run_id, task_id, runner="codex_stub")` with
           `source_event_id` = the `action.running` event uid (registered
           by `ToolRegistry.dispatch` and stashed on the connection as
           `_jarvis_running_event_uid`; see `dispatch`).
        4. Write `${run_dir}/diff.json` with
           `{"status": _SPAWN_WORKER_ARTIFACT_STATUS, "run_id", "task_id"}`.
        5. Schedule `threading.Timer(_TIMER_DELAY_SECONDS, _emit_worker_reported, ...)`.
        6. Return `RawResult(semantics="ack", ...)`. Lifecycle stays at
           `running`; L3 Result Interpreter (Step 9) transitions to
           terminal once `worker.reported` is observed.

    The controllable artifact knob `_SPAWN_WORKER_ARTIFACT_STATUS` is
    written verbatim into `diff.json["status"]`; tests monkeypatch the
    module-level constant for negative cases.
    """
    task_id = action_request.arguments["task_id"]
    if not isinstance(task_id, str):
        msg = f"spawn_worker: task_id must be a string (got {type(task_id).__name__})"
        raise TypeError(msg)

    run_id = "R" + uuid.uuid4().hex[:8]

    # `ToolRegistry.dispatch` stashed the action.running event_uid here
    # before calling us; that's the canonical source_event_id per the
    # canonical trace (evt 09 source = evt 08).
    running_event_uid = _get_running_event_uid(conn, action_request.action_id)

    emit_event(
        conn,
        type="run.started",
        payload={"run_id": run_id, "task_id": task_id, "runner": "codex_stub"},
        source_event_id=running_event_uid,
        correlation={
            "action_id": action_request.action_id,
            "run_id": run_id,
            "task_id": task_id,
        },
    )

    run_dir = runtime_paths.artifact_dir_for_run(run_id)
    diff_path = run_dir / "diff.json"
    diff_payload = {
        "status": _SPAWN_WORKER_ARTIFACT_STATUS,
        "run_id": run_id,
        "task_id": task_id,
    }
    diff_path.write_text(json.dumps(diff_payload, sort_keys=True), encoding="utf-8")

    db_path = runtime_paths.event_log
    timer = threading.Timer(
        _TIMER_DELAY_SECONDS,
        _emit_worker_reported,
        args=(
            db_path,
            action_request.action_id,
            run_id,
            task_id,
            running_event_uid,
            str(diff_path),
            action_request.turn_id,
        ),
    )
    # `daemon=True` so a test that forgets to join doesn't hang the suite.
    timer.daemon = True
    timer.start()

    payload: dict[str, Any] = {"run_id": run_id, "status": "dispatched"}
    return RawResult(
        action_id=action_request.action_id,
        semantics="ack",
        payload=payload,
        tool_output=tool_result(payload),
        error=None,
    )


def verify_diff_handler(
    action_request: ActionRequest,
    conn: sqlite3.Connection,
    runtime_paths: RuntimePathsLike,
    lifecycle: ActionLifecycle,
) -> RawResult:
    """L0 `verify_diff` stub — sync lifecycle (ADR § Verification is real predicate).

    Reads the artifact `${run_dir}/diff.json`, checks `data["status"] == "ok"`,
    emits the appropriate `action.result_observed` event, transitions
    lifecycle `running → result_observed`, and returns the `RawResult`.

    Cross-task isolation (Finding 1, follow-up to Step 6):
        When `action_request.target_entity_ref` is non-None, the artifact's
        recorded `task_id` MUST match. Without this check an LLM-supplied
        (or hallucinated) `run_id` could point to a different task's
        successful diff and produce a false `verified_complete` on the
        active task (spec §3.4.11 violation: "verification must verify
        *this* action's postcondition").

    Five outcomes (ordered: structural before predicate):

    - Artifact missing → `semantics="error"`, `error="artifact_missing"`.
    - Artifact lacks a `task_id` field while `target_entity_ref` is set →
      `semantics="error"`, `error="artifact_missing_task_id"`. Day-1
      hardness call: artifacts that don't declare their owner are not
      trustworthy targets.
    - Artifact `task_id` mismatches `target_entity_ref` →
      `semantics="error"`, `error="cross_task_artifact"`. This is the
      C5 evidence-binding defense.
    - Predicate matches → `semantics="verification"` with `content_hash`
      and `predicate` in the payload.
    - Predicate fails → `semantics="error"`, `error="predicate_failed"`.

    When `target_entity_ref` is None (defensive path): skip the task_id
    check — cannot validate without a target. Day-1 callers always pass a
    target; this branch exists only for defense.

    Error context fields (`expected_task_id`, `actual_task_id`,
    `artifact_path`, `predicate`, `run_id`) are carried inside the
    `tool_output` JSON string AND nested under a single `error_payload`
    key on the emitted `action.result_observed` event. `error_payload`
    is the registered `optional_payload` entry for these per-error-class
    fields (see `jarvis.state.event_log` registry entry for
    `action.result_observed`).
    """
    run_id = action_request.arguments["run_id"]
    if not isinstance(run_id, str):
        msg = f"verify_diff: run_id must be a string (got {type(run_id).__name__})"
        raise TypeError(msg)

    diff_path = runtime_paths.artifact_dir_for_run(run_id) / "diff.json"
    running_event_uid = _get_running_event_uid(conn, action_request.action_id)

    if not diff_path.exists():
        return _verify_diff_emit_error(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_request.action_id,
            source_event_id=running_event_uid,
            run_id=run_id,
            payload={
                "artifact_path": str(diff_path),
                "predicate": "status==ok",
            },
            error_code="artifact_missing",
        )

    raw_bytes = diff_path.read_bytes()
    content_hash = hashlib.sha256(raw_bytes).hexdigest()
    data = json.loads(raw_bytes.decode("utf-8"))
    actual_status = data.get("status") if isinstance(data, dict) else None
    actual_task_id = data.get("task_id") if isinstance(data, dict) else None
    expected_task_id = action_request.target_entity_ref

    # Cross-task artifact isolation: validate BEFORE predicate evaluation.
    # When the caller declared a target_entity_ref (Day-1 always does), the
    # artifact's recorded task_id must match. A None target_entity_ref is the
    # defensive branch — cannot validate without a target.
    if expected_task_id is not None:
        if not isinstance(data, dict) or "task_id" not in data:
            return _verify_diff_emit_error(
                conn=conn,
                lifecycle=lifecycle,
                action_id=action_request.action_id,
                source_event_id=running_event_uid,
                run_id=run_id,
                payload={
                    "artifact_path": str(diff_path),
                    "predicate": "status==ok",
                    "expected_task_id": expected_task_id,
                    "actual_task_id": None,
                },
                error_code="artifact_missing_task_id",
            )
        if actual_task_id != expected_task_id:
            return _verify_diff_emit_error(
                conn=conn,
                lifecycle=lifecycle,
                action_id=action_request.action_id,
                source_event_id=running_event_uid,
                run_id=run_id,
                payload={
                    "artifact_path": str(diff_path),
                    "predicate": "status==ok",
                    "expected_task_id": expected_task_id,
                    "actual_task_id": actual_task_id,
                },
                error_code="cross_task_artifact",
            )

    if actual_status == "ok":
        verification_payload: dict[str, Any] = {
            "artifact_path": str(diff_path),
            "content_hash": content_hash,
            "predicate": "status==ok",
        }
        emit_event(
            conn,
            type="action.result_observed",
            payload={
                "action_id": action_request.action_id,
                "semantics": "verification",
                "tool_output": tool_result(verification_payload),
                "run_id": run_id,
            },
            source_event_id=running_event_uid,
            correlation={"action_id": action_request.action_id, "run_id": run_id},
        )
        lifecycle.transition(action_request.action_id, "result_observed")
        return RawResult(
            action_id=action_request.action_id,
            semantics="verification",
            payload=verification_payload,
            tool_output=tool_result(verification_payload),
            error=None,
        )

    return _verify_diff_emit_error(
        conn=conn,
        lifecycle=lifecycle,
        action_id=action_request.action_id,
        source_event_id=running_event_uid,
        run_id=run_id,
        payload={
            "artifact_path": str(diff_path),
            "actual_status": actual_status,
            "predicate": "status==ok",
        },
        error_code="predicate_failed",
    )


def _verify_diff_emit_error(  # noqa: PLR0913 — all kwargs are part of the canonical error-event shape.
    *,
    conn: sqlite3.Connection,
    lifecycle: ActionLifecycle,
    action_id: str,
    source_event_id: str,
    run_id: str,
    payload: Mapping[str, Any],
    error_code: str,
) -> RawResult:
    """Emit an `action.result_observed(semantics=error)` and return a RawResult.

    Single point of error-path construction for `verify_diff_handler` —
    all four error branches (`artifact_missing`, `artifact_missing_task_id`,
    `cross_task_artifact`, `predicate_failed`) share this code path, so
    the event shape, lifecycle transition, and `RawResult.error` tag
    stay consistent.

    For the task-isolation error codes (`cross_task_artifact` and
    `artifact_missing_task_id`) the per-error context fields
    (`expected_task_id`, `actual_task_id`, `artifact_path`, `predicate`)
    are nested into the emitted event under a single `error_payload`
    key. `error_payload` is registered as an `optional_payload` entry
    on `action.result_observed`, so a future strict-mode registry flip
    keeps these error events legal.
    """
    tool_output = tool_error(error_code, code=error_code, **dict(payload))
    event_payload: dict[str, Any] = {
        "action_id": action_id,
        "semantics": "error",
        "tool_output": tool_output,
        "error": error_code,
        "run_id": run_id,
    }
    if error_code in {"cross_task_artifact", "artifact_missing_task_id"}:
        event_payload["error_payload"] = dict(payload)
    emit_event(
        conn,
        type="action.result_observed",
        payload=event_payload,
        source_event_id=source_event_id,
        correlation={"action_id": action_id, "run_id": run_id},
    )
    lifecycle.transition(action_id, "result_observed")
    return RawResult(
        action_id=action_id,
        semantics="error",
        payload=payload,
        tool_output=tool_output,
        error=error_code,
    )


# --- ToolRegistry ------------------------------------------------------------


class ToolRegistry:
    """L4 dispatch surface (ADR § Module map L4 row).

    Owns:
        - The `name → ToolDefinition` table.
        - `for_caller(...)` — filtered list the L3 system-prompt assembler
          renders into the LLM tool list. Per-caller filtering is the
          Day-1 mechanism for caller_scope (`{JARVIS_LLM}` for
          spawn_worker; `{JARVIS_LLM, OBSERVER}` for verify_diff).
        - `dispatch(...)` — the L3-facing entry point. Validates caller,
          lifecycle precondition, emits `action.dispatched` +
          `action.running`, calls the handler, returns its `RawResult`.

    Owns NOT:
        - Lifecycle FSM mutation outside `authorized → dispatched → running`
          (the handler is responsible for the terminal transition on sync
          tools; async tools leave lifecycle at `running`).
        - Event emission outside the two dispatch events; the handler
          emits run.started / worker.reported / action.result_observed as
          its tool semantics dictate.

    Thread-safe by `threading.RLock` around the internal dict; same
    rationale as `ActionLifecycle`.
    """

    def __init__(self) -> None:
        """Construct an empty registry (no tools yet)."""
        self._tools: dict[str, ToolDefinition] = {}
        self._lock = threading.RLock()

    def register(self, tool_def: ToolDefinition) -> None:
        """Register a ToolDefinition. Raises DuplicateToolError on re-register.

        Unlike legacy `tools_v2/registry.py` (which logged and overwrote),
        Day-1 raises so a double-register due to a misconfigured
        composition root is caught at runtime.
        """
        with self._lock:
            if tool_def.name in self._tools:
                msg = f"tool {tool_def.name!r} is already registered"
                raise DuplicateToolError(msg)
            self._tools[tool_def.name] = tool_def

    def get_definitions(self) -> tuple[ToolDefinition, ...]:
        """Return every registered ToolDefinition, in registration order."""
        with self._lock:
            return tuple(self._tools.values())

    def for_caller(self, caller_principal: CallerPrincipal) -> tuple[ToolDefinition, ...]:
        """Return only the tools `caller_principal` is allowed to dispatch.

        Used by L3 to assemble the LLM tool list — e.g. JARVIS_LLM sees
        both `spawn_worker` and `verify_diff`; an OBSERVER caller sees
        only `verify_diff`.
        """
        with self._lock:
            return tuple(
                t for t in self._tools.values() if caller_principal in t.allowed_callers
            )

    def dispatch(
        self,
        action_request: ActionRequest,
        conn: sqlite3.Connection,
        runtime_paths: RuntimePathsLike,
        lifecycle: ActionLifecycle,
    ) -> RawResult:
        """Dispatch one ActionRequest.

        Preconditions:
            - `action_request.tool_name` is registered → else `UnknownToolError`.
            - `action_request.caller_principal` is in `tool_def.allowed_callers`
              → else `CallerNotAllowedError`.
            - `lifecycle.state_of(action_id) == "authorized"` → else
              `IllegalLifecycleTransition` (the caller, L3, is expected
              to `register` the action and transition `proposed →
              authorized` before this call).

        On precondition pass, emits:
            1. `action.dispatched(action_id)` → lifecycle authorized →
               dispatched. The event_uid is the `source_event_id` of
               the next event.
            2. `action.running(action_id)` → lifecycle dispatched →
               running. The event_uid is stashed on `conn` under
               `_jarvis_running_event_uid` so the handler can use it as
               `source_event_id` for `run.started` /
               `action.result_observed`.

        The handler then runs and returns a `RawResult`. The handler is
        responsible for any further lifecycle transitions (sync tools
        terminal-transition before returning; async tools leave the
        lifecycle at `running`).
        """
        with self._lock:
            tool_def = self._tools.get(action_request.tool_name)
        if tool_def is None:
            msg = f"unknown tool: {action_request.tool_name!r}"
            raise UnknownToolError(msg)

        if action_request.caller_principal not in tool_def.allowed_callers:
            allowed_names = sorted(c.value for c in tool_def.allowed_callers)
            msg = (
                f"tool {tool_def.name!r} is not callable from "
                f"caller_principal={action_request.caller_principal.value!r}. "
                f"Allowed: {allowed_names!r}"
            )
            raise CallerNotAllowedError(msg)

        current = lifecycle.state_of(action_request.action_id)
        if current != "authorized":
            msg = (
                f"dispatch precondition not met for action_id="
                f"{action_request.action_id!r}: lifecycle must be 'authorized' "
                f"at dispatch (current={current!r})"
            )
            raise IllegalLifecycleTransition(msg)

        dispatched_event = emit_event(
            conn,
            type="action.dispatched",
            payload={"action_id": action_request.action_id},
            correlation=_action_correlation(action_request),
        )
        lifecycle.transition(action_request.action_id, "dispatched")

        running_event = emit_event(
            conn,
            type="action.running",
            payload={"action_id": action_request.action_id},
            source_event_id=dispatched_event.event_uid,
            correlation=_action_correlation(action_request),
        )
        lifecycle.transition(action_request.action_id, "running")

        # Stash the running event_uid on the connection so the handler
        # can use it as the source_event_id for run.started /
        # action.result_observed. Done via attribute set (not a
        # contextvar / threadlocal) because the conn is the single
        # synchronous handoff point between dispatch and the handler.
        _set_running_event_uid(conn, action_request.action_id, running_event.event_uid)
        try:
            return tool_def.handler(action_request, conn, runtime_paths, lifecycle)
        finally:
            _clear_running_event_uid(conn, action_request.action_id)


# --- running_event_uid handoff (dispatcher → handler) -----------------------
#
# The handlers need to know the `event_uid` of the `action.running` event
# the dispatcher just emitted, so they can set it as `source_event_id` on
# the events they emit next (`run.started`, `action.result_observed`).
#
# We cannot stash this on `sqlite3.Connection` (it forbids arbitrary
# attribute assignment), and we cannot widen the ToolHandler signature
# (the public contract is fixed). Instead, the dispatcher records the
# uid in a module-level dict keyed by `action_id`, guarded by a Lock,
# and clears the entry in a `try/finally`. Per-`action_id` keying is
# safe because Day-1 each `action_id` is unique and only one dispatch
# is in flight per `action_id` at a time (the lifecycle FSM rejects
# re-dispatch).

_RUNNING_UID_LOCK: Final[threading.Lock] = threading.Lock()
_RUNNING_UID_TABLE: Final[dict[str, str]] = {}


def _set_running_event_uid(
    conn: sqlite3.Connection,  # noqa: ARG001 — `conn` kept for API symmetry / future per-conn keying.
    action_id: str,
    event_uid: str,
) -> None:
    """Stash the action.running event_uid for the handler to pick up."""
    with _RUNNING_UID_LOCK:
        _RUNNING_UID_TABLE[action_id] = event_uid


def _get_running_event_uid(
    conn: sqlite3.Connection,  # noqa: ARG001 — see `_set_running_event_uid`.
    action_id: str,
) -> str:
    """Retrieve the action.running event_uid stashed by the dispatcher."""
    with _RUNNING_UID_LOCK:
        if action_id not in _RUNNING_UID_TABLE:
            msg = (
                f"running_event_uid for action_id={action_id!r} was not stashed; "
                "handler must be called through ToolRegistry.dispatch"
            )
            raise IllegalLifecycleTransition(msg)
        return _RUNNING_UID_TABLE[action_id]


def _clear_running_event_uid(
    conn: sqlite3.Connection,  # noqa: ARG001 — see `_set_running_event_uid`.
    action_id: str,
) -> None:
    """Drop the stash entry once the handler returns."""
    with _RUNNING_UID_LOCK:
        _RUNNING_UID_TABLE.pop(action_id, None)


def _action_correlation(action_request: ActionRequest) -> dict[str, str]:
    """Build the canonical `{action_id, run_id?, turn_id?}` correlation mapping."""
    out: dict[str, str] = {"action_id": action_request.action_id}
    if action_request.run_id is not None:
        out["run_id"] = action_request.run_id
    if action_request.turn_id is not None:
        out["turn_id"] = action_request.turn_id
    return out


# --- Default registry assembly ----------------------------------------------

_SPAWN_WORKER_INPUT_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "task_id": {
            "type": "string",
            "description": "task to spawn worker for",
        },
    },
    "required": ["task_id"],
}

_VERIFY_DIFF_INPUT_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "run_id": {
            "type": "string",
            "description": "run whose diff.json to verify",
        },
    },
    "required": ["run_id"],
}


def build_default_registry() -> ToolRegistry:
    """Assemble the Day-1 ToolRegistry (`spawn_worker` + `verify_diff`).

    The composition root (`jarvis.runtime`, Step 10) calls this once at
    startup and passes the registry to L3 + L4.
    """
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="spawn_worker",
            description=(
                "Spawn a worker (codex stub Day-1) for the given task_id; "
                "writes diff.json artifact and reports completion asynchronously."
            ),
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L2",
            result_semantics="ack",
            is_async=True,
            input_schema=_SPAWN_WORKER_INPUT_SCHEMA,
            handler=spawn_worker_handler,
        )
    )
    registry.register(
        ToolDefinition(
            name="verify_diff",
            description=(
                "Verify a worker's diff.json artifact against a predicate; "
                "returns verification semantics on match, error on miss."
            ),
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM, CallerPrincipal.OBSERVER}),
            risk_level="L0",
            result_semantics="verification",
            is_async=False,
            input_schema=_VERIFY_DIFF_INPUT_SCHEMA,
            handler=verify_diff_handler,
        )
    )
    return registry


__all__ = [
    "ActionLifecycle",
    "CallerNotAllowedError",
    "DuplicateToolError",
    "IllegalLifecycleTransition",
    "LifecycleState",
    "RawResult",
    "ResultSemantics",
    "RuntimePathsLike",
    "ToolDefinition",
    "ToolRegistry",
    "ToolRegistryError",
    "UnknownToolError",
    "build_default_registry",
    "spawn_worker_handler",
    "tool_error",
    "tool_result",
    "verify_diff_handler",
]
