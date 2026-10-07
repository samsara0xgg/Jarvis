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
   L3 trigger event (``action.result_observed``, ``action.timeout_assumed``,
   ``action.failed`` or ``action.cancelled``). ``time.sleep`` is intentional
   here per spec §3.4.1: the composition root polls across thread
   boundaries; the "no time.sleep" rule applies only to L3 / L4 gate
   machinery.

Layer rules: ``jarvis.runtime`` may import everything below it. It is
imported by ``jarvis.cli`` only.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import ipaddress
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from collections.abc import Mapping  # runtime use: isinstance in the config readers.
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from datetime import time as clock
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from jarvis.decision import (
    DEFAULT_MAX_TOOL_ITERATIONS,
    DecideContext,
    EntityResolverLike,
    LifecycleLike,
    ResolvedEntityLike,
    ToolRegistryLike,
    decide,
    emit_turn_ended,
    open_prefix_warm,
    spoken_reply_rules,
)
from jarvis.decision.attention import rule_judge_v1
from jarvis.decision.confirm_grammar import ConfirmGrammarConfigError, load_confirm_grammar
from jarvis.decision.cost_guard import CostRecorder
from jarvis.decision.jev_oneshot import TOOL_GROUPS, JevOneShot
from jarvis.decision.llm import LLMClient
from jarvis.decision.llm_session import LLMSessionFactory
from jarvis.decision.mail_reply import MailReply
from jarvis.decision.moment import FIELDS as MOMENT_FIELDS
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
    spoken_risk_context,
    spoken_turn,
)
from jarvis.decision.response_run import (
    CancelAccepted,
    CancelAlreadyTerminal,
    CancelPlaybackAuthorized,
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
from jarvis.decision.stream_gate import routine_stream_policy, spoken_stream_policy
from jarvis.decision.surrogate_route import JevLog, SurrogateRoute, offered
from jarvis.decision.think_mode import ThinkMode, ThinkModeConfigError, load_think_mode
from jarvis.decision.tier0 import Tier0ConfigError, load_tier0_table, validate_tier0_table
from jarvis.decision.turn_end_asks import TurnEndAsks
from jarvis.decision.voice_words import VoiceWords
from jarvis.deployment import RuntimePaths, bootstrap_runtime, load_env_file
from jarvis.deployment.launchd import logs_dir
from jarvis.deployment.models import default_sensevoice_dir, default_silero_vad_path
from jarvis.deployment.night_power import MacPower
from jarvis.execution.dashboard_tool import build_dashboard_tool
from jarvis.execution.job_ledger_tool import build_job_ledger_tool
from jarvis.execution.mcp_oauth import DEFAULT_OAUTH_CALLBACK_PORT
from jarvis.execution.mcp_tools import DEFAULT_MCP_TIMEOUT_S, McpServers, is_oauth, stdio_env
from jarvis.execution.path_resolver import (
    FileTargetsConfigError,
    configure_file_targets,
    resolve_write_target,
)
from jarvis.execution.path_resolver import resolve as resolve_file_entity
from jarvis.execution.tools import (
    DEFAULT_SCREEN_MAX_WIDTH_PX,
    DEFAULT_WEB_FETCH_MAX_BYTES,
    DEFAULT_WEB_FETCH_MAX_TEXT_BYTES,
    DEFAULT_WEB_SEARCH_MAX_RESULTS,
    DEFAULT_WEB_TIMEOUT_S,
    ActionLifecycle,
    ReadOnlyToolRegistry,
    ToolContext,
    ToolRegistry,
    VisionClient,
    build_default_registry,
    release_turn_actions,
    turn_action_ids,
)
from jarvis.execution.workers import Workers, make_worker_tools
from jarvis.runtime.daily_report import (
    PLAN_SERVER,
    DailyReportService,
    DailySchedule,
    microsoft_plan,
    past_day_answer,
)
from jarvis.runtime.dashboard import (
    DRAFT_LINE_CHARS,
    DRAFT_LINE_PREFIX,
    PAGES,
    VIEW_LINE_CHARS,
    VIEW_LINE_PREFIX,
    MailDrafts,
    ViewState,
)
from jarvis.runtime.decision_state import DecisionStateCache
from jarvis.runtime.home import Home, mail_body, mail_summarizer
from jarvis.runtime.job_mail import LINKEDIN_ALERTS, JobMail, JobMailSettings
from jarvis.runtime.moment import Moment, MomentSettings
from jarvis.runtime.night_run import NightRun, night_settings
from jarvis.runtime.plugin_connections import PluginConnections
from jarvis.runtime.plugins import Plugins, load_plugins
from jarvis.runtime.projects import ProjectsService
from jarvis.runtime.reminders import Reminders
from jarvis.runtime.settings import REPLY_LINES, SETUP_VOICES, Settings, apply_settings
from jarvis.runtime.setup import write_setting
from jarvis.runtime.stream_bridge import LoopBoundTokenStream
from jarvis.runtime.work_state import WorkStateService, build_analyst
from jarvis.shared import CallerPrincipal, Event, lang, llm_io_log
from jarvis.shared.action_admission import bind_action_admission
from jarvis.shared.device_link import DeviceCallError
from jarvis.shared.lang import language_name
from jarvis.shared.pricing import load_pricing_table
from jarvis.shared.realtime import (
    RESPONSE_CANCEL_REASONS,
    AlreadyTerminal,
    Wave1FeatureFlags,
    Wave4ResponseFlags,
    Wave5InputFlags,
    new_response_id,
)
from jarvis.shared.realtime_trace import (
    configure_realtime_trace_jsonl,
    record_realtime_trace,
)
from jarvis.state.authorized_dispatch_outbox import (
    ConfirmationRevalidationError,
    answer_confirmation_once,
)
from jarvis.state.committed_event_bus import CommittedEventBus
from jarvis.state.daily_report import resolve_zone
from jarvis.state.event_log import (
    emit_event,
    iter_events_for_turn,
    open_event_log,
    open_runtime_event_log,
)
from jarvis.state.memory_db import (
    MemorySettings,
    SessionSettings,
    append_record,
    open_memory_db,
    record_sent,
    render_context,
)
from jarvis.state.projects import parse_catalog
from jarvis.state.stream_emission import committed_text_prefix
from jarvis.state.trigger_consumption import mark_trigger_consumed
from jarvis.state.turn_overlap import any_turn_in_flight, turn_activity_since
from jarvis.state.voice_settings import VoiceSettings
from jarvis.surface.ambient_sounds import AmbientSounds
from jarvis.surface.cli import (
    PreEmitTokenError,
    SurfaceState,
    emit_surface_user_intent,
    parse_response_channels,
    record_pre_emit_token,
)
from jarvis.surface.cli_render import render_response
from jarvis.surface.stream_emission import emit_permitted_segment
from jarvis.surface.terminal_events import BrainEvents
from jarvis.surface.terminal_link import TerminalHub
from jarvis.surface.voice_cues import VoiceCues

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import tzinfo

    from jarvis.decision import ResponsePlan
    from jarvis.decision.confirm_grammar import ConfirmGrammarTable
    from jarvis.decision.llm_stream import LLMStreamHandle
    from jarvis.decision.packet import SnapshotReader
    from jarvis.decision.tier0 import Tier0Table
    from jarvis.shared.realtime_trace import TraceValue


LOGGER = logging.getLogger("jarvis.runtime")


# --- Defaults ---------------------------------------------------------------

# Default config / prompt asset paths relative to the repo root. The
# composition root resolves the repo root by walking up from this module
# until a ``config/jarvis.yaml`` exists; tests / users may override.
_DEFAULT_CONFIG_FILENAME = Path("config") / "jarvis.yaml"
_DEFAULT_PROMPT_FILENAME = Path("prompts") / "jarvis_v1.md"

# ADR-0005 §12 pre-flight artifacts for hand-assembled runtimes (tests).
# ``bootstrap_runtime_app`` anchors the absent-key default at
# ``<runtime root>/models`` instead (``jarvis.deployment.models``).
DEFAULT_SENSEVOICE_DIR = Path("data/sensevoice-small-int8")
DEFAULT_SILERO_VAD_PATH = Path("data/silero_vad.onnx")

# Trigger event types the runtime loop expects from L4 paths.
# ``action.result_observed`` is defensive — sync tools emit it inline
# so decide() consumes it within one invocation, but we accept it here
# so a late-firing scheduled re-entry does not deadlock the poll loop.
# ``action.timeout_assumed`` / ``action.failed`` / ``action.cancelled``
# are the terminal failure events (supervisor sweep, dispatcher): the
# runtime must wake decide() so L3 can emit a canonical limitation
# response.
_RUNTIME_TRIGGER_TYPES: tuple[str, ...] = (
    "action.result_observed",
    "action.timeout_assumed",
    "action.failed",
    "action.cancelled",
)

# Default polling cadence for ``_wait_for_next_trigger``. 10 ms balances
# CPU usage with first-byte latency once the Timer fires.
_DEFAULT_POLL_INTERVAL_S: float = 0.01

# Default per-turn iteration ceiling. Protects ``run_turn`` from a stuck
# trigger chain. 50 is ample headroom.
_DEFAULT_MAX_ITERATIONS: int = 50

# Conversational per-trigger wait. 5 s is generous, and small enough that
# a stuck ordinary turn cannot pin one of ``max_concurrent_turns`` for
# long — see :func:`_trigger_wait_budget`.
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

# ADR-0008 §6 `realtime.response.cancel_timeout_ms` default. Implemented as
# the cancel connection's SQLite `busy_timeout`, so it bounds how long the
# terminal CAS waits for a contended writer — never an in-flight provider
# call, which has no cancellation seam until ADR-0008 Step 6.
_FALLBACK_CANCEL_TIMEOUT_MS: int = 500

# Default `tools.screen.vision_preset` (ADR-0011 D7) — the `llm.presets.*`
# key `screen_look` reads for its one vision call when the config's
# `tools.screen` block doesn't override it.
_DEFAULT_VISION_PRESET_NAME: str = "vision"

# System prompt for the injected vision client (`_LLMVisionClient` below).
# Asking for the user's language means `screen_look`'s text observation
# slots straight into a Tier 0 reply without a translation hop.
_VISION_SYSTEM_PROMPT: str = (
    "You are a screen-reading assistant. Describe what is currently "
    "visible in the screenshot factually and concisely. Respond in {language}."
)



# --- Exceptions -------------------------------------------------------------


class RuntimeBootstrapError(RuntimeError):
    """Raised when :func:`bootstrap_runtime_app` cannot assemble the runtime."""


class TriggerWaitTimeout(RuntimeError):  # noqa: N818 — Day-1 vocabulary keeps `Timeout` suffix per ADR § Multi-trigger loop.
    """Raised by :func:`_wait_for_next_trigger` when no trigger arrives in time."""


# --- ADR 0170 — the process's role ------------------------------------------

Role = Literal["all", "brain"]

# What the brain forces off whatever the user's files say: each of these reads or
# drives this machine's own devices, files or apps, and a terminal owns those.
_BRAIN_OVERRIDES: Final[dict[str, Any]] = {
    "observer": {
        "repos": [],
        "timesink": {"enabled": False},
        "usage": {"enabled": False},
        "claude_sessions": {"enabled": False},
    },
    "realtime": {"ambient_sounds": False, "gpt_live": {"enabled": False}},
    "daily_report": {"codex_sessions": False},
    "tools": {"workers": {"enabled": False}},
}


def _role(config: Mapping[str, Any]) -> Role:
    """``runtime.role``: ``all`` (one machine does everything, the default) or ``brain``."""
    block = config.get("runtime")
    value = block.get("role", "all") if isinstance(block, Mapping) else "all"
    if value == "all":
        return "all"
    if value == "brain":
        return "brain"
    msg = f"runtime: runtime.role must be all or brain, not {value!r}"
    raise RuntimeBootstrapError(msg)


def _listen(config: Mapping[str, Any], role: Role) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``runtime.listen_addresses`` and ``runtime.listen_hosts``: where a brain also answers.

    Only a brain may have any, and only on private addresses (ADR 0170): a wildcard or a
    public address stops the boot, so no setting can open the daemon to the internet.
    """
    block = config.get("runtime")
    block = block if isinstance(block, Mapping) else {}
    raw = {key: block.get(key) or [] for key in ("listen_addresses", "listen_hosts")}
    for key, value in raw.items():
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            msg = f"runtime: runtime.{key} must be a list of strings"
            raise RuntimeBootstrapError(msg)
    if not raw["listen_addresses"] and not raw["listen_hosts"]:
        return (), ()
    if role != "brain":
        msg = "runtime: runtime.listen_addresses and listen_hosts need runtime.role: brain"
        raise RuntimeBootstrapError(msg)
    for item in raw["listen_addresses"]:
        try:
            address = ipaddress.ip_address(item)
        except ValueError:
            msg = f"runtime: runtime.listen_addresses has {item!r}, which is not an IP address"
            raise RuntimeBootstrapError(msg) from None
        if address.is_unspecified or address.is_multicast or address.is_global:
            msg = f"runtime: runtime.listen_addresses has {item!r}, which is not a private address"
            raise RuntimeBootstrapError(msg)
    if any(not item or "*" in item for item in raw["listen_hosts"]):
        msg = "runtime: runtime.listen_hosts must be plain host names, no wildcards"
        raise RuntimeBootstrapError(msg)
    return tuple(raw["listen_addresses"]), tuple(raw["listen_hosts"])


def _for_role(config: dict[str, Any], role: Role) -> dict[str, Any]:
    """The config this role runs on: the brain's has every device-bound switch forced off."""
    return _overlay(config, _BRAIN_OVERRIDES) if role == "brain" else config


def _on_menu(table: Tier0Table, registry: ToolRegistry, role: Role) -> Tier0Table:
    """The Tier 0 rows the registry can serve.

    A brain does not hold the night-run tools; a row naming one would fail the boot's
    cross-check, so it is dropped (ADR 0170).
    """
    if role != "brain":
        return table
    held = {tool.name for tool in registry.get_definitions()}
    return tuple(row for row in table if row.tool_name in held)


def _no_audio_devices(_kind: str) -> list[str]:
    """The brain has no speaker or microphone to list."""
    return []


def _no_default_audio_device(_kind: str) -> None:
    """The brain has no default speaker or microphone to name."""


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


def _make_entity_resolver(
    conn: sqlite3.Connection, terminals: TerminalHub | None = None,
) -> EntityResolverLike:
    """Build the resolve-on-propose callable (ADR-0011 D4), closing over `conn`.

    Wired into `DecideContext.entity_resolver`; `_dispatch_one_tool_call`
    calls it for a `requires_entity=True` tool whose `target_entity_ref`
    is still unset. `path_resolver.resolve` never raises for a bad or
    unresolvable query (its own docstring's contract) — a miss is the
    normal `None` return, not an exception, so this wrapper adds no
    try/except of its own; a real bug inside `resolve` should surface,
    not be swallowed here.

    On a brain (ADR 0170) the file is on the terminal's disk, so the terminal resolves
    the name; no terminal connected is a miss.
    """

    def _resolve(query: str) -> ResolvedEntityLike | None:
        path: str
        source: str
        if terminals is None:
            target = resolve_file_entity(query, "file", conn)
            if target is None:
                return None
            path, source = str(target.path), target.source
        else:
            try:
                found = terminals.call("resolve_file", {"query": query}, None)
            except DeviceCallError:
                return None
            path, source = str(found.get("path", "")), str(found.get("source", ""))
            if not path:
                return None
        return _ResolvedFileEntity(
            entity_id=f"file:{path}",
            canonical=path,
            confidence="bookmark" if source == "bookmark" else "fuzzy",
            match_basis=source,
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
        tool_registry: L4 default registry.
        lifecycle: L4 per-process action lifecycle FSM.
        llm_client: L3 multi-provider LLM client.
        system_prompt: Rendered system prompt string (verbatim
            content of ``prompts/jarvis_v1.md``).
        tier0_table: Spec §17 Tier 0 whitelist loaded from
            ``config/tier0_patterns.yaml``; empty tuple = Tier 0
            disabled.
        confirm_grammar_table: ADR-0012 §3 D6 exact-sentence yes/no
            grammar loaded from ``config/confirm_grammar.yaml``; empty
            tuple = the answer-path grammar hook disabled (same "off
            means inert" posture as an empty ``tier0_table``).
        role: ``runtime.role`` (ADR 0170); the daemon starts no microphone,
            playback, power observer or device watcher in ``brain``.
        listen_addresses: ``runtime.listen_addresses``, the private addresses a
            brain also listens on; ``listen_hosts``, the Host names it accepts
            there. Empty unless the role is ``brain``.
        terminal_hub: the connected terminals a brain's device-bound tool calls go to;
            ``None`` unless the role is ``brain``.
    """

    config: Mapping[str, Any]
    runtime_paths: RuntimePaths
    conn: sqlite3.Connection
    tool_registry: ToolRegistry
    lifecycle: ActionLifecycle
    llm_client: LLMClient
    system_prompt: str
    tier0_table: Tier0Table = ()
    confirm_grammar_table: ConfirmGrammarTable = ()
    wave1_features: Wave1FeatureFlags = field(default_factory=Wave1FeatureFlags)
    response_flags: Wave4ResponseFlags = field(default_factory=Wave4ResponseFlags)
    llm_session_factory: LLMSessionFactory | None = None
    response_runs: ResponseRunRegistry | None = None
    committed_event_bus: CommittedEventBus | None = None
    # ADR 0164 — the one decision-snapshot cache every turn thread shares by reference
    # (internally locked, like ``committed_event_bus``); None folds the whole log per read.
    decision_state: DecisionStateCache | None = None
    input_flags: Wave5InputFlags = field(default_factory=Wave5InputFlags)
    # ADR-0006 §5 — the two voice artifact locations, already resolved against
    # the config file's own directory. The daemon reads these instead of
    # interpreting a relative module constant against its working directory;
    # the defaults keep a hand-assembled runtime byte-identical to today.
    sensevoice_dir: Path = DEFAULT_SENSEVOICE_DIR
    silero_vad_path: Path = DEFAULT_SILERO_VAD_PATH
    # memory.db wiring (``memory:`` config block). None = no memory store:
    # hand-assembled runtimes write no records and inject no note.
    memory: MemorySettings | None = None
    # ``session:`` config block: compaction thresholds and the Live brief budget.
    session: SessionSettings = field(default_factory=SessionSettings)
    # ADR-0008 Step 8 — tool cues loaded from ``config/tool_cues.yaml``;
    # empty tuple = no cue can veto the routine route (the other pre-route
    # conditions still apply).
    tool_cues: ToolCueTable = ()
    # ADR 0122: Jev between Tier 0 and the model; None = off (``realtime.surrogate_route``).
    surrogate_route: SurrogateRoute | None = None
    # ADR 0108: `llm.think`, the words that make one turn think. None = never.
    think_mode: ThinkMode | None = None
    # ADR 0019: the resident codex app-server and the four worker tools bound
    # to it. None = a hand-assembled runtime without workers.
    workers: Workers | None = None
    # ADR 0031: the MCP clients entered at boot. None = no `tools.mcp.servers`.
    mcp_servers: McpServers | None = None
    plugin_connections: PluginConnections | None = None
    # ADR 0023: the one current-work-state refresh workflow, shared by the
    # `refresh_work_state` tool and the Resonance dashboard routes.
    work_state: WorkStateService | None = None
    # ADR 0037: the project view and its sorting job. None = no `projects` list.
    projects: ProjectsService | None = None
    # ADR 0051: the companion home's Today, mail and brief reads. None = hand-assembled.
    home: Home | None = None
    # ADR 0176: what the Dashboard shows (``dashboard.view.enabled``). None = off.
    view: ViewState | None = None
    # ADR 0147: the Dashboard's mail page's reply drafts (``dashboard.mail.enabled``, which
    # needs the view: a draft goes under the letter the view has open). None = off.
    mail_drafts: MailDrafts | None = None
    # ADR 0148: one line each for the state block, in order; None skips a producer.
    live_context: tuple[Callable[[], str | None], ...] = ()
    # ADR 0152: what his voice carried that the words did not; shared with the voice path.
    voice_cues: VoiceCues | None = None
    # ADR 0151: non-speech sounds around him, fed by the voice session; its line is the last
    # live-context producer. None = off.
    ambient: AmbientSounds | None = None
    # ADR 0125: Jev's read of whether a finished agent turn asks Allen something. None = off.
    turn_end_asks: TurnEndAsks | None = None
    # ADR 0130: Jev's read of short words heard over her voice or in hands-free mode. None = off.
    voice_words: VoiceWords | None = None
    # ADR 0139: the voice line's one Jev request (intent, relation, tool group). None = off.
    oneshot: JevOneShot | None = None
    # ADR 0155: the job-mail poller and the ledger it keeps. None = off.
    job_mail: JobMail | None = None
    # ADR 0161: the situation TimeSink last recorded, read on demand. None = off.
    moment: Moment | None = None
    # ADR 0171: the clock that fires the owner's reminders. None = hand-assembled.
    reminders: Reminders | None = None
    # ADR 0052: the Settings page's file. None = hand-assembled.
    settings: Settings | None = None
    # ADR 0093: the night run; the daemon ticks it. None = hand-assembled.
    night: NightRun | None = None
    # ADR 0174: her voice volume and speed; the TTS provider reads it, `set_voice` writes it.
    voice_settings: VoiceSettings | None = None
    # ADR 0101: the day before's report, written once a day. None = `daily_report.at` unset.
    daily_schedule: DailySchedule | None = None
    # ADR 0170: ``brain`` runs headless and starts nothing device-bound.
    role: Role = "all"
    # ADR 0170: the private addresses a brain also listens on, and the Host names it accepts.
    listen_addresses: tuple[str, ...] = ()
    listen_hosts: tuple[str, ...] = ()
    # ADR 0170: where a brain's device-bound tool calls go. None unless ``role`` is ``brain``.
    terminal_hub: TerminalHub | None = None


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
            this turn drove.
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


_IDLE_WORKING_WINDOW_MS: Final[int] = 5 * 60 * 1000
_IDLE_QUIET_MS: Final[int] = 15 * 1000


def _daemon_idle(event_log_path: Path) -> bool:
    """No turn in flight and no turn, answer or playback row in the last 15 s (ADR 0164).

    The same in-flight probe the voice session's quiet clock uses
    (``_turn_working`` in ``inherent_loop``); an unreadable log counts as busy.
    """
    now_ms = int(time.time() * 1000)
    try:
        with contextlib.closing(
            open_runtime_event_log(event_log_path, deadline=time.monotonic() + 0.25),
        ) as conn:
            return not (
                any_turn_in_flight(conn, since_ms=now_ms - _IDLE_WORKING_WINDOW_MS)
                or turn_activity_since(conn, since_ms=now_ms - _IDLE_QUIET_MS)
            )
    except sqlite3.Error:
        return False


def _snapshot_reader(runtime: JarvisRuntime) -> SnapshotReader | None:
    """The shared incremental reader every read of a turn goes through (ADR 0164)."""
    return runtime.decision_state.read if runtime.decision_state is not None else None


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


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        msg = f"config at {path} is not a YAML mapping"
        raise RuntimeBootstrapError(msg)
    return raw


def _overlay(base: Mapping[str, Any], top: Mapping[str, Any]) -> dict[str, Any]:
    """``top`` over ``base``: mappings merge key by key, any other value replaces."""
    merged = dict(base)
    for key, value in top.items():
        below = merged.get(key)
        if isinstance(below, Mapping) and isinstance(value, Mapping):
            merged[key] = _overlay(below, value)
        else:
            merged[key] = value
    return merged


def _load_full_config(path: Path, settings_path: Path | None = None) -> Mapping[str, Any]:
    """The shipped config at ``path`` with the user's own settings laid over it.

    ``config/jarvis.yaml`` holds what every install shares; the runtime
    root's ``settings.yaml`` holds this user's values (projects, watched
    repos, timezone, vault, ...). A missing settings file changes nothing.
    """
    raw = _read_yaml_mapping(path)
    if settings_path is not None and settings_path.is_file():
        raw = _overlay(raw, _read_yaml_mapping(settings_path))
    name = _assistant_name(raw)
    return {key: _named(value, name) for key, value in raw.items()}


def _assistant_name(config: Mapping[str, Any]) -> str:
    """``assistant_name`` — what the user calls the assistant; ``Jarvis`` when unset."""
    name = config.get("assistant_name")
    return name.strip() if isinstance(name, str) and name.strip() else "Jarvis"


def _language(config: Mapping[str, Any]) -> lang.Language:
    """``language`` from the user's settings, else the system language."""
    raw = config.get("language")
    chosen = lang.normalize(raw)
    if chosen is None and raw not in (None, ""):
        LOGGER.warning("settings: language %r is not zh or en; using the system language", raw)
    return chosen or _system_language()


def _system_language() -> lang.Language:
    """The first of macOS's preferred languages (then ``$LANG``): Chinese or English."""
    out = ""
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["/usr/bin/defaults", "read", "-g", "AppleLanguages"],
                capture_output=True, text=True, timeout=2, check=False,
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            out = ""
    first = out.strip().strip("()").split(",")[0].strip().strip('"')
    return lang.normalize(first) or lang.normalize(os.environ.get("LANG")) or "en"


def save_language(settings_path: Path, code: str) -> lang.Language:
    """Switch every fixed sentence to ``code`` now and keep it in ``settings.yaml``.

    Only the top-level ``language:`` line changes (or is appended); the rest
    of the user's file, comments included, stays as written.
    """
    chosen = lang.normalize(code)
    if chosen is None:
        msg = f"language must be one of {', '.join(lang.LANGUAGES)}"
        raise ValueError(msg)
    write_setting(settings_path, "language", chosen)
    return lang.set_language(chosen)


def _named(value: Any, name: str) -> Any:  # noqa: ANN401 — any YAML value.
    """Put the assistant's name in for every ``{assistant}`` in the config's strings."""
    if isinstance(value, str):
        return value.replace("{assistant}", name)
    if isinstance(value, Mapping):
        return {key: _named(item, name) for key, item in value.items()}
    if isinstance(value, list):
        return [_named(item, name) for item in value]
    return value


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


def _voice_service_tier(config: Mapping[str, Any], trigger: Event) -> str | None:
    """``realtime.response.voice_service_tier``, for a turn Allen spoke; else ``None``.

    The tier rides every model request that turn makes through ``decide()`` and
    nothing else: the client still sends it to OpenAI's own host only.
    """
    realtime = config.get("realtime")
    response = realtime.get("response") if isinstance(realtime, Mapping) else None
    tier = response.get("voice_service_tier") if isinstance(response, Mapping) else None
    if not isinstance(tier, str) or not tier.strip() or not spoken_turn(trigger):
        return None
    return tier.strip()


def _jev_log(config: Mapping[str, Any], root: Path) -> JevLog | None:
    """``jev_log`` (ADR 0128): on unless ``enabled: false``; the file is made at the first line."""
    block = config.get("jev_log")
    if isinstance(block, Mapping) and block.get("enabled") is False:
        return None
    return JevLog(root / "jev" / "decisions.jsonl")


def _surrogate_route(
    config: Mapping[str, Any], config_path: Path, log: JevLog | None = None,
) -> SurrogateRoute | None:
    """``realtime.surrogate_route`` (ADR 0122): off unless enabled; bad values stop boot."""
    realtime = config.get("realtime")
    block = realtime.get("surrogate_route") if isinstance(realtime, Mapping) else None
    if not isinstance(block, Mapping) or block.get("enabled") is not True:
        return None
    model, bar, timeout = block.get("model"), block.get("min_confidence"), block.get("timeout_ms")
    if (
        not isinstance(model, str) or not model.strip()
        or isinstance(bar, bool) or not isinstance(bar, int | float) or not 0 < bar <= 1
        or isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0
    ):
        msg = (
            f"runtime: {config_path} realtime.surrogate_route needs model (text),"
            " min_confidence (0-1] and timeout_ms (positive int)"
        )
        raise RuntimeBootstrapError(msg)
    return SurrogateRoute(
        model=model.strip(), min_confidence=float(bar), timeout_ms=timeout,
        parallel=block.get("parallel") is True, zdr=block.get("zdr") is not False,
        log=log,
    )


def _max_tool_iterations(config: Mapping[str, Any], trigger: Event) -> int:
    """``llm.max_tool_iterations`` (ADR 0060); a spoken turn reads ``max_tool_iterations_voice``.

    Unset or not a positive int keeps decide()'s 5 (voice: the same bound as every other turn).
    """
    block = config.get("llm")
    if not isinstance(block, Mapping):
        return DEFAULT_MAX_TOOL_ITERATIONS
    keys = ("max_tool_iterations_voice", "max_tool_iterations") if spoken_turn(trigger) else (
        "max_tool_iterations",
    )
    for key in keys:
        value = block.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return DEFAULT_MAX_TOOL_ITERATIONS


def _think_mode(llm_config: Mapping[str, Any], config_path: Path) -> ThinkMode | None:
    """``llm.think`` (ADR 0108); a broken block stops boot like a broken tool-cue table."""
    try:
        return load_think_mode(llm_config)
    except ThinkModeConfigError as exc:
        msg = f"runtime: {config_path} {exc}"
        raise RuntimeBootstrapError(msg) from exc


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
    3. ``independent_response_cancel``, ``routine_streaming``,
       ``spoken_streaming`` or ``lifecycle_commentary`` requested without a
       surviving ``response_run_lifecycle``. Cancellation,
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
        or requested.routine_streaming
        or requested.spoken_streaming
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
        "independent_response_cancel=%s "
        "routine_streaming=%s spoken_streaming=%s lifecycle_commentary=%s; effective "
        "response_run_lifecycle=%s independent_response_cancel=%s "
        "routine_streaming=%s spoken_streaming=%s "
        "lifecycle_commentary=%s",
        reason,
        requested.response_run_lifecycle,
        requested.independent_response_cancel,
        requested.routine_streaming,
        requested.spoken_streaming,
        requested.lifecycle_commentary,
        flags.response_run_lifecycle,
        flags.independent_response_cancel,
        flags.routine_streaming,
        flags.spoken_streaming,
        flags.lifecycle_commentary,
    )
    record_realtime_trace(
        "response_activation_downgraded",
        reason=reason,
        requested=(
            f"response_run_lifecycle={requested.response_run_lifecycle},"
            f"independent_response_cancel={requested.independent_response_cancel},"
            f"routine_streaming={requested.routine_streaming},"
            f"spoken_streaming={requested.spoken_streaming},"
            f"lifecycle_commentary={requested.lifecycle_commentary}"
        ),
    )
    return _Wave4ResponseActivation(flags=flags, requested=requested, reason=reason)


def _wave4_response_flags(config: Mapping[str, Any]) -> Wave4ResponseFlags:
    """Return the validated Wave-4A flags for ``config``."""
    return _wave4_response_activation(config).flags


def _wave5_input_flags(config: Mapping[str, Any]) -> Wave5InputFlags:
    """Resolve the ADR-0008 D8 intent-pump switch.

    Parallel decisions require authorization consumption, isolated response
    clients and atomic accounting as well as input claims.
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


def _timesink_db_path(full_config: Mapping[str, Any]) -> Path | None:
    """Opt into local TimeSink reads; an absent setting never opens a user's store."""
    observer = full_config.get("observer")
    config = observer.get("timesink") if isinstance(observer, Mapping) else None
    if not isinstance(config, Mapping) or config.get("enabled") is not True:
        return None
    raw = config.get("db_path")
    if not isinstance(raw, str) or not raw.strip():
        message = "observer.timesink.db_path must be a nonempty local path"
        raise ValueError(message)
    return Path(raw).expanduser().resolve()


_FALLBACK_TIMESINK_POLL_INTERVAL_S: Final[float] = 300.0
_FALLBACK_WORK_STATE_PRESET: Final[str] = "gpt6-luna"
_DAILY_REPORT_TIMEOUT_S: Final[float] = 900.0
"""ADR 0028 — a whole day served whole: the draft call read 300k tokens in 175 s on v4-pro
(2026-09-12), and a retry after a timeout would resend it all."""


def _timesink_poll_interval_s(config: Mapping[str, Any]) -> float:
    """``observer.timesink.poll_interval_s`` — how often the head poll runs (ADR 0023)."""
    observer = config.get("observer")
    block = observer.get("timesink") if isinstance(observer, Mapping) else None
    raw = block.get("poll_interval_s") if isinstance(block, Mapping) else None
    return _positive_float(raw, _FALLBACK_TIMESINK_POLL_INTERVAL_S)


def _work_state_preset(config: Mapping[str, Any]) -> str:
    """``work_state.preset`` — the llm preset the on-demand analysis runs on (ADR 0023)."""
    block = config.get("work_state")
    raw = block.get("preset") if isinstance(block, Mapping) else None
    return raw if isinstance(raw, str) and raw.strip() else _FALLBACK_WORK_STATE_PRESET


def _work_state_timezone(config: Mapping[str, Any]) -> tzinfo | None:
    """``work_state.timezone`` — the local zone day windows are cut in; unset = system local."""
    block = config.get("work_state")
    raw = block.get("timezone") if isinstance(block, Mapping) else None
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return ZoneInfo(raw.strip())
    except ZoneInfoNotFoundError as exc:
        message = f"work_state.timezone must be an IANA zone name: {raw!r}"
        raise ValueError(message) from exc


def _audio_devices(kind: str) -> list[str]:
    """The Settings page's speaker / microphone choices; none when PortAudio cannot list them."""
    try:
        from jarvis.surface.voice_backend import device_names  # noqa: PLC0415 — loaded on demand.

        return device_names(kind)
    except Exception as exc:  # noqa: BLE001 — a text-only checkout has no audio stack.
        LOGGER.warning("settings: no %s devices: %s: %s", kind, type(exc).__name__, exc)
        return []


def _default_audio_device(kind: str) -> str | None:
    """The name the Settings page shows beside "System default"; none if CoreAudio will not say."""
    try:
        from jarvis.surface.voice_backend import default_device_name  # noqa: PLC0415 — on demand.

        return default_device_name(kind)
    except Exception as exc:  # noqa: BLE001 — a text-only checkout has no audio stack.
        LOGGER.warning("settings: no default %s device: %s: %s", kind, type(exc).__name__, exc)
        return None


def _home_weather(config: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """``home.weather`` — where the home's forecast is for (``latitude``, ``longitude``)."""
    block = config.get("home")
    place = block.get("weather") if isinstance(block, Mapping) else None
    return place if isinstance(place, Mapping) else None


def _dashboard_switch(config: Mapping[str, Any], name: str) -> bool:
    """``dashboard.<name>.enabled``: off unless true."""
    block = config.get("dashboard")
    switch = block.get(name) if isinstance(block, Mapping) else None
    return isinstance(switch, Mapping) and switch.get("enabled") is True


def _dashboard_mail(config: Mapping[str, Any]) -> bool:
    """``dashboard.mail.enabled`` (ADR 0147): the Dashboard's mail page; off unless true."""
    return _dashboard_switch(config, "mail")


def _dashboard_view(config: Mapping[str, Any]) -> bool:
    """``dashboard.view.enabled`` (ADR 0176): the shell reports its view."""
    return _dashboard_switch(config, "view")


def _ambient_sounds(config: Mapping[str, Any]) -> bool:
    """``realtime.ambient_sounds`` (ADR 0151): sound labels in the state block; on unless false."""
    realtime = config.get("realtime")
    value = realtime.get("ambient_sounds", True) if isinstance(realtime, Mapping) else True
    if not isinstance(value, bool):
        msg = "realtime.ambient_sounds must be true or false"
        raise TypeError(msg)
    return value


def _dashboard_state(
    config: Mapping[str, Any],
) -> tuple[ViewState | None, ViewState | None, MailDrafts | None]:
    """The Dashboard's view, the view the mail page reads, and the mail drafts (None = off).

    The mail page needs the view (ADR 0176: its open letter is the view's open item); with
    ``dashboard.mail.enabled`` alone it stays off.
    """
    view = ViewState() if _dashboard_view(config) else None
    if view is None and _dashboard_mail(config):
        LOGGER.warning("dashboard.mail.enabled needs dashboard.view.enabled: the mail page is off")
    mail_view = view if _dashboard_mail(config) else None
    return view, mail_view, None if mail_view is None else MailDrafts(mail_view)


def _register_dashboard_tool(registry: ToolRegistry, view: ViewState | None) -> None:
    """``show_on_dashboard`` (ADR 0176), registered only with ``dashboard.view.enabled``."""
    for tool in build_dashboard_tool(PAGES, None if view is None else view.present):
        registry.register(tool)


_LIVE_LINE_CHARS: Final = 200


def _live_lines(producers: tuple[Callable[[], str | None], ...]) -> tuple[str, ...]:
    """ADR 0148: what each live-context producer says now; a raising one is skipped, logged."""
    lines: list[str] = []
    for produce in producers:
        try:
            line = produce()
        except Exception:  # a producer must never fail a turn.
            LOGGER.exception("live context: a producer failed, skipped")
            continue
        if line:
            # The open letter's draft and the Dashboard's view are the lines that may run long.
            limit = (
                DRAFT_LINE_CHARS if line.startswith(DRAFT_LINE_PREFIX)
                else VIEW_LINE_CHARS if line.startswith(VIEW_LINE_PREFIX)
                else _LIVE_LINE_CHARS
            )
            lines.append(line[:limit])
    return tuple(lines)


def _mail_reply(
    config: Mapping[str, Any], config_path: Path, log: JevLog | None = None,
) -> MailReply | None:
    """``home.mail_reply`` (ADR 0123): off unless enabled; bad values stop boot."""
    home = config.get("home")
    block = home.get("mail_reply") if isinstance(home, Mapping) else None
    if not isinstance(block, Mapping) or block.get("enabled") is not True:
        return None
    model, timeout = block.get("model"), block.get("timeout_ms")
    bars = [  # fyi_at, yes_at, junk_at
        v for v in (block.get("fyi_at"), block.get("yes_at"), block.get("junk_at"))
        if isinstance(v, int | float) and not isinstance(v, bool)
    ]
    if (
        not isinstance(model, str) or not model.strip()
        or isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0
        or len(bars) != 3 or not 0 <= bars[0] < bars[1] <= 1  # noqa: PLR2004 — fyi_at, yes_at
        or not 0 < bars[2] <= 1
    ):
        msg = (
            f"runtime: {config_path} home.mail_reply needs model (text), timeout_ms (positive"
            " int), fyi_at < yes_at, both in [0, 1], and junk_at in (0, 1]"
        )
        raise RuntimeBootstrapError(msg)
    # min_confidence is the choice question's bar; this route asks none, so it stays unused.
    route = SurrogateRoute(
        model=model.strip(), min_confidence=1.0, timeout_ms=timeout, log=log,
    )
    rating = block.get("importance")  # ADR 0141
    return MailReply(
        route, float(bars[1]), float(bars[0]), float(bars[2]),
        importance=isinstance(rating, Mapping) and rating.get("enabled") is True,
    )


def _moment(config: Mapping[str, Any], config_path: Path, db_path: Path) -> Moment | None:
    """``moment`` (ADR 0161): off unless enabled; bad values stop boot.

    It reads TimeSink only through ``observer.timesink``: with that off every fact is unknown
    and nothing is held.
    """
    block = config.get("moment")
    if not isinstance(block, Mapping):
        return None
    if not isinstance(block.get("enabled"), bool):
        msg = f"runtime: {config_path} moment.enabled must be true or false"
        raise RuntimeBootstrapError(msg)
    fields = block.get("fields")
    if (
        not isinstance(fields, Mapping)
        or set(fields) != set(MOMENT_FIELDS)
        or not all(isinstance(on, bool) for on in fields.values())
    ):
        msg = (
            f"runtime: {config_path} moment.fields must set each of {MOMENT_FIELDS} "
            "to true or false"
        )
        raise RuntimeBootstrapError(msg)
    if not block["enabled"]:
        return None
    return Moment(MomentSettings(dict(fields)), _timesink_db_path(config), db_path)


def _job_mail(  # noqa: PLR0913 - the config, its collaborators and the moment
    config: Mapping[str, Any], config_path: Path, log: JevLog | None,
    connections: PluginConnections, db_path: Path,
    moment: Moment | None = None,
) -> JobMail | None:
    """``job_mail`` (ADR 0155): off unless enabled; bad values stop boot."""
    block = config.get("job_mail")
    if not isinstance(block, Mapping) or block.get("enabled") is not True:
        return None

    def number(key: str, *, low: float, high: float | None = None, whole: bool = False) -> Any:  # noqa: ANN401 - int or float by key
        value = block.get(key)
        kind = int if whole else int | float
        if isinstance(value, bool) or not isinstance(value, kind) or value < low or (
            high is not None and value > high
        ):
            msg = (
                f"runtime: {config_path} job_mail.{key} must be "
                f"{'an integer' if whole else 'a number'} of at least {low}"
                + ("" if high is None else f" and at most {high}")
            )
            raise RuntimeBootstrapError(msg)
        return value

    model = block.get("model")
    if not isinstance(model, str) or not model.strip():
        msg = f"runtime: {config_path} job_mail.model must be text"
        raise RuntimeBootstrapError(msg)
    if not isinstance(block.get("speak"), bool):
        msg = f"runtime: {config_path} job_mail.speak must be true or false"
        raise RuntimeBootstrapError(msg)
    if block.get("linkedin_alerts") not in LINKEDIN_ALERTS:
        msg = f"runtime: {config_path} job_mail.linkedin_alerts must be one of {LINKEDIN_ALERTS}"
        raise RuntimeBootstrapError(msg)
    excluded = block.get("exclude_domains")
    if not isinstance(excluded, list) or not all(
        isinstance(one, str) and one.strip() for one in excluded
    ):
        msg = f"runtime: {config_path} job_mail.exclude_domains must be a list of domain names"
        raise RuntimeBootstrapError(msg)
    since = block.get("backfill_since")
    if since is not None and (not isinstance(since, date) or isinstance(since, datetime)):
        msg = f"runtime: {config_path} job_mail.backfill_since must be a date (YYYY-MM-DD) or null"
        raise RuntimeBootstrapError(msg)
    settings = JobMailSettings(
        poll_s=float(number("poll_s", low=1)),
        backfill_days=number("backfill_days", low=1, high=60, whole=True),
        max_messages_per_cycle=number("max_messages_per_cycle", low=1, high=100, whole=True),
        header_skip_at=float(number("header_skip_at", low=0.0001, high=1)),
        body_min=float(number("body_min", low=0.0001, high=1)),
        max_body_chars=number("max_body_chars", low=100, whole=True),
        max_calls_per_day=number("max_calls_per_day", low=1, whole=True),
        speak=block["speak"],
        speak_gap_s=float(number("speak_gap_s", low=0)),
        linkedin_alerts=block["linkedin_alerts"],
        exclude_domains=tuple(one.strip().lower() for one in excluded),
        backfill_since=since,
    )
    timeout = number("timeout_ms", low=1, whole=True)
    # min_confidence is the choice question's bar; these questions read probabilities instead.
    route = SurrogateRoute(model=model.strip(), min_confidence=1.0, timeout_ms=timeout, log=log)
    return JobMail(
        settings, route, connections, db_path, rule_judge_v1,
        moment=moment, timesink_path=_timesink_db_path(config),
    )


def _register_job_ledger(registry: ToolRegistry, job_mail: JobMail | None) -> None:
    """The ``job_ledger`` tool reads what ``GET /inherent/jobs`` serves; absent while it is off."""
    for tool in build_job_ledger_tool(None if job_mail is None else job_mail.ledger):
        registry.register(tool)


def _turn_end_asks(
    config: Mapping[str, Any], config_path: Path, log: JevLog | None = None,
) -> TurnEndAsks | None:
    """``agents.turn_end_asks`` (ADR 0125): off unless enabled; bad values stop boot."""
    agents = config.get("agents")
    block = agents.get("turn_end_asks") if isinstance(agents, Mapping) else None
    if not isinstance(block, Mapping) or block.get("enabled") is not True:
        return None
    model, bar, timeout = block.get("model"), block.get("at"), block.get("timeout_ms")
    question_bar = block.get("question_at")
    if (
        not isinstance(model, str) or not model.strip()
        or isinstance(bar, bool) or not isinstance(bar, int | float) or not 0 < bar <= 1
        or isinstance(question_bar, bool) or not isinstance(question_bar, int | float)
        or not 0 < question_bar <= bar
        or isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0
    ):
        msg = (
            f"runtime: {config_path} agents.turn_end_asks needs model (text), at in (0, 1],"
            " question_at in (0, at] and timeout_ms (positive int)"
        )
        raise RuntimeBootstrapError(msg)
    # min_confidence is the choice question's bar; this route asks none, so it stays unused.
    route = SurrogateRoute(
        model=model.strip(), min_confidence=1.0, timeout_ms=timeout, log=log,
    )
    return TurnEndAsks(route, float(bar), float(question_bar))


def _voice_words(
    config: Mapping[str, Any], config_path: Path, log: JevLog | None = None,
) -> VoiceWords | None:
    """``realtime.jev_words`` (ADR 0130): off unless enabled; bad values stop boot."""
    realtime = config.get("realtime")
    block = realtime.get("jev_words") if isinstance(realtime, Mapping) else None
    if not isinstance(block, Mapping) or block.get("enabled") is not True:
        return None
    model, bar = block.get("model"), block.get("at")
    timeout, chars = block.get("timeout_ms"), block.get("max_chars")
    if (
        not isinstance(model, str) or not model.strip()
        or isinstance(bar, bool) or not isinstance(bar, int | float) or not 0 < bar <= 1
        or isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0
        or isinstance(chars, bool) or not isinstance(chars, int) or chars <= 0
    ):
        msg = (
            f"runtime: {config_path} realtime.jev_words needs model (text), at in (0, 1],"
            " timeout_ms (positive int) and max_chars (positive int)"
        )
        raise RuntimeBootstrapError(msg)
    # min_confidence is the choice question's bar; this route asks none, so it stays unused.
    route = SurrogateRoute(
        model=model.strip(), min_confidence=1.0, timeout_ms=timeout, log=log,
    )
    return VoiceWords(route, float(bar), chars)


def _event_emitter(event_log_path: Path) -> Callable[[str, dict[str, Any], str], None]:
    """``(type, payload, turn_id) -> None``: one event on a turn, on the caller's own connection."""

    def emit(event_type: str, payload: dict[str, Any], turn_id: str) -> None:
        with contextlib.closing(
            open_runtime_event_log(event_log_path, deadline=time.monotonic() + 1.0),
        ) as conn:
            emit_event(
                conn, type=event_type, payload=payload, correlation={"turn_id": turn_id},
            )

    return emit


def _bar(value: object) -> float | None:
    """A confidence bar in (0, 1], or None when it is anything else."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 < value <= 1:
        return None
    return float(value)


def _jev_oneshot(
    config: Mapping[str, Any],
    config_path: Path,
    log: JevLog | None,
    tier0_table: Tier0Table,
    emit: Callable[[str, dict[str, Any], str], None] | None = None,
) -> JevOneShot | None:
    """``realtime.jev_oneshot`` (ADR 0139, 0140): off unless enabled; bad values stop boot.

    It needs ``realtime.surrogate_route`` on (its instant functions and transport bar); the
    control words need ``realtime.jev_words`` too, and without it the surface asks nothing.
    """
    realtime = config.get("realtime")
    if not isinstance(realtime, Mapping):
        return None
    block = realtime.get("jev_oneshot")
    if not isinstance(block, Mapping) or block.get("enabled") is not True:
        return None
    prefix = f"runtime: {config_path} realtime.jev_oneshot"
    model, timeout = block.get("model"), block.get("timeout_ms")
    relation, tool_line = block.get("relation"), block.get("tool_line")
    words = realtime.get("jev_words")
    surrogate = realtime.get("surrogate_route")
    if not isinstance(surrogate, Mapping) or surrogate.get("enabled") is not True:
        msg = f"{prefix} needs realtime.surrogate_route enabled"
        raise RuntimeBootstrapError(msg)
    if (
        not isinstance(model, str) or not model.strip()
        or isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0
        or not isinstance(relation, Mapping) or not isinstance(tool_line, Mapping)
    ):
        msg = f"{prefix} needs model (text), timeout_ms (positive int), relation and tool_line"
        raise RuntimeBootstrapError(msg)
    relation_at = _bar(relation.get("at")) if relation.get("enabled") is True else None
    window = relation.get("window_s")
    tool_at = _bar(tool_line.get("at")) if tool_line.get("enabled") is True else None
    groups = tool_line.get("groups")
    if (
        (relation.get("enabled") is True and relation_at is None)
        or isinstance(window, bool) or not isinstance(window, int | float) or window <= 0
        or (tool_line.get("enabled") is True and tool_at is None)
        or not isinstance(groups, list) or not all(g in TOOL_GROUPS and g != "none" for g in groups)
    ):
        msg = (
            f"{prefix}.relation needs at in (0, 1] and window_s > 0; .tool_line needs at in"
            " (0, 1] and groups from the tool group names"
        )
        raise RuntimeBootstrapError(msg)
    words_at = words_timeout = words_chars = None
    if isinstance(words, Mapping) and words.get("enabled") is True:
        words_at, words_timeout = _bar(words.get("at")), words.get("timeout_ms")
        words_chars = words.get("max_chars")
        if (
            words_at is None
            or isinstance(words_timeout, bool) or not isinstance(words_timeout, int)
            or isinstance(words_chars, bool) or not isinstance(words_chars, int)
        ):
            msg = f"{prefix} reads realtime.jev_words at, timeout_ms and max_chars: not valid"
            raise RuntimeBootstrapError(msg)
    route = SurrogateRoute(
        model=model.strip(), min_confidence=1.0, timeout_ms=timeout,
        zdr=block.get("zdr") is not False, log=log,
    )
    return JevOneShot(
        route, offered(tier0_table),
        words_at=words_at, words_timeout_ms=words_timeout or 800, max_chars=words_chars or 24,
        relation_at=relation_at, window_s=float(window),
        tool_at=tool_at, tool_groups=frozenset(groups), emit=emit,
    )


def _daily_report_preset(config: Mapping[str, Any]) -> str:
    """``daily_report.preset`` — the llm preset the report runs on (ADR 0024)."""
    block = config.get("daily_report")
    raw = block.get("preset") if isinstance(block, Mapping) else None
    if isinstance(raw, str) and raw.strip():
        return raw
    return _work_state_preset(config)


def _codex_sessions_path(config: Mapping[str, Any]) -> Path | None:
    """Codex's own session directory, only when ``daily_report.codex_sessions`` opts in."""
    block = config.get("daily_report")
    if not isinstance(block, Mapping) or block.get("codex_sessions") is not True:
        return None
    root = Path.home() / ".codex" / "sessions"
    return root if root.is_dir() else None


def _daily_schedule(
    service: DailyReportService, event_log: Path, config: Mapping[str, Any],
) -> DailySchedule | None:
    """``daily_report.at`` — when the daemon writes yesterday's report (ADR 0101); unset: off."""
    block = config.get("daily_report")
    raw = block.get("at") if isinstance(block, Mapping) else None
    if raw is None:
        return None
    try:
        at = clock.fromisoformat(str(raw))
    except ValueError:
        LOGGER.warning("daily_report.at %r is not HH:MM; no daily report is written", raw)
        return None
    zone = resolve_zone(None, _work_state_timezone(config))[1]
    return DailySchedule(service, event_log_path=event_log, at=at, zone=zone)


def _work_state_tool_refresh(
    service: WorkStateService,
    daily_report: DailyReportService,
) -> Callable[[Mapping[str, Any], ToolContext], dict[str, Any]]:
    """Bind the service to the flat tool's ``(args, ctx)`` handler shape.

    A question about a past day gets that day's saved report instead of an analysis of now.
    """

    def refresh(args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
        # force too: the model passed force=true for "what did I do yesterday" (2026-10-01).
        past = past_day_answer(daily_report, ctx.conn, args.get("question"))
        if past is not None:
            return past
        return service.refresh(
            ctx.conn,
            question=args.get("question"),
            note=args.get("note"),
            force=bool(args.get("force", False)),
            trigger="conversation",
            action_id=ctx.action_id,
        )

    return refresh


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


def _obsidian_vault_root(config: Mapping[str, Any]) -> Path | None:
    """Return `tools.obsidian.vault_root`, `~`-expanded (ADR-0011 D7); blank is None.

    Threaded into `build_default_registry`'s `search_notes` closure at
    registry-build time — L4 handlers do not load YAML themselves. None
    registers no `search_notes`, so a user who named no vault never has a
    folder such as `~/Documents` touched on their behalf.
    """
    block = config.get("tools")
    if isinstance(block, Mapping):
        obsidian_block = block.get("obsidian")
        if isinstance(obsidian_block, Mapping):
            raw = obsidian_block.get("vault_root")
            if isinstance(raw, str) and raw.strip():
                return Path(raw).expanduser()
    return None


def _install_open_path(config: Mapping[str, Any]) -> None:
    """Hand `tools.open_path` to L4's resolver; malformed bookmarks fail the boot."""
    block = config.get("tools")
    raw = block.get("open_path") if isinstance(block, Mapping) else None
    try:
        configure_file_targets(raw if isinstance(raw, Mapping) else {})
    except FileTargetsConfigError as exc:
        msg = f"runtime: tools.open_path invalid: {exc}"
        raise RuntimeBootstrapError(msg) from exc


def _realtime_model_path(
    config: Mapping[str, Any],
    *,
    key: str,
    config_dir: Path,
    fallback: Path,
) -> Path:
    """Return `realtime.<key>` as an absolute path, or ``fallback`` (ADR-0006 §5).

    Resolution rule for every path-valued ``realtime.*`` key: ``~``-expanded,
    and if still relative anchored at the directory holding the config file the
    operator pointed at with ``--config`` — never at ``os.getcwd()``, which is
    the defect this key exists to remove, and never at ``repo_root``, which is
    itself the guess ``config_path.parent.parent``.

    An absent or malformed value degrades to ``fallback`` with one warning and
    never fails boot, matching :func:`_obsidian_vault_root`: a missing artifact
    is already handled softly by the daemon's pre-flight, so a config typo must
    not be harder to recover from than a missing file.
    """
    block = config.get("realtime")
    if not isinstance(block, Mapping):
        return fallback
    raw = block.get(key)
    if raw is None:
        return fallback
    if not isinstance(raw, str) or not raw.strip():
        LOGGER.warning(
            "realtime.%s must be a non-empty string; using %s.", key, fallback,
        )
        return fallback
    try:
        # `Path.expanduser` RAISES on a `~` it cannot resolve, unlike
        # `os.path.expanduser`, which returns the string untouched. A missing
        # slash -- `~models/silero.onnx` -- is enough, and this runs inside
        # `bootstrap_runtime_app`, so an uncaught raise takes down every CLI
        # command, not just `serve`.
        candidate = Path(raw.strip()).expanduser()
    except RuntimeError:
        LOGGER.warning(
            "realtime.%s (%r) has an unresolvable '~' prefix; using %s.",
            key,
            raw,
            fallback,
        )
        return fallback
    if candidate.is_absolute():
        return candidate
    return (config_dir / candidate).resolve()


def _web_search_provider_config(config: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Return `(search_provider, api_key)` from `tools.web.*`.

    A blank `search_provider` picks by which key the environment holds,
    `TAVILY_API_KEY` first, then `EXA_API_KEY`; with neither it returns
    `(None, None)` and no `web_search` is registered.

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
    provider = ""
    raw_key_env: object = None
    block = config.get("tools")
    if isinstance(block, Mapping):
        web_block = block.get("web")
        if isinstance(web_block, Mapping):
            raw_provider = web_block.get("search_provider")
            if isinstance(raw_provider, str):
                provider = raw_provider.strip()
            raw_key_env = web_block.get("search_api_key_env")
    if not provider:
        for name in ("tavily", "exa"):
            found = os.environ.get(f"{name.upper()}_API_KEY", "").strip()
            if found:
                return name, found
        return None, None

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
the synchronous `decide()` loop on a hung proxy. Scoped to the vision
client; the decision loop's own bound is `llm.timeout_s` in
config/jarvis.yaml."""

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
                    system=_VISION_SYSTEM_PROMPT.format(language=language_name()),
                )
            else:
                result = self._cost_recorder.chat(
                    self._llm_client,
                    messages=messages,
                    system=_VISION_SYSTEM_PROMPT.format(language=language_name()),
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


def diagnostics_flag(config: Mapping[str, Any], key: str) -> bool:
    """A ``diagnostics:`` switch (ADR 0120); absent or anything but ``true`` is off."""
    block = config.get("diagnostics")
    return isinstance(block, Mapping) and block.get(key) is True


def _load_runtime_env_and_trace(paths: RuntimePaths) -> None:
    """Load fill-only runtime env, then apply its optional trace destination."""
    load_env_file(paths.root)
    _configure_realtime_trace_export(paths)


def _register_workers(
    registry: ToolRegistry, paths: RuntimePaths, config: Mapping[str, Any]
) -> Workers | None:
    """ADR 0019: workers are threads on one ``codex app-server``.

    Off unless ``tools.workers.enabled`` and ``codex`` is on this machine;
    ``tools.workers.roots`` lists the folders a worker may be started in.
    The server starts lazily on the first spawn, so a one-shot CLI turn
    pays nothing; the daemon stops it at shutdown.
    """
    tools_block = config.get("tools")
    block = tools_block.get("workers") if isinstance(tools_block, Mapping) else None
    if not isinstance(block, Mapping) or block.get("enabled") is not True:
        return None
    codex = shutil.which("codex")
    if codex is None:
        LOGGER.warning("tools.workers is on but codex is not on PATH; no workers this boot")
        return None
    roots = tuple(Path(str(r)).expanduser().resolve() for r in block.get("roots") or ())
    workers = Workers(paths.root / "codex.sock", paths.event_log, codex_bin=codex)
    for worker_tool in make_worker_tools(workers, roots):
        registry.register(worker_tool)
    return workers


def _mcp_block(config: Mapping[str, Any]) -> Mapping[str, Any]:
    """The `tools.mcp` block, or an empty mapping."""
    tools_block = config.get("tools")
    block = tools_block.get("mcp") if isinstance(tools_block, Mapping) else None
    return block if isinstance(block, Mapping) else {}


def _mcp_servers(
    block: Mapping[str, Any], paths: RuntimePaths, *, open_url: Callable[[str], object] | None
) -> McpServers:
    """One :class:`McpServers` per the block's knobs; tokens live under `<root>/mcp/`."""
    return McpServers(
        timeout_s=float(block.get("timeout_s", DEFAULT_MCP_TIMEOUT_S)),
        token_dir=paths.root / "mcp",
        callback_port=int(block.get("oauth_callback_port", DEFAULT_OAUTH_CALLBACK_PORT)),
        open_url=open_url,
    )


def _plugins(config: Mapping[str, Any], repo_root: Path) -> Plugins:
    """ADR 0035: the plugins `tools.plugins` switches on, read from `<repo>/plugins/`."""
    tools_block = config.get("tools")
    block = tools_block.get("plugins") if isinstance(tools_block, Mapping) else None
    return load_plugins(repo_root / "plugins", block if isinstance(block, Mapping) else {})


def _all_mcp_servers(config: Mapping[str, Any], plugins: Plugins) -> dict[str, Any]:
    """Plugin servers, then `tools.mcp.servers`, which replaces a plugin server of its name."""
    servers = _mcp_block(config).get("servers")
    return {**plugins.servers, **(dict(servers) if isinstance(servers, Mapping) else {})}


def _wire_plan_reader(service: DailyReportService, connections: PluginConnections) -> None:
    """ADR 0036: the report reads calendar and To Do itself, through the live connection."""
    if any(PLAN_SERVER in package.servers for package in connections.packages.values()):
        service.plan_reader = lambda start, end: microsoft_plan(
            connections.client_for(PLAN_SERVER), start, end
        )


def mcp_login(
    server: str, *, config_path: Path | None = None, runtime_root: Path | None = None
) -> int:
    """ADR 0032: log one `auth: oauth` server in through the browser; the daemon reuses the token.

    A local server that keeps its own login names its login command in
    ``login_args`` (ADR 0055); that command runs here, in the terminal, with the
    entry's command and environment, so it asks for exactly what the daemon's
    server will use. Returns a process exit code: 0 logged in, 1 the server
    refused or never asked for a login, 2 the entry is missing or has no login;
    a login command's own exit code otherwise.
    """
    if config_path is None:
        repo_root = _locate_repo_root(Path(__file__).parent)
        config_path = repo_root / _DEFAULT_CONFIG_FILENAME
    else:
        repo_root = config_path.resolve().parent.parent
    paths = bootstrap_runtime(runtime_root)
    load_env_file(paths.root)
    config = _load_full_config(config_path, paths.settings)
    block = _mcp_block(config)
    servers = _all_mcp_servers(config, _plugins(config, repo_root))
    spec = servers.get(server)
    if not isinstance(spec, Mapping):
        known = ", ".join(sorted(servers)) or "none"
        sys.stderr.write(
            f"mcp-login: no server {server!r} in `tools.mcp.servers` or an enabled plugin"
            f" of {config_path} (known: {known})\n"
        )
        return 2
    if spec.get("command") and spec.get("login_args"):
        login = [str(spec["command"]), *(os.path.expandvars(str(a)) for a in spec["login_args"])]
        env = {**os.environ, **stdio_env(spec)}
        return subprocess.run(login, env=env, cwd=spec.get("cwd"), check=False).returncode  # noqa: S603 — Allen's own config names the command.
    if not is_oauth(spec):
        sys.stderr.write(f"mcp-login: {server} does not log in with OAuth; nothing to do\n")
        return 2
    mcp_servers = _mcp_servers(block, paths, open_url=webbrowser.open)
    try:
        tools = mcp_servers.connect({server: spec})
    finally:
        mcp_servers.stop()
    token = mcp_servers.token_path(server)
    if not tools:
        sys.stderr.write(f"mcp-login: {server} did not come up; see the warning above\n")
        return 1
    if not mcp_servers.has_login(server):
        # Some servers answer tools/list unauthenticated; that is not a login.
        sys.stderr.write(
            f"mcp-login: {server} answered without asking for a login; no token stored\n"
        )
        return 1
    sys.stdout.write(
        f"mcp-login: {server} logged in, {len(tools)} tools; token at {token}. "
        "Restart the daemon to put them on the menu.\n"
    )
    return 0


def bootstrap_runtime_app(  # noqa: C901, PLR0915 - composition root wiring stays explicit
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
       registers the tools; the :class:`ActionLifecycle` is per-process FSM.
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

    config_dir = config_path.resolve().parent

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
    # ADR 0052: the Settings page's saved values lie over the YAML and the
    # user's settings.yaml for this boot.
    full_config = apply_settings(_load_full_config(config_path, paths.settings), paths.root)
    role = _role(full_config)
    listen_addresses, listen_hosts = _listen(full_config, role)
    full_config = _for_role(full_config, role)
    lang.set_language(_language(full_config))
    log_llm_io = diagnostics_flag(full_config, "log_llm_io")
    llm_io_log.configure(logs_dir(paths.root) / "llm-io.jsonl" if log_llm_io else None)
    realtime_block = full_config.setdefault("realtime", {})
    if not realtime_block.get("tts_voice"):
        realtime_block["tts_voice"] = SETUP_VOICES[lang.language()][0]
    wave1_features = _wave1_feature_flags(full_config)
    (
        web_search_max_results,
        web_fetch_max_bytes,
        web_fetch_max_text_bytes,
        web_timeout_s,
    ) = _web_tools_config(full_config)
    web_search_provider, web_search_api_key = _web_search_provider_config(full_config)
    _install_open_path(full_config)
    vision_preset_name, screen_max_width_px = _screen_tools_config(full_config)
    memory = MemorySettings.from_config(full_config.get("memory"), runtime_root=paths.root)
    # ADR 0170: a brain's TimeSink and git are its terminal's, so what reads them asks the hub.
    terminal_hub = TerminalHub(events=BrainEvents(conn)) if role == "brain" else None
    device = None if terminal_hub is None else terminal_hub.call
    moment = _moment(full_config, config_path, memory.db_path)
    # ADR 0068: refuse a memory.db a newer Jarvis wrote before anything writes to it.
    open_memory_db(memory.db_path).close()
    session = SessionSettings.from_config(full_config.get("session"))
    work_state = WorkStateService(
        event_log_path=paths.event_log,
        memory_path=memory.db_path,
        timesink_path=_timesink_db_path(full_config),
        repos=_observer_repo_paths(full_config),
        analyst=build_analyst(
            full_config,
            _work_state_preset(full_config),
            pricing_path=repo_root / "data" / "pricing.json",
            account_cost=wave1_features.exactly_once_cost_accounting,
        ),
        model=_work_state_preset(full_config),
        tz=_work_state_timezone(full_config),
        device=device,
    )
    catalog = parse_catalog(full_config.get("projects"))
    projects = (
        None
        if not catalog
        else ProjectsService(
            event_log_path=paths.event_log,
            timesink_path=_timesink_db_path(full_config),
            projects=catalog,
            sorter=build_analyst(
                full_config,
                _work_state_preset(full_config),
                pricing_path=repo_root / "data" / "pricing.json",
                account_cost=wave1_features.exactly_once_cost_accounting,
                kind="projects",
            ),
            model=_work_state_preset(full_config),
            tz=_work_state_timezone(full_config),
        )
    )
    daily_report = DailyReportService(
        memory_path=memory.db_path,
        timesink_path=_timesink_db_path(full_config),
        repos=_observer_repo_paths(full_config),
        reporter=build_analyst(
            full_config,
            _daily_report_preset(full_config),
            pricing_path=repo_root / "data" / "pricing.json",
            account_cost=wave1_features.exactly_once_cost_accounting,
            kind="daily_report",
            timeout_s=_DAILY_REPORT_TIMEOUT_S,
        ),
        model=_daily_report_preset(full_config),
        tz=_work_state_timezone(full_config),
        codex_sessions_path=_codex_sessions_path(full_config),
        device=device,
    )
    night = (
        None
        if role == "brain"  # ADR 0170: holding a Mac awake and dark is the terminal's job
        else NightRun(
            paths.event_log,
            night_settings(full_config),
            MacPower(),
            zone=resolve_zone(None, _work_state_timezone(full_config))[1],
        )
    )
    voice_settings = VoiceSettings(paths.root / "voice-settings.json")
    view, mail_view, mail_drafts = _dashboard_state(full_config)
    voice_cues = VoiceCues()
    ambient = (
        AmbientSounds(log_path=logs_dir(paths.root) / "ambient-sounds.jsonl")
        if _ambient_sounds(full_config) else None
    )
    registry = build_default_registry(
        mail_drafts=mail_drafts,
        memory_db_path=memory.db_path,
        observed_repos=_observer_repo_paths(full_config),
        timesink_db_path=_timesink_db_path(full_config),
        work_state_refresh=_work_state_tool_refresh(work_state, daily_report),
        night=night,
        voice_settings=voice_settings,
        confirmation_dispatch_outbox=wave1_features.confirmation_dispatch_outbox,
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
        device_link=device,
    )
    workers = _register_workers(registry, paths, full_config)
    _register_dashboard_tool(registry, view)
    plugin_connections = PluginConnections(
        repo_root=repo_root, runtime_root=paths.root, event_log=paths.event_log,
        registry=registry, config=full_config,
    )
    plugin_connections.initialize()
    _wire_plan_reader(daily_report, plugin_connections)
    lifecycle = ActionLifecycle()

    # 3b. Spec §17 Tier 0 whitelist — sits next to jarvis.yaml so Allen
    #     edits one config directory. Invalid content fails the boot
    #     loudly (no silent pattern drops); missing file = Tier 0 off.
    tier0_path = config_path.parent / "tier0_patterns.yaml"
    try:
        tier0_table = _on_menu(load_tier0_table(tier0_path), registry, role)
        regex_router_tools = registry.for_caller(CallerPrincipal.REGEX_ROUTER)
        validate_tier0_table(
            tier0_table,
            allowed_tool_names=frozenset(t.name for t in regex_router_tools),
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
    llm_config = full_config.get("llm")
    if not isinstance(llm_config, Mapping):
        msg = f"runtime: {config_path} has no 'llm' section"
        raise RuntimeBootstrapError(msg)
    llm_client = LLMClient(llm_config)
    think_mode = _think_mode(llm_config, config_path)

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

    # Plugin skills are sampled per turn so connections need no daemon restart.
    system_prompt = prompt_path.read_text(encoding="utf-8").replace(
        "{assistant}", _assistant_name(full_config)
    )
    if full_config.get("reply_language") in REPLY_LINES:
        reply_line = REPLY_LINES[full_config["reply_language"]]
        system_prompt = f"{system_prompt.rstrip()}\n\n{reply_line}\n"
    plugin_connections.publish_event = (
        committed_event_bus.publish if committed_event_bus is not None else None
    )

    jev_log = _jev_log(full_config, paths.root)
    job_mail = _job_mail(
        full_config, config_path, jev_log, plugin_connections, memory.db_path, moment,
    )
    _register_job_ledger(registry, job_mail)
    return JarvisRuntime(
        config=full_config,
        runtime_paths=paths,
        conn=conn,
        tool_registry=registry,
        lifecycle=lifecycle,
        llm_client=llm_client,
        system_prompt=system_prompt,
        role=role,
        listen_addresses=listen_addresses,
        listen_hosts=listen_hosts,
        terminal_hub=terminal_hub,
        tier0_table=tier0_table,
        confirm_grammar_table=confirm_grammar_table,
        wave1_features=wave1_features,
        response_flags=response_flags,
        llm_session_factory=llm_session_factory,
        response_runs=response_runs,
        committed_event_bus=committed_event_bus,
        decision_state=DecisionStateCache(
            partial(open_runtime_event_log, paths.event_log),
            is_idle=partial(_daemon_idle, paths.event_log),
        ),
        input_flags=_wave5_input_flags(full_config),
        surrogate_route=_surrogate_route(full_config, config_path, jev_log),
        memory=memory,
        session=session,
        workers=workers,
        plugin_connections=plugin_connections,
        sensevoice_dir=_realtime_model_path(
            full_config,
            key="sensevoice_dir",
            config_dir=config_dir,
            fallback=default_sensevoice_dir(paths.root),
        ),
        silero_vad_path=_realtime_model_path(
            full_config,
            key="silero_vad_path",
            config_dir=config_dir,
            fallback=default_silero_vad_path(paths.root),
        ),
        tool_cues=tool_cues,
        think_mode=think_mode,
        work_state=work_state,
        projects=projects,
        home=Home(
            plugin_connections,
            resolve_zone(None, _work_state_timezone(full_config)),
            _home_weather(full_config),
            _mail_reply(full_config, config_path, jev_log),
            mail_view,
            None if mail_view is None else mail_summarizer(
                # ADR 0148: the cheapest preset (gpt-6-luna), never Jev: a body goes here.
                build_analyst(
                    full_config,
                    _work_state_preset(full_config),
                    pricing_path=repo_root / "data" / "pricing.json",
                    account_cost=wave1_features.exactly_once_cost_accounting,
                    kind="mail_summary",
                ),
                paths.event_log,
            ),
        ),
        view=view,
        mail_drafts=mail_drafts,
        ambient=ambient,
        live_context=(
            *(() if view is None else (view.line,)),
            *(() if mail_drafts is None else (mail_drafts.line,)),
            voice_cues.line,
            *(() if ambient is None else (ambient.line,)),
        ),
        voice_cues=voice_cues,
        turn_end_asks=_turn_end_asks(full_config, config_path, jev_log),
        job_mail=job_mail,
        moment=moment,
        reminders=Reminders(paths.event_log, moment=moment),
        voice_words=_voice_words(full_config, config_path, jev_log),
        oneshot=_jev_oneshot(
            full_config, config_path, jev_log, tier0_table, _event_emitter(paths.event_log),
        ),
        settings=Settings(
            paths.root,
            full_config,
            _no_audio_devices if role == "brain" else _audio_devices,
            _no_default_audio_device if role == "brain" else _default_audio_device,
        ),
        night=night,
        voice_settings=voice_settings,
        daily_schedule=_daily_schedule(daily_report, paths.event_log, full_config),
    )


# --- ADR-0008 Wave 4A ResponseRun seams -------------------------------------


def _labels_phases(base_url: str | None) -> bool:
    """Whether the host labels each message ``commentary`` or ``final_answer``.

    Only OpenAI's own /v1/responses does. On a compatible host (the x.ai
    presets) the line before a call would stream as the answer.
    """
    return "api.openai.com" in (base_url or "api.openai.com")


def _warm_next_prefix(
    runtime: JarvisRuntime,
    memory: MemorySettings,
    *,
    llm_client: LLMClient,
    system_prompt: str,
    responses: bool,
) -> None:
    """Send the next turn's prompt prefix in the background (``open_prefix_warm``).

    The history is read the way the next turn will read it, now that this
    turn's rows are in. Its own thread and Event Log connection: the answer is
    already on its way and nothing waits for this.
    """

    async def _drain(handle: LLMStreamHandle) -> None:
        async for _event in handle.events():
            pass

    def _run() -> None:
        try:
            history = render_context(
                memory.db_path, exclude_id="", since=runtime.session.history_since,
                recent=runtime.session.recent_records,
                context=runtime.session.context,
                raw_max_chars=runtime.session.context_raw_max_chars,
            ).history
            with contextlib.closing(
                open_runtime_event_log(runtime.runtime_paths.event_log),
            ) as conn:
                handle = open_prefix_warm(
                    conn,
                    llm_client=llm_client,
                    system_prompt=system_prompt,
                    history=history,
                    tool_registry=cast("ToolRegistryLike", runtime.tool_registry),
                    responses=responses,
                    committed_event_bus=runtime.committed_event_bus,
                )
                if handle is not None:
                    asyncio.run(_drain(handle))
        except Exception:  # noqa: BLE001 — a failed warm-up costs only the cache it tried to fill
            LOGGER.warning("prefix warm failed", exc_info=True)

    threading.Thread(target=_run, name="jarvis-prefix-warm", daemon=True).start()


def _late_cancel_reason(registry: ResponseRunRegistry, run: ResponseRun) -> str | None:
    """Why a run that just opened is already cancelled: a stop or a newer sentence came first."""
    if registry.turn_stopped(run.turn_id):
        return "user_stop"
    if (
        run.phase == "final"
        and run.interrupt_policy.generation_action == "cancel"
        and registry.turn_superseded(run.turn_id)
    ):
        return "superseded"
    return None


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
    ``routine_stream_policy`` with the streaming seam bound. With
    ``spoken_streaming`` on, a turn Allen spoke opens as ``spoken`` under
    ``spoken_stream_policy`` first. Every other turn (and every correction
    run) keeps ``legacy_full_text_policy``.
    """
    if not runtime.response_flags.response_run_lifecycle:
        return None
    if runtime.llm_session_factory is None:  # pragma: no cover - bootstrap pairs them
        return None

    response_id = new_response_id()
    think = runtime.think_mode
    preset = think.preset_for(runtime.conn, user_intent_event) if think else None
    snapshot = runtime.llm_session_factory.snapshot(preset)
    request_client = runtime.llm_session_factory.create(snapshot, response_id=response_id)
    policy = legacy_full_text_policy(
        evidence_snapshot_hash=evidence_snapshot_hash(runtime.conn),
        preset_snapshot_hash=snapshot.snapshot_hash,
    )
    route: str | None = None
    context = None
    if (
        runtime.response_flags.spoken_streaming
        and correction is None
        and spoken_turn(user_intent_event)
        and snapshot.provider == "openai"
        and _labels_phases(snapshot.base_url)
    ):
        # docs/plans/speak-as-written-proposal.md: a turn Allen spoke streams
        # every request through /v1/responses, whose phase labels tell the line
        # before a call from the answer, and speaks the answer as it is written.
        route = "spoken"
        context = spoken_risk_context(
            assemble_packet(user_intent_event, runtime.conn, _snapshot_reader(runtime)),
            response_id=response_id,
            turn_id=turn_id,
        )
        policy = spoken_stream_policy(context, preset_snapshot_hash=snapshot.snapshot_hash)
    elif runtime.response_flags.routine_streaming and correction is None:
        packet = assemble_packet(user_intent_event, runtime.conn, _snapshot_reader(runtime))
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
        # Checked after registering, so a stop (or a sweep, ADR 0074) landing at
        # any moment either sees the run or is seen here.
        late_cancel = _late_cancel_reason(runtime.response_runs, run)
        if late_cancel is not None:
            request_response_cancel(
                runtime.response_runs,
                terminalizer,
                ResponseCancelRequest(
                    request_id="CREQ" + uuid.uuid4().hex,
                    response_id=run.response_id,
                    scope="generation",
                    reason=late_cancel,
                ),
            )
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
        first_clause_chars=(
            runtime.response_flags.spoken_first_clause_chars if route == "spoken" else 0
        ),
        structured=route == "spoken" and runtime.response_flags.spoken_structured,
    )
    record_realtime_trace(
        "routine_stream_route_opened", turn_id=turn_id, response_id=response_id,
    )
    return run, terminalizer, seam


def make_response_cancel_callable(  # noqa: C901 - one seam, two scopes' exact outcome words
    runtime: JarvisRuntime,
    *,
    stop_foreground_output: Callable[[str], str] | None = None,
) -> Callable[[str, str, str], str]:
    """Build the injectable ``(response_id, scope, reason) -> outcome`` seam.

    Returned strings for ``scope="generation"``: ``"cancelled"``,
    ``"already_terminal"``, ``"unknown_response"``, ``"unsupported_scope"``,
    ``"timeout"``. ``scope="foreground_output"`` adds L5's own words
    ``"applied"``, ``"stale"`` and ``"uncertain"``, plus
    ``"policy_hash_mismatch"`` and ``"policy_ignore"`` from L3's policy
    check. Without ``stop_foreground_output`` there is no playback actor to
    reach, so that scope answers ``"unsupported_scope"``.

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

    def _cancel(  # noqa: PLR0911 - policy, timeout, CAS and playback outcomes stay distinct
        response_id: str,
        scope: str,
        reason: str,
    ) -> str:
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
        if isinstance(outcome, CancelPlaybackAuthorized):
            if stop_foreground_output is None:
                return "unsupported_scope"
            return stop_foreground_output(response_id)
        return outcome.reason

    return _cancel


def make_foreground_decision_callable(
    *, wait_for_lane: bool = False,
) -> Callable[[str, int, str, int], str]:
    """Build the injectable foreground-lane arbitration seam.

    ``(incumbent_group, incumbent_row_id, candidate_group, candidate_row_id)
    -> outcome``, where the outcome is ``"enqueue_after_drain"``,
    ``"supersede"`` or ``"decline"``.

    The policy is a pure L3 function with no clock and no IO, so there is
    nothing to bind but ``realtime.response.slow_results``; the builder exists
    because ``jarvis.decision`` and ``jarvis.surface`` are siblings
    (``.importlinter``) and the runtime is the only layer allowed to wire them.
    """
    return partial(decide_foreground, wait_for_lane=True) if wait_for_lane else decide_foreground


def _turn_has_run(event_log_path: Path, turn_id: str) -> bool:
    """Whether the turn already opened a response run (its answer is out or over)."""
    with contextlib.closing(open_runtime_event_log(event_log_path)) as conn:
        return conn.execute(
            "SELECT 1 FROM events WHERE type = 'response.started' "
            "AND json_extract(payload_json, '$.turn_id') = ? LIMIT 1",
            (turn_id,),
        ).fetchone() is not None


def make_turn_cancel_callable(runtime: JarvisRuntime) -> Callable[[str, str], str]:
    """Build the ``(turn_id, reason) -> outcome`` seam behind a turn stop.

    The surface's stop while Jarvis is still thinking: that turn's answer has
    no response id on the wire until it is whole (ADR 0108), so the surface
    names the turn and every run of it still open is cancelled as
    ``generation``. ``no_open_run`` when none is open: the answer is out, and
    the surface stops that by its response id.
    """
    cancel = make_response_cancel_callable(runtime)
    registry = runtime.response_runs
    event_log_path = runtime.runtime_paths.event_log

    def _cancel_turn(turn_id: str, reason: str) -> str:
        if registry is not None and reason == "user_stop":
            # Before the scan: a run opening meanwhile checks this on its own.
            registry.mark_turn_stopped(turn_id)
        runs = [] if registry is None else [
            run for run in registry.open_runs() if run.turn_id == turn_id
        ]
        outcomes = [cancel(run.response_id, "generation", reason) for run in runs]
        if outcomes:
            return "cancelled" if "cancelled" in outcomes else outcomes[0]
        if registry is None or reason != "user_stop" or _turn_has_run(event_log_path, turn_id):
            return "no_open_run"
        return "stopped_before_start"

    return _cancel_turn


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


# ADR 0053: a hold that is never released (a lost release) delays an answer by
# at most this; an utterance itself ends by max_utterance_s (30 s).
_ALLEN_TALKING_CEILING_S: Final[float] = 60.0
# How far back a voice sentence still counts as the first half of a split one.
_SUPERSEDE_WINDOW_S: Final[float] = 10.0


def make_supersede_unspoken_callable(
    runtime: JarvisRuntime,
    drop_unspoken: Callable[[frozenset[str]], frozenset[str]],
) -> Callable[[str], None]:
    """Build the ADR 0074 ``(accepted_turn_id) -> None`` seam.

    Called for a voice utterance just accepted, before its
    ``utterance.received`` is written. Every other turn of the last
    ``_SUPERSEDE_WINDOW_S`` that answers a voice sentence or a filled-in ask
    card (``clarify`` intent) whose run is still open, whose policy lets its
    generation be cancelled, and whose answer never reached the speaker is
    dropped: L5 discards its queued audio (``drop_unspoken``), then the run is
    cancelled with reason ``superseded``. Its run is open because no run
    completes while Allen is talking, so nothing of it reaches memory.db and
    the new turn's prompt folds its words in (ADR 0044). A card it put up and
    that still waits is rejected with rule ``superseded`` (ADR 0074), so the
    new turn asks afresh.

    A turn whose run has not opened yet (a line accepted under 0.5 s before
    this one) is remembered and cancelled the same way as its run registers.

    With ``slow_results`` on, a turn that already dispatched an action is
    skipped: it is working on a lookup, not half of a split sentence, and the
    new question gets its own answer while the lookup's follows. Live test
    2026-10-01: a weekday question 7 s after a Micron search was dispatched
    cancelled it, re-ran the search for 24 s and never answered the weekday.
    """
    cancel = make_response_cancel_callable(runtime)
    registry = runtime.response_runs
    event_log_path = runtime.runtime_paths.event_log

    def _supersede(turn_id: str) -> None:
        if registry is None:  # pragma: no cover - wiring pairs the two flags
            return
        runs = [
            run
            for run in registry.open_runs()
            if run.phase == "final"
            and run.turn_id != turn_id
            and run.interrupt_policy.generation_action == "cancel"
        ]
        any_open = bool(runs)
        since_ms = int((time.time() - _SUPERSEDE_WINDOW_S) * 1000)
        with contextlib.closing(
            open_runtime_event_log(event_log_path, deadline=time.monotonic() + 1.0),
        ) as conn:
            recent = {
                str(row[0])
                for row in conn.execute(
                    "SELECT json_extract(payload_json, '$.turn_id') FROM events "
                    "WHERE ts_epoch_ms >= ? AND (type = 'utterance.received' "
                    "OR (type = 'surface.user_intent' "
                    "AND json_extract(payload_json, '$.channel') = 'clarify'))",
                    (since_ms,),
                )
            }
            working: set[str] = set()
            if runtime.response_flags.slow_results:
                # The action names its turn in `correlation_json` (L4 stamps it
                # from the ActionRequest), so this is a durable read, and it is
                # after `since_ms` because the turn's utterance was.
                working = {
                    str(row[0])
                    for row in conn.execute(
                        "SELECT json_extract(correlation_json, '$.turn_id') FROM events "
                        "WHERE ts_epoch_ms >= ? AND type = 'action.dispatched'",
                        (since_ms,),
                    )
                }
                runs = [run for run in runs if run.turn_id not in working]
            # A line accepted within ~0.5 s of the one before it is swept before
            # that line's run opens (its request is built after its words): mark
            # every recent turn with no run yet, and `register` cancels it on open.
            registry.mark_turns_superseded(
                recent - {turn_id} - working
                - {run.turn_id for run in registry.open_runs()}
                - {
                    str(row[0])
                    for row in conn.execute(
                        "SELECT json_extract(correlation_json, '$.turn_id') FROM events "
                        "WHERE ts_epoch_ms >= ? AND type IN "
                        "('turn.ended', 'turn.failed', 'response.cancelled')",
                        (since_ms,),
                    )
                },
                for_s=_SUPERSEDE_WINDOW_S,
            )
            # ponytail: a run that passed the completion hold just before Allen
            # started talking can complete between this drop and its cancel; its
            # queued audio is then lost while its row stays. A millisecond window.
            dropped = (
                drop_unspoken(frozenset(run.turn_id for run in runs) & recent)
                if any_open
                else frozenset()
            )
            for run in runs:
                if run.turn_id not in dropped:
                    continue
                outcome = cancel(run.response_id, "generation", "superseded")
                LOGGER.info(
                    "unspoken answer superseded by %s: turn %s response %s -> %s",
                    turn_id, run.turn_id, run.response_id, outcome,
                )
                if outcome == "cancelled":
                    _withdraw_card(conn, run.turn_id, turn_id)

    return _supersede


def make_relation_supersede_callable(
    runtime: JarvisRuntime,
    drop_unspoken: Callable[[frozenset[str]], frozenset[str]],
    stop_playback: Callable[[], object],
) -> Callable[[str, str, str], str]:
    """Build the ADR 0139 ``(earlier_turn_id, turn_id, relation) -> outcome`` seam.

    Jev read a line as a supplement or correction of the one before it, confidently enough. The
    earlier turn's open answer run is cancelled as ``superseded`` (the new turn's prompt then
    folds the earlier words in, ADR 0044) and its card withdrawn, the way ADR 0074 does it for
    an accepted line, but aimed at that one turn and not bounded by the 10 s window. A turn
    that dispatched an action is left alone (it is working on a lookup). An answer that has
    begun playing is left to barge-in, except for a correction, which stops it and cancels the
    run. Outcomes: ``superseded``, ``stopped``, ``audible``, ``working``, ``no_open_run``,
    ``no_registry``.
    """
    cancel = make_response_cancel_callable(runtime)
    registry = runtime.response_runs
    event_log_path = runtime.runtime_paths.event_log

    def _relate(earlier: str, turn_id: str, relation: str) -> str:
        if registry is None:  # pragma: no cover - wiring pairs the two flags
            return "no_registry"
        runs = [
            run
            for run in registry.open_runs()
            if run.phase == "final"
            and run.turn_id == earlier
            and run.interrupt_policy.generation_action == "cancel"
        ]
        if not runs:
            return "no_open_run"
        with contextlib.closing(
            open_runtime_event_log(event_log_path, deadline=time.monotonic() + 1.0),
        ) as conn:
            working = conn.execute(
                "SELECT 1 FROM events WHERE type = 'action.dispatched' "
                "AND json_extract(correlation_json, '$.turn_id') = ? LIMIT 1",
                (earlier,),
            ).fetchone()
            if working is not None:
                return "working"
            unspoken = earlier in drop_unspoken(frozenset({earlier}))
            if not unspoken:
                if relation != "correction":
                    return "audible"
                stop_playback()
            for run in runs:
                outcome = cancel(run.response_id, "generation", "superseded")
                LOGGER.info(
                    "earlier answer %s by %s (%s): response %s -> %s",
                    "superseded" if unspoken else "stopped", turn_id, relation,
                    run.response_id, outcome,
                )
                if outcome == "cancelled":
                    _withdraw_card(conn, run.turn_id, turn_id)
        return "superseded" if unspoken else "stopped"

    return _relate


def _withdraw_card(conn: sqlite3.Connection, dropped_turn_id: str, turn_id: str) -> None:
    """ADR 0074: reject the card a dropped turn put up, if it still waits.

    Through the one-answer-per-ask primitive, so a button press racing this
    either lands first (and this finds the card answered) or finds it gone.
    """
    rows = conn.execute(
        "SELECT json_extract(payload_json, '$.confirmation_id') FROM events "
        "WHERE type = 'confirmation.requested' "
        "AND json_extract(correlation_json, '$.turn_id') = ? ORDER BY id DESC LIMIT 1",
        (dropped_turn_id,),
    ).fetchall()
    if not rows:
        return
    confirmation_id = str(rows[0][0])
    try:
        answer_confirmation_once(
            conn,
            confirmation_id=confirmation_id,
            accepted=False,
            utterance_raw="",
            grammar_rule_id="superseded",
            correlation={"turn_id": turn_id},
        )
    except ConfirmationRevalidationError:
        return  # already answered, or a newer ask replaced it
    LOGGER.info("card %s of dropped turn %s withdrawn", confirmation_id, dropped_turn_id)


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

    Trigger types: ``action.result_observed`` (defensive — sync
    tools emit this inline so decide() already absorbed it, but a
    late re-entry is accepted to keep the poll loop drainable), and
    ``action.timeout_assumed`` / ``action.failed`` / ``action.cancelled``
    (terminal failures emitted by the dispatcher or the supervisor
    sweep when the
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
            it. An action unknown to this process (crash recovery) is not
            filtered.
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


def _trigger_wait_budget(override: float | None) -> float:
    """Return this iteration's per-trigger wait budget, in seconds.

    An explicit caller value always wins. Scenarios and any surface that
    wants its own budget keep it; otherwise the conversational default, so
    an ordinary turn cannot pin one of ``max_concurrent_turns`` for long.
    """
    if override is not None:
        return override
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


_MAIL_GET: Final[str] = "mcp__gmail__gmail_get"


def _fetched_mail(conn: sqlite3.Connection, turn_id: str) -> tuple[str, str] | None:
    """ADR 0063: the message this turn read whole with ``gmail_get``, as a mail record.

    Only when the turn read exactly one message in full: a turn that read
    several is a list, answered in words. The record is headers, a blank
    line, then the body, and its id is the result's event uid.
    """
    gets = [
        event.payload
        for event in iter_events_for_turn(conn, turn_id, ("action.proposed",))
        if event.payload.get("tool_name") == _MAIL_GET
        and (event.payload.get("arguments") or {}).get("format", "full") == "full"
    ]
    if len({str((get.get("arguments") or {}).get("messageId")) for get in gets}) != 1:
        return None
    row = conn.execute(
        "SELECT event_uid, payload_json FROM events WHERE type = 'action.result_observed' "
        "AND json_extract(payload_json, '$.action_id') = ?",
        (gets[-1].get("action_id"),),
    ).fetchone()
    try:
        output = json.loads(json.loads(row[1])["tool_output"]) if row else {}
        message = json.loads(output.get("text", "")) if isinstance(output, dict) else None
    except (ValueError, KeyError, TypeError):
        return None  # a windowed (over-long) or failed answer shows no card
    if not isinstance(message, dict) or "error" in message or not message.get("id"):
        return None
    headers = [
        f"{name}: {message[key]}"
        for name, key in (
            ("From", "from"), ("To", "to"), ("Date", "date"), ("Subject", "subject"),
            ("Thread-Id", "threadId"), ("Gmail-Id", "id"),
        )
        if isinstance(message.get(key), str) and message[key]
    ]
    body = mail_body(str(message.get("body") or ""))
    return str(row[0]), "\n".join(headers) + "\n\n" + body


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
    2. Otherwise the decide() call paused on an action; poll the event
       log for the next ``action.result_observed`` /
       ``action.timeout_assumed`` / ``action.failed`` /
       ``action.cancelled`` row, re-enter decide() with that trigger,
       repeat.
    3. Release the turn's live action ids from the ``finally``.
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
    # memory.db: Allen's utterance lands before any routing (Tier 0 included).
    # The event uid is the record id, so a retried turn cannot double-write.
    # ADR-0016 D3: a Live delegation arrives with ``record_id`` because the
    # voice surface already wrote the row; that id is then what the prompt
    # note excludes, so the request appears once.
    memory = runtime.memory
    surface_record_id = user_intent_event.payload.get("record_id")
    memory_exclude_id = (
        surface_record_id
        if isinstance(surface_record_id, str) and surface_record_id
        else user_intent_event.event_uid
    )
    surface_wrote_row = memory_exclude_id != user_intent_event.event_uid
    # ADR 0062: a card's button is Allen's act, not his words; it writes no row.
    card_button = isinstance(user_intent_event.payload.get("confirmation_decision"), Mapping)
    if memory is not None and continuation is None and not surface_wrote_row and not card_button:
        audio_ref = user_intent_event.payload.get("audio_artifact_ref")
        append_record(
            memory.db_path,
            record_id=user_intent_event.event_uid,
            source="allen",
            text=str(user_intent_event.payload.get("transcript", "")),
            audio_path=audio_ref if isinstance(audio_ref, str) else None,
        )
    # One consistent read of memory.db for this turn's prompt: the history
    # block goes ahead of every per-turn note, the time line after them.
    memory_context = (
        render_context(
            memory.db_path, exclude_id=memory_exclude_id, since=runtime.session.history_since,
            recent=runtime.session.recent_records,
            context=runtime.session.context,
            raw_max_chars=runtime.session.context_raw_max_chars,
        )
        if memory is not None
        else None
    )
    # docs/plans/replay-as-sent-proposal.md: this turn's message is kept as
    # sent for later histories; a card's button wrote no row to keep it under.
    record_sent_message: Callable[[str], None] | None = None
    if memory is not None and runtime.session.replay_sent:
        record_sent_message = (
            (lambda _text: None)
            if card_button
            else partial(record_sent, memory.db_path, memory_exclude_id)
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
        _raise_if_cancelled("before the model")  # a stop that came before the run opened
        if continuation is not None:
            _raise_if_cancelled("before continuation")
            if continuation.failure is not None:
                raise continuation.failure  # noqa: TRY301 — resume through the same failure owner
            if run is not None:
                run.mark("generating")
        turn_system_prompt = runtime.system_prompt
        if runtime.plugin_connections is not None:
            turn_system_prompt += "\n\n" + runtime.plugin_connections.skills_prompt()
        connected_apps = (
            runtime.plugin_connections.connected_apps_line()
            if runtime.plugin_connections is not None
            else None
        )
        if runtime.session.replay_sent:
            # What every turn would repeat rides the system prompt, cached once,
            # instead of each replayed state block.
            if stream_route is not None and stream_route.context.route == "spoken":
                turn_system_prompt += "\n\n" + spoken_reply_rules(
                    structured=stream_route.structured,
                )
            if connected_apps is not None:
                turn_system_prompt += "\n\n" + connected_apps
                connected_apps = None
        decide_ctx = DecideContext(
            conn=runtime.conn,
            runtime_paths=runtime.runtime_paths,
            # ``ToolRegistry`` / ``ActionLifecycle`` satisfy the L3
            # Protocols structurally; the casts pin the boundary because
            # mypy's invariant generic stance over Protocol attribute
            # types treats the more-specific RawResult / LifecycleState
            # return types as a conflict.
            # ADR-0016 D6 — a Live delegation sees only read-only tools; the
            # shared registry is untouched, the view lives for this call.
            tool_registry=cast(
                "ToolRegistryLike",
                ReadOnlyToolRegistry(runtime.tool_registry)
                if (
                    user_intent_event.payload.get("channel") == "gpt_live"
                    or user_intent_event.payload.get("plugin_origin_channel") == "gpt_live"
                )
                else runtime.tool_registry,
            ),
            lifecycle=cast("LifecycleLike", runtime.lifecycle),
            # ADR-0008 D4 — with the Wave-4A flag on, this turn's provider
            # identity is the run's own immutable client, so two overlapping
            # runs can never read each other's last-call metadata. With the
            # flag off this is the same shared client object as before.
            llm_client=run.request_client if run is not None else runtime.llm_client,
            # The profile rides the system prompt: stable across turns, so it
            # sits in the cached prefix rather than in the per-turn context.
            system_prompt=(
                f"{turn_system_prompt.rstrip()}\n\n{memory_context.profile}"
                if memory_context is not None and memory_context.profile
                else turn_system_prompt
            ),
            tier0_table=runtime.tier0_table,
            # ADR-0011 D4 — resolve-on-propose. Built fresh per turn (a
            # trivial closure) rather than stored on JarvisRuntime: it
            # closes over `runtime.conn`, which the JarvisRuntime fields
            # above already carry, so there is nothing to cache.
            entity_resolver=_make_entity_resolver(runtime.conn, runtime.terminal_hub),
            # ADR-0012 D1 — write-target resolve-on-propose. Same
            # per-turn-closure rationale as `entity_resolver` above. A brain wires none:
            # the target would resolve on the wrong disk, and `write_file` is frozen anyway.
            write_entity_resolver=(
                None if runtime.terminal_hub is not None
                else _make_write_entity_resolver(runtime.conn)
            ),
            # ADR-0012 §3 D4/V2 — confirmation TTL, config-overridable
            # via `confirmation.ttl_ms` so the live burn can shorten it.
            confirmation_ttl_ms=_confirmation_ttl_ms(runtime.config),
            max_tool_iterations=_max_tool_iterations(runtime.config, user_intent_event),
            # ADR-0012 §3 D6 — answer-path grammar, threaded the same
            # way tier0_table is threaded.
            confirm_grammar_table=runtime.confirm_grammar_table,
            wave1_features=runtime.wave1_features,
            history=memory_context.history if memory_context is not None else (),
            time_note=memory_context.now if memory_context is not None else None,
            connected_apps=connected_apps,
            live_context=_live_lines(runtime.live_context),
            record_sent_message=record_sent_message,
            cancellation_checkpoint=run.check_cancelled if run is not None else None,
            request_admission=(
                partial(run.admit_request, runtime.conn) if run is not None else None
            ),
            routine_stream=stream_route,
            read_snapshot=_snapshot_reader(runtime),
            slow_results=runtime.response_flags.slow_results,
            surrogate_route=runtime.surrogate_route,
            oneshot=runtime.oneshot,
            # The same boot value as the system prompt's reply-language line.
            reply_language=str(runtime.config.get("reply_language", "follow")),
            service_tier=_voice_service_tier(runtime.config, user_intent_event),
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
        written_apart = False
        last_gate_event_uid: str | None = None

        while response_plan is None and iterations < max_iterations:
            _raise_if_cancelled("before a decide iteration")
            iterations += 1
            with bind_action_admission(
                run.admission_guard if run is not None else contextlib.nullcontext,
            ):
                result = decide(trigger_event, decide_ctx)
            if trigger_event.type in {
                "action.failed", "action.timeout_assumed", "action.cancelled",
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
                streamed = result.route in {"casual_or_explanatory", "spoken"}
                written_apart = result.written_apart
                last_gate_event_uid = result.last_gate_event_uid
                break

            # No final plan -> decide() paused on an action. Wait for the
            # next L4-side trigger event.
            # ADR-0009 D4 / F9 — the waiter is scoped to the actions THIS
            # turn dispatched. Read fresh each iteration: the action
            # that paused decide() was registered inside the call above.
            owned_action_ids = turn_action_ids(effective_turn_id)
            wait_timeout_s = _trigger_wait_budget(trigger_timeout_s)
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
            # ADR 0053: no answer completes while Allen's words are coming in,
            # so his next sentence can still drop it unwritten and unheard.
            registry = runtime.response_runs
            if registry is not None:
                hold_ends = time.monotonic() + _ALLEN_TALKING_CEILING_S
                while (
                    not registry.wait_completion_allowed(0.05)
                    and time.monotonic() < hold_ends
                ):
                    _raise_if_cancelled("while Allen was talking")
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
            written_apart=written_apart,
        )
        rendered = capture.getvalue()
        sys.stdout.write(rendered)
        sys.stdout.flush()
        collected_events.append(render_event)
        if memory is not None:
            # ADR 0063: the one message this turn read whole shows above the answer.
            mail = _fetched_mail(runtime.conn, effective_turn_id)
            if mail is not None:
                append_record(memory.db_path, record_id=mail[0], source="mail", text=mail[1])
            # The full answer text, not the spoken form (ADR 0040), which
            # lives only in the voice channel; the audit event's uid is the
            # record id. A written part that only adds to the spoken part
            # (ADR 0114) is not the full answer: both go in.
            answer = str(
                render_event.payload.get("document_text")
                or render_event.payload.get("text", ""),
            )
            if render_event.payload.get("written_apart") is True:
                answer = f"{render_event.payload.get('voice_text', '')}\n\n{answer}"
            append_record(
                memory.db_path,
                record_id=render_event.event_uid,
                source="jarvis",
                text=answer,
            )
            if (
                runtime.response_flags.prefix_warm
                and run is not None
                and run.request_client.provider == "openai"
                and user_intent_event.payload.get("channel") != "gpt_live"
            ):
                _warm_next_prefix(
                    runtime,
                    memory,
                    llm_client=run.request_client,
                    system_prompt=decide_ctx.system_prompt,
                    responses=(
                        (stream_route is not None and stream_route.context.route == "spoken")
                        or run.request_client.preset_snapshot.api == "responses"
                    ),
                )
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
            if run is not None and runtime.response_runs is not None:
                runtime.response_runs.unregister(run.response_id)
            release_turn_actions(effective_turn_id)


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
