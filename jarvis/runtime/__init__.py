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
   emit ``surface.user_intent`` (L5), call :func:`jarvis.decision.decide`
   (re-entering as more triggers arrive), record the Pre-emit token,
   and render the final ``ResponsePlan`` to stdout (L5).
3. :func:`_wait_for_next_trigger` — poll the event log for the next
   L3 trigger event (``worker.reported``, ``action.result_observed``,
   ``action.timeout_assumed``, or ``action.failed``) produced by L4's
   ``threading.Timer`` worker thread. ``time.sleep`` is intentional
   here per spec §3.4.1: the composition root polls across thread
   boundaries; the "no time.sleep" rule applies only to L3 / L4 gate
   machinery.

Layer rules: ``jarvis.runtime`` may import everything below it. It is
imported by ``jarvis.cli`` only.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import logging
import os
import re
import sqlite3
import sys
import time
import uuid
from collections.abc import Mapping  # runtime use: isinstance in the config readers.
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

import yaml

from jarvis.decision import (
    DecideContext,
    EntityResolverLike,
    LifecycleLike,
    ResolvedEntityLike,
    ToolRegistryLike,
    decide,
    emit_turn_ended,
)
from jarvis.decision.confirm_grammar import ConfirmGrammarConfigError, load_confirm_grammar
from jarvis.decision.cost_guard import CostRecorder
from jarvis.decision.llm import LLMClient, load_llm_config
from jarvis.decision.llm_session import LLMSessionFactory
from jarvis.decision.packet import assemble_packet
from jarvis.decision.policy import (
    PolicyConsistencyError,
    effective_policy,
    validate_requires_confirmation,
)
from jarvis.decision.pre_route import (
    ROUTINE_ATTENTION_CHANNEL,
    RoutineStreamRoute,
    StreamCorrection,
    ToolCueConfigError,
    ToolCueTable,
    load_tool_cues,
    pre_route,
    routine_risk_context,
)
from jarvis.decision.response_run import (
    CancelAccepted,
    CancelAlreadyTerminal,
    CancelTimedOut,
    ResponseCancelledError,
    ResponseCancelRequest,
    ResponseRun,
    ResponseRunRegistry,
    ResponseTerminalizer,
    decide_foreground,
    evidence_snapshot_hash,
    legacy_full_text_policy,
    request_response_cancel,
    start_response_run,
)
from jarvis.decision.result_interpreter import emit_stash_conflict_surfacing
from jarvis.decision.stream_gate import routine_stream_policy
from jarvis.decision.tier0 import Tier0ConfigError, load_tier0_table, validate_tier0_table
from jarvis.deployment import RuntimePaths, bootstrap_runtime, load_env_file
from jarvis.execution.action_runner import ActionRunner, VerificationOutcome
from jarvis.execution.diff_capture import StashError, restore_pretask_changes
from jarvis.execution.path_resolver import (
    FileTargetsConfigError,
    load_file_targets_config,
    resolve_write_target,
)
from jarvis.execution.path_resolver import resolve as resolve_file_entity
from jarvis.execution.tools import (
    DEFAULT_OBSIDIAN_VAULT_ROOT,
    DEFAULT_SCREEN_MAX_WIDTH_PX,
    DEFAULT_WEB_FETCH_MAX_BYTES,
    DEFAULT_WEB_FETCH_MAX_TEXT_BYTES,
    DEFAULT_WEB_SEARCH_MAX_RESULTS,
    DEFAULT_WEB_SEARCH_PROVIDER,
    DEFAULT_WEB_TIMEOUT_S,
    ActionLifecycle,
    ToolRegistry,
    VisionClient,
    build_default_registry,
    release_turn_actions,
    turn_action_ids,
)
from jarvis.runtime.stream_bridge import LoopBoundTokenStream
from jarvis.shared import CallerPrincipal, Event
from jarvis.shared.action_admission import bind_action_admission
from jarvis.shared.pricing import load_pricing_table
from jarvis.shared.realtime import (
    RESPONSE_CANCEL_REASONS,
    AlreadyTerminal,
    Wave1FeatureFlags,
    Wave4ActionFlags,
    Wave4ResponseFlags,
    Wave5InputFlags,
    new_response_id,
)
from jarvis.shared.realtime_trace import (
    configure_realtime_trace_jsonl,
    record_realtime_trace,
)
from jarvis.state.committed_event_bus import CommittedEventBus
from jarvis.state.event_log import iter_events, open_event_log, open_runtime_event_log
from jarvis.state.stream_emission import committed_text_prefix
from jarvis.state.trigger_consumption import mark_trigger_consumed
from jarvis.surface.cli import (
    PreEmitTokenError,
    SurfaceState,
    emit_surface_user_intent,
    parse_response_channels,
    record_pre_emit_token,
)
from jarvis.surface.cli_render import render_response
from jarvis.surface.stream_emission import emit_permitted_segment

if TYPE_CHECKING:
    from collections.abc import Callable

    from jarvis.decision import ResponsePlan
    from jarvis.decision.confirm_grammar import ConfirmGrammarTable
    from jarvis.decision.tier0 import Tier0Table
    from jarvis.shared.realtime_trace import TraceValue


LOGGER = logging.getLogger("jarvis.runtime")


# --- Defaults ---------------------------------------------------------------

# Default config / prompt asset paths relative to the repo root. The
# composition root resolves the repo root by walking up from this module
# until a ``config/jarvis.yaml`` exists; tests / users may override.
_DEFAULT_CONFIG_FILENAME = Path("config") / "jarvis.yaml"
_DEFAULT_PROMPT_FILENAME = Path("prompts") / "jarvis_v1.md"

# Trigger event types the runtime loop expects from L4 async paths.
# ``worker.reported`` is the spawn_worker happy-path Timer event.
# ``action.result_observed`` is defensive — sync tools emit it inline
# so decide() consumes it within one invocation, but we accept it here
# so a late-firing scheduled re-entry does not deadlock the poll loop.
# ``action.timeout_assumed`` and ``action.failed`` are the spawn_worker
# terminal failure events (B-0003b): the Codex turn timed out or the
# subprocess crashed; the runtime must wake decide() so L3 can fold a
# Limitation Claim onto the trace and emit a canonical limitation
# response. Without these in the trigger set the runtime waiter would
# deadlock and the user would see silence after a 10-min Codex hang.
_RUNTIME_TRIGGER_TYPES: tuple[str, ...] = (
    "worker.reported",
    "action.result_observed",
    "action.timeout_assumed",
    "action.failed",
    # ADR-0008 D9 (Step 4). Once `spawn_worker` is truly background, a cancel
    # is a fourth way for the action a turn is waiting on to end, and it is
    # the only one the handler does not write itself. Without it here the
    # cancelled turn sits in the waiter until its trigger timeout expires
    # even though its terminal is already durable.
    "action.cancelled",
)

# Default polling cadence for ``_wait_for_next_trigger``. 10 ms balances
# CPU usage with first-byte latency once the Timer fires.
_DEFAULT_POLL_INTERVAL_S: float = 0.01

# Default per-turn iteration ceiling. Protects ``run_turn`` from a stuck
# trigger chain. ADR § Acceptance F2 puts the Day-1 happy path at ~3
# iterations (utterance -> worker.reported -> verify-result composition),
# so 50 is ample headroom.
_DEFAULT_MAX_ITERATIONS: int = 50

# Conversational per-trigger wait: the budget for a turn with NO
# background async worker of its own in flight. The spawn_worker Timer
# fires at ~10 ms in Day-1; ``verify_diff`` re-entry happens inline. 5 s
# is generous, and small enough that a stuck ordinary turn cannot pin one
# of ``max_concurrent_turns`` for long. A turn that IS waiting on a
# background worker gets the runner's lease timeout instead — see
# :func:`_trigger_wait_budget`.
_DEFAULT_TRIGGER_TIMEOUT_S: float = 5.0

# Fallback for a runtime whose config carries no ``observer:`` block
# (hand-assembled test runtimes). ``config/jarvis.yaml`` is the real
# source; mirroring the shipped default here means a missing block reads
# the same cadence the daemon would actually poll at.
_FALLBACK_OBSERVER_POLL_INTERVAL_S: float = 60.0

# Fallback for a runtime whose config carries no ``confirmation:`` block.
# Mirrors ``jarvis.decision._DEFAULT_CONFIRMATION_TTL_MS`` (private in
# that module — not imported here, same as
# ``_FALLBACK_OBSERVER_POLL_INTERVAL_S`` above keeps its own mirror
# rather than importing ``packet.DEFAULT_OBSERVER_POLL_INTERVAL_S``).
_FALLBACK_CONFIRMATION_TTL_MS: int = 600_000

# ADR-0014 D14 expiry sweep cadence, and the floor it is clamped to. The
# floor exists because the sweep opens a write transaction on a worker
# thread: a sub-second interval would contend with real turns for the
# SQLite writer for no gain, since `confirmation.ttl_ms` defaults to ten
# minutes and the live burn's shortest useful value is seconds.
_FALLBACK_CONFIRMATION_EXPIRY_SWEEP_INTERVAL_S: float = 30.0
_MIN_CONFIRMATION_EXPIRY_SWEEP_INTERVAL_S: float = 5.0

# ADR-0008 §6 `realtime.response.cancel_timeout_ms` default. Implemented as
# the cancel connection's SQLite `busy_timeout`, so it bounds how long the
# terminal CAS waits for a contended writer — never an in-flight provider
# call, which has no cancellation seam until ADR-0008 Step 6.
_FALLBACK_CANCEL_TIMEOUT_MS: int = 500

# ADR-0008 §6 `realtime.actions.lease_timeout_s` default — see
# `jarvis.execution.action_runner._DEFAULT_LEASE_TIMEOUT_S` for why 900 s.
_FALLBACK_LEASE_TIMEOUT_S: float = 900.0

# Default `tools.screen.vision_preset` (ADR-0011 D7) — the `llm.presets.*`
# key `screen_look` reads for its one vision call when the config's
# `tools.screen` block doesn't override it.
_DEFAULT_VISION_PRESET_NAME: str = "vision"

# System prompt for the injected vision client (`_LLMVisionClient` below).
# Directed at the vision model, not Allen, so it stays English; asking for
# a Chinese answer means `screen_look`'s text observation slots straight
# into the rest of Jarvis's Chinese-speaking pipeline without a translation
# hop.
_VISION_SYSTEM_PROMPT: str = (
    "You are a screen-reading assistant. Describe what is currently "
    "visible in the screenshot factually and concisely. Respond in "
    "Chinese (中文)."
)

# Fallback ``tools.obsidian.vault_root`` (ADR-0011 D7) for a runtime
# whose config carries no ``tools:`` block. NIT-FIX 8 (ADR-0011 §12):
# unlike ``_FALLBACK_OBSERVER_POLL_INTERVAL_S`` above, this one does
# NOT hold its own copy of the literal — two copies of the same vault
# path in two layers can silently drift. `jarvis.execution.tools`
# (L4, the layer this fallback exists for) owns
# ``DEFAULT_OBSIDIAN_VAULT_ROOT``; `jarvis.runtime` just imports it —
# a higher-layer-imports-lower-layer edge `.importlinter` allows.


# --- Exceptions -------------------------------------------------------------


class RuntimeBootstrapError(RuntimeError):
    """Raised when :func:`bootstrap_runtime_app` cannot assemble the runtime."""


class TriggerWaitTimeout(RuntimeError):  # noqa: N818 — Day-1 vocabulary keeps `Timeout` suffix per ADR § Multi-trigger loop.
    """Raised by :func:`_wait_for_next_trigger` when no trigger arrives in time."""


# --- ADR-0011 D4 — resolve-on-propose wiring --------------------------------


@dataclass(frozen=True)
class _ResolvedFileEntity:
    """Adapts `path_resolver.ResolvedTarget` to L3's `ResolvedEntityLike`.

    `jarvis.runtime` is the only module allowed to know both the L3
    Protocol shape and the L4 `ResolvedTarget` dataclass it adapts —
    that is the whole point of the resolve-on-propose seam (ADR-0011
    D4): L3 never imports `jarvis.execution.path_resolver` directly.
    """

    entity_id: str
    canonical: str
    confidence: str
    match_basis: str


def _make_entity_resolver(conn: sqlite3.Connection) -> EntityResolverLike:
    """Build the resolve-on-propose callable (ADR-0011 D4), closing over `conn`.

    Wired into `DecideContext.entity_resolver`; `_dispatch_one_tool_call`
    calls it for a `requires_entity=True` tool whose `target_entity_ref`
    is still unset. `path_resolver.resolve` never raises for a bad or
    unresolvable query (its own docstring's contract) — a miss is the
    normal `None` return, not an exception, so this wrapper adds no
    try/except of its own; a real bug inside `resolve` should surface,
    not be swallowed here.
    """

    def _resolve(query: str) -> ResolvedEntityLike | None:
        target = resolve_file_entity(query, "file", conn)
        if target is None:
            return None
        return _ResolvedFileEntity(
            entity_id=f"file:{target.path}",
            canonical=str(target.path),
            confidence="bookmark" if target.source == "bookmark" else "fuzzy",
            match_basis=target.source,
        )

    return _resolve


def _make_write_entity_resolver(conn: sqlite3.Connection) -> EntityResolverLike:
    """Build the `write_file` resolve-on-propose callable (ADR-0012 D1).

    Mirrors :func:`_make_entity_resolver` but calls
    `path_resolver.resolve_write_target` instead of `path_resolver.resolve`,
    so a non-existent target whose parent directory is in scope also
    resolves (D1's extension of ADR-0011 D4 for write targets). Wired
    into `DecideContext.write_entity_resolver`;
    `_dispatch_one_tool_call` picks this resolver instead of
    `entity_resolver` when the tool being resolved is `write_file`.
    """

    def _resolve(query: str) -> ResolvedEntityLike | None:
        target = resolve_write_target(query, conn)
        if target is None:
            return None
        return _ResolvedFileEntity(
            entity_id=f"file:{target.path}",
            canonical=str(target.path),
            confidence="bookmark" if target.source == "bookmark" else "fuzzy",
            match_basis=target.source,
        )

    return _resolve


def _entity_bookmarks() -> tuple[tuple[str, str], ...]:
    """Load `config/file_targets.yaml` bookmarks as `(alias, abs-path)` pairs.

    Seeds the EntityRegistry projection's config route (ADR-0011 D4).
    `jarvis.state` may not import `jarvis.execution.path_resolver`, so
    the composition root loads the config here and threads the pairs
    down as plain data — the same reason `tier0_table` is loaded here
    and threaded rather than re-parsed inside `jarvis.decision`.
    """
    return tuple(
        (alias, str(path))
        for alias, path in load_file_targets_config().bookmarks.items()
    )


# --- Public dataclasses -----------------------------------------------------


@dataclass(frozen=True)
class JarvisRuntime:
    """Assembled runtime — every cross-layer wire lives here.

    Frozen so the composition root can hand it across to ``run_turn``
    and tests / canary scans without worrying about mutation. The
    ``conn`` field is the only mutable resource by nature
    (``sqlite3.Connection``); everything else is immutable values —
    with two deliberate ADR-0008 Wave-4A exceptions, ``response_runs``
    and ``committed_event_bus``. Both are internally locked live
    resources shared BY REFERENCE through the ``dataclasses.replace``
    copy the daemon hands to each turn worker thread, which is the
    mechanism: the event-loop thread's cancel route must be able to see
    a run the worker thread registered. Same posture as ``conn`` and
    ``lifecycle``, which are already mutable resources on this frozen
    dataclass.

    L5 :class:`SurfaceState` is deliberately not a field here.
    Per spec §3.6.7 (Inherent boundaries), local surface state has no
    truth, doesn't survive across turns, and doesn't affect decisions —
    so ``run_turn`` allocates a fresh empty :class:`SurfaceState`
    locally each invocation and discards it after
    :func:`jarvis.surface.cli_render.render_response`.

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
        tier0_table: Spec §17 Tier 0 whitelist loaded from
            ``config/tier0_patterns.yaml``; empty tuple = Tier 0
            disabled.
        entity_bookmarks: ``(alias, absolute-path)`` pairs loaded from
            ``config/file_targets.yaml`` (ADR-0011 D4) — seeds the
            EntityRegistry projection's config route. Empty tuple =
            no bookmarks configured.
        confirm_grammar_table: ADR-0012 §3 D6 exact-sentence yes/no
            grammar loaded from ``config/confirm_grammar.yaml``; empty
            tuple = the answer-path grammar hook disabled (same "off
            means inert" posture as an empty ``tier0_table``).
    """

    config: Mapping[str, Any]
    runtime_paths: RuntimePaths
    conn: sqlite3.Connection
    tool_registry: ToolRegistry
    lifecycle: ActionLifecycle
    llm_client: LLMClient
    system_prompt: str
    tier0_table: Tier0Table = ()
    entity_bookmarks: tuple[tuple[str, str], ...] = ()
    confirm_grammar_table: ConfirmGrammarTable = ()
    wave1_features: Wave1FeatureFlags = field(default_factory=Wave1FeatureFlags)
    response_flags: Wave4ResponseFlags = field(default_factory=Wave4ResponseFlags)
    action_flags: Wave4ActionFlags = field(default_factory=Wave4ActionFlags)
    action_runner: ActionRunner | None = None
    llm_session_factory: LLMSessionFactory | None = None
    response_runs: ResponseRunRegistry | None = None
    committed_event_bus: CommittedEventBus | None = None
    input_flags: Wave5InputFlags = field(default_factory=Wave5InputFlags)
    # ADR-0008 Step 8 — tool cues loaded from ``config/tool_cues.yaml``;
    # empty tuple = no cue can veto the routine route (the other pre-route
    # conditions still apply).
    tool_cues: ToolCueTable = ()


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
        attention_channel: L3 Attention Policy verdict from the final
            decide() invocation. Drives the Step-18 surface-render
            dispatch in :func:`jarvis.surface.cli_render.render_response`.
    """

    response_text: str
    response_plan: ResponsePlan
    turn_id: str
    iterations: int
    events_emitted: tuple[Event, ...]
    attention_channel: str


@dataclass(frozen=True)
class WaitingTurn:
    """Connection-free checkpoint while L4 works; live turn ownership stays held."""

    intent: Event
    run: ResponseRun | None
    after_id: int
    action_ids: frozenset[str]
    deadline: float
    iterations: int
    events: tuple[Event, ...]
    trigger: Event | None = None
    failure: Exception | None = None
    queued: bool = False


class TurnSuspended(Exception):  # noqa: N818 — internal scheduler handoff, not a failure.
    """Transfer a quiet turn back to the runtime scheduler."""

    def __init__(self, checkpoint: WaitingTurn) -> None:
        """Carry only connection-free state across worker invocations."""
        super().__init__(checkpoint.intent.payload["turn_id"])
        self.checkpoint = checkpoint


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


def _positive_float(value: object, fallback: float) -> float:
    """Coerce a YAML scalar to a positive float; ``fallback`` on anything else.

    ``bool`` is excluded explicitly because it is an ``int`` subclass —
    ``poll_interval_s: true`` would otherwise become a 1-second poll.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return fallback
    return float(value) if value > 0 else fallback


def _observer_poll_interval_s(config: Mapping[str, Any]) -> float:
    """Return ``observer.poll_interval_s`` in seconds (ADR-0009 D5).

    Read by TWO consumers, which is the whole reason it lives here in the
    composition root rather than inside the daemon module: the observer
    task polls at this cadence, and :func:`drive_turn` hands the same
    number to L3 so the Status Board note's stale threshold (3x the poll
    interval, ADR-0009 D6 v0) is derived from the cadence the daemon
    actually runs at. Configure the interval away from 60 and the note's
    judgement moves with it instead of silently diverging.
    """
    block = config.get("observer")
    if not isinstance(block, Mapping):
        return _FALLBACK_OBSERVER_POLL_INTERVAL_S
    return _positive_float(
        block.get("poll_interval_s"), _FALLBACK_OBSERVER_POLL_INTERVAL_S,
    )


def _confirmation_ttl_ms(config: Mapping[str, Any]) -> int:
    """Return ``confirmation.ttl_ms`` (ADR-0012 §3 D4/V2 — default 10 min).

    Config-overridable so the live burn (ADR-0012 §7 acceptance row
    C3, TTL expiry) can use a short value without touching code. Same
    fill-only-with-fallback shape as :func:`_observer_poll_interval_s`
    above; ``bool`` is excluded explicitly for the same reason
    :func:`_positive_float` excludes it (``bool`` is an ``int``
    subclass).
    """
    block = config.get("confirmation")
    if not isinstance(block, Mapping):
        return _FALLBACK_CONFIRMATION_TTL_MS
    value = block.get("ttl_ms")
    if isinstance(value, bool) or not isinstance(value, int):
        return _FALLBACK_CONFIRMATION_TTL_MS
    return value if value > 0 else _FALLBACK_CONFIRMATION_TTL_MS


def _confirmation_expiry_sweep_interval_s(config: Mapping[str, Any]) -> float:
    """Return ``confirmation.expiry_sweep_interval_s``, clamped to its floor.

    Reuses the existing top-level ``confirmation:`` block that already owns
    ``ttl_ms`` — the deadline and the cadence that notices it belong
    together. A value under the floor downgrades once, with one warning,
    rather than being honored.
    """
    block = config.get("confirmation")
    if not isinstance(block, Mapping):
        return _FALLBACK_CONFIRMATION_EXPIRY_SWEEP_INTERVAL_S
    interval = _positive_float(
        block.get("expiry_sweep_interval_s"),
        _FALLBACK_CONFIRMATION_EXPIRY_SWEEP_INTERVAL_S,
    )
    if interval < _MIN_CONFIRMATION_EXPIRY_SWEEP_INTERVAL_S:
        LOGGER.warning(
            "confirmation.expiry_sweep_interval_s clamped: requested %.3fs, "
            "effective %.1fs (floor)",
            interval,
            _MIN_CONFIRMATION_EXPIRY_SWEEP_INTERVAL_S,
        )
        return _MIN_CONFIRMATION_EXPIRY_SWEEP_INTERVAL_S
    return interval


def _durable_confirmation_expiry_enabled(config: Mapping[str, Any]) -> bool:
    """Resolve ``realtime.confirmation.durable_expiry.enabled`` (ADR-0014 D14).

    This flag writes durable ``confirmation.expired`` rows, and a row cannot
    be unwritten, so it defaults false and additionally requires
    ``realtime.enabled``; requesting it without the parent downgrades once,
    with one warning, to today's lazy read-time expiry.
    """
    realtime = config.get("realtime")
    if not isinstance(realtime, Mapping):
        return False
    block = realtime.get("confirmation")
    durable = block.get("durable_expiry") if isinstance(block, Mapping) else None
    requested = (
        durable.get("enabled") is True if isinstance(durable, Mapping) else False
    )
    if not requested:
        return False
    if realtime.get("enabled") is not True:
        LOGGER.warning(
            "realtime.confirmation.durable_expiry downgraded "
            "(realtime_parent_disabled): requested enabled=True; effective False",
        )
        record_realtime_trace(
            "confirmation_expiry_activation_downgraded",
            reason="realtime_parent_disabled",
        )
        return False
    return True


def _wave1_feature_flags(config: Mapping[str, Any]) -> Wave1FeatureFlags:
    """Parse opt-in Wave 1 adoption flags; missing/malformed means all off."""
    realtime = config.get("realtime")
    if not isinstance(realtime, Mapping) or realtime.get("enabled") is not True:
        return Wave1FeatureFlags()
    safety = realtime.get("concurrency_safety")
    return Wave1FeatureFlags.from_mapping(safety if isinstance(safety, Mapping) else None)


@dataclass(frozen=True)
class _Wave4ResponseActivation:
    """Validated Wave-4A activation decision; the whole flag graph, once."""

    flags: Wave4ResponseFlags
    requested: Wave4ResponseFlags
    reason: str


def _wave4_response_activation(config: Mapping[str, Any]) -> _Wave4ResponseActivation:
    """Resolve the ADR-0008 Step 2 flag graph in exactly one place.

    Every precondition for the two Wave-4A switches lives here rather than
    being re-derived at each combination point — that ad-hoc ladder is the
    root cause of the flag drift the Wave 0-3 audit found. Rules run in
    order and produce at most one downgrade per boot, each with one warning
    and one ``response_activation_downgraded`` trace point:

    1. ``realtime`` absent / not a mapping / ``realtime.enabled`` not True.
    2. ``response_run_lifecycle`` requested without the Wave-1
       transactional-append and lifecycle-terminal-CAS primitives it writes
       through.
    3. ``independent_response_cancel``, ``typed_conversation_history``,
       ``routine_streaming`` or ``lifecycle_commentary`` requested without a
       surviving ``response_run_lifecycle``. Cancellation, typed history,
       routine streaming and D6 commentary all require stable response
       lifecycle identities — without the lifecycle switch no ResponseRun,
       terminalizer or registry is constructed at all.
    """
    realtime = config.get("realtime")
    if not isinstance(realtime, Mapping):
        return _Wave4ResponseActivation(
            flags=Wave4ResponseFlags(),
            requested=Wave4ResponseFlags(),
            reason="not_requested",
        )
    response_raw = realtime.get("response")
    commentary_raw = realtime.get("commentary")
    requested = Wave4ResponseFlags.from_mapping(
        response_raw if isinstance(response_raw, Mapping) else None,
        commentary=commentary_raw if isinstance(commentary_raw, Mapping) else None,
    )
    if realtime.get("enabled") is not True:
        if not requested.all_disabled:
            return _downgraded_response_activation(requested, "realtime_parent_disabled")
        return _Wave4ResponseActivation(
            flags=Wave4ResponseFlags(),
            requested=requested,
            reason="not_requested",
        )

    wave1 = _wave1_feature_flags(config)
    if requested.response_run_lifecycle and not (
        wave1.transactional_event_append and wave1.lifecycle_terminal_cas
    ):
        return _downgraded_response_activation(requested, "wave1_primitives_disabled")
    if (
        requested.independent_response_cancel
        or requested.typed_conversation_history
        or requested.routine_streaming
        or requested.lifecycle_commentary
    ) and not requested.response_run_lifecycle:
        return _downgraded_response_activation(requested, "lifecycle_flag_disabled")
    return _Wave4ResponseActivation(
        flags=requested,
        requested=requested,
        reason="validated" if not requested.all_disabled else "not_requested",
    )


def _downgraded_response_activation(
    requested: Wave4ResponseFlags,
    reason: str,
) -> _Wave4ResponseActivation:
    """Log and trace one downgrade, then return the safe flag set.

    ``lifecycle_flag_disabled`` drops only the cancel seam and keeps whatever
    the operator asked for on the lifecycle switch; every other reason drops
    both switches, because the run lifecycle is what the cancel seam needs to
    exist at all. A downgrade never *enables* a switch the config left off.
    """
    flags = (
        Wave4ResponseFlags(
            response_run_lifecycle=requested.response_run_lifecycle,
            independent_response_cancel=False,
        )
        if reason == "lifecycle_flag_disabled"
        else Wave4ResponseFlags()
    )
    LOGGER.warning(
        "realtime.response downgraded (%s): requested response_run_lifecycle=%s "
        "independent_response_cancel=%s typed_conversation_history=%s "
        "routine_streaming=%s lifecycle_commentary=%s; effective "
        "response_run_lifecycle=%s independent_response_cancel=%s "
        "typed_conversation_history=%s routine_streaming=%s "
        "lifecycle_commentary=%s",
        reason,
        requested.response_run_lifecycle,
        requested.independent_response_cancel,
        requested.typed_conversation_history,
        requested.routine_streaming,
        requested.lifecycle_commentary,
        flags.response_run_lifecycle,
        flags.independent_response_cancel,
        flags.typed_conversation_history,
        flags.routine_streaming,
        flags.lifecycle_commentary,
    )
    record_realtime_trace(
        "response_activation_downgraded",
        reason=reason,
        requested=(
            f"response_run_lifecycle={requested.response_run_lifecycle},"
            f"independent_response_cancel={requested.independent_response_cancel},"
            f"typed_conversation_history={requested.typed_conversation_history},"
            f"routine_streaming={requested.routine_streaming},"
            f"lifecycle_commentary={requested.lifecycle_commentary}"
        ),
    )
    return _Wave4ResponseActivation(flags=flags, requested=requested, reason=reason)


def _wave4_response_flags(config: Mapping[str, Any]) -> Wave4ResponseFlags:
    """Return the validated Wave-4A flags for ``config``."""
    return _wave4_response_activation(config).flags


def _wave4_action_flags(config: Mapping[str, Any]) -> Wave4ActionFlags:
    """Resolve the ADR-0008 Step 3 switch, with the same preconditions as 4A.

    ``action_runner`` writes `action.running`, the cleanup trio and every
    canonical terminal through the Wave-1 transactional-append and
    lifecycle-terminal-CAS primitives, so requesting it without them (or
    without ``realtime.enabled``) downgrades once, with one warning, to the
    inline dispatch path.
    """
    realtime = config.get("realtime")
    if not isinstance(realtime, Mapping):
        return Wave4ActionFlags()
    actions_raw = realtime.get("actions")
    requested = Wave4ActionFlags.from_mapping(
        actions_raw if isinstance(actions_raw, Mapping) else None,
    )
    if requested.all_disabled:
        return requested
    wave1 = _wave1_feature_flags(config)
    reason: str | None = None
    if realtime.get("enabled") is not True:
        reason = "realtime_parent_disabled"
    elif not (wave1.transactional_event_append and wave1.lifecycle_terminal_cas):
        reason = "wave1_primitives_disabled"
    if reason is None:
        if requested.true_async_workers and not requested.action_runner:
            # Nothing would own the work after `dispatch` returned.
            LOGGER.warning(
                "realtime.actions downgraded (action_runner_disabled): requested "
                "true_async_workers=True; effective true_async_workers=False",
            )
            record_realtime_trace(
                "action_activation_downgraded",
                reason="action_runner_disabled",
            )
            return Wave4ActionFlags(action_runner=False, true_async_workers=False)
        return requested
    LOGGER.warning(
        "realtime.actions downgraded (%s): requested action_runner=%s, "
        "true_async_workers=%s; effective both False",
        reason,
        requested.action_runner,
        requested.true_async_workers,
    )
    record_realtime_trace("action_activation_downgraded", reason=reason)
    return Wave4ActionFlags()


def _wave5_input_flags(config: Mapping[str, Any]) -> Wave5InputFlags:
    """Resolve the ADR-0008 D8 intent-pump switch.

    Parallel decisions require authorization consumption, isolated response
    clients, atomic accounting, and runner ownership as well as input claims.
    """
    realtime = config.get("realtime")
    if not isinstance(realtime, Mapping):
        return Wave5InputFlags()
    input_raw = realtime.get("input")
    requested = Wave5InputFlags.from_mapping(
        input_raw if isinstance(input_raw, Mapping) else None,
    )
    if requested.all_disabled:
        return requested
    wave1 = _wave1_feature_flags(config)
    reason: str | None = None
    if realtime.get("enabled") is not True:
        reason = "realtime_parent_disabled"
    elif not all((
        wave1.transactional_event_append,
        wave1.lifecycle_terminal_cas,
        wave1.confirmation_dispatch_outbox,
        wave1.exactly_once_cost_accounting,
    )):
        reason = "concurrency_safety_disabled"
    elif not _wave4_response_flags(config).response_run_lifecycle:
        reason = "response_lifecycle_disabled"
    elif not _wave4_action_flags(config).action_runner:
        reason = "action_runner_disabled"
    if reason is None:
        return requested
    LOGGER.warning(
        "realtime.input downgraded (%s): requested "
        "intent_pump=True; effective intent_pump=False",
        reason,
    )
    record_realtime_trace(
        "input_activation_downgraded",
        reason=reason,
    )
    return Wave5InputFlags()


def _positive_int(
    config: Mapping[str, Any],
    *,
    section: str,
    key: str,
    fallback: int,
) -> int:
    """Read one positive int from ``realtime.<section>.<key>``; fail closed."""
    realtime = config.get("realtime")
    if not isinstance(realtime, Mapping):
        return fallback
    block = realtime.get(section)
    if not isinstance(block, Mapping):
        return fallback
    value = block.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        if value is not None:
            LOGGER.warning(
                "realtime.%s.%s=%r is not a positive int; using %d",
                section,
                key,
                value,
                fallback,
            )
        return fallback
    return value


def _cancel_timeout_ms(config: Mapping[str, Any]) -> int:
    """Read ``realtime.response.cancel_timeout_ms``; fail closed to 500 ms."""
    realtime = config.get("realtime")
    if not isinstance(realtime, Mapping):
        return _FALLBACK_CANCEL_TIMEOUT_MS
    response = realtime.get("response")
    if not isinstance(response, Mapping):
        return _FALLBACK_CANCEL_TIMEOUT_MS
    value = response.get("cancel_timeout_ms")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        if value is not None:
            LOGGER.warning(
                "realtime.response.cancel_timeout_ms=%r is not a positive int; using %d",
                value,
                _FALLBACK_CANCEL_TIMEOUT_MS,
            )
        return _FALLBACK_CANCEL_TIMEOUT_MS
    return value


def _observer_repo_paths(config: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the watched repos from ``observer.repos``; empty = observer off.

    ``~`` is expanded here so a config line like ``~/Projects/jarvis``
    does not degrade into a silent per-cycle F6 skip. Non-string and
    blank entries are dropped rather than raising: a typo in one row of
    an Allen-managed list must not refuse to boot the daemon.
    """
    block = config.get("observer")
    if not isinstance(block, Mapping):
        return ()
    raw = block.get("repos")
    if not isinstance(raw, list):
        return ()
    return tuple(
        str(Path(item).expanduser())
        for item in raw
        if isinstance(item, str) and item.strip()
    )


def _obsidian_vault_root(config: Mapping[str, Any]) -> Path:
    """Return `tools.obsidian.vault_root`, `~`-expanded (ADR-0011 D7).

    Threaded into `build_default_registry`'s `search_notes` closure at
    registry-build time — L4 handlers do not load YAML themselves
    (same reason `tier0_table` / `observer_poll_interval_s` are read
    here and threaded down rather than re-parsed inside `jarvis.decision`
    or `jarvis.execution`). A missing/malformed `tools:` or `obsidian:`
    block degrades to the shipped default rather than failing boot —
    `search_notes` on a misconfigured key still resolves quietly to
    "vault not found" (same posture as `_observer_poll_interval_s`).
    """
    block = config.get("tools")
    if isinstance(block, Mapping):
        obsidian_block = block.get("obsidian")
        if isinstance(obsidian_block, Mapping):
            raw = obsidian_block.get("vault_root")
            if isinstance(raw, str) and raw.strip():
                return Path(raw).expanduser()
    return DEFAULT_OBSIDIAN_VAULT_ROOT


def _web_search_provider_config(config: Mapping[str, Any]) -> tuple[str, str | None]:
    """Return `(search_provider, api_key)` from `tools.web.*`.

    The key is resolved HERE, from the env var named by
    `tools.web.search_api_key_env` — same indirection the LLM presets
    use (`api_key_env`), so no credential is ever written into
    `config/jarvis.yaml`. L4 receives the resolved value; it does not
    read YAML or the environment itself.

    `search_api_key_env` is OPTIONAL: left blank it derives from the
    provider (`exa` -> `EXA_API_KEY`). An explicit name that belongs to
    a DIFFERENT provider is refused rather than honoured — pointing
    `search_provider: tavily` at `EXA_API_KEY` would post one vendor's
    credential to the other vendor's server, which is a leak, not a
    misconfiguration to paper over. Refusing yields no key, so
    `_resolve_search_backend` degrades to `ddgs` with its own warning.

    Best-effort like its `tools.web` siblings: a missing block, an
    unknown provider name, or an unset variable all degrade inside
    `_resolve_search_backend` rather than failing boot.
    """
    provider = DEFAULT_WEB_SEARCH_PROVIDER
    raw_key_env: object = None
    block = config.get("tools")
    if isinstance(block, Mapping):
        web_block = block.get("web")
        if isinstance(web_block, Mapping):
            raw_provider = web_block.get("search_provider")
            if isinstance(raw_provider, str) and raw_provider.strip():
                provider = raw_provider.strip()
            raw_key_env = web_block.get("search_api_key_env")

    key_env = f"{provider.strip().upper()}_API_KEY"
    if isinstance(raw_key_env, str) and raw_key_env.strip():
        key_env = raw_key_env.strip()
        if not _key_env_matches_provider(key_env, provider):
            LOGGER.error(
                "tools.web.search_api_key_env=%r does not belong to "
                "search_provider=%r — refusing to send that credential to the "
                "wrong vendor; leave the key blank to derive it automatically",
                key_env, provider,
            )
            return provider, None

    api_key = os.environ.get(key_env)
    return provider, (api_key.strip() if api_key else None)


def _key_env_matches_provider(key_env: str, provider: str) -> bool:
    """Is `key_env` plausibly the credential for `provider`?

    Only guards the case that actually leaks: a variable named for one
    KNOWN provider while a different KNOWN provider is selected. A name
    mentioning neither (an operator's own convention, e.g.
    `JARVIS_SEARCH_KEY`) is left alone — this is a footgun guard, not a
    naming policy.
    """
    known = ("exa", "tavily")
    selected = provider.strip().lower()
    if selected not in known:
        return True
    upper = key_env.upper()
    mentioned = [name for name in known if name.upper() in upper]
    return not mentioned or selected in mentioned


def _web_tools_config(config: Mapping[str, Any]) -> tuple[int, int, int, float]:
    """Return `(search_max_results, fetch_max_bytes, fetch_max_text_bytes, timeout_s)`.

    Read from `tools.web.*`.

    ADR-0011 D7. Same best-effort posture as `_obsidian_vault_root` — a
    missing/malformed `tools.web` block degrades to the shipped
    defaults rather than failing boot; these are UX knobs, not trust
    sources.

    D7's schema ships ONE `timeout_s` shared by `web_search` and
    `web_fetch`, though D5's prose separately gives the two tools
    different timeouts (15s / 20s) — see `jarvis.execution.tools`'s
    `DEFAULT_WEB_TIMEOUT_S` docstring for the full reconciliation
    (ADR-0011 §12 errata candidate). The single configured value is
    applied to both tools here.
    """
    search_max_results = DEFAULT_WEB_SEARCH_MAX_RESULTS
    fetch_max_bytes = DEFAULT_WEB_FETCH_MAX_BYTES
    fetch_max_text_bytes = DEFAULT_WEB_FETCH_MAX_TEXT_BYTES
    timeout_s = DEFAULT_WEB_TIMEOUT_S
    block = config.get("tools")
    if isinstance(block, Mapping):
        web_block = block.get("web")
        if isinstance(web_block, Mapping):
            raw_results = web_block.get("search_max_results")
            if (
                isinstance(raw_results, int)
                and not isinstance(raw_results, bool)
                and raw_results > 0
            ):
                search_max_results = raw_results
            raw_bytes = web_block.get("fetch_max_bytes")
            if isinstance(raw_bytes, int) and not isinstance(raw_bytes, bool) and raw_bytes > 0:
                fetch_max_bytes = raw_bytes
            raw_text_bytes = web_block.get("fetch_max_text_bytes")
            if (
                isinstance(raw_text_bytes, int)
                and not isinstance(raw_text_bytes, bool)
                and raw_text_bytes > 0
            ):
                fetch_max_text_bytes = raw_text_bytes
            raw_timeout = web_block.get("timeout_s")
            if (
                isinstance(raw_timeout, (int, float))
                and not isinstance(raw_timeout, bool)
                and raw_timeout > 0
            ):
                timeout_s = float(raw_timeout)
    return search_max_results, fetch_max_bytes, fetch_max_text_bytes, timeout_s


def _screen_tools_config(config: Mapping[str, Any]) -> tuple[str, int]:
    """Return `(vision_preset_name, max_width_px)` from `tools.screen.*` (ADR-0011 D7).

    Same best-effort posture as `_web_tools_config` — a missing/malformed
    `tools.screen` block degrades to the shipped defaults rather than
    failing boot; these are UX/cost knobs, not trust sources.
    """
    preset_name = _DEFAULT_VISION_PRESET_NAME
    max_width_px = DEFAULT_SCREEN_MAX_WIDTH_PX
    block = config.get("tools")
    if isinstance(block, Mapping):
        screen_block = block.get("screen")
        if isinstance(screen_block, Mapping):
            raw_preset = screen_block.get("vision_preset")
            if isinstance(raw_preset, str) and raw_preset.strip():
                preset_name = raw_preset
            raw_width = screen_block.get("max_width_px")
            if isinstance(raw_width, int) and not isinstance(raw_width, bool) and raw_width > 0:
                max_width_px = raw_width
    return preset_name, max_width_px


_VISION_CALL_TIMEOUT_S: Final[float] = 20.0
"""Bound on the vision preset's OpenAI SDK call (MUST-FIX 2, ADR-0011
§12). Same 20 s order as `DEFAULT_WEB_TIMEOUT_S` (Step 6's web tools) —
every other seam in `screen_look` is bounded (`_SCREEN_CAPTURE_TIMEOUT_S`
/ `_SIPS_TIMEOUT_S` = 10 s each), yet without this the one network call
had NO timeout: the SDK's own default is `Timeout(connect=5, read=600,
write=600, pool=600)` with `max_retries=2`, i.e. up to ~1800 s blocking
the synchronous `decide()` loop on a hung proxy. Deliberately scoped to
ONLY the vision client — the decision loop's own `LLMClient` (built
separately, below) shares the same unbounded-timeout omission, but a
`deep` preset with a large `max_tokens` can legitimately run long;
bounding it is a separate decision, out of this ADR's scope."""

_VISION_CALL_MAX_RETRIES: Final[int] = 1
"""Paired with `_VISION_CALL_TIMEOUT_S` so the worst case is a small
multiple of the timeout (~40 s: one attempt + one retry) rather than
half an hour."""

_VISION_ERROR_EXCERPT_MAX_CHARS: Final[int] = 300
"""Bound on the redacted excerpt `VisionCallError` carries (MUST-FIX 1a,
ADR-0011 §12) — independent of, and a first line of defense ahead of,
`_emit_tool_error`'s own byte cap in `jarvis.execution.tools`."""

_BASE64_DATA_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"data:[\w./+-]*;base64,[A-Za-z0-9+/=]+",
)
_LONG_BASE64_RUN_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9+/]{64,}={0,2}")


class VisionCallError(RuntimeError):
    """Raised by `_LLMVisionClient.describe_image` in place of the raw SDK error.

    MUST-FIX 1a, ADR-0011 §12: this is the ONE call site that holds the
    screenshot's base64 data URL, so an upstream (proxy or provider)
    that echoes the request body it failed on — a common thin-proxy
    error shape — would otherwise ship image bytes into `str(exc)`,
    which `screen_look`'s handler interpolates verbatim into
    `action.result_observed` (durable SQLite, spec §3.3.9 forbids this)
    and into the tool-result message the decision LLM sees. Redaction
    is by PATTERN (a `data:...;base64,` URL, or any standalone long
    base64-alphabet run), not by trusting the upstream's error shape —
    the whole point is that an arbitrary upstream can echo the request
    in a format this code has never seen.
    """


def _redact_vision_error(exc: Exception) -> str:
    """Scrub base64 image payloads out of a vision-call exception's message.

    Returns `"{type name}: {bounded, redacted excerpt}"` — diagnosable
    (the real exception type survives) without risking a fresh
    un-redacted echo downstream. `raise ... from None` at the call site
    severs the exception chain so nothing that later prints a full
    traceback (e.g. `__cause__`/`__context__`) can resurrect the
    original, un-redacted message either.
    """
    text = str(exc)
    text = _BASE64_DATA_URL_RE.sub("data:[redacted-base64]", text)
    text = _LONG_BASE64_RUN_RE.sub("[redacted-base64]", text)
    if len(text) > _VISION_ERROR_EXCERPT_MAX_CHARS:
        text = text[:_VISION_ERROR_EXCERPT_MAX_CHARS] + "…[truncated]"
    return f"{type(exc).__name__}: {text}"


class _LLMVisionClient:
    """Adapts `LLMClient` (L3) to `jarvis.execution.tools.VisionClient` (ADR-0011 D5/D7).

    L4 cannot import L3 (`.importlinter` sibling isolation), so
    `screen_look`'s one vision call is injected through this
    composition-root-only adapter. Reads the image bytes and
    base64-encodes them HERE, at call time, inside the vision request
    only — never in the event-log payload (spec §3.3.9 / ADR-0011 §3
    D5): the L4 handler only ever hands this a `Path` and gets a `str`
    back.
    """

    def __init__(
        self,
        llm_client: LLMClient,
        *,
        cost_recorder: CostRecorder | None = None,
    ) -> None:
        """Wrap a vision-preset-bound `LLMClient` (see `_build_vision_client`)."""
        self._llm_client = llm_client
        self._cost_recorder = cost_recorder

    def describe_image(self, image_path: Path, *, question: str | None) -> str:
        """Send one image + optional question to the vision preset; return text.

        Raises `VisionCallError` — never the raw SDK/HTTP exception —
        on any failure (MUST-FIX 1a, ADR-0011 §12): see that class's
        docstring for why.
        """
        data_url = "data:image/png;base64," + base64.b64encode(
            image_path.read_bytes(),
        ).decode("ascii")
        prompt_text = question or "Describe what is currently on this screen."
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_text},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ]
        try:
            if self._cost_recorder is None:
                result = self._llm_client.chat(
                    messages=messages,
                    system=_VISION_SYSTEM_PROMPT,
                )
            else:
                result = self._cost_recorder.chat(
                    self._llm_client,
                    messages=messages,
                    system=_VISION_SYSTEM_PROMPT,
                    kind="vision",
                    turn_id=None,
                )
        except Exception as exc:  # noqa: BLE001 — deliberately re-raised, scrubbed, as VisionCallError; see MUST-FIX 1a.
            raise VisionCallError(_redact_vision_error(exc)) from None
        return result.text or ""


class _RequestScopedVisionClient:
    """Mint isolated provider and accounting state in the calling worker."""

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        event_log_path: Path | None,
        pricing_path: Path | None,
    ) -> None:
        self._factory = LLMSessionFactory(config)
        self._snapshot = self._factory.snapshot()
        self._event_log_path = event_log_path
        self._pricing = {} if pricing_path is None else load_pricing_table(pricing_path)

    def describe_image(self, image_path: Path, *, question: str | None) -> str:
        """Perform one bounded request with its own SQLite connection."""
        client = self._factory.create(self._snapshot, response_id=new_response_id())
        with contextlib.ExitStack() as stack:
            recorder = None
            if self._event_log_path is not None:
                conn = stack.enter_context(
                    contextlib.closing(open_runtime_event_log(self._event_log_path)),
                )
                recorder = CostRecorder(conn, pricing_table=self._pricing)
            return _LLMVisionClient(client, cost_recorder=recorder).describe_image(
                image_path, question=question,
            )


def _build_vision_client(
    config: Mapping[str, Any],
    preset_name: str,
    *,
    event_log_path: Path | None = None,
    pricing_path: Path | None = None,
    account_cost: bool = False,
) -> VisionClient | None:
    """Build the `screen_look` vision seam from `llm.presets.<preset_name>` (ADR-0011 D7).

    Returns `None` when the preset is absent/malformed — either
    STRUCTURALLY (missing `llm`/`presets` block, preset not a mapping)
    or by CONTENT (an unsupported `provider` string, a non-numeric
    `max_tokens` — SHOULD-FIX 3, ADR-0011 §12): `screen_look` still
    registers (the menu stays complete) but every call degrades to a
    `vision_unconfigured` error observation (ADR-0011 §5). A malformed
    preset is one optional tool misconfigured, not a trust source going
    missing (contrast `_load_file_targets_config`'s deliberate
    fail-boot posture, which protects a trust source the gate
    consults) — so it must not take the whole daemon down at boot.

    A DEDICATED `LLMClient` is built here rather than reusing the
    decision loop's own client (constructed at step 4, below):
    `LLMClient.switch_model`/`_apply_preset` mutates the client's
    active provider/model/base_url/key in place, so sharing one
    instance between the decision loop's `chat()` calls and
    `screen_look`'s vision calls would risk leaving the WRONG model
    active for the next turn. This reuses the SAME loader
    (`LLMClient.__init__` / `_apply_preset` reading a `presets` dict) —
    not a parallel YAML parser — just a second instance scoped to one
    preset, with an explicit bounded `timeout_s`/`max_retries`
    (MUST-FIX 2) the decision client does not get.
    """
    llm_block = config.get("llm")
    if not isinstance(llm_block, Mapping):
        return None
    presets = llm_block.get("presets")
    if not isinstance(presets, Mapping):
        return None
    preset = presets.get(preset_name)
    if not isinstance(preset, Mapping):
        return None
    vision_llm_config: dict[str, Any] = {
        "provider": "openai",
        "presets": {preset_name: dict(preset)},
        "default_preset": preset_name,
        "timeout_s": _VISION_CALL_TIMEOUT_S,
        "max_retries": _VISION_CALL_MAX_RETRIES,
    }
    try:
        LLMClient(vision_llm_config)
    except (ValueError, TypeError) as exc:
        LOGGER.warning(
            "screen_look: llm.presets.%s is malformed (%s: %s); screen_look will "
            "report vision_unconfigured at use time instead of the daemon failing to boot",
            preset_name,
            type(exc).__name__,
            exc,
        )
        return None
    if account_cost and event_log_path is None:
        message = "vision accounting requires an Event Log path"
        raise ValueError(message)
    return _RequestScopedVisionClient(
        vision_llm_config,
        event_log_path=event_log_path if account_cost else None,
        pricing_path=pricing_path,
    )


def _build_vision_cost_recorder(
    conn: sqlite3.Connection,
    wave1_features: Wave1FeatureFlags,
    *,
    pricing_path: Path,
) -> CostRecorder | None:
    """Map the rollout flag to the shared L3 guard at the composition root."""
    if not wave1_features.exactly_once_cost_accounting:
        return None
    return CostRecorder(
        conn,
        pricing_table=load_pricing_table(pricing_path),
    )


def _configure_realtime_trace_export(paths: RuntimePaths) -> None:
    """Enable the controlled JSONL live-burn seam without touching Event Log."""
    trace_jsonl_raw = os.environ.get("JARVIS_REALTIME_TRACE_JSONL")
    if not trace_jsonl_raw:
        configure_realtime_trace_jsonl(None)
        return
    trace_jsonl = Path(trace_jsonl_raw).expanduser()
    if not trace_jsonl.is_absolute():
        trace_jsonl = paths.root / trace_jsonl
    if trace_jsonl.resolve() == paths.event_log.resolve():
        msg = "JARVIS_REALTIME_TRACE_JSONL must not target the production Event Log"
        raise RuntimeBootstrapError(msg)
    try:
        configure_realtime_trace_jsonl(trace_jsonl)
    except OSError as exc:
        msg = f"runtime: cannot open realtime trace JSONL {trace_jsonl}: {exc}"
        raise RuntimeBootstrapError(msg) from exc


def _load_runtime_env_and_trace(paths: RuntimePaths) -> None:
    """Load fill-only runtime env, then apply its optional trace destination."""
    load_env_file(paths.root)
    _configure_realtime_trace_export(paths)


def bootstrap_runtime_app(  # noqa: PLR0915 - composition root wiring stays explicit
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
    3b. L3 Tier 0 whitelist — ``config/tier0_patterns.yaml`` parsed and
       cross-checked against the registry's ``regex_router`` surface.
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

    # 1b. ADR-0009 D1 — fill-only ``${runtime_root}/env`` loader, BEFORE
    #     any surface preflight reads the environment (launchd strips the
    #     shell env; secrets must not live in the 0644 plist).
    # Optional live-burn seam. This JSONL is diagnostic-only and must never be
    # the canonical SQLite Event Log. Relative paths stay inside runtime_root;
    # no path means the exporter is disabled (the production default).
    _load_runtime_env_and_trace(paths)

    # 2. L2 event log.
    conn = open_event_log(paths.event_log)

    # 3. L4 registry + lifecycle. Config is loaded here (ahead of step 4's
    #    LLM-config read) because `search_notes`/`web_search`/`web_fetch`
    #    need `tools.obsidian.vault_root` / `tools.web.*` threaded into
    #    the registry at build time (ADR-0011 D7) — L4 handlers do not
    #    load YAML themselves.
    full_config = _load_full_config(config_path)
    wave1_features = _wave1_feature_flags(full_config)
    (
        web_search_max_results,
        web_fetch_max_bytes,
        web_fetch_max_text_bytes,
        web_timeout_s,
    ) = _web_tools_config(full_config)
    web_search_provider, web_search_api_key = _web_search_provider_config(full_config)
    vision_preset_name, screen_max_width_px = _screen_tools_config(full_config)
    # 3a. ADR-0008 Step 3 (Wave 4B). The runner is built before the registry
    #     because the registry closes over it; with the switch off it stays
    #     None and `dispatch` keeps running handlers inline.
    action_flags = _wave4_action_flags(full_config)
    action_runner = (
        ActionRunner(
            event_log_path=paths.event_log,
            max_concurrent_runs=_positive_int(
                full_config,
                section="actions",
                key="max_concurrent_runs",
                fallback=1,
            ),
            lease_timeout_s=float(
                _positive_int(
                    full_config,
                    section="actions",
                    key="lease_timeout_s",
                    fallback=int(_FALLBACK_LEASE_TIMEOUT_S),
                ),
            ),
        )
        if action_flags.action_runner
        else None
    )
    registry = build_default_registry(
        action_runner=action_runner,
        confirmation_dispatch_outbox=wave1_features.confirmation_dispatch_outbox,
        # ADR-0008 Step 4: with this on, `dispatch` returns as soon as an
        # is_async ActionRun is accepted and the runner owns the rest.
        background_async=action_flags.true_async_workers,
        obsidian_vault_root=_obsidian_vault_root(full_config),
        web_search_max_results=web_search_max_results,
        web_search_provider=web_search_provider,
        web_search_api_key=web_search_api_key,
        web_fetch_max_bytes=web_fetch_max_bytes,
        web_fetch_max_text_bytes=web_fetch_max_text_bytes,
        web_timeout_s=web_timeout_s,
        vision_client=_build_vision_client(
            full_config,
            vision_preset_name,
            event_log_path=paths.event_log,
            pricing_path=repo_root / "data" / "pricing.json",
            account_cost=wave1_features.exactly_once_cost_accounting,
        ),
        screen_max_width_px=screen_max_width_px,
    )
    lifecycle = ActionLifecycle()

    # 3b. Spec §17 Tier 0 whitelist — sits next to jarvis.yaml so Allen
    #     edits one config directory. Invalid content fails the boot
    #     loudly (no silent pattern drops); missing file = Tier 0 off.
    tier0_path = config_path.parent / "tier0_patterns.yaml"
    try:
        tier0_table = load_tier0_table(tier0_path)
        regex_router_tools = registry.for_caller(CallerPrincipal.REGEX_ROUTER)
        validate_tier0_table(
            tier0_table,
            allowed_tool_names=frozenset(t.name for t in regex_router_tools),
            async_tool_names=frozenset(t.name for t in regex_router_tools if t.is_async),
            entity_required_tool_names=frozenset(
                t.name for t in regex_router_tools if t.requires_entity
            ),
            # ADR-0012 §3 D5 — a Tier 0 row must never target a
            # `requires_confirmation` tool (Tier 0 has no LLM to
            # receive Allen's confirmation answer).
            requires_confirmation_tool_names=frozenset(
                t.name for t in regex_router_tools if t.requires_confirmation
            ),
        )
    except Tier0ConfigError as exc:
        msg = f"runtime: {tier0_path} invalid: {exc}"
        raise RuntimeBootstrapError(msg) from exc

    # 3c. ADR-0011 D2 — boot invariant: every registered tool's
    #     `requires_confirmation` must equal `risk_rank(risk_level) >=
    #     risk_rank(confirmation_threshold)`, so the two fields can never
    #     drift (spec §14.2 / ADR-0011 V6). Loud failure, same shape as
    #     the Tier 0 block above.
    try:
        validate_requires_confirmation(
            registry.get_definitions(),
            effective_policy().confirmation_threshold,
        )
    except PolicyConsistencyError as exc:
        msg = f"runtime: tool registry requires_confirmation invariant violated: {exc}"
        raise RuntimeBootstrapError(msg) from exc

    # 3d. ADR-0011 D4 — EntityRegistry config seed. A missing/empty
    #     `file_targets.yaml` still degrades to no bookmarks (same
    #     posture as `open_path`'s own use of this config) — genuinely
    #     best-effort. A MALFORMED file is different: it would silently
    #     remove a trust source the Pre-action Gate consults (ADR-0011
    #     §12.2), so `load_file_targets_config` raising
    #     `FileTargetsConfigError` fails the boot loudly instead of
    #     degrading, matching steps 3b/3c above — a silent trust
    #     reduction is worse than a loud boot failure.
    try:
        entity_bookmarks = _entity_bookmarks()
    except FileTargetsConfigError as exc:
        msg = f"runtime: config/file_targets.yaml invalid: {exc}"
        raise RuntimeBootstrapError(msg) from exc

    # 3e. ADR-0012 §3 D6 — answer-path grammar table. Same posture as
    #     3b above: sits next to jarvis.yaml, missing file = the
    #     grammar hook disabled (inert, not broken), malformed content
    #     fails the boot loudly.
    confirm_grammar_path = config_path.parent / "confirm_grammar.yaml"
    try:
        confirm_grammar_table = load_confirm_grammar(confirm_grammar_path)
    except ConfirmGrammarConfigError as exc:
        msg = f"runtime: {confirm_grammar_path} invalid: {exc}"
        raise RuntimeBootstrapError(msg) from exc

    # 3d. ADR-0008 Step 8 tool cues — same posture as the grammar table.
    tool_cues_path = config_path.parent / "tool_cues.yaml"
    try:
        tool_cues = load_tool_cues(tool_cues_path)
    except ToolCueConfigError as exc:
        msg = f"runtime: {tool_cues_path} invalid: {exc}"
        raise RuntimeBootstrapError(msg) from exc

    # 4. L3 LLM client. `full_config` was already loaded at step 3 above.
    llm_config = load_llm_config(config_path)
    llm_client = LLMClient(llm_config)

    # 4b. ADR-0008 Step 2 (Wave 4A). The whole flag graph is resolved once,
    #     here; every downstream site reads the validated result. With the
    #     switches off nothing below is constructed: no session factory, no
    #     registry, no extra env read and no extra SDK transport.
    response_activation = _wave4_response_activation(full_config)
    response_flags = response_activation.flags
    realtime_raw = full_config.get("realtime")
    # The bus is gated on `realtime.enabled` rather than a Wave-4A switch so
    # Waves 4B/4C reuse this same line. It is inert while nobody subscribes.
    committed_event_bus = (
        CommittedEventBus()
        if isinstance(realtime_raw, Mapping) and realtime_raw.get("enabled") is True
        else None
    )
    llm_session_factory = (
        LLMSessionFactory(llm_config) if response_flags.response_run_lifecycle else None
    )
    response_runs = (
        ResponseRunRegistry() if response_flags.independent_response_cancel else None
    )

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
        tier0_table=tier0_table,
        entity_bookmarks=entity_bookmarks,
        confirm_grammar_table=confirm_grammar_table,
        wave1_features=wave1_features,
        response_flags=response_flags,
        action_flags=action_flags,
        action_runner=action_runner,
        llm_session_factory=llm_session_factory,
        response_runs=response_runs,
        committed_event_bus=committed_event_bus,
        input_flags=_wave5_input_flags(full_config),
        tool_cues=tool_cues,
    )


# --- ADR-0008 Wave 4A ResponseRun seams -------------------------------------


def _start_drive_turn_response(
    runtime: JarvisRuntime,
    *,
    user_intent_event: Event,
    turn_id: str,
    correction: StreamCorrection | None = None,
) -> tuple[ResponseRun, ResponseTerminalizer, RoutineStreamRoute | None] | None:
    """Open one durable ResponseRun for this turn, or ``None`` when flagged off.

    ``runtime`` here is ``drive_turn``'s own runtime — in the daemon that is
    the ``dataclasses.replace(conn=worker_conn)`` copy, so the run's
    ``BEGIN IMMEDIATE`` lands on the worker thread's own connection and never
    on the asyncio event loop's.

    ADR-0008 Step 8: with ``routine_streaming`` on, the route is decided here,
    before the run opens, and a ``casual_or_explanatory`` turn opens under
    ``routine_stream_policy`` with the streaming seam bound; every other turn
    (and every correction run) keeps ``legacy_full_text_policy``.
    """
    if not runtime.response_flags.response_run_lifecycle:
        return None
    if runtime.llm_session_factory is None:  # pragma: no cover - bootstrap pairs them
        return None

    response_id = new_response_id()
    snapshot = runtime.llm_session_factory.snapshot(None)
    request_client = runtime.llm_session_factory.create(snapshot, response_id=response_id)
    policy = legacy_full_text_policy(
        evidence_snapshot_hash=evidence_snapshot_hash(runtime.conn),
        preset_snapshot_hash=snapshot.snapshot_hash,
    )
    route: str | None = None
    context = None
    if runtime.response_flags.routine_streaming and correction is None:
        packet = assemble_packet(
            user_intent_event, runtime.conn, entity_bookmarks=runtime.entity_bookmarks,
        )
        route = pre_route(
            packet,
            tier0_table=runtime.tier0_table,
            tool_cues=runtime.tool_cues,
            now_ms=int(time.time() * 1000),
        )
        if route == "casual_or_explanatory":
            context = routine_risk_context(packet, response_id=response_id, turn_id=turn_id)
            routine = routine_stream_policy(context, preset_snapshot_hash=snapshot.snapshot_hash)
            if routine.emission_mode == "routine_stream":
                policy = routine
            else:
                # The risk floor of the request itself refused; D2 rule 1.
                route, context = "unknown", None
    run = start_response_run(
        runtime.conn,
        turn_id=turn_id,
        trigger_event_uid=user_intent_event.event_uid,
        request_client=request_client,
        policy=policy,
        response_id=response_id,
        committed_event_bus=runtime.committed_event_bus,
        corrects_response_id=correction.corrects_response_id if correction else None,
        route=route,
    )
    terminalizer = ResponseTerminalizer(
        lambda: runtime.conn,
        close_after=False,
        committed_event_bus=runtime.committed_event_bus,
    )
    if runtime.response_flags.independent_response_cancel and runtime.response_runs is not None:
        runtime.response_runs.register(run)
    if context is None:
        return run, terminalizer, None
    transcript_raw = user_intent_event.payload.get("transcript", "")
    query = transcript_raw if isinstance(transcript_raw, str) else ""
    seam = RoutineStreamRoute(
        policy=policy,
        context=context,
        open_stream=lambda handle: LoopBoundTokenStream(
            handle, cancellation_token=run.cancellation_token,
        ),
        emit_segment=lambda permit, text: emit_permitted_segment(
            runtime.conn,
            permit,
            text,
            query=query,
            attention_channel=ROUTINE_ATTENTION_CHANNEL,
            committed_event_bus=runtime.committed_event_bus,
        ).event,
        segment_guard=run.admission_guard,
        committed_event_bus=runtime.committed_event_bus,
    )
    record_realtime_trace(
        "routine_stream_route_opened", turn_id=turn_id, response_id=response_id,
    )
    return run, terminalizer, seam


def make_response_cancel_callable(
    runtime: JarvisRuntime,
) -> Callable[[str, str, str], str]:
    """Build the injectable ``(response_id, scope, reason) -> outcome`` seam.

    Returned strings: ``"cancelled"``, ``"already_terminal"``,
    ``"unknown_response"``, ``"unsupported_scope"``, ``"timeout"``.

    The callable opens its OWN connection per call. That is mandatory, not
    stylistic: ``open_event_log`` uses ``check_same_thread=True`` and the
    surface offloads this onto an ``asyncio.to_thread`` worker. The
    connection also carries ``PRAGMA busy_timeout = cancel_timeout_ms``,
    which is what gives ``realtime.response.cancel_timeout_ms`` a concrete
    meaning: a contended ``BEGIN IMMEDIATE`` gives up after that many
    milliseconds, no terminal is written, and the caller may retry.
    """
    cancel_timeout_ms = _cancel_timeout_ms(runtime.config)
    event_log_path = runtime.runtime_paths.event_log
    committed_event_bus = runtime.committed_event_bus
    registry = runtime.response_runs

    def _cancel(response_id: str, scope: str, reason: str) -> str:
        deadline = time.monotonic() + cancel_timeout_ms / 1000

        def _connect() -> sqlite3.Connection:
            return open_runtime_event_log(event_log_path, deadline=deadline)

        if registry is None:  # pragma: no cover - wiring pairs the two flags
            return "unknown_response"
        if scope not in ("generation", "foreground_output"):
            return "unsupported_scope"
        normalized_reason = reason
        if normalized_reason not in RESPONSE_CANCEL_REASONS:
            LOGGER.warning(
                "cancel-response: reason %r is outside the closed vocabulary; "
                "recording it as 'operator_request'",
                reason,
            )
            normalized_reason = "operator_request"
        outcome = request_response_cancel(
            registry,
            ResponseTerminalizer(
                _connect,
                close_after=True,
                committed_event_bus=committed_event_bus,
            ),
            ResponseCancelRequest(
                request_id="CREQ" + uuid.uuid4().hex,
                response_id=response_id,
                scope="generation" if scope == "generation" else "foreground_output",
                reason=normalized_reason,
            ),
            deadline=deadline,
        )
        if isinstance(outcome, CancelAccepted):
            return "cancelled"
        if isinstance(outcome, CancelAlreadyTerminal):
            return "already_terminal"
        if isinstance(outcome, CancelTimedOut):
            return "timeout"
        return outcome.reason

    return _cancel


def make_foreground_decision_callable() -> Callable[[str, int, str, int], str]:
    """Build the injectable foreground-lane arbitration seam.

    ``(incumbent_group, incumbent_row_id, candidate_group, candidate_row_id)
    -> outcome``, where the outcome is ``"enqueue_after_drain"``,
    ``"supersede"`` or ``"decline"``.

    The policy is a pure L3 function with no clock and no IO, so there is
    nothing to bind; the builder exists only because ``jarvis.decision`` and
    ``jarvis.surface`` are siblings (``.importlinter``) and the runtime is the
    only layer allowed to wire them together.
    """
    return decide_foreground


def make_barge_in_interrupt_callable(
    runtime: JarvisRuntime,
) -> Callable[[str], str]:
    """Build the injectable ``(confirm_source) -> outcome`` barge-in seam.

    ADR-0006 D8: the runtime is a mechanical applier of the interrupt policy
    the run was started with, never its author.  The target is resolved from
    the live-run index rather than supplied by L5 — the voice session never
    learns a response id — and a confirmed barge-in cancels only when that
    run's own ``ResponseInterruptPolicy`` permits it.

    Returned strings beyond ``make_response_cancel_callable``'s own outcomes:
    ``"no_open_run"``, ``"ambiguous_open_runs"``, ``"policy_ignore"``,
    ``"policy_generation_continue"``.
    """
    cancel = make_response_cancel_callable(runtime)
    registry = runtime.response_runs

    def _interrupt(confirm_source: str) -> str:
        if registry is None:  # pragma: no cover - wiring pairs the two flags
            return "no_open_run"
        # A commentary run for the same turn is legitimately open while the
        # final run waits on its action (ADR-0006 D8): counting it would make
        # every action-dispatching turn read as `ambiguous_open_runs`.
        open_runs = tuple(run for run in registry.open_runs() if run.phase == "final")
        if not open_runs:
            return "no_open_run"
        if len(open_runs) > 1:
            return "ambiguous_open_runs"
        run = open_runs[0]
        policy = run.interrupt_policy
        if policy.confirmed_playback != "interrupt_expected_playback_generation":
            return "policy_ignore"
        if policy.generation_action != "cancel":
            return "policy_generation_continue"
        LOGGER.info(
            "barge-in confirmed via %s; cancelling response %s generation",
            confirm_source,
            run.response_id,
        )
        return cancel(run.response_id, "generation", "barge_in")

    return _interrupt


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


# Candidate rows for the in-turn waiter. NOT ``LIMIT 1``: since
# ADR-0009 D4 the waiter also filters by action_id (see
# :func:`_wait_for_next_trigger`), so a foreign orphan's terminal row
# sitting at the head of the cursor must be skipped over rather than
# blocking every later row this turn actually owns.
_SELECT_NEXT_TRIGGER_SQL = (  # noqa: S608 — placeholders interpolation is over a hard-coded type tuple, not user input.
    "SELECT id, event_uid, type, schema_version, ts_epoch_ms, "
    "payload_json, source_event_id, correlation_json "
    "FROM events WHERE id > ? AND type IN ({placeholders}) "
    "ORDER BY id ASC"
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


def _absorbed_inline(lifecycle: ActionLifecycle, action_id: str | None) -> bool:
    """True when this process already moved ``action_id`` past ``running``.

    Sync tool handlers transition the FSM inline before ``dispatch`` returns,
    so their ``action.result_observed`` row is history to the waiter, not a
    trigger. ``None`` (unknown here — another process wrote it) is not
    treated as absorbed.
    """
    if action_id is None:
        return False
    state = lifecycle.state_of(action_id)
    return state is not None and state != "running"


def _event_action_id(event: Event) -> str | None:
    """Return the action_id this event belongs to, or ``None``.

    Correlation first (the canonical slot every L4 emitter fills via
    ``_action_correlation``), payload second (all four trigger types
    mirror the id there, and the supervisor sweep's
    ``action.timeout_assumed`` fills both).

    ADR-0009 D4 partitions terminal events between two consumers — the
    in-turn waiter (``action_id`` IS one of the turn's own) and
    ``_system_trigger_watcher`` (``action_id`` is in NO live turn's set).
    Both predicates read the key through THIS function, so the two
    filters are complements of one another by construction and no event
    can be claimed twice or dropped by both.
    """
    for source in (event.correlation, event.payload):
        if source is None:
            continue
        value = source.get("action_id")
        if isinstance(value, str):
            return value
    return None


def _wait_for_next_trigger(  # noqa: PLR0913 — one defaulted cancel predicate on top of the existing waiter contract.
    conn: sqlite3.Connection,
    *,
    after_id: int,
    action_ids: frozenset[str],
    lifecycle: ActionLifecycle,
    timeout: float,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[Event, int]:
    """Poll the event log for the next L3 trigger event after ``after_id``.

    Trigger types: ``worker.reported`` (spawn_worker happy-path
    Timer thread), ``action.result_observed`` (defensive — sync
    tools emit this inline so decide() already absorbed it, but a
    late re-entry from a stale Timer is accepted to keep the poll
    loop drainable), and ``action.timeout_assumed`` /
    ``action.failed`` (B-0003b — spawn_worker terminal failures
    emitted by the L4 handler when the Codex turn times out or the
    subprocess crashes; the runtime waiter must wake decide() so L3
    can fold a Limitation Claim).

    Scoping (ADR-0009 D4 / F9). Type + ``after_id`` alone is NOT a
    correlation: a supervisor sweep or wake reconciliation closing some
    *foreign* orphan writes an ``action.timeout_assumed`` into the same
    log, and the pre-D4 waiter would hand it to whichever turn happened
    to be in flight — which then adopts the orphan's correlation and
    ends the wrong turn with the wrong limitation. Every candidate row
    is therefore matched against ``action_ids``, the set of actions THIS
    turn dispatched; foreign rows are skipped over (the local cursor
    still advances past them, so they are examined once, not once per
    poll). This is a filter, not a new trigger type — the
    ``_RUNTIME_TRIGGER_TYPES`` tuple is untouched.

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
        action_ids: The action_ids this turn owns — read fresh per call
            from :func:`jarvis.execution.tools.turn_action_ids` so an
            action dispatched during the previous decide() iteration is
            already in scope. A row whose ``action_id`` is outside this
            set belongs to somebody else and is never returned.
        lifecycle: The process-local action FSM. An
            ``action.result_observed`` row is only a wake-up for an action
            this process still holds at ``running``; a sync tool writes
            the same row inline and moves its action past ``running``
            before ``dispatch`` returns, so decide() has already absorbed
            it. Without this check a turn that ran a sync tool and then
            paused on ``spawn_worker`` in the same decide() iteration wakes
            on the sync tool's row instead of ``worker.reported`` (found
            live 2026-09-04: the worker never got its ``action.result_observed``
            and the turn ended with ``verification_skipped``). An action
            unknown to this process (crash recovery) is not filtered.
        timeout: Hard wall-clock cap in seconds; raise
            :class:`TriggerWaitTimeout` if exceeded.
        poll_interval_s: Sleep between polls (default 10 ms).
        cancelled: ADR-0008 Wave 4A — optional predicate checked once
            per poll tick. When it returns True the wait aborts with
            :class:`~jarvis.decision.response_run.ResponseCancelledError`.
            This is the single line that makes a response cancel land
            within one poll interval during a 600 s action wait instead
            of after it. ``None`` (the default, and what every caller
            passes with the flag off) restores the exact legacy loop.

    Returns:
        Tuple of ``(event, new_after_id)`` — the freshly-folded
        :class:`Event` plus the SQLite row id to use for the next
        wait call.

    Raises:
        TriggerWaitTimeout: No matching trigger arrived within
            ``timeout`` seconds.
        ResponseCancelledError: ``cancelled`` reported that this turn's
            ResponseRun already reached a cancelled terminal.
    """
    deadline = time.monotonic() + timeout
    cursor_id = after_id
    while True:
        rows = conn.execute(
            _SELECT_NEXT_TRIGGER_SQL,
            (cursor_id, *_RUNTIME_TRIGGER_TYPES),
        ).fetchall()
        for row in rows:
            cursor_id = int(row[0])
            event = _hydrate_event_row(row)
            if _event_action_id(event) in action_ids:
                if event.type == "action.result_observed" and _absorbed_inline(
                    lifecycle, _event_action_id(event),
                ):
                    continue
                return event, cursor_id
        if cancelled is not None and cancelled():
            msg = (
                "runtime: response run cancelled while waiting for a trigger of "
                f"types {_RUNTIME_TRIGGER_TYPES!r} (after_id={after_id})."
            )
            raise ResponseCancelledError(msg)
        if time.monotonic() >= deadline:
            msg = (
                f"runtime: no trigger event of types {_RUNTIME_TRIGGER_TYPES!r} "
                f"for action_ids={sorted(action_ids)!r} arrived within "
                f"{timeout!r}s (after_id={after_id})."
            )
            raise TriggerWaitTimeout(msg)
        time.sleep(poll_interval_s)


def _trigger_wait_budget(
    runtime: JarvisRuntime,
    *,
    turn_id: str,
    override: float | None,
) -> float:
    """Return this iteration's per-trigger wait budget, in seconds.

    The single timeout authority for an in-turn action wait:

    * An explicit caller value always wins. Scenarios and any surface that
      wants its own budget keep it.
    * Otherwise, while the ActionRunner still owns one of this turn's
      actions, the budget is that runner's lease timeout
      (``realtime.actions.lease_timeout_s``, ADR-0008 §6). That is exactly
      the ADR-0008 Step 4 ``true_async_workers`` case: a foreground
      dispatch has already been awaited by the time control reaches this
      wait, so a job still in flight here is a background worker whose
      ``worker.reported`` is minutes away. Reusing the lease clock keeps
      one authority — a turn's wait can then neither expire before the
      action it waits on nor outlive it.
    * Otherwise the conversational default, so an ordinary turn still
      cannot pin one of ``max_concurrent_turns`` for a quarter of an hour.

    Read fresh on every iteration, like ``turn_action_ids`` beside it: the
    action that paused ``decide()`` was registered inside the call above.
    """
    if override is not None:
        return override
    runner = runtime.action_runner
    if runner is not None and runner.turn_has_inflight(turn_id):
        return runner.lease_timeout_s
    return _DEFAULT_TRIGGER_TIMEOUT_S


def run_turn(
    runtime: JarvisRuntime,
    *,
    utterance: str,
    turn_id: str | None = None,
    max_iterations: int = _DEFAULT_MAX_ITERATIONS,
    trigger_timeout_s: float | None = None,
) -> RunTurnResult:
    """Drive one conversation turn end-to-end (CLI / scenario entrypoint).

    Steps (spec §3.4.1 multi-trigger loop):

    1. Mint or accept a ``turn_id`` (composition root owns this; L3
       sees the same id on the trace correlation).
    2. Emit ``surface.user_intent`` via L5 surface adapter (spec
       §3.6.1) — this is the first trigger event.
    3. Delegate the post-emit body to :func:`drive_turn`, which drives
       the L3 decide loop, runs the stash-pop finalizer, and renders
       the final :class:`ResponsePlan` across the surfaces dictated by
       the L3 Attention Policy.

    The split into :func:`run_turn` (emit + delegate) and
    :func:`drive_turn` (post-emit body) exists so non-CLI surfaces can
    drive a turn without re-emitting ``surface.user_intent``. The
    daemon HTTP handler (ADR-0003 Step 7) writes the intent event from
    the request body, and the daemon watcher (Step 8) picks it up and
    calls :func:`drive_turn` directly — both bypass :func:`run_turn`.

    Args:
        runtime: Assembled :class:`JarvisRuntime`.
        utterance: User text to drive the turn.
        turn_id: Optional explicit turn id (tests). Default: minted.
        max_iterations: Hard ceiling on decide() invocations.
        trigger_timeout_s: Explicit per-trigger wait timeout. ``None``
            (production) resolves per iteration via
            :func:`_trigger_wait_budget`.

    Returns:
        Frozen :class:`RunTurnResult` describing what was written and
        the events emitted.
    """
    effective_turn_id = turn_id if turn_id is not None else _new_turn_id()

    utterance_event = emit_surface_user_intent(
        runtime.conn,
        transcript=utterance,
        turn_id=effective_turn_id,
    )

    return drive_turn(
        runtime,
        user_intent_event=utterance_event,
        max_iterations=max_iterations,
        trigger_timeout_s=trigger_timeout_s,
    )


def drive_turn(  # noqa: C901, PLR0912, PLR0913, PLR0915 — composition-root entrypoint; argument set + traced multi-trigger loop are the cross-surface contract, and every ResponseRun branch is flag-guarded.
    runtime: JarvisRuntime,
    *,
    user_intent_event: Event,
    available_surfaces: frozenset[str] | None = None,
    max_iterations: int = _DEFAULT_MAX_ITERATIONS,
    trigger_timeout_s: float | None = None,
    streaming_enabled: bool = False,
    suspend_when_waiting: bool = False,
    continuation: WaitingTurn | None = None,
) -> RunTurnResult:
    """Drive the post-emit body of one turn from an already-emitted intent event.

    Same body as the pre-extract ``run_turn`` (decide loop + finalize +
    render). The ``surface.user_intent`` event is supplied externally;
    :func:`drive_turn` does NOT re-emit it. The effective ``turn_id``
    is read from ``user_intent_event.payload["turn_id"]``.

    The caller is responsible for emitting ``surface.user_intent``
    first. This function exists so non-CLI surfaces — specifically the
    ADR-0003 daemon HTTP handler (writes the intent) and the daemon
    watcher (picks it up and drives) — can drive a turn from a
    pre-emitted event without double-emitting and looping the watcher.

    Steps (spec §3.4.1 multi-trigger loop):

    1. Call :func:`jarvis.decision.decide` with ``user_intent_event``
       as the first trigger. If it returns a :class:`ResponsePlan`,
       finalize immediately.
    2. Otherwise the decide() call paused on an async tool
       (spawn_worker); poll the event log for the next
       ``worker.reported`` / ``action.result_observed`` /
       ``action.timeout_assumed`` / ``action.failed`` row, re-enter
       decide() with that trigger, repeat.
    3. Run the stash-pop finalizer (ADR-0002 § Dirty-tree policy,
       amended 2026-08-25) from the ``finally`` — lexically after the
       last ``decide(...)`` call so the ``verify_command`` subprocess
       saw exactly Codex's tree, and unconditionally so an exception
       between decide() and finalization cannot orphan the pre-task
       stash. Canary ``test_canary_stash_pop_after_verify`` enforces
       the ordering inside :func:`drive_turn`.
    4. Record the Pre-emit token on a fresh :class:`SurfaceState`, then
       call :func:`jarvis.surface.cli_render.render_response` which
       routes the channel-split text across the L3 attention channel's
       physical surfaces (say / banner / stdout when attached) AND
       enforces the Pre-emit token check (canary H3) AND emits the
       audit ``surface.response_emitted`` event. When
       ``streaming_enabled=True`` (daemon path, ADR-0003 Step 2),
       render_response ALSO emits ``surface.response_open`` plus N
       ``surface.response_chunk`` rows BEFORE the audit
       ``surface.response_emitted``; the user transcript (lifted from
       ``user_intent_event.payload["transcript"]``) lands on the open
       payload as ``query``.

    The Pre-emit token check protects against a runtime that
    accidentally re-uses an old plan or fails to refresh the token —
    :class:`PreEmitTokenError` propagates out. It — and every other
    exception — still passes through the ``finally`` that releases this
    turn's live action_ids (ADR-0009 D4).

    Args:
        runtime: Assembled :class:`JarvisRuntime`.
        user_intent_event: The already-emitted ``surface.user_intent``
            :class:`Event`. Its ``payload["turn_id"]`` is the
            canonical turn id used through this turn.
        available_surfaces: Passed through to
            :func:`jarvis.surface.cli_render.render_response`; daemon
            callers use ``frozenset()`` to suppress physical surfaces.
        max_iterations: Hard ceiling on decide() invocations.
        trigger_timeout_s: Explicit per-trigger wait timeout. ``None``
            (production) resolves per iteration via
            :func:`_trigger_wait_budget`.
        streaming_enabled: Forwarded to
            :func:`jarvis.surface.cli_render.render_response`; the
            daemon watcher (ADR-0003 Step 2 Build 5) passes ``True``
            so the renderer emits the 3-event Inherent taxonomy.
            Default ``False`` preserves CLI single-emit semantics.
        suspend_when_waiting: Yield a connection-free checkpoint instead of
            occupying an input worker while L4 runs.
        continuation: Resume a previously suspended turn with a ready trigger
            or failure supplied by the runtime scheduler.

    Returns:
        Frozen :class:`RunTurnResult` describing what was written and
        the events emitted.
    """
    effective_turn_id = str(user_intent_event.payload["turn_id"])
    record_realtime_trace(
        "intent_queue_accepted" if continuation is None else "turn_continuation_resumed",
        turn_id=effective_turn_id,
        source="runtime_drive_turn",
    )
    # ADR-0008 Step 2 (Wave 4A) — open the durable ResponseRun before the
    # decide loop so the per-run request client, the terminal owner and the
    # L5 ids all name the same response. Returns None with the flag off, and
    # every use below is guarded on that None.
    run_pair = None if continuation is not None else _start_drive_turn_response(
        runtime,
        user_intent_event=user_intent_event,
        turn_id=effective_turn_id,
    )
    run: ResponseRun | None = continuation.run if continuation is not None else None
    terminalizer: ResponseTerminalizer | None = None
    stream_route: RoutineStreamRoute | None = None
    if run_pair is not None:
        run, terminalizer, stream_route = run_pair
    elif run is not None:
        terminalizer = ResponseTerminalizer(
            lambda: runtime.conn, close_after=False,
            committed_event_bus=runtime.committed_event_bus,
        )
    suspended = False

    def _run_cancelled() -> bool:
        """Return whether this turn's ResponseRun has been cancelled."""
        return run is not None and run.cancellation_token.is_cancelled

    def _raise_if_cancelled(where: str) -> None:
        """Abort the turn when this run's cancel already won its terminal.

        Called at every point where the run's FSM would otherwise advance.
        The cancel path commits ``response.cancelled`` before it sets the
        token, so observing the token means the terminal is already durable
        and there is nothing left for this turn to write.
        """
        if run is not None and run.cancellation_token.is_cancelled:
            msg = (
                f"drive_turn: response run cancelled {where} "
                f"(turn_id={effective_turn_id!r})."
            )
            raise ResponseCancelledError(msg)

    response_trace_ids: dict[str, TraceValue] = (
        {}
        if run is None
        else {"response_id": run.response_id, "response_group_id": run.response_group_id}
    )
    record_realtime_trace(
        "response_started" if continuation is None else "response_resumed",
        turn_id=effective_turn_id,
        trigger_type=user_intent_event.type,
        **response_trace_ids,
    )
    # ADR-0009 D4 — every action this turn dispatches registers itself
    # in the L4 live set (`ToolRegistry.dispatch`). The release MUST run
    # even when the turn raises: a leaked action_id is one the supervisor
    # sweep will refuse to close for the life of the process, so a crashed
    # turn would permanently protect the very orphan it created.
    try:
        if continuation is not None:
            _raise_if_cancelled("before continuation")
            if continuation.failure is not None:
                raise continuation.failure  # noqa: TRY301 — resume through the same failure owner
            if run is not None:
                run.mark("generating")
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
            # ADR-0008 D4 — with the Wave-4A flag on, this turn's provider
            # identity is the run's own immutable client, so two overlapping
            # runs can never read each other's last-call metadata. With the
            # flag off this is the same shared client object as before.
            llm_client=run.request_client if run is not None else runtime.llm_client,
            system_prompt=runtime.system_prompt,
            tier0_table=runtime.tier0_table,
            # ADR-0009 D5/D6 — the Status Board note calls an observation
            # "stale" at 3x the observer's poll interval. Read from the
            # same config key the observer task polls on, so the two can
            # never disagree about what "stale" means.
            observer_poll_interval_s=int(_observer_poll_interval_s(runtime.config)),
            # ADR-0011 D4 — EntityRegistry config seed, threaded the same
            # way tier0_table is threaded.
            entity_bookmarks=runtime.entity_bookmarks,
            # ADR-0011 D4 — resolve-on-propose. Built fresh per turn (a
            # trivial closure) rather than stored on JarvisRuntime: it
            # closes over `runtime.conn`, which the JarvisRuntime fields
            # above already carry, so there is nothing to cache.
            entity_resolver=_make_entity_resolver(runtime.conn),
            # ADR-0012 D1 — write-target resolve-on-propose. Same
            # per-turn-closure rationale as `entity_resolver` above.
            write_entity_resolver=_make_write_entity_resolver(runtime.conn),
            # ADR-0012 §3 D4/V2 — confirmation TTL, config-overridable
            # via `confirmation.ttl_ms` so the live burn can shorten it.
            confirmation_ttl_ms=_confirmation_ttl_ms(runtime.config),
            # ADR-0012 §3 D6 — answer-path grammar, threaded the same
            # way tier0_table is threaded.
            confirm_grammar_table=runtime.confirm_grammar_table,
            wave1_features=runtime.wave1_features,
            typed_conversation_history=runtime.response_flags.typed_conversation_history,
            cancellation_checkpoint=run.check_cancelled if run is not None else None,
            request_admission=(
                partial(run.admit_request, runtime.conn) if run is not None else None
            ),
            routine_stream=stream_route,
        )

        # SQLite row id of the surface.user_intent event — used as the
        # "after_id" anchor for the trigger poll loop.
        last_seen_id = (
            continuation.after_id if continuation is not None else _latest_row_id(runtime.conn)
        )

        collected_events: list[Event] = list(continuation.events) if continuation else []
        trigger_event: Event = (
            continuation.trigger
            if continuation is not None and continuation.trigger is not None
            else user_intent_event
        )
        response_plan: ResponsePlan | None = None
        # Track the attention_channel from the final decide() iteration so
        # we can route the response to the right surfaces in Step 18.
        # Default ``"voice_notify"`` covers the flagship ADR-0002 scenario
        # when an L3 branch returns a plan without explicitly setting the
        # field; the L3 Attention Policy emits one of the 9 channels for
        # canonical branches today.
        final_attention_channel: str = "voice_notify"
        iterations = continuation.iterations if continuation is not None else 0
        streamed = False
        last_gate_event_uid: str | None = None

        while response_plan is None and iterations < max_iterations:
            _raise_if_cancelled("before a decide iteration")
            iterations += 1
            with bind_action_admission(
                run.admission_guard if run is not None else contextlib.nullcontext,
            ):
                result = decide(trigger_event, decide_ctx)
            if trigger_event.type in {
                "worker.reported", "action.failed", "action.timeout_assumed", "action.cancelled",
            }:
                mark_trigger_consumed(runtime.conn, trigger_event.event_uid, effective_turn_id)
            collected_events.extend(result.events_emitted)
            if result.stream_failure is not None and run is not None and terminalizer is not None:
                # ADR-0008 D3: the stream could not be finalized. The run
                # fails naming what it exposed, and a full-text correction
                # run continues that prefix for the same turn.
                failure = result.stream_failure
                terminalizer.fail(
                    run.facts,
                    reason=failure.reason,
                    retryable=False,
                    committed_prefix_hash=failure.committed_prefix_hash,
                )
                run.mark("failed")
                if runtime.response_runs is not None:
                    runtime.response_runs.unregister(run.response_id)
                correction = StreamCorrection(
                    corrects_response_id=run.response_id,
                    committed_prefix=committed_text_prefix(runtime.conn, run.response_id).text,
                )
                record_realtime_trace(
                    "routine_stream_correction_opened",
                    turn_id=effective_turn_id,
                    failed_response_id=run.response_id,
                    reason=failure.reason,
                )
                replacement = _start_drive_turn_response(
                    runtime,
                    user_intent_event=user_intent_event,
                    turn_id=effective_turn_id,
                    correction=correction,
                )
                if replacement is None:  # pragma: no cover - the flag graph pins lifecycle on
                    msg = "drive_turn: correction run requires response_run_lifecycle"
                    raise RuntimeBootstrapError(msg)  # noqa: TRY301
                run, terminalizer, _ = replacement
                decide_ctx = replace(
                    decide_ctx,
                    llm_client=run.request_client,
                    routine_stream=None,
                    stream_correction=correction,
                    cancellation_checkpoint=run.check_cancelled,
                    request_admission=partial(run.admit_request, runtime.conn),
                )
                continue
            response_plan = result.response_plan
            if response_plan is not None:
                final_attention_channel = result.attention_channel
                streamed = result.route == "casual_or_explanatory"
                last_gate_event_uid = result.last_gate_event_uid
                break

            # No final plan -> decide() paused on an async tool. Wait for the
            # next L4-side trigger event.
            # ADR-0009 D4 / F9 — the waiter is scoped to the actions THIS
            # turn dispatched. Read fresh each iteration: the action
            # that paused decide() was registered inside the call above.
            owned_action_ids = turn_action_ids(effective_turn_id)
            wait_timeout_s = _trigger_wait_budget(
                runtime,
                turn_id=effective_turn_id,
                override=trigger_timeout_s,
            )
            record_realtime_trace(
                "action_wait_started",
                turn_id=effective_turn_id,
                action_count=len(owned_action_ids),
                timeout_s=wait_timeout_s,
            )
            if run is not None:
                _raise_if_cancelled("before the action wait")
                run.mark("waiting_action")
            if suspend_when_waiting:
                suspended = True
                raise TurnSuspended(WaitingTurn(  # noqa: TRY301 — scheduler handoff preserves finally ownership
                    intent=user_intent_event, run=run, after_id=last_seen_id,
                    action_ids=owned_action_ids, deadline=time.monotonic() + wait_timeout_s,
                    iterations=iterations, events=tuple(collected_events),
                ))
            try:
                next_event, last_seen_id = _wait_for_next_trigger(
                    runtime.conn,
                    after_id=last_seen_id,
                    action_ids=owned_action_ids,
                    lifecycle=runtime.lifecycle,
                    timeout=wait_timeout_s,
                    cancelled=_run_cancelled if run is not None else None,
                )
            except TriggerWaitTimeout:
                record_realtime_trace(
                    "action_wait_completed",
                    turn_id=effective_turn_id,
                    outcome="bounded_timeout",
                    timeout_s=wait_timeout_s,
                )
                raise
            if run is not None:
                _raise_if_cancelled("while waiting on an action")
                run.mark("generating")
            action_id = _event_action_id(next_event)
            record_realtime_trace(
                "action_wait_completed",
                turn_id=effective_turn_id,
                outcome="trigger_observed",
                trigger_type=next_event.type,
                action_id=action_id,
            )
            record_realtime_trace(
                "action_result_available",
                turn_id=effective_turn_id,
                action_id=action_id,
                result_source=next_event.type,
            )
            trigger_event = next_event

        if response_plan is None:
            if run is not None and terminalizer is not None:
                terminalizer.fail(
                    run.facts,
                    reason="turn_exhausted_iterations",
                    retryable=False,
                )
            msg = (
                f"drive_turn: exhausted max_iterations={max_iterations} without a final "
                f"ResponsePlan (turn_id={effective_turn_id!r})."
            )
            raise RuntimeBootstrapError(msg)  # noqa: TRY301 — the turn's own failure, not a helper's.

        # L5 emission (Step 18 — channel-split + multi-surface dispatch).
        # The Pre-emit token guard inside render_response() preserves the
        # canary H3 runtime check — calling record_pre_emit_token() then
        # render_response() in this order is the only legal path.
        # SurfaceState is allocated fresh per turn (spec §3.6.7 — local
        # surface state owns no truth and doesn't survive across turns)
        # and discarded once render_response returns the cleared state.
        surface_state = SurfaceState(last_gate_response_hash=None)
        primed_state = record_pre_emit_token(surface_state, response_plan.response_hash)

        # ADR-0008 D1 — generation completion, not physical delivery, is the
        # ResponseRun's terminal condition, so the terminal is written BEFORE
        # render. The accepted consequence is pinned by
        # `test_completed_without_delivery_when_render_raises`: if render
        # then raises, the log holds response.completed with no
        # surface.response_emitted. The opposite ordering is unrecoverable —
        # it would let a cancel land after the words were already spoken.
        if run is not None and terminalizer is not None:
            _raise_if_cancelled("before finalizing")
            run.mark("finalizing")
            completion = terminalizer.complete(
                run.facts,
                response_hash=response_plan.response_hash,
            )
            if isinstance(completion, AlreadyTerminal):
                # A cancel won the CAS between the check above and this
                # append. Nothing is rendered — the words must not be spoken
                # for a response the operator already stopped — and the turn
                # ends the same way every other cancelled turn does, so the
                # daemon watcher does not record it as a completed answer.
                msg = (
                    "drive_turn: response run reached "
                    f"{completion.event.type} before its completion CAS "
                    f"(turn_id={effective_turn_id!r})."
                )
                raise ResponseCancelledError(msg)  # noqa: TRY301 — the turn's own outcome, not a helper's.
            run.mark("completed")
            if streamed:
                # The finalizer wrote nothing; the turn closes after the
                # terminal, sourced on the last stream gate verdict.
                collected_events.append(
                    emit_turn_ended(
                        runtime.conn,
                        turn_id=effective_turn_id,
                        final_response_hash=response_plan.response_hash,
                        consumed_trigger_event_uid=user_intent_event.event_uid,
                        source_event_id=last_gate_event_uid or run.facts.started_event_uid,
                    ),
                )

        # The in-memory capture stream serves two ends at once. First the
        # operator console: render_response() writes the cli_stdout slice
        # into it so the runtime can re-emit the same bytes to the real
        # sys.stdout. Second the test / programmatic caller: the returned
        # RunTurnResult.response_text is the captured string. Beyond
        # stdout, render_response also routes voice text to say and
        # document text to the notify banner per the channel mapping, and
        # emits the audit surface.response_emitted event itself.
        capture: io.StringIO = io.StringIO()
        transcript_raw = user_intent_event.payload.get("transcript", "")
        query = transcript_raw if isinstance(transcript_raw, str) else ""
        _, render_event = render_response(
            primed_state,
            response_plan,
            conn=runtime.conn,
            turn_id=effective_turn_id,
            attention_channel=final_attention_channel,
            stream=capture,
            available_surfaces=available_surfaces,
            streaming_enabled=streaming_enabled,
            query=query,
            response_id=run.response_id if run is not None else None,
            response_group_id=run.response_group_id if run is not None else None,
            delivery_terminal_only=streamed,
        )
        rendered = capture.getvalue()
        sys.stdout.write(rendered)
        sys.stdout.flush()
        collected_events.append(render_event)
        record_realtime_trace(
            "response_completed",
            turn_id=effective_turn_id,
            iterations=iterations,
            gate_mode=response_plan.required_gate_mode,
        )

        return RunTurnResult(
            response_text=rendered,
            response_plan=response_plan,
            turn_id=effective_turn_id,
            iterations=iterations,
            events_emitted=tuple(collected_events),
            attention_channel=final_attention_channel,
        )
    except TurnSuspended:
        raise
    except ResponseCancelledError:
        # The cancel caller already committed response.cancelled. Re-raise so
        # the daemon watcher can tell a cancelled turn from a failed one.
        raise
    except Exception:
        if run is not None and terminalizer is not None:
            # The turn's own exception is the one the caller must see. A CAS
            # that cannot reach the log here leaves the run open, and the boot
            # reconciler closes it as daemon_restart — losing the terminal is
            # recoverable, masking the real failure is not.
            try:
                terminalizer.fail(
                    run.facts,
                    reason="turn_raised",
                    retryable=False,
                    committed_prefix_hash=terminalizer.committed_prefix_hash(run),
                )
            except Exception:
                LOGGER.exception(
                    "drive_turn: could not write response.failed (response_id=%r)",
                    run.response_id,
                )
        raise
    finally:
        if not suspended:
            # --- Stash-pop finalizer (ADR-0002 § Dirty-tree policy, amended
            # 2026-08-25). Lives in the finally: lexically after the last
            # decide() call — canary ``test_canary_stash_pop_after_verify``
            # enforces the ordering, so verify_diff read exactly Codex's
            # tree — and unconditionally, so an exception between decide()
            # and finalization cannot orphan Allen's pre-task stash. The
            # guard keeps the finally from masking the original exception.
            if run is not None and runtime.response_runs is not None:
                runtime.response_runs.unregister(run.response_id)

            def _finalize_turn_resources() -> VerificationOutcome:
                """Restore this turn's stashes, release its actions, report the outcome.

                ADR-0008 D9 (Step 4): the runner decides *when* this runs — here
                on this thread when every action of the turn already finished,
                or on a background worker's own thread the moment it does. Either
                way it opens its own Event Log connection, because
                ``runtime.conn`` belongs to whichever thread ``drive_turn`` is on
                and SQLite would refuse it from the worker.

                Order is the ADR-0002 Dirty-tree policy: verify_diff has already
                reached its terminal by the time the driver asks, the stash goes
                back next, and only then may the cleanup terminal say the repo is
                safe.
                """
                finalizer_conn = open_event_log(runtime.runtime_paths.event_log)
                try:
                    _pop_pending_stashes(
                        finalizer_conn,
                        artifacts_root=runtime.runtime_paths.artifacts_root,
                        turn_id=effective_turn_id,
                    )
                    return _turn_verification_outcome(finalizer_conn, effective_turn_id)
                finally:
                    with contextlib.suppress(sqlite3.Error):
                        finalizer_conn.close()

            try:
                if runtime.action_runner is None:
                    _finalize_turn_resources()
                else:
                    runtime.action_runner.finalize_turn_cleanup(
                        effective_turn_id,
                        # Overridden by whatever `_finalize_turn_resources`
                        # derives; this is the value for a turn whose finalizer
                        # could not run at all.
                        verification_outcome="verification_skipped",
                        on_finalize=_finalize_turn_resources,
                    )
            except Exception:
                LOGGER.exception(
                    "drive_turn: turn cleanup finalizer failed (turn_id=%r)",
                    effective_turn_id,
                )
            release_turn_actions(effective_turn_id)


def _turn_verification_outcome(
    conn: sqlite3.Connection,
    turn_id: str,
) -> VerificationOutcome:
    """Report what this turn's cleanup actually established, not what it hoped.

    ADR-0008 D9 makes ``verification_outcome`` a durable claim about the
    repository's state, so it is derived from the turn's own rows rather than
    assumed: a surfaced stash conflict outranks everything, a
    ``semantics="verification"`` result means ``verify_diff`` passed, and the
    remaining case is honestly ``verification_skipped`` — the common one,
    because most turns dispatch no verifying action at all.
    """
    verified = False
    for event in iter_events(conn):
        if (event.correlation or {}).get("turn_id") != turn_id:
            continue
        if (
            event.type == "worker.artifact_observed"
            and event.payload.get("kind") == "stash_conflict"
        ):
            return "conflict_surfaced"
        if (
            event.type == "action.result_observed"
            and event.payload.get("semantics") == "verification"
        ):
            verified = True
    return "verified" if verified else "verification_skipped"


# --- Stash-pop finalizer (ADR-0002 § Dirty-tree policy) --------------------


def _task_repo_path(conn: sqlite3.Connection, task_id: str) -> Path | None:
    """Look up the ``repo_path`` payload from the latest ``task.created`` event.

    Walks the event log directly (L2 read is allowed from the
    composition root per ``.importlinter``); returns ``None`` when the
    task_id is unknown or carries no ``repo_path``. The stash-pop helper
    treats ``None`` as "skip this run" — we never `git -C` into a
    nonexistent dir.
    """
    latest_repo: str | None = None
    for evt in iter_events(conn):
        if evt.type != "task.created":
            continue
        if evt.payload.get("task_id") != task_id:
            continue
        raw = evt.payload.get("repo_path")
        if isinstance(raw, str) and raw:
            latest_repo = raw
    return Path(latest_repo) if latest_repo is not None else None


def _pop_pending_stashes(  # noqa: C901 — composition walker folds the clean / conflict / git-error stash-pop outcomes per worker.reported row; splitting the guard ladder hurts readability.
    conn: sqlite3.Connection,
    *,
    artifacts_root: Path,
    turn_id: str,
) -> None:
    """Restore every pre-task stash recorded by this turn's terminal events.

    Walks the event log for terminal worker / action events —
    ``worker.reported``, ``action.failed``, ``action.timeout_assumed``,
    ``action.cancelled`` — whose ``correlation.turn_id`` matches
    ``turn_id``. L4's ``spawn_worker_handler`` stamps the ``stash_ref``
    onto each of those payloads at emit time (success via the
    ``worker.reported`` literal, failure paths via
    ``_spawn_worker_emit_terminal_failure``); the ``run_id`` rides on the
    payload (``worker.reported``) or on the correlation (failure events).
    The cancel and assumed-timeout terminals are written by the
    ActionRunner rather than by L4 once ``spawn_worker`` is truly
    background, so those carry ``stash_ref`` plus ``run_id`` and
    ``task_id`` on the payload — see
    :func:`jarvis.execution.action_runner._stamp_worker_identity`. For
    each stash_ref-carrying row we
    call :func:`jarvis.execution.diff_capture.restore_pretask_changes`
    with the repo cwd resolved from ``task.created.repo_path``. The
    runtime invokes this finalizer from ``drive_turn``'s ``finally`` —
    strictly after the last ``decide()`` call, so verify_diff always
    read exactly Codex's tree, and unconditionally, so an exception
    mid-turn cannot orphan the stash.

    This call is the SOLE legitimate site for
    ``restore_pretask_changes``; canary
    ``test_canary_stash_pop_after_verify`` enforces that L4 never pops
    the stash and that L3 / runtime own the order (verify_diff first,
    pop second).

    Args:
        conn: Open Event Log connection.
        artifacts_root: ``RuntimePaths.artifacts_root`` — conflict
            patches land at ``<artifacts_root>/run_<run_id>/conflict.patch``.
        turn_id: The composition root's turn correlation id. Only
            terminal events tagged with this turn are popped.

    Returns:
        None. A clean pop adds no events. A stash-pop CONFLICT is routed
        through :func:`jarvis.decision.result_interpreter.emit_stash_conflict_surfacing`
        so it becomes observable — ``worker.artifact_observed(kind=stash_conflict)``
        plus a ``Limitation`` Claim — rather than a silent ``conflict.patch``
        write nothing references (ADR-0002 J13). Non-conflict
        :class:`StashError` is logged and skipped so a single stuck stash
        doesn't mask the user-facing response.
    """
    # Terminal event types that may carry a pre-task stash_ref. A run that
    # appears under two types (e.g. worker.reported + action.timeout_assumed)
    # is popped once via the shared seen_run_ids dedup below.
    #
    # `action.cancelled` belongs here for the same reason the other three do:
    # ADR-0008 §12 lists "cancelled-stash cleanup" among the paths that must
    # reach a cleanup terminal, and once `spawn_worker` runs in the background
    # the cancel terminal is written by the runner rather than the handler —
    # so it is the ONLY durable row naming the stash of a cancelled run. Omit
    # it and cancelling a Codex worker silently shelves the user's uncommitted
    # work with nothing left to restore it.
    terminal_types = {
        "worker.reported",
        "action.failed",
        "action.timeout_assumed",
        "action.cancelled",
    }
    seen_run_ids: set[str] = set()
    for evt in iter_events(conn):
        if evt.type not in terminal_types:
            continue
        if evt.correlation is None or evt.correlation.get("turn_id") != turn_id:
            continue
        # worker.reported carries run_id in the payload; the failure
        # events carry it on the correlation only.
        run_id_raw = evt.payload.get("run_id")
        if not isinstance(run_id_raw, str):
            run_id_raw = evt.correlation.get("run_id")
        stash_ref_raw = evt.payload.get("stash_ref")
        # Handler-written terminals carry task_id on the correlation; the two
        # the runner writes (cancel, assumed timeout) carry it on the payload,
        # because the runner's correlation is the canonical
        # {action_id, run_id?, turn_id?} triple and has no task slot.
        task_id_raw = evt.correlation.get("task_id") if evt.correlation is not None else None
        if not isinstance(task_id_raw, str):
            task_id_raw = evt.payload.get("task_id")
        if not isinstance(run_id_raw, str) or run_id_raw in seen_run_ids:
            continue
        seen_run_ids.add(run_id_raw)
        # stash_ref may be None on a clean tree at spawn-time — pass
        # through; restore_pretask_changes is a no-op for None.
        stash_ref: str | None = stash_ref_raw if isinstance(stash_ref_raw, str) else None
        if stash_ref is None:
            continue
        if not isinstance(task_id_raw, str):
            continue
        repo_path = _task_repo_path(conn, task_id_raw)
        if repo_path is None:
            continue
        try:
            conflict = restore_pretask_changes(
                repo_path,
                stash_ref,
                artifact_dir=artifacts_root,
                run_id=run_id_raw,
            )
        except StashError:
            # Don't propagate — a non-conflict error here means git
            # itself failed (e.g. stash ref vanished); log and continue
            # so the user-facing response is not held hostage.
            LOGGER.exception(
                "runtime: failed to pop stash %s for run %s",
                stash_ref,
                run_id_raw,
            )
            continue
        if conflict is None:
            continue
        # Stash-pop CONFLICT: restore_pretask_changes preserved the patch
        # and left the tree holding Codex's edits (a merge conflict is
        # reset to Codex's HEAD; an uncommitted-overwrite abort is left
        # as-is). Route it through L3 so the conflict surfaces as
        # worker.artifact_observed + a Limitation Claim
        # rather than a silent file write — ADR-0002 J13 / dirty-tree policy
        # ("never silently overwrite Allen's work"). The worker.reported row
        # carries the spawn_worker action that created the stash.
        action_id_raw = evt.payload.get("action_id")
        if not isinstance(action_id_raw, str):
            continue
        emit_stash_conflict_surfacing(
            conn,
            patch_path=conflict.patch_path,
            reason=conflict.reason,
            run_id=run_id_raw,
            action_id=action_id_raw,
            task_id=task_id_raw,
            source_event_id=evt.event_uid,
            correlation={
                "run_id": run_id_raw,
                "task_id": task_id_raw,
                "turn_id": turn_id,
            },
        )


__all__ = [
    "JarvisRuntime",
    "PreEmitTokenError",
    "RunTurnResult",
    "RuntimeBootstrapError",
    "TriggerWaitTimeout",
    "bootstrap_runtime_app",
    "drive_turn",
    # Re-exported for jarvis.cli: H13 forbids a single file importing two
    # middle-layer siblings, and the CLI already imports jarvis.deployment.
    # Same route PreEmitTokenError above already takes.
    "parse_response_channels",
    "run_turn",
]
