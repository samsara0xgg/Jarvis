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
2. The turn lives in :mod:`jarvis.runtime.turn` (ADR 0371): this module
   holds only the public types it shares with bootstrap, :class:`JarvisRuntime`
   and :class:`RunTurnResult`.

Layer rules: ``jarvis.runtime`` may import everything below it. It is
imported by ``jarvis.cli`` only.
"""

from __future__ import annotations

import base64
import contextlib
import logging
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Mapping  # runtime use: isinstance in the config readers.
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from datetime import time as clock
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from jarvis.decision.attention import rule_judge_v1
from jarvis.decision.confirm_grammar import ConfirmGrammarConfigError, load_confirm_grammar
from jarvis.decision.cost_guard import CostRecorder
from jarvis.decision.jev_oneshot import TOOL_GROUPS, JevOneShot
from jarvis.decision.llm import LLMClient
from jarvis.decision.llm_anthropic import set_stale_files_handler
from jarvis.decision.llm_session import LLMSessionFactory
from jarvis.decision.mail_reply import MailReply
from jarvis.decision.moment import FIELDS as MOMENT_FIELDS
from jarvis.decision.policy import (
    PolicyConsistencyError,
    effective_policy,
    validate_requires_confirmation,
)
from jarvis.decision.pre_route import (
    ToolCueConfigError,
    ToolCueTable,
    load_tool_cues,
)
from jarvis.decision.response_run import (
    ResponseRunRegistry,
)
from jarvis.decision.surrogate_route import JevLog, SurrogateRoute, offered
from jarvis.decision.think_mode import ThinkMode, ThinkModeConfigError, load_think_mode
from jarvis.decision.tier0 import Tier0ConfigError, load_tier0_table, validate_tier0_table
from jarvis.decision.turn_end_asks import TurnEndAsks
from jarvis.decision.voice_words import VoiceWords
from jarvis.deployment import RuntimePaths, load_env_file
from jarvis.deployment.launchd import logs_dir
from jarvis.deployment.models import default_meaning_dir
from jarvis.deployment.night_power import MacPower
from jarvis.execution.archive_tool import build_archive_tool
from jarvis.execution.around import Surroundings
from jarvis.execution.conversation_tool import build_conversation_tool
from jarvis.execution.dashboard_tool import build_dashboard_tool
from jarvis.execution.geocode import Unplaced, geocode
from jarvis.execution.job_ledger_tool import (
    build_job_ledger_tool,
    build_record_application_tool,
)
from jarvis.execution.mail_tool import build_mail_tool
from jarvis.execution.mcp_oauth import DEFAULT_OAUTH_CALLBACK_PORT
from jarvis.execution.mcp_tools import DEFAULT_MCP_TIMEOUT_S, McpServers, is_oauth, stdio_env
from jarvis.execution.my_day_tool import build_my_day_tool
from jarvis.execution.path_resolver import (
    FileTargetsConfigError,
    configure_file_targets,
)
from jarvis.execution.tidy_tool import build_tidy_tool
from jarvis.execution.tools import (
    ActionLifecycle,
    Tool,
    ToolContext,
    ToolRegistry,
    VisionClient,
    build_default_registry,
)
from jarvis.execution.transit_tool import TransitOffers, build_leg_planner
from jarvis.execution.workers import Workers, make_worker_tools
from jarvis.runtime import audio_output, device_tools
from jarvis.runtime.about_item import AboutItems
from jarvis.runtime.bus_live import BusLive
from jarvis.runtime.claude_daily import ClaudeDailyIntake, ClaudeDailySettings
from jarvis.runtime.commitment_intake import CommitmentIntake, CommitmentSettings
from jarvis.runtime.config import (
    DailyReportSettings,
    DashboardSettings,
    LocateSettings,
    Role,
    RuntimeBootstrapError,
    RuntimeConfig,
    WebToolsSettings,
    WorkersSettings,
)
from jarvis.runtime.daily_report import (
    PLAN_SERVER,
    DailyReportService,
    DailySchedule,
    microsoft_plan,
    past_day_answer,
)
from jarvis.runtime.dashboard import (
    CLOSE,
    PAGES,
    SETTINGS_CATEGORIES,
    MailDrafts,
    ViewState,
)
from jarvis.runtime.decision_state import DecisionStateCache
from jarvis.runtime.deep_search import build_deep_search
from jarvis.runtime.departures import Departures
from jarvis.runtime.doing_run import DoingSchedule, DoingSettings
from jarvis.runtime.highlight_marks import HighlightMarks
from jarvis.runtime.home import Home, mail_summarizer, mail_text, weather_lookup
from jarvis.runtime.interview_reminders import InterviewSettings
from jarvis.runtime.job_mail import LINKEDIN_ALERTS, JobMail, JobMailSettings
from jarvis.runtime.ledger import LedgerContext, LedgerSettings
from jarvis.runtime.locate import HerePolicy, Locator
from jarvis.runtime.mail_codes import MailCodes, MailCodesSettings
from jarvis.runtime.moment import Moment, MomentSettings
from jarvis.runtime.night_run import NightRun, night_settings
from jarvis.runtime.now_block import calendar_line, doing_now_line, plan_line
from jarvis.runtime.picture_files import PictureFiles
from jarvis.runtime.picture_notes import PictureNotes
from jarvis.runtime.plugin_connections import PluginConnections
from jarvis.runtime.plugins import Plugins, load_plugins
from jarvis.runtime.projects import ProjectsService
from jarvis.runtime.push import Push
from jarvis.runtime.reminders import Reminders
from jarvis.runtime.rides import Rides
from jarvis.runtime.settings import REPLY_LINES, Settings
from jarvis.runtime.settings_owner import SettingsDrift, check_own, page_keys
from jarvis.runtime.setup import write_setting
from jarvis.runtime.things_ledger import ThingsLedger
from jarvis.runtime.things_mirror import ThingsMirror
from jarvis.runtime.turn_things import TurnThings, TurnThingsSettings
from jarvis.runtime.weatherkit import WeatherKit
from jarvis.runtime.whereabouts import Whereabouts
from jarvis.runtime.work_state import WorkStateService, build_analyst
from jarvis.shared import CallerPrincipal, Event, lang, llm_io_log
from jarvis.shared.clock import Clock, SystemClock, ZoneError
from jarvis.shared.lang import language_name
from jarvis.shared.pricing import load_pricing_table
from jarvis.shared.realtime import (
    Wave1FeatureFlags,
    Wave4ResponseFlags,
    new_response_id,
)
from jarvis.shared.realtime_trace import (
    configure_realtime_trace_jsonl,
)
from jarvis.state import device_reads, embedding, imports
from jarvis.state import places as place_state
from jarvis.state.attachments import ATTACHMENTS_DIRNAME, Attachments
from jarvis.state.committed_event_bus import CommittedEventBus
from jarvis.state.day_line import DaySources
from jarvis.state.event_log import (
    emit_event,
    open_event_log,
    open_runtime_event_log,
)
from jarvis.state.ledger import TURN_WAIT_S, LedgerSources, TerminalScreen
from jarvis.state.locate_wait import LocateWaits
from jarvis.state.memory_db import (
    MemorySettings,
    PlacesBlock,
    open_memory_db,
)
from jarvis.state.projects import parse_catalog
from jarvis.state.shares import shares_line
from jarvis.state.turn_overlap import any_turn_in_flight, turn_activity_since
from jarvis.state.voice_settings import VoiceSettings
from jarvis.state.voiceprint import VoiceprintBook
from jarvis.surface.ambient_sounds import AmbientSounds
from jarvis.surface.cli import (
    PreEmitTokenError,
    parse_response_channels,
)
from jarvis.surface.mac_location import read_mac_location
from jarvis.surface.terminal_events import BrainEvents
from jarvis.surface.terminal_link import TerminalHub
from jarvis.surface.voice_cues import VoiceCues

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import tzinfo

    from jarvis.decision import ResponsePlan
    from jarvis.decision.confirm_grammar import ConfirmGrammarTable
    from jarvis.decision.tier0 import Tier0Table
    from jarvis.runtime.work_state import LLMAnalyst
    from jarvis.shared.device_link import DeviceLink


LOGGER = logging.getLogger("jarvis.runtime")


# --- Defaults ---------------------------------------------------------------

# System prompt for the injected vision client (`_LLMVisionClient` below).
# Asking for the user's language means `screen_look`'s text observation
# slots straight into a Tier 0 reply without a translation hop.
_VISION_SYSTEM_PROMPT: str = (
    "You are a screen-reading assistant. Describe what is currently "
    "visible in the screenshot factually and concisely. Respond in {language}."
)


# --- ADR 0170 — the process's role ------------------------------------------


def _settings_drift(
    hub: TerminalHub | None, paths: RuntimePaths, config: Mapping[str, Any],
) -> SettingsDrift | None:
    """ADR 0227: a brain warns of device keys in its own files and hears its terminals' settings."""
    if hub is None:
        return None
    check_own("brain", paths.settings, paths.root)
    drift = SettingsDrift(paths.root, paths.settings, config)
    hub.on_settings = drift.on_report
    return drift


def _on_menu(table: Tier0Table, registry: ToolRegistry, role: Role) -> Tier0Table:
    """The Tier 0 rows the registry can serve.

    A brain holds only the tools its role builds; a row naming another would fail the boot's
    cross-check, so it is dropped (ADR 0170). The night-run tools are held, as proxies. A device
    tool no terminal has declared yet is held too (ADR 0225): its row waits on the menu.
    """
    if role != "brain":
        return table
    held = {tool.name for tool in (*registry.get_definitions(), *registry.device_catalogue())}
    return tuple(row for row in table if row.tool_name in held)


def _regex_router_tools(registry: ToolRegistry) -> tuple[Tool, ...]:
    """What Tier 0 rows may name: the menu's, and a brain's device tools no terminal declared yet.

    A row that waits for its terminal (ADR 0225) must not fail the boot's cross-check.
    """
    return tuple({
        tool.name: tool
        for tool in (*registry.for_caller(CallerPrincipal.REGEX_ROUTER),
                     *registry.device_catalogue())
        if CallerPrincipal.REGEX_ROUTER in tool.allowed_callers
    }.values())


def _no_audio_devices(_kind: str) -> list[str]:
    """The brain has no speaker or microphone to list."""
    return []


def _no_default_audio_device(_kind: str) -> None:
    """The brain has no default speaker or microphone to name."""


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
        config: The parsed, validated config (ADR 0370), with ``role`` (ADR 0170: the daemon
            starts no microphone, playback, power observer or device watcher in ``brain``) and
            the private addresses and Host names it also listens on (ADR 0170, 0207).
        runtime_paths: Bootstrapped L6 layout (root + event_log +
            artifacts_root + registry).
        conn: Open Event Log connection. The caller closes it when
            the process exits.
        tool_registry: L4 default registry.
        lifecycle: L4 per-process action lifecycle FSM.
        llm_client: L3 multi-provider LLM client.
        system_prompt: Rendered system prompt string (verbatim
            content of ``prompts/jarvis_v1.md``).
        llm_session_factory: mints each ResponseRun's immutable request client (ADR 0332).
        tier0_table: Spec §17 Tier 0 whitelist loaded from
            ``config/tier0_patterns.yaml``; empty tuple = Tier 0
            disabled.
        confirm_grammar_table: ADR-0012 §3 D6 exact-sentence yes/no
            grammar loaded from ``config/confirm_grammar.yaml``; empty
            tuple = the answer-path grammar hook disabled (same "off
            means inert" posture as an empty ``tier0_table``).
        terminal_hub: the connected terminals a brain's device-bound tool calls go to;
            ``None`` unless the role is ``brain``.
        phone_events: where a paired phone's events are appended (ADR 0197); ``None``
            unless the daemon is a brain or listens beyond loopback (ADR 0207).
        locator: asks the phone for a fresh fix (ADR 0281); ``None`` where ``phone_events`` is.
        attachments: the store of files a phone sends with a turn or a share (ADR 0211).
        picture_notes: the worker that writes a note for each picture after its turn (ADR 0223).
    """

    config: RuntimeConfig
    runtime_paths: RuntimePaths
    conn: sqlite3.Connection
    tool_registry: ToolRegistry
    lifecycle: ActionLifecycle
    llm_client: LLMClient
    system_prompt: str
    llm_session_factory: LLMSessionFactory
    tier0_table: Tier0Table = ()
    confirm_grammar_table: ConfirmGrammarTable = ()
    # ADR 0321: a fresh client on the ``confirmation.words_preset`` for each reading of his
    # words against a waiting card; ``None`` where that preset is missing.
    confirmation_words: Callable[[], LLMClient] | None = None
    wave1_features: Wave1FeatureFlags = field(default_factory=Wave1FeatureFlags)
    response_flags: Wave4ResponseFlags = field(default_factory=Wave4ResponseFlags)
    response_runs: ResponseRunRegistry | None = None
    committed_event_bus: CommittedEventBus | None = None
    # ADR 0164 — the one decision-snapshot cache every turn thread shares by reference
    # (internally locked, like ``committed_event_bus``); None folds the whole log per read.
    decision_state: DecisionStateCache | None = None
    # ADR 0286: the owner's voice, per microphone: scores what a terminal, a phone or this
    # machine's own microphone heard. None = hand-assembled.
    voiceprints: VoiceprintBook | None = None
    # memory.db wiring (``memory:`` config block). None = no memory store:
    # hand-assembled runtimes write no records and inject no note.
    memory: MemorySettings | None = None
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
    # ADR 0180: re-analyse in the background when Allen changes what he is doing
    # (``work_state.auto``, off by default: it sends screen material unasked).
    work_state_auto: bool = False
    # ADR 0037: the project view and its sorting job. None = no `projects` list.
    projects: ProjectsService | None = None
    # ADR 0051: the companion home's Today, mail and brief reads. None = hand-assembled.
    home: Home | None = None
    # ADR 0176: what the Dashboard shows (``dashboard.view.enabled``). None = off.
    view: ViewState | None = None
    # ADR 0302, 0310: the writer of the doing lane, on its clock and when he asks. None = off.
    doing: DoingSchedule | None = None
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
    # ADR 0266: verification codes in new mail go to the Mac clipboard. None = off.
    mail_codes: MailCodes | None = None
    # ADR 0274: the poller that reads his mail for things he has to go to. None = off.
    commitments: CommitmentIntake | None = None
    # ADR 0299: the ledger of his things: its carry-in and its rings pass. None = hand-assembled.
    things: ThingsLedger | None = None
    # ADR 0299: Outlook and To Do read into the ledger. None = hand-assembled.
    things_mirror: ThingsMirror | None = None
    # ADR 0300: the pass that reads each record of his, once answered, into the ledger. None = off.
    turn_things: TurnThings | None = None
    # ADR 0295, 0303: the poller of the nightly letter on his Claude chats (Gmail part off unless
    # enabled) and reader of the Mac's day report on his Claude Code work. None = hand-assembled.
    claude_daily: ClaudeDailyIntake | None = None
    # ADR 0161: the situation TimeSink last recorded, read on demand. None = off.
    moment: Moment | None = None
    # ADR 0179: the clock that fires the owner's reminders. None = hand-assembled.
    reminders: Reminders | None = None
    # ADR 0210: the pushes to his paired phone; pushes nothing until a key is configured.
    push: Push | None = None
    # ADR 0224: the bus ride follower; its loop runs while there is a watch or a ride.
    rides: Rides | None = None
    # ADR 0217: the device each turn came from, the devices there are, where the phone put him.
    whereabouts: Whereabouts | None = None
    # ADR 0298: the record of the day-line item his phone has open. None = hand-assembled.
    about_items: AboutItems | None = None
    # ADR 0288: one bus leg for a background job, given the job's own context. None: no maps key.
    leg_planner: Callable[[ToolContext], Callable[..., dict[str, Any]] | None] | None = None
    # ADR 0052: the Settings page's file. None = hand-assembled.
    settings: Settings | None = None
    # ADR 0093: the night run; the daemon ticks it. None = hand-assembled.
    night: NightRun | None = None
    # ADR 0174: her voice volume and speed; the TTS provider reads it, `set_voice` writes it.
    voice_settings: VoiceSettings | None = None
    # ADR 0101: the day before's report, written once a day. None = `daily_report.at` unset.
    daily_schedule: DailySchedule | None = None
    # ADR 0201: the day, week and job-hunt blocks of the prompt, from his own data. None = off
    # (no ``ledger:`` block, or no memory store).
    ledger: LedgerContext | None = None
    # ADR 0234: the ``[Places he keeps]`` block after ``[About the user]``. None = hand-assembled.
    places_block: PlacesBlock | None = None
    # ADR 0156: where sound would come out now, for the notices' ``audio_private`` and the job
    # mail; on a brain (ADR 0170) the output of the terminal that plays.
    sound_output: Callable[..., dict[str, Any]] = audio_output.current_output
    # ADR 0170: where a brain's device-bound tool calls go. None unless ``role`` is ``brain``.
    terminal_hub: TerminalHub | None = None
    # ADR 0227: what a brain's terminals report of their own settings files, and the owner's answer.
    settings_drift: SettingsDrift | None = None
    # ADR 0197, 0207: where a paired phone's events are appended.
    phone_events: BrainEvents | None = None
    # ADR 0281: asks his phone for a fresh fix for `where_am_i`; None where no phone can answer.
    locator: Locator | None = None
    # ADR 0211: the pictures and text files a phone sends with a turn or a share.
    attachments: Attachments | None = None
    # ADR 0223: writes the note of each picture after its turn. None without a vision preset.
    picture_notes: PictureNotes | None = None
    # ADR 0255: the Files API copies of the pictures in history.
    picture_files: PictureFiles | None = None
    # ADR 0322, 0333: the one clock the tools and the services take the time and their waits from.
    clock: Clock = field(default_factory=SystemClock)


@dataclass(frozen=True)
class RunTurnResult:
    """Outcome of :func:`~jarvis.runtime.turn.drive.run_turn` for one conversation turn.

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


# --- bootstrap_runtime_app --------------------------------------------------


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


def _confirmation_words_client(config: RuntimeConfig) -> Callable[[], LLMClient] | None:
    """ADR 0321: the client that reads his words against a waiting card, one per reading.

    It runs on ``confirmation.words_preset`` with ``confirmation.words_timeout_s``: one attempt,
    no retry, so a slow provider leaves the card waiting for its button. A missing preset reads
    nothing: such words run as an ordinary turn.
    """
    name = config.confirmation.words_preset
    llm_block = config.raw.get("llm")
    presets = llm_block.get("presets") if isinstance(llm_block, Mapping) else None
    preset = presets.get(name) if isinstance(presets, Mapping) else None
    if not isinstance(preset, Mapping):
        LOGGER.warning("confirmation: llm.presets.%s missing; no words read against a card", name)
        return None
    factory = LLMSessionFactory(
        {
            "provider": "openai",
            "presets": {name: dict(preset)},
            "default_preset": name,
            "timeout_s": config.confirmation.words_timeout_s,
            "max_retries": 0,
        },
    )
    snapshot = factory.snapshot()
    return lambda: factory.create(snapshot, response_id=new_response_id())


def _jev_log(config: Mapping[str, Any], root: Path) -> JevLog | None:
    """``jev_log`` (ADR 0128): on unless ``enabled: false``; the file is made at the first line."""
    block = config.get("jev_log")
    if isinstance(block, Mapping) and block.get("enabled") is False:
        return None
    return JevLog(root / "jev" / "decisions.jsonl")


def _surrogate_route(
    config: Mapping[str, Any], config_path: Path, clock: Clock, log: JevLog | None = None,
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
        model=model.strip(), min_confidence=float(bar), timeout_ms=timeout, clock=clock,
        parallel=block.get("parallel") is True, zdr=block.get("zdr") is not False,
        log=log,
    )


def _think_mode(llm_config: Mapping[str, Any], config_path: Path) -> ThinkMode | None:
    """``llm.think`` (ADR 0108); a broken block stops boot like a broken tool-cue table."""
    try:
        return load_think_mode(llm_config)
    except ThinkModeConfigError as exc:
        msg = f"runtime: {config_path} {exc}"
        raise RuntimeBootstrapError(msg) from exc


def _ledger(
    config: RuntimeConfig, memory: MemorySettings | None, event_log: Path,
    device: DeviceLink | None = None, attachments: Path | None = None,
) -> LedgerContext | None:
    """ADR 0201: the one ledger both prompt renders ask; None without a ``ledger:`` block.

    On a brain (``device``, ADR 0170) its TimeSink is the terminal's. ``attachments`` is where his
    pictures are kept, which the day line of ADR 0235 reads.
    """
    settings = LedgerSettings.from_config(config.raw.get("ledger"), config.reply_language)
    if settings is None or memory is None:
        return None
    sources = LedgerSources(
        memory.db_path, event_log, config.observer.timesink_db_path, config.zone,
        None if device is None else TerminalScreen(device), attachments,
    )
    return LedgerContext(sources, settings)


def _highlight_marks(
    config: RuntimeConfig, memory: MemorySettings, paths: RuntimePaths,
    device: DeviceLink | None,
) -> HighlightMarks:
    """ADR 0235: what `mark_highlight` does, over the same day line `GET /inherent/day` folds."""
    return HighlightMarks(
        paths.event_log,
        DaySources(
            config.zone, config.observer.timesink_db_path, memory.db_path,
            None if device is None else TerminalScreen(device, wait_s=TURN_WAIT_S),
            paths.artifacts_root / ATTACHMENTS_DIRNAME,
            shipped=paths.root / imports.IMPORTS_DIRNAME,
        ),
    )


def _doing_schedule(
    config: RuntimeConfig, memory: MemorySettings, paths: RuntimePaths, device: DeviceLink | None,
) -> DoingSchedule | None:
    """ADR 0302, 0310: the writer of the doing lane, over the day line `GET /inherent/day` folds."""
    settings = DoingSettings.from_config(config.raw.get("doing"))
    if settings is None:
        return None
    llm_config = config.raw.get("llm")
    return DoingSchedule(
        settings=settings,
        sources=DaySources(
            config.zone, config.observer.timesink_db_path, memory.db_path,
            None if device is None else TerminalScreen(device, wait_s=TURN_WAIT_S),
            paths.artifacts_root / ATTACHMENTS_DIRNAME,
            shipped=paths.root / imports.IMPORTS_DIRNAME,
        ),
        memory_db=memory.db_path,
        event_log_path=paths.event_log,
        llm_config=llm_config if isinstance(llm_config, Mapping) else {},
        pricing_table=load_pricing_table(config.pricing_path),
    )


def _about_items(
    config: RuntimeConfig, memory: MemorySettings, paths: RuntimePaths,
) -> AboutItems:
    """ADR 0298: the day line `GET /inherent/day` folds, from local stores only."""
    return AboutItems(
        paths.event_log,
        DaySources(
            config.zone, config.observer.timesink_db_path, memory.db_path,
            attachments=paths.artifacts_root / ATTACHMENTS_DIRNAME,
            shipped=paths.root / imports.IMPORTS_DIRNAME,
        ),
    )


_DAILY_REPORT_TIMEOUT_S: Final[float] = 900.0
"""ADR 0028 — a whole day served whole: the draft call read 300k tokens in 175 s on v4-pro
(2026-09-12), and a retry after a timeout would resend it all."""
_OUTPUT_TIMEOUT_S: Final[float] = 5.0
"""How long a brain waits for its terminal to say where sound would come out: its probe takes
at most 3 s, and a slow answer is a no (ADR 0156)."""


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


def _locator(settings: LocateSettings, waits: LocateWaits, clock: Clock) -> Locator:
    """``locate`` — how long Here waits for his phone's fresh fix (ADR 0281)."""
    return Locator(
        waits, clock, socket_wait_s=settings.socket_wait_s, push_wait_s=settings.push_wait_s,
    )


def _live_departure(live: BusLive) -> Callable[[str, tuple[float, float], int], int | None]:
    """BC Transit's live departure (epoch ms) of a route at a stop, for the `bus` tool."""
    def departure(route: str, stop: tuple[float, float], around_ms: int) -> int | None:
        found = live.departure(route, stop, around_ms)
        return found[1] if found else None

    return departure


def _here_location(role: str) -> Callable[[], dict[str, Any]] | None:
    """This Mac's location for `bus` and `where_am_i`, read on demand (ADR 0194).

    None in the `brain` role, which has no Mac to read, and off macOS. Every role also gets the
    phone's last report (`_phone_here`, ADR 0198); which one answers is decided per turn
    (ADR 0212), and a brain's only reading is the phone's.
    """
    if role == "brain" or sys.platform != "darwin":
        return None
    return lambda: asdict(read_mac_location())


def _locate_places(db_path: Path, api_key: str) -> None:
    """ADR 0237: give each place kept as an address the point Google gives it, once.

    A place is asked about until a geocoder change touches it; one that fails stays an address
    and is asked again at the next boot. The log names the place, never its address or the key.
    """
    for one in place_state.unlocated(db_path):
        if one.address is None:
            continue
        try:
            lat, lng = geocode(one.address, api_key)
            written = place_state.locate(db_path, one.id, lat=lat, lng=lng)
        except (Unplaced, LookupError, ValueError, sqlite3.Error) as exc:
            LOGGER.warning("place %r not located: %s", one.name, exc)
            continue
        if written.change_id is not None:
            LOGGER.info("place %r located from its address", one.name)


def _start_locating_places(db_path: Path, api_key: str) -> None:
    """Run :func:`_locate_places` in the background when there is a key and a place to locate."""
    if api_key and place_state.unlocated(db_path):
        threading.Thread(
            target=_locate_places, args=(db_path, api_key), name="jarvis-place-locate", daemon=True,
        ).start()


def _dashboard_state(
    settings: DashboardSettings,
) -> tuple[ViewState | None, ViewState | None, MailDrafts | None]:
    """The Dashboard's view, the view the mail page reads, and the mail drafts (None = off).

    The mail page needs the view (ADR 0176: its open letter is the view's open item); with
    ``dashboard.mail.enabled`` alone it stays off.
    """
    view = ViewState() if settings.view else None
    mail_view = view if settings.mail else None
    return view, mail_view, None if mail_view is None else MailDrafts(mail_view)


def _register_dashboard_tool(registry: ToolRegistry, view: ViewState | None) -> None:
    """``show_on_dashboard`` (ADR 0176), registered only with ``dashboard.view.enabled``."""
    for tool in build_dashboard_tool(
        (*PAGES, CLOSE), None if view is None else view.present, SETTINGS_CATEGORIES
    ):
        registry.register(tool)


def _shares_line(event_log_path: Path) -> str | None:
    """ADR 0211: the line naming what Allen last saved from other apps, folded from the log."""
    with contextlib.closing(open_runtime_event_log(event_log_path)) as conn:
        return shares_line(conn)


def _places_block(
    memory_db: Path, event_log: Path, zone: tzinfo, now: datetime,
) -> str:
    """ADR 0234: the ``[Places he keeps]`` block of the system prompt; never fails the turn."""
    try:
        with contextlib.closing(open_runtime_event_log(event_log)) as conn:
            return place_state.prompt_block(memory_db, conn, int(now.timestamp() * 1000), zone)
    except Exception:  # a prompt block must never fail a turn.
        LOGGER.exception("places: the prompt block failed, left out")
        return ""


def _mail_reply(
    config: Mapping[str, Any], config_path: Path, clock: Clock, log: JevLog | None = None,
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
        model=model.strip(), min_confidence=1.0, timeout_ms=timeout, clock=clock, log=log,
    )
    rating = block.get("importance")  # ADR 0141
    return MailReply(
        route, float(bars[1]), float(bars[0]), float(bars[2]),
        importance=isinstance(rating, Mapping) and rating.get("enabled") is True,
    )


def _moment(
    config: Mapping[str, Any], config_path: Path, db_path: Path, device: DeviceLink | None = None,
    timesink_path: Path | None = None,
) -> Moment | None:
    """``moment`` (ADR 0161): off unless enabled; bad values stop boot.

    It reads TimeSink only through ``observer.timesink``: with that off every fact is unknown
    and nothing is held. On a brain (``device``) it is the terminal's TimeSink that is read.
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
    return Moment(MomentSettings(dict(fields)), timesink_path, db_path, device)


def _job_mail(  # noqa: PLR0913 - the config, its collaborators, the clock and the moment
    config: Mapping[str, Any], config_path: Path, log: JevLog | None,
    connections: PluginConnections, db_path: Path, clock: Clock,
    moment: Moment | None = None, device: DeviceLink | None = None,
    event_log: Path | None = None, timesink_path: Path | None = None,
) -> JobMail | None:
    """``job_mail`` (ADR 0155): off unless enabled; bad values stop boot.

    ``clock`` is the runtime's; its zone is the owner's: a time a letter gives with no zone of
    its own is his clock there.
    """
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
        interview_reminders=_interview_reminders(block.get("interview_reminders"), config_path),
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
    route = SurrogateRoute(
        model=model.strip(), min_confidence=1.0, timeout_ms=timeout, clock=clock, log=log,
    )
    return JobMail(
        settings, route, connections, db_path, rule_judge_v1, clock=clock,
        moment=moment, timesink_path=timesink_path, device=device, event_log=event_log,
    )


def _mail_codes(
    config: Mapping[str, Any], config_path: Path, connections: PluginConnections,
    device: DeviceLink | None,
) -> MailCodes | None:
    """``mail_codes`` (ADR 0266): off unless enabled; bad values stop boot."""
    block = config.get("mail_codes")
    if not isinstance(block, Mapping) or block.get("enabled") is not True:
        return None
    values = {}
    for key, low, high in (("poll_s", 1, None), ("window_min", 1, 60)):
        value = block.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float) or value < low or (
            high is not None and value > high
        ):
            msg = f"runtime: {config_path} mail_codes.{key} must be a number of at least {low}" + (
                "" if high is None else f" and at most {high}"
            )
            raise RuntimeBootstrapError(msg)
        values[key] = value
    return MailCodes(
        MailCodesSettings(float(values["poll_s"]), int(values["window_min"])), connections, device,
    )


def _commitments(  # noqa: PLR0913 - the config and the poller's collaborators
    config: Mapping[str, Any], config_path: Path, connections: PluginConnections, db_path: Path,
    event_log: Path, analyst_for: Callable[[str], LLMAnalyst | None],
) -> CommitmentIntake | None:
    """``commitments`` (ADR 0274): off unless enabled; bad values stop boot."""
    block = config.get("commitments")
    if not isinstance(block, Mapping) or block.get("enabled") is not True:
        return None
    where = f"runtime: {config_path} commitments"
    numbers = {}
    for key, low, high in (
        ("poll_s", 1, None), ("max_mails_per_cycle", 1, 200), ("remind_before_min", 1, 1440),
    ):
        value = block.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float) or value < low or (
            high is not None and value > high
        ):
            msg = f"{where}.{key} must be a number of at least {low}" + (
                "" if high is None else f" and at most {high}"
            )
            raise RuntimeBootstrapError(msg)
        numbers[key] = value
    since = block.get("backfill_since")
    if not isinstance(since, date) or isinstance(since, datetime):
        msg = f"{where}.backfill_since must be a date (YYYY-MM-DD)"
        raise RuntimeBootstrapError(msg)
    preset = block.get("preset")
    if not isinstance(preset, str) or not preset.strip():
        msg = f"{where}.preset must be the name of an llm preset"
        raise RuntimeBootstrapError(msg)
    settings = CommitmentSettings(
        float(numbers["poll_s"]), since, int(numbers["max_mails_per_cycle"]), preset.strip(),
        int(numbers["remind_before_min"]),
    )
    return CommitmentIntake(
        settings, connections, db_path, event_log,
        mail_summarizer(analyst_for(settings.preset), event_log),
    )


_BACKFILL_MAX_DAYS: Final[int] = 60


def _turn_things(  # noqa: PLR0913 - the config and the pass's collaborators
    config: Mapping[str, Any], config_path: Path, db_path: Path, event_log: Path, zone: tzinfo,
    analyst_for: Callable[[str], LLMAnalyst | None],
) -> TurnThings | None:
    """``turn_things`` (ADR 0300): off unless enabled; bad values stop boot."""
    block = config.get("turn_things")
    if not isinstance(block, Mapping) or block.get("enabled") is not True:
        return None
    where = f"runtime: {config_path} turn_things"
    days = block.get("backfill_days")
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= _BACKFILL_MAX_DAYS:
        msg = f"{where}.backfill_days must be a whole number of days from 1 to {_BACKFILL_MAX_DAYS}"
        raise RuntimeBootstrapError(msg)
    preset = block.get("preset")
    if not isinstance(preset, str) or not preset.strip():
        msg = f"{where}.preset must be the name of an llm preset"
        raise RuntimeBootstrapError(msg)
    settings = TurnThingsSettings(days, preset.strip())
    return TurnThings(
        settings, db_path, event_log, mail_summarizer(analyst_for(settings.preset), event_log),
        zone,
    )


def _things_ledger(
    event_log: Path, db_path: Path, zone: tzinfo, remind_before_min: int,
) -> ThingsLedger:
    """The ledger of his things (ADR 0299), carried in once now; it runs with the poller off too.

    A carried-in commitment rings ``commitments.remind_before_min`` ahead, else 60 minutes. A
    carry-in that fails is logged and tried again by the daemon's rings pass.
    """
    ledger = ThingsLedger(event_log, db_path, zone, remind_before_min)
    try:
        ledger.carry_in()
    except Exception:
        LOGGER.exception("things: the carry-in failed; the daemon tries it again")
    return ledger


def _claude_daily(
    config: Mapping[str, Any], config_path: Path, connections: PluginConnections, db_path: Path,
    root: Path,
) -> ClaudeDailyIntake:
    """``claude_daily`` (ADR 0295): Gmail letters off unless enabled; bad values stop boot.

    Always built (ADR 0303): the Mac's day report is read whether or not Gmail letters are on.
    """
    block = config.get("claude_daily")
    if not isinstance(block, Mapping) or block.get("enabled") is not True:
        # Gmail letters are off, but the Mac's day report of his Claude Code work (ADR 0303) is
        # still read from the imports archive: an intake that never touches Gmail.
        return ClaudeDailyIntake.code_reports_only(
            connections, db_path, root / imports.IMPORTS_DIRNAME,
        )
    where = f"runtime: {config_path} claude_daily"
    numbers = {}
    for key, low, high in (("poll_s", 60, None), ("max_letters_per_cycle", 1, 200)):
        value = block.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float) or value < low or (
            high is not None and value > high
        ):
            msg = f"{where}.{key} must be a number of at least {low}" + (
                "" if high is None else f" and at most {high}"
            )
            raise RuntimeBootstrapError(msg)
        numbers[key] = value
    since = block.get("backfill_since")
    if not isinstance(since, date) or isinstance(since, datetime):
        msg = f"{where}.backfill_since must be a date (YYYY-MM-DD)"
        raise RuntimeBootstrapError(msg)
    settings = ClaudeDailySettings(
        float(numbers["poll_s"]), since, int(numbers["max_letters_per_cycle"]),
    )
    return ClaudeDailyIntake(settings, connections, db_path, root / imports.IMPORTS_DIRNAME)


def _sound_output(
    hub: TerminalHub | None, job_mail: JobMail | None, reminders: Reminders,
) -> Callable[..., dict[str, Any]]:
    """ADR 0156: where sound would come out now, given to everything that asks.

    One machine plays on its own output. A brain (ADR 0170) asks the terminal that plays what she
    says unprompted.
    """
    output = (
        audio_output.current_output if hub is None
        else audio_output.TerminalOutput(partial(hub.call_player, timeout_s=_OUTPUT_TIMEOUT_S))
    )
    reminders.output = output
    if job_mail is not None:
        job_mail.output = output
    return output


def _interview_reminders(block: object, config_path: Path) -> InterviewSettings | None:
    """``job_mail.interview_reminders`` (ADR 0186): None while off; bad values stop boot."""
    where = f"runtime: {config_path} job_mail.interview_reminders"
    if not isinstance(block, Mapping) or not isinstance(block.get("enabled"), bool):
        msg = f"{where}.enabled must be true or false"
        raise RuntimeBootstrapError(msg)
    if not block["enabled"]:
        return None
    raw = block.get("evening_at")
    try:
        evening = clock.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        evening = None
    if evening is None or evening.tzinfo:
        msg = f'{where}.evening_at must be a local time such as "20:00"'
        raise RuntimeBootstrapError(msg)
    minutes = block.get("before_min")
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 1440:  # noqa: PLR2004 - a day
        msg = f"{where}.before_min must be a whole number of minutes from 1 to 1440"
        raise RuntimeBootstrapError(msg)
    return InterviewSettings(evening, minutes)


def _register_job_ledger(registry: ToolRegistry, job_mail: JobMail | None) -> None:
    """``job_ledger`` reads what ``GET /inherent/jobs`` serves, ``record_application`` adds a row.

    Both are absent while job mail is off.
    """
    for tool in (
        *build_job_ledger_tool(None if job_mail is None else job_mail.ledger),
        *build_record_application_tool(None if job_mail is None else job_mail.add_application),
    ):
        registry.register(tool)


def _register_mail(registry: ToolRegistry, home: Home) -> None:
    """``mail`` (ADR 0242): the overview ``GET /inherent/mail`` serves, a search, one message."""
    for tool in build_mail_tool(home.mail, home.gmail, mail_text):
        registry.register(tool)


def _register_conversation(registry: ToolRegistry, memory_db: Path, runtime_root: Path) -> None:
    """``conversation`` (ADR 0243): his words and hers, searched, summarized by day, opened.

    ADR 0260: the search also goes by meaning, with the model under the runtime root.
    """
    embedder = embedding.shared(default_meaning_dir(runtime_root))
    for tool in build_conversation_tool(memory_db, embedder=embedder):
        registry.register(tool)


def _register_archive(registry: ToolRegistry, memory_db: Path, runtime_root: Path) -> None:
    """``archive`` (ADR 0259): the payments and Claude conversations he handed over."""
    embedder = embedding.shared(default_meaning_dir(runtime_root))
    for tool in build_archive_tool(memory_db, embedder=embedder):
        registry.register(tool)


def _register_tidy(registry: ToolRegistry, doing: DoingSchedule | None) -> None:
    """``tidy_day`` (ADR 0310): he asks her to tidy today's timeline now."""
    for tool in build_tidy_tool(None if doing is None else doing.tidy):
        registry.register(tool)


def _register_my_day(registry: ToolRegistry, sources: LedgerSources) -> None:
    """``my_day`` (ADR 0244): today and what he does now, a day, a span, one project."""
    for tool in build_my_day_tool(sources):
        registry.register(tool)


@dataclass(frozen=True)
class Catalog:
    """What :func:`register_catalog` hands back besides the registry."""

    registry: ToolRegistry
    workers: Workers | None
    plugins: PluginConnections
    job_mail: JobMail | None
    home: Home


def register_catalog(  # noqa: PLR0913 - one call site per caller, the dependencies are the point
    *,
    build_registry: Callable[[], ToolRegistry],
    offer_devices: Callable[[ToolRegistry], None],
    paths: RuntimePaths,
    workers: WorkersSettings,
    view: ViewState | None,
    make_plugins: Callable[[ToolRegistry], PluginConnections],
    make_late: Callable[[PluginConnections], tuple[JobMail | None, Home]],
    memory_db: Path,
    doing: DoingSchedule | None,
    ledger: LedgerSources,
) -> Catalog:
    """Every tool the daemon serves, registered in its order from dependencies already built.

    The one place that order lives. The daemon calls it with real dependencies, the tool exam
    with stand-ins; the menu's order is part of the cached prompt prefix, so it must not differ.
    ``make_late`` builds what needs the plugin connections initialized: job mail and the home.
    """
    registry = build_registry()
    # ADR 0225: a brain's menu offers what the paired terminals declared, before any is here.
    offer_devices(registry)
    started = _register_workers(registry, paths, workers)
    _register_dashboard_tool(registry, view)
    plugins = make_plugins(registry)
    plugins.initialize()
    job_mail, home = make_late(plugins)
    _register_job_ledger(registry, job_mail)
    _register_mail(registry, home)
    _register_conversation(registry, memory_db, paths.root)
    _register_archive(registry, memory_db, paths.root)
    _register_tidy(registry, doing)
    _register_my_day(registry, ledger)
    plugins.refresh_search()  # ADR 0247: every built-in tool is registered now
    return Catalog(registry, started, plugins, job_mail, home)


def _turn_end_asks(
    config: Mapping[str, Any], config_path: Path, clock: Clock, log: JevLog | None = None,
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
        model=model.strip(), min_confidence=1.0, timeout_ms=timeout, clock=clock, log=log,
    )
    return TurnEndAsks(route, float(bar), float(question_bar))


def _voice_words(
    config: Mapping[str, Any], config_path: Path, clock: Clock, log: JevLog | None = None,
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
        model=model.strip(), min_confidence=1.0, timeout_ms=timeout, clock=clock, log=log,
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


def _jev_oneshot(  # noqa: PLR0913 - the config, the clock, the dataset, the table, the emitter
    config: Mapping[str, Any],
    config_path: Path,
    clock: Clock,
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
        model=model.strip(), min_confidence=1.0, timeout_ms=timeout, clock=clock,
        zdr=block.get("zdr") is not False, log=log,
    )
    return JevOneShot(
        route, offered(tier0_table),
        words_at=words_at, words_timeout_ms=words_timeout or 800, max_chars=words_chars or 24,
        relation_at=relation_at, window_s=float(window),
        tool_at=tool_at, tool_groups=frozenset(groups), emit=emit,
    )


def _codex_sessions_path(settings: DailyReportSettings) -> Path | None:
    """Codex's own session directory, only when ``daily_report.codex_sessions`` opts in."""
    if not settings.codex_sessions:
        return None
    root = Path.home() / ".codex" / "sessions"
    return root if root.is_dir() else None


def _reads_connected(hub: TerminalHub) -> bool:
    """Whether a connected terminal declared the TimeSink and git reads (ADR 0170)."""
    return any(tools >= device_reads.DEVICE_READS for _, tools in hub.connected())


def _daily_schedule(
    service: DailyReportService, event_log: Path, config: RuntimeConfig,
    hub: TerminalHub | None = None,
) -> DailySchedule | None:
    """``daily_report.at`` — when the daemon writes yesterday's report (ADR 0101); unset: off."""
    if config.daily_report.at is None:
        return None
    return DailySchedule(
        service, event_log_path=event_log, at=config.daily_report.at, zone=config.zone,
        device_ready=None if hub is None else partial(_reads_connected, hub),
    )


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


def _install_open_path(block: Mapping[str, Any]) -> None:
    """Hand `tools.open_path` to L4's resolver; malformed bookmarks fail the boot."""
    try:
        configure_file_targets(block)
    except FileTargetsConfigError as exc:
        msg = f"runtime: tools.open_path invalid: {exc}"
        raise RuntimeBootstrapError(msg) from exc


def _web_search_credentials(web: WebToolsSettings) -> tuple[str | None, str | None]:
    """Return `(search_provider, api_key)` for `tools.web.*`.

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
    provider = web.search_provider
    if not provider:
        for name in ("tavily", "exa"):
            found = os.environ.get(f"{name.upper()}_API_KEY", "").strip()
            if found:
                return name, found
        return None, None

    key_env = f"{provider.upper()}_API_KEY"
    if web.search_api_key_env:
        key_env = web.search_api_key_env
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
the tool dispatcher's own cap in `jarvis.execution.tools`."""

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
        cost_recorder: CostRecorder,
    ) -> None:
        """Wrap a vision-preset-bound `LLMClient` (see `_build_vision_client`)."""
        self._llm_client = llm_client
        self._cost_recorder = cost_recorder

    def describe_image(
        self, image_path: Path, *, question: str | None, instructions: str | None = None,
    ) -> str:
        """Send one image + optional question to the vision preset; return text.

        ``instructions`` replaces the screen-reading system prompt (a picture he sent, ADR 0223).
        Raises `VisionCallError` — never the raw SDK/HTTP exception —
        on any failure (MUST-FIX 1a, ADR-0011 §12): see that class's
        docstring for why.
        """
        mime = mimetypes.guess_type(image_path.name)[0] or "image/png"
        data_url = f"data:{mime};base64," + base64.b64encode(
            image_path.read_bytes(),
        ).decode("ascii")
        prompt_text = question or "Describe what is currently on this screen."
        system = instructions or _VISION_SYSTEM_PROMPT.format(language=language_name())
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
            result = self._cost_recorder.chat(
                self._llm_client,
                messages=messages,
                system=system,
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
        event_log_path: Path,
        pricing_path: Path | None,
    ) -> None:
        self._factory = LLMSessionFactory(config)
        self._snapshot = self._factory.snapshot()
        self._event_log_path = event_log_path
        self._pricing = {} if pricing_path is None else load_pricing_table(pricing_path)

    def describe_image(
        self, image_path: Path, *, question: str | None, instructions: str | None = None,
    ) -> str:
        """Perform one bounded request with its own SQLite connection."""
        client = self._factory.create(self._snapshot, response_id=new_response_id())
        with contextlib.closing(open_runtime_event_log(self._event_log_path)) as conn:
            recorder = CostRecorder(conn, pricing_table=self._pricing)
            return _LLMVisionClient(client, cost_recorder=recorder).describe_image(
                image_path, question=question, instructions=instructions,
            )


def _build_vision_client(
    config: Mapping[str, Any],
    preset_name: str,
    *,
    event_log_path: Path,
    pricing_path: Path | None = None,
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
    decision loop's own client (constructed at step 4, below): a client
    is pinned to one preset, so the vision model is a client of its own.
    This reuses the SAME loader
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
    return _RequestScopedVisionClient(
        vision_llm_config,
        event_log_path=event_log_path,
        pricing_path=pricing_path,
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


def load_runtime_env_and_trace(paths: RuntimePaths) -> None:
    """Load fill-only runtime env, then apply its optional trace destination."""
    load_env_file(paths.root)
    _configure_realtime_trace_export(paths)


def _register_workers(
    registry: ToolRegistry, paths: RuntimePaths, settings: WorkersSettings,
) -> Workers | None:
    """ADR 0019: workers are threads on one ``codex app-server``.

    Off unless ``tools.workers.enabled`` and ``codex`` is on this machine;
    ``tools.workers.roots`` lists the folders a worker may be started in.
    The server starts lazily on the first spawn, so a one-shot CLI turn
    pays nothing; the daemon stops it at shutdown.
    """
    if not settings.enabled:
        return None
    codex = shutil.which("codex")
    if codex is None:
        LOGGER.warning("tools.workers is on but codex is not on PATH; no workers this boot")
        return None
    workers = Workers(paths.root / "codex.sock", paths.event_log, codex_bin=codex)
    for worker_tool in make_worker_tools(workers, settings.roots):
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
        redirect_uri=block.get("oauth_redirect_uri") or None,
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


def _has_no_browser() -> bool:
    """True on Linux with no display, where ``webbrowser`` would start lynx or w3m here."""
    return sys.platform != "darwin" and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    )


def _print_only(_url: str) -> None:
    """The login's URL is already on stderr; open nothing."""


def mcp_login(
    server: str,
    *,
    config: RuntimeConfig,
    paths: RuntimePaths,
    open_browser: bool = True,
) -> int:
    """ADR 0032: log one `auth: oauth` server in through the browser; the daemon reuses the token.

    ``open_browser`` False, or a machine with no desktop (a brain on a Raspberry Pi), prints the
    authorization URL and opens nothing: open it in a browser on another machine that reaches this
    one's callback port through ``ssh -L``. The token is still stored here (ADR 0170).

    A local server that keeps its own login names its login command in
    ``login_args`` (ADR 0055); that command runs here, in the terminal, with the
    entry's command and environment, so it asks for exactly what the daemon's
    server will use. Returns a process exit code: 0 logged in, 1 the server
    refused or never asked for a login, 2 the entry is missing or has no login;
    a login command's own exit code otherwise.
    """
    block = _mcp_block(config.raw)
    servers = _all_mcp_servers(config.raw, _plugins(config.raw, config.repo_root))
    spec = servers.get(server)
    if not isinstance(spec, Mapping):
        known = ", ".join(sorted(servers)) or "none"
        sys.stderr.write(
            f"mcp-login: no server {server!r} in `tools.mcp.servers` or an enabled plugin"
            f" of {config.config_path} (known: {known})\n"
        )
        return 2
    if spec.get("command") and spec.get("login_args"):
        login = [str(spec["command"]), *(os.path.expandvars(str(a)) for a in spec["login_args"])]
        env = {**os.environ, **stdio_env(spec)}
        return subprocess.run(login, env=env, cwd=spec.get("cwd"), check=False).returncode  # noqa: S603 — Allen's own config names the command.
    if not is_oauth(spec):
        sys.stderr.write(f"mcp-login: {server} does not log in with OAuth; nothing to do\n")
        return 2
    opener: Callable[[str], object] = webbrowser.open
    if not open_browser or _has_no_browser():
        opener = _print_only
        port = int(block.get("oauth_callback_port", DEFAULT_OAUTH_CALLBACK_PORT))
        sys.stderr.write(
            f"mcp-login: no browser is opened here. Open the URL below in a browser that reaches "
            f"this machine's 127.0.0.1:{port}, e.g. from a Mac: "
            f"ssh -L {port}:127.0.0.1:{port} <user>@<this host>\n"
        )

    mcp_servers = _mcp_servers(block, paths, open_url=opener)
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


def import_exports(
    source: str,
    files: Sequence[Path],
    *,
    config: RuntimeConfig,
    paths: RuntimePaths,
) -> imports.Counts:
    """ADR 0259: keep the files raw under the runtime root and read their entries into memory.db.

    Raises:
        ValueError: ``source`` has no parser yet.
        OSError: a file cannot be read or kept.
    """
    with contextlib.closing(open_memory_db(config.memory.db_path)) as conn:
        return imports.import_files(conn, paths.root / imports.IMPORTS_DIRNAME, source, files)


def open_voiceprints(config: RuntimeConfig) -> VoiceprintBook:
    """ADR 0286: the owner's profiles in this runtime's ``memory.db``, for the CLI."""
    return VoiceprintBook(config.memory.db_path)


def bootstrap_runtime_app(  # noqa: PLR0915 - composition root wiring stays explicit
    config: RuntimeConfig, paths: RuntimePaths,
) -> JarvisRuntime:
    """Assemble a frozen :class:`JarvisRuntime` ready for :func:`run_turn`.

    The caller has laid out the runtime root (:func:`jarvis.deployment.bootstrap_runtime`),
    loaded its environment (:func:`load_runtime_env_and_trace`) and parsed the config
    (:func:`~jarvis.runtime.config.load_runtime_config`): a bad value has stopped it before
    anything here opens a file.

    Wiring (bottom-up across the layer DAG):

    1. L2 event log — :func:`jarvis.state.event_log.open_event_log`
       opens (or creates) the SQLite spine, idempotently installing
       the schema / indexes / append-only triggers.
    2. L4 registry + lifecycle — :func:`jarvis.execution.tools.build_default_registry`
       registers the tools; the :class:`ActionLifecycle` is per-process FSM.
    2b. L3 Tier 0 whitelist — ``config/tier0_patterns.yaml`` parsed and
       cross-checked against the registry's ``regex_router`` surface.
    3. L3 LLM client — :class:`jarvis.decision.llm.LLMClient` reads the
       ``llm:`` block from the YAML config. Lazy SDK construction —
       no network call until ``run_turn`` actually invokes ``chat``.

    L5 :class:`SurfaceState` is allocated per turn inside :func:`run_turn`
    (spec §3.6.7 — local surface state owns no truth and doesn't survive
    across turns), so the composition root does not wire it here.

    Args:
        config: The parsed config, with the role's overrides applied.
        paths: The runtime root's layout.

    Returns:
        Frozen :class:`JarvisRuntime`.

    Raises:
        RuntimeBootstrapError: When the prompt file, the Tier 0, grammar or cue tables, or the
            ``llm`` section is missing or malformed, or the zone database has no rule for now.
    """
    config_path, repo_root = config.config_path, config.repo_root
    if not config.prompt_path.is_file():
        msg = f"runtime: prompt file {config.prompt_path} does not exist"
        raise RuntimeBootstrapError(msg)
    system_clock = SystemClock(config.zone)
    try:
        system_clock.check_rule()
    except ZoneError as exc:
        msg = f"runtime: {exc}"
        raise RuntimeBootstrapError(msg) from exc

    # 1. L2 event log.
    conn = open_event_log(paths.event_log)

    # 2. L4 registry + lifecycle. `search_notes`/`web_search`/`web_fetch`
    #    need `tools.obsidian.vault_root` / `tools.web.*` threaded into
    #    the registry at build time (ADR-0011 D7) — L4 handlers do not
    #    load YAML themselves.
    role = config.role
    listen_addresses = config.listen_addresses
    lang.set_language(config.language)
    log_llm_io = config.diagnostics.log_llm_io
    llm_io_log.configure(logs_dir(paths.root) / "llm-io.jsonl" if log_llm_io else None)
    wave1_features = config.realtime.wave1
    tools = config.tools
    web_search_provider, web_search_api_key = _web_search_credentials(tools.web)
    _install_open_path(tools.open_path)
    memory = config.memory
    voiceprint_gate = config.voiceprint_gate  # ADR 0286, read once at boot
    # ADR 0170: a brain's TimeSink and git are its terminal's, so what reads them asks the hub.
    locate_waits = LocateWaits()
    phone_events = (
        BrainEvents(conn, locate_waits) if role == "brain" or listen_addresses else None
    )
    terminal_hub = TerminalHub(events=phone_events) if role == "brain" else None
    device = None if terminal_hub is None else terminal_hub.call
    settings_drift = _settings_drift(terminal_hub, paths, config.raw)
    moment = _moment(
        config.raw, config_path, memory.db_path, device, config.observer.timesink_db_path,
    )
    # ADR 0068: refuse a memory.db a newer Jarvis wrote before anything writes to it.
    open_memory_db(memory.db_path).close()
    local_zone = config.zone
    # ADR 0234: home and school move out of the settings into the places table, once.
    place_state.migrate_settings(memory.db_path, config.transit_places)
    maps_key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    _start_locating_places(memory.db_path, maps_key)
    kept_places = partial(place_state.home_school, memory.db_path)
    work_state = WorkStateService(
        event_log_path=paths.event_log,
        memory_path=memory.db_path,
        timesink_path=config.observer.timesink_db_path,
        repos=config.observer.repos,
        analyst=build_analyst(
            config.raw,
            config.work_state.preset,
            pricing_path=config.pricing_path,
            account_cost=True,
        ),
        model=config.work_state.preset,
        tz=config.zone,
        device=device,
    )
    catalog = parse_catalog(config.raw.get("projects"))
    projects = (
        None
        if not catalog
        else ProjectsService(
            event_log_path=paths.event_log,
            timesink_path=config.observer.timesink_db_path,
            projects=catalog,
            sorter=build_analyst(
                config.raw,
                config.work_state.preset,
                pricing_path=config.pricing_path,
                account_cost=True,
                kind="projects",
            ),
            model=config.work_state.preset,
            tz=config.zone,
            device=device,
        )
    )
    daily_report = DailyReportService(
        memory_path=memory.db_path,
        timesink_path=config.observer.timesink_db_path,
        repos=config.observer.repos,
        reporter=build_analyst(
            config.raw,
            config.daily_report.preset,
            pricing_path=config.pricing_path,
            account_cost=True,
            kind="daily_report",
            timeout_s=_DAILY_REPORT_TIMEOUT_S,
        ),
        model=config.daily_report.preset,
        tz=config.zone,
        codex_sessions_path=_codex_sessions_path(config.daily_report),
        device=device,
    )
    night = (
        None
        if role == "brain"  # ADR 0170: holding a Mac awake and dark is the terminal's job
        else NightRun(
            paths.event_log,
            night_settings(config.raw),
            MacPower(),
            zone=config.zone,
        )
    )
    voice_settings = VoiceSettings(paths.root / "voice-settings.json")
    view, mail_view, mail_drafts = _dashboard_state(config.dashboard)
    voice_cues = VoiceCues()
    ambient = (
        AmbientSounds(log_path=logs_dir(paths.root) / "ambient-sounds.jsonl")
        if config.realtime.ambient_sounds else None
    )
    bus_live = BusLive(paths.root / "cache")
    transit_offers = TransitOffers()  # the list `bus` keeps, and its chat card rows (ADR 0205)
    vision_client = _build_vision_client(
        config.raw,
        tools.vision_preset,
        event_log_path=paths.event_log,
        pricing_path=config.pricing_path,
    )
    deep_search = build_deep_search(
        config.raw,
        event_log_path=paths.event_log,
        pricing_path=config.pricing_path,
        account_cost=True,
    )
    rides = Rides(paths.event_log, bus_live, places=kept_places, offers=transit_offers)  # ADR 0224
    locator = (  # ADR 0281: only where a phone's answer can arrive
        None if phone_events is None else _locator(config.locate, locate_waits, system_clock)
    )
    here = HerePolicy(  # ADR 0322: the one Here every tool call reads where he is from
        paths.event_log, system_clock, locator=locator, mac=_here_location(role),
        memory_db=memory.db_path, phone=config.locate.phone, max_age_s=config.locate.max_age_s,
    )
    weatherkit = WeatherKit.from_config(config.raw)  # ADR 0285: logs which source is used
    surroundings = Surroundings(  # ADR 0284: held by position; the tool and the state line share it
        maps_key, config.maps_monthly_usd, memory.db_path, local_zone,
    )
    doing = _doing_schedule(config, memory, paths, device)
    jev_log = _jev_log(config.raw, paths.root)

    def make_late(connections: PluginConnections) -> tuple[JobMail | None, Home]:
        _wire_plan_reader(daily_report, connections)
        job_mail = _job_mail(
            config.raw, config_path, jev_log, connections, memory.db_path, system_clock,
            moment, device, paths.event_log, config.observer.timesink_db_path,
        )
        home = Home(
            connections,
            (config.zone_name, config.zone),
            config.home_weather,
            _mail_reply(config.raw, config_path, system_clock, jev_log),
            mail_view,
            None if mail_view is None else mail_summarizer(
                # ADR 0148: the cheapest preset (gpt-6-luna), never Jev: a body goes here.
                build_analyst(
                    config.raw,
                    config.work_state.preset,
                    pricing_path=config.pricing_path,
                    account_cost=True,
                    kind="mail_summary",
                ),
                paths.event_log,
            ),
            weatherkit,
        )
        return job_mail, home

    build_registry = partial(
        build_default_registry,
        mail_drafts=mail_drafts,
        memory_db_path=memory.db_path,
        observed_repos=config.observer.repos,
        timesink_db_path=config.observer.timesink_db_path,
        work_state_refresh=_work_state_tool_refresh(work_state, daily_report),
        night=night,
        voice_settings=voice_settings,
        weather_lookup=weather_lookup(
            config.home_weather, weatherkit,
            config.zone_name,
        ),
        transit_api_key=maps_key,
        maps_monthly_usd=config.maps_monthly_usd,
        surroundings=surroundings,
        transit_places=kept_places,
        transit_offers=transit_offers,
        clock=system_clock,
        here=here,
        bus_live=_live_departure(bus_live),
        ride_control=rides.control(),
        highlight_marker=_highlight_marks(config, memory, paths, device).mark,
        plan_day_ends=config.plan_day_ends,
        ring_before_min=config.remind_before_min,
        confirmation_dispatch_outbox=wave1_features.confirmation_dispatch_outbox,
        obsidian_vault_root=tools.obsidian_vault_root,
        web_search_max_results=tools.web.search_max_results,
        web_search_provider=web_search_provider,
        web_search_api_key=web_search_api_key,
        web_fetch_max_bytes=tools.web.fetch_max_bytes,
        web_fetch_max_text_bytes=tools.web.fetch_max_text_bytes,
        web_timeout_s=tools.web.timeout_s,
        web_deep_search=deep_search,
        vision_client=vision_client,
        screen_max_width_px=tools.screen_max_width_px,
        device_link=device,
    )
    served = register_catalog(
        build_registry=build_registry,
        offer_devices=partial(device_tools.follow_terminals, terminal_hub, paths.root),
        paths=paths,
        workers=tools.workers,
        view=view,
        make_plugins=lambda reg: PluginConnections(
            repo_root=repo_root, runtime_root=paths.root, event_log=paths.event_log,
            registry=reg, config=config.raw,
        ),
        make_late=make_late,
        memory_db=memory.db_path,
        doing=doing,
        ledger=LedgerSources(
            memory.db_path, paths.event_log, config.observer.timesink_db_path, local_zone,
            None if device is None else TerminalScreen(device, wait_s=TURN_WAIT_S),
            paths.artifacts_root / ATTACHMENTS_DIRNAME,
        ),
    )
    registry, workers, plugin_connections = served.registry, served.workers, served.plugins
    job_mail, home = served.job_mail, served.home
    lifecycle = ActionLifecycle()

    # 3b. Spec §17 Tier 0 whitelist — sits next to jarvis.yaml so Allen
    #     edits one config directory. Invalid content fails the boot
    #     loudly (no silent pattern drops); missing file = Tier 0 off.
    tier0_path = config_path.parent / "tier0_patterns.yaml"
    try:
        tier0_table = _on_menu(load_tier0_table(tier0_path), registry, role)
        regex_router_tools = _regex_router_tools(registry)
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

    # 4. L3 LLM client. `config.raw` was already loaded at step 3 above.
    llm_config = config.raw.get("llm")
    if not isinstance(llm_config, Mapping):
        msg = f"runtime: {config_path} has no 'llm' section"
        raise RuntimeBootstrapError(msg)
    llm_client = LLMClient(llm_config)
    think_mode = _think_mode(llm_config, config_path)

    # 4b. ADR-0008 Step 2 (Wave 4A). The whole flag graph was resolved once, when the
    #     config was parsed; every downstream site reads the validated result. Every
    #     runtime gets the session factory and the cancel registry (ADR 0332).
    response_flags = config.realtime.response.flags
    # The bus is gated on `realtime.enabled` rather than a Wave-4A switch so
    # Waves 4B/4C reuse this same line. It is inert while nobody subscribes.
    committed_event_bus = CommittedEventBus() if config.realtime.enabled else None
    llm_session_factory = LLMSessionFactory(llm_config)
    response_runs = ResponseRunRegistry()

    # Plugin skills are sampled per turn so connections need no daemon restart.
    system_prompt = config.prompt_path.read_text(encoding="utf-8").replace(
        "{assistant}", config.assistant_name
    )
    if config.reply_language in REPLY_LINES:
        reply_line = REPLY_LINES[config.reply_language]
        system_prompt = f"{system_prompt.rstrip()}\n\n{reply_line}\n"
    plugin_connections.publish_event = (
        committed_event_bus.publish if committed_event_bus is not None else None
    )

    mail_codes = _mail_codes(config.raw, config_path, plugin_connections, device)
    commitments = _commitments(
        config.raw, config_path, plugin_connections, memory.db_path, paths.event_log,
        lambda preset: build_analyst(
            config.raw,
            preset,
            pricing_path=config.pricing_path,
            account_cost=True,
            kind="commitments",
        ),
    )
    claude_daily = _claude_daily(
        config.raw, config_path, plugin_connections, memory.db_path, paths.root,
    )
    things_ledger = _things_ledger(
        paths.event_log, memory.db_path, local_zone, config.remind_before_min,
    )
    things_mirror = ThingsMirror(plugin_connections, paths.event_log, local_zone)
    turn_things = _turn_things(
        config.raw, config_path, memory.db_path, paths.event_log, local_zone,
        lambda preset: build_analyst(
            config.raw,
            preset,
            pricing_path=config.pricing_path,
            account_cost=True,
            kind="turn_things",
        ),
    )
    reminders = Reminders(
        paths.event_log,
        moment=moment,
        departures=Departures(paths.event_log, bus_live, transit_offers),
    )
    output = _sound_output(terminal_hub, job_mail, reminders)
    attachments = Attachments(paths.artifacts_root / ATTACHMENTS_DIRNAME)
    picture_files = PictureFiles(attachments, memory_db=memory.db_path)
    set_stale_files_handler(picture_files.stale)  # ADR 0255
    runtime = JarvisRuntime(
        config=config,
        runtime_paths=paths,
        conn=conn,
        tool_registry=registry,
        lifecycle=lifecycle,
        llm_client=llm_client,
        system_prompt=system_prompt,
        clock=system_clock,
        terminal_hub=terminal_hub,
        settings_drift=settings_drift,
        phone_events=phone_events,
        locator=locator,
        attachments=attachments,
        picture_files=picture_files,
        picture_notes=(
            None if vision_client is None else PictureNotes(
                memory_db=memory.db_path,
                store=attachments,
                vision=vision_client,
                event_log=paths.event_log,
                pinned_language=config.reply_language,
            )
        ),
        tier0_table=tier0_table,
        confirm_grammar_table=confirm_grammar_table,
        confirmation_words=_confirmation_words_client(config),
        wave1_features=wave1_features,
        response_flags=response_flags,
        llm_session_factory=llm_session_factory,
        response_runs=response_runs,
        committed_event_bus=committed_event_bus,
        decision_state=DecisionStateCache(
            partial(open_runtime_event_log, paths.event_log),
            is_idle=partial(_daemon_idle, paths.event_log),
        ),
        surrogate_route=_surrogate_route(config.raw, config_path, system_clock, jev_log),
        memory=memory,
        workers=workers,
        plugin_connections=plugin_connections,
        voiceprints=VoiceprintBook(memory.db_path, gate=lambda: voiceprint_gate),
        tool_cues=tool_cues,
        think_mode=think_mode,
        work_state=work_state,
        work_state_auto=config.work_state.auto,
        projects=projects,
        home=home,
        view=view,
        doing=doing,
        mail_drafts=mail_drafts,
        ambient=ambient,
        live_context=(
            rides.line,
            *(() if view is None else (view.line,)),
            *(() if mail_drafts is None else (mail_drafts.line,)),
            voice_cues.line,
            *(() if ambient is None else (ambient.line,)),
            partial(_shares_line, paths.event_log),
            # ADR 0249: what he is doing now, with its age, then his next things, read from the
            # ledger of his things (ADR 0299).
            partial(doing_now_line, paths.event_log, local_zone, system_clock.now),
            partial(calendar_line, paths.event_log, local_zone, system_clock.now),
            # ADR 0291: today's plan, settled or not, with its next item.
            partial(
                plan_line, paths.event_log, local_zone, system_clock.now,
                config.plan_day_ends,
            ),
        ),
        voice_cues=voice_cues,
        turn_end_asks=_turn_end_asks(config.raw, config_path, system_clock, jev_log),
        job_mail=job_mail,
        mail_codes=mail_codes,
        commitments=commitments,
        claude_daily=claude_daily,
        things=things_ledger,
        things_mirror=things_mirror,
        turn_things=turn_things,
        moment=moment,
        push=Push.from_config(config.raw, paths.root, paths.event_log),
        whereabouts=Whereabouts(
            paths.root, terminal_hub, moment, memory_db=memory.db_path, zone=local_zone,
            around=surroundings, here=here,
        ),
        about_items=_about_items(config, memory, paths),
        reminders=reminders,
        rides=rides,
        leg_planner=None if not maps_key else partial(
            build_leg_planner, maps_key, kept_places,
            kept=partial(place_state.read_kept, memory.db_path),
            cap_usd=config.maps_monthly_usd,
        ),
        voice_words=_voice_words(config.raw, config_path, system_clock, jev_log),
        oneshot=_jev_oneshot(
            config.raw, config_path, system_clock, jev_log, tier0_table,
            _event_emitter(paths.event_log),
        ),
        settings=Settings(
            paths.root,
            config.raw,
            _no_audio_devices if role == "brain" else _audio_devices,
            _no_default_audio_device if role == "brain" else _default_audio_device,
            owned=page_keys("brain", "both") if role == "brain" else None,
        ),
        night=night,
        voice_settings=voice_settings,
        daily_schedule=_daily_schedule(daily_report, paths.event_log, config, terminal_hub),
        ledger=_ledger(config, memory, paths.event_log, device, attachments.directory),
        places_block=partial(_places_block, memory.db_path, paths.event_log, local_zone),
        sound_output=output,
    )
    _follow_conversation_setting(runtime.settings, llm_session_factory)
    return runtime


def _follow_conversation_setting(settings: Settings | None, factory: LLMSessionFactory) -> None:
    """ADR 0297: the page's conversation model and effort reach the next turn at once."""
    if settings is None:
        return
    settings.on_conversation = factory.choose
    if chosen := settings.conversation_saved():
        factory.choose(*chosen)


__all__ = [
    "JarvisRuntime",
    "PreEmitTokenError",
    "RunTurnResult",
    "bootstrap_runtime_app",
    "import_exports",
    "open_voiceprints",
    # Re-exported for jarvis.cli: H13 forbids a single file importing two
    # middle-layer siblings, and the CLI already imports jarvis.deployment.
    # Same route PreEmitTokenError above already takes.
    "parse_response_channels",
]
