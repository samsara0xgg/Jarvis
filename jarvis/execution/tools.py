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

import codecs
import hashlib
import ipaddress
import json
import logging
import math
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from jarvis.execution.action_runner import (
    GLOBAL_RESOURCE_KEY,
    ActionJob,
    ActionRunner,
    ActionRunnerError,
    ActionSubmission,
    CancelAccepted,
    CancelAlreadyTerminal,
    CancellationMode,
    CancelOutcome,
    CancelUnconfirmed,
    ResourceKeyResolutionError,
    ToolConcurrency,
    current_execution_context,
)
from jarvis.execution.codex_action import (
    CODEX_CANCELLED_ERROR,
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
from jarvis.shared.action_admission import action_admission_guard
from jarvis.shared.text import truncate_utf8
from jarvis.state.authorized_dispatch_outbox import admit_authorized_dispatch
from jarvis.state.event_log import emit_event, iter_events, iter_events_of_types
from jarvis.state.lifecycle_terminal import terminalize_action
from jarvis.state.memory_db import DEFAULT_SEARCH_LIMIT
from jarvis.state.memory_db import search_records as search_memory_records
from jarvis.state.projections import make_snapshot

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping, Sequence

    from jarvis.execution.action_runner import ActionExecutionContext
    from jarvis.shared import Event


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
        domain: One of the spec §14.1 domains (e.g. ``"git"``,
            ``"task_ledger"``, ``"agent_control"``, ``"state_read"``,
            ``"mac_gui"``). Feeds audit payloads and future surface
            filtering (ADR-0011 D2).
        read_only: True if the tool performs no durable-state mutation.
            Feeds ``surface_for`` grouping and the Pre-emit scrub
            context (ADR-0011 D2).
        requires_entity: True if the Pre-action Gate must see a
            resolved ``target_entity_ref`` on the ActionRequest before
            letting this tool through. Feeds the gate's entity arm
            landing in Step 3 (ADR-0011 D3).
        requires_confirmation: True if dispatch needs a valid
            ``authorization_lease`` (feeds ADR-0012's confirmation
            template line). Must equal ``risk_rank(risk_level) >=
            risk_rank(confirmation_threshold)``; boot validation
            (``jarvis.decision.policy.validate_requires_confirmation``)
            enforces this so the two fields cannot drift.
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
    domain: str
    read_only: bool
    requires_entity: bool
    requires_confirmation: bool
    post_action_check: PostActionCheck | None = None
    result_budget_s: Callable[[], float] | None = None
    cancellation_mode: CancellationMode = "unsupported"
    """ADR-0008 D9 cancellation capability of this tool's handler.

    Defaults to ``"unsupported"`` for every tool shipped today, and that is
    the honest value: none of their handlers polls
    :func:`jarvis.execution.action_runner.current_execution_context`, so a
    cancel request could not stop them. F9 requires that such an action keeps
    running and never gets a false ``action.cancelled`` — declaring the
    capability here is what makes the runner refuse before it writes one.
    """


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


def _emit_executor_reported(  # noqa: PLR0913 — one keyword per durable field of the run's end-of-run row.
    conn: sqlite3.Connection,
    *,
    task_id: str,
    run_id: str,
    status: str,
    summary: str,
    source_event_id: str,
    correlation: Mapping[str, str],
    diff_path: str | None = None,
    cost: Mapping[str, Any] | None = None,
) -> None:
    """Emit one ``task.executor_reported`` row for a finished Codex run.

    ADR-0008 Step 4 makes this the durable home of the run's accounting
    facts. A truly background `spawn_worker` hands its ``RawResult`` to the
    runner, not to L3, so ``metadata["cost"]`` no longer reaches the only
    layer allowed to emit ``cost.recorded``; L3 reads them back from here at
    re-entry instead. L4 still never emits ``cost.recorded`` itself.
    """
    payload: dict[str, Any] = {
        "task_id": task_id,
        "run_id": run_id,
        "status": status,
        "summary": summary,
    }
    if diff_path is not None:
        payload["diff_path"] = diff_path
    if cost is not None:
        model = cost.get("model")
        if isinstance(model, str) and model:
            payload["model"] = model
        kind = cost.get("kind")
        if isinstance(kind, str) and kind:
            payload["executor"] = kind
        payload["tokens_in"] = int(cost.get("tokens_in", 0) or 0)
        payload["tokens_out"] = int(cost.get("tokens_out", 0) or 0)
    emit_event(
        conn,
        type="task.executor_reported",
        payload=payload,
        source_event_id=source_event_id,
        correlation=dict(correlation),
    )


def _spawn_worker_classify_failure(
    codex_result: CodexActionResult,
) -> tuple[Literal["action.failed", "action.timeout_assumed"], str, str] | None:
    """Classify a finished Codex turn into its terminal, or ``None`` if it succeeded.

    Returns ``(event_type, error_code, error_message)``. The two rows are the
    ones ADR-0002's Negative-path appendix names: an interrupt or an expired
    budget is ``action.timeout_assumed`` under the canonical
    ``codex_turn_timeout`` tag, and every other structured error is
    ``action.failed`` with the upstream tag preserved. A cancelled turn never
    reaches here — the caller returns before this, because its terminal
    belongs to whoever asked for the cancel (ADR-0008 D9).
    """
    if codex_result.interrupted or codex_result.error == "codex_turn_timeout":
        return (
            "action.timeout_assumed",
            "codex_turn_timeout",
            codex_result.error or "codex turn timed out",
        )
    if codex_result.error is not None:
        return (
            "action.failed",
            codex_result.error.split(":", 1)[0],
            codex_result.error,
        )
    return None


def _spawn_worker_cancel_seam(
    stash_ref: str | None,
    *,
    run_id: str,
    task_id: str,
) -> Callable[[], bool] | None:
    """Record this run's stash and identity, and return its cancel poll.

    ADR-0008 D9 (Step 4). All three need the same object, and all are no-ops
    off the runner: `current_execution_context()` returns ``None`` on the
    pre-Step-3 inline path, where the handler writes its own terminal and that
    terminal already carries the stash ref and both ids.

    The identity travels with the ref because the runner writes the cancel and
    timeout terminals, and the cleanup finalizer needs ``task_id`` to find the
    repository the stash belongs to and ``run_id`` to key its artifacts. Only
    the handler knows them: ``run_id`` is minted at ``run.started``, after the
    dispatcher built the context.
    """
    context = current_execution_context()
    if context is None:
        return None
    context.record_stash_ref(stash_ref)
    context.record_worker_identity(run_id=run_id, task_id=task_id)
    return lambda: context.is_cancel_requested


def _spawn_worker_cancelled_result(  # noqa: PLR0913 — one keyword per durable id the cancelled run still has to report.
    *,
    conn: sqlite3.Connection,
    action_id: str,
    task_id: str,
    run_id: str,
    source_event_id: str,
    correlation: Mapping[str, str],
    cost: Mapping[str, Any],
    stash_ref: str | None,
) -> RawResult:
    """Report a Codex turn the caller stopped, without writing a terminal.

    ADR-0008 D9 gives ``action.cancelled`` to the canceller, which may write
    it only once the runner confirms quiescence. Racing a terminal in from
    this thread would relabel an operator's stop as a timeout and make the
    cancel answer ``already_terminal``. The run still owes the ledger an
    end-of-run row and its token accounting, so those are emitted here; the
    pre-task stash reached the runner through the execution context, so the
    cleanup finalizer restores the tree either way.
    """
    _emit_executor_reported(
        conn,
        task_id=task_id,
        run_id=run_id,
        status="cancelled",
        summary="codex turn cancelled on request",
        source_event_id=source_event_id,
        correlation=correlation,
        cost=cost,
    )
    return RawResult(
        action_id=action_id,
        semantics="error",
        payload={"run_id": run_id, "status": "cancelled", "error": CODEX_CANCELLED_ERROR},
        tool_output=tool_error(
            "codex turn cancelled on request",
            code=CODEX_CANCELLED_ERROR,
        ),
        error=CODEX_CANCELLED_ERROR,
        metadata={"cost": dict(cost), "stash_ref": stash_ref},
    )


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

    failure_payload: dict[str, Any] = {
        "action_id": action_id,
        "error": error_code,
        "reason": error_message,
    }
    if stash_ref is not None:
        # Phase 0 batch 6: the runtime stash-pop finalizer scans terminal
        # failure events too — carry the ref so timeout / crash paths
        # restore Allen's pre-task stash instead of orphaning it.
        failure_payload["stash_ref"] = stash_ref

    executor_status = "failed" if event_type == "action.failed" else "timeout"
    if task_id is not None and run_id is not None:
        # The Task Ledger projection bridges through run.started for
        # task_id; emit task.executor_reported with a failure status so
        # the projection sees a terminal report on this run.
        _emit_executor_reported(
            conn,
            task_id=task_id,
            run_id=run_id,
            status=executor_status,
            summary=error_message,
            source_event_id=source_event_id,
            correlation=correlation,
            cost=cost,
        )
    terminalize_action(
        conn,
        event_type=event_type,
        payload=failure_payload,
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


def _worker_report_extras(submit_report: Mapping[str, Any]) -> dict[str, Any]:
    """Optional WorkerReport fields for the ``worker.reported`` payload.

    Phase 0 batch 5: reclaim the ``submit_report`` fields the payload
    literal previously dropped (spec §5.4.2 registry entry). Strict
    type guards — only well-typed values reach the log, lists are
    shallow-copied with ``str()``-coerced elements capped at 20 items,
    and the report_missing path (``submit_report == {}``) adds nothing.
    """
    extras: dict[str, Any] = {}
    for list_key in ("changed_files", "commands_run", "tests_run"):
        list_value = submit_report.get(list_key)
        if isinstance(list_value, list):
            extras[list_key] = [str(item) for item in list_value[:20]]
    for str_key in ("remaining_risks", "next_recommended_action"):
        str_value = submit_report.get(str_key)
        if isinstance(str_value, str):
            extras[str_key] = str_value
    review_flag = submit_report.get("needs_human_review")
    if isinstance(review_flag, bool):
        extras["needs_human_review"] = review_flag
    return extras


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
    should_cancel = _spawn_worker_cancel_seam(stash_ref, run_id=run_id, task_id=task_id)

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
            should_cancel=should_cancel,
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

    # 7-. Cancelled -- somebody asked this action to stop and the Codex
    # subprocess is being torn down. Deliberately NO terminal here: ADR-0008
    # D9 gives `action.cancelled` to the canceller, which may write it only
    # after the runner confirms quiescence. Racing a terminal in from this
    # thread would label an operator's stop as a timeout and make the cancel
    # answer `already_terminal`. The stash ref already reached the runner via
    # the execution context, so the cleanup finalizer still restores the tree.
    if codex_result.error == CODEX_CANCELLED_ERROR:
        return _spawn_worker_cancelled_result(
            conn=conn,
            action_id=action_request.action_id,
            task_id=task_id,
            run_id=run_id,
            source_event_id=running_event_uid,
            correlation=correlation,
            cost=cost,
            stash_ref=stash_ref,
        )

    # 7a/7b. Timeout (turn/interrupt was issued by the driver ->
    # action.timeout_assumed) and crash (initialize / thread/start /
    # turn/start / subprocess -> action.failed with the upstream tag
    # preserved). Both end here, with no worker.reported.
    classified = _spawn_worker_classify_failure(codex_result)
    if classified is not None:
        event_type, error_code, error_message = classified
        return _spawn_worker_emit_terminal_failure(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_request.action_id,
            task_id=task_id,
            run_id=run_id,
            source_event_id=running_event_uid,
            error_code=error_code,
            error_message=error_message,
            event_type=event_type,
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
    # 12. task.executor_reported -- projection-side terminal signal for
    # this run, and the durable home of its token accounting.
    _emit_executor_reported(
        conn,
        task_id=task_id,
        run_id=run_id,
        status=report_status,
        summary=report_summary,
        source_event_id=running_event_uid,
        correlation=correlation,
        diff_path=str(diff_artifact_path),
        cost=cost,
    )

    # Publish the reentry trigger only after its accounting facts commit.
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
            **_worker_report_extras(submit_report),
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
        the full ``diff_text`` (in-memory only, for the L3 reviewer)
        plus a bounded ``diff_text_preview``, a ``diff_nonempty`` flag
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

    repo_path = _resolve_repo_path(action_request, conn=conn)
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


def _resolve_repo_path(
    action_request: ActionRequest, *, conn: sqlite3.Connection | None = None,
) -> Path:
    """Return the repo cwd for the verify_command subprocess.

    Order: ``action_request.payload["repo_path"]`` (preferred — L3 plumbs
    it from the Task Ledger), then ``arguments["repo_path"]`` (test /
    direct call), then the verified run's task repository, then ``Path.cwd``.
    Both the resource lease and the actual subprocess use this resolver.
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
    if conn is not None:
        provenance = _verify_diff_run_provenance(action_request, conn)
        if provenance.task_id is not None:
            record = _load_task_record(conn, provenance.task_id)
            raw_repo = record.get("repo_path") if record is not None else None
            if isinstance(raw_repo, str) and raw_repo:
                return Path(raw_repo)
    return Path.cwd()


def _build_observation_slot(*, action_id: str, diff_path: Path) -> RawResult:
    """Read the diff artifact and shape slot 1 (``observation``).

    Missing files fall through to ``diff_text=""`` /
    ``diff_nonempty=False`` rather than raising — the spec treats a
    no-op diff as a real Day-2 outcome (Codex completed but did not
    edit), not an artifact-missing error. The C5 cross-task isolation
    check from Day-1 is intentionally dropped (Day-2 L3 owns the
    artifact-binding from spawn_worker's run_id correlation).

    The payload carries BOTH the full ``diff_text`` (in-memory only —
    the L3 reviewer reads it per ADR-0002 § Reviewer contract) and the
    bounded ``diff_text_preview`` retained as fallback.
    """
    diff_text = diff_path.read_text(encoding="utf-8") if diff_path.exists() else ""
    diff_nonempty = bool(diff_text.strip())
    content_hash = hashlib.sha256(diff_text.encode("utf-8")).hexdigest()
    payload: dict[str, Any] = {
        # In-memory only: the L3 reviewer reads the full text off this
        # slot. NEVER emitted into an event payload — the emitted
        # ``action.result_observed`` carries ``tool_output`` (path +
        # hash + flag), and the Evidence extras allowlist admits
        # ``artifact_path`` / ``content_hash`` only.
        "diff_text": diff_text,
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
    terminalize_action(
        conn,
        event_type="action.result_observed",
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

    terminalize_action(
        conn,
        event_type="action.result_observed",
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
        terminalize_action(
            conn,
            event_type="action.result_observed",
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

    terminalize_action(
        conn,
        event_type="action.result_observed",
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

    terminalize_action(
        conn,
        event_type="action.result_observed",
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


# --- memo inbox (create_memo / list_memos) -----------------------------------

_MEMO_MAX_CHARS: Final[int] = 2000


def create_memo_handler(
    action_request: ActionRequest,
    conn: sqlite3.Connection,
    runtime_paths: RuntimePathsLike,  # noqa: ARG001 — signature uniformity.
    lifecycle: ActionLifecycle,
) -> RawResult:
    """Append one memo to the event log (`memo.captured`), ack semantics.

    Primary caller is the Tier 0 ``note_capture`` row (``/note ...``);
    the L2 event IS the memo store — ``list_memos`` folds it back.
    """
    running_event_uid = _get_running_event_uid(conn, action_request.action_id)
    text = str(action_request.arguments.get("text", "")).strip()[:_MEMO_MAX_CHARS]
    if not text:
        return _memo_error(action_request, conn, lifecycle, running_event_uid, "empty_text")

    memo_event = emit_event(
        conn,
        type="memo.captured",
        payload={"text": text, "action_id": action_request.action_id},
        source_event_id=running_event_uid,
        correlation={"action_id": action_request.action_id},
    )
    payload: dict[str, Any] = {"memo_event_uid": memo_event.event_uid, "text": text}
    tool_output_str = tool_result(payload)
    terminalize_action(
        conn,
        event_type="action.result_observed",
        payload={
            "action_id": action_request.action_id,
            "semantics": "ack",
            "tool_output": tool_output_str,
        },
        source_event_id=running_event_uid,
        correlation={"action_id": action_request.action_id},
    )
    lifecycle.transition(action_request.action_id, "result_observed")
    return RawResult(
        action_id=action_request.action_id,
        semantics="ack",
        payload=payload,
        tool_output=tool_output_str,
        error=None,
    )


def list_memos_handler(
    action_request: ActionRequest,
    conn: sqlite3.Connection,
    runtime_paths: RuntimePathsLike,  # noqa: ARG001 — signature uniformity.
    lifecycle: ActionLifecycle,
) -> RawResult:
    """Fold every `memo.captured` event into a numbered, dated list."""
    running_event_uid = _get_running_event_uid(conn, action_request.action_id)
    lines: list[str] = []
    for event in iter_events_of_types(conn, ("memo.captured",)):
        stamp = datetime.fromtimestamp(event.ts_epoch_ms / 1000).astimezone()
        lines.append(f"{len(lines) + 1}. [{stamp:%m-%d %H:%M}] {event.payload.get('text', '')}")
    payload: dict[str, Any] = {
        "count": len(lines),
        "rendered": "\n".join(lines) if lines else "还没有备忘录。",
    }
    tool_output_str = tool_result(payload)
    terminalize_action(
        conn,
        event_type="action.result_observed",
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


_SEARCH_RECORDS_INPUT_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "keyword": {
            "type": "string",
            "description": (
                "Substring to match in the record text (case-insensitive; "
                "Chinese works as-is). Omit to match every record."
            ),
        },
        "from": {
            "type": "string",
            "description": (
                "Earliest timestamp, ISO 8601 with UTC offset, "
                "e.g. 2026-09-08T00:00:00-04:00."
            ),
        },
        "to": {
            "type": "string",
            "description": "Latest timestamp, ISO 8601 with UTC offset.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "description": "Maximum rows to return, newest first. Default 20.",
        },
    },
    "required": [],
}


def _make_search_records_handler(db_path: Path) -> ToolHandler:
    """Bind `search_records` to the memory.db path; observation semantics."""

    def _handler(
        action_request: ActionRequest,
        conn: sqlite3.Connection,
        runtime_paths: RuntimePathsLike,  # noqa: ARG001 — signature uniformity.
        lifecycle: ActionLifecycle,
    ) -> RawResult:
        running_event_uid = _get_running_event_uid(conn, action_request.action_id)
        args = action_request.arguments
        keyword = args.get("keyword")
        from_ts = args.get("from")
        to_ts = args.get("to")
        limit = args.get("limit")
        rows = search_memory_records(
            db_path,
            keyword=keyword.strip() if isinstance(keyword, str) and keyword.strip() else None,
            from_ts=from_ts if isinstance(from_ts, str) and from_ts.strip() else None,
            to_ts=to_ts if isinstance(to_ts, str) and to_ts.strip() else None,
            limit=limit if isinstance(limit, int) and limit > 0 else DEFAULT_SEARCH_LIMIT,
        )
        lines = [f"[{ts}] {source}: {text}" for ts, source, text in rows]
        payload: dict[str, Any] = {
            "count": len(rows),
            "rendered": "\n".join(lines) if lines else "没有找到匹配的记录。",
        }
        tool_output_str = tool_result(payload)
        terminalize_action(
            conn,
            event_type="action.result_observed",
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

    return _handler


def _memo_error(
    action_request: ActionRequest,
    conn: sqlite3.Connection,
    lifecycle: ActionLifecycle,
    running_event_uid: str | None,
    error: str,
) -> RawResult:
    tool_output_str = tool_error(error)
    terminalize_action(
        conn,
        event_type="action.failed",
        payload={"action_id": action_request.action_id, "error": error},
        source_event_id=running_event_uid,
        correlation={"action_id": action_request.action_id},
    )
    lifecycle.transition(action_request.action_id, "failed")
    return RawResult(
        action_id=action_request.action_id,
        semantics="error",
        payload={},
        tool_output=tool_output_str,
        error=error,
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
        return _emit_tool_error(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_request.action_id,
            running_event_uid=running_event_uid,
            code="invalid_argument",
            message=f"open_path: {exc}",
        )

    target = resolve_path_target(args.query, args.target_kind, conn)
    if target is None:
        return _emit_tool_error(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_request.action_id,
            running_event_uid=running_event_uid,
            code="target_not_found",
            message=f"open_path: no file/folder matched {args.query!r}",
        )

    # ADR-0011 D4: `open_path` is an `entity.resolved` EMITTER (it keeps
    # its own resolve-then-act contract per D2's footnote rather than
    # going through resolve-on-propose) — a successful resolution here
    # feeds the EntityRegistry projection's `file:` route the same way a
    # `read_file`-style pre-gate resolution would. `path_resolver` itself
    # stays pure; this emission lives in the handler, same as the
    # `action.result_observed` emission below.
    emit_event(
        conn,
        type="entity.resolved",
        payload={
            "entity_type": "file",
            "natural_ref": args.query,
            "resolved_to": f"file:{target.path}",
            "confidence": "bookmark" if target.source == "bookmark" else "fuzzy",
            "candidates": [],
            "match_basis": target.source,
            "outcome": "resolved",
        },
        source_event_id=running_event_uid,
        correlation={"action_id": action_request.action_id},
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
        return _emit_tool_error(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_request.action_id,
            running_event_uid=running_event_uid,
            code="open_failed",
            message=f"open_path: subprocess failed to start: {exc}",
        )

    if proc.returncode != 0:
        stderr_tail = (proc.stderr or "").strip()[-_OUTPUT_TAIL_BYTES:]
        return _emit_tool_error(
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

    terminalize_action(
        conn,
        event_type="action.result_observed",
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


# --- ADR-0011 D5 read-only tools (search_notes / read_file / read_clipboard) -
#
# All three are L0, sync, `result_semantics="observation"`. Two shared
# helpers below cover the "emit exactly one action.result_observed +
# terminal-transition lifecycle" contract every sync handler in this
# module follows (see `list_tasks_handler` for the observation shape).
# `_emit_tool_error` also backs `open_path_handler`'s failure exits —
# its body used to be duplicated there verbatim as `_open_path_error`;
# that duplicate is gone, `open_path_handler` calls this one directly.


def _emit_tool_observation(  # noqa: PLR0913 — all kwargs are the shared sync-handler success shape (conn/lifecycle/action_id/running_event_uid/payload/semantics); splitting them into a bundle defeats the point of a shared helper, same rationale as `_emit_tool_error`.
    *,
    conn: sqlite3.Connection,
    lifecycle: ActionLifecycle,
    action_id: str,
    running_event_uid: str,
    payload: Mapping[str, Any],
    semantics: ResultSemantics = "observation",
) -> RawResult:
    """Shared success exit for the ADR-0011 D5 tools — observations AND acks alike.

    NIT-FIX 8, ADR-0011 §12: despite the name, this is not
    observation-only. ``semantics`` defaults to ``"observation"`` (every D5 tool but
    ``open_url``, ADR-0011 §3 D5's result_semantics column). ``open_url``
    passes ``semantics="ack"`` — it is the one D5 tool whose result is an
    Execution Claim ``executed`` rather than an observation (the browser
    opening is not verified) — so it shares this same emit+transition
    shape instead of duplicating it.
    """
    tool_output_str = tool_result(payload)
    terminalize_action(
        conn,
        event_type="action.result_observed",
        payload={
            "action_id": action_id,
            "semantics": semantics,
            "tool_output": tool_output_str,
        },
        source_event_id=running_event_uid,
        correlation={"action_id": action_id},
    )
    lifecycle.transition(action_id, "result_observed")
    return RawResult(
        action_id=action_id,
        semantics=semantics,
        payload=payload,
        tool_output=tool_output_str,
        error=None,
    )


_TOOL_ERROR_MESSAGE_MAX_BYTES: Final[int] = 8192
"""Same 8 KiB order as every other D5 tool's output cap (MUST-FIX 1b,
ADR-0011 §12). `_emit_tool_error` is the single choke point every sync
handler's failure exit goes through — capping HERE, once, means no
handler can write an unbounded `tool_output` / `action.result_observed`
row by interpolating a raw exception's `str()` verbatim (the uncapped
`f"...: {exc}"` idiom appears at half a dozen call sites; this bounds
all of them without touching any of them individually)."""


def _emit_tool_error(  # noqa: PLR0913 — all kwargs are the shared sync-handler failure shape (conn/lifecycle/action_id/running_event_uid/code/message); splitting them into a bundle defeats the point of a shared helper.
    *,
    conn: sqlite3.Connection,
    lifecycle: ActionLifecycle,
    action_id: str,
    running_event_uid: str,
    code: str,
    message: str,
) -> RawResult:
    """Shared error exit: `action.result_observed(error)` + terminal-transition.

    Used by the three ADR-0011 D5 tools AND `open_path_handler` — same
    "emit exactly one action.result_observed, transition the lifecycle
    terminal, on every exit path" contract every sync L4 handler in
    this module follows (the invariant `_get_running_event_uid`
    documents at its call sites).

    `message` is capped at `_TOOL_ERROR_MESSAGE_MAX_BYTES` (MUST-FIX 1b,
    ADR-0011 §12 / spec §3.5.11's bounded-payload rule) — a handler
    that interpolates a raw exception's `str()` can otherwise write an
    unbounded row into durable state (an upstream that echoes its
    request body, or simply a pathological error message).
    """
    message_bytes = message.encode("utf-8")
    if len(message_bytes) > _TOOL_ERROR_MESSAGE_MAX_BYTES:
        capped_text, undelivered_bytes, _lossy = truncate_utf8(
            message_bytes[:_TOOL_ERROR_MESSAGE_MAX_BYTES], len(message_bytes),
        )
        message = f"{capped_text}…[truncated {undelivered_bytes} bytes]"
    tool_output_str = tool_error(message, code=code)
    terminalize_action(
        conn,
        event_type="action.result_observed",
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


# --- search_notes ------------------------------------------------------------

_SEARCH_NOTES_DEFAULT_MAX_RESULTS: Final[int] = 5
_SEARCH_NOTES_MAX_RESULTS_CAP: Final[int] = 10

_SEARCH_NOTES_LINE_MAX_BYTES: Final[int] = 512
"""Per-matched-line cap (MUST-FIX 1, ADR-0011 §12). A vault note can
contain one pathological line (e.g. a pasted JSON blob, measured at
2,000,000+ chars in the wild) many times larger than any sane search
snippet; without this cap that ONE line would consume the entire
``_SEARCH_NOTES_TOTAL_MAX_BYTES`` budget below and crowd out every
other match. Codepoint-safe cut via `jarvis.shared.text.truncate_utf8`."""

_SEARCH_NOTES_TOTAL_MAX_BYTES: Final[int] = 8192
"""Aggregate cap across all returned rows — same 8 KiB order as the
`read_file`/`read_clipboard` sibling caps. The per-line cap above
already bounds the worst case to roughly
``_SEARCH_NOTES_MAX_RESULTS_CAP * _SEARCH_NOTES_LINE_MAX_BYTES`` (10 *
512 = 5120 today), but this is the hard ceiling that still holds if
either of those two numbers changes independently later."""

DEFAULT_OBSIDIAN_VAULT_ROOT: Final[Path] = Path("~/Documents/Obsidian Vault").expanduser()
"""Fallback vault root when the composition root passes none in (e.g. the
two existing integration tests that call ``build_default_registry()``
with no arguments). Production always overrides this with
``tools.obsidian.vault_root`` (ADR-0011 D7, `jarvis.runtime`).

NIT-FIX 8, ADR-0011 §12: single source of truth — `jarvis.runtime`
imports this constant (an L4 -> higher-layer import direction
`.importlinter` allows) rather than holding its own copy of the
literal, so the two layers can't drift apart."""

_SEARCH_NOTES_INPUT_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Case-insensitive search text. Whitespace-separated tokens "
                "must ALL appear in a line for it to match."
            ),
        },
        "max_results": {
            "type": "integer",
            "minimum": 1,
            "description": "Maximum number of matching lines to return. Default 5, capped at 10.",
        },
    },
    "required": ["query"],
}


def _resolved_within(path: Path, root: Path) -> bool:
    """Containment check: is ``path`` (resolved) under ``root`` (already resolved)?

    Mirrors `jarvis.execution.path_resolver._under_home` — resolve
    first (following symlinks), then test — so a symlinked ``*.md``
    that points outside ``root`` is caught even though 3.12's
    ``rglob`` already refuses to descend into a symlinked *directory*
    on its own (SHOULD-FIX 6, ADR-0011 §12).
    """
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved == root or root in resolved.parents


def _cap_rows_total_bytes(rows: list[str], max_bytes: int) -> list[str]:
    """Enforce an aggregate output cap across rendered rows (MUST-FIX 1).

    The caller's per-row cap already bounds any single row; this guards
    the SUM. Walks ``rows`` in order, keeping whole rows until the next
    one would exceed ``max_bytes``, then truncates that ONE row
    (codepoint-safe, same marker convention as the per-row cap) and
    drops everything after it.

    Shared by ``search_notes`` and ``web_search`` — both render a list
    of rows whose count is bounded but whose total size is not.
    """
    capped: list[str] = []
    used = 0
    for row in rows:
        row_bytes = row.encode("utf-8")
        if used + len(row_bytes) <= max_bytes:
            capped.append(row)
            used += len(row_bytes)
            continue
        # The marker is part of the output, so it has to come OUT of the
        # remaining budget rather than be appended past it — otherwise
        # the sum overshoots `max_bytes` by the marker's own length
        # every time the cap fires. Size the reservation with
        # `len(row_bytes)`: the real `undelivered` is always <= that, so
        # it never renders more digits than budgeted for.
        remaining = max_bytes - used
        marker_budget = len(f"…[truncated {len(row_bytes)} bytes]".encode())
        keep = remaining - marker_budget
        if keep > 0:
            text, undelivered, _lossy = truncate_utf8(row_bytes[:keep], len(row_bytes))
            capped.append(f"{text}…[truncated {undelivered} bytes]")
        break
    return capped


def _make_search_notes_handler(vault_root: Path) -> ToolHandler:  # noqa: C901 — one linear validate/search/rank pass; splitting scatters the fail-fast checks from the loop they guard.
    """Bind ``vault_root`` into a ``search_notes`` handler closure (ADR-0011 D7).

    ``search_notes`` is the only ADR-0011 D5 tool that needs a config
    value the ``ActionRequest`` / ``RuntimePathsLike`` surface doesn't
    already carry. Per the ADR-0011 build brief, L4 handlers do not
    load YAML themselves — the composition root
    (``jarvis.runtime.bootstrap_runtime_app``) reads
    ``tools.obsidian.vault_root`` once at registry-build time and
    closes over it here, the same seam a test uses to point the tool
    at a ``tmp_path`` fixture instead of Allen's real vault.

    No index (spec: vault is small) — every call walks the tree fresh.
    A missing vault root is an empty result with a note, not an error
    (Allen may rename the vault). A matched ``*.md`` that resolves
    outside ``vault_root`` (a symlink escape) is silently skipped
    (SHOULD-FIX 6). A note whose bytes are not valid UTF-8 is skipped
    from line-matching and its path collected under
    ``payload["non_utf8_files"]`` instead of narrating mojibake
    (SHOULD-FIX 7).
    """

    def _handler(  # noqa: C901, PLR0912 — one linear validate/search/rank pass (containment + encoding + per-line + total-cap guards all live in the loop they protect); splitting them out scatters the fail-fast checks from the data they guard, same rationale as the outer function's own noqa.
        action_request: ActionRequest,
        conn: sqlite3.Connection,
        runtime_paths: RuntimePathsLike,  # noqa: ARG001 — signature uniformity.
        lifecycle: ActionLifecycle,
    ) -> RawResult:
        running_event_uid = _get_running_event_uid(conn, action_request.action_id)
        action_id = action_request.action_id

        query = action_request.arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            return _emit_tool_error(
                conn=conn,
                lifecycle=lifecycle,
                action_id=action_id,
                running_event_uid=running_event_uid,
                code="invalid_argument",
                message=f"search_notes: query must be a non-empty string (got {query!r})",
            )

        max_results = _SEARCH_NOTES_DEFAULT_MAX_RESULTS
        max_results_arg = action_request.arguments.get("max_results")
        if (
            isinstance(max_results_arg, (int, float))
            and not isinstance(max_results_arg, bool)
            and math.isfinite(max_results_arg)
        ):
            # SHOULD-FIX 6, ADR-0011 §12: `json.loads` accepts `Infinity`/
            # `NaN` in the LLM tool-arg path, and `int(inf)` raises
            # OverflowError — the `isfinite` guard folds that shape into
            # "ignore, use the default", same as any other malformed
            # `max_results_arg` this branch already ignores silently.
            max_results = int(max_results_arg)
        max_results = max(1, min(max_results, _SEARCH_NOTES_MAX_RESULTS_CAP))

        if not vault_root.is_dir():
            payload: dict[str, Any] = {
                "results": [],
                "note": f"vault root not found: {vault_root} (Allen may have renamed it)",
            }
            return _emit_tool_observation(
                conn=conn,
                lifecycle=lifecycle,
                action_id=action_id,
                running_event_uid=running_event_uid,
                payload=payload,
            )

        tokens = [tok.lower() for tok in query.split() if tok]
        results: list[str] = []
        non_utf8_files: list[str] = []
        vault_root_resolved = vault_root.resolve()
        for md_path in sorted(vault_root.rglob("*.md")):
            if len(results) >= max_results:
                break
            if not _resolved_within(md_path, vault_root_resolved):
                continue  # SHOULD-FIX 6: symlinked *.md escaping the vault.
            try:
                raw = md_path.read_bytes()
            except OSError:
                continue
            text, _undelivered, lossy = truncate_utf8(raw, len(raw))
            if lossy:
                # SHOULD-FIX 7: don't narrate mojibake as real content —
                # flag the file instead of returning garbled lines.
                non_utf8_files.append(str(md_path))
                continue
            for line in text.splitlines():
                if len(results) >= max_results:
                    break
                if not all(tok in line.lower() for tok in tokens):
                    continue
                line_bytes = line.strip().encode("utf-8")
                line_text, line_undelivered, _lossy = truncate_utf8(
                    line_bytes[:_SEARCH_NOTES_LINE_MAX_BYTES], len(line_bytes),
                )
                if line_undelivered > 0:
                    line_text += f"…[truncated {line_undelivered} bytes]"
                results.append(f"{md_path} — {line_text}")

        results = _cap_rows_total_bytes(results, _SEARCH_NOTES_TOTAL_MAX_BYTES)
        payload = {"results": results}
        if non_utf8_files:
            payload["non_utf8_files"] = non_utf8_files
        return _emit_tool_observation(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_id,
            running_event_uid=running_event_uid,
            payload=payload,
        )

    return _handler


# --- read_file -----------------------------------------------------------------

_READ_FILE_MAX_BYTES: Final[int] = 8192
"""Output cap (8 KiB), ADR-0011 D5. A per-tool module constant, not a
schema field (§11 defer table: ``max_output_bytes`` deferred)."""

_READ_FILE_ENTITY_PREFIX: Final[str] = "file:"

_READ_FILE_INPUT_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "target": {
            "type": "string",
            "description": (
                "Spoken name or description of the file to read. Resolved to a "
                "canonical path before reading (never used as a raw path)."
            ),
        },
    },
    "required": ["target"],
}


def read_file_handler(
    action_request: ActionRequest,
    conn: sqlite3.Connection,
    runtime_paths: RuntimePathsLike,  # noqa: ARG001 — signature uniformity.
    lifecycle: ActionLifecycle,
) -> RawResult:
    """L0 `read_file` — the first `requires_entity=True` tool (ADR-0011 D5).

    Reads the RESOLVED canonical path off
    `action_request.target_entity_ref` — NEVER `arguments["target"]`,
    the raw free text the LLM supplied. That is the entire point of
    D4's resolve-on-propose contract and invariant I2 (no fabricated
    entity IDs): by the time this handler runs, the Pre-action Gate has
    already required a trusted ``"file:<abs-path>"`` ref, so the raw
    argument is never even read here.

    `target_entity_ref is None` cannot pass the gate for a
    `requires_entity=True` tool (ADR-0011 D3), but this handler checks
    it anyway — a handler must be safe standing alone, not merely
    behind a gate that happens to always run first in production.

    Text files only: a NUL byte anywhere in the `_READ_FILE_MAX_BYTES`
    prefix marks the file binary (same heuristic `git` uses) and the
    handler refuses rather than returning decoded garbage. Output is
    capped at 8 KiB, cut at the last valid UTF-8 codepoint boundary
    (never mid-character), with an explicit ``…[truncated N bytes]``
    marker appended when the file is larger. A file whose bytes are
    not valid UTF-8 at all (e.g. GBK) still returns an observation —
    best-effort lossy-decoded — but with `payload["encoding"] ==
    "lossy"` so the caller knows not to trust it as real text
    (SHOULD-FIX 7, ADR-0011 §12).
    """
    running_event_uid = _get_running_event_uid(conn, action_request.action_id)
    action_id = action_request.action_id

    ref = action_request.target_entity_ref
    if ref is None or not ref.startswith(_READ_FILE_ENTITY_PREFIX):
        return _emit_tool_error(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_id,
            running_event_uid=running_event_uid,
            code="no_resolved_target",
            message="read_file: no resolved target_entity_ref (the gate should have refused this)",
        )

    path = Path(ref[len(_READ_FILE_ENTITY_PREFIX) :])
    if not path.is_file():
        return _emit_tool_error(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_id,
            running_event_uid=running_event_uid,
            code="file_not_found",
            message=f"read_file: {path} does not exist or is not a regular file",
        )

    try:
        total_bytes = path.stat().st_size
        with path.open("rb") as fh:
            prefix = fh.read(_READ_FILE_MAX_BYTES)
    except OSError as exc:
        return _emit_tool_error(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_id,
            running_event_uid=running_event_uid,
            code="read_failed",
            message=f"read_file: {exc}",
        )

    if b"\x00" in prefix:
        return _emit_tool_error(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_id,
            running_event_uid=running_event_uid,
            code="binary_file",
            message=f"read_file: {path} looks binary, refusing to read it as text",
        )

    text, undelivered_bytes, lossy = truncate_utf8(prefix, total_bytes)
    if undelivered_bytes > 0:
        text += f"…[truncated {undelivered_bytes} bytes]"

    payload: dict[str, Any] = {
        "path": str(path),
        "content": text,
        "truncated": undelivered_bytes > 0,
        "total_bytes": total_bytes,
    }
    if lossy:
        # SHOULD-FIX 7: `path` is not valid UTF-8 (e.g. GBK) — tell the
        # caller explicitly rather than shipping a clean-looking
        # observation full of mojibake the LLM would narrate as real
        # content.
        payload["encoding"] = "lossy"
    return _emit_tool_observation(
        conn=conn,
        lifecycle=lifecycle,
        action_id=action_id,
        running_event_uid=running_event_uid,
        payload=payload,
    )


# --- read_clipboard --------------------------------------------------------------

_CLIPBOARD_MAX_BYTES: Final[int] = 8192
_CLIPBOARD_TIMEOUT_S: Final[float] = 5.0


def read_clipboard_handler(
    action_request: ActionRequest,
    conn: sqlite3.Connection,
    runtime_paths: RuntimePathsLike,  # noqa: ARG001 — signature uniformity.
    lifecycle: ActionLifecycle,
) -> RawResult:
    """L0 `read_clipboard` — `pbpaste`, capped at 8 KiB (ADR-0011 D5).

    Zero arguments. An empty clipboard is a VALID empty observation,
    not an error — Allen's clipboard being empty is itself an answer,
    not a tool failure.
    """
    running_event_uid = _get_running_event_uid(conn, action_request.action_id)
    action_id = action_request.action_id

    try:
        proc = subprocess.run(
            ["pbpaste"],  # noqa: S607 — `pbpaste` resolved via PATH, matches mdfind/open precedent.
            timeout=_CLIPBOARD_TIMEOUT_S,
            capture_output=True,
            text=False,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _emit_tool_error(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_id,
            running_event_uid=running_event_uid,
            code="clipboard_read_failed",
            message=f"read_clipboard: pbpaste failed to run: {exc}",
        )

    if proc.returncode != 0:
        stderr_tail = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        return _emit_tool_error(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_id,
            running_event_uid=running_event_uid,
            code="clipboard_read_failed",
            message=f"read_clipboard: pbpaste exited {proc.returncode}: {stderr_tail}",
        )

    raw = proc.stdout or b""
    total_bytes = len(raw)
    text, undelivered_bytes, _lossy = truncate_utf8(raw[:_CLIPBOARD_MAX_BYTES], total_bytes)
    if undelivered_bytes > 0:
        text += f"…[truncated {undelivered_bytes} bytes]"

    payload: dict[str, Any] = {
        "content": text,
        "truncated": undelivered_bytes > 0,
        "total_bytes": total_bytes,
    }
    return _emit_tool_observation(
        conn=conn,
        lifecycle=lifecycle,
        action_id=action_id,
        running_event_uid=running_event_uid,
        payload=payload,
    )


# --- SSRF egress guard (ADR-0011 D5) -----------------------------------------
#
# Shared by `web_fetch` and `open_url` — both hand a URL Allen (or the LLM)
# did not type into a terminal to something that reaches the network / the
# GUI. Refusing here is an in-handler guard refuse (ADR-0011 §5: "not a gate
# refuse — the URL is an argument, not an entity"), never a gate-level
# `requires_entity` concern and never an uncaught exception.

_ALLOWED_URL_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})

_IPV4_LITERAL_CHARS: Final[frozenset[str]] = frozenset("0123456789.")
"""Character set of an IPv4-dotted-literal-*shaped* host (MUST-FIX 1,
ADR-0011 §12) — deliberately includes bare-digit strings (`"0"`,
`"2130706433"`) and short forms (`"127.1"`), not just 4-part dotted
quads. Every one of those shapes is exactly what a browser's WHATWG URL
parser recognizes as "ends in a number" and converts to an IPv4 address
(decimal, or octal per-part on a leading zero) — never as a DNS
hostname. Treating them the same way here (strict-parse-or-refuse,
never fall through to `getaddrinfo`) is what closes the bypass."""


def _strict_ip_literal(
    host: str,
) -> tuple[bool, ipaddress.IPv4Address | ipaddress.IPv6Address | None]:
    """Return `(is_literal_shaped, addr)`.

    `is_literal_shaped=True, addr=None` means `host` LOOKS like an IP
    literal — digits-and-dots only, or contains a `:` (bracketed IPv6)
    — but failed a STRICT `ipaddress` parse. `0177.0.0.1` is exactly
    this: `socket.getaddrinfo` reads the leading zero as decimal
    (`177.0.0.1`, public) while every WHATWG URL parser (every browser,
    and `open_url`'s downstream `open`) reads it as octal (`127.0.0.1`,
    loopback) — CPython's `ipaddress.IPv4Address` has rejected leading
    zeros since 3.9.5, which is exactly the WHATWG-compatible reading.
    A literal-shaped host must never reach `getaddrinfo`, which would
    silently paper over the ambiguity in the resolver's favour.

    `is_literal_shaped=False` means `host` is an ordinary DNS hostname
    — untouched, resolved via `getaddrinfo` as before.

    IPv6 zone suffixes (`fe80::1%en0`) are stripped at the first literal
    `%` before parsing — `ipaddress.IPv6Address` rejects them outright,
    and the zone plays no part in which address a scoped literal names.
    """
    if host and set(host) <= _IPV4_LITERAL_CHARS:
        try:
            return True, ipaddress.IPv4Address(host)
        except ValueError:
            return True, None
    if ":" in host:
        try:
            return True, ipaddress.IPv6Address(host.split("%", 1)[0])
        except ValueError:
            return True, None
    return False, None


def _resolve_hostname_ips(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve `host` to its address(es), literal IPs parsed directly.

    Everything else goes via `socket.getaddrinfo` (real DNS). This is
    the default `resolver` `validate_egress_url` closes over; the
    ADR-0011 §7 SSRF acceptance table substitutes a canned mapping here
    instead (a hostname with both a public and a private A record cannot
    be reproduced against real DNS in a hermetic test).

    An IP-literal-shaped host (`_strict_ip_literal`) is parsed strictly
    and NEVER handed to `getaddrinfo` (MUST-FIX 1, ADR-0011 §12) — a
    literal that fails strict parsing returns an empty list (refused)
    rather than falling through to the resolver's own, looser
    normalization of legacy forms (decimal `2130706433`, hex
    `0x7f000001`, mixed `0x7f.0.0.1` are NOT digits-and-dots-only shaped
    and are unaffected — `getaddrinfo` already normalizes those to
    their dotted form identically to a browser, so no bypass exists
    there). A host that fails to resolve at all returns an empty list
    (the guard then refuses — an unresolvable host cannot be proven
    safe).
    """
    is_literal_shaped, literal_addr = _strict_ip_literal(host)
    if is_literal_shaped:
        return [] if literal_addr is None else [literal_addr]
    try:
        infos = socket.getaddrinfo(host, None)
    except (OSError, UnicodeError):
        return []
    addrs: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    seen: set[str] = set()
    for info in infos:
        raw_ip = str(info[4][0])
        if raw_ip in seen:
            continue
        seen.add(raw_ip)
        try:
            addrs.append(ipaddress.ip_address(raw_ip.split("%", 1)[0]))
        except ValueError:
            continue
    return addrs


_NAT64_WELL_KNOWN_PREFIX: Final[ipaddress.IPv6Network] = ipaddress.IPv6Network("64:ff9b::/96")
"""RFC 6052 NAT64 well-known prefix. An address in this block is a
translated IPv4 address embedded in the low 32 bits — judging the /96
prefix itself (`is_global` says True; it is not IANA special-purpose
"reserved" in any way `ipaddress` models cheaply) tells you nothing;
judging the EMBEDDED address is the only thing that means anything
(MUST-FIX 4, ADR-0011 §12)."""


def _is_globally_routable(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Allowlist predicate — permit only a globally routable address.

    MUST-FIX 4, ADR-0011 §12. Inverted from the original denylist enumeration
    (loopback/link-local/private/unspecified), which left
    `100.64.0.0/10` — CGNAT, and the exact range Allen's Tailscale
    tailnet (including the RPi that runs Jarvis) lives on — reachable
    purely by omission: `ipaddress` classifies it as neither
    `is_loopback`, `is_link_local`, `is_private`, nor `is_unspecified`.

    `is_global` alone is not sufficient either — empirically (not just per
    the stdlib docs) it says `True` for: IPv4/IPv6 multicast
    (`224.0.0.1`, `ff02::1`), the deprecated IPv6 site-local block
    (`fec0::1`, `fec0::/10`), and any address in the NAT64 well-known
    prefix REGARDLESS of what it embeds (`64:ff9b::7f00:1` embeds
    `127.0.0.1`). Each gets an explicit refusal below; the NAT64 case
    recurses on the embedded IPv4 address instead of trusting the /96
    prefix, so a real NAT64-translated public address is not refused
    just for sharing the prefix.

    An IPv4-mapped IPv6 address (`::ffff:127.0.0.1`) is unwrapped to its
    IPv4 form first so it is judged by the exact same rule as the
    literal it represents — empirically `is_global` already agrees with
    the unwrapped form for every address family tried here, but the
    unwrap is kept as the documented, not-accidental, invariant.
    """
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if isinstance(addr, ipaddress.IPv6Address) and addr in _NAT64_WELL_KNOWN_PREFIX:
        embedded = ipaddress.IPv4Address(addr.packed[-4:])
        return _is_globally_routable(embedded)
    if addr.is_multicast:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.is_site_local:
        return False
    return addr.is_global


def _is_blocked_address(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for anything NOT globally routable.

    See `_is_globally_routable` (MUST-FIX 4, ADR-0011 §12) for the
    allowlist this inverts.
    """
    return not _is_globally_routable(addr)


@dataclass(frozen=True)
class _EgressUrlParts:
    """Scheme/host/port plus the canonical re-serialized URL string."""

    scheme: str
    host: str
    port: int | None
    canonical: str


def _parse_egress_url(url: str) -> tuple[_EgressUrlParts | None, str]:
    r"""Parse `url` into `(parts, reason)` — `parts is None` iff refused.

    `reason` names why, used verbatim by `validate_egress_url`.

    This is the ROOT-CAUSE fix (ADR-0011 §12 MUST-FIX 1/2): the bug
    class underlying both fixes is "the validated string is not the
    used string" — `validate_egress_url` parses with `urlsplit`, then
    the ORIGINAL string gets handed to a second parser (macOS
    LaunchServices/the browser for `open_url`, `httpx` for `web_fetch`)
    that can read it differently (backslash-as-slash in a WHATWG
    "special scheme" authority is the concrete case: `urlsplit` puts
    everything before the last `\@` into userinfo and the rest into
    host; the browser reinterprets the backslash as a path separator
    and lands on a completely different host).

    `parts.canonical` is rebuilt from ONLY the parsed scheme/host/port/
    path/query — userinfo and fragment are dropped unconditionally,
    never forwarded to `open` or `httpx`. `validate_egress_url` and
    `_normalize_egress_url` both call this SAME function, so the string
    a caller ends up acting on is always byte-identical to the one the
    guard's scheme/address checks ran against.

    A URL that does not survive a re-parse of its own canonical form
    with IDENTICAL scheme/host/port is refused outright (`parts is
    None`) rather than trusted — this is the literal "anything that
    does not survive parse → re-serialize → reparse identically is
    refused" rule; in practice this never fires for a legitimate URL
    (host/port can't contain any of the delimiters being reassembled
    around them) and exists as a named invariant, not a vector this
    project has observed.
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None, f"malformed URL {url!r}"

    host = parsed.hostname
    if not host:
        return None, "URL has no hostname"

    try:
        port = parsed.port
    except ValueError as exc:
        return None, f"invalid port in URL: {exc}"

    scheme = parsed.scheme.lower()
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    canonical = urlunsplit((scheme, netloc, parsed.path or "/", parsed.query, ""))

    reparsed = urlsplit(canonical)
    try:
        reparsed_port = reparsed.port
    except ValueError:
        return None, "URL does not survive canonical re-serialization (port)"
    if reparsed.scheme != scheme or reparsed.hostname != host or reparsed_port != port:
        return None, "URL does not survive canonical re-serialization (host/scheme mismatch)"

    return _EgressUrlParts(scheme=scheme, host=host, port=port, canonical=canonical), ""


def _normalize_egress_url(url: str) -> str | None:
    """Canonical re-serialized form of an already-guard-allowed `url`.

    The ONLY string a call site may act on (ADR-0011 §12 MUST-FIX 1/2
    root cause; see `_parse_egress_url`). `None` means `url` cannot be
    canonicalized — should not happen for a URL `validate_egress_url`
    just allowed (both call the identical parser), but callers check
    for it and refuse rather than ever fall back to the raw string.
    """
    parts, _reason = _parse_egress_url(url)
    return parts.canonical if parts is not None else None


def validate_egress_url(
    url: str,
    *,
    resolver: Callable[
        [str], Sequence[ipaddress.IPv4Address | ipaddress.IPv6Address]
    ] = _resolve_hostname_ips,
) -> tuple[bool, str]:
    """SSRF egress guard shared by `web_fetch` and `open_url` (ADR-0011 D5).

    Pure aside from the injected `resolver` (real DNS by default; the
    acceptance script substitutes a canned lookup). Returns
    `(allowed, reason)` — `reason` is `""` when `allowed` and otherwise
    names exactly what tripped, for the caller's error observation.

    Checks, in order (each is a real bypass if skipped, per ADR-0011 D5):

    1. The URL must parse into a canonical form that survives its own
       re-parse (`_parse_egress_url`) — a malformed URL, one with no
       hostname, or an unparseable port is refused before anything
       else is inspected.
    2. Scheme must be `http`/`https`. `file:`/`ftp:`/`data:`/
       `javascript:`/anything else is refused — this matters most for
       `open_url`, where macOS `open` will happily launch a non-http
       scheme.
    3. EVERY address `resolver` returns is checked, not just the first —
       a host with both a public and a private A record is refused.

    Callers that walk redirects (`web_fetch`) MUST call this again on
    every hop's `Location` — this function only ever sees one URL. A
    caller that gets `allowed=True` back MUST fetch/open
    `_normalize_egress_url(url)`, never `url` itself (see
    `_parse_egress_url`'s docstring for why).
    """
    parts, reason = _parse_egress_url(url)
    if parts is None:
        return False, reason

    if parts.scheme not in _ALLOWED_URL_SCHEMES:
        return False, f"scheme {parts.scheme!r} is not in the http/https allowlist"

    addrs = resolver(parts.host)
    if not addrs:
        return False, f"host {parts.host!r} did not resolve to any address"

    for addr in addrs:
        if _is_blocked_address(addr):
            return False, f"host {parts.host!r} resolves to blocked address {addr}"

    return True, ""


# --- web_search ----------------------------------------------------------------

DEFAULT_WEB_SEARCH_MAX_RESULTS: Final[int] = 5
"""Default `max_results` absent both a request argument and a
`tools.web.search_max_results` config override (ADR-0011 D7). Public so
`jarvis.runtime` can fall back to it — same single-source-of-truth
reasoning as `DEFAULT_OBSIDIAN_VAULT_ROOT` (NIT-FIX 8, ADR-0011 §12)."""

_WEB_SEARCH_MAX_RESULTS_CAP: Final[int] = 8
"""Hard ceiling (ADR-0011 D5) — NOT configurable, unlike the default
above. Applies regardless of what the request argument or config asks
for."""

DEFAULT_WEB_SEARCH_PROVIDER: Final[str] = "ddgs"
"""Default `tools.web.search_provider`.

`ddgs` (unofficial DuckDuckGo scraping) stays the DEFAULT only because
it needs no credential — it is not the recommended choice. It returns
link-plus-blurb, so any actual content costs a second `web_fetch`
iteration, and it is routinely rate-limited or blocked outright: turn
Te3815a16 burned three of its five tool-loop iterations on it and got
`No results found` from the last one. Point this at `exa` or `tavily`
(with `search_api_key_env` set) and search returns page text directly,
which is both better grounded and cheaper in loop iterations."""

_WEB_SEARCH_RESULT_TEXT_CAP: Final[int] = 1500
"""Per-result text budget, in characters, requested from a text-bearing
provider and enforced locally regardless of what it returns. Bounds any
ONE result; `_WEB_SEARCH_TOTAL_MAX_BYTES` bounds the sum."""

_WEB_SEARCH_TOTAL_MAX_BYTES: Final[int] = 8192
"""Aggregate cap across all rendered rows — same 8 KiB order as every
other tool's output cap. Without it, 8 results x full page text would
dwarf the rest of the decision packet."""

DEFAULT_WEB_TIMEOUT_S: Final[float] = 20.0
"""Shared `web_search`/`web_fetch` timeout absent a `tools.web.timeout_s`
config override. ADR-0011 D5's prose gives the two tools DIFFERENT
timeouts (15s / 20s) but D7's shipped config schema has only ONE
`timeout_s` key under `tools.web` — deliberately unprefixed, unlike its
`search_max_results`/`fetch_max_bytes` siblings, which reads as intent
to share one knob. Step 6 resolves the conflict by applying the single
configured value to both tools (ADR-0011 §12 errata candidate)."""

_WEB_SEARCH_INPUT_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Search query text.",
        },
        "max_results": {
            "type": "integer",
            "minimum": 1,
            "description": "Maximum number of results to return. Default 5, capped at 8.",
        },
    },
    "required": ["query"],
}


def _ddgs_search_backend(
    query: str,
    max_results: int,
    *,
    timeout_s: float,
) -> list[tuple[str, str, str]]:
    """Real DuckDuckGo backend via the `ddgs` package (ADR-0011 D5).

    One function, as D5 explicitly asks for, so a keyed API is a
    config-sized swap later — nothing above this function knows or
    cares that DuckDuckGo is behind it. Returns `(title, url, snippet)`
    tuples. The acceptance script monkeypatches this name directly
    rather than mocking `ddgs` internals, so it never makes a real
    network call.
    """
    # Imported here, not at module top, so a `ddgs` import error surfaces as
    # this tool's own error observation rather than failing
    # `import jarvis.execution.tools` for every caller.
    from ddgs import DDGS  # noqa: PLC0415

    # NIT-FIX 8, ADR-0011 §12: `int(timeout_s)` floors any sub-1s config
    # value to 0 — `max(1, ...)` keeps a deliberately-short timeout at a
    # sane floor instead of silently becoming "no timeout" / "instant
    # timeout" depending on how `ddgs` treats `0`.
    with DDGS(timeout=max(1, int(timeout_s))) as client:
        rows = client.text(query, max_results=max_results)
    return [
        (str(row.get("title", "")), str(row.get("href", "")), str(row.get("body", "")))
        for row in rows
    ]


def _collapse_ws(text: str) -> str:
    """Squeeze runs of whitespace to single spaces.

    Extracted page text arrives with the source document's newlines and
    indentation intact. Those bytes count against
    `_WEB_SEARCH_RESULT_TEXT_CAP` while carrying no meaning, and one
    result rendered across many lines would also break the one-row-per-
    result shape the results list promises.
    """
    return " ".join(text.split())


class _KeyedSearchBackend(Protocol):
    """A `web_search` backend that needs a credential (Exa, Tavily)."""

    def __call__(
        self, query: str, max_results: int, *, timeout_s: float, api_key: str,
    ) -> list[tuple[str, str, str]]: ...


def _exa_search_backend(
    query: str,
    max_results: int,
    *,
    timeout_s: float,
    api_key: str,
) -> list[tuple[str, str, str]]:
    """Exa `/search` with page text inlined (`contents.text`).

    The point of a text-bearing provider is that ONE tool call answers
    "what does the web say about X" — the LLM does not have to spend a
    second tool-loop iteration on `web_fetch` to see any actual content.
    That matters directly: the loop bound is 5, and the search-then-
    fetch dance is what exhausted it in turn Te3815a16.

    `highlights` (Exa's query-relevant passages) is preferred over
    `text` (the page from its top) for the same reason Tavily's
    `content` beats its `raw_content`: against a per-result budget, a
    page's opening bytes are nav chrome, not the answer. That reasoning
    is EVIDENCED for Tavily and only INFERRED here — this backend has
    never run against a real Exa key, so treat the field preference as
    unverified.
    """
    response = httpx.post(
        "https://api.exa.ai/search",
        headers={"x-api-key": api_key},
        json={
            "query": query,
            "numResults": max_results,
            "contents": {
                "text": {"maxCharacters": _WEB_SEARCH_RESULT_TEXT_CAP},
                "highlights": True,
            },
        },
        timeout=timeout_s,
    )
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    return [
        (
            str(row.get("title") or ""),
            str(row.get("url") or ""),
            _exa_row_text(row),
        )
        for row in rows
        if isinstance(row, dict)
    ]


def _exa_row_text(row: Mapping[str, Any]) -> str:
    """Prefer an Exa row's `highlights` over its `text` (see caller)."""
    highlights = row.get("highlights")
    if isinstance(highlights, list) and highlights:
        return " … ".join(str(h) for h in highlights if h)
    return str(row.get("text") or "")


def _tavily_search_backend(
    query: str,
    max_results: int,
    *,
    timeout_s: float,
    api_key: str,
) -> list[tuple[str, str, str]]:
    """Tavily `/search`, taking its relevance-selected `content`.

    `content` is Tavily's query-relevant extract; `raw_content` is the
    whole parsed page. `raw_content` was tried first and was WRONG:
    against a per-result budget of `_WEB_SEARCH_RESULT_TEXT_CAP`, a
    page's opening bytes are nav chrome, so the budget bought
    "[Skip to content](#main) ![logo]…" while `content` for the same
    result read "operated by Air Canada and China Eastern Airlines and
    the flight time is 13 hours and 50 minutes" — the actual answer, at
    ~1.3 KB. So `content` is requested and preferred, `raw_content` is
    not asked for at all (it is billed payload we would discard), and
    the fallback stays only for a row that somehow carries one and not
    the other. A result whose full page IS needed can be handed to
    `web_fetch` — which, since the extract-then-cap fix, can read it.
    """
    response = httpx.post(
        "https://api.tavily.com/search",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"query": query, "max_results": max_results},
        timeout=timeout_s,
    )
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    return [
        (
            str(row.get("title") or ""),
            str(row.get("url") or ""),
            str(row.get("content") or row.get("raw_content") or ""),
        )
        for row in rows
        if isinstance(row, dict)
    ]


class _SearchBackend(Protocol):
    """Uniform call shape every `web_search` backend is adapted to."""

    def __call__(
        self, query: str, max_results: int, *, timeout_s: float,
    ) -> list[tuple[str, str, str]]: ...


def _resolve_search_backend(
    provider: str, api_key: str | None,
) -> tuple[_SearchBackend, str]:
    """Pick the `web_search` backend, returning `(backend, name_actually_used)`.

    A keyed provider named without its key does NOT fail boot — it warns
    and degrades to `ddgs`, because losing web search entirely is worse
    than losing result quality. The degrade is never silent: it is
    logged here AND the provider actually used is reported in every
    `web_search` result payload, so the event log shows which backend
    answered rather than leaving it to be inferred.
    """
    normalized = provider.strip().lower()
    keyed: Mapping[str, _KeyedSearchBackend] = {
        "exa": _exa_search_backend,
        "tavily": _tavily_search_backend,
    }
    if normalized in keyed:
        if not api_key:
            LOGGER.warning(
                "tools.web.search_provider=%r but its search_api_key_env names an "
                "unset/empty variable — degrading to the ddgs backend",
                normalized,
            )
            return _call_ddgs, "ddgs"
        return _with_api_key(keyed[normalized], api_key), normalized
    if normalized != "ddgs":
        LOGGER.warning(
            "unknown tools.web.search_provider=%r — using the ddgs backend",
            provider,
        )
    return _call_ddgs, "ddgs"


def _call_ddgs(
    query: str, max_results: int, *, timeout_s: float,
) -> list[tuple[str, str, str]]:
    """Adapt `_ddgs_search_backend` to `_SearchBackend`.

    Deliberately a late-bound module-global lookup rather than a
    captured function object: `_ddgs_search_backend`'s own docstring
    promises the acceptance script can monkeypatch that NAME, and
    capturing it at registry-build time would quietly break that seam.
    """
    return _ddgs_search_backend(query, max_results, timeout_s=timeout_s)


def _with_api_key(backend: _KeyedSearchBackend, api_key: str) -> _SearchBackend:
    """Close a keyed backend over its credential, yielding a `_SearchBackend`."""

    def _call(
        query: str, max_results: int, *, timeout_s: float,
    ) -> list[tuple[str, str, str]]:
        return backend(query, max_results, timeout_s=timeout_s, api_key=api_key)

    return _call


def _make_web_search_handler(
    *,
    default_max_results: int,
    timeout_s: float,
    provider: str = DEFAULT_WEB_SEARCH_PROVIDER,
    api_key: str | None = None,
) -> ToolHandler:
    """Bind `tools.web.{search_max_results,timeout_s,search_provider}` into a closure.

    ADR-0011 D7. Same shape as `_make_search_notes_handler` — config
    read once at registry-build time, closed over here.
    """
    backend, provider_used = _resolve_search_backend(provider, api_key)

    def _handler(
        action_request: ActionRequest,
        conn: sqlite3.Connection,
        runtime_paths: RuntimePathsLike,  # noqa: ARG001 — signature uniformity.
        lifecycle: ActionLifecycle,
    ) -> RawResult:
        running_event_uid = _get_running_event_uid(conn, action_request.action_id)
        action_id = action_request.action_id

        try:
            query = action_request.arguments.get("query")
            if not isinstance(query, str) or not query.strip():
                return _emit_tool_error(
                    conn=conn,
                    lifecycle=lifecycle,
                    action_id=action_id,
                    running_event_uid=running_event_uid,
                    code="invalid_argument",
                    message=f"web_search: query must be a non-empty string (got {query!r})",
                )

            max_results = default_max_results
            max_results_arg = action_request.arguments.get("max_results")
            if (
                isinstance(max_results_arg, (int, float))
                and not isinstance(max_results_arg, bool)
                and math.isfinite(max_results_arg)
            ):
                # SHOULD-FIX 6, ADR-0011 §12: `json.loads` accepts
                # `Infinity`/`NaN` in the LLM tool-arg path, and
                # `int(inf)` raises OverflowError — fold that shape
                # into "ignore, use the default", same as any other
                # malformed `max_results_arg` this branch already
                # ignores silently.
                max_results = int(max_results_arg)
            max_results = max(1, min(max_results, _WEB_SEARCH_MAX_RESULTS_CAP))

            try:
                rows = backend(query, max_results, timeout_s=timeout_s)
            except Exception as exc:  # noqa: BLE001 — backend breakage (ddgs anti-bot churn is expected and normal, ADR-0011 §5; a keyed provider can 401/429 just as routinely) degrades to an error observation naming the backend, never a crash; no retry loop in-handler.
                return _emit_tool_error(
                    conn=conn,
                    lifecycle=lifecycle,
                    action_id=action_id,
                    running_event_uid=running_event_uid,
                    code="web_search_backend_error",
                    message=f"web_search: {provider_used} backend error: {exc}",
                )

            results = _cap_rows_total_bytes(
                [
                    f"{i}. {title} — {url} — {_collapse_ws(text)[:_WEB_SEARCH_RESULT_TEXT_CAP]}"
                    for i, (title, url, text) in enumerate(rows, start=1)
                ],
                _WEB_SEARCH_TOTAL_MAX_BYTES,
            )
            # Naming the backend keeps the `_resolve_search_backend`
            # degrade visible in the event log instead of inferable.
            payload: dict[str, Any] = {"results": results, "provider": provider_used}
            return _emit_tool_observation(
                conn=conn,
                lifecycle=lifecycle,
                action_id=action_id,
                running_event_uid=running_event_uid,
                payload=payload,
            )
        except Exception as exc:  # noqa: BLE001 — handler-boundary catch-all (MUST-FIX 2, ADR-0011 §12): §5's contract is "never a crash"; anything not already folded into a named error observation above (e.g. a future argument-shape bug) still terminal-transitions the lifecycle instead of stranding it at `running`, naming the real exception type so a genuine bug stays diagnosable.
            return _emit_tool_error(
                conn=conn,
                lifecycle=lifecycle,
                action_id=action_id,
                running_event_uid=running_event_uid,
                code="web_search_unexpected_error",
                message=f"web_search: unexpected {type(exc).__name__}: {exc}",
            )

    return _handler


# --- web_fetch -----------------------------------------------------------------

DEFAULT_WEB_FETCH_MAX_BYTES: Final[int] = 2 * 1024 * 1024
"""Default `tools.web.fetch_max_bytes` (ADR-0011 D7) — how many bytes of
the RESPONSE BODY are drained off the socket.

This is a memory/DoS bound, NOT the output cap — that is
:data:`DEFAULT_WEB_FETCH_MAX_TEXT_BYTES`, applied to the extracted text
further downstream.

It was 8 KiB ("same order as every other D5 tool's output cap"), which
conflated the two: the drain stops at this many bytes of *raw markup*,
and a real page spends far more than 8 KiB on `<head>` (inline CSS,
JSON-LD, script tags) before the first byte of visible text. A 150 KB
page therefore reached the parser as pure `<head>` and extracted to
nothing at all — the tool reported "cut off before any body text was
parsed" on ordinary sites, i.e. it could not read the web. 2 MiB clears
the head of essentially any real document while still refusing an
unbounded stream."""

DEFAULT_WEB_FETCH_MAX_TEXT_BYTES: Final[int] = 8192
"""Default `tools.web.fetch_max_text_bytes` — the cap on the text this
tool actually returns, applied AFTER HTML extraction.

This is the 8 KiB context bound the old `fetch_max_bytes` was really
reaching for. Capping extracted text (rather than raw bytes) is what
makes the number mean what it says: 8 KiB of readable prose, not 8 KiB
of markup that may contain none."""

_WEB_FETCH_HEADERS: Final[Mapping[str, str]] = {
    # httpx's default UA (`python-httpx/x.y.z`) is refused outright by a
    # meaningful slice of the web. Wikipedia answers it with a bare 403
    # whose body reads "Please set a user-agent and respect our robot
    # policy" — which reached the LLM as an unexplained failure on an
    # ordinary encyclopedia lookup.
    #
    # Self-identifying (a real name plus a contact URL) is what the
    # sites asking for a UA actually want, and it keeps this honest:
    # `web_fetch` does not pretend to be a browser, and pairing a
    # browser UA with a client that runs no JS earns bot-detection
    # blocks rather than avoiding them.
    "User-Agent": "Jarvis/1.0 (+https://github.com/alllllenshi/jarvis)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.5",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
"""Request headers for every `web_fetch` hop (see the UA note above)."""

_WEB_FETCH_MAX_REDIRECTS: Final[int] = 3
"""Hard ceiling on redirects FOLLOWED (ADR-0011 D5: "at most 3
redirects") — not configurable. A 4th redirect response is refused
rather than followed; up to `_WEB_FETCH_MAX_REDIRECTS` + 1 HTTP
requests are made in total (the initial request plus each followed
redirect)."""

_WEB_FETCH_INPUT_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "description": "The http(s) URL to fetch.",
        },
    },
    "required": ["url"],
}

_REDIRECT_STATUS_CODES: Final[frozenset[int]] = frozenset({301, 302, 303, 307, 308})


def _parse_content_length(raw: str | None) -> int | None:
    """Parse a `Content-Length` header value, or `None` if absent/junk."""
    if raw is None:
        return None
    try:
        value = int(raw.strip())
    except ValueError:
        return None
    return value if value >= 0 else None


@dataclass(frozen=True)
class _HopResponse:
    """One non-redirect-following HTTP response (ADR-0011 D5 `web_fetch`)."""

    status_code: int
    location: str | None
    content_type: str | None
    body: bytes
    total_bytes: int
    total_bytes_exact: bool = True
    """False iff `total_bytes` is a lower bound, not a measured total —
    the drain stopped at `max_bytes` (or the fetch deadline) on a
    response with no usable `Content-Length` (MUST-FIX 3, ADR-0011
    §12). `body` itself is always the true prefix actually read,
    capped at `max_bytes` regardless of this flag."""


class _FetchDeadlineExceededError(Exception):
    """Raised when the overall wall-clock fetch budget elapses mid-drain.

    MUST-FIX 3, ADR-0011 §12. Raised by `_http_get_one_hop`, caught by
    `_fetch_url_backend` — never escapes this module.
    """


def _http_get_one_hop(
    url: str, *, timeout_s: float, max_bytes: int, deadline: float,
) -> _HopResponse:
    """Issue ONE GET with redirects disabled, streamed, body capped at `max_bytes`.

    The caller (`_fetch_url_backend`) owns redirect-following — this
    function never does, so every hop's `Location` can be re-validated
    by the SSRF guard before it is followed (ADR-0011 D5 point 5).

    The drain STOPS as soon as `max_bytes` is collected (MUST-FIX 3,
    ADR-0011 §12) — `httpx`'s `timeout` is a per-read timeout, not a
    wall-clock budget, so draining the FULL body (as this used to do,
    to report an exact `total_bytes`) let a slow/adversarial server
    hold the connection open indefinitely while trickling data, and let
    a huge body cost full transfer time to deliver a capped prefix.
    `deadline` (an absolute `time.monotonic()` value, shared across
    every hop by `_fetch_url_backend`) is also checked every chunk, so
    a slow-loris server that never triggers `httpx`'s own read timeout
    still gets cut off.

    `total_bytes`/`total_bytes_exact`: when the server sends a
    `Content-Length`, that is trusted as the exact total (matches what
    every HTTP client does). Otherwise, if the drain reached the cap or
    deadline before the stream ended, `total_bytes` is only the number
    of bytes actually read off the wire — a true LOWER BOUND, reported
    as such via `total_bytes_exact=False` — never a claimed exact count
    for data that was never measured.

    This is the one seam the acceptance script monkeypatches
    (`tools._http_get_one_hop`) to exercise `_fetch_url_backend`'s
    redirect-walking + guard-revalidation + truncation logic without a
    real socket — same "substitute a seam" shape Step 5 used for
    `vault_root`.
    """
    with (
        httpx.Client(timeout=timeout_s, follow_redirects=False, trust_env=False) as client,
        client.stream("GET", url, headers=_WEB_FETCH_HEADERS) as response,
    ):
        content_type = response.headers.get("content-type")
        location = response.headers.get("location")
        declared_length = _parse_content_length(response.headers.get("content-length"))
        chunks: list[bytes] = []
        collected = 0
        stopped_early = False
        for chunk in response.iter_bytes():
            if collected < max_bytes:
                chunks.append(chunk[: max_bytes - collected])
            collected += len(chunk)
            if collected >= max_bytes:
                stopped_early = True
                break
            if time.monotonic() >= deadline:
                # Distinct from the `max_bytes` cap above: reaching the
                # cap is a normal, expected truncation of a large body;
                # running out of wall-clock time mid-drain means the
                # server is too slow (slow-loris) — surfaced as an
                # error by `_fetch_url_backend`, not a truncated
                # success.
                msg = f"exceeded the fetch deadline after {collected} bytes"
                raise _FetchDeadlineExceededError(msg)
        body = b"".join(chunks)
        if declared_length is not None:
            total_bytes, total_bytes_exact = declared_length, True
        elif stopped_early:
            total_bytes, total_bytes_exact = collected, False
        else:
            total_bytes, total_bytes_exact = collected, True
        return _HopResponse(
            status_code=response.status_code,
            location=location,
            content_type=content_type,
            body=body,
            total_bytes=total_bytes,
            total_bytes_exact=total_bytes_exact,
        )


@dataclass(frozen=True)
class _FetchOutcome:
    """Result of `_fetch_url_backend` — success fields XOR error fields."""

    ok: bool
    status_code: int = 0
    content_type: str | None = None
    body: bytes = b""
    total_bytes: int = 0
    total_bytes_exact: bool = True
    final_url: str = ""
    error_code: str | None = None
    error_message: str | None = None


def _fetch_url_backend(  # noqa: PLR0911 — one linear guard/canonicalize/fetch/redirect loop; each return is a distinct named terminal outcome (`_FetchOutcome.error_code`), same rationale as the other D5 handlers' `_emit_tool_error` call sites — splitting them apart would scatter the outcomes from the loop that produces them.
    url: str,
    *,
    timeout_s: float,
    max_bytes: int,
    max_redirects: int = _WEB_FETCH_MAX_REDIRECTS,
    resolver: Callable[
        [str], Sequence[ipaddress.IPv4Address | ipaddress.IPv6Address]
    ]
    | None = None,
) -> _FetchOutcome:
    """Guarded GET with manual redirect walking (ADR-0011 D5).

    Validates EVERY hop — the initial `url` and each `Location` header —
    through `validate_egress_url` before issuing that hop's request,
    and always fetches `_normalize_egress_url`'s canonical form of that
    hop, never the raw `Location`/input string (ADR-0011 §12 MUST-FIX
    1/2 root cause — the validated string must be the used string).
    This is what stops the classic bypass: a public URL that 302s to
    `http://127.0.0.1:8006/`. A guard refusal at any hop, and exceeding
    `max_redirects`, both return `ok=False` with a named `error_code`;
    neither ever raises past this function.

    A single wall-clock `deadline` (MUST-FIX 3, ADR-0011 §12) is set
    ONCE from `timeout_s` and shared across every hop, including the
    guard/canonicalization work between hops — no redirect chain can
    make one `web_fetch` call run longer than `timeout_s` total,
    contrary to the previous per-hop-only `httpx` timeout.

    `resolver` defaults to `None`, resolved to the module-level
    `_resolve_hostname_ips` INSIDE the function body rather than as a
    literal default value — a default value is bound once at `def`
    time, so monkeypatching `tools._resolve_hostname_ips` afterward
    (the acceptance script's hermetic seam for every caller that does
    not pass `resolver` explicitly, e.g. `web_fetch`'s registered
    handler) would silently have no effect against a frozen default.
    """
    resolve = resolver if resolver is not None else _resolve_hostname_ips
    deadline = time.monotonic() + timeout_s
    current = url
    hops_followed = 0
    while True:
        allowed, reason = validate_egress_url(current, resolver=resolve)
        if not allowed:
            return _FetchOutcome(
                ok=False,
                error_code="ssrf_guard_refused",
                error_message=f"web_fetch egress guard refused {current!r}: {reason}",
            )

        normalized = _normalize_egress_url(current)
        if normalized is None:
            # Should be unreachable — `validate_egress_url` just
            # allowed `current` via the identical parser — but refuse
            # rather than ever fall back to the raw string.
            return _FetchOutcome(
                ok=False,
                error_code="ssrf_guard_refused",
                error_message=f"web_fetch: {current!r} passed the guard but would not canonicalize",
            )

        remaining_s = deadline - time.monotonic()
        if remaining_s <= 0:
            return _FetchOutcome(
                ok=False,
                error_code="deadline_exceeded",
                error_message=(
                    f"web_fetch: exceeded the {timeout_s:g}s wall-clock budget "
                    f"before hop {hops_followed + 1}"
                ),
            )

        try:
            hop = _http_get_one_hop(
                normalized, timeout_s=remaining_s, max_bytes=max_bytes, deadline=deadline,
            )
        except _FetchDeadlineExceededError:
            return _FetchOutcome(
                ok=False,
                error_code="deadline_exceeded",
                error_message=f"web_fetch: exceeded the {timeout_s:g}s wall-clock budget",
            )
        except httpx.HTTPError as exc:
            return _FetchOutcome(
                ok=False,
                error_code="network_error",
                error_message=f"web_fetch: {exc}",
            )

        if hop.status_code in _REDIRECT_STATUS_CODES:
            if hop.location is None:
                return _FetchOutcome(
                    ok=False,
                    error_code="network_error",
                    error_message=f"web_fetch: {normalized!r} redirected with no Location header",
                )
            if hops_followed >= max_redirects:
                return _FetchOutcome(
                    ok=False,
                    error_code="too_many_redirects",
                    error_message=(
                        f"web_fetch: exceeded {max_redirects} redirects starting from {url!r}"
                    ),
                )
            hops_followed += 1
            current = urljoin(normalized, hop.location)
            continue

        return _FetchOutcome(
            ok=True,
            status_code=hop.status_code,
            content_type=hop.content_type,
            body=hop.body,
            total_bytes=hop.total_bytes,
            total_bytes_exact=hop.total_bytes_exact,
            final_url=normalized,
        )


class _ReadableHTMLParser(HTMLParser):
    """Strip an HTML document to its page title + tag-stripped visible text.

    stdlib-only (ADR-0011 D5: no bs4/lxml — `html.parser` is fine).
    `<script>`/`<style>` contents are dropped entirely; every other
    tag's text is kept, collapsed to single-space-joined chunks. Feeding
    a byte-capped, possibly mid-tag-truncated document is safe —
    `HTMLParser` is deliberately lenient about malformed/incomplete
    markup, and an unclosed tag at EOF simply stops emitting text there.
    """

    def __init__(self) -> None:
        """Start with empty title/text buffers and zero script/style depth."""
        super().__init__(convert_charrefs=True)
        self._title_parts: list[str] = []
        self._text_parts: list[str] = []
        self._in_title = False
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:  # noqa: ARG002 — HTMLParser calls this positionally; attrs is unused but must stay in the override's signature.
        """Track entry into `<title>` and `<script>`/`<style>` regions."""
        if tag in ("script", "style"):
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        """Track exit from `<title>` and `<script>`/`<style>` regions."""
        if tag in ("script", "style"):
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        """Collect text data outside of skipped regions."""
        if self._skip_depth:
            return
        if self._in_title:
            self._title_parts.append(data)
            return
        stripped = data.strip()
        if stripped:
            self._text_parts.append(stripped)

    @property
    def title(self) -> str:
        """Return the concatenated `<title>` text, stripped."""
        return "".join(self._title_parts).strip()

    @property
    def text(self) -> str:
        """Return the space-joined visible text chunks."""
        return " ".join(self._text_parts)


def _extract_readable_html(html_text: str) -> tuple[str, str]:
    """Return `(title, stripped_text)` from an HTML document (stdlib-only)."""
    parser = _ReadableHTMLParser()
    parser.feed(html_text)
    return parser.title, parser.text


def _parse_content_type_charset(content_type: str | None) -> str | None:
    """Extract a `charset=` parameter from a `Content-Type` header value."""
    if not content_type:
        return None
    for part in content_type.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.strip().lower() == "charset":
            return value.strip().strip('"').strip("'") or None
    return None


def _decode_hop_body(
    body: bytes, total_bytes: int, content_type: str | None,
) -> tuple[str, int, bool]:
    """`(text, undelivered_bytes, lossy)` honoring a declared `charset=`.

    SHOULD-FIX 7, ADR-0011 §12 — a `charset=gb2312` page used to decode
    to 100% U+FFFD because everything went through the UTF-8-only
    `truncate_utf8` path regardless of what `Content-Type` declared.

    UTF-8 (declared, or no usable charset named) still goes through
    `truncate_utf8` UNCHANGED — its codepoint-safe boundary handling is
    never duplicated here; this only adds ONE more decode attempt in
    front of it for a charset Python actually knows (stdlib `codecs`,
    no new dependency). A body cut mid-character by the caller's own
    `max_bytes` cap decodes with `errors="replace"` and is reported
    `lossy=True`, same signal `truncate_utf8` gives its own lossy case.
    """
    charset = _parse_content_type_charset(content_type)
    if charset is None or charset.lower().replace("_", "-") in ("utf-8", "utf8"):
        return truncate_utf8(body, total_bytes)
    try:
        codec_name = codecs.lookup(charset).name
    except LookupError:
        return truncate_utf8(body, total_bytes)
    undelivered = max(0, total_bytes - len(body))
    try:
        return body.decode(codec_name), undelivered, False
    except UnicodeDecodeError:
        return body.decode(codec_name, errors="replace"), undelivered, True
    except LookupError:
        # A declared charset can name a REGISTERED but non-text codec
        # (e.g. `charset=base64`) — `codecs.lookup` accepts it but
        # `bytes.decode` refuses it with `LookupError`, not
        # `UnicodeDecodeError`. Caught here (not left to MUST-FIX 2's
        # handler-boundary catch-all) so an unusual header degrades to
        # the same lossy-UTF-8 fallback as an unknown charset, rather
        # than a generic "unexpected error" observation.
        return truncate_utf8(body, total_bytes)


def _cap_extracted_text(text: str, max_bytes: int) -> tuple[str, int]:
    """Cap ALREADY-EXTRACTED text to `max_bytes`, codepoint-safe.

    Returns `(capped, undelivered_bytes)`; `undelivered_bytes == 0`
    means nothing was cut. The `lossy` leg of `truncate_utf8` cannot
    fire here — `text` is a valid `str`, so re-encoding it always
    produces well-formed UTF-8 and the only possible cut is a clean
    codepoint boundary.

    This is the counterpart to the drain-side `max_bytes` cap in
    `_fetch_hop`: that one bounds bytes off the socket, this one bounds
    what the LLM is asked to read. Keeping them separate is the whole
    point — see :data:`DEFAULT_WEB_FETCH_MAX_BYTES`.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, 0
    capped, undelivered, _lossy = truncate_utf8(encoded[:max_bytes], len(encoded))
    return capped, undelivered


def _truncation_marker(
    *, text_undelivered: int, body_undelivered: int, body_total_exact: bool,
) -> str:
    """Render the inline `…[…]` marker naming which cut(s) fired.

    Both can fire on one response — a page past the drain cap whose
    extracted text ALSO overruns the text cap — and they mean different
    things to a reader deciding whether to fetch more, so name both
    rather than picking one. Returns `""` when nothing was cut.
    """
    markers: list[str] = []
    if text_undelivered > 0:
        markers.append(f"truncated {text_undelivered} bytes of extracted text")
    if body_undelivered > 0:
        # MUST-FIX 3, ADR-0011 §12: when the drain stopped at the cap or
        # the fetch deadline before the server declared a
        # Content-Length, the shortfall is a true LOWER bound (bytes
        # actually seen past the cap), never a claimed exact count.
        qualifier = "" if body_total_exact else "at least "
        markers.append(f"download stopped {qualifier}{body_undelivered} bytes short")
    if not markers:
        return ""
    return "…[" + "; ".join(markers) + "]"


def _looks_like_html(body: bytes) -> bool:
    """Sniff a leading `<`/`<!doctype` to detect an HTML document.

    SHOULD-FIX 7, ADR-0011 §12 — used only when `Content-Type` is
    absent, so a header-less HTML response still gets routed through
    the tag-stripping path instead of being dumped as raw markup under
    the "plain text" branch.
    """
    stripped = body.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return stripped.startswith(b"<")


def _make_web_fetch_handler(  # noqa: C901 — thin closure factory; the complexity ruff counts lives entirely in the nested `_handler` (see its own noqa), not in this function's own body.
    *, max_bytes: int, max_text_bytes: int, timeout_s: float,
) -> ToolHandler:
    """Bind the `tools.web.*` fetch knobs into a closure (ADR-0011 D7).

    `fetch_max_bytes`, `fetch_max_text_bytes` and `timeout_s`.
    `max_bytes` bounds the socket drain; `max_text_bytes` bounds the
    extracted text handed back. See :data:`DEFAULT_WEB_FETCH_MAX_BYTES`
    for why collapsing the two made the tool unable to read real pages.
    """

    def _handler(  # noqa: C901, PLR0912 — one linear guard/content-type/decode/shape pass (MUST-FIX 2's handler-boundary try/except wraps all of it, ADR-0011 §12) — splitting the content-type sniff, charset decode, and HTML-placeholder branches into helpers would scatter the fail-fast checks from the payload fields they gate, same rationale as `_make_search_notes_handler`'s own noqa.
        action_request: ActionRequest,
        conn: sqlite3.Connection,
        runtime_paths: RuntimePathsLike,  # noqa: ARG001 — signature uniformity.
        lifecycle: ActionLifecycle,
    ) -> RawResult:
        running_event_uid = _get_running_event_uid(conn, action_request.action_id)
        action_id = action_request.action_id

        try:
            url = action_request.arguments.get("url")
            if not isinstance(url, str) or not url.strip():
                return _emit_tool_error(
                    conn=conn,
                    lifecycle=lifecycle,
                    action_id=action_id,
                    running_event_uid=running_event_uid,
                    code="invalid_argument",
                    message=f"web_fetch: url must be a non-empty string (got {url!r})",
                )

            outcome = _fetch_url_backend(url, timeout_s=timeout_s, max_bytes=max_bytes)
            if not outcome.ok:
                return _emit_tool_error(
                    conn=conn,
                    lifecycle=lifecycle,
                    action_id=action_id,
                    running_event_uid=running_event_uid,
                    code=outcome.error_code or "web_fetch_failed",
                    message=outcome.error_message or "web_fetch: unknown failure",
                )

            mime = (outcome.content_type or "").split(";", 1)[0].strip().lower()
            if not mime and _looks_like_html(outcome.body):
                # SHOULD-FIX 7, ADR-0011 §12: a response with NO
                # Content-Type used to skip both the non-text branch
                # below and the HTML branch further down, dumping raw
                # markup as if it were the tool's plain-text case.
                mime = "text/html"
            if mime and not mime.startswith("text/"):
                payload: dict[str, Any] = {
                    "url": outcome.final_url,
                    "status_code": outcome.status_code,
                    "content_type": outcome.content_type,
                    "note": (
                        f"non-text content-type {outcome.content_type!r}; "
                        "body not decoded as text"
                    ),
                }
                return _emit_tool_observation(
                    conn=conn,
                    lifecycle=lifecycle,
                    action_id=action_id,
                    running_event_uid=running_event_uid,
                    payload=payload,
                )

            text, undelivered_bytes, lossy = _decode_hop_body(
                outcome.body, outcome.total_bytes, outcome.content_type,
            )
            body_truncated = undelivered_bytes > 0

            title: str | None = None
            placeholder = False
            if mime == "text/html":
                title, html_text = _extract_readable_html(text)
                if html_text.strip():
                    content = html_text
                elif body_truncated:
                    # SHOULD-FIX 7, ADR-0011 §12: the cap cut the
                    # document before any body text was parsed — unlike
                    # the genuinely-empty-parse case below, the page may
                    # well have content past the cut. Don't blame JS
                    # for a truncation artifact, and don't glue the
                    # byte-count marker onto this sentence either — it
                    # already names the cut inline (`placeholder=True`
                    # skips the generic marker append below).
                    #
                    # Now reachable only past a 2 MiB `<head>`, not the
                    # old 8 KiB one: this used to be the ROUTINE outcome
                    # for any real page, which is what made the tool
                    # useless.
                    placeholder = True
                    content = (
                        "(the page was cut off by the download cap before any "
                        "body text was parsed — cannot tell whether it has "
                        "visible content past the cut)"
                    )
                else:
                    # ADR-0011 D5: no JS rendering — an SPA that hydrates via
                    # script returns only its shell here. Declared limitation,
                    # not an empty-page bug; say so instead of narrating
                    # nothing as "the page has no content".
                    placeholder = True
                    content = (
                        "(no visible text in the initial HTML — this may be a "
                        "JavaScript-rendered page; this tool does not execute JS)"
                    )
            else:
                content = text

            # The output cap lands HERE — on extracted text, not on the
            # raw bytes upstream. A placeholder is a fixed sentence the
            # tool wrote itself; capping it would only mangle it.
            text_undelivered = 0
            if not placeholder:
                content, text_undelivered = _cap_extracted_text(content, max_text_bytes)

            if not placeholder:
                content += _truncation_marker(
                    text_undelivered=text_undelivered,
                    body_undelivered=undelivered_bytes if body_truncated else 0,
                    body_total_exact=outcome.total_bytes_exact,
                )

            payload = {
                "url": outcome.final_url,
                "status_code": outcome.status_code,
                "content_type": outcome.content_type,
                "content": content,
                # Unchanged contract: "you did not get the whole thing",
                # now true for either cut.
                "truncated": bool(text_undelivered > 0 or body_truncated),
                "total_bytes": outcome.total_bytes,
            }
            if not outcome.total_bytes_exact:
                payload["total_bytes_exact"] = False
            if title is not None:
                payload["title"] = title
            if lossy:
                payload["encoding"] = "lossy"
            return _emit_tool_observation(
                conn=conn,
                lifecycle=lifecycle,
                action_id=action_id,
                running_event_uid=running_event_uid,
                payload=payload,
            )
        except Exception as exc:  # noqa: BLE001 — handler-boundary catch-all (MUST-FIX 2, ADR-0011 §12): closes the class where a URL passes the SSRF guard but breaks a DIFFERENT parser downstream (`httpx.InvalidURL`/`CookieConflict`/`StreamError` are NOT `httpx.HTTPError` subclasses and would otherwise escape `_fetch_url_backend`'s own except clause, then `ToolRegistry.dispatch`'s bare `finally`, all the way past `decide()`, which has no `except Exception`). §5's contract is "never a crash": name the real exception type and terminal-transition the lifecycle exactly like every other error path here.
            return _emit_tool_error(
                conn=conn,
                lifecycle=lifecycle,
                action_id=action_id,
                running_event_uid=running_event_uid,
                code="web_fetch_unexpected_error",
                message=f"web_fetch: unexpected {type(exc).__name__}: {exc}",
            )

    return _handler


# --- open_url --------------------------------------------------------------

_OPEN_URL_SUBPROCESS_TIMEOUT_S: Final[float] = 10.0

_OPEN_URL_INPUT_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "description": "The http(s) URL to open in the default browser.",
        },
    },
    "required": ["url"],
}


def open_url_handler(  # noqa: PLR0911 — one linear validate/canonicalize/subprocess/ack pass; each return is a distinct named terminal outcome via `_emit_tool_error`/`_emit_tool_observation`, same rationale as the D5 handlers this mirrors.
    action_request: ActionRequest,
    conn: sqlite3.Connection,
    runtime_paths: RuntimePathsLike,  # noqa: ARG001 — signature uniformity.
    lifecycle: ActionLifecycle,
) -> RawResult:
    """Open a URL in the default browser (ADR-0011 D5).

    Same scheme + address checks as `web_fetch` via `validate_egress_url`
    — `open http://127.0.0.1:8006/shutdown` would otherwise open Allen's
    browser against his own daemon. `open` is handed
    `_normalize_egress_url`'s canonical re-serialization of `url`, never
    `url` itself (ADR-0011 §12 MUST-FIX 1/2 root cause) — the browser's
    WHATWG URL parser reads some strings (a backslash before the last
    `@` in the authority, in particular) differently from the
    `urlsplit`-based guard that just validated them; canonicalizing
    strips exactly the userinfo/fragment components that trick relies
    on. Result is an ACK (`result_semantics="ack"`, Execution Claim
    `executed`) — the browser actually opening is not verified, same
    posture `open_path_handler` documents for its own `open` call.
    """
    running_event_uid = _get_running_event_uid(conn, action_request.action_id)
    action_id = action_request.action_id

    try:
        url = action_request.arguments.get("url")
        if not isinstance(url, str) or not url.strip():
            return _emit_tool_error(
                conn=conn,
                lifecycle=lifecycle,
                action_id=action_id,
                running_event_uid=running_event_uid,
                code="invalid_argument",
                message=f"open_url: url must be a non-empty string (got {url!r})",
            )

        allowed, reason = validate_egress_url(url)
        if not allowed:
            return _emit_tool_error(
                conn=conn,
                lifecycle=lifecycle,
                action_id=action_id,
                running_event_uid=running_event_uid,
                code="ssrf_guard_refused",
                message=f"open_url egress guard refused {url!r}: {reason}",
            )

        normalized = _normalize_egress_url(url)
        if normalized is None:
            # Should be unreachable — the guard just allowed `url` via
            # the identical parser — but refuse rather than ever fall
            # back to the raw string.
            return _emit_tool_error(
                conn=conn,
                lifecycle=lifecycle,
                action_id=action_id,
                running_event_uid=running_event_uid,
                code="ssrf_guard_refused",
                message=f"open_url: {url!r} passed the guard but would not canonicalize",
            )

        try:
            # argv list, no shell; `normalized` already passed the
            # scheme+address allowlist above; `open` resolved via PATH
            # matches the `pbpaste`/`open_path` precedent.
            proc = subprocess.run(  # noqa: S603
                ["open", normalized],  # noqa: S607
                timeout=_OPEN_URL_SUBPROCESS_TIMEOUT_S,
                capture_output=True,
                text=True,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return _emit_tool_error(
                conn=conn,
                lifecycle=lifecycle,
                action_id=action_id,
                running_event_uid=running_event_uid,
                code="open_failed",
                message=f"open_url: subprocess failed to start: {exc}",
            )

        if proc.returncode != 0:
            stderr_tail = (proc.stderr or "").strip()[-_OUTPUT_TAIL_BYTES:]
            return _emit_tool_error(
                conn=conn,
                lifecycle=lifecycle,
                action_id=action_id,
                running_event_uid=running_event_uid,
                code="open_failed",
                message=f"open_url: `open` exited {proc.returncode}: {stderr_tail}",
            )

        payload: dict[str, Any] = {"url": normalized}
        return _emit_tool_observation(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_id,
            running_event_uid=running_event_uid,
            payload=payload,
            semantics="ack",
        )
    except Exception as exc:  # noqa: BLE001 — handler-boundary catch-all (MUST-FIX 2, ADR-0011 §12), same rationale as `web_fetch`'s: §5's contract is "never a crash", so any unexpected failure still terminal-transitions the lifecycle, naming the real exception type, instead of stranding it at `running`.
        return _emit_tool_error(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_id,
            running_event_uid=running_event_uid,
            code="open_url_unexpected_error",
            message=f"open_url: unexpected {type(exc).__name__}: {exc}",
        )


# --- screen_look ---------------------------------------------------------------


class VisionClient(Protocol):
    """Injected one-shot vision seam for `screen_look` (ADR-0011 D5/D7).

    L4 cannot import `jarvis.decision.llm.LLMClient` — sibling layers
    under `.importlinter`. `jarvis.runtime` (the composition root)
    builds the real implementation around the `llm.presets.vision`
    preset and binds it into `build_default_registry` at
    registry-build time — the same seam Step 5/6 used for
    `vault_root` / `tools.web.*`.

    One call: an image path (+ optional question) in, text out. The
    handler hands this a `Path`, never bytes — reading the file and
    base64-encoding it for the actual vision request happens entirely
    inside the injected implementation, so no image data ever passes
    through, or is held by, this L4 module (spec §3.3.9 / ADR-0011 §3
    D5: the payload carries the artifact path, never image bytes).
    Any failure must be raised, not swallowed — the handler's own
    boundary catch turns it into an error observation; the screenshot
    artifact already on disk is unaffected by a vision failure
    (ADR-0011 §5).
    """

    def describe_image(self, image_path: Path, *, question: str | None) -> str:
        """Return a text description of the image at `image_path`."""
        ...


DEFAULT_SCREEN_MAX_WIDTH_PX: Final[int] = 1568
"""Default `tools.screen.max_width_px` (ADR-0011 D7) — the vision
model's own recommended upper bound on input image width."""

_SCREEN_ARTIFACTS_DIRNAME: Final[str] = "screen_artifacts"
"""Subdirectory of `runtime_paths.artifacts_root`, mirroring the
`voice_artifacts/` precedent (`jarvis/runtime/inherent_loop.py:735`).
Nothing prunes it — same as `voice_artifacts/`, which has no pruner
either (`jarvis/surface/voice_artifact_store.py`); screenshots
accumulate on disk indefinitely."""

_SCREEN_CAPTURE_TIMEOUT_S: Final[float] = 10.0
_SIPS_TIMEOUT_S: Final[float] = 10.0

_SCREEN_LOOK_TEXT_MAX_BYTES: Final[int] = 8192
"""Same 8 KiB order as every other D5 tool's output cap."""

_PNG_MAGIC: Final[bytes] = b"\x89PNG\r\n\x1a\n"


def _looks_like_png(path: Path) -> bool:
    """Check the real 8-byte PNG magic (NIT-FIX 6, ADR-0011 §12).

    A bare `st_size == 0` check lets a truncated write through — e.g.
    a 3-byte fragment from an interrupted `screencapture` is non-empty
    and would otherwise reach the vision model (and get billed for
    garbage input) instead of being rejected here. Stdlib only, no new
    imaging dependency.
    """
    try:
        with path.open("rb") as fh:
            return fh.read(len(_PNG_MAGIC)) == _PNG_MAGIC
    except OSError:
        return False


_SCREEN_LOOK_INPUT_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": (
                "Optional: focus the screen description on this question "
                "instead of a generic summary."
            ),
        },
    },
    "required": [],
}


def _run_screencapture(
    output_path: Path, *, timeout_s: float,
) -> subprocess.CompletedProcess[bytes]:
    """Real `screencapture -x -m` invocation (ADR-0011 D5).

    Named seam, not inlined, so the acceptance script can substitute a
    fake without ever invoking the real macOS screenshot utility — the
    first real invocation triggers the Screen Recording TCC prompt,
    which needs Allen physically present to approve.

    `-m` restricts the capture to the main display (NIT-FIX 6, ADR-0011
    §12): without it, a multi-display Mac gets one file per display,
    and every file but the one at `output_path` is an orphan — no
    payload field ever references it and nothing prunes it. Declared
    limitation: on a multi-display Mac this tool only ever sees the
    main display.
    """
    return subprocess.run(  # noqa: S603
        ["screencapture", "-x", "-m", str(output_path)],  # noqa: S607 — resolved via PATH, matches pbpaste/open precedent.
        timeout=timeout_s,
        capture_output=True,
        text=False,
        check=False,
    )


def _run_sips_downscale(
    image_path: Path, max_width_px: int, *, timeout_s: float,
) -> subprocess.CompletedProcess[bytes]:
    """Real in-place `sips -Z <max_width_px>` downscale (ADR-0011 D5).

    `-Z` / `--resampleHeightWidthMax` bounds the LARGER of the two
    dimensions to at most `max_width_px` (preserving aspect ratio, and
    not upscaling an already-smaller image) — not width specifically.
    For an ordinary landscape screenshot the two readings coincide, so
    behavior matches intent; a rotated display would make height the
    binding dimension, and `-Z` still produces the correct bound in
    that case too (NIT-FIX 6, ADR-0011 §12 — `max_width_px` is simply a
    narrower name than what this actually controls). Named seam, same
    rationale as `_run_screencapture`.
    """
    return subprocess.run(  # noqa: S603
        ["sips", "-Z", str(max_width_px), str(image_path)],  # noqa: S607 — resolved via PATH.
        timeout=timeout_s,
        capture_output=True,
        text=False,
        check=False,
    )


def _screen_recording_permission_message() -> str:
    """ADR-0011 §5's TCC-denial sentence, with the real interpreter path."""
    return f"Screen Recording permission missing for {sys.executable} — System Settings → Privacy"


def _make_screen_look_handler(  # noqa: C901 — one linear capture/downscale/vision/cap pass; each return is a distinct named terminal outcome, same rationale as the other D5 closure factories.
    *,
    vision_client: VisionClient | None,
    max_width_px: int,
) -> ToolHandler:
    """Bind the injected vision client + `tools.screen.max_width_px` (ADR-0011 D7).

    Same shape as `_make_web_search_handler` — config/deps read once at
    registry-build time, closed over here. `vision_client=None` means
    `jarvis.runtime` found no usable `llm.presets.vision` block: the
    tool still registers (the menu stays complete) but every call
    degrades to a `vision_unconfigured` error observation — AFTER the
    screenshot is captured and saved, so evidence survives a config
    gap the same way it survives a live proxy outage (ADR-0011 §5).
    """

    def _handler(  # noqa: PLR0911 — one linear capture/downscale/vision/cap pass; each return is a distinct named terminal outcome via `_emit_tool_error`/`_emit_tool_observation`, same rationale as the other D5 handlers.
        action_request: ActionRequest,
        conn: sqlite3.Connection,
        runtime_paths: RuntimePathsLike,
        lifecycle: ActionLifecycle,
    ) -> RawResult:
        running_event_uid = _get_running_event_uid(conn, action_request.action_id)
        action_id = action_request.action_id

        try:
            question_raw = action_request.arguments.get("question")
            question = (
                question_raw.strip()
                if isinstance(question_raw, str) and question_raw.strip()
                else None
            )

            artifacts_dir = runtime_paths.artifacts_root / _SCREEN_ARTIFACTS_DIRNAME
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            # `action_id` makes this collision-free (SHOULD-FIX 4, ADR-0011
            # §12): the millisecond timestamp alone repeats across
            # back-to-back dispatches, silently repointing an OLDER
            # `action.result_observed` row's `artifact_path` at a
            # DIFFERENT screenshot's bytes — undetectable after the fact,
            # since the path IS the evidence (§3.3.9's whole design).
            # The timestamp prefix is kept only for chronological `ls`.
            image_path = artifacts_dir / f"{int(time.time() * 1000)}_{action_id}.png"

            try:
                capture_proc = _run_screencapture(image_path, timeout_s=_SCREEN_CAPTURE_TIMEOUT_S)
            except (OSError, subprocess.TimeoutExpired) as exc:
                return _emit_tool_error(
                    conn=conn,
                    lifecycle=lifecycle,
                    action_id=action_id,
                    running_event_uid=running_event_uid,
                    code="screen_capture_start_failed",
                    message=f"screen_look: screencapture failed to run: {exc}",
                )

            if capture_proc.returncode != 0:
                # SHOULD-FIX 5, ADR-0011 §12: a non-zero exit's cause is
                # NOT determinable from here — it could be a TCC denial,
                # a bad path, a full disk, or anything else `screencapture`
                # exits non-zero for. Unlike the "wrote nothing" branch
                # below (a shape that genuinely IS specific to TCC denial
                # on some macOS versions), asserting a permission cause
                # here would send Allen to System Settings for e.g. a
                # filesystem problem. Report what actually happened, and
                # name Screen Recording only as the likely cause on a
                # first run — actionable without claiming certainty.
                stderr_tail = (capture_proc.stderr or b"").decode("utf-8", errors="replace").strip()
                return _emit_tool_error(
                    conn=conn,
                    lifecycle=lifecycle,
                    action_id=action_id,
                    running_event_uid=running_event_uid,
                    code="screen_capture_failed",
                    message=(
                        f"screen_look: screencapture exited {capture_proc.returncode}"
                        f"{f': {stderr_tail}' if stderr_tail else ''}. If this is the "
                        f"first screen_look call, the likely cause is missing Screen "
                        f"Recording permission for {sys.executable} — System Settings → "
                        "Privacy; otherwise this is a genuine screencapture failure."
                    ),
                )

            if not image_path.is_file() or not _looks_like_png(image_path):
                # ADR-0011 §5: on some macOS versions a TCC-denied
                # screencapture exits 0 but writes nothing (or an empty
                # /truncated file) instead of failing loudly. This
                # branch — unlike the non-zero-exit one above — genuinely
                # IS specific enough to name Screen Recording as the
                # cause: a successful (exit-0) capture that produced no
                # valid PNG is the documented TCC-denial shape on those
                # macOS versions, not a generic failure. Checking the
                # PNG magic (not just `st_size`) also catches a
                # truncated/partial write (NIT-FIX 6) before it reaches
                # the vision model. Detecting a FOURTH shape — an
                # all-black image written by some macOS versions on
                # TCC denial — would require decoding pixel data (a
                # new imaging dependency); deliberately not attempted.
                return _emit_tool_error(
                    conn=conn,
                    lifecycle=lifecycle,
                    action_id=action_id,
                    running_event_uid=running_event_uid,
                    code="screen_recording_permission_denied",
                    message=(
                        f"screen_look: {_screen_recording_permission_message()} "
                        "(screencapture produced no image data)"
                    ),
                )

            try:
                sips_proc = _run_sips_downscale(image_path, max_width_px, timeout_s=_SIPS_TIMEOUT_S)
                if sips_proc.returncode != 0:
                    LOGGER.warning(
                        "screen_look: sips downscale failed (exit %s) for %s; "
                        "continuing with the full-resolution screenshot",
                        sips_proc.returncode,
                        image_path,
                    )
            except (OSError, subprocess.TimeoutExpired) as exc:
                # Downscaling is a cost/latency optimization for the
                # vision call, not a correctness requirement — the
                # call still works against the full-resolution PNG,
                # and the artifact on disk is untouched either way.
                LOGGER.warning(
                    "screen_look: sips failed to run (%s) for %s; "
                    "continuing with the full-resolution screenshot",
                    exc,
                    image_path,
                )

            if vision_client is None:
                return _emit_tool_error(
                    conn=conn,
                    lifecycle=lifecycle,
                    action_id=action_id,
                    running_event_uid=running_event_uid,
                    code="vision_unconfigured",
                    message=(
                        "screen_look: no vision client configured "
                        f"(llm.presets.vision missing or invalid); screenshot saved at {image_path}"
                    ),
                )

            try:
                description_raw = vision_client.describe_image(image_path, question=question)
            except Exception as exc:  # noqa: BLE001 — vision preset / proxy failures degrade to an error observation (ADR-0011 §5); the screenshot artifact above already exists on disk regardless.
                return _emit_tool_error(
                    conn=conn,
                    lifecycle=lifecycle,
                    action_id=action_id,
                    running_event_uid=running_event_uid,
                    code="vision_call_failed",
                    message=(
                        f"screen_look: vision call failed: {type(exc).__name__}: {exc}; "
                        f"screenshot saved at {image_path}"
                    ),
                )

            description_bytes = description_raw.encode("utf-8")
            text, undelivered_bytes, _lossy = truncate_utf8(
                description_bytes[:_SCREEN_LOOK_TEXT_MAX_BYTES], len(description_bytes),
            )
            if undelivered_bytes > 0:
                text += f"…[truncated {undelivered_bytes} bytes]"

            payload: dict[str, Any] = {
                "artifact_path": str(image_path),
                "description": text,
                "truncated": undelivered_bytes > 0,
                "total_bytes": len(description_bytes),
            }
            return _emit_tool_observation(
                conn=conn,
                lifecycle=lifecycle,
                action_id=action_id,
                running_event_uid=running_event_uid,
                payload=payload,
            )
        except Exception as exc:  # noqa: BLE001 — handler-boundary catch-all (ADR-0011 §12 MUST-FIX 2 precedent): never strand the lifecycle at `running`, name the real exception type.
            return _emit_tool_error(
                conn=conn,
                lifecycle=lifecycle,
                action_id=action_id,
                running_event_uid=running_event_uid,
                code="screen_look_unexpected_error",
                message=f"screen_look: unexpected {type(exc).__name__}: {exc}",
            )

    return _handler


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

    def __init__(
        self,
        *,
        action_runner: ActionRunner | None = None,
        resource_key_resolver: ResourceKeyResolver | None = None,
        background_async: bool = False,
        confirmation_dispatch_outbox: bool = False,
    ) -> None:
        """Construct an empty registry (no tools yet).

        With ``action_runner=None`` — every caller before ADR-0008 Step 3 —
        ``dispatch`` runs the handler inline on the calling thread, exactly as
        it always has. With a runner installed, the same call submits an
        ActionRun and waits on its handle: the events, the ordering and the
        returned bundle are identical, but the handler executes on the
        runner's thread under a resolved resource lease.

        ``background_async`` is ADR-0008 Step 4: an ``is_async`` tool then
        returns from ``dispatch`` as soon as it is accepted, and its result
        reaches L3 the way the tool always claimed it would — through the
        durable ``worker.reported`` / terminal row that re-enters ``decide``.
        It requires a runner, because there is nothing to own the work
        otherwise.
        """
        if background_async and action_runner is None:
            msg = "background_async dispatch requires an ActionRunner"
            raise ActionRunnerError(msg)
        self._tools: dict[str, ToolDefinition] = {}
        self._lock = threading.RLock()
        self._action_runner = action_runner
        self._background_async = background_async
        self._confirmation_dispatch_outbox = confirmation_dispatch_outbox
        self._resource_key_resolver = (
            resource_key_resolver
            if resource_key_resolver is not None
            else default_resource_key_resolver
        )

    @property
    def action_runner(self) -> ActionRunner | None:
        """Return the installed ActionRunner, or ``None`` on the legacy path."""
        return self._action_runner

    @property
    def background_async(self) -> bool:
        """Return whether declared-async tools dispatch without being awaited."""
        return self._background_async

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
        tool_def = self._checked_tool_def(action_request, lifecycle)
        dispatched_event = self._emit_dispatched(action_request, conn, tool_def, lifecycle)
        if self._action_runner is None:
            return self._run_inline(
                action_request,
                conn,
                runtime_paths,
                lifecycle,
                tool_def=tool_def,
                dispatched_event_uid=dispatched_event.event_uid,
            )
        submission = self._submit_to_runner(
            action_request,
            conn,
            runtime_paths,
            lifecycle,
            tool_def=tool_def,
            dispatched_event_uid=dispatched_event.event_uid,
            runner=self._action_runner,
        )
        if self._background_async and tool_def.is_async:
            # ADR-0008 D9: "submit returns after dispatch, not after the
            # action finishes". L3 gets an acknowledgement and pauses on the
            # durable trigger; the handle is owned by the runner, whose
            # finalizer closes the cleanup debt when the worker really ends.
            return RawResultBundle(
                slots=(
                    RawResult(
                        action_id=action_request.action_id,
                        semantics="ack",
                        payload={
                            "action_id": action_request.action_id,
                            "dispatch": "background",
                        },
                        tool_output=tool_result(
                            {
                                "action_id": action_request.action_id,
                                "dispatch": "background",
                            },
                        ),
                        error=None,
                        metadata=None,
                    ),
                ),
            )
        return submission.handle.result()

    def submit(
        self,
        action_request: ActionRequest,
        conn: sqlite3.Connection,
        runtime_paths: RuntimePathsLike,
        lifecycle: ActionLifecycle,
    ) -> ActionSubmission:
        """Dispatch one ActionRequest and return its live handle.

        ADR-0008 D9: this returns after dispatch, not after the action
        finishes. Wave 4B's only caller is ``dispatch`` itself, which
        immediately awaits the handle; Wave 5's background worker is what
        stops awaiting it.

        Raises:
            ActionRunnerError: No ActionRunner is installed on this registry.
        """
        if self._action_runner is None:
            msg = "ToolRegistry.submit requires an ActionRunner"
            raise ActionRunnerError(msg)
        tool_def = self._checked_tool_def(action_request, lifecycle)
        dispatched_event = self._emit_dispatched(action_request, conn, tool_def, lifecycle)
        return self._submit_to_runner(
            action_request,
            conn,
            runtime_paths,
            lifecycle,
            tool_def=tool_def,
            dispatched_event_uid=dispatched_event.event_uid,
            runner=self._action_runner,
        )

    def _checked_tool_def(
        self,
        action_request: ActionRequest,
        lifecycle: ActionLifecycle,
    ) -> ToolDefinition:
        """Validate the three dispatch preconditions and return the definition."""
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
        return tool_def

    def _emit_dispatched(
        self,
        action_request: ActionRequest,
        conn: sqlite3.Connection,
        tool_def: ToolDefinition,
        lifecycle: ActionLifecycle,
    ) -> Event:
        """Register the action as live and commit its `action.dispatched`."""
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
        with action_admission_guard():
            if (
                self._confirmation_dispatch_outbox
                and action_request.authorization_lease is not None
            ):
                dispatched_event = admit_authorized_dispatch(
                    conn, action_request, payload=dispatched_payload,
                    correlation=_action_correlation(action_request),
                )
            else:
                dispatched_event = emit_event(
                    conn,
                    type="action.dispatched",
                    payload=dispatched_payload,
                    correlation=_action_correlation(action_request),
                )
            lifecycle.transition(action_request.action_id, "dispatched")
        return dispatched_event

    def _run_inline(  # noqa: PLR0913 — the four dispatch arguments plus the two values `dispatch` already computed.
        self,
        action_request: ActionRequest,
        conn: sqlite3.Connection,
        runtime_paths: RuntimePathsLike,
        lifecycle: ActionLifecycle,
        *,
        tool_def: ToolDefinition,
        dispatched_event_uid: str,
    ) -> RawResultBundle:
        """Run the handler on the calling thread — the pre-Wave-4B path."""
        running_event = emit_event(
            conn,
            type="action.running",
            payload={"action_id": action_request.action_id},
            source_event_id=dispatched_event_uid,
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

    def _submit_to_runner(  # noqa: PLR0913 — the four dispatch arguments plus the three values `dispatch` already resolved.
        self,
        action_request: ActionRequest,
        conn: sqlite3.Connection,
        runtime_paths: RuntimePathsLike,
        lifecycle: ActionLifecycle,
        *,
        tool_def: ToolDefinition,
        dispatched_event_uid: str,
        runner: ActionRunner,
    ) -> ActionSubmission:
        """Resolve the resource keys, then hand the job to the runner.

        Resolution runs after `action.dispatched` and before `action.running`,
        which is where ADR-0008 D9 puts it: a request that is accepted but
        whose resources cannot be named fails as an action rather than running
        unlocked.
        """
        try:
            concurrency = self._resource_key_resolver(action_request, tool_def, conn)
        except ResourceKeyResolutionError as exc:
            self._fail_before_running(
                action_request,
                conn,
                lifecycle,
                source_event_id=dispatched_event_uid,
                error_code="resource_key_resolution",
                message=str(exc),
            )
            raise

        action_id = action_request.action_id

        def _run(
            worker_conn: sqlite3.Connection,
            context: ActionExecutionContext,
        ) -> RawResult | RawResultBundle:
            """Run the handler on the runner's thread and its own connection."""
            _set_running_event_uid(worker_conn, action_id, context.running_event_uid)
            try:
                return tool_def.handler(
                    action_request, worker_conn, runtime_paths, lifecycle,
                )
            finally:
                _clear_running_event_uid(worker_conn, action_id)

        def _on_running(_running_event_uid: str) -> None:
            """Advance the in-process lifecycle once `action.running` committed."""
            lifecycle.transition(action_id, "running")

        def _on_terminal(event_type: str) -> None:
            """Advance the in-process lifecycle for a runner-written terminal.

            A cancel or an assumed timeout is written by the runner, not by
            the handler, so without this the FSM would keep calling a
            cancelled action ``running``.
            """
            state: LifecycleState = (
                "cancelled" if event_type == "action.cancelled" else "timeout_assumed"
            )
            if lifecycle.state_of(action_id) in ("dispatched", "running"):
                lifecycle.transition(action_id, state)

        def _on_dispatch_failure(
            worker_conn: sqlite3.Connection,
            exc: BaseException,
        ) -> None:
            """Terminalize a job that never reached its handler.

            The lease could not be taken, so nothing downstream will ever
            write this action's terminal. On the background path there is no
            caller left holding the handle to notice, which is exactly why
            this is the runner's hook rather than a `try` around `result()`.
            The runner hands over its own connection: this runs on the worker
            thread, and ``conn`` above belongs to the dispatching thread.
            """
            self._fail_before_running(
                action_request,
                worker_conn,
                lifecycle,
                source_event_id=dispatched_event_uid,
                error_code="resource_lease",
                message=str(exc),
            )

        def _on_finished() -> None:
            """Un-publish the action once the runner is completely done with it."""
            release_running_action(action_id)

        # Published BEFORE `submit`, for the same reason `_emit_dispatched`
        # publishes before `action.dispatched` lands: a sweep tick in
        # between must never see the action unprotected.
        register_running_action(action_id)
        try:
            return runner.submit(
                ActionJob(
                    action_id=action_id,
                    turn_id=action_request.turn_id,
                    run_id=action_request.run_id,
                    concurrency=concurrency,
                    cancellation_mode=tool_def.cancellation_mode,
                    dispatched_event_uid=dispatched_event_uid,
                    correlation=_action_correlation(action_request),
                    run=_run,
                    on_running=_on_running,
                    # Only a mutating, turn-owned lease survives quiescence:
                    # the turn's `drive_turn` finalizer is what releases it,
                    # after verify-then-stash-restore. A read-shared or
                    # untracked action has nothing left to clean up and frees
                    # at quiescence.
                    carries_cleanup_debt=(
                        concurrency.carries_cleanup_debt
                        and concurrency.parent_action_id is None
                        and action_request.turn_id is not None
                    ),
                    on_terminal=_on_terminal,
                    on_finished=_on_finished,
                    on_dispatch_failure=_on_dispatch_failure,
                ),
            )
        except BaseException:
            # `submit` refused the job outright (shutdown), so no
            # `on_finished` will ever fire for it.
            release_running_action(action_id)
            raise

    def _fail_before_running(  # noqa: PLR0913 — one terminal payload per keyword.
        self,
        action_request: ActionRequest,
        conn: sqlite3.Connection,
        lifecycle: ActionLifecycle,
        *,
        source_event_id: str,
        error_code: str,
        message: str,
    ) -> None:
        """Write one `action.failed` for a job that never reached its handler."""
        terminalize_action(
            conn,
            event_type="action.failed",
            payload={
                "action_id": action_request.action_id,
                "error": error_code,
                "reason": message,
            },
            source_event_id=source_event_id,
            correlation=_action_correlation(action_request),
        )
        if lifecycle.state_of(action_request.action_id) in ("dispatched", "running"):
            lifecycle.transition(action_request.action_id, "failed")


# --- resource-key resolution (ADR-0008 D9) ----------------------------------

_SPAWN_WORKER_TOOL_NAME: Final[str] = "spawn_worker"
_VERIFY_DIFF_TOOL_NAME: Final[str] = "verify_diff"
_CANCEL_ACTION_TOOL_NAME: Final[str] = "cancel_action"
"""The tools whose resource keys are resolved by name.

Matched by name rather than by a new ``ToolDefinition`` field because these
are the only two tools whose keys depend on runtime state (the Task Ledger's
``repo_path`` and the run being verified). Every other tool is classified by
``read_only`` alone, which the definition already carries. The literals are
repeated at the two registration sites so the AST canaries that read those
definitions keep seeing a plain string.
"""


type ResourceKeyResolver = Callable[
    [ActionRequest, ToolDefinition, "sqlite3.Connection"],
    ToolConcurrency,
]
"""Injectable seam that names the canonical resources one action will touch."""


def canonical_resource_key(path: Path) -> str:
    """Return the canonical lease key for a filesystem resource.

    ``Path.resolve()`` is the realpath ADR-0008 D9 asks for: two requests that
    name the same repository through different symlinks or relative paths must
    produce the same key or they will not serialize.
    """
    return "repo:" + str(path.resolve())


def _spawn_worker_resource_key(
    action_request: ActionRequest,
    conn: sqlite3.Connection,
) -> str:
    """Return the canonical repo key `spawn_worker` will stash and mutate.

    The fallback mirrors ``spawn_worker_handler`` exactly (``Path.cwd()`` when
    the task record carries no ``repo_path``), so the lease always names the
    tree the handler actually touches. A missing ``task_id`` is unresolvable,
    not a fallback: the handler would raise anyway, and running unlocked is
    the one outcome D9 forbids.

    Raises:
        ResourceKeyResolutionError: ``task_id`` is absent or not a string.
    """
    task_id = action_request.arguments.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        msg = (
            f"spawn_worker action {action_request.action_id!r} carries no string "
            f"'task_id'; its repository resource key cannot be resolved"
        )
        raise ResourceKeyResolutionError(msg)
    record = _load_task_record(conn, task_id)
    raw_repo = record.get("repo_path") if record is not None else None
    repo = Path(raw_repo) if isinstance(raw_repo, str) and raw_repo else Path.cwd()
    return canonical_resource_key(repo)


@dataclass(frozen=True)
class _VerifiedRun:
    """The durable provenance of the run one `verify_diff` is checking."""

    parent_action_id: str | None
    task_id: str | None


def _verify_diff_run_provenance(
    action_request: ActionRequest,
    conn: sqlite3.Connection,
) -> _VerifiedRun:
    """Resolve the run this verification checks back to its creator.

    Durable provenance, not a caller assertion: the child names a ``run_id``,
    and ``run.started`` is what binds that run to both the action that created
    it (through the correlation) and the task it belongs to (through the
    payload). Empty fields mean no such run is on the log.
    """
    run_id = action_request.arguments.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return _VerifiedRun(parent_action_id=None, task_id=None)
    for event in iter_events(conn):
        if event.type != "run.started" or event.payload.get("run_id") != run_id:
            continue
        parent = (event.correlation or {}).get("action_id")
        task_id = event.payload.get("task_id")
        return _VerifiedRun(
            parent_action_id=parent if isinstance(parent, str) and parent else None,
            task_id=task_id if isinstance(task_id, str) and task_id else None,
        )
    return _VerifiedRun(parent_action_id=None, task_id=None)


def default_resource_key_resolver(
    action_request: ActionRequest,
    tool_def: ToolDefinition,
    conn: sqlite3.Connection,
) -> ToolConcurrency:
    """Resolve one request's canonical resource keys and lease mode.

    Four rules, in ADR-0008 D9's order:

    1. ``spawn_worker`` takes ``realpath(repo)`` write-exclusively — it
       stashes, runs Codex over the tree, and later restores.
    2. ``verify_diff`` reads the same tree and borrows its parent run's scope
       instead of contending with it.
    3. a read-only tool needs no lease at all.
    4. anything else that mutates and declares no resource semantics is
       global-exclusive, which is the fail-closed default rather than a guess
       about what it touches.
    """
    if tool_def.name == _SPAWN_WORKER_TOOL_NAME:
        return ToolConcurrency(
            resource_keys=(_spawn_worker_resource_key(action_request, conn),),
            mode="write_exclusive",
            carries_cleanup_debt=True,
        )
    if tool_def.name == _VERIFY_DIFF_TOOL_NAME:
        provenance = _verify_diff_run_provenance(action_request, conn)
        return ToolConcurrency(
            resource_keys=(canonical_resource_key(_resolve_repo_path(action_request, conn=conn)),),
            mode="read_shared",
            parent_action_id=provenance.parent_action_id,
        )
    if tool_def.read_only or tool_def.name == _CANCEL_ACTION_TOOL_NAME:
        # ADR-0008 D10: `cancel_action` signals a handle and mutates no
        # resource. Taking the target's key — in any mode — would queue the
        # cancel behind the very `write_exclusive` lease it is releasing.
        return ToolConcurrency(resource_keys=(), mode="read_shared")
    return ToolConcurrency(
        resource_keys=(GLOBAL_RESOURCE_KEY,),
        mode="global_exclusive",
    )


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

# The runner's half of the same question. ADR-0008 Step 4 gave actions a
# SECOND owner: with `true_async_workers` on, `dispatch` returns an ack and
# the ActionRunner keeps running the job after `drive_turn`'s `finally` has
# dropped the turn's whole entry above. The turn table alone therefore
# reports a live Codex worker as unowned the instant its turn unwinds, and
# the supervisor sweep would `assume_timeout` it out from under L4.
#
# Keyed by action_id, not by turn: a runner job outlives its turn, and one
# with no turn at all (`turn_id=None`) was never in the table above.
# Registered by the dispatch path just before `submit`, dropped by the
# runner's `on_finished` hook once the job — cleanup included — is done.
_RUNNING_ACTIONS_LOCK: Final[threading.Lock] = threading.Lock()
_RUNNING_ACTIONS: Final[set[str]] = set()


def register_live_action(*, turn_id: str, action_id: str) -> None:
    """Publish ``action_id`` as driven by ``turn_id`` (idempotent)."""
    with _LIVE_ACTIONS_LOCK:
        _LIVE_ACTIONS_BY_TURN.setdefault(turn_id, set()).add(action_id)


def release_turn_actions(turn_id: str) -> None:
    """Drop every action_id ``turn_id`` registered. No-op if unknown."""
    with _LIVE_ACTIONS_LOCK:
        _LIVE_ACTIONS_BY_TURN.pop(turn_id, None)


def register_running_action(action_id: str) -> None:
    """Publish ``action_id`` as owned by the ActionRunner (idempotent)."""
    with _RUNNING_ACTIONS_LOCK:
        _RUNNING_ACTIONS.add(action_id)


def release_running_action(action_id: str) -> None:
    """Drop the runner's claim on ``action_id``. No-op if unknown."""
    with _RUNNING_ACTIONS_LOCK:
        _RUNNING_ACTIONS.discard(action_id)


def running_action_ids() -> frozenset[str]:
    """Snapshot every action_id the ActionRunner has not finished."""
    with _RUNNING_ACTIONS_LOCK:
        return frozenset(_RUNNING_ACTIONS)


def live_action_ids() -> frozenset[str]:
    """Snapshot every action_id L4 still owns, from EITHER owner.

    The union of the two claims: an action a live turn is driving, and an
    action whose runner job has not finished. L6 must see both — since
    ADR-0008 Step 4 a background worker outlives the turn that dispatched
    it, so "no turn owns it" no longer implies "nothing is running".
    """
    with _LIVE_ACTIONS_LOCK:
        turn_owned = frozenset(
            action_id
            for action_ids in _LIVE_ACTIONS_BY_TURN.values()
            for action_id in action_ids
        )
    return turn_owned | running_action_ids()


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


# --- cancel_action (ADR-0008 D10) --------------------------------------------
#
# The L2 control command that stops one exact, currently cancellable
# ActionRun. Registered only when an ActionRunner exists: without one there
# is no background action to cancel, and the registry, the LLM's tool list
# and every event trail stay byte-identical to the runner-less build.

_CANCEL_ACTION_INPUT_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "target_action_id": {
            "type": "string",
            "description": "action_id of the open action to stop, from the open actions list.",
        },
        "reason": {
            "type": "string",
            "description": "Why Allen wants it stopped, in a few words.",
        },
    },
    "required": ["target_action_id", "reason"],
    "additionalProperties": False,
}

_CANCEL_ACTION_QUIESCENCE_BUDGET_S: Final[float] = 30.0
"""How long the handler waits for the target to quiesce.

The same budget the runner gives its own shutdown cancel
(:meth:`ActionRunner.shutdown`). A target that does not quiesce in time
keeps running and gets no `action.cancelled` (ADR-0008 F9); the ack then
says so.
"""


def _cancel_outcome_payload(outcome: CancelOutcome) -> dict[str, Any]:
    """Serialize one :data:`CancelOutcome` variant as the tool's ack payload."""
    payload: dict[str, Any] = {"target_action_id": outcome.action_id}
    if isinstance(outcome, CancelAccepted):
        payload.update(status="accepted", event_uid=outcome.event_uid)
    elif isinstance(outcome, CancelAlreadyTerminal):
        payload.update(
            status="already_terminal",
            terminal_type=outcome.terminal_type,
            event_uid=outcome.event_uid,
        )
    elif isinstance(outcome, CancelUnconfirmed):
        payload.update(status="unconfirmed", reason=outcome.reason)
    else:
        payload["status"] = "unsupported"
    return payload


def _make_cancel_action_handler(runner: ActionRunner) -> ToolHandler:
    """Close the `cancel_action` handler over the live ActionRunner."""

    def cancel_action_handler(
        action_request: ActionRequest,
        conn: sqlite3.Connection,
        runtime_paths: RuntimePathsLike,  # noqa: ARG001 — handler signature uniformity.
        lifecycle: ActionLifecycle,
    ) -> RawResult:
        """Ask the runner to cancel the target; ack with what it answered.

        Writes no event about the target: `action.cancelled` stays the
        runner's, written only after confirmed quiescence. The only row
        this handler emits is this control action's own sync terminal.
        """
        target = action_request.arguments.get("target_action_id")
        target_id = target if isinstance(target, str) else ""
        reason = action_request.arguments.get("reason")
        context = runner.context_of(target_id)
        # The target's own declared mode, read from its live context. With
        # no context (dispatched but not yet running, or already reaped)
        # the runner answers from the durable fold whatever the mode, so
        # any supported mode reaches that path — "unsupported" would
        # short-circuit to `CancelUnsupported` before it.
        mode: CancellationMode = (
            context.cancellation_mode if context is not None else "cooperative"
        )
        outcome = runner.cancel_action(
            target_id,
            reason=reason if isinstance(reason, str) else "",
            timeout_s=_CANCEL_ACTION_QUIESCENCE_BUDGET_S,
            cancellation_mode=mode,
        )
        payload = _cancel_outcome_payload(outcome)
        tool_output_str = tool_result(payload)
        terminalize_action(
            conn,
            event_type="action.result_observed",
            payload={
                "action_id": action_request.action_id,
                "semantics": "ack",
                "tool_output": tool_output_str,
            },
            source_event_id=_get_running_event_uid(conn, action_request.action_id),
            correlation=_action_correlation(action_request),
        )
        lifecycle.transition(action_request.action_id, "result_observed")
        return RawResult(
            action_id=action_request.action_id,
            semantics="ack",
            payload=payload,
            tool_output=tool_output_str,
            error=None,
        )

    return cancel_action_handler


# --- write_file (ADR-0012 D1) ------------------------------------------------
#
# The only L3 tool: read_only=False, requires_entity=True,
# requires_confirmation=True. Dispatch reaches this handler only via a
# gated ActionRequest carrying a valid AuthorizationLease (ADR-0012 D2)
# — the Pre-action Gate's confirm_required/lease machinery is what makes
# that true, not anything in this handler.

_WRITE_FILE_ENTITY_PREFIX: Final[str] = "file:"

_WRITE_FILE_MODES: Final[frozenset[str]] = frozenset({"create", "overwrite", "append"})

_WRITE_FILE_MODE_TO_OPEN_MODE: Final[Mapping[str, str]] = {
    "create": "x",
    "overwrite": "w",
    "append": "a",
}

_WRITE_FILE_INPUT_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "target": {
            "type": "string",
            "description": (
                "Spoken name, description, or path of the file to write. "
                "Resolved to a canonical path before writing (never used as "
                "a raw path). An existing file resolves normally; a "
                "non-existent file resolves only if its parent directory is "
                "in scope."
            ),
        },
        "content": {
            "type": "string",
            "description": "Text content to write.",
        },
        "mode": {
            "type": "string",
            "description": (
                "'create' refuses if the file already exists; 'overwrite' "
                "and 'append' both require an existing file."
            ),
        },
    },
    "required": ["target", "content", "mode"],
}


def write_file_handler(  # noqa: PLR0911 — one linear resolve/validate/mode/write pass; each return is a distinct, named failure or the single success exit — splitting it would only relocate the branches, not reduce them.
    action_request: ActionRequest,
    conn: sqlite3.Connection,
    runtime_paths: RuntimePathsLike,  # noqa: ARG001 — signature uniformity.
    lifecycle: ActionLifecycle,
) -> RawResult:
    """L3 `write_file` — the confirmation flow's acceptance instrument (ADR-0012 D1).

    Writes to the RESOLVED canonical path off
    `action_request.target_entity_ref` — NEVER `arguments["target"]`,
    the raw free text the LLM supplied. Same discipline as
    `read_file_handler`: the Pre-action Gate has already required a
    trusted `"file:<abs-path>"` ref (ADR-0011 D3 + ADR-0012 D1's
    write-target resolution extension) by the time this handler runs,
    so the raw argument is never read here. `target_entity_ref is
    None` cannot pass the gate for a `requires_entity=True` tool, but
    this handler checks it anyway — a handler must be safe standing
    alone, not merely behind a gate that happens to always run first
    in production.

    Mode semantics: `create` refuses an already-existing file;
    `overwrite`/`append` both require an existing file. The existence
    check happens twice by design — once explicitly (for a clear,
    mode-specific error code) and once implicitly via the `"x"` open
    mode for `create` (atomic — closes the TOCTOU gap the explicit
    check alone would leave between check and write).

    Any I/O error (permission denied, disk full, parent directory
    vanished between resolve and dispatch, ...) becomes an error
    observation via `_emit_tool_error` and rides the EXISTING
    Limitation routing — ADR-0012 §4 failure-mode table: "write_file
    handler I/O error -> error observation -> Limitation routing
    (existing machinery)". No new error machinery is introduced here.

    Result semantics is `"ack"` (Execution Claim `executed` — the
    write-was-accepted, not content-verified; ADR-0012 D1 explicitly
    defers read-back `post_action_check` to a future ADR).
    """
    running_event_uid = _get_running_event_uid(conn, action_request.action_id)
    action_id = action_request.action_id

    ref = action_request.target_entity_ref
    if ref is None or not ref.startswith(_WRITE_FILE_ENTITY_PREFIX):
        return _emit_tool_error(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_id,
            running_event_uid=running_event_uid,
            code="no_resolved_target",
            message="write_file: no resolved target_entity_ref (the gate should have refused this)",
        )
    path = Path(ref[len(_WRITE_FILE_ENTITY_PREFIX) :])

    mode_raw = action_request.arguments.get("mode")
    mode = mode_raw if isinstance(mode_raw, str) else ""
    if mode not in _WRITE_FILE_MODES:
        return _emit_tool_error(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_id,
            running_event_uid=running_event_uid,
            code="invalid_mode",
            message=(
                f"write_file: mode must be one of {sorted(_WRITE_FILE_MODES)!r}, "
                f"got {mode_raw!r}"
            ),
        )

    content_raw = action_request.arguments.get("content")
    content = content_raw if isinstance(content_raw, str) else None
    if content is None:
        return _emit_tool_error(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_id,
            running_event_uid=running_event_uid,
            code="missing_content",
            message="write_file: 'content' argument must be a string",
        )

    exists = path.is_file()
    if mode == "create" and exists:
        return _emit_tool_error(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_id,
            running_event_uid=running_event_uid,
            code="target_exists",
            message=f"write_file: {path} already exists; mode=create refuses to overwrite it",
        )
    if mode in ("overwrite", "append") and not exists:
        return _emit_tool_error(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_id,
            running_event_uid=running_event_uid,
            code="target_not_found",
            message=f"write_file: {path} does not exist; mode={mode!r} requires an existing file",
        )

    try:
        with path.open(_WRITE_FILE_MODE_TO_OPEN_MODE[mode], encoding="utf-8") as fh:
            fh.write(content)
    except OSError as exc:
        return _emit_tool_error(
            conn=conn,
            lifecycle=lifecycle,
            action_id=action_id,
            running_event_uid=running_event_uid,
            code="write_failed",
            message=f"write_file: {exc}",
        )

    payload: dict[str, Any] = {
        "path": str(path),
        "mode": mode,
        "bytes_written": len(content.encode("utf-8")),
    }
    return _emit_tool_observation(
        conn=conn,
        lifecycle=lifecycle,
        action_id=action_id,
        running_event_uid=running_event_uid,
        payload=payload,
        semantics="ack",
    )


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
    # frozen 2026-09-12: engineering is off the LLM menu; the observer keeps it.
    allowed_callers=frozenset({CallerPrincipal.OBSERVER}),
    risk_level="L0",
    result_semantics="observation",
    is_async=False,
    input_schema=_VERIFY_DIFF_INPUT_SCHEMA,
    handler=verify_diff_handler,
    domain="git",
    read_only=True,
    requires_entity=False,
    requires_confirmation=False,
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


def build_default_registry(  # noqa: PLR0913 — every kwarg is a distinct D7 config value threaded into one tool's closure at registry-build time; bundling them into one options object defeats the point of each tool owning its own defaulted knobs.
    *,
    obsidian_vault_root: Path | None = None,
    web_search_max_results: int = DEFAULT_WEB_SEARCH_MAX_RESULTS,
    web_search_provider: str = DEFAULT_WEB_SEARCH_PROVIDER,
    web_search_api_key: str | None = None,
    web_fetch_max_bytes: int = DEFAULT_WEB_FETCH_MAX_BYTES,
    web_fetch_max_text_bytes: int = DEFAULT_WEB_FETCH_MAX_TEXT_BYTES,
    web_timeout_s: float = DEFAULT_WEB_TIMEOUT_S,
    vision_client: VisionClient | None = None,
    screen_max_width_px: int = DEFAULT_SCREEN_MAX_WIDTH_PX,
    action_runner: ActionRunner | None = None,
    resource_key_resolver: ResourceKeyResolver | None = None,
    background_async: bool = False,
    confirmation_dispatch_outbox: bool = False,
    memory_db_path: Path | None = None,
) -> ToolRegistry:
    """Assemble the default ToolRegistry (Day-1 six + ADR-0011 D5 seven).

    The composition root (`jarvis.runtime`, Step 10) calls this once at
    startup and passes the registry to L3 + L4.

    Args:
        obsidian_vault_root: Vault root for `search_notes` (ADR-0011
            D7, `tools.obsidian.vault_root`). `None` (the default, and
            what the two existing integration tests pass implicitly)
            falls back to :data:`DEFAULT_OBSIDIAN_VAULT_ROOT` — the
            composition root always supplies the real configured
            value; a hand-built test registry can point this at a
            `tmp_path` fixture instead.
        web_search_max_results: `tools.web.search_max_results` (ADR-0011
            D7) — the `web_search` default absent a request argument.
        web_search_provider: `tools.web.search_provider` — which backend
            answers `web_search`. See
            :data:`DEFAULT_WEB_SEARCH_PROVIDER` for why the keyless
            default is not the recommended one.
        web_search_api_key: The credential named by
            `tools.web.search_api_key_env`, already resolved from the
            environment by the composition root. `None` with a keyed
            provider degrades to `ddgs` with a warning rather than
            failing boot.
        web_fetch_max_bytes: `tools.web.fetch_max_bytes` (ADR-0011 D7) —
            how much of the response body `web_fetch` drains off the
            socket. A memory bound, NOT the output cap.
        web_fetch_max_text_bytes: `tools.web.fetch_max_text_bytes` —
            `web_fetch`'s real output cap, applied to the text left
            after HTML extraction.
        web_timeout_s: `tools.web.timeout_s` (ADR-0011 D7) — shared by
            `web_search` and `web_fetch` (see :data:`DEFAULT_WEB_TIMEOUT_S`
            for why D5's per-tool 15s/20s split collapses to one knob).
        vision_client: Injected `screen_look` vision seam (ADR-0011
            D5/D7). `None` (the default, and what the two existing
            integration tests pass implicitly) means no usable
            `llm.presets.vision` block was found; `screen_look` still
            registers but every call degrades to an error observation
            AFTER saving the screenshot. `jarvis.runtime` always
            supplies a real client when `llm.presets.vision` parses.
        screen_max_width_px: `tools.screen.max_width_px` (ADR-0011 D7)
            — the `sips` downscale ceiling before the vision call.
        action_runner: ADR-0008 Step 3 execution boundary. `None` (the
            default, and every caller before Wave 4B) keeps `dispatch`
            running handlers inline on the calling thread.
        confirmation_dispatch_outbox: Require atomic L2 admission of
            confirmation-backed authorized dispatch debt when enabled.
        resource_key_resolver: Overrides
            :func:`default_resource_key_resolver`. Only consulted when an
            ActionRunner is installed; a test injects one to declare
            resource semantics for a tool the default resolver would
            classify by `read_only` alone.
        background_async: ADR-0008 Step 4. `True` makes `dispatch` return
            an acknowledgement for an `is_async` tool as soon as its
            ActionRun is accepted, instead of blocking on the handle.
            Requires `action_runner`.
        memory_db_path: `memory.db_path` — registers `search_records`
            over that memory.db. `None` (hand-built test registries)
            registers no memory tool.
    """
    vault_root = (
        obsidian_vault_root if obsidian_vault_root is not None else DEFAULT_OBSIDIAN_VAULT_ROOT
    )
    registry = ToolRegistry(
        action_runner=action_runner,
        resource_key_resolver=resource_key_resolver,
        background_async=background_async,
        confirmation_dispatch_outbox=confirmation_dispatch_outbox,
    )
    registry.register(
        ToolDefinition(
            name="spawn_worker",
            description=(
                "Spawn a worker (codex stub Day-1) for the given task_id; "
                "writes diff.json artifact and reports completion asynchronously."
            ),
            # frozen 2026-09-12: engineering is off the LLM menu; no caller may reach this.
            allowed_callers=frozenset(),
            risk_level="L2",
            result_semantics="ack",
            is_async=True,
            input_schema=_SPAWN_WORKER_INPUT_SCHEMA,
            handler=spawn_worker_handler,
            domain="agent_control",
            read_only=False,
            requires_entity=False,
            requires_confirmation=False,
            # ADR-0009 D4: the only Day-2 tool that can outlive its
            # dispatch call, hence the only one carrying a supervisor
            # deadline. Passed as the resolver itself so a per-run
            # `JARVIS_CODEX_TURN_TIMEOUT_S` moves the persisted deadline
            # in step with the in-process driver deadline.
            result_budget_s=_resolve_codex_turn_timeout_s,
            # ADR-0008 D9 (Step 4). `spawn_worker_handler` polls its
            # execution context and hands `run_codex_action` a
            # `should_cancel`; the driver sends `turn/interrupt` and the
            # finalizer closes the client, which terminates and if needed
            # kills the `codex app-server` subprocess. "terminate_process"
            # rather than "cooperative" because that is what actually
            # happens to the child — the declaration has to survive being
            # read as a promise about the OS process.
            cancellation_mode="terminate_process",
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
            # frozen 2026-09-12: engineering is off the LLM menu; no caller may reach this.
            allowed_callers=frozenset(),
            risk_level="L1",
            result_semantics="ack",
            is_async=False,
            input_schema=_CREATE_TASK_INPUT_SCHEMA,
            handler=create_task_handler,
            domain="task_ledger",
            read_only=False,
            requires_entity=False,
            requires_confirmation=False,
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
            # frozen 2026-09-12: engineering is off the LLM menu; no caller may reach this.
            allowed_callers=frozenset(),
            risk_level="L0",
            result_semantics="observation",
            is_async=False,
            input_schema=_LIST_TASKS_INPUT_SCHEMA,
            handler=list_tasks_handler,
            domain="task_ledger",
            read_only=True,
            requires_entity=False,
            requires_confirmation=False,
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
            domain="state_read",
            read_only=True,
            requires_entity=False,
            requires_confirmation=False,
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
            domain="mac_gui",
            read_only=False,
            # requires_entity stays False deliberately (ADR-0011 D2
            # footnote): open_path keeps its own resolve-then-act
            # contract internally — it *is* a resolver caller — and
            # participates in the EntityRegistry as an emitter, not a
            # gate consumer. Do not "fix" this to True.
            requires_entity=False,
            requires_confirmation=False,
        )
    )
    registry.register(
        ToolDefinition(
            name="search_notes",
            description=(
                "Case-insensitive full-text search over Allen's Obsidian vault "
                "(*.md files only). Returns matching lines with their source "
                "file. No index — brute-force scan, fine for a small vault."
            ),
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L0",
            result_semantics="observation",
            is_async=False,
            input_schema=_SEARCH_NOTES_INPUT_SCHEMA,
            handler=_make_search_notes_handler(vault_root),
            domain="obsidian",
            read_only=True,
            requires_entity=False,
            requires_confirmation=False,
        )
    )
    registry.register(
        ToolDefinition(
            name="read_file",
            description=(
                "Read a text file's contents by spoken name or description. "
                "The target is resolved to a canonical path before reading — "
                "never pass a raw filesystem path. Binary files are rejected; "
                "output is capped at 8 KiB."
            ),
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L0",
            result_semantics="observation",
            is_async=False,
            input_schema=_READ_FILE_INPUT_SCHEMA,
            handler=read_file_handler,
            domain="file_read",
            read_only=True,
            requires_entity=True,
            requires_confirmation=False,
        )
    )
    registry.register(
        ToolDefinition(
            name="read_clipboard",
            description="Read the current macOS clipboard's text contents. No arguments.",
            allowed_callers=frozenset(
                {CallerPrincipal.REGEX_ROUTER, CallerPrincipal.JARVIS_LLM},
            ),
            risk_level="L0",
            result_semantics="observation",
            is_async=False,
            input_schema={"type": "object", "properties": {}, "required": []},
            handler=read_clipboard_handler,
            domain="clipboard",
            read_only=True,
            requires_entity=False,
            requires_confirmation=False,
        )
    )
    registry.register(
        ToolDefinition(
            name="create_memo",
            description=(
                "Save a short memo to Allen's memo inbox for later review. "
                "Use when Allen says '记一下 X' / '备忘 X'."
            ),
            allowed_callers=frozenset(
                {CallerPrincipal.REGEX_ROUTER, CallerPrincipal.JARVIS_LLM},
            ),
            risk_level="L1",
            result_semantics="ack",
            is_async=False,
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string", "description": "Memo text."}},
                "required": ["text"],
            },
            handler=create_memo_handler,
            domain="memo",
            read_only=False,
            requires_entity=False,
            requires_confirmation=False,
        )
    )
    registry.register(
        ToolDefinition(
            name="list_memos",
            description="List every saved memo, oldest first, with capture time. No arguments.",
            allowed_callers=frozenset(
                {CallerPrincipal.REGEX_ROUTER, CallerPrincipal.JARVIS_LLM},
            ),
            risk_level="L0",
            result_semantics="observation",
            is_async=False,
            input_schema={"type": "object", "properties": {}, "required": []},
            handler=list_memos_handler,
            domain="memo",
            read_only=True,
            requires_entity=False,
            requires_confirmation=False,
        )
    )
    registry.register(
        ToolDefinition(
            name="web_search",
            description=(
                "Search the web (DuckDuckGo) and return numbered "
                "title — url — snippet rows. Use for questions about "
                "current events or anything not already known."
            ),
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L1",
            result_semantics="observation",
            is_async=False,
            input_schema=_WEB_SEARCH_INPUT_SCHEMA,
            handler=_make_web_search_handler(
                default_max_results=web_search_max_results,
                timeout_s=web_timeout_s,
                provider=web_search_provider,
                api_key=web_search_api_key,
            ),
            domain="browser",
            read_only=True,
            requires_entity=False,
            requires_confirmation=False,
        )
    )
    registry.register(
        ToolDefinition(
            name="web_fetch",
            description=(
                "Fetch a URL's content: page title + tag-stripped readable "
                "text for HTML, raw text otherwise. Does not execute "
                "JavaScript, so a JS-rendered page may return only its shell."
            ),
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L1",
            result_semantics="observation",
            is_async=False,
            input_schema=_WEB_FETCH_INPUT_SCHEMA,
            handler=_make_web_fetch_handler(
                max_bytes=web_fetch_max_bytes,
                max_text_bytes=web_fetch_max_text_bytes,
                timeout_s=web_timeout_s,
            ),
            domain="browser",
            read_only=True,
            requires_entity=False,
            requires_confirmation=False,
        )
    )
    registry.register(
        ToolDefinition(
            name="open_url",
            description="Open a URL in the default browser. Use for '用浏览器打开 X' requests.",
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L1",
            result_semantics="ack",
            is_async=False,
            input_schema=_OPEN_URL_INPUT_SCHEMA,
            handler=open_url_handler,
            domain="mac_gui",
            read_only=False,
            requires_entity=False,
            requires_confirmation=False,
        )
    )
    registry.register(
        ToolDefinition(
            name="screen_look",
            description=(
                "Take a screenshot of Allen's screen and describe what's on "
                "it via a vision model; an optional `question` focuses the "
                "description on something specific. The decision LLM never "
                "sees the screenshot pixels — only this tool's returned "
                "text description."
            ),
            allowed_callers=frozenset(
                {CallerPrincipal.REGEX_ROUTER, CallerPrincipal.JARVIS_LLM},
            ),
            risk_level="L1",
            result_semantics="observation",
            is_async=False,
            input_schema=_SCREEN_LOOK_INPUT_SCHEMA,
            handler=_make_screen_look_handler(
                vision_client=vision_client,
                max_width_px=screen_max_width_px,
            ),
            domain="screen",
            read_only=True,
            requires_entity=False,
            requires_confirmation=False,
        )
    )
    registry.register(
        ToolDefinition(
            name="write_file",
            description=(
                "Write text content to a file by spoken name, description, "
                "or path. The target is resolved to a canonical path before "
                "writing — never pass a raw filesystem path. mode='create' "
                "refuses an existing file; 'overwrite'/'append' both require "
                "one. Risk L3 — every dispatch requires Allen's explicit "
                "confirmation."
            ),
            # frozen 2026-09-12: engineering is off the LLM menu; no caller may reach this.
            allowed_callers=frozenset(),
            risk_level="L3",
            result_semantics="ack",
            is_async=False,
            input_schema=_WRITE_FILE_INPUT_SCHEMA,
            handler=write_file_handler,
            domain="file_write",
            read_only=False,
            requires_entity=True,
            requires_confirmation=True,
        )
    )
    if memory_db_path is not None:
        registry.register(
            ToolDefinition(
                name="search_records",
                description=(
                    "Search the memory store of everything Allen said and every "
                    "answer given, newest first, with timestamps. Call this whenever "
                    "Allen asks what he said before (我之前说过什么 / 刚才说的 / "
                    "昨天说的 / 上周二说的) or refers to an earlier conversation, and "
                    "quote the original words and their time back to him. Filter by "
                    "keyword substring and/or an ISO 8601 time range; every argument "
                    "is optional."
                ),
                allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
                risk_level="L0",
                result_semantics="observation",
                is_async=False,
                input_schema=_SEARCH_RECORDS_INPUT_SCHEMA,
                handler=_make_search_records_handler(memory_db_path),
                domain="memo",
                read_only=True,
                requires_entity=False,
                requires_confirmation=False,
            )
        )
    if action_runner is not None:
        # ponytail: the cancel still takes one runner run slot, so with
        # `max_concurrent_runs=1` it waits behind its own target; skip the
        # slot for resource-free control actions if that ever ships.
        registry.register(
            ToolDefinition(
                name=_CANCEL_ACTION_TOOL_NAME,
                description=(
                    "Request cancellation of one exact, currently cancellable "
                    "ActionRun. Use when Allen asks to stop, cancel or abort a "
                    "running action."
                ),
                # frozen 2026-09-12: engineering is off the LLM menu; no caller may reach this.
                allowed_callers=frozenset(),
                # ADR-0008 D10: L2 is the fixed risk of the cancellation
                # command; the target's risk never transfers. Under the L3
                # confirmation threshold that makes `requires_confirmation`
                # False, which boot validation requires at L2.
                risk_level="L2",
                result_semantics="ack",
                is_async=False,
                input_schema=_CANCEL_ACTION_INPUT_SCHEMA,
                handler=_make_cancel_action_handler(action_runner),
                domain="agent_control",
                read_only=False,
                requires_entity=True,
                requires_confirmation=False,
                post_action_check=None,
                result_budget_s=None,
            )
        )
    return registry


__all__ = [
    "DEFAULT_OBSIDIAN_VAULT_ROOT",
    "DEFAULT_SCREEN_MAX_WIDTH_PX",
    "DEFAULT_WEB_FETCH_MAX_BYTES",
    "DEFAULT_WEB_FETCH_MAX_TEXT_BYTES",
    "DEFAULT_WEB_SEARCH_MAX_RESULTS",
    "DEFAULT_WEB_SEARCH_PROVIDER",
    "DEFAULT_WEB_TIMEOUT_S",
    "VERIFY_DIFF_TOOL_DEF",
    "ActionLifecycle",
    "CallerNotAllowedError",
    "DuplicateToolError",
    "IllegalLifecycleTransition",
    "LifecycleState",
    "PostActionCheck",
    "RawResult",
    "RawResultBundle",
    "ResourceKeyResolver",
    "ResultSemantics",
    "RuntimePathsLike",
    "ToolDefinition",
    "ToolRegistry",
    "ToolRegistryError",
    "UnknownToolError",
    "VisionClient",
    "build_default_registry",
    "canonical_resource_key",
    "create_task_handler",
    "default_resource_key_resolver",
    "get_current_time_handler",
    "list_tasks_handler",
    "live_action_ids",
    "open_path_handler",
    "open_url_handler",
    "read_clipboard_handler",
    "read_file_handler",
    "register_live_action",
    "release_turn_actions",
    "spawn_worker_handler",
    "tool_error",
    "tool_result",
    "turn_action_ids",
    "validate_egress_url",
    "verify_diff_handler",
    "write_file_handler",
]
