"""Composition root — the only module allowed to wire across middle layers.

Per ADR 0001 § Six-layer boundary contract and ``.importlinter`` layer
rules: ``jarvis.runtime`` is the SINGLE module in the codebase that may
import ``jarvis.decision``, ``jarvis.execution``, ``jarvis.surface``,
``jarvis.deployment`` simultaneously. Every other module respects the
sibling-isolation contract via Protocols.

Responsibilities (Day-1):

1. :func:`bootstrap_runtime_app` — load YAML config, render the system
   prompt, bootstrap L6 paths, open the L2 event log, build the L4
   default registry + lifecycle, instantiate the L3 LLM client, and
   return a frozen :class:`JarvisRuntime`. No LLM call happens during
   bootstrap.
2. :func:`run_turn` — drive ONE conversation turn end-to-end:
   emit ``utterance.received`` (L5), call :func:`jarvis.decision.decide`
   (re-entering as more triggers arrive), record the Pre-emit token,
   and render the final ``ResponsePlan`` to stdout (L5).
3. :func:`_wait_for_next_trigger` — poll the event log for the next
   L3 trigger event (``worker.reported`` or ``action.result_observed``)
   produced by L4's ``threading.Timer`` worker thread. ``time.sleep``
   is intentional here per spec §3.4.1: the composition root polls
   across thread boundaries; the "no time.sleep" rule applies only
   to L3 / L4 gate machinery.

Layer rules: ``jarvis.runtime`` may import everything below it. It is
imported by ``jarvis.cli`` only.
"""

from __future__ import annotations

import io
import json
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import yaml

from jarvis.decision import DecideContext, LifecycleLike, ToolRegistryLike, decide
from jarvis.decision.llm import LLMClient, load_llm_config
from jarvis.deployment import RuntimePaths, bootstrap_runtime
from jarvis.execution.tools import ActionLifecycle, ToolRegistry, build_default_registry
from jarvis.shared import Event
from jarvis.state.event_log import open_event_log
from jarvis.surface.cli import (
    PreEmitTokenError,
    SurfaceState,
    emit_utterance_received,
    record_pre_emit_token,
    write_output,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping

    from jarvis.decision import ResponsePlan


# --- Defaults ---------------------------------------------------------------

# Default config / prompt asset paths relative to the repo root. The
# composition root resolves the repo root by walking up from this module
# until a ``config/jarvis.yaml`` exists; tests / users may override.
_DEFAULT_CONFIG_FILENAME = Path("config") / "jarvis.yaml"
_DEFAULT_PROMPT_FILENAME = Path("prompts") / "jarvis_v1.md"

# Trigger event types the runtime loop expects from L4 async paths. Day-1
# only ``worker.reported`` (from the spawn_worker Timer) and
# ``action.result_observed`` (defensive — sync tools emit this inline so
# decide() consumes it within one invocation, but we accept it here so a
# late-firing scheduled re-entry does not deadlock the poll loop).
_RUNTIME_TRIGGER_TYPES: tuple[str, ...] = ("worker.reported", "action.result_observed")

# Default polling cadence for ``_wait_for_next_trigger``. 10 ms balances
# CPU usage with first-byte latency once the Timer fires.
_DEFAULT_POLL_INTERVAL_S: float = 0.01

# Default per-turn iteration ceiling. Protects ``run_turn`` from a stuck
# trigger chain. ADR § Acceptance F2 puts the Day-1 happy path at ~3
# iterations (utterance -> worker.reported -> verify-result composition),
# so 50 is ample headroom.
_DEFAULT_MAX_ITERATIONS: int = 50

# Default per-trigger wait. The spawn_worker Timer fires at ~10 ms in
# Day-1; ``verify_diff`` re-entry happens inline. 5 s is generous.
_DEFAULT_TRIGGER_TIMEOUT_S: float = 5.0


# --- Exceptions -------------------------------------------------------------


class RuntimeBootstrapError(RuntimeError):
    """Raised when :func:`bootstrap_runtime_app` cannot assemble the runtime."""


class TriggerWaitTimeout(RuntimeError):  # noqa: N818 — Day-1 vocabulary keeps `Timeout` suffix per ADR § Multi-trigger loop.
    """Raised by :func:`_wait_for_next_trigger` when no trigger arrives in time."""


# --- Public dataclasses -----------------------------------------------------


@dataclass(frozen=True)
class JarvisRuntime:
    """Assembled runtime — every cross-layer wire lives here.

    Frozen so the composition root can hand it across to ``run_turn``
    and tests / canary scans without worrying about mutation. The
    ``conn`` field is the only mutable resource by nature
    (``sqlite3.Connection``); everything else is immutable values.

    L5 :class:`SurfaceState` is deliberately not a field here.
    Per spec §3.6.7 (Inherent boundaries), local surface state has no
    truth, doesn't survive across turns, and doesn't affect decisions —
    so ``run_turn`` allocates a fresh empty :class:`SurfaceState`
    locally each invocation and discards it after :func:`write_output`.

    Attributes:
        config: Raw parsed YAML config (read-only mapping form).
        runtime_paths: Bootstrapped L6 layout (root + event_log +
            artifacts_root + registry).
        conn: Open Event Log connection. The caller closes it when
            the process exits.
        tool_registry: L4 default registry (spawn_worker + verify_diff).
        lifecycle: L4 per-process action lifecycle FSM.
        llm_client: L3 multi-provider LLM client.
        system_prompt: Rendered system prompt string (verbatim
            content of ``prompts/jarvis_v1.md``).
    """

    config: Mapping[str, Any]
    runtime_paths: RuntimePaths
    conn: sqlite3.Connection
    tool_registry: ToolRegistry
    lifecycle: ActionLifecycle
    llm_client: LLMClient
    system_prompt: str


@dataclass(frozen=True)
class RunTurnResult:
    """Outcome of :func:`run_turn` for one conversation turn.

    Attributes:
        response_text: The exact text the surface adapter wrote to
            stdout (post channel-split: ``document`` channel, falling
            back to ``voice`` if document was empty).
        response_plan: The final approved :class:`ResponsePlan` from
            :func:`jarvis.decision.decide`.
        turn_id: Correlation id used across the trace.
        iterations: Number of :func:`jarvis.decision.decide` invocations
            this turn drove (1 for sync-only, 2 for the Day-1 happy
            path with one ``worker.reported`` re-entry).
        events_emitted: Frozen tuple of every Event the decide()
            invocations emitted (concatenated across iterations).
    """

    response_text: str
    response_plan: ResponsePlan
    turn_id: str
    iterations: int
    events_emitted: tuple[Event, ...]


# --- bootstrap_runtime_app --------------------------------------------------


def _locate_repo_root(start: Path) -> Path:
    """Walk up from ``start`` until ``config/jarvis.yaml`` exists; return that dir."""
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / _DEFAULT_CONFIG_FILENAME).is_file():
            return candidate
    msg = (
        f"runtime: could not locate config/jarvis.yaml by walking up from {start}. "
        "Pass config_path=Path(...) explicitly to bootstrap_runtime_app."
    )
    raise RuntimeBootstrapError(msg)


def _load_full_config(path: Path) -> Mapping[str, Any]:
    """Parse the entire YAML config file (not just the ``llm:`` block)."""
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        msg = f"config at {path} is not a YAML mapping"
        raise RuntimeBootstrapError(msg)
    return raw


def bootstrap_runtime_app(
    *,
    config_path: Path | None = None,
    prompt_path: Path | None = None,
    runtime_root: Path | None = None,
) -> JarvisRuntime:
    """Assemble a frozen :class:`JarvisRuntime` ready for :func:`run_turn`.

    Wiring (bottom-up across the layer DAG):

    1. L6 bootstrap — :func:`jarvis.deployment.bootstrap_runtime` resolves
       the runtime root (env var / explicit / default) and creates the
       base directories.
    2. L2 event log — :func:`jarvis.state.event_log.open_event_log`
       opens (or creates) the SQLite spine, idempotently installing
       the schema / indexes / append-only triggers.
    3. L4 registry + lifecycle — :func:`jarvis.execution.tools.build_default_registry`
       registers ``spawn_worker`` + ``verify_diff``; the
       :class:`ActionLifecycle` is per-process FSM.
    4. L3 LLM client — :class:`jarvis.decision.llm.LLMClient` reads the
       ``llm:`` block from the YAML config. Lazy SDK construction —
       no network call until ``run_turn`` actually invokes ``chat``.

    L5 :class:`SurfaceState` is allocated per turn inside :func:`run_turn`
    (spec §3.6.7 — local surface state owns no truth and doesn't survive
    across turns), so the composition root does not wire it here.

    Args:
        config_path: Optional explicit path to ``config/jarvis.yaml``.
            Default: resolved by walking up from this module's file.
        prompt_path: Optional explicit path to ``prompts/jarvis_v1.md``.
            Default: ``${repo_root}/prompts/jarvis_v1.md``.
        runtime_root: Optional override for the L6 runtime root
            (tests / sandboxed runs).

    Returns:
        Frozen :class:`JarvisRuntime`.

    Raises:
        RuntimeBootstrapError: When config / prompt files cannot be
            located or parsed.
    """
    # Path resolution. The composition root tolerates either
    # repo-rooted defaults or explicit overrides; we never look at
    # ``os.getcwd()``.
    if config_path is None:
        repo_root = _locate_repo_root(Path(__file__).parent)
        config_path = repo_root / _DEFAULT_CONFIG_FILENAME
    else:
        repo_root = config_path.resolve().parent.parent

    if prompt_path is None:
        prompt_path = repo_root / _DEFAULT_PROMPT_FILENAME

    if not config_path.is_file():
        msg = f"runtime: config file {config_path} does not exist"
        raise RuntimeBootstrapError(msg)
    if not prompt_path.is_file():
        msg = f"runtime: prompt file {prompt_path} does not exist"
        raise RuntimeBootstrapError(msg)

    # 1. L6 paths.
    paths = bootstrap_runtime(runtime_root)

    # 2. L2 event log.
    conn = open_event_log(paths.event_log)

    # 3. L4 registry + lifecycle.
    registry = build_default_registry()
    lifecycle = ActionLifecycle()

    # 4. L3 LLM client.
    full_config = _load_full_config(config_path)
    llm_config = load_llm_config(config_path)
    llm_client = LLMClient(llm_config)

    # 5. Prompt.
    system_prompt = prompt_path.read_text(encoding="utf-8")

    return JarvisRuntime(
        config=full_config,
        runtime_paths=paths,
        conn=conn,
        tool_registry=registry,
        lifecycle=lifecycle,
        llm_client=llm_client,
        system_prompt=system_prompt,
    )


# --- run_turn ---------------------------------------------------------------


def _new_turn_id() -> str:
    """Mint a fresh turn id (``"T" + 8-hex``)."""
    return "T" + uuid.uuid4().hex[:8]


def _latest_row_id(conn: sqlite3.Connection) -> int:
    """Return the current MAX(events.id) or 0 if the table is empty."""
    cursor = conn.execute("SELECT IFNULL(MAX(id), 0) FROM events")
    row = cursor.fetchone()
    if row is None:
        return 0
    return int(row[0])


_SELECT_NEXT_TRIGGER_SQL = (  # noqa: S608 — placeholders interpolation is over a hard-coded type tuple, not user input.
    "SELECT id, event_uid, type, schema_version, ts_epoch_ms, "
    "payload_json, source_event_id, correlation_json "
    "FROM events WHERE id > ? AND type IN ({placeholders}) "
    "ORDER BY id ASC LIMIT 1"
).format(placeholders=",".join("?" for _ in _RUNTIME_TRIGGER_TYPES))


def _hydrate_event_row(row: tuple[Any, ...]) -> Event:
    """Re-hydrate an events-table row into a frozen :class:`Event`.

    Mirrors ``jarvis.state.event_log._row_to_event`` (which is module-private
    by L2 design). The runtime composition root may legitimately read
    rows back out of the log when waiting for cross-thread triggers.
    """
    (
        _id,
        event_uid,
        type_,
        schema_version,
        ts_epoch_ms,
        payload_json,
        source_event_id,
        correlation_json,
    ) = row
    payload: dict[str, Any] = json.loads(payload_json)
    correlation: dict[str, str] | None = (
        None if correlation_json is None else json.loads(correlation_json)
    )
    return Event(
        event_uid=event_uid,
        type=type_,
        schema_version=schema_version,
        ts_epoch_ms=ts_epoch_ms,
        payload=payload,
        source_event_id=source_event_id,
        correlation=correlation,
    )


def _wait_for_next_trigger(
    conn: sqlite3.Connection,
    *,
    after_id: int,
    timeout: float,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
) -> tuple[Event, int]:
    """Poll the event log for the next L3 trigger event after ``after_id``.

    Day-1 trigger types: ``worker.reported`` (spawn_worker Timer
    thread) and ``action.result_observed`` (defensive — sync tools
    emit this inline so decide() already absorbed it, but a late
    re-entry from a stale Timer is accepted to keep the poll loop
    drainable).

    The runtime composition root explicitly polls across thread
    boundaries; ``time.sleep`` is the cleanest primitive here. The
    "no time.sleep" rule is for L3/L4 gate machinery that must not
    block on wall-clock; the orchestration loop is a different
    concern (spec §3.4.1).

    Args:
        conn: Open Event Log connection. NOTE: this is the same
            connection :func:`bootstrap_runtime_app` opened — the
            ``threading.Timer`` callback opens its OWN connection per
            ``jarvis.execution.tools`` so ``check_same_thread=True``
            stays honored.
        after_id: The most recent SQLite row id the caller has
            already consumed. Returned events all have ``id >
            after_id``.
        timeout: Hard wall-clock cap in seconds; raise
            :class:`TriggerWaitTimeout` if exceeded.
        poll_interval_s: Sleep between polls (default 10 ms).

    Returns:
        Tuple of ``(event, new_after_id)`` — the freshly-folded
        :class:`Event` plus the SQLite row id to use for the next
        wait call.

    Raises:
        TriggerWaitTimeout: No matching trigger arrived within
            ``timeout`` seconds.
    """
    deadline = time.monotonic() + timeout
    while True:
        cursor = conn.execute(
            _SELECT_NEXT_TRIGGER_SQL,
            (after_id, *_RUNTIME_TRIGGER_TYPES),
        )
        row = cursor.fetchone()
        if row is not None:
            row_id = int(row[0])
            return _hydrate_event_row(row), row_id
        if time.monotonic() >= deadline:
            msg = (
                f"runtime: no trigger event of types {_RUNTIME_TRIGGER_TYPES!r} "
                f"arrived within {timeout!r}s (after_id={after_id})."
            )
            raise TriggerWaitTimeout(msg)
        time.sleep(poll_interval_s)


def run_turn(
    runtime: JarvisRuntime,
    *,
    utterance: str,
    turn_id: str | None = None,
    max_iterations: int = _DEFAULT_MAX_ITERATIONS,
    trigger_timeout_s: float = _DEFAULT_TRIGGER_TIMEOUT_S,
) -> RunTurnResult:
    """Drive one conversation turn end-to-end.

    Steps (spec §3.4.1 multi-trigger loop):

    1. Mint or accept a ``turn_id`` (composition root owns this; L3
       sees the same id on the trace correlation).
    2. Emit ``utterance.received`` via L5 surface adapter — this is
       the first trigger event.
    3. Call :func:`jarvis.decision.decide`. If it returns a
       :class:`ResponsePlan`, finalize immediately.
    4. Otherwise, the decide() call paused on an async tool
       (spawn_worker); poll the event log for the next
       ``worker.reported`` / ``action.result_observed`` row, re-enter
       decide() with that trigger, repeat.
    5. Record the Pre-emit token on the surface state, then call
       :func:`write_output` which both renders the document side of
       the channel split AND enforces the token check (canary H3).

    The Pre-emit token check protects against a runtime that
    accidentally re-uses an old plan or fails to refresh the token —
    :class:`PreEmitTokenError` propagates out.

    Args:
        runtime: Assembled :class:`JarvisRuntime`.
        utterance: User text to drive the turn.
        turn_id: Optional explicit turn id (tests). Default: minted.
        max_iterations: Hard ceiling on decide() invocations.
        trigger_timeout_s: Per-trigger wait timeout.

    Returns:
        Frozen :class:`RunTurnResult` describing what was written and
        the events emitted.
    """
    effective_turn_id = turn_id if turn_id is not None else _new_turn_id()

    utterance_event = emit_utterance_received(
        runtime.conn,
        transcript=utterance,
        turn_id=effective_turn_id,
    )

    decide_ctx = DecideContext(
        conn=runtime.conn,
        runtime_paths=runtime.runtime_paths,
        # ``ToolRegistry`` / ``ActionLifecycle`` satisfy the L3
        # Protocols structurally; the casts pin the boundary because
        # mypy's invariant generic stance over Protocol attribute
        # types treats the more-specific RawResult / LifecycleState
        # return types as a conflict.
        tool_registry=cast("ToolRegistryLike", runtime.tool_registry),
        lifecycle=cast("LifecycleLike", runtime.lifecycle),
        llm_client=runtime.llm_client,
        system_prompt=runtime.system_prompt,
    )

    # SQLite row id of the utterance.received event — used as the
    # "after_id" anchor for the trigger poll loop.
    last_seen_id = _latest_row_id(runtime.conn)

    collected_events: list[Event] = []
    trigger_event: Event = utterance_event
    response_plan: ResponsePlan | None = None
    iterations = 0

    while response_plan is None and iterations < max_iterations:
        iterations += 1
        result = decide(trigger_event, decide_ctx)
        collected_events.extend(result.events_emitted)
        response_plan = result.response_plan
        if response_plan is not None:
            break

        # No final plan -> decide() paused on an async tool. Wait for the
        # next L4-side trigger event.
        next_event, last_seen_id = _wait_for_next_trigger(
            runtime.conn,
            after_id=last_seen_id,
            timeout=trigger_timeout_s,
        )
        trigger_event = next_event

    if response_plan is None:
        msg = (
            f"run_turn: exhausted max_iterations={max_iterations} without a final "
            f"ResponsePlan (turn_id={effective_turn_id!r})."
        )
        raise RuntimeBootstrapError(msg)

    # L5 emission. The Pre-emit token guard inside write_output() is
    # the canary H3 runtime check — calling record_pre_emit_token()
    # then write_output() in this order is the only legal path.
    # SurfaceState is allocated fresh per turn (spec §3.6.7 — local
    # surface state owns no truth and doesn't survive across turns)
    # and discarded once write_output returns the cleared state.
    surface_state = SurfaceState(last_gate_response_hash=None)
    primed_state = record_pre_emit_token(surface_state, response_plan.response_hash)

    # Capture the rendered text so we can both echo it to the operator
    # console (sys.stdout) and return it to the caller for tests /
    # programmatic clients. write_output() handles the channel split
    # and the Pre-emit token check.
    capture: io.StringIO = io.StringIO()
    write_output(primed_state, response_plan, stream=capture)
    rendered = capture.getvalue()
    sys.stdout.write(rendered)
    sys.stdout.flush()

    return RunTurnResult(
        response_text=rendered,
        response_plan=response_plan,
        turn_id=effective_turn_id,
        iterations=iterations,
        events_emitted=tuple(collected_events),
    )


__all__ = [
    "JarvisRuntime",
    "PreEmitTokenError",
    "RunTurnResult",
    "RuntimeBootstrapError",
    "TriggerWaitTimeout",
    "bootstrap_runtime_app",
    "run_turn",
]
