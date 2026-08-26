"""L4 Capability Execution — ToolRegistry + ActionLifecycle + handlers.

This module is the SINGLE place in `jarvis/` allowed to:

- dispatch L4 tool handlers,
- write artifact files into `${runtime_paths.artifacts_root}/run_<run_id>/`.

Per spec.html §3.4 (`ActionRequest` / lifecycle / ToolRegistry) +
spec.html §3.5 (CallerPrincipal scoping) + ADR 0001 § Stub strategy L4
rows + § Canonical event trace evt 04..21 + § Acceptance B (lifecycle).

Day-2 (post Step 10) ships three tools:

- `spawn_worker` — ASYNC-shaped L2 task action backed by a synchronous
  Codex turn. The dispatcher emits `action.dispatched` → `action.running`;
  the handler then runs the full Codex flow (pre-flight version check,
  dirty-tree stash, `run_codex_action`, heartbeat loop, diff capture,
  artifact persistence, `worker.*` event emission) and returns a
  `RawResult(semantics="ack")`. The handler emits `worker.reported`
  itself, on the SAME thread, but the lifecycle is intentionally left
  at `running`. L3's `_handle_worker_reported` (the Day-1 re-entry
  branch) consumes the `worker.reported` event on the next decide()
  cycle and transitions lifecycle `running → result_observed` via the
  synthetic-RawResult path — that flow is unchanged from Day-1, so
  Day-2 keeps the same `result_semantics="ack"` on the ToolDefinition.
- `verify_diff` — SYNC L0 read. Reads the artifact, checks predicate
  `data["status"] == "ok"`, emits `action.result_observed` with
  `semantics="verification"` (match) or `semantics="error"` (miss),
  transitioning lifecycle `running → result_observed`.
- `create_task` — SYNC L1. Records `task.created` with optional
  `verify_command` auto-detection (Step 4).

`tool_result` / `tool_error` JSON serializers are adapted verbatim from
`/Users/alllllenshi/Projects/jarvis-legacy/tools_v2/helpers.py` per
ADR § Reference sources.

`RuntimePaths` is consumed via a structural `RuntimePathsLike` Protocol
(see below) so this module does NOT import `jarvis.deployment` — that
would violate the `.importlinter` middle-layer sibling rule (execution
and deployment are independent siblings in `decision | execution |
surface | deployment`). `jarvis.runtime` (the composition root, Step 17)
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
For async-shaped handlers (`spawn_worker`) the lifecycle is left at
`running` and L3's `_handle_worker_reported` branch transitions it
terminal once it folds the worker.reported event into a synthetic
RawResult.

Dirty-tree stash ordering (ADR-0002 § Dirty-tree policy, lines 663-713)
=====================================================================

`spawn_worker_handler` calls `isolate_pretask_changes` BEFORE Codex
runs and forwards the resulting `stash_ref` on the returned
`RawResult.metadata["stash_ref"]`. It MUST NOT call
`restore_pretask_changes` itself — the runtime composition (Step 17)
pops the stash AFTER `verify_diff_handler` exits, so the verify path's
working tree reflects only Codex's edits. Canary
`test_canary_stash_pop_after_verify` AST-scans this file to enforce
the no-call invariant.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

from jarvis.execution.codex_action import (
    CodexActionResult,
    CodexVersionTooLowError,
    ensure_codex_version_supported,
    run_codex_action,
)
from jarvis.execution.diff_capture import (
    isolate_pretask_changes,
    write_diff_artifact,
)
from jarvis.execution.path_resolver import TargetKind, load_file_targets_config
from jarvis.execution.path_resolver import resolve as resolve_path_target
from jarvis.execution.verify_command_detect import detect_verify_command
from jarvis.shared import (
    ActionRequest,
    CallerPrincipal,
    RawResult,
    RawResultBundle,
    ResultSemantics,
    RiskLevel,
)
from jarvis.state.event_log import emit_event, iter_events
from jarvis.state.projections import make_snapshot

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping


LOGGER = logging.getLogger(__name__)


# --- Codex execution defaults ------------------------------------------------

# Codex executor identity emitted on `task.executor_assigned`. Day-2 Mac-only
# flagship targets `codex` (the OpenAI Codex app-server CLI) running the
# `gpt-5.5` model per ADR-0002 § Codex contract. The literals appear on
# events so trace consumers can filter by executor / model without crossing
# back into L4.
_CODEX_EXECUTOR_NAME: Final[str] = "codex"
_CODEX_DEFAULT_MODEL: Final[str] = "gpt-5.5"

# Per-turn Codex wall-clock budget (spec §3.4.8 timeout_policy backstop —
# the synchronous driver deadline in `run_codex_action`, NOT the absent
# supervisor sweep over `result_expected_by`). Defaults to 600s (the
# canonical "Codex 10-min turn timeout"); `JARVIS_CODEX_TURN_TIMEOUT_S`
# overrides it so the J9 timeout lifecycle is exercisable live in seconds
# (and as an operator turn-length cap). Read at dispatch time, not import,
# so a per-run override takes effect without re-importing the module.
_CODEX_TURN_TIMEOUT_ENV: Final[str] = "JARVIS_CODEX_TURN_TIMEOUT_S"
_CODEX_TURN_TIMEOUT_DEFAULT_S: Final[float] = 600.0

# ADR-0009 D4 — grace added to a tool's own budget when the dispatcher
# stamps `action.dispatched.result_expected_by_ms`. 100s is not a taste
# call: 600s (Codex) + 100s = 700s = `supervisor.default_budget_s`, the
# anchor the sweep falls back to for rows that predate the stamp. Keeping
# them equal means a stamped row and a legacy row of the same age are
# judged overdue at the same instant, so the fallback ladder is a true
# reconstruction of the stamp rather than a second, disagreeing policy.
# It also leaves the in-process driver deadline (600s) a clear 100s head
# start to emit its own terminal event before the supervisor assumes the
# worker is gone — the sweep is the backstop, not the primary closer.
_DISPATCH_DEADLINE_GRACE_S: Final[float] = 100.0


def _resolve_codex_turn_timeout_s() -> float:
    """Return the per-turn Codex budget from the env override or the default.

    A malformed ``JARVIS_CODEX_TURN_TIMEOUT_S`` falls back to the 600s
    default rather than crashing the worker spawn — a bad ops knob must
    not turn every task into an ``action.failed``.
    """
    raw = os.environ.get(_CODEX_TURN_TIMEOUT_ENV)
    if raw is None:
        return _CODEX_TURN_TIMEOUT_DEFAULT_S
    try:
        return float(raw)
    except ValueError:
        LOGGER.warning(
            "%s=%r is not a number; using %.0fs default turn budget",
            _CODEX_TURN_TIMEOUT_ENV,
            raw,
            _CODEX_TURN_TIMEOUT_DEFAULT_S,
        )
        return _CODEX_TURN_TIMEOUT_DEFAULT_S


# Per-turn Codex heartbeat cadence (spec §3.5.8 ladder — the idle-poll
# interval at which `run_codex_action` fires `on_heartbeat`, which this
# module turns into `worker.heartbeat` events). Defaults to 30s (the
# canonical "worker still alive" signal); `JARVIS_CODEX_HEARTBEAT_INTERVAL_S`
# lowers it so the J4 heartbeat lifecycle is exercisable live in seconds
# instead of needing a 30s real-Codex turn. Read at dispatch time, not
# import, so a per-run override takes effect without re-importing the module.
_CODEX_HEARTBEAT_INTERVAL_ENV: Final[str] = "JARVIS_CODEX_HEARTBEAT_INTERVAL_S"
_CODEX_HEARTBEAT_INTERVAL_DEFAULT_S: Final[float] = 30.0


def _resolve_codex_heartbeat_interval_s() -> float:
    """Return the heartbeat cadence from the env override or the default.

    A malformed ``JARVIS_CODEX_HEARTBEAT_INTERVAL_S`` falls back to the 30s
    default rather than crashing the worker spawn — a bad ops knob must not
    turn every task into an ``action.failed``.
    """
    raw = os.environ.get(_CODEX_HEARTBEAT_INTERVAL_ENV)
    if raw is None:
        return _CODEX_HEARTBEAT_INTERVAL_DEFAULT_S
    try:
        return float(raw)
    except ValueError:
        LOGGER.warning(
            "%s=%r is not a number; using %.0fs default heartbeat cadence",
            _CODEX_HEARTBEAT_INTERVAL_ENV,
            raw,
            _CODEX_HEARTBEAT_INTERVAL_DEFAULT_S,
        )
        return _CODEX_HEARTBEAT_INTERVAL_DEFAULT_S


# --- Public type aliases -----------------------------------------------------

# `RawResult` and `ResultSemantics` live in `jarvis.shared` since Step 0b of
# ADR-0002 (so L3 can import them without crossing the layer DAG). They are
# re-exported via `__all__` below for backward compatibility with Day-1
# callers that imported them from `jarvis.execution.tools`.


class RuntimePathsLike(Protocol):
    """Structural view of `jarvis.deployment.RuntimePaths` used by L4.

    L4 cannot import L6 (sibling layers in `.importlinter`). The composition
    root (`jarvis.runtime`, Step 17) passes a real `RuntimePaths` instance;
    it satisfies this Protocol structurally.

    Surface:
        - ``event_log`` — DB path (still consumed by the dispatcher's
          source-event-uid handshake; Day-2 handlers also walk events
          via :func:`iter_events` to find ``task.created`` records).
        - ``artifacts_root`` — root artifact dir; Day-2 spawn_worker
          passes this to :func:`write_diff_artifact` which builds the
          ``run_<run_id>/diff.txt`` path itself.
        - ``artifact_dir_for_run`` — convenience for sync tools
          (``verify_diff``) that need the per-run dir without going
          through :mod:`diff_capture`.
    """

    @property
    def event_log(self) -> Path:
        """Path to the SQLite Event Log file."""
        ...

    @property
    def artifacts_root(self) -> Path:
        """Root artifact directory; per-run subdirs live underneath."""
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
        RawResult | RawResultBundle,
    ]


@dataclass(frozen=True)
class PostActionCheck:
    """Per-tool inline post_action_check declaration (spec §3.5.7).

    Day-2 uses one variant: the inline ``verify_command`` chained
    predicate on ``verify_diff``. The handler runs an inline subprocess
    after the primary observation slot and decides the chained slot's
    ``result_semantics`` based on ``expected_predicate``. Mismatch slot
    semantics defaults to ``"error"`` per spec §3.4.11.

    Attributes:
        mode: ``"inline"`` — chained in the same L4 invocation. Day-2's
            only supported mode; future modes (e.g. ``"deferred"``) are
            out of scope.
        check_tool: Sentinel name for the inline subprocess; NOT a tool
            reference (no dispatcher round-trip). Day-2 value:
            ``"verify_command"``.
        expected_predicate: Pass/fail expression evaluated against the
            chained result. Day-2 value: ``"exit_code == 0"``. The
            handler reads this string but the comparison logic is
            hard-coded — the field exists for the AST canary and audit
            trail, not for late-binding evaluation.
        result_semantics_on_match: ``ResultSemantics`` tag the chained
            slot carries when ``expected_predicate`` matches. Day-2:
            ``"verification"``. Mismatch path: handler assigns
            ``"error"`` per spec §3.4.11.
        timeout_ms: Per-check wall-clock timeout in milliseconds. Day-2
            default: ``600_000`` (10 minutes).
    """

    mode: Literal["inline"]
    check_tool: str
    expected_predicate: str
    result_semantics_on_match: ResultSemantics
    timeout_ms: int


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
        post_action_check: Optional inline post-action chained check
            (spec §3.5.7). ``None`` for Day-1 / single-slot tools;
            Day-2 ``verify_diff`` declares one so the handler can chain
            an inline ``verify_command`` subprocess into the second
            ``RawResultBundle`` slot.
        result_budget_s: Optional per-tool wall-clock budget, in seconds,
            for the tool to produce a terminal event (ADR-0009 D4). A
            zero-argument callable rather than a float because the Codex
            budget is env-overridable per run
            (``JARVIS_CODEX_TURN_TIMEOUT_S``) and must be read at
            dispatch time, not at registry-build time. ``None`` (the
            default, and every sync tool) means "no declared budget":
            the dispatcher stamps no deadline and the supervisor sweep
            falls back to ``dispatched ts + supervisor.default_budget_s``.
    """

    name: str
    description: str
    allowed_callers: frozenset[CallerPrincipal]
    risk_level: RiskLevel
    result_semantics: ResultSemantics
    is_async: bool
    input_schema: Mapping[str, Any]
    handler: ToolHandler
    post_action_check: PostActionCheck | None = None
    result_budget_s: Callable[[], float] | None = None


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


def _load_task_record(conn: sqlite3.Connection, task_id: str) -> Mapping[str, Any] | None:
    """Return the most recent ``task.created`` payload for ``task_id``, or None.

    L4 is allowed to read L2 (event_log) per `.importlinter`; this walk
    is the L4-internal lookup for ``goal`` and ``repo_path`` that the
    Codex spawn needs. The Task Ledger projection's TaskLedgerRecord
    only carries ``goal``; ``repo_path`` lives on the raw event payload.
    Walking the (small Day-2) event log here keeps the projection's
    public surface minimal and avoids a write to Step 5's projection
    contract from inside a Day-2 build step.
    """
    latest: Mapping[str, Any] | None = None
    for evt in iter_events(conn):
        if evt.type != "task.created":
            continue
        if evt.payload.get("task_id") == task_id:
            latest = evt.payload
    return latest


def _emit_worker_heartbeat_factory(  # noqa: PLR0913 — all kwargs are immutable correlation ids; bundling them defeats the point of a closure factory.
    *,
    conn: sqlite3.Connection,
    action_id: str,
    run_id: str,
    task_id: str,
    source_event_id: str,
    turn_id: str | None,
) -> Callable[[dict[str, Any]], None]:
    """Build the ``on_heartbeat`` closure for :func:`run_codex_action`.

    The closure emits one ``worker.heartbeat`` event per call with the
    payload Codex's poll loop already shapes (``summary``,
    ``elapsed_ms``, ``last_item_summary``). Top-level (not nested in
    the handler) for clarity; the closure captures only immutable
    correlation ids plus the live ``conn`` (handler-thread bound).
    """
    correlation: dict[str, str] = {"action_id": action_id, "run_id": run_id, "task_id": task_id}
    if turn_id is not None:
        correlation["turn_id"] = turn_id

    def _emit(payload: dict[str, Any]) -> None:
        emit_event(
            conn,
            type="worker.heartbeat",
            payload={
                "run_id": run_id,
                "action_id": action_id,
                "elapsed_ms": int(payload.get("elapsed_ms", 0)),
                "last_log_line": str(payload.get("last_item_summary", "")),
                "summary": str(payload.get("summary", "codex turn in progress")),
            },
            source_event_id=source_event_id,
            correlation=correlation,
        )

    return _emit


def _spawn_worker_emit_terminal_failure(  # noqa: PLR0913 — Day-2 failure paths fold seven correlation ids + a typed event-type discriminator; combining them masks the action.failed / action.timeout_assumed split.
    *,
    conn: sqlite3.Connection,
    lifecycle: ActionLifecycle,
    action_id: str,
    task_id: str | None,
    run_id: str | None,
    source_event_id: str,
    error_code: str,
    error_message: str,
    event_type: Literal["action.failed", "action.timeout_assumed"],
    stash_ref: str | None,
    cost: Mapping[str, Any] | None,
    turn_id: str | None,
) -> RawResult:
    """Emit the terminal failure event + transition lifecycle running -> terminal.

    Also emits an end-of-run ``task.executor_reported`` so the Task
    Ledger projection records a failed/timeout run, then assembles a
    ``RawResult(semantics="error")`` with cost + stash_ref metadata
    forwarded so the runtime composition can plumb them (cost recording
    in L3, stash pop in Step 17 -- both unchanged by the failure path).
    """
    correlation: dict[str, str] = {"action_id": action_id}
    if run_id is not None:
        correlation["run_id"] = run_id
    if task_id is not None:
        correlation["task_id"] = task_id
    if turn_id is not None:
        correlation["turn_id"] = turn_id

    emit_event(
        conn,
        type=event_type,
        payload={
            "action_id": action_id,
            "error": error_code,
            "reason": error_message,
        },
        source_event_id=source_event_id,
        correlation=correlation,
    )

    executor_status = "failed" if event_type == "action.failed" else "timeout"
    if task_id is not None and run_id is not None:
        # The Task Ledger projection bridges through run.started for
        # task_id; emit task.executor_reported with a failure status so
        # the projection sees a terminal report on this run.
        emit_event(
            conn,
            type="task.executor_reported",
            payload={
                "task_id": task_id,
                "run_id": run_id,
                "status": executor_status,
                "summary": error_message,
            },
            source_event_id=source_event_id,
            correlation=correlation,
        )
    terminal_state: LifecycleState = (
        "failed" if event_type == "action.failed" else "timeout_assumed"
    )
    if lifecycle.state_of(action_id) == "running":
        lifecycle.transition(action_id, terminal_state)

    metadata: dict[str, Any] = {}
    if cost is not None:
        metadata["cost"] = dict(cost)
    metadata["stash_ref"] = stash_ref

    payload: dict[str, Any] = {
        "run_id": run_id,
        "status": executor_status,
        "error": error_code,
    }
    return RawResult(
        action_id=action_id,
        semantics="error",
        payload=payload,
        tool_output=tool_error(error_message, code=error_code),
        error=error_code,
        metadata=metadata,
    )


def spawn_worker_handler(
    action_request: ActionRequest,
    conn: sqlite3.Connection,
    runtime_paths: RuntimePathsLike,
    lifecycle: ActionLifecycle,
) -> RawResult:
    """L2 ``spawn_worker`` -- real Codex flow (ADR-0002 D-day step 3 + Codex contract).

    Replaces the Day-1 ``threading.Timer`` + hardcoded
    ``{"status":"ok"}`` stub with the full Codex flow:

    1. Pre-flight: :func:`ensure_codex_version_supported` (>= 0.125.0).
       On :class:`CodexVersionTooLowError` -> emit ``action.failed``,
       lifecycle ``running -> failed``, return RawResult(semantics=error).
    2. Mint ``run_id = "R" + uuid.hex[:8]``. Load the task record
       (goal + repo_path) from the Event Log.
    3. Emit ``task.executor_assigned(executor="codex", model="gpt-5.5")``.
    4. Emit ``run.started(runner="codex")``.
    5. Dirty-tree stash via :func:`isolate_pretask_changes` BEFORE
       Codex runs; the resulting ``stash_ref`` (40-char commit SHA or
       ``None``) travels back on ``RawResult.metadata["stash_ref"]``
       for the runtime composition to pop AFTER verify_diff exits
       (ADR-0002 Dirty-tree policy).
    6. :func:`run_codex_action` with an ``on_heartbeat`` closure that
       emits ``worker.heartbeat`` per 30s.
    7. Branch on the :class:`CodexActionResult`:
       - timeout/interrupted -> ``action.timeout_assumed`` +
         ``task.executor_reported(status="timeout")``.
       - crash (any other ``error``) -> ``action.failed`` +
         ``task.executor_reported(status="failed")``.
       - ``submit_report is None`` -> emit ``worker.report_missing``
         (Limitation Claim signal per spec 3.5.8); fall through to a
         degraded ``worker.reported`` with ``status="report_missing"``.
       - Happy path: persist diff via :func:`write_diff_artifact`,
         emit ``worker.artifact_observed`` (content_hash =
         sha256(diff_text)), emit ``worker.reported`` using the
         submit_report payload as authoritative, emit
         ``task.executor_reported``.
    8. Return ``RawResult(semantics="ack", ...)``. Lifecycle is left at
       ``running`` on the happy + report-missing path so L3's
       ``_handle_worker_reported`` re-entry (Day-1 mechanism) folds
       worker.reported into a synthetic RawResult and transitions
       ``running -> result_observed`` on the next decide() cycle.

    ``RawResult.metadata`` carries:
        - ``"cost"`` -- ``{kind, model, tokens_in, tokens_out, run_id}``
          for L3 to emit ``cost.recorded`` (single emit-site per spec
          5.4.1).
        - ``"stash_ref"`` -- stash ref forwarded to runtime composition
          (Step 17) so it can call ``restore_pretask_changes`` AFTER
          verify_diff exits. ADR-0002 Dirty-tree policy: this handler
          MUST NOT call ``restore_pretask_changes`` itself; canary
          ``test_canary_stash_pop_after_verify`` AST-scans the body.
    """
    task_id = action_request.arguments["task_id"]
    if not isinstance(task_id, str):
        msg = f"spawn_worker: task_id must be a string (got {type(task_id).__name__})"
        raise TypeError(msg)

    running_event_uid = _get_running_event_uid(conn, action_request.action_id)

    # 1. Pre-flight: codex --version >= 0.125.0. On failure the dispatcher
    # has already transitioned lifecycle authorized -> dispatched -> running;
    # we close the loop with action.failed + lifecycle running -> failed.
    try:
        ensure_codex_version_supported()
    except CodexVersionTooLowError as exc:
        return _spawn_worker_emit_terminal_failure(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_request.action_id,
            task_id=None,
            run_id=None,
            source_event_id=running_event_uid,
            error_code="codex_version_too_low",
            error_message=str(exc),
            event_type="action.failed",
            stash_ref=None,
            cost=None,
            turn_id=action_request.turn_id,
        )

    # 2. Mint run_id + load task record.
    run_id = "R" + uuid.uuid4().hex[:8]
    task_record = _load_task_record(conn, task_id)
    goal = (
        str(task_record["goal"])
        if task_record is not None and "goal" in task_record
        else task_id
    )
    repo_path_str = (
        str(task_record["repo_path"])
        if task_record is not None and task_record.get("repo_path") is not None
        else None
    )
    repo_path = Path(repo_path_str) if repo_path_str is not None else Path.cwd()

    correlation: dict[str, str] = {
        "action_id": action_request.action_id,
        "run_id": run_id,
        "task_id": task_id,
    }
    if action_request.turn_id is not None:
        correlation["turn_id"] = action_request.turn_id

    # 3. task.executor_assigned (executor=codex, model=gpt-5.5).
    emit_event(
        conn,
        type="task.executor_assigned",
        payload={
            "task_id": task_id,
            "executor": _CODEX_EXECUTOR_NAME,
            "action_id": action_request.action_id,
            "model": _CODEX_DEFAULT_MODEL,
        },
        source_event_id=running_event_uid,
        correlation=correlation,
    )

    # 4. run.started(runner="codex"). The Day-1 "codex_stub" label is retired.
    emit_event(
        conn,
        type="run.started",
        payload={"run_id": run_id, "task_id": task_id, "runner": _CODEX_EXECUTOR_NAME},
        source_event_id=running_event_uid,
        correlation=correlation,
    )

    # 5. Dirty-tree stash BEFORE Codex. The stash_ref is forwarded on
    # RawResult.metadata; the runtime composition (Step 17) pops it
    # AFTER verify_diff exits (ADR-0002 Dirty-tree policy).
    stash_ref = isolate_pretask_changes(repo_path, run_id=run_id)

    # 6. Run Codex with the heartbeat closure.
    on_heartbeat = _emit_worker_heartbeat_factory(
        conn=conn,
        action_id=action_request.action_id,
        run_id=run_id,
        task_id=task_id,
        source_event_id=running_event_uid,
        turn_id=action_request.turn_id,
    )
    # run_codex_action can raise PermissionError / OSError /
    # FileNotFoundError after a08c4a2 (codex_home rmtree-then-raise on
    # seed or client-init failure). Route those into action.failed
    # explicitly — 7a/7b below only handle the `codex_result.error`
    # branch, so an unhandled raise would leave the lifecycle stuck in
    # `running` with zero terminal event on the log. `except Exception`
    # so KeyboardInterrupt / SystemExit still propagate.
    try:
        codex_result: CodexActionResult = run_codex_action(
            task_goal=goal,
            cwd=repo_path,
            timeout_s=_resolve_codex_turn_timeout_s(),
            on_heartbeat=on_heartbeat,
            heartbeat_interval_s=_resolve_codex_heartbeat_interval_s(),
        )
    except Exception as exc:  # noqa: BLE001 — any Codex spawn failure folds into one action.failed.
        return _spawn_worker_emit_terminal_failure(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_request.action_id,
            task_id=task_id,
            run_id=run_id,
            source_event_id=running_event_uid,
            error_code="codex_spawn_failed",
            error_message=f"{type(exc).__name__}: {exc}",
            event_type="action.failed",
            stash_ref=stash_ref,
            cost=None,
            turn_id=action_request.turn_id,
        )
    cost: dict[str, Any] = {
        "kind": _CODEX_EXECUTOR_NAME,
        "model": _CODEX_DEFAULT_MODEL,
        "tokens_in": int(codex_result.tokens_in),
        "tokens_out": int(codex_result.tokens_out),
        "run_id": run_id,
    }

    # 7a. Timeout -- turn/interrupt was issued by the driver. Emit
    # action.timeout_assumed and end here (no worker.reported).
    if codex_result.interrupted or codex_result.error == "codex_turn_timeout":
        return _spawn_worker_emit_terminal_failure(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_request.action_id,
            task_id=task_id,
            run_id=run_id,
            source_event_id=running_event_uid,
            error_code="codex_turn_timeout",
            error_message=codex_result.error or "codex turn timed out",
            event_type="action.timeout_assumed",
            stash_ref=stash_ref,
            cost=cost,
            turn_id=action_request.turn_id,
        )

    # 7b. Crash (initialize / thread/start / turn/start / subprocess). The
    # canonical tags are "codex_initialize_failed", "codex_thread_start_failed",
    # "codex_turn_start_failed", "codex_subprocess_crashed". All map to
    # action.failed with the upstream tag preserved.
    if codex_result.error is not None:
        return _spawn_worker_emit_terminal_failure(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_request.action_id,
            task_id=task_id,
            run_id=run_id,
            source_event_id=running_event_uid,
            error_code=codex_result.error.split(":", 1)[0],
            error_message=codex_result.error,
            event_type="action.failed",
            stash_ref=stash_ref,
            cost=cost,
            turn_id=action_request.turn_id,
        )

    # 8. Happy path (codex completed turn; submit_report may or may not
    # be present). Persist the diff artifact.
    diff_artifact_path = write_diff_artifact(
        codex_result.diff_text,
        artifact_dir=runtime_paths.artifacts_root,
        run_id=run_id,
    )
    content_hash = hashlib.sha256(codex_result.diff_text.encode("utf-8")).hexdigest()

    # 9. worker.artifact_observed for the diff.
    emit_event(
        conn,
        type="worker.artifact_observed",
        payload={
            "run_id": run_id,
            "action_id": action_request.action_id,
            "artifact_path": str(diff_artifact_path),
            "content_hash": content_hash,
            "kind": "diff",
        },
        source_event_id=running_event_uid,
        correlation=correlation,
    )

    # 10. worker.report_missing branch when Codex never called submit_report.
    report_missing = codex_result.submit_report is None
    if report_missing:
        emit_event(
            conn,
            type="worker.report_missing",
            payload={
                "run_id": run_id,
                "action_id": action_request.action_id,
                "reason": "codex completed turn without calling submit_report tool",
            },
            source_event_id=running_event_uid,
            correlation=correlation,
        )

    # 11. worker.reported -- use the submit_report payload as authoritative
    # when present; otherwise emit a degraded report with status=report_missing.
    submit_report = codex_result.submit_report or {}
    report_status = str(submit_report.get("status", "report_missing"))
    submit_summary = submit_report.get("summary")
    if isinstance(submit_summary, str) and submit_summary:
        report_summary = submit_summary
    elif report_missing:
        report_summary = "(no summary -- submit_report missing)"
    else:
        report_summary = ""
    emit_event(
        conn,
        type="worker.reported",
        payload={
            "run_id": run_id,
            "action_id": action_request.action_id,
            "status": report_status,
            "summary": report_summary,
            "artifact_path": str(diff_artifact_path),
            "stash_ref": stash_ref,
        },
        source_event_id=running_event_uid,
        correlation=correlation,
    )

    # 12. task.executor_reported -- projection-side terminal signal for
    # this run.
    emit_event(
        conn,
        type="task.executor_reported",
        payload={
            "task_id": task_id,
            "run_id": run_id,
            "status": report_status,
            "summary": report_summary,
            "diff_path": str(diff_artifact_path),
        },
        source_event_id=running_event_uid,
        correlation=correlation,
    )

    # 13. Build the RawResult. Lifecycle stays at `running` per Day-1's
    # async-shape pattern -- L3's _handle_worker_reported branch transitions
    # to result_observed once it consumes the worker.reported row.
    _ = lifecycle  # async-shape: lifecycle terminal-transition is L3's job.
    payload: dict[str, Any] = {
        "run_id": run_id,
        "status": report_status,
        "summary": report_summary,
        "artifact_path": str(diff_artifact_path),
        "stash_ref": stash_ref,
    }
    metadata: dict[str, Any] = {
        "cost": cost,
        "stash_ref": stash_ref,
    }
    return RawResult(
        action_id=action_request.action_id,
        semantics="ack",
        payload=payload,
        tool_output=tool_result(payload),
        error=None,
        metadata=metadata,
    )


_VERIFY_COMMAND_TIMEOUT_S: Final[float] = 600.0
"""Per-check wall-clock timeout (seconds) for the inline verify_command
subprocess. Mirrors :attr:`PostActionCheck.timeout_ms` (600_000ms) on
:data:`VERIFY_DIFF_TOOL_DEF`. Module-level so unit tests can patch it
without monkeypatching the standard library.
"""

_VERIFY_COMMAND_TIMEOUT_EXIT_CODE: Final[int] = 124
"""Convention: exit_code=124 maps to GNU ``timeout(1)``'s timeout
exit. The handler synthesizes this when ``subprocess.TimeoutExpired``
fires so the chained slot's payload stays uniform with the happy /
non-zero paths.
"""

_OUTPUT_TAIL_BYTES: Final[int] = 2048
"""Maximum stdout/stderr tail to retain on the chained slot. Keeps the
event payload bounded; the full output would balloon the SQLite row
for `npm test`-style verify commands.
"""

_DIFF_PREVIEW_BYTES: Final[int] = 500
"""Maximum prefix of diff text to embed on the observation slot.
The full diff lives at ``artifact_ref``; the preview is for the LLM
context window only.
"""


def verify_diff_handler(
    action_request: ActionRequest,
    conn: sqlite3.Connection,
    runtime_paths: RuntimePathsLike,
    lifecycle: ActionLifecycle,
) -> RawResultBundle:
    """L4 observation handler with inline post_action_check chain.

    Per ADR-0002 § Verify_diff contract (lines 763-913, spec §3.4.11 +
    §3.5.7). Returns a :class:`RawResultBundle` with one or two slots:

    Slot 1 (mandatory, ``result_semantics="observation"``)
        The diff artifact capture — read the diff text from the path
        on ``action_request.arguments["artifact_path"]`` (Day-2 L3
        plumbs this from the worker's run); fall back to
        ``runtime_paths.artifact_dir_for_run(run_id) / "diff.txt"``
        when only ``run_id`` is supplied. The slot's payload carries
        a bounded ``diff_text_preview`` plus a ``diff_nonempty`` flag
        and the absolute ``artifact_ref``. NO ``git apply --check``:
        Codex already applied the patch in place upstream
        (``spawn_worker``).
    Slot 2 (conditional, ``result_semantics="verification" | "error"``)
        Only present when ``action_request.payload`` carries a
        non-None ``verify_command`` string (L3 reads this from the
        Task Ledger projection at Step 12). The handler runs the
        command via ``subprocess.run(["/bin/sh", "-c", cmd],
        cwd=repo_path, timeout=600, ...)`` and assigns the slot's
        semantics based on the predicate ``exit_code == 0`` from
        ``post_action_check.expected_predicate``. Non-zero exit / timeout
        → ``"error"`` per spec §3.4.11. Timeout uses exit_code=124 by
        convention (matches GNU ``timeout(1)``) and sets
        ``timed_out=True`` on the payload.

    Trust model: ``verify_command`` is Allen-authored at ``task.created``
    time (D-1 session) via the natural-language ``create_task`` path;
    Day-2 has no untrusted ingest path. ``/bin/sh -c`` is therefore
    trusted-by-Allen, the same class as the repo's local test command
    (ADR § Trust model).

    Stash-pop ordering (CRITICAL, § Dirty-tree policy lines 663-713):
    this handler runs with ``cwd=repo_path`` and the ``verify_command``
    subprocess therefore reads the working tree exactly as Codex left
    it. The runtime composition (Step 17) MUST pop the pre-task stash
    AFTER this handler returns; calling early would layer Allen's
    pre-task changes back onto the verify cwd mid-check and pollute
    the exit code. Static guarantee: ``test_canary_stash_pop_after_verify``.

    NO reviewer call inside this handler (L3 / Step 12 owns
    :func:`jarvis.decision.reviewer.review_diff`). NO ``git apply
    --check`` (Codex applied in place). NO ``restore_pretask_changes``
    (runtime composition's job per Step 17).

    Lifecycle terminal transition (``running -> result_observed``) is
    emitted INSIDE this handler so the L4 sync-handler invariant
    (`spec § Acceptance B`) still holds. The transition fires once the
    bundle is fully built, just before return; this means a single
    ``action.result_observed`` event is emitted here per slot pair,
    carrying the slot 1 semantics for backward compat; Step 12's L3
    Result Interpreter will fan out into one event per slot.
    """
    diff_path = _resolve_diff_artifact_path(
        action_request=action_request,
        runtime_paths=runtime_paths,
    )
    running_event_uid = _get_running_event_uid(conn, action_request.action_id)

    observation_slot = _build_observation_slot(
        action_id=action_request.action_id,
        diff_path=diff_path,
    )

    verify_command_raw = (
        action_request.payload.get("verify_command")
        if action_request.payload is not None
        else None
    )
    verify_command = verify_command_raw if isinstance(verify_command_raw, str) else None

    if verify_command is None:
        _emit_verify_diff_result_event(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_request.action_id,
            source_event_id=running_event_uid,
            slot=observation_slot,
        )
        return RawResultBundle(slots=(observation_slot,))

    repo_path = _resolve_repo_path(action_request)
    verification_slot = _build_verification_slot(
        action_id=action_request.action_id,
        verify_command=verify_command,
        repo_path=repo_path,
    )
    _emit_verify_diff_result_event(
        conn=conn,
        lifecycle=lifecycle,
        action_id=action_request.action_id,
        source_event_id=running_event_uid,
        slot=observation_slot,
    )
    return RawResultBundle(slots=(observation_slot, verification_slot))


def _resolve_diff_artifact_path(
    *,
    action_request: ActionRequest,
    runtime_paths: RuntimePathsLike,
) -> Path:
    """Pick the diff artifact path from ``arguments``.

    Preference order:
        1. ``arguments["artifact_path"]`` (Day-2 L3 plumbing) — used as-is.
        2. ``arguments["run_id"]`` → ``runtime_paths.artifact_dir_for_run(
           run_id) / "diff.txt"`` (legacy / unit-test path; matches
           spawn_worker's :func:`write_diff_artifact` output).

    Raises ``KeyError`` only if neither is present (the LLM tool schema
    declares ``run_id`` required at Day-2 registration, so this is
    defense-in-depth).
    """
    args = action_request.arguments
    artifact_path_arg = args.get("artifact_path")
    if isinstance(artifact_path_arg, str) and artifact_path_arg:
        return Path(artifact_path_arg)
    run_id = args.get("run_id")
    if isinstance(run_id, str) and run_id:
        return runtime_paths.artifact_dir_for_run(run_id) / "diff.txt"
    msg = (
        "verify_diff: arguments must carry either 'artifact_path' (Day-2) "
        "or 'run_id' (legacy)"
    )
    raise KeyError(msg)


def _resolve_repo_path(action_request: ActionRequest) -> Path:
    """Return the repo cwd for the verify_command subprocess.

    Order: ``action_request.payload["repo_path"]`` (preferred — L3 plumbs
    it from the Task Ledger), then ``arguments["repo_path"]`` (test /
    direct call), then :func:`Path.cwd`. The cwd fallback keeps Day-2
    unit tests using ``tmp_path`` for the artifact while letting the
    verify_command runs in the same dir succeed.
    """
    payload_repo = (
        action_request.payload.get("repo_path")
        if action_request.payload is not None
        else None
    )
    if isinstance(payload_repo, str) and payload_repo:
        return Path(payload_repo)
    args_repo = action_request.arguments.get("repo_path")
    if isinstance(args_repo, str) and args_repo:
        return Path(args_repo)
    return Path.cwd()


def _build_observation_slot(*, action_id: str, diff_path: Path) -> RawResult:
    """Read the diff artifact and shape slot 1 (``observation``).

    Missing files fall through to ``diff_text=""`` /
    ``diff_nonempty=False`` rather than raising — the spec treats a
    no-op diff as a real Day-2 outcome (Codex completed but did not
    edit), not an artifact-missing error. The C5 cross-task isolation
    check from Day-1 is intentionally dropped (Day-2 L3 owns the
    artifact-binding from spawn_worker's run_id correlation).
    """
    diff_text = diff_path.read_text(encoding="utf-8") if diff_path.exists() else ""
    diff_nonempty = bool(diff_text.strip())
    content_hash = hashlib.sha256(diff_text.encode("utf-8")).hexdigest()
    payload: dict[str, Any] = {
        "diff_text_preview": diff_text[:_DIFF_PREVIEW_BYTES],
        "diff_nonempty": diff_nonempty,
        "artifact_path": str(diff_path),
        "artifact_ref": str(diff_path),
        "content_hash": content_hash,
    }
    return RawResult(
        action_id=action_id,
        semantics="observation",
        payload=payload,
        tool_output=tool_result(
            diff_path=str(diff_path),
            diff_nonempty=diff_nonempty,
            content_hash=content_hash,
        ),
        error=None,
        metadata=None,
    )


def _build_verification_slot(
    *,
    action_id: str,
    verify_command: str,
    repo_path: Path,
) -> RawResult:
    """Run ``verify_command`` inline and shape slot 2.

    ``cwd=repo_path`` MUST stay as Codex left the tree (see § Dirty-tree
    policy stash-pop ordering on :func:`verify_diff_handler`). The
    predicate ``exit_code == 0`` from :class:`PostActionCheck` is
    evaluated HERE — the handler picks ``"verification"`` on match,
    ``"error"`` otherwise.

    Timeout: ``subprocess.TimeoutExpired`` synthesizes
    ``exit_code=124`` (GNU ``timeout(1)`` convention) and
    ``timed_out=True`` on the slot payload; the slot's
    ``RawResult.error`` carries ``"verify_command_timeout"`` for the
    Limitation Claim ladder (spec §3.4.11).
    """
    start_mono = time.monotonic()
    try:
        proc = subprocess.run(  # noqa: S603 — Allen-authored trust class per ADR § Trust model.
            ["/bin/sh", "-c", verify_command],
            cwd=str(repo_path),
            timeout=_VERIFY_COMMAND_TIMEOUT_S,
            capture_output=True,
            text=True,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.monotonic() - start_mono) * 1000)
        stdout_tail = _decode_tail(exc.stdout)
        stderr_tail = "timeout"
        exit_code = _VERIFY_COMMAND_TIMEOUT_EXIT_CODE
        payload: dict[str, Any] = {
            "verify_command": verify_command,
            "exit_code": exit_code,
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
            "duration_ms": duration_ms,
            "timed_out": True,
        }
        return RawResult(
            action_id=action_id,
            semantics="error",
            payload=payload,
            tool_output=tool_result(
                verify_command=verify_command,
                exit_code=exit_code,
                duration_ms=duration_ms,
                timed_out=True,
            ),
            error="verify_command_timeout",
            metadata=None,
        )

    duration_ms = int((time.monotonic() - start_mono) * 1000)
    stdout_tail = (proc.stdout or "")[-_OUTPUT_TAIL_BYTES:]
    stderr_tail = (proc.stderr or "")[-_OUTPUT_TAIL_BYTES:]
    exit_code = proc.returncode
    semantics_on_chain: ResultSemantics = (
        "verification" if exit_code == 0 else "error"
    )
    payload = {
        "verify_command": verify_command,
        "exit_code": exit_code,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
        "duration_ms": duration_ms,
        "timed_out": False,
    }
    return RawResult(
        action_id=action_id,
        semantics=semantics_on_chain,
        payload=payload,
        tool_output=tool_result(
            verify_command=verify_command,
            exit_code=exit_code,
            duration_ms=duration_ms,
        ),
        error=None if exit_code == 0 else f"verify_command_exit_{exit_code}",
        metadata=None,
    )


def _decode_tail(stdout: object) -> str:
    """Render a ``TimeoutExpired.stdout`` value as a bounded text tail.

    ``subprocess.TimeoutExpired.stdout`` is ``bytes`` when ``text=False``
    and ``str`` when ``text=True``; both shapes appear in the wild on
    different Python versions. Coerce to ``str``, trim to
    :data:`_OUTPUT_TAIL_BYTES`, and tolerate non-UTF-8 bytes via the
    ``replace`` error handler.
    """
    if stdout is None:
        return ""
    if isinstance(stdout, bytes):
        return stdout[-_OUTPUT_TAIL_BYTES:].decode("utf-8", errors="replace")
    if isinstance(stdout, str):
        return stdout[-_OUTPUT_TAIL_BYTES:]
    return ""


def _emit_verify_diff_result_event(
    *,
    conn: sqlite3.Connection,
    lifecycle: ActionLifecycle,
    action_id: str,
    source_event_id: str,
    slot: RawResult,
) -> None:
    """Emit a single ``action.result_observed`` event + transition lifecycle.

    Day-2 Step 11 keeps the L4 sync-handler invariant (one
    ``action.result_observed`` per dispatch + terminal lifecycle
    transition before return) by emitting the event for the observation
    slot only. Step 12's L3 Result Interpreter will fan out one
    ``action.result_observed`` per :class:`RawResultBundle` slot when it
    consumes the bundle, so the verification-slot event lands at that
    layer. Day-2 unit tests assert the bundle shape directly; they do not
    rely on two L4-emitted events.
    """
    emit_event(
        conn,
        type="action.result_observed",
        payload={
            "action_id": action_id,
            "semantics": slot.semantics,
            "tool_output": slot.tool_output,
        },
        source_event_id=source_event_id,
        correlation={"action_id": action_id},
    )
    lifecycle.transition(action_id, "result_observed")


# --- create_task handler (ADR-0002 Step 4) ----------------------------------


def _new_task_id() -> str:
    """Mint a fresh task id (`"T_" + 8-hex`).

    Day-2 Step 4: task ids are LLM-visible and used as `target_entity_ref`
    on follow-up actions (spawn_worker / verify_diff). Eight hex chars is
    the same width as `run_id` / `action_id` for visual symmetry in the
    event trace.
    """
    return "T_" + uuid.uuid4().hex[:8]


@dataclass(frozen=True)
class _CreateTaskArgs:
    """Parsed + validated arguments for `create_task_handler`."""

    goal: str
    repo_path: str | None
    deadline: str | None
    source: str


def _parse_create_task_args(arguments: Mapping[str, Any]) -> _CreateTaskArgs:
    """Validate `create_task` arguments and return a typed bundle.

    Raises ``KeyError`` if `goal` is absent (the registered tool schema
    declares it required, but the handler defends in depth so the L3
    Pre-action Gate is not the only place enforcing schema). Raises
    ``TypeError`` if any provided field is the wrong type.
    """
    goal = arguments["goal"]
    if not isinstance(goal, str):
        msg = f"create_task: goal must be a string (got {type(goal).__name__})"
        raise TypeError(msg)

    repo_path_arg = arguments.get("repo_path")
    if repo_path_arg is not None and not isinstance(repo_path_arg, str):
        msg = (
            f"create_task: repo_path must be a string or None "
            f"(got {type(repo_path_arg).__name__})"
        )
        raise TypeError(msg)

    deadline = arguments.get("deadline")
    if deadline is not None and not isinstance(deadline, str):
        msg = (
            f"create_task: deadline must be a string or None "
            f"(got {type(deadline).__name__})"
        )
        raise TypeError(msg)

    source = arguments.get("source", "manual")
    if not isinstance(source, str):
        msg = f"create_task: source must be a string (got {type(source).__name__})"
        raise TypeError(msg)

    return _CreateTaskArgs(
        goal=goal,
        repo_path=repo_path_arg,
        deadline=deadline,
        source=source,
    )


def create_task_handler(
    action_request: ActionRequest,
    conn: sqlite3.Connection,
    runtime_paths: RuntimePathsLike,  # noqa: ARG001 — kept for handler signature uniformity.
    lifecycle: ActionLifecycle,
) -> RawResult:
    """L1 `create_task` stub — sync lifecycle, emits one `task.created`.

    Per ADR-0002 § D14 ("Natural-language path through L3 calling the
    new `create_task` L4 tool") + § Step 4 build order.

    Steps:
        1. Mint `task_id = _new_task_id()`.
        2. Parse args via `_parse_create_task_args`:
           - `goal` (required)
           - `repo_path` (optional)
           - `deadline` (optional)
           - `source` (optional; defaults to `"manual"`)
        3. If `repo_path` is provided, call `detect_verify_command(
           Path(repo_path))` to pick the verify_command string per the
           four pinned detection rules (or `None` if no framework matches
           — Limitation-Claim downgrade path).
        4. Emit `task.created` with required (`task_id`, `goal`) +
           non-None optional (`source`, `deadline`, `repo_path`,
           `verify_command`). Source defaults to `"manual"`.
        5. Emit `action.result_observed(semantics="ack")` and transition
           lifecycle `running → result_observed` (Day-1 sync-handler
           pattern; see `verify_diff_handler` for the canonical sequence).
        6. Return `RawResult(semantics="ack", payload={"task_id": ...,
           "verify_command": ...})`.

    No CLI side door: per D14 there is no `jarvis task add` subcommand;
    task creation flows exclusively through the L3 LLM dispatching this
    tool. The handler emits `task.created` itself — production code
    elsewhere must NOT mint `task.created` rows (tests can seed the
    event log directly).
    """
    args = _parse_create_task_args(action_request.arguments)

    task_id = _new_task_id()
    running_event_uid = _get_running_event_uid(conn, action_request.action_id)

    verify_command: str | None = None
    if args.repo_path is not None:
        verify_command = detect_verify_command(Path(args.repo_path))

    # Build the task.created payload with non-None optional fields only,
    # so the registry's optional_payload contract stays tight (no None
    # sentinel values masquerading as data).
    task_created_payload: dict[str, Any] = {
        "task_id": task_id,
        "goal": args.goal,
        "source": args.source,
    }
    if args.deadline is not None:
        task_created_payload["deadline"] = args.deadline
    if args.repo_path is not None:
        task_created_payload["repo_path"] = args.repo_path
    if verify_command is not None:
        task_created_payload["verify_command"] = verify_command

    task_correlation: dict[str, str] = {
        "task_id": task_id,
        "action_id": action_request.action_id,
    }
    if action_request.turn_id is not None:
        task_correlation["turn_id"] = action_request.turn_id

    emit_event(
        conn,
        type="task.created",
        payload=task_created_payload,
        source_event_id=running_event_uid,
        correlation=task_correlation,
    )

    # Sync handler: emit action.result_observed(ack) + terminal-transition.
    ack_payload: dict[str, Any] = {"task_id": task_id}
    if verify_command is not None:
        ack_payload["verify_command"] = verify_command
    tool_output_str = tool_result(ack_payload)

    emit_event(
        conn,
        type="action.result_observed",
        payload={
            "action_id": action_request.action_id,
            "semantics": "ack",
            "tool_output": tool_output_str,
        },
        source_event_id=running_event_uid,
        correlation={
            "action_id": action_request.action_id,
            "task_id": task_id,
        },
    )
    lifecycle.transition(action_request.action_id, "result_observed")

    return RawResult(
        action_id=action_request.action_id,
        semantics="ack",
        payload=ack_payload,
        tool_output=tool_output_str,
        error=None,
    )


# --- list_tasks handler (F6 — read-only L0 observation tool) ----------------


_LIST_TASKS_VALID_STATUSES: Final[frozenset[str]] = frozenset({"open", "verified", "all"})
"""Allowed ``status`` filter values for `list_tasks`.

Mirrors the `enum` on `_LIST_TASKS_INPUT_SCHEMA` so the handler can
defend in depth without re-listing the values inline. Internal triple
(`open` / `reported_complete` / `verified_complete`) stays in
`TaskLedgerSnapshot.derive_status`; the LLM only sees the collapsed
binary `{open, verified}` plus the catch-all `all`.
"""

_LIST_TASKS_DEFAULT_LIMIT: Final[int] = 10
"""Default maximum number of task rows the handler returns when the LLM
does not specify `limit` in `arguments`.
"""

_LIST_TASKS_STATUS_TO_SURFACE: Final[Mapping[str, str]] = {
    "open": "open",
    "verified_complete": "verified",
    "reported_complete": "reported",
}
"""Map the internal derived status triple onto the JSON `status` string
the LLM sees. `reported_complete` is surfaced as `reported` so a future
LLM-side prompt can distinguish a worker self-report from a verified
completion; today's filter only branches on `open` vs `verified`.
"""


_LIST_TASKS_INPUT_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["open", "verified", "all"],
            "description": (
                "Filter by derived task status. 'open' = task.created with "
                "no task.verified yet. 'verified' = task.verified + verified "
                "Postcondition Claim. 'all' = no filter. Default 'open'."
            ),
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "description": "Maximum number of tasks to return. Default 10.",
        },
    },
    "required": [],
}


def list_tasks_handler(
    action_request: ActionRequest,
    conn: sqlite3.Connection,
    runtime_paths: RuntimePathsLike,  # noqa: ARG001 — signature uniformity.
    lifecycle: ActionLifecycle,
) -> RawResult:
    """L0 read-only ``list_tasks`` — return Task Ledger filtered by derived status.

    Answers Allen's "我有哪些 open task?" / "show my open work" via a
    fold of the live Event Log into :class:`TaskLedgerSnapshot` plus a
    derived-status filter. Read-only — no events emitted beyond the
    mandatory single ``action.result_observed(semantics="observation")``.

    Arguments (all optional):
        status: ``"open"`` (default) | ``"verified"`` | ``"all"``.
            Invalid values short-circuit to a ``semantics="error"``
            slot with ``code="invalid_argument"`` — defense-in-depth
            for the LLM's schema (the schema's ``enum`` should catch
            this first).
        limit: positive integer; default 10. The schema enforces
            ``minimum: 1`` on the LLM side; the handler clamps to
            ``max(1, limit)`` so a zero/negative slipped through is
            normalized rather than silently yielding an empty list.

    Returns:
        ``RawResult(semantics="observation", payload={"tasks": [...]})``
        where each entry is
        ``{"task_id": str, "goal": str, "status": str}``. Status is
        surfaced via :data:`_LIST_TASKS_STATUS_TO_SURFACE` so the LLM
        only sees ``open``/``verified``/``reported`` rather than the
        internal ``verified_complete``/``reported_complete`` triple.

    Iteration order: ``records_by_task_id`` preserves insertion order
    (Python 3.7+ dict guarantee), so tasks appear in the order their
    originating ``task.created`` was emitted.
    """
    running_event_uid = _get_running_event_uid(conn, action_request.action_id)

    status_arg = action_request.arguments.get("status", "open")
    if not isinstance(status_arg, str) or status_arg not in _LIST_TASKS_VALID_STATUSES:
        valid = sorted(_LIST_TASKS_VALID_STATUSES)
        error_msg = f"invalid 'status' (got {status_arg!r}); must be one of {valid!r}"
        tool_output_str = tool_error(error_msg, code="invalid_argument")
        emit_event(
            conn,
            type="action.result_observed",
            payload={
                "action_id": action_request.action_id,
                "semantics": "error",
                "tool_output": tool_output_str,
                "error": "invalid_argument",
            },
            source_event_id=running_event_uid,
            correlation={"action_id": action_request.action_id},
        )
        lifecycle.transition(action_request.action_id, "result_observed")
        return RawResult(
            action_id=action_request.action_id,
            semantics="error",
            payload={"error": "invalid_argument"},
            tool_output=tool_output_str,
            error="invalid_argument",
        )

    limit_arg = action_request.arguments.get("limit", _LIST_TASKS_DEFAULT_LIMIT)
    limit = max(1, int(limit_arg))

    snapshot = make_snapshot(conn).task_ledger
    out: list[dict[str, Any]] = []
    for task_id, record in snapshot.records_by_task_id.items():
        derived = snapshot.derive_status(task_id)
        if status_arg == "open" and derived != "open":
            continue
        if status_arg == "verified" and derived != "verified_complete":
            continue
        status_str = _LIST_TASKS_STATUS_TO_SURFACE.get(derived, derived)
        out.append({"task_id": record.task_id, "goal": record.goal, "status": status_str})
        if len(out) >= limit:
            break

    payload: dict[str, Any] = {"tasks": out}
    tool_output_str = tool_result(payload)

    emit_event(
        conn,
        type="action.result_observed",
        payload={
            "action_id": action_request.action_id,
            "semantics": "observation",
            "tool_output": tool_output_str,
        },
        source_event_id=running_event_uid,
        correlation={"action_id": action_request.action_id},
    )
    lifecycle.transition(action_request.action_id, "result_observed")

    return RawResult(
        action_id=action_request.action_id,
        semantics="observation",
        payload=payload,
        tool_output=tool_output_str,
        error=None,
    )


# --- get_current_time (F-Tier0) ---------------------------------------------


_WEEKDAYS_ZH: Final[tuple[str, ...]] = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
"""`datetime.weekday()` (Mon=0) indexed into the zh-CN weekday names."""

_DAY_PERIODS: Final[tuple[tuple[int, str], ...]] = (
    (6, "凌晨"),
    (9, "早上"),
    (12, "上午"),
    (13, "中午"),
    (18, "下午"),
    (24, "晚上"),
)
"""`(exclusive upper-bound hour, zh-CN day-period label)`, ascending."""

_SPOKEN_MINUTE_FILLER_BELOW: Final[int] = 10
"""Minutes below this take the spoken `零` filler (`10点零2分`)."""


def _spoken_day_period(hour: int) -> str:
    """Map a 24h hour to the zh-CN day-period prefix used by TTS."""
    for upper_bound, label in _DAY_PERIODS:
        if hour < upper_bound:
            return label
    return "晚上"


def _spoken_clock(hour: int, minute: int) -> str:
    """Render a 24h `(hour, minute)` as one idiomatic zh-CN spoken clock string.

    This field exists only to be spoken by TTS, so it follows speech
    convention rather than digit-for-digit transcription:

    - minute 0 says ``整`` (``上午10点整``), never ``10点0分``;
    - minutes 1-9 take the ``零`` filler (``10点零2分``) — dropping it
      makes the utterance wrong, not merely terse;
    - hour 0 is ``零点`` (``凌晨零点30分``); the naive 12h wrap would say
      ``凌晨12点``, which contradicts itself since ``12点`` reads as noon.

    Args:
        hour: Hour in 24h form, 0-23.
        minute: Minute, 0-59.

    Returns:
        The day-period prefix followed by the spoken clock reading.
    """
    period = _spoken_day_period(hour)
    hour_label = "零" if hour == 0 else str(hour % 12 or 12)
    if minute == 0:
        return f"{period}{hour_label}点整"
    if minute < _SPOKEN_MINUTE_FILLER_BELOW:
        return f"{period}{hour_label}点零{minute}分"
    return f"{period}{hour_label}点{minute}分"


def get_current_time_handler(
    action_request: ActionRequest,
    conn: sqlite3.Connection,
    runtime_paths: RuntimePathsLike,  # noqa: ARG001 — signature uniformity.
    lifecycle: ActionLifecycle,
) -> RawResult:
    """Read the system clock; observation semantics only (spec §3.5.4).

    Zero arguments, zero side effects, risk L0. The Tier 0 regex path
    (spec §17) is the primary caller; jarvis_llm may also call it.

    Returns:
        ``RawResult(semantics="observation")`` whose payload carries the
        machine keys ``iso`` / ``date`` / ``time`` / ``weekday`` plus the
        TTS-ready ``spoken_time`` / ``spoken_date`` the L5 templates read.
    """
    running_event_uid = _get_running_event_uid(conn, action_request.action_id)

    now = datetime.now().astimezone()
    weekday = _WEEKDAYS_ZH[now.weekday()]
    payload: dict[str, Any] = {
        "iso": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "weekday": weekday,
        "spoken_time": _spoken_clock(now.hour, now.minute),
        "spoken_date": f"{now.month}月{now.day}日{weekday}",
    }
    tool_output_str = tool_result(payload)

    emit_event(
        conn,
        type="action.result_observed",
        payload={
            "action_id": action_request.action_id,
            "semantics": "observation",
            "tool_output": tool_output_str,
        },
        source_event_id=running_event_uid,
        correlation={"action_id": action_request.action_id},
    )
    lifecycle.transition(action_request.action_id, "result_observed")

    return RawResult(
        action_id=action_request.action_id,
        semantics="observation",
        payload=payload,
        tool_output=tool_output_str,
        error=None,
    )


# --- open_path handler --------------------------------------------------------

_OPEN_PATH_DEFAULT_TARGET_KIND: Final[str] = "any"
_OPEN_PATH_DEFAULT_APP: Final[str] = "default"
_OPEN_PATH_SUBPROCESS_TIMEOUT_S: Final[float] = 10.0


@dataclass(frozen=True)
class _OpenPathArgs:
    """Parsed + validated arguments for `open_path_handler`."""

    query: str
    target_kind: TargetKind
    app: Literal["default", "vscode"]


def _parse_open_path_args(arguments: Mapping[str, Any]) -> _OpenPathArgs:
    """Validate `open_path` arguments and return a typed bundle.

    Raises (`KeyError` for the missing required `query`, `TypeError` for a
    wrong type, an empty/whitespace-only query, or an out-of-enum value)
    rather than returning a sentinel — the caller, `open_path_handler`,
    catches both and turns them into a `tool_error(code="invalid_argument")`
    RawResult (see `list_tasks_handler`'s `invalid_argument` shape for the
    convention this mirrors). The schema's `enum`/`required` should catch
    most of this first for `jarvis_llm` callers, but Tier 0's literal-string
    `args` (spec §17) — and a Tier 0 capture group that strips to an empty
    string, e.g. "帮我打开 。" — bypass schema validation entirely, so the
    handler must defend here rather than let an unhandled exception strand
    the lifecycle at `running`.
    """
    query = arguments["query"]
    if not isinstance(query, str) or not query.strip():
        msg = f"open_path: query must be a non-empty string (got {query!r})"
        raise TypeError(msg)

    target_kind = arguments.get("target_kind", _OPEN_PATH_DEFAULT_TARGET_KIND)
    if target_kind not in ("file", "folder", "any"):
        msg = f"open_path: target_kind must be one of file/folder/any (got {target_kind!r})"
        raise TypeError(msg)

    app = arguments.get("app", _OPEN_PATH_DEFAULT_APP)
    if app not in ("default", "vscode"):
        msg = f"open_path: app must be one of default/vscode (got {app!r})"
        raise TypeError(msg)

    return _OpenPathArgs(query=query, target_kind=target_kind, app=app)


def _build_open_argv(path: Path, app: Literal["default", "vscode"]) -> tuple[list[str], str]:
    """Build the `open`/`open -a` argv per the app + extension rules.

    - `app == "vscode"` (file or folder) -> `open -a <editor_app> <path>`,
      unconditionally — an explicit "用 VS Code 打开" request always wins.
    - `app == "default"` and `path` is a file whose extension is in
      `editor_extensions` -> also `open -a <editor_app> <path>`.
    - Everything else (folders, non-editor files) -> the macOS default
      handler, `open <path>`.

    Returns `(argv, app_used)` where `app_used` is `editor_app` or the
    literal string `"default"` — the latter feeds the success payload's
    `app_used` field verbatim.
    """
    config = load_file_targets_config()
    if app == "vscode":
        return ["open", "-a", config.editor_app, str(path)], config.editor_app
    if path.is_file() and path.suffix.lstrip(".").lower() in config.editor_extensions:
        return ["open", "-a", config.editor_app, str(path)], config.editor_app
    return ["open", str(path)], "default"


def _open_path_error(  # noqa: PLR0913 — all kwargs are the shared sync-handler failure shape (conn/lifecycle/action_id/running_event_uid/code/message); splitting them into a bundle defeats the point of a shared helper.
    *,
    conn: sqlite3.Connection,
    lifecycle: ActionLifecycle,
    action_id: str,
    running_event_uid: str,
    code: str,
    message: str,
) -> RawResult:
    """Shared `open_path` failure path: `action.result_observed(error)` + terminal-transition.

    Mirrors `list_tasks_handler`'s `invalid_argument` failure shape — every
    exit from a sync L4 handler, success or failure, emits exactly one
    `action.result_observed` and transitions the lifecycle terminal before
    returning (the invariant `_get_running_event_uid` documents at its call
    sites).
    """
    tool_output_str = tool_error(message, code=code)
    emit_event(
        conn,
        type="action.result_observed",
        payload={
            "action_id": action_id,
            "semantics": "error",
            "tool_output": tool_output_str,
            "error": code,
        },
        source_event_id=running_event_uid,
        correlation={"action_id": action_id},
    )
    lifecycle.transition(action_id, "result_observed")
    return RawResult(
        action_id=action_id,
        semantics="error",
        payload={"error": code},
        tool_output=tool_output_str,
        error=code,
    )


def open_path_handler(
    action_request: ActionRequest,
    conn: sqlite3.Connection,
    runtime_paths: RuntimePathsLike,  # noqa: ARG001 — signature uniformity.
    lifecycle: ActionLifecycle,
) -> RawResult:
    """Open a file or folder on Allen's Mac by spoken name (spec §17 companion tool).

    "打开 X" / "用 VS Code 打开 X" — Allen names a file or folder loosely
    (an alias, a partial filename, a description); the handler resolves it
    via :func:`jarvis.execution.path_resolver.resolve` (pure logic, no
    event emission — see that module's docstring for the bookmark /
    Spotlight / one-level-scan strategy and the ranking rule) and, on a
    match, shells out to macOS `open`.

    This handler owns exactly what `resolve()` deliberately does not:

    - argv construction (`_build_open_argv`),
    - the `open` subprocess call,
    - the L4 event-emission + lifecycle-transition contract every sync
      handler in this module follows — one `action.result_observed` +
      one terminal `lifecycle.transition`, on EVERY exit path, success or
      failure (see `create_task_handler` for the canonical success shape,
      `list_tasks_handler` for the canonical failure shape this mirrors).

    Arguments (`arguments` on `action_request`):
        query: Required. Spoken name/description of the target.
        target_kind: Optional `"file" | "folder" | "any"`, default `"any"`.
        app: Optional `"default" | "vscode"`, default `"default"`.

    Returns:
        Success: `RawResult(semantics="observation", payload={"opened_name",
        "opened_path", "app_used", "target_kind"})`.
        Failure: `RawResult(semantics="error")` with `error` one of
        `"invalid_argument"` (missing/empty `query` or an out-of-enum
        `target_kind`/`app` — caught from `_parse_open_path_args`),
        `"target_not_found"` (no candidate matched), or `"open_failed"`
        (the `open` subprocess errored or exited non-zero). Every path,
        success or failure, emits exactly one `action.result_observed`
        and terminal-transitions the lifecycle before returning — no
        exit leaves the lifecycle stranded at `running`.
    """
    running_event_uid = _get_running_event_uid(conn, action_request.action_id)

    try:
        args = _parse_open_path_args(action_request.arguments)
    except (KeyError, TypeError) as exc:
        return _open_path_error(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_request.action_id,
            running_event_uid=running_event_uid,
            code="invalid_argument",
            message=f"open_path: {exc}",
        )

    target = resolve_path_target(args.query, args.target_kind, conn)
    if target is None:
        return _open_path_error(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_request.action_id,
            running_event_uid=running_event_uid,
            code="target_not_found",
            message=f"open_path: no file/folder matched {args.query!r}",
        )

    argv, app_used = _build_open_argv(target.path, args.app)

    try:
        proc = subprocess.run(  # noqa: S603 — argv list, no shell; path comes only from resolve()'s home-scoped candidates.
            argv,
            timeout=_OPEN_PATH_SUBPROCESS_TIMEOUT_S,
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _open_path_error(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_request.action_id,
            running_event_uid=running_event_uid,
            code="open_failed",
            message=f"open_path: subprocess failed to start: {exc}",
        )

    if proc.returncode != 0:
        stderr_tail = (proc.stderr or "").strip()[-_OUTPUT_TAIL_BYTES:]
        return _open_path_error(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_request.action_id,
            running_event_uid=running_event_uid,
            code="open_failed",
            message=f"open_path: `open` exited {proc.returncode}: {stderr_tail}",
        )

    payload: dict[str, Any] = {
        "opened_name": target.display_name,
        "opened_path": str(target.path),
        "app_used": app_used,
        "target_kind": args.target_kind,
    }
    tool_output_str = tool_result(payload)

    emit_event(
        conn,
        type="action.result_observed",
        payload={
            "action_id": action_request.action_id,
            "semantics": "observation",
            "tool_output": tool_output_str,
        },
        source_event_id=running_event_uid,
        correlation={"action_id": action_request.action_id},
    )
    lifecycle.transition(action_request.action_id, "result_observed")

    return RawResult(
        action_id=action_request.action_id,
        semantics="observation",
        payload=payload,
        tool_output=tool_output_str,
        error=None,
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
    ) -> RawResultBundle:
        """Dispatch one ActionRequest and return its :class:`RawResultBundle`.

        Day-2 (ADR-0002 § RawResultBundle contract wrapping rule): the
        dispatcher uniformly returns ``RawResultBundle`` to L3.
        Single-slot tools (``spawn_worker``, ``create_task``) return a
        bare :class:`RawResult` from their handler; the dispatcher wraps
        them in ``RawResultBundle(slots=(result,))`` on return.
        Multi-slot tools (``verify_diff`` per spec §3.5.7) return
        :class:`RawResultBundle` directly and pass through. This keeps
        the dispatcher signature uniform (``Callable[..., RawResultBundle]``)
        and avoids a union return type at every L3 call site — narrows
        once at this boundary.

        Preconditions:
            - `action_request.tool_name` is registered → else `UnknownToolError`.
            - `action_request.caller_principal` is in `tool_def.allowed_callers`
              → else `CallerNotAllowedError`.
            - `lifecycle.state_of(action_id) == "authorized"` → else
              `IllegalLifecycleTransition` (the caller, L3, is expected
              to `register` the action and transition `proposed →
              authorized` before this call).

        On precondition pass, registers the action_id in the live set
        (ADR-0009 D4 — the supervisor sweep skips live action_ids;
        `jarvis.runtime.drive_turn` releases the turn's entries in a
        `finally`), then emits:
            1. `action.dispatched(action_id)` → lifecycle authorized →
               dispatched. The event_uid is the `source_event_id` of
               the next event. Carries `result_expected_by_ms` when
               `tool_def.result_budget_s` is declared — the persisted
               deadline the supervisor sweep reads (ADR-0009 D4,
               spec §3.4.8).
            2. `action.running(action_id)` → lifecycle dispatched →
               running. The event_uid is stashed on `conn` under
               `_jarvis_running_event_uid` so the handler can use it as
               `source_event_id` for `run.started` /
               `action.result_observed`.

        The handler then runs and returns a `RawResult` or
        `RawResultBundle`. The handler is responsible for any further
        lifecycle transitions (sync tools terminal-transition before
        returning; async tools leave the lifecycle at `running`).
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

        # ADR-0009 D4 — publish the action as live BEFORE the first event
        # lands, so a sweep tick that runs between the two never sees an
        # unprotected open action. `drive_turn` owns the release.
        if action_request.turn_id is not None:
            register_live_action(
                turn_id=action_request.turn_id,
                action_id=action_request.action_id,
            )

        dispatched_payload: dict[str, Any] = {"action_id": action_request.action_id}
        result_expected_by_ms = _result_expected_by_ms(tool_def)
        if result_expected_by_ms is not None:
            dispatched_payload["result_expected_by_ms"] = result_expected_by_ms
        dispatched_event = emit_event(
            conn,
            type="action.dispatched",
            payload=dispatched_payload,
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
            handler_result = tool_def.handler(
                action_request, conn, runtime_paths, lifecycle,
            )
        finally:
            _clear_running_event_uid(conn, action_request.action_id)

        # § RawResultBundle wrapping rule. Multi-slot tools
        # (`verify_diff` Day-2) already return RawResultBundle; bare
        # RawResult returns from single-slot tools get wrapped here so
        # L3 always sees the bundle shape.
        if isinstance(handler_result, RawResultBundle):
            return handler_result
        return RawResultBundle(slots=(handler_result,))


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


def _result_expected_by_ms(tool_def: ToolDefinition) -> int | None:
    """Return the epoch-ms deadline to stamp on `action.dispatched`, or None.

    ADR-0009 D4. `None` for every tool that declares no
    `result_budget_s`; the supervisor sweep then falls back to
    `dispatched ts + supervisor.default_budget_s` for that action.
    """
    budget = tool_def.result_budget_s
    if budget is None:
        return None
    return int(time.time() * 1000) + int((budget() + _DISPATCH_DEADLINE_GRACE_S) * 1000)


# --- Live action-id set (ADR-0009 D4 active-turn exclusion) ------------------
#
# The supervisor sweep (`jarvis.deployment.sleep_wake`) closes open
# actions whose deadline has passed. It must never close an action a turn
# is still driving, and it cannot ask the `ActionLifecycle` FSM: that is a
# per-process dict, empty in the sweep's own view after any restart.
#
# So the dispatch path publishes each action_id here, and the composition
# root (`jarvis.runtime.drive_turn`) drops the turn's whole entry in a
# `finally` — including when the turn raises, because an entry nobody
# removes pins its action_id "active" for the life of the process and the
# sweep could never close the very orphan that crash created.
#
# Keyed by turn_id (not a flat set) so one turn's release cannot unprotect
# a concurrent turn's actions — the daemon drives turns on worker threads.
# Storage sits in L4 because L4 is where dispatch happens and L4 may not
# import runtime; L6 reads it only through a value the composition root
# passes down, so `deployment -> execution` never appears in the graph.
# Same module-level + Lock shape as `_RUNNING_UID_TABLE` below.

_LIVE_ACTIONS_LOCK: Final[threading.Lock] = threading.Lock()
_LIVE_ACTIONS_BY_TURN: Final[dict[str, set[str]]] = {}


def register_live_action(*, turn_id: str, action_id: str) -> None:
    """Publish ``action_id`` as driven by ``turn_id`` (idempotent)."""
    with _LIVE_ACTIONS_LOCK:
        _LIVE_ACTIONS_BY_TURN.setdefault(turn_id, set()).add(action_id)


def release_turn_actions(turn_id: str) -> None:
    """Drop every action_id ``turn_id`` registered. No-op if unknown."""
    with _LIVE_ACTIONS_LOCK:
        _LIVE_ACTIONS_BY_TURN.pop(turn_id, None)


def live_action_ids() -> frozenset[str]:
    """Snapshot every action_id currently driven by some live turn."""
    with _LIVE_ACTIONS_LOCK:
        return frozenset(
            action_id
            for action_ids in _LIVE_ACTIONS_BY_TURN.values()
            for action_id in action_ids
        )


def turn_action_ids(turn_id: str) -> frozenset[str]:
    """Snapshot the action_ids ``turn_id`` alone owns (empty when unknown).

    The per-turn narrowing of :func:`live_action_ids`, read by the
    composition root's in-turn trigger waiter (ADR-0009 D4 / F9). The
    waiter must accept ONLY its own actions' terminal events: the global
    live set would still let one in-flight turn adopt a *concurrent*
    turn's timeout — the daemon drives turns on worker threads, so two
    can be live at once.
    """
    with _LIVE_ACTIONS_LOCK:
        return frozenset(_LIVE_ACTIONS_BY_TURN.get(turn_id, ()))


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
            "description": "run whose diff.txt to read (legacy / fallback)",
        },
        "artifact_path": {
            "type": "string",
            "description": (
                "Absolute path to the worker's diff.txt artifact "
                "(Day-2 L3 plumbs this directly; optional when run_id "
                "is supplied)"
            ),
        },
    },
    "required": ["run_id"],
}


# `verify_diff`'s ToolDefinition is bound at module scope so the
# Tier-1 canary `test_canary_verify_diff_post_action_check` can locate
# the `post_action_check=...` kwarg statically (AST scan).
VERIFY_DIFF_TOOL_DEF: Final[ToolDefinition] = ToolDefinition(
    name="verify_diff",
    description=(
        "Read the worker's diff artifact and (optionally) run a verify "
        "command to check postcondition. Dual-slot per spec §3.5.7: "
        "slot 1 is the diff observation; slot 2 is the verify_command "
        "exit predicate when the bound task carries a verify_command."
    ),
    allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM, CallerPrincipal.OBSERVER}),
    risk_level="L0",
    result_semantics="observation",
    is_async=False,
    input_schema=_VERIFY_DIFF_INPUT_SCHEMA,
    handler=verify_diff_handler,
    post_action_check=PostActionCheck(
        mode="inline",
        check_tool="verify_command",
        expected_predicate="exit_code == 0",
        result_semantics_on_match="verification",
        timeout_ms=600_000,
    ),
)

# create_task is L1 — risk floor for ledger writes per ADR-0002 Step 4
# build-order row. The schema mirrors ADR-0002 § Step 4 ("required:
# goal; optional: repo_path, deadline, source"). Source is enumerated
# rather than free-form so the Task Ledger projection can index by it
# without a separate normalization step.
_CREATE_TASK_INPUT_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "goal": {
            "type": "string",
            "description": "Plain-language task description",
        },
        "repo_path": {
            "type": "string",
            "description": "Absolute path to the target repo (optional)",
        },
        "deadline": {
            "type": "string",
            "description": "ISO date or natural-language deadline (optional)",
        },
        "source": {
            "type": "string",
            "description": (
                "Origin of the task: manual|automated|imported "
                "(optional, default 'manual')"
            ),
        },
    },
    "required": ["goal"],
}

# open_path is L1 — a subprocess side effect (`open <path>`), so it sits
# one rung above the L0 read-only observation tools even though it emits
# no claim. `app` stays a plain default/vscode enum rather than an
# arbitrary bundle-id string — the only Day-1 override is "force VS Code",
# matching the two Tier 0 patterns in config/tier0_patterns.yaml.
_OPEN_PATH_INPUT_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Spoken name or description of the file or folder to open.",
        },
        "target_kind": {
            "type": "string",
            "enum": ["file", "folder", "any"],
            "description": "Restrict the match to a file, a folder, or either. Default 'any'.",
        },
        "app": {
            "type": "string",
            "enum": ["default", "vscode"],
            "description": (
                "'default' uses the macOS default handler (or the configured "
                "editor for editor_extensions files); 'vscode' forces Visual "
                "Studio Code regardless of extension. Default 'default'."
            ),
        },
    },
    "required": ["query"],
}


def build_default_registry() -> ToolRegistry:
    """Assemble the Day-1 ToolRegistry (`spawn_worker` + `verify_diff` + `create_task`).

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
            # ADR-0009 D4: the only Day-2 tool that can outlive its
            # dispatch call, hence the only one carrying a supervisor
            # deadline. Passed as the resolver itself so a per-run
            # `JARVIS_CODEX_TURN_TIMEOUT_S` moves the persisted deadline
            # in step with the in-process driver deadline.
            result_budget_s=_resolve_codex_turn_timeout_s,
        )
    )
    registry.register(VERIFY_DIFF_TOOL_DEF)
    registry.register(
        ToolDefinition(
            name="create_task",
            description=(
                "Record a new task in the Task Ledger. Use when Allen says "
                "'帮我做 X' / '今天/明天给我 Y' / similar."
            ),
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L1",
            result_semantics="ack",
            is_async=False,
            input_schema=_CREATE_TASK_INPUT_SCHEMA,
            handler=create_task_handler,
        )
    )
    registry.register(
        ToolDefinition(
            name="list_tasks",
            description=(
                "Return a list of tasks from the Task Ledger filtered by "
                "derived status. Read-only — emits no claim, only "
                "observation. Use when Allen asks 'what tasks do I have' "
                "or 'show my open work'."
            ),
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L0",
            result_semantics="observation",
            is_async=False,
            input_schema=_LIST_TASKS_INPUT_SCHEMA,
            handler=list_tasks_handler,
        )
    )
    registry.register(
        ToolDefinition(
            name="get_current_time",
            description="Read the current local date and time (observation only).",
            allowed_callers=frozenset(
                {CallerPrincipal.REGEX_ROUTER, CallerPrincipal.JARVIS_LLM},
            ),
            risk_level="L0",
            result_semantics="observation",
            is_async=False,
            input_schema={"type": "object", "properties": {}, "required": []},
            handler=get_current_time_handler,
        )
    )
    registry.register(
        ToolDefinition(
            name="open_path",
            description=(
                "Open a file or folder on Allen's Mac by spoken name (bookmark "
                "alias, partial filename, or description). Use for '打开 X' / "
                "'用 VS Code 打开 X' requests."
            ),
            allowed_callers=frozenset(
                {CallerPrincipal.REGEX_ROUTER, CallerPrincipal.JARVIS_LLM},
            ),
            risk_level="L1",
            result_semantics="observation",
            is_async=False,
            input_schema=_OPEN_PATH_INPUT_SCHEMA,
            handler=open_path_handler,
        )
    )
    return registry


__all__ = [
    "VERIFY_DIFF_TOOL_DEF",
    "ActionLifecycle",
    "CallerNotAllowedError",
    "DuplicateToolError",
    "IllegalLifecycleTransition",
    "LifecycleState",
    "PostActionCheck",
    "RawResult",
    "RawResultBundle",
    "ResultSemantics",
    "RuntimePathsLike",
    "ToolDefinition",
    "ToolRegistry",
    "ToolRegistryError",
    "UnknownToolError",
    "build_default_registry",
    "create_task_handler",
    "get_current_time_handler",
    "list_tasks_handler",
    "live_action_ids",
    "open_path_handler",
    "register_live_action",
    "release_turn_actions",
    "spawn_worker_handler",
    "tool_error",
    "tool_result",
    "turn_action_ids",
    "verify_diff_handler",
]
