"""Composition root — the only module allowed to wire across middle layers.

Per ADR 0001 § Six-layer boundary contract and ``.importlinter`` layer
rules: ``jarvis.runtime`` is the SINGLE module in the codebase that may
import ``jarvis.decision``, ``jarvis.execution``, ``jarvis.surface``,
``jarvis.deployment`` simultaneously. Every other module respects the
sibling-isolation contract via Protocols.

Responsibilities (Day-1):

1. :func:`jarvis.runtime.bootstrap.app.bootstrap_runtime_app` — assemble the
   frozen :class:`JarvisRuntime` from the parsed config, in stages that clean up after
   themselves (the package :mod:`jarvis.runtime.bootstrap`, ADR 0372). No LLM call happens
   during bootstrap.
2. The turn lives in :mod:`jarvis.runtime.turn` (ADR 0371): this module
   holds only the public types it shares with bootstrap, :class:`JarvisRuntime`
   and :class:`RunTurnResult`.

Layer rules: ``jarvis.runtime`` may import everything below it. It is
imported by ``jarvis.cli`` only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from jarvis.runtime import audio_output
from jarvis.shared.clock import Clock, SystemClock
from jarvis.shared.realtime import (
    Wave1FeatureFlags,
    Wave4ResponseFlags,
)
from jarvis.surface.cli import (
    PreEmitTokenError,
    parse_response_channels,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

    from jarvis.decision import ResponsePlan
    from jarvis.decision.confirm_grammar import ConfirmGrammarTable
    from jarvis.decision.jev_oneshot import JevOneShot
    from jarvis.decision.llm import LLMClient
    from jarvis.decision.llm_session import LLMSessionFactory
    from jarvis.decision.pre_route import (
        ToolCueTable,
    )
    from jarvis.decision.response_run import (
        ResponseRunRegistry,
    )
    from jarvis.decision.surrogate_route import SurrogateRoute
    from jarvis.decision.think_mode import ThinkMode
    from jarvis.decision.tier0 import Tier0Table
    from jarvis.decision.turn_end_asks import TurnEndAsks
    from jarvis.decision.voice_words import VoiceWords
    from jarvis.deployment import RuntimePaths
    from jarvis.execution.mcp_tools import McpServers
    from jarvis.execution.tools import (
        ActionLifecycle,
        ToolContext,
        ToolRegistry,
    )
    from jarvis.execution.workers import Workers
    from jarvis.runtime.about_item import AboutItems
    from jarvis.runtime.claude_daily import ClaudeDailyIntake
    from jarvis.runtime.commitment_intake import CommitmentIntake
    from jarvis.runtime.config import (
        RuntimeConfig,
    )
    from jarvis.runtime.daily_report import (
        DailySchedule,
    )
    from jarvis.runtime.dashboard import (
        MailDrafts,
        ViewState,
    )
    from jarvis.runtime.decision_state import DecisionStateCache
    from jarvis.runtime.doing_run import DoingSchedule
    from jarvis.runtime.home import Home
    from jarvis.runtime.job_mail import JobMail
    from jarvis.runtime.ledger import LedgerContext
    from jarvis.runtime.locate import Locator
    from jarvis.runtime.mail_codes import MailCodes
    from jarvis.runtime.moment import Moment
    from jarvis.runtime.night_run import NightRun
    from jarvis.runtime.picture_files import PictureFiles
    from jarvis.runtime.picture_notes import PictureNotes
    from jarvis.runtime.plugin_connections import PluginConnections
    from jarvis.runtime.projects import ProjectsService
    from jarvis.runtime.push import Push
    from jarvis.runtime.reminders import Reminders
    from jarvis.runtime.rides import Rides
    from jarvis.runtime.settings import Settings
    from jarvis.runtime.settings_owner import SettingsDrift
    from jarvis.runtime.things_ledger import ThingsLedger
    from jarvis.runtime.things_mirror import ThingsMirror
    from jarvis.runtime.turn_things import TurnThings
    from jarvis.runtime.whereabouts import Whereabouts
    from jarvis.runtime.work_state import WorkStateService
    from jarvis.shared import Event
    from jarvis.state.attachments import Attachments
    from jarvis.state.committed_event_bus import CommittedEventBus
    from jarvis.state.memory_db import (
        MemorySettings,
        PlacesBlock,
    )
    from jarvis.state.voice_settings import VoiceSettings
    from jarvis.state.voiceprint import VoiceprintBook
    from jarvis.surface.ambient_sounds import AmbientSounds
    from jarvis.surface.terminal_events import BrainEvents
    from jarvis.surface.terminal_link import TerminalHub
    from jarvis.surface.voice_cues import VoiceCues


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


__all__ = [
    "JarvisRuntime",
    "PreEmitTokenError",
    "RunTurnResult",
    # Re-exported for jarvis.cli: H13 forbids a single file importing two
    # middle-layer siblings, and the CLI already imports jarvis.deployment.
    # Same route PreEmitTokenError above already takes.
    "parse_response_channels",
]
