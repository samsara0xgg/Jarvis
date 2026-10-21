"""Daemon entrypoint: serve the Inherent FastAPI app + watcher tasks.

Composition root (``runtime/``). Owns the daemon lifecycle for the
text Inherent surface introduced by ADR-0003 Step 1 (single-envelope)
and rewired for Step 2 (three-envelope ``open`` / ``append`` x N /
``done`` wire schema):

1. Run inside :func:`jarvis.deployment.daemon_session`, which holds the
   per-runtime-root process lock (ADR-0003 D4, ADR 0318 — refuses if
   another daemon already holds it) before anything is erased or opened.
2. Build the shared :class:`jarvis.surface.inherent_output.InherentBroadcaster`
   and a sync ``submit_callable`` bound to
   :func:`jarvis.surface.cli.emit_surface_user_intent` (mints a fresh
   ``turn_id`` per call).
3. Build the FastAPI app via
   :func:`jarvis.surface.inherent_server.create_app`.
4. Spawn two background async tasks:

   - :func:`_intent_pump_watcher` — polls the Event Log for new
     ``surface.user_intent`` and ``utterance.received`` rows, claims each
     once, and drives the turn via :func:`jarvis.runtime.drive_turn` on a
     worker thread with ``streaming_enabled=True`` so
     :func:`render_response` emits the ADR-0003 Step 2 three-event
     Inherent taxonomy (``surface.response_open`` +
     ``surface.response_chunk`` x N) before the audit
     ``surface.response_emitted``. ADR-0003 D9 F3: an uncaught exception
     from ``drive_turn`` is caught, logged, and recorded as a
     ``turn.failed`` audit event; the pump keeps polling.
   - :func:`_response_watcher` — polls the Event Log for new
     ``surface.response_{open,chunk,emitted}`` rows in ONE cursor
     (``WHERE type IN (...) ORDER BY id``) and dispatches to
     :meth:`InherentBroadcaster.broadcast_open` /
     :meth:`InherentBroadcaster.broadcast_chunk` /
     :meth:`InherentBroadcaster.broadcast_done` based on ``event.type``.
     Per ADR-0003 D16, three sibling watchers would race against
     asyncio's wakeup order and could send ``append`` before ``open``;
     a single-cursor + monotonic SQLite row id eliminates the race by
     construction.

5. Run :meth:`uvicorn.Server.serve` (blocks until SIGINT / SIGTERM).
6. On exit: cancel both watchers, await with ``return_exceptions=True``;
   the process lock is released last, by the caller's ``daemon_session``.

SQLite thread-safety note
-------------------------

:func:`jarvis.state.event_log.open_event_log` opens the connection with
the default ``check_same_thread=True``. The watchers' polling SELECTs
run on the event-loop thread (which is also the thread the runtime
``conn`` was opened on), so polling is safe. Re-entering ``drive_turn``
on the same connection from a different thread would crash; per the
established ``_bg_emit`` precedent in
:mod:`jarvis.execution.tools`/``test_runtime_composition``, the watcher
opens a fresh ``sqlite3.Connection`` to the same DB file inside the
worker thread and passes a shallow-replaced :class:`JarvisRuntime` to
``drive_turn``. The fresh connection is closed in the worker's
``finally``.

Layer rules
-----------

``runtime/`` is the composition root and may import from every sibling
layer (``deployment``, ``state``, ``surface``); ``lint-imports`` allows
this and refuses any new cross-sibling edges.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import json
import logging
import math
import os
import secrets
import signal
import socket
import sqlite3
import threading
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol, cast

import uvicorn

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Generator, Sequence
    from datetime import date, tzinfo

    from jarvis.decision import ToolRegistryLike
    from jarvis.decision.llm import LLMClient
    from jarvis.decision.llm_session import LLMSessionFactory
    from jarvis.decision.turn_end_asks import TurnEndAsks
    from jarvis.deployment.sleep_wake import PowerObserver
    from jarvis.runtime.dashboard import MailDrafts
    from jarvis.runtime.home import Home
    from jarvis.runtime.job_mail import JobMail
    from jarvis.runtime.moment import Moment
    from jarvis.runtime.picture_notes import PictureNotes
    from jarvis.runtime.reminders import Reminders
    from jarvis.runtime.rides import Rides
    from jarvis.runtime.settings import Settings
    from jarvis.runtime.settings_owner import SettingsDrift
    from jarvis.runtime.things_mirror import ThingsMirror
    from jarvis.runtime.work_state import WorkStateService
    from jarvis.shared.clock import Clock
    from jarvis.shared.realtime import PresentationIntent, Wave4ResponseFlags
    from jarvis.state.attachments import AttachmentRef
    from jarvis.state.committed_event_bus import CommittedEventBus
    from jarvis.state.voice_settings import VoiceSettings
    from jarvis.state.voiceprint import VoiceprintBook
    from jarvis.surface.ambient_sounds import AmbientSounds
    from jarvis.surface.codex_sessions import CodexSession
    from jarvis.surface.terminal_link import TerminalHub
    from jarvis.surface.voice_cues import VoiceCues

from jarvis.decision import request_confirmation
from jarvis.decision.commentary import (
    COMMENTARY_ATTENTION_CHANNEL,
    commentary_intent_for,
    commentary_speech_text,
    lead_in_speech_text,
)
from jarvis.decision.gates import ResponsePlan, pre_emit_gate
from jarvis.decision.response_run import (
    ResponseCancelRequest,
    ResponseRun,
    ResponseRunRegistry,
    ResponseTerminalizer,
    deterministic_commentary_policy,
    evidence_snapshot_hash,
    reconcile_open_responses,
    request_response_cancel,
    start_response_run,
)
from jarvis.deployment import data, inherent_v2_token_matches, models, rotate_inherent_v2_token
from jarvis.deployment.launchd import logs_dir, repo_root, supervised
from jarvis.deployment.sleep_wake import install_power_observer
from jarvis.execution.tools import SCREEN_ARTIFACTS_DIRNAME
from jarvis.execution.transit_tool import live_card
from jarvis.runtime import (
    JarvisRuntime,
    _assistant_name,
    _confirmation_ttl_ms,
    _event_action_id,
    _jev_log,
    _new_turn_id,
    _positive_int,
    _timesink_db_path,
    _work_state_timezone,
    device_voice,
    diagnostics_flag,
    make_barge_in_interrupt_callable,
    make_compose_callable,
    make_continuation_callable,
    make_foreground_decision_callable,
    make_relation_supersede_callable,
    make_response_cancel_callable,
    make_supersede_unspoken_callable,
    make_turn_cancel_callable,
    picture_files,
    save_language,
)
from jarvis.runtime.card_feedback import CardFeedback
from jarvis.runtime.core_memory import CoreMemorySettings, section_caps
from jarvis.runtime.daemon.devices import output_or_default as _output_or_default
from jarvis.runtime.daemon.devices import start_audio_device_watch as _start_audio_device_watch
from jarvis.runtime.daemon.devices import watch_board as _watch_board
from jarvis.runtime.daemon.intent_pump import start_intent_pump as _start_intent_pump
from jarvis.runtime.daemon.live_backend import LIVE_TERMINAL_TYPES as _LIVE_TERMINAL_TYPES
from jarvis.runtime.daemon.live_backend import LiveBackend as _LiveBackend
from jarvis.runtime.daemon.live_backend import ask_outcome as _ask_outcome
from jarvis.runtime.daemon.observers import agents_port as _agents_port
from jarvis.runtime.daemon.observers import claude_sessions_read as _claude_sessions_read
from jarvis.runtime.daemon.observers import claude_sessions_served as _claude_sessions_served
from jarvis.runtime.daemon.observers import make_usage_observer as _make_usage_observer
from jarvis.runtime.daemon.observers import refresh_usage_now as _refresh_usage_now
from jarvis.runtime.daemon.observers import start_meaning_index as _start_meaning_index
from jarvis.runtime.daemon.observers import start_repo_observer as _start_repo_observer
from jarvis.runtime.daemon.observers import start_timesink_observer as _start_timesink_observer
from jarvis.runtime.daemon.observers import start_usage_observer as _start_usage_observer
from jarvis.runtime.daemon.output_watchers import TTS_SILENT_CHANNELS as _TTS_SILENT_CHANNELS
from jarvis.runtime.daemon.output_watchers import PhoneRows as _PhoneRows
from jarvis.runtime.daemon.output_watchers import SpokenRows as _SpokenRows
from jarvis.runtime.daemon.output_watchers import add_places as _add_places
from jarvis.runtime.daemon.output_watchers import add_reactions as _add_reactions
from jarvis.runtime.daemon.output_watchers import add_steps as _add_steps
from jarvis.runtime.daemon.output_watchers import log_watcher_death as _log_watcher_death
from jarvis.runtime.daemon.output_watchers import phone_ends_watcher as _phone_ends_watcher
from jarvis.runtime.daemon.output_watchers import response_watcher as _response_watcher
from jarvis.runtime.daemon.output_watchers import tool_status_watcher as _tool_status_watcher
from jarvis.runtime.daemon.output_watchers import tts_watcher as _tts_watcher
from jarvis.runtime.daemon.output_watchers import turn_devices as _turn_devices
from jarvis.runtime.daemon.polling import DEFAULT_POLL_INTERVAL_S as _DEFAULT_POLL_INTERVAL_S
from jarvis.runtime.daemon.restart import fetch_models_then_restart as _fetch_models_then_restart
from jarvis.runtime.daemon.restart import restart_soon as _restart_soon
from jarvis.runtime.daemon.restart import restart_when_idle as _restart_when_idle
from jarvis.runtime.daemon.restart import restarting as _restarting
from jarvis.runtime.daemon.sweep import start_sweep_control_plane as _start_sweep_control_plane
from jarvis.runtime.daemon.voice_power import SingleIngressActivation as _SingleIngressActivation
from jarvis.runtime.daemon.voice_power import VoiceInputOwners as _VoiceInputOwners
from jarvis.runtime.daemon.voice_power import VoicePowerCoordinator as _VoicePowerCoordinator
from jarvis.runtime.day_experience import DayExperienceSchedule, DayExperienceSettings
from jarvis.runtime.day_plan import DayPlanSettings, DayPlanSources, build_schedule
from jarvis.runtime.day_summary import DaySummarySchedule, DaySummarySettings
from jarvis.runtime.dictation import (
    COMMAND_PROMPT,
    Dictation,
    build_dictation,
    load_user_terms,
    local_polisher,
    whisper_ears,
)
from jarvis.runtime.doing_answers import DoingAnswers
from jarvis.runtime.doing_day import page_reach, read_day_doing
from jarvis.runtime.highlight_marks import HighlightMarks
from jarvis.runtime.job_mail import served_notices, settle_notice
from jarvis.runtime.memory_page import MemoryPage
from jarvis.runtime.night_watch import NightWatch
from jarvis.runtime.phone_voice import PhoneSpeech
from jarvis.runtime.plan_run import Hooks, build_watch, leg_session, make_route_legs
from jarvis.runtime.session_compaction import CompactionSweep, preset_context_length
from jarvis.runtime.settings import SETTINGS_FILE
from jarvis.runtime.setup import Setup
from jarvis.runtime.spend_cap import SpendCap, SpendCapSettings
from jarvis.runtime.work_state import auto_refresh
from jarvis.shared import Event, lang
from jarvis.shared.pricing import load_pricing_table
from jarvis.shared.realtime import (
    AlreadyTerminal,
    new_response_id,
)
from jarvis.shared.realtime_trace import record_realtime_trace
from jarvis.state import (
    footprint,
    phone_track,
    picture_notes,
    places,
    quiet_mode,
    reactions,
    things,
)
from jarvis.state.daily_report import resolve_zone
from jarvis.state.day_highlights import day_notes
from jarvis.state.day_line import DaySources, day_window, fold_day
from jarvis.state.departures import ID_PREFIX as PIN_PREFIX
from jarvis.state.device_tokens import PairingCodes, device_name_for_token, device_token_matches
from jarvis.state.doing import fact_spans
from jarvis.state.event_log import (
    MAC_NODE,
    confirmation_device,
    emit_event,
    fetch_events_after,
    get_event,
    iter_events_of_types,
    latest_id,
    open_event_log,
    open_runtime_event_log,
    turn_origin,
)
from jarvis.state.imports import IMPORTS_DIRNAME, shared_reads
from jarvis.state.input_submission_inbox import (
    InputReceipt,
    PayloadConflictError,
    SubmissionInProgressError,
    SubmissionKey,
    claim_asr_request,
    release_asr_request,
    resolve_asr_request,
    submit_text_once,
)
from jarvis.state.ledger import TURN_WAIT_S, TerminalScreen
from jarvis.state.lifelog import DEFAULT_MIN_FREE_BYTES, LIFELOG_DIRNAME, Lifelog
from jarvis.state.memory_db import (
    HISTORY_PICTURES,
    MemorySettings,
    conversation_rows,
    recent_image_ids,
    remember_fact,
    retention_days,
)
from jarvis.state.phone_day import day_summary
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.state.projections import (
    CLARIFICATION_EVENT_TYPES,
    PendingClarification,
    PendingConfirmations,
    rebuild_projections,
)
from jarvis.state.push_tokens import register_body
from jarvis.state.reminders import ID_PREFIX
from jarvis.state.shipments import Shipments
from jarvis.state.turn_overlap import any_turn_in_flight
from jarvis.surface import (
    respeaker_board,
    terminal_listen,
    terminal_voice,
    voice_aec,
    voice_artifact_store,
    voice_asr,
    voice_audio,
    voice_backend,
    voice_controls,
    voice_ducking,
    voice_live,
    voice_media,
    voice_native_out,
    voice_pipeline,
    voice_session,
    voice_tts,
    voice_wake,
)
from jarvis.surface.claude_sessions import ClaudeSessions
from jarvis.surface.cli import emit_surface_user_intent, record_pre_emit_token
from jarvis.surface.cli_render import render_response
from jarvis.surface.device_pairing import DevicePairing, brain_urls
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import (
    InherentDeps,
    InherentV2Deps,
    InputSubmissionOutcome,
    create_app,
    manager_authorize,
    require_local_key,
)
from jarvis.surface.phone_link import PhoneHub
from jarvis.surface.phone_player import PHONE_SAMPLE_RATE_HZ, PhoneAudioStreamPlayer
from jarvis.surface.playback_recovery import reconcile_open_playback
from jarvis.surface.terminal_events import BrainEvents, phone_turn_id
from jarvis.surface.terminal_link import SETTINGS_NOTICE
from jarvis.surface.usage_observer import (
    latest_usage,
    redeem_codex_reset,
    usage_history,
)
from jarvis.surface.voice_print import load_extractor

LOGGER = logging.getLogger("jarvis.runtime.inherent_loop")

# The commentary path (``_open_commentary_in_worker_thread``, ``_commentary_watcher``) is split
# by its own owner and still spells the two L2 cursor helpers by their old names; its tests patch
# ``_fetch_events_after`` as the poll seam. Drop both lines with that split.
_latest_id = latest_id
_fetch_events_after = fetch_events_after


# How long serve_inherent's teardown may take from the moment serving ends. launchd kills
# a daemon 20 s after its stop (ExitTimeOut's default; the agent's plist sets none), so the
# watchers' wait and the stops of the children it started must end well inside that. A
# systemd unit waits longer (the brain's sets none, so 90 s); one bound serves both.
_SHUTDOWN_BUDGET_S: Final[float] = 15.0

# What the stop of a child gets (the codex app-server, the MCP clients) when the budget is
# already spent by a turn that would not end: a child left running outlives the daemon.
_CHILD_STOP_FLOOR_S: Final[float] = 2.0

# Default port the Inherent client connects to. Picked to NOT collide
# with the legacy ``ui/web/server.py`` default (8000) or the
# inherent-swift dev server (8001).
_DEFAULT_PORT: int = 8006


# The voice ASR / VAD artifact locations come from ``runtime.sensevoice_dir``
# / ``runtime.silero_vad_path`` (ADR-0006 §5), resolved once at the composition
# root against the config file's own directory. ADR-0005 §12 (pre-flight) —
# ``serve_inherent`` checks them BEFORE starting the microphone owner so a missing
# wheel surfaces as a single log line instead of a crashed daemon thread on
# first wake. Tests pin their own paths via :func:`_voice_models_preflight`
# kwargs.

# The two ``SileroVad`` profiles the daemon can bind (``voice_audio``'s
# ``_MODE_THRESHOLDS`` keys).  A closed set: ADR-0006 D8 names both and
# ``realtime.single_audio_ingress.output_active_vad_mode`` selects between them.
_VAD_MODES: Final[tuple[str, ...]] = ("record", "tts")
# ADR 0144: how long an ask card from a barge-pause fragment stays off the screen, so a
# continuation that folds the fragment in (ADR 0074) arrives before it ever shows.
_FRAGMENT_CARD_HOLD_MS: Final[int] = 1_500
_CARD_EVENT_TYPES: Final[tuple[str, ...]] = (
    "confirmation.requested", "confirmation.accepted", "confirmation.rejected", "gate.evaluated",
)
"""What the pending card folds from (ADR 0062), the rows the answer path itself re-reads."""


def _default_vad_profiles() -> dict[str, voice_audio.VadThresholds]:
    """Return the shipped per-mode thresholds — L5 stays their single source."""
    return {mode: voice_audio.SileroVad.thresholds(mode) for mode in _VAD_MODES}


@dataclasses.dataclass(frozen=True)
class _VoiceKnobs:
    """Every flat ``realtime:`` voice value, resolved once (ADR-0006 §5, :716).

    The field defaults ARE the shipped constants, so a config that sets none of
    these produces an instance indistinguishable from the hard-coded daemon —
    the property the whole change is required to preserve.  Parsed once in
    :func:`serve_inherent` and threaded from there; nothing below the
    composition root reads ``realtime:`` for these values.
    """

    # The engine's detection probability gate.
    wake_threshold: float = 0.5
    tts_voice: str = voice_tts.DEFAULT_TTS_VOICE
    tts_model: str = voice_tts.DEFAULT_TTS_MODEL
    tts_primary_endpoint: str = voice_tts.DEFAULT_TTS_PRIMARY_ENDPOINT
    tts_fallback_endpoint: str = voice_tts.DEFAULT_TTS_FALLBACK_ENDPOINT
    # MiniMax is asked for the highest rate it natively produces in our config
    # and resampled to the device rate via soxr on the way out.
    tts_sample_rate_in_hz: int = 32000
    tts_connect_timeout_s: float = voice_tts.DEFAULT_TTS_CONNECT_TIMEOUT_S
    tts_first_chunk_timeout_s: float = voice_tts.DEFAULT_TTS_FIRST_CHUNK_TIMEOUT_S
    tts_between_chunk_timeout_s: float = voice_tts.DEFAULT_TTS_BETWEEN_CHUNK_TIMEOUT_S
    vad_profiles: Mapping[str, voice_audio.VadThresholds] = dataclasses.field(
        default_factory=_default_vad_profiles,
    )


def _knob_number(values: Mapping[str, Any], key: str, fallback: float) -> float:
    """Return a positive numeric ``realtime.<key>``; ``fallback`` with a warning.

    Degrade rather than raise: these are read at the composition root, outside
    every downgrade boundary the daemon has, so a typo must not cost the boot.
    ``bool`` is excluded explicitly because it is an ``int`` subclass.
    """
    raw = values.get(key)
    if raw is None:
        return fallback
    if (
        isinstance(raw, bool)
        or not isinstance(raw, int | float)
        or not math.isfinite(raw)
        or raw <= 0
    ):
        LOGGER.warning(
            "realtime.%s must be a positive finite number; using %s.", key, fallback,
        )
        return fallback
    return float(raw)


def _knob_text(values: Mapping[str, Any], key: str, fallback: str) -> str:
    """Return a non-empty string ``realtime.<key>``; ``fallback`` with a warning."""
    raw = values.get(key)
    if raw is None:
        return fallback
    if not isinstance(raw, str) or not raw.strip():
        LOGGER.warning(
            "realtime.%s must be a non-empty string; using %r.", key, fallback,
        )
        return fallback
    return raw.strip()


def _knob_count(
    values: Mapping[str, Any],
    key: str,
    fallback: int,
    *,
    label: str | None = None,
) -> int:
    """Return a positive integer ``realtime.<key>``; ``fallback`` with a warning.

    ``label`` names the key in the warning when it differs from the lookup —
    a nested profile field is looked up as ``required_hits`` but must be
    reported as ``realtime.vad.record.required_hits`` to be actionable.
    """
    raw = values.get(key)
    if raw is None:
        return fallback
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        LOGGER.warning(
            "realtime.%s must be a positive integer; using %d.",
            key if label is None else label,
            fallback,
        )
        return fallback
    return raw


def _vad_profile(raw: object, *, mode: str, fallback: voice_audio.VadThresholds,
                 ) -> voice_audio.VadThresholds:
    """Parse one ``realtime.vad.<mode>`` profile; each field degrades alone."""
    if raw is None:
        return fallback
    if not isinstance(raw, Mapping):
        LOGGER.warning("realtime.vad.%s must be a mapping; using defaults.", mode)
        return fallback
    values: Mapping[str, Any] = raw
    prob = values.get("prob_threshold")
    db = values.get("db_threshold")
    if prob is not None and (
        isinstance(prob, bool) or not isinstance(prob, int | float) or not 0.0 < prob <= 1.0
    ):
        LOGGER.warning(
            "realtime.vad.%s.prob_threshold must be in (0, 1]; using %s.",
            mode,
            fallback.prob_threshold,
        )
        prob = None
    if db is not None and (isinstance(db, bool) or not isinstance(db, int | float)):
        LOGGER.warning(
            "realtime.vad.%s.db_threshold must be a number; using %s.",
            mode,
            fallback.db_threshold,
        )
        db = None
    return voice_audio.VadThresholds(
        prob_threshold=fallback.prob_threshold if prob is None else float(prob),
        db_threshold=fallback.db_threshold if db is None else float(db),
        smoothing_window=_knob_count(
            values,
            "smoothing_window",
            fallback.smoothing_window,
            label=f"vad.{mode}.smoothing_window",
        ),
        required_hits=_knob_count(
            values,
            "required_hits",
            fallback.required_hits,
            label=f"vad.{mode}.required_hits",
        ),
        required_misses=_knob_count(
            values,
            "required_misses",
            fallback.required_misses,
            label=f"vad.{mode}.required_misses",
        ),
    )


def _vad_profiles(raw: object) -> dict[str, voice_audio.VadThresholds]:
    """Parse ``realtime.vad``; both profiles must share their debounce triple.

    ``DuplexVoiceSession`` calls :meth:`SileroVad.set_mode` on every
    output-active transition, i.e. mid-utterance.  That is only safe because
    ``reset`` sizes the smoothing deques from ``smoothing_window`` and the
    assembler snapshots ``required_misses`` once, so two profiles that disagree
    on either would desynchronise both.  A config that breaks the invariant is
    refused whole rather than half-applied.
    """
    defaults = _default_vad_profiles()
    if raw is None:
        return defaults
    if not isinstance(raw, Mapping):
        LOGGER.warning("realtime.vad must be a mapping; using defaults.")
        return defaults
    parsed = {
        mode: _vad_profile(raw.get(mode), mode=mode, fallback=defaults[mode])
        for mode in _VAD_MODES
    }
    debounce = {
        (p.smoothing_window, p.required_hits, p.required_misses) for p in parsed.values()
    }
    if len(debounce) > 1:
        LOGGER.warning(
            "realtime.vad profiles disagree on smoothing_window/required_hits/"
            "required_misses; the session switches profiles mid-utterance and "
            "cannot resize its debounce state. Using defaults for both.",
        )
        return defaults
    return parsed


def _visible_ask_card(conn: sqlite3.Connection, now_ms: int) -> PendingClarification | None:
    """ADR 0066: the ask card to show, or ``None``.

    ADR 0144: a card its turn put up from a barge-pause fragment stays hidden for
    ``_FRAGMENT_CARD_HOLD_MS``. ADR 0206: a continuation no longer closes it, so the hold
    only delays showing it.
    """
    slot = PendingClarification.from_events(iter_events_of_types(conn, CLARIFICATION_EVENT_TYPES))
    if slot is None or not slot.waiting:
        return None
    asked = conn.execute(
        "SELECT ts_epoch_ms FROM events WHERE type = 'clarification.requested' "
        "AND json_extract(payload_json, '$.clarification_id') = ?",
        (slot.clarification_id,),
    ).fetchone()
    if asked is not None and now_ms - int(asked[0]) < _FRAGMENT_CARD_HOLD_MS and conn.execute(
        "SELECT 1 FROM events WHERE type = 'utterance.received' "
        "AND json_extract(correlation_json, '$.turn_id') = ? "
        "AND json_extract(payload_json, '$.endpoint_reason') = 'barge_pause' LIMIT 1",
        (slot.asked_turn_id,),
    ).fetchone() is not None:
        return None
    return slot


def _voice_knobs(config: Mapping[str, Any]) -> _VoiceKnobs:
    """Resolve every flat ``realtime:`` voice value once (ADR-0006 §5)."""
    block = config.get("realtime")
    values: Mapping[str, Any] = block if isinstance(block, Mapping) else {}
    d = _VoiceKnobs()
    return _VoiceKnobs(
        wake_threshold=_knob_number(values, "wake_threshold", d.wake_threshold),
        tts_voice=_knob_text(values, "tts_voice", d.tts_voice),
        tts_model=_knob_text(values, "tts_model", d.tts_model),
        tts_primary_endpoint=_knob_text(
            values, "tts_primary_endpoint", d.tts_primary_endpoint,
        ),
        tts_fallback_endpoint=_knob_text(
            values, "tts_fallback_endpoint", d.tts_fallback_endpoint,
        ),
        tts_sample_rate_in_hz=_knob_count(
            values, "tts_sample_rate_in_hz", d.tts_sample_rate_in_hz,
        ),
        tts_connect_timeout_s=_knob_number(
            values, "tts_connect_timeout_s", d.tts_connect_timeout_s,
        ),
        tts_first_chunk_timeout_s=_knob_number(
            values, "tts_first_chunk_timeout_s", d.tts_first_chunk_timeout_s,
        ),
        tts_between_chunk_timeout_s=_knob_number(
            values, "tts_between_chunk_timeout_s", d.tts_between_chunk_timeout_s,
        ),
        vad_profiles=_vad_profiles(values.get("vad")),
    )


def _voice_models_preflight(
    *,
    sensevoice_dir: Path,
    silero_path: Path,
) -> tuple[bool, list[str]]:
    """Verify on-disk model artifacts before starting the microphone owner.

    ADR-0005 §12 — pre-flight semantic: do NOT attempt to construct a
    SenseVoice recognizer or Silero VAD when the underlying ``.onnx``
    files are not present. Returning ``(False, [...])`` lets
    :func:`serve_inherent` log one ERROR and keep the text path
    running; the alternative (constructing the recognizer eagerly)
    would let the input thread crash on its first inference frame and
    pollute the daemon log with an unhelpful traceback.

    Returns ``(all_ok, missing_paths_list)``.
    """
    missing: list[str] = []
    sv_model = sensevoice_dir / "model.int8.onnx"
    sv_tokens = sensevoice_dir / "tokens.txt"
    if not sv_model.exists():
        missing.append(f"sensevoice-small-int8 model.int8.onnx (expected at {sv_model})")
    if not sv_tokens.exists():
        missing.append(f"sensevoice-small-int8 tokens.txt (expected at {sv_tokens})")
    if not silero_path.exists():
        missing.append(f"silero_vad.onnx (expected at {silero_path})")
    return (not missing, missing)


def _reconcile_open_responses_in_thread(
    event_log_path: Path,
    committed_event_bus: CommittedEventBus | None,
) -> int:
    """Close every open ResponseRun once at boot (ADR-0008 F14 / §4.4).

    Runs on an ``asyncio.to_thread`` worker with its OWN connection. The
    offload is mandatory, not stylistic: ``serve_inherent``'s body executes
    on the event-loop thread and ``runtime.conn`` was opened there with
    ``check_same_thread=True``, so the reconciler's ``BEGIN IMMEDIATE``
    could not legally run on it. Doing this inside the startup barrier,
    before any watcher task exists, also means the reconciler and a fresh
    run can never contend.

    Returns the number of runs it closed.
    """
    conn = open_event_log(event_log_path)
    try:
        events = reconcile_open_responses(
            conn,
            ResponseTerminalizer(
                lambda: conn,
                close_after=False,
                committed_event_bus=committed_event_bus,
            ),
        )
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
    return len(events)


def _reconcile_open_playback_in_thread(
    event_log_path: Path,
    committed_event_bus: CommittedEventBus | None,
) -> int:
    """Close every playback generation a dead process abandoned (ADR-0008 §4.4).

    Runs on an ``asyncio.to_thread`` worker with its OWN connection, for the
    same ``check_same_thread`` reason as the reconciler above. Returns
    the number of generations it closed.
    """
    conn = open_event_log(event_log_path)
    try:
        events = reconcile_open_playback(conn, committed_event_bus=committed_event_bus)
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
    return len(events)


# --- ADR-0008 D6 lifecycle commentary ---------------------------------------
#
# The observer below is the whole of the D6 wiring. It is deliberately a
# durable-cursor watcher rather than a `CommittedEventBus` subscriber:
# `jarvis/execution/tools.py` is never handed a bus, so `action.dispatched`
# commits unpublished and a subscriber would silently lose the acknowledge
# row. The Event Log is the only place it is guaranteed to appear.
#
# Nothing here re-enters `decide()`: `_RUNTIME_TRIGGER_TYPES` is untouched and
# no model is ever called to produce a phrase (ADR-0008 D6, "a deep model is
# never called only to generate 我在查"). A turn he spoke says one fixed line
# when it is taking a while (ADR 0116, 0121, 0136): at the dispatch of a tool
# that is not quiet, saying what it is about to do, or once `_COMMENTARY_AFTER_S`
# have passed with nothing of the answer started; then a follow-up at each of
# `_COMMENTARY_STILL_AFTER_S` while the answer still has not started, at most
# `_COMMENTARY_MAX_LINES` in all.

_COMMENTARY_ACTION_TYPES: Final[tuple[str, ...]] = (
    "action.dispatched", "utterance.received", "route.tool_predicted",
)
"""The D6 rows that speak (:data:`jarvis.decision.commentary._D6_ROWS`)."""

_COMMENTARY_EARLIEST_S: Final[float] = 1.5
"""How long after Allen's words a tool's dispatch line may start.

An answer that begins within it needs no lead-in: a light switched in 0.4 s
(2026-09-26 to 09-29). The line speaks for the turn, not for the one tool:
a tool that has already finished does not silence it; an answer that is out
does. A Tier 0 turn ends within about 1 s, so the floor and the turn-ended
check keep it silent.
"""

_COMMENTARY_AFTER_S: Final[float] = 8.0
"""How long after Allen's words, with nothing of the answer started, the line
speaks whether or not a tool is involved (ADR 0121: a toolless turn answers at
a median 2.8 s and p90 5.0 s, so 2.5 s fired right before the answer; 2026-10-02
Allen moved it from 4 s to 8 s: 8 of 16 turns that heard a line at 4 s were
answered before 8 s)."""

_COMMENTARY_STILL_AFTER_S: Final[tuple[float, ...]] = (25.0,)
"""How long after Allen's words each follow-up ("still working") may speak while
the answer still has not started (ADR 0121)."""

_COMMENTARY_MAX_LINES: Final = 2
"""The most lines one turn says: the first and the follow-ups (ADR 0121)."""

_COMMENTARY_SHUTDOWN_BUDGET_S: Final[float] = 0.5
"""Whole-budget SQLite wait the teardown cancel may spend on the event loop."""

_COMMENTARY_ORIGIN_TRIGGER_TYPE: Final = "utterance.received"
"""What makes a turn one he spoke. A typed turn (`surface.user_intent`), a
GPT-Live relay (also `surface.user_intent`) and a reconciliation or
supervisor-sweep turn (no `turn.started` at all) never match it.
"""

_TURN_WORKING_WINDOW_MS: Final[int] = 5 * 60 * 1000
_RECENT_SPEECH_WINDOW_MS: Final[int] = 60 * 1000
"""A turn of his words still open this long after it began is the supervisor's, not
conversation mode's to wait for (the window ADR 0107 gives a turn in flight)."""

_COMMENTARY_LOCK: Final = threading.Lock()
"""One decide-and-render at a time, so the dispatch and the clock of the same
turn cannot both find it unspoken."""


@dataclasses.dataclass(frozen=True)
class _OpenCommentary:
    """One commentary ResponseRun that rendered but has not been heard yet.

    It stays open on purpose. ``response.completed`` is written when the
    phrase reaches the speaker (`surface.playback_started`); one that never
    did is cancelled at teardown.
    """

    subject_ref: str
    run: ResponseRun
    registry: ResponseRunRegistry
    response_hash: str


def _commentary_terminalizer(
    runtime: JarvisRuntime,
    *,
    deadline: float | None = None,
) -> ResponseTerminalizer:
    """Build a terminalizer that owns its connection, like the cancel seam.

    The run outlives the worker call that opened it, so the terminal owner
    cannot close over that call's connection; ``close_after=True`` with a
    per-call ``open_runtime_event_log`` is the established shape
    (:func:`jarvis.runtime.make_response_cancel_callable`).

    ``deadline`` becomes the connection's SQLite ``busy_timeout``. The
    teardown path passes one because it runs on the event-loop thread: an
    unbounded wait there would hold the loop for the default five seconds per
    unheard run while a worker owns the writer.
    """
    event_log_path = runtime.runtime_paths.event_log
    return ResponseTerminalizer(
        lambda: open_runtime_event_log(event_log_path, deadline=deadline),
        close_after=True,
        committed_event_bus=runtime.committed_event_bus,
    )


def _emit_pre_emit_verdict(
    conn: sqlite3.Connection,
    *,
    plan: ResponsePlan,
    turn_id: str,
) -> None:
    """Record the Pre-emit Gate verdict this commentary was approved under.

    ADR-0001 § Gate contracts: the gate emits
    ``gate.evaluated(gate="pre_emit", ...)`` before any byte of the response
    reaches L5. The commentary path calls the gate itself rather than going
    through ``decide()``, so it owns that append too; the payload is the same
    shape ``jarvis.decision``'s emitter writes.
    """
    emit_event(
        conn,
        type="gate.evaluated",
        payload={
            "gate": "pre_emit",
            "outcome": plan.permission,
            "reasons": [
                f"permission={plan.permission}",
                f"downgrade_required={plan.downgrade_required}",
                f"active_claim_levels={list(plan.active_claim_levels)}",
            ],
            "response_hash": plan.response_hash,
            "claim_levels": list(plan.active_claim_levels),
            "attempt": 0,
        },
        correlation={"turn_id": turn_id},
    )


# The trigger a turn was claimed from: the first `turn.started` naming the turn, undecoded.
_SELECT_TURN_CLAIM_TRIGGER_SQL: Final[str] = (
    "SELECT source_event_id FROM events WHERE type = 'turn.started' "
    "AND json_extract(payload_json, '$.turn_id') = ? ORDER BY id ASC LIMIT 1"
)


def _commentary_turn(
    conn: sqlite3.Connection, event: Event,
) -> tuple[str, str, int] | None:
    """Return this row's turn Allen spoke, his words and when, or ``None``.

    Durable reads and no heuristics: the row names its turn, that turn's
    ``turn.started`` names the trigger it was claimed from, and the trigger's
    own type decides whether Allen spoke this.
    """
    # Both rows that speak name their turn (`_action_correlation` fills it
    # from the ActionRequest; the voice pipeline writes it on his words).
    turn_id = (event.correlation or {}).get("turn_id")
    if not isinstance(turn_id, str) or not turn_id:
        return None
    started = conn.execute(_SELECT_TURN_CLAIM_TRIGGER_SQL, (turn_id,)).fetchone()
    if started is None or started[0] is None:
        return None
    trigger = get_event(conn, started[0])
    if trigger is None or trigger.type != _COMMENTARY_ORIGIN_TRIGGER_TYPE:
        return None
    transcript = trigger.payload.get("transcript")
    return turn_id, transcript if isinstance(transcript, str) else "", trigger.ts_epoch_ms


_SELECT_ACTION_TOOL_NAME_SQL = (
    "SELECT id, json_extract(payload_json, '$.tool_name'), "
    "json_extract(payload_json, '$.arguments.depth') FROM events "
    "WHERE type = 'action.proposed' AND json_extract(payload_json, '$.action_id') = ? LIMIT 1"
)


# The turn's latest line before a call, up to the row that speaks (a call's
# proposal, or the latest row for the clock). It speaks in place of the fixed
# phrase; a line never spoken at one dispatch (a quiet tool's) speaks at the next.
_SELECT_LEAD_IN_SQL = (
    "SELECT json_extract(payload_json, '$.lead_in') FROM events "
    "WHERE type = 'action.proposed' AND json_extract(payload_json, '$.turn_id') = ? "
    "AND json_extract(payload_json, '$.lead_in') IS NOT NULL AND id <= ? "
    "ORDER BY id DESC LIMIT 1"
)


_SELECT_TURN_ENDED_SQL = (
    "SELECT 1 FROM events WHERE type IN ('turn.ended', 'turn.failed') "
    "AND json_extract(payload_json, '$.turn_id') = ? LIMIT 1"
)

# His next words, his barge-in or the stop button cancel this turn's answer: it will
# never come, so no wait line is owed. (answer_started only ever cancels a wait line.)
_SELECT_TURN_SUPERSEDED_SQL = (
    "SELECT 1 FROM events WHERE type = 'response.cancelled' "
    "AND json_extract(payload_json, '$.reason') IN ('superseded', 'barge_in', 'user_stop') "
    "AND json_extract(payload_json, '$.turn_id') = ? LIMIT 1"
)

# The answer is `phase="final"` (or any phase but commentary): its first segment
# commits `surface.response_open` together with `surface.response_chunk` in a
# spoken_streaming turn, and the whole answer does at once in any other turn;
# `surface.playback_started` is the answer reaching the speaker. Any of the three
# counts. `response.started` does not: it opens the run before the model is asked.
_SELECT_ANSWER_STARTED_SQL = (
    "SELECT 1 FROM events WHERE type IN "
    "('surface.response_open', 'surface.response_chunk', 'surface.playback_started') "
    "AND json_extract(payload_json, '$.turn_id') = ? "
    "AND json_extract(payload_json, '$.phase') IS NOT 'commentary' LIMIT 1"
)

_SELECT_COMMENTARY_COUNT_SQL = (
    "SELECT COUNT(*) FROM events WHERE type = 'response.started' "
    "AND json_extract(payload_json, '$.phase') = 'commentary' "
    "AND json_extract(payload_json, '$.turn_id') = ?"
)


def _turn_commentary_count(conn: sqlite3.Connection, turn_id: str) -> int:
    """Return how many commentary runs this turn has opened.

    ``start_response_run`` writes ``response.started`` first, before the
    Pre-emit Gate and the three surface rows, so it is the earliest durable
    mark of "this turn already spoke" and the only one that beats a second
    row racing in on the next 10 ms poll. Reading the log rather than
    in-memory observer state is what makes the cap survive a restart, and
    what makes it hold when two rows of the same turn open on different
    worker threads.
    """
    row = conn.execute(_SELECT_COMMENTARY_COUNT_SQL, (turn_id,)).fetchone()
    return int(row[0]) if row is not None else 0


def _turn_over_or_answering(conn: sqlite3.Connection, turn_id: str) -> bool:
    """Return whether the turn has ended, been superseded or its answer has started."""
    return (
        conn.execute(_SELECT_TURN_ENDED_SQL, (turn_id,)).fetchone() is not None
        or conn.execute(_SELECT_TURN_SUPERSEDED_SQL, (turn_id,)).fetchone() is not None
        or conn.execute(_SELECT_ANSWER_STARTED_SQL, (turn_id,)).fetchone() is not None
    )


def _cancel_unheard_commentary(
    runtime: JarvisRuntime,
    entry: _OpenCommentary,
    *,
    reason: str,
    deadline: float | None = None,
) -> None:
    """Cancel a commentary run that never reached the speaker."""
    request_response_cancel(
        entry.registry,
        _commentary_terminalizer(runtime, deadline=deadline),
        ResponseCancelRequest(
            request_id="CREQ" + uuid.uuid4().hex,
            response_id=entry.run.response_id,
            scope="generation",
            reason=reason,
        ),
    )
    # Unconditional: whatever outcome came back, the watcher is done with this
    # entry, so the operator seam must stop seeing it.
    entry.registry.unregister(entry.run.response_id)


def _complete_commentary(runtime: JarvisRuntime, entry: _OpenCommentary) -> None:
    """Close a commentary run whose phrase reached the speaker."""
    # Before any early return: this entry is being released either way.
    entry.registry.unregister(entry.run.response_id)
    if not entry.run.is_open:
        # A cancel already won the CAS — playback of a cancelled phrase that
        # started anyway is not a reason to raise out of the watcher.
        return
    entry.run.mark("finalizing")
    outcome = _commentary_terminalizer(runtime).complete(
        entry.run.facts,
        response_hash=entry.response_hash,
    )
    if isinstance(outcome, AlreadyTerminal):
        return
    entry.run.mark("completed")


def _render_commentary(  # noqa: PLR0913 - the run's identity plus what it says
    runtime: JarvisRuntime,
    conn: sqlite3.Connection,
    *,
    intent: PresentationIntent,
    trigger_event: Event,
    turn_id: str,
    speech: str,
) -> _OpenCommentary:
    """Open one ``phase="commentary"`` run and deliver its single segment.

    ``speech`` is the model's own line before the call when it wrote a usable
    one, else the intent's fixed phrase.

    ``turn_id`` is the action's own turn, so ``response_group_id`` derives to
    that turn's group and voice_media appends the phrase to the lane instead
    of interrupting whatever else that group is saying. The trigger is the
    row that justified the phrase, a tool's dispatch or his words: D6's
    "truth derives from the durable event" is literally this run's
    ``source_event_id``.

    A request client is built only because ``response.started`` carries the
    preset snapshot. No request is ever issued, so the run has no cost
    disposition to record.
    """
    factory = runtime.llm_session_factory
    response_id = new_response_id()
    snapshot = factory.snapshot(None)
    run = start_response_run(
        conn,
        turn_id=turn_id,
        trigger_event_uid=trigger_event.event_uid,
        request_client=factory.create(snapshot, response_id=response_id),
        policy=deterministic_commentary_policy(
            active_subject_ref=intent.subject_ref,
            evidence_snapshot_hash=evidence_snapshot_hash(conn),
            preset_snapshot_hash=snapshot.snapshot_hash,
        ),
        response_id=response_id,
        phase="commentary",
        channel="speech",
        committed_event_bus=runtime.committed_event_bus,
    )
    if trigger_event.type == "action.dispatched":
        run.link_action(intent.subject_ref)
    # The registry the normal response path registers under
    # (`jarvis/runtime/__init__.py`), so the commentary run is reachable by id
    # like any final run; a runtime without one keeps a private registry.
    registry = runtime.response_runs if runtime.response_runs is not None else ResponseRunRegistry()
    # No subject is in scope for a fixed lifecycle phrase, so the Pre-emit
    # Gate short-circuits to its routine pass-through and hands back the
    # token `render_response` demands.
    plan = pre_emit_gate(speech)
    _emit_pre_emit_verdict(conn, plan=plan, turn_id=turn_id)
    render_response(
        record_pre_emit_token(plan.response_hash),
        plan,
        conn=conn,
        turn_id=turn_id,
        attention_channel=COMMENTARY_ATTENTION_CHANNEL,
        available_surfaces=frozenset(),
        streaming_enabled=True,
        response_id=run.response_id,
        response_group_id=run.response_group_id,
        phase="commentary",
    )
    record_realtime_trace(
        "lifecycle_commentary_rendered",
        response_id=run.response_id,
        response_group_id=run.response_group_id,
        turn_id=turn_id,
        subject_ref=intent.subject_ref,
        intent_type=intent.intent_type,
        model_lead_in=speech != commentary_speech_text(intent),
    )
    # Registered last, once nothing above can still raise: an entry the
    # watcher never receives is an entry no close path can ever unregister,
    # and in the runtime registry that would be a permanently open run.
    registry.register(run)
    return _OpenCommentary(
        subject_ref=intent.subject_ref,
        run=run,
        registry=registry,
        response_hash=plan.response_hash,
    )


def _commentary_turn_is_over(runtime: JarvisRuntime, trigger_event: Event) -> bool:
    """Whether the turn this row belongs to can no longer say a line.

    True for a turn he did not speak, one that has ended and one whose answer has
    started: the later clocks of the row have nothing left to watch for.
    """
    conn = open_runtime_event_log(runtime.runtime_paths.event_log)
    try:
        turn = _commentary_turn(conn, trigger_event)
        return turn is None or _turn_over_or_answering(conn, turn[0])
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()


def _hold_until(clock: Clock, said_at_ms: int, after_s: float, stop: threading.Event) -> bool:
    """Wait on ``clock`` until ``after_s`` past his words; ``False`` if shutdown came first."""
    return not clock.wait(stop, max(said_at_ms / 1000 + after_s - clock.time(), 0.0))


def _open_commentary_in_worker_thread(  # noqa: C901, PLR0911 - one early return per suppression rule
    runtime: JarvisRuntime,
    *,
    trigger_event: Event,
    stop: threading.Event | None = None,
    after_s: float | None = None,
    still: bool = False,
) -> _OpenCommentary | None:
    """Decide and deliver one row's commentary on a worker thread.

    The row is the ``action.dispatched`` of a tool that is not quiet (ADR 0136), Jev's
    ``route.tool_predicted`` for the line (ADR 0140; it speaks at once, before any tool) or the
    ``utterance.received`` that carries his words; the second one is the clock
    of ADR 0116 and waits ``after_s`` (default :data:`_COMMENTARY_AFTER_S`) after
    his words before it looks at anything. ``still`` makes it a follow-up line
    of a wait that goes on (ADR 0121), which may be any of the turn's lines up
    to :data:`_COMMENTARY_MAX_LINES`; the first line needs the turn to have none.

    Opens its own connection for the same ``check_same_thread`` reason
    :func:`_drive_turn_in_worker_thread` does, and for a second one: the
    projection rebuild and the four appends must not stall the event loop
    the TTS watcher polls on.

    Returns ``None`` — writing nothing at all — when the row maps to no D6
    intent (a quiet tool), when the turn is not one he spoke
    (typed, GPT-Live, system), when the turn has ended or its answer has started
    by the time the wait is over, when this turn already said its lines, or when
    a confirmation is still awaiting an answer. The ended and answer-started
    checks are the last thing before the line is rendered. An answer that begins
    after them is the media owner's to resolve: it drops a line that has not
    started playing (ADR 0121). ``stop`` ends the wait early at shutdown.
    """
    clock = trigger_event.type == "utterance.received"
    stop = stop or threading.Event()
    conn = open_runtime_event_log(runtime.runtime_paths.event_log)
    try:
        clock_s = _COMMENTARY_AFTER_S if after_s is None else after_s
        if clock and not _hold_until(runtime.clock, trigger_event.ts_epoch_ms, clock_s, stop):
            return None  # before the lookup below: the claim row lands just after his words
        turn = _commentary_turn(conn, trigger_event)
        if turn is None:
            return None
        turn_id, user_text, said_at_ms = turn
        tool_name: str | None = None
        tool_depth: str | None = None
        bound_id = _latest_id(conn)
        if not clock:
            row = conn.execute(
                _SELECT_ACTION_TOOL_NAME_SQL, (trigger_event.payload.get("action_id"),),
            ).fetchone()
            if row is not None:
                bound_id, tool_name, tool_depth = row
        intent = commentary_intent_for(
            trigger_event,
            user_text=user_text,
            tool_name=tool_name,
            tool_depth=tool_depth,
            still=still,
            pick=secrets.choice,
        )
        if intent is None:  # a quiet tool
            return None
        line = None
        # The model's own line replaces the first line only, and not a deep search's: that
        # fixed line carries the wait.
        if not still and not (tool_name == "web_search" and tool_depth == "deep"):
            lead_in = conn.execute(_SELECT_LEAD_IN_SQL, (turn_id, bound_id)).fetchone()
            line = lead_in_speech_text(lead_in[0] if lead_in is not None else None)
        # A quick answer needs no lead-in: give it until the floor to begin. Jev's predicted
        # group (ADR 0140) is a read or a draft, never quick, and speaks at once.
        if trigger_event.type == "action.dispatched" and not _hold_until(
            runtime.clock, said_at_ms, _COMMENTARY_EARLIEST_S, stop,
        ):
            return None
        with _COMMENTARY_LOCK:
            if _turn_commentary_count(conn, turn_id) >= (_COMMENTARY_MAX_LINES if still else 1):
                # The turn's lines are said. Checked after the origin filter and
                # before every write, so that a suppression path below never
                # consumes a slot, and under the lock, because the dispatch and
                # the clocks of one turn race.
                return None
            # ADR 0164: the shared cache folds only what was appended since the last read;
            # hand-built runtimes hold none and fold the whole log.
            projections = (
                runtime.decision_state.read(conn).projections
                if runtime.decision_state is not None
                else rebuild_projections(conn)
            )
            slot = projections.pending_confirmations.slot
            if slot is not None and slot.is_live(int(runtime.clock.time() * 1000)):
                # ADR-0014: new commentary never overwrites an unresolved
                # confirmation. The slot is globally unique and its
                # `action_snapshot` carries no `action_id`, so this is enforced
                # at the only granularity the fold supports.
                return None
            if _turn_over_or_answering(conn, turn_id) or (
                runtime.response_runs is not None
                and (
                    runtime.response_runs.turn_stopped(turn_id)
                    or runtime.response_runs.turn_text_only(turn_id)
                )
            ):
                # The stop button's mark covers a turn still queued, whose run
                # has not opened to write its response.cancelled yet. A turn that answers
                # in text only (ADR 0269) has no wait line to say.
                # Last before the render, after the slow projection rebuild: the
                # answer is out or on its way (a quick answer, or a Tier 0 turn
                # that ends before its tool's rows reach this watcher), and a
                # phrase now would talk over it or follow it.
                return None
            return _render_commentary(
                runtime,
                conn,
                intent=intent,
                trigger_event=trigger_event,
                turn_id=turn_id,
                speech=line or commentary_speech_text(intent),
            )
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()


async def _commentary_watcher(  # noqa: C901, PLR0915 - the row handlers share the watcher's state
    runtime: JarvisRuntime,
    *,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
) -> None:
    """Background task: speak the deterministic wait lines of a turn he spoke.

    One cursor over ``action.dispatched``, ``utterance.received``,
    ``surface.speech_dropped`` plus ``surface.playback_started``, anchored at
    the boot high-water mark exactly like ``_tts_watcher`` — a historical
    action from a previous session must never speak fake progress.
    ``surface.playback_started`` closes a phrase that reached the speaker;
    ``surface.speech_dropped`` is the media owner discarding one that had not
    started when its turn's answer opened (ADR 0121), so its run is cancelled.

    Each row that may speak gets a task of its own, because the clock row
    waits seconds and must not hold back the rows behind it. Audibility is
    decided by the per-turn count in :func:`_open_commentary_in_worker_thread`,
    not here: a turn says at most :data:`_COMMENTARY_MAX_LINES` lines, and the
    first row of that turn that actually opens one wins. The ``spoken`` set
    below is the cheaper guard in front of it — one entry per
    ``(subject, event type)``, so a repeated row is dropped before the thread
    hop and the projection rebuild. It decides work, not what is heard.

    Per-row dispatch is wrapped in a catch-all for the same reason
    ``_tts_watcher``'s is: commentary is a courtesy, and a failure to produce
    it must never stall the watcher or the turn it is commenting on.
    """
    after_id = _latest_id(runtime.conn)
    open_by_subject: dict[str, _OpenCommentary] = {}  # by response id
    spoken: set[tuple[str, str]] = set()
    tasks: set[asyncio.Task[None]] = set()
    # A playback or drop row can be read while the worker that rendered its phrase has not yet
    # handed the run back; it waits here, by response id, until `_speak` registers that run.
    early: dict[str, Event] = {}
    stop = threading.Event()

    async def _speak(ev: Event) -> None:
        # The clock row keeps watching the turn: the first line, then a follow-up
        # at each later time while the answer has still not started.
        schedule: list[tuple[float | None, bool]] = [(None, False)]
        if ev.type == "utterance.received":
            schedule = [(_COMMENTARY_AFTER_S, False)]
            schedule += [(after_s, True) for after_s in _COMMENTARY_STILL_AFTER_S]
        for after_s, still in schedule:
            work = asyncio.ensure_future(
                asyncio.to_thread(
                    _open_commentary_in_worker_thread,
                    runtime,
                    trigger_event=ev,
                    stop=stop,
                    after_s=after_s,
                    still=still,
                ),
            )
            try:
                opened = await asyncio.shield(work)
            except asyncio.CancelledError:
                # Shutdown while the worker may be mid-render: its run would otherwise be
                # lost with the cancelled await and stay open for good.
                if (late := await work) is not None:
                    open_by_subject[late.run.response_id] = late
                raise
            if opened is not None:
                open_by_subject[opened.run.response_id] = opened
                if (arrived := early.pop(opened.run.response_id, None)) is not None:
                    await _commentary_closed(runtime, open_by_subject, arrived, None)
            elif len(schedule) > 1 and (
                stop.is_set() or await asyncio.to_thread(_commentary_turn_is_over, runtime, ev)
            ):
                return

    async def _on_row(ev: Event) -> None:
        try:
            if ev.type in ("surface.playback_started", "surface.speech_dropped"):
                await _commentary_closed(runtime, open_by_subject, ev, early if tasks else None)
                return
            subject = _event_action_id(ev) or ev.event_uid
            if (subject, ev.type) in spoken:
                return
            spoken.add((subject, ev.type))
            task = asyncio.create_task(_speak(ev))
            tasks.add(task)
            task.add_done_callback(_task_done)
        except Exception as exc:  # noqa: BLE001 — commentary must not crash the watcher.
            LOGGER.warning("commentary_watcher: %s raised: %r", ev.type, exc)

    def _task_done(task: asyncio.Task[None]) -> None:
        tasks.discard(task)
        if not tasks:
            early.clear()  # nothing can still be on its way to register
        if not task.cancelled() and (exc := task.exception()) is not None:
            LOGGER.warning("commentary_watcher: a line raised: %r", exc)

    LOGGER.info("commentary_watcher started (after_id=%d)", after_id)
    try:
        while True:
            new_events = _fetch_events_after(
                runtime.conn,
                after_id=after_id,
                event_types=(
                    *_COMMENTARY_ACTION_TYPES,
                    "surface.speech_dropped",
                    "surface.playback_started",
                ),
            )
            for row_id, ev in new_events:
                after_id = max(after_id, row_id)
                await _on_row(ev)
            await asyncio.sleep(poll_interval_s)
    except asyncio.CancelledError:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)  # a run mid-render lands first
        shutdown_deadline = time.monotonic() + _COMMENTARY_SHUTDOWN_BUDGET_S
        for entry in open_by_subject.values():
            with contextlib.suppress(Exception):
                _cancel_unheard_commentary(
                    runtime,
                    entry,
                    reason="shutdown",
                    deadline=shutdown_deadline,
                )
        LOGGER.info("commentary_watcher cancelled")
        raise


async def _commentary_closed(
    runtime: JarvisRuntime,
    open_by_subject: dict[str, _OpenCommentary],
    event: Event,
    early: dict[str, Event] | None,
) -> None:
    """Close (heard) or cancel (dropped) the run this row names.

    A run that is not ours yet is kept in ``early`` when a worker may still be handing it
    over (``early`` is not None); the watcher closes it as soon as it registers the run.
    """
    handler = _commentary_heard if event.type == "surface.playback_started" else _commentary_dropped
    if not await handler(runtime, open_by_subject, event) and early is not None:
        early[str(event.payload.get("response_id"))] = event


async def _commentary_dropped(
    runtime: JarvisRuntime,
    open_by_subject: dict[str, _OpenCommentary],
    event: Event,
) -> bool:
    """Cancel the commentary run whose phrase the media owner discarded unplayed.

    The answer of the turn opened before the phrase started: the phrase was
    never spoken, so its run must not stay open until teardown. False when the
    run is not (yet) one of ours.
    """
    entry = open_by_subject.pop(str(event.payload.get("response_id")), None)
    if entry is None:
        return False
    await asyncio.to_thread(
        _cancel_unheard_commentary, runtime, entry, reason="answer_started",
    )
    return True


async def _commentary_heard(
    runtime: JarvisRuntime,
    open_by_subject: dict[str, _OpenCommentary],
    event: Event,
) -> bool:
    """Close the commentary run this playback belongs to; False when it is not (yet) ours."""
    response_id = event.payload.get("response_id")
    entry = next(
        (item for item in open_by_subject.values() if item.run.response_id == response_id),
        None,
    )
    if entry is None:
        return False
    del open_by_subject[entry.run.response_id]
    await asyncio.to_thread(_complete_commentary, runtime, entry)
    return True


def _make_cancel_voice_runs(
    runtime: JarvisRuntime, *, only_turns: Callable[[str], bool] | None = None,
) -> Callable[[], None]:
    """ADR 0138: the ``() -> None`` that ends every answer still being written for the speaker.

    An exit from the surface must leave nothing that can speak after it,
    whatever its age or number. Every open run of a turn that came from Allen's
    words, that is not for a document alone, and whose policy lets its generation
    be cancelled, is cancelled as ``user_stop``; its turn is marked stopped so a run
    of it opening later is cancelled at its open, and the media owner drops what
    it had of a cancelled run. A turn submitted on a channel the speaker is silent
    for (``_TTS_SILENT_CHANNELS``) and background turns are left alone.

    A turn he spoke whose answer has not started (ADR 0269) is not cancelled: it answers in
    text only, and only a wait line or an answer already under way is ended.

    ``only_turns`` (ADR 0219) narrows it to the turns it accepts: a device's exit ends that
    device's answers, not another's.
    """
    cancel = make_response_cancel_callable(runtime)
    registry = runtime.response_runs
    event_log_path = runtime.runtime_paths.event_log

    def _from_his_words(conn: sqlite3.Connection, turn_id: str) -> bool:
        channels = [
            row[0]
            for row in conn.execute(
                "SELECT json_extract(payload_json, '$.channel') FROM events "
                "WHERE type IN ('utterance.received', 'surface.user_intent') "
                "AND json_extract(payload_json, '$.turn_id') = ?",
                (turn_id,),
            )
        ]
        return bool(channels) and not any(channel in _TTS_SILENT_CHANNELS for channel in channels)

    def _cancel_voice_runs() -> None:
        if registry is None:  # pragma: no cover - bootstrap always builds the registry
            return
        runs = [
            run
            for run in registry.open_runs()
            if run.channel != "document" and run.interrupt_policy.generation_action == "cancel"
        ]
        if not runs:
            return
        with contextlib.closing(
            open_runtime_event_log(event_log_path, deadline=time.monotonic() + 1.0),
        ) as conn:
            runs = [run for run in runs if _from_his_words(conn, run.turn_id)]
            if only_turns is not None:
                runs = [run for run in runs if only_turns(run.turn_id)]
            unanswered = {
                run.turn_id
                for run in runs
                if run.phase == "final"
                and conn.execute(_SELECT_ANSWER_STARTED_SQL, (run.turn_id,)).fetchone() is None
            }
        for turn_id in unanswered:
            registry.mark_turn_text_only(turn_id)
        runs = [run for run in runs if run.turn_id not in unanswered or run.phase != "final"]
        for run in runs:
            if run.turn_id not in unanswered:
                registry.mark_turn_stopped(run.turn_id)
        for run in runs:
            outcome = cancel(run.response_id, "generation", "user_stop")
            LOGGER.info(
                "exit: run %s of turn %s cancelled -> %s", run.response_id, run.turn_id, outcome,
            )

    return _cancel_voice_runs


def _say_conversation_line(
    runtime: JarvisRuntime, turn_id: str, reason: str, text: str, phrase: str | None = None,
) -> None:
    """ADR 0102: one fixed line back to 「等我一下」 or a dismissal, no model and no turn.

    ``phrase`` is the exact line when the caller has built it (a reminder, ADR 0179); otherwise
    one wording of ``conversation.<reason>`` in the language of ``text``.

    The words make no turn, so a ``surface.conversation_words`` row is the
    run's trigger. Generation is done when the run opens, so it completes
    before render like a final answer (ADR-0008 D1). Runs on its own thread:
    the capture thread that heard the words must not wait on SQLite.
    """
    factory = runtime.llm_session_factory
    try:
        conn = open_runtime_event_log(runtime.runtime_paths.event_log)
        try:
            trigger = emit_event(
                conn,
                type="surface.conversation_words",
                payload={"turn_id": turn_id, "reason": reason, "transcript": text},
                committed_event_bus=runtime.committed_event_bus,
            )
            if phrase is None:
                said_in = lang.reply_language(
                    text, str(runtime.config.get("reply_language", "follow")),
                )
                phrase = secrets.choice(lang.variants(f"conversation.{reason}", said_in))
            plan = pre_emit_gate(phrase)
            response_id = new_response_id()
            snapshot = factory.snapshot(None)
            run = start_response_run(
                conn,
                turn_id=turn_id,
                trigger_event_uid=trigger.event_uid,
                request_client=factory.create(snapshot, response_id=response_id),
                policy=deterministic_commentary_policy(
                    active_subject_ref=trigger.event_uid,
                    evidence_snapshot_hash=evidence_snapshot_hash(conn),
                    preset_snapshot_hash=snapshot.snapshot_hash,
                ),
                response_id=response_id,
                phase="commentary",
                channel="speech",
                committed_event_bus=runtime.committed_event_bus,
            )
            _emit_pre_emit_verdict(conn, plan=plan, turn_id=turn_id)
            run.mark("finalizing")
            _commentary_terminalizer(runtime).complete(run.facts, response_hash=plan.response_hash)
            run.mark("completed")
            render_response(
                record_pre_emit_token(plan.response_hash),
                plan,
                conn=conn,
                turn_id=turn_id,
                attention_channel=COMMENTARY_ATTENTION_CHANNEL,
                available_surfaces=frozenset(),
                streaming_enabled=True,
                response_id=run.response_id,
                response_group_id=run.response_group_id,
                phase="commentary",
            )
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - a courtesy line never breaks capture
        LOGGER.warning("conversation line failed (%s) turn_id=%s", reason, turn_id, exc_info=True)


def _final_recognizer(
    runtime: _ConfigHost, sensevoice: voice_asr.SenseVoiceRecognizer,
) -> voice_asr.AsrRecognizer:
    """``realtime.final_asr``: SenseVoice, Whisper as 言文 hears (proposal), or both (ADR 0132)."""
    realtime = runtime.config.get("realtime")
    choice = realtime.get("final_asr") if isinstance(realtime, Mapping) else None
    if choice == "hybrid":
        # A loop is heard as nothing, so SenseVoice's words stand: a hotter retry took 4.6 s on
        # his laughter and still gave 886 characters of "beginning," (2026-10-03 15:56).
        # ADR 0150: the Chinese and English passes hear a short word list.
        listed = realtime.get("final_asr_terms") if isinstance(realtime, Mapping) else None
        terms = [str(t) for t in listed if str(t).strip()] if isinstance(listed, list) else []
        words = (lambda: terms) if terms else None
        whisper_zh = whisper_ears(language="zh", terms=words, retry_loops=False)
        whisper_en = whisper_ears(language="en", terms=words, retry_loops=False)
        if whisper_zh is None or whisper_en is None:
            LOGGER.warning("realtime.final_asr: hybrid needs mlx-whisper; hearing with SenseVoice")
            return sensevoice
        LOGGER.info("voice turns of 1 s or more hear with Whisper; SenseVoice keeps the rest")
        return voice_asr.HybridFinalRecognizer(
            sensevoice=sensevoice,
            whisper_zh=whisper_zh,
            whisper_en=whisper_en,
            whisper_command=whisper_ears(language="zh", prompt=COMMAND_PROMPT),
        )
    if choice != "whisper":
        return sensevoice
    dictation_config = runtime.config.get("dictation") or {}
    vocab_path = Path(str(dictation_config.get("vocab_path", "")))
    whisper = whisper_ears(terms=functools.partial(load_user_terms, vocab_path))
    if whisper is None:
        LOGGER.warning("realtime.final_asr: whisper needs mlx-whisper; hearing with SenseVoice")
        return sensevoice
    LOGGER.info("voice turns hear with Whisper; SenseVoice keeps the partials")
    return voice_asr.WhisperFinalRecognizer(whisper=whisper, partials=sensevoice)


class _ConfigHost(Protocol):
    """A host of which the voice builders read only the loaded config."""

    @property
    def config(self) -> Mapping[str, Any]: ...


class _PipelineHost(_ConfigHost, Protocol):
    """What the voice pipeline's builder reads from its host: a runtime's, or a terminal's."""

    @property
    def runtime_paths(self) -> _EventLogPath: ...

    @property
    def memory(self) -> MemorySettings | None: ...

    @property
    def voice_cues(self) -> VoiceCues | None: ...


def _build_voice_pipeline(
    runtime: _PipelineHost,
    *,
    broadcaster: InherentBroadcaster,
    sensevoice_dir: Path,
    emit: Callable[[Mapping[str, object], Mapping[str, str]], Event] | None = None,
    voiceprint_model: Path | None = None,
) -> voice_pipeline.VoicePipeline:
    """Construct the L5 :class:`VoicePipeline` with a fresh-conn factory.

    ADR-0005 §4.2 / §5.1 — the pipeline runs on the wake or PTT worker
    thread, never the event loop, so the conn factory MUST open a new
    SQLite connection per call (``check_same_thread`` invariant).

    A voice terminal (ADR 0172) has no log of its own to commit to: it passes ``emit``, which
    sends the utterance to the brain, and the audio it keeps stays on its own disk.

    With ``voiceprint_model`` (ADR 0286) a turn is embedded beside its final ASR; a model file
    that is not there means no voiceprint.
    """
    # realtime.final_asr_language pins SenseVoice's language, and with it the hybrid's Whisper.
    realtime = runtime.config.get("realtime")
    pinned = realtime.get("final_asr_language") if isinstance(realtime, Mapping) else None
    sensevoice = voice_asr.SenseVoiceRecognizer(
        model_dir=sensevoice_dir, language=str(pinned or "") or None,
    )
    recognizer = _final_recognizer(runtime, sensevoice)
    db_path = runtime.runtime_paths.event_log
    memory = runtime.memory
    artifacts_dir = memory.audio_dir if memory is not None and memory.retain_audio else None
    return voice_pipeline.VoicePipeline(
        conn_factory=None if emit is not None else lambda: open_runtime_event_log(db_path),
        emit=emit,
        recognizer=recognizer,
        broadcaster=broadcaster,
        artifacts_dir=artifacts_dir,
        cues=runtime.voice_cues,
        voiceprints=None if voiceprint_model is None else load_extractor(voiceprint_model),
    )


def _build_echo_canceller(runtime: _ConfigHost) -> voice_aec.EchoCanceller | None:
    """One canceller for the streaming player and the mic ingress.

    ``echo_cancellation`` is ``auto`` (the default: on for any microphone but the
    reSpeaker, which does its own), ``true`` (always on) or ``false`` (none built).
    """
    realtime_raw = runtime.config.get("realtime")
    realtime = realtime_raw if isinstance(realtime_raw, Mapping) else {}
    ingress_raw = realtime.get("single_audio_ingress")
    ingress = ingress_raw if isinstance(ingress_raw, Mapping) else {}
    mode = ingress.get("echo_cancellation", "auto")
    if mode is False:
        return None
    # Diagnostics keep the last 8 s of mic / played / cleaned audio in memory;
    # every conversation barge-in writes them under <runtime>/aec-diagnostics.
    diagnostics = ingress.get("echo_diagnostics") is True
    return voice_aec.EchoCanceller(
        history_s=8.0 if diagnostics else 0.0, follow_device=mode is not True,
    )


def _native_streaming_player(
    *,
    echo_canceller: voice_aec.EchoCanceller | None,
    **kwargs: Any,  # noqa: ANN401 - NativeAudioStreamPlayer's keyword surface
) -> voice_tts.AudioStreamPlayer | None:
    """The native (ADR 0129) player, started; ``None`` keeps her on the Python player.

    Her voice must never disappear because of this switch: a helper that cannot
    be built, or one that does not come up, logs one warning and falls back.
    The echo canceller's far end is the helper's rendered blocks (ADR 0129).
    """
    try:
        player = voice_native_out.NativeAudioStreamPlayer(
            generation_safe=True,
            playback_tap=echo_canceller.add_playback if echo_canceller is not None else None,
            **kwargs,
        )
        started = player.start()
    except Exception as exc:  # noqa: BLE001 - the switch must fail safe
        LOGGER.warning("native voice output unavailable (%r); using the Python player.", exc)
        return None
    if not started.started:
        LOGGER.warning(
            "native voice output did not start (%s); using the Python player.",
            started.reason,
        )
        return None
    return player


def _tts_volume_kwargs(realtime: Mapping[str, Any]) -> dict[str, Any]:
    """MiniMax `voice_setting.vol` from ``realtime.tts_volume``.

    Absent or null keeps the MiniMaxWSClient signature default, the single place the
    calibrated value lives. Deliberately unvalidated like `output_device`, but with a wider
    blast radius: it is read before the builder's own try, so a non-numeric value raises out
    of `_build_tts_pipeline` and the caller drops voice input with it, where a bad
    `output_device` only degrades TTS to text-only.
    """
    tts_volume = realtime.get("tts_volume")
    return {} if tts_volume is None else {"volume": tts_volume}


def _minimax_client(
    knobs: _VoiceKnobs,
    api_key: str,
    volume_kwargs: Mapping[str, Any],
    *,
    recorder: voice_artifact_store.TtsRecorder | None,
    voice_settings: VoiceSettings | None,
) -> voice_tts.MiniMaxWSClient:
    """The MiniMax client the daemon synthesizes with: the one place its knobs are applied."""
    return voice_tts.MiniMaxWSClient(
        api_key=api_key,
        voice=knobs.tts_voice,
        model=knobs.tts_model,
        primary_endpoint=knobs.tts_primary_endpoint,
        fallback_endpoint=knobs.tts_fallback_endpoint,
        sample_rate_in=knobs.tts_sample_rate_in_hz,
        connect_timeout_s=knobs.tts_connect_timeout_s,
        first_chunk_timeout_s=knobs.tts_first_chunk_timeout_s,
        between_chunk_timeout_s=knobs.tts_between_chunk_timeout_s,
        recorder=recorder,
        voice_settings=voice_settings,
        **volume_kwargs,
    )


class _EventLogPath(Protocol):
    @property
    def event_log(self) -> Path: ...


class SpeakerHost(Protocol):
    """What the speaking half of voice reads from its host: a daemon's runtime, or a terminal's.

    A voice terminal (ADR 0172) has no :class:`JarvisRuntime`; it passes the same few values.
    """

    @property
    def config(self) -> Mapping[str, Any]: ...

    @property
    def response_flags(self) -> Wave4ResponseFlags: ...

    @property
    def runtime_paths(self) -> _EventLogPath: ...

    @property
    def conn(self) -> sqlite3.Connection: ...

    @property
    def memory(self) -> MemorySettings | None: ...

    @property
    def voice_settings(self) -> VoiceSettings | None: ...

    @property
    def response_runs(self) -> ResponseRunRegistry | None: ...


def _build_tts_pipeline(  # noqa: PLR0913 - degradation boundary
    runtime: SpeakerHost,
    broadcaster: InherentBroadcaster,
    *,
    ducker: voice_ducking.SystemAudioDucker | None = None,
    voice: _VoiceKnobs | None = None,
    echo_canceller: voice_aec.EchoCanceller | None = None,
    remote: voice_media.StreamingTTSProvider | None = None,
    network_lost_dir: Path | None = None,
) -> voice_media.StreamingTTSPipeline | None:
    """Build the speech subsystem, or ``None`` for a text-only boot.

    ``remote`` (ADR 0172) is a voice terminal's provider, whose sessions run on the brain,
    which holds the key: the media actor is built around it, with no key here.
    ``network_lost_dir`` is where a terminal keeps the audio of the cut-off line between runs,
    so a start does not pay the brain for the same sentence again.

    ADR-0005 §5.3 — the env var is the sole credential source for the MiniMax WebSocket.
    Without it we skip the whole subsystem. So does every other way the media actor cannot
    start (an invalid ``realtime.streaming_output``, the event-log features off, no TTS
    endpoint, a startup error): there is no second speech path to fall back to.

    The optional ``ducker`` is the :class:`SystemAudioDucker` the media actor takes
    its output lease from.
    """
    knobs = _VoiceKnobs() if voice is None else voice
    api_key = "" if remote is not None else os.environ.get("MINIMAX_API_KEY")
    if remote is None and not api_key:
        LOGGER.warning(
            "MINIMAX_API_KEY unset; skipping TTS subsystem (text path only).",
        )
        return None
    realtime_raw = runtime.config.get("realtime")
    realtime = realtime_raw if isinstance(realtime_raw, Mapping) else {}
    volume_kwargs = _tts_volume_kwargs(realtime)
    # Passed straight through to sd.OutputStream, which maps a name to a device
    # index itself. Absent from CoreAudio's list: the system default (ADR 0054).
    output_device = _output_or_default(realtime.get("output_device"))
    # ADR 0052: the Settings page's voice volume, applied in the player.
    playback_volume = float(realtime.get("playback_volume") or 1.0)
    streaming_raw = realtime.get("streaming_output")
    streaming = streaming_raw if isinstance(streaming_raw, Mapping) else {}
    try:
        media_config = voice_media.streaming_media_config_from_mapping(streaming)
    except ValueError as exc:
        LOGGER.warning(
            "realtime.streaming_output config invalid (%s); this boot will not speak.",
            exc,
        )
        return None
    # THE output rate, for the provider's resampler and the player.
    # ``realtime.streaming_output.canonical_sample_rate_hz`` is its only source:
    # a second constant here could only ever agree with it or silently disable
    # streaming. The default (48 kHz) is the macOS built-in device rate, so
    # CoreAudio is not forced into a hardware-rate switch on every play — which
    # was producing audible pops for any other app sharing the speaker.
    output_sample_rate_hz = media_config.canonical_sample_rate_hz

    # ADR 0120: with the diagnostics switch on, her audio is kept beside the recordings.
    recorder = (
        voice_artifact_store.TtsRecorder(runtime.memory.audio_dir)
        if remote is None
        and diagnostics_flag(runtime.config, "record_tts_audio")
        and runtime.memory is not None
        else None
    )
    provider: voice_media.StreamingTTSProvider = (
        remote
        if remote is not None
        else _minimax_client(
            knobs, api_key or "", volume_kwargs,
            recorder=recorder, voice_settings=runtime.voice_settings,
        )
    )
    if not (realtime.get("enabled") is True and provider.streaming_candidate_count > 0):
        LOGGER.warning(
            "streaming speech cannot run (realtime.enabled is off, or no TTS endpoint is "
            "configured); this boot will not speak.",
        )
        return None
    player = (
        _native_streaming_player(
            echo_canceller=echo_canceller,
            sample_rate_hz=output_sample_rate_hz,
            ring_seconds=media_config.ring_seconds,
            device=output_device,
            volume=playback_volume,
        )
        if streaming.get("native_player") is True
        else None
    ) or voice_tts.AudioStreamPlayer(
        sample_rate_hz=output_sample_rate_hz,
        ring_seconds=media_config.ring_seconds,
        lazy_open=True,
        generation_safe=True,
        # 2048 frames (43 ms) of slack: with 1024 the Python callback still
        # missed CoreAudio's cycle under load (~40 overloads a minute on the
        # Multi-Output device, 2026-10-02), each one a pop in her voice.
        blocksize=2048,
        # The host buffer behind it: final ASR and turn start still stall the
        # callback past 43 ms when Allen stops talking, a pop if she is speaking.
        latency=0.12,
        device=output_device,
        playback_tap=echo_canceller.add_playback if echo_canceller is not None else None,
        volume=playback_volume,
    )
    text_registry = runtime.response_runs
    try:
        return voice_media.StreamingTTSPipeline(
            provider=provider,
            player=player,
            conn_factory=lambda: open_runtime_event_log(runtime.runtime_paths.event_log),
            boot_high_water_id=latest_id(runtime.conn),
            config=(
                media_config if network_lost_dir is None
                else dataclasses.replace(media_config, network_lost_cache_dir=network_lost_dir)
            ),
            broadcaster=broadcaster,
            ducker=ducker,
            foreground_decision_callable=make_foreground_decision_callable(
                wait_for_lane=runtime.response_flags.slow_results,
            ),
            silent_intent_channels=_TTS_SILENT_CHANNELS,
            speaks_turn=(
                None if text_registry is None
                else lambda _conn, turn_id: not text_registry.turn_text_only(turn_id)
            ),
        )
    except voice_media.StreamingMediaStartupError as exc:
        LOGGER.warning(
            "realtime.streaming_output startup failed in %s (output device %s); "
            "this boot is text-only.",
            exc.phase,
            exc.device_disposition,
        )
    except Exception as exc:  # noqa: BLE001 - the speech subsystem fails to text-only
        LOGGER.warning(
            "realtime.streaming_output startup failed (%r); this boot is text-only.",
            exc,
        )
    if isinstance(player, voice_native_out.NativeAudioStreamPlayer):
        player.stop()
    return None


def _build_brain_voice(
    runtime: JarvisRuntime, knobs: _VoiceKnobs,
) -> terminal_voice.BrainVoice | None:
    """What lets a brain speak to a voice terminal: the key, and the answer's rows (ADR 0172).

    ``None`` without ``MINIMAX_API_KEY``: as on one machine, no key is no speech, and the
    terminal is told so when it connects.
    """
    api_key = os.environ.get("MINIMAX_API_KEY")
    if not api_key:
        LOGGER.warning("MINIMAX_API_KEY unset; a voice terminal will not be spoken to.")
        return None
    realtime_raw = runtime.config.get("realtime")
    realtime = realtime_raw if isinstance(realtime_raw, Mapping) else {}
    recorder = (
        voice_artifact_store.TtsRecorder(runtime.memory.audio_dir)
        if diagnostics_flag(runtime.config, "record_tts_audio") and runtime.memory is not None
        else None
    )
    client = _minimax_client(
        knobs, api_key, _tts_volume_kwargs(realtime),
        recorder=recorder,
        voice_settings=runtime.voice_settings,
    )
    return terminal_voice.BrainVoice(
        client,
        _SpokenRows(
            runtime.conn,
            None if runtime.response_runs is None else runtime.response_runs.turn_text_only,
        ),
    )


def _phone_barge_in(runtime: JarvisRuntime, device: str) -> str:
    """The local barge-in's generation interrupt, aimed at ``device``'s open answer alone."""
    only_turns = device_voice.device_turns(runtime.runtime_paths.event_log, device)
    return make_barge_in_interrupt_callable(runtime, only_turns=only_turns)("phone_barge")


def _build_phone_speech(
    runtime: JarvisRuntime, knobs: _VoiceKnobs, rows: _PhoneRows,
) -> PhoneSpeech | None:
    """What gives a voice phone its own media actor: the key and the streaming speech config.

    ``None`` without ``MINIMAX_API_KEY`` or without the streaming pipeline's own preconditions
    (``_build_tts_pipeline``'s): the phone is then told ``voice: false`` and gets rows only.
    The actor runs at the phone's 32 kHz so nothing is resampled, and without the Mac's
    fallbacks: ``say`` would speak on this machine's speaker, and a cut-off line is the
    Mac's to prefetch.
    """
    api_key = os.environ.get("MINIMAX_API_KEY")
    if not api_key:
        LOGGER.warning("MINIMAX_API_KEY unset; a phone will not be spoken to.")
        return None
    realtime_raw = runtime.config.get("realtime")
    realtime = realtime_raw if isinstance(realtime_raw, Mapping) else {}
    streaming_raw = realtime.get("streaming_output")
    streaming = streaming_raw if isinstance(streaming_raw, Mapping) else {}
    try:
        base = voice_media.streaming_media_config_from_mapping(streaming)
    except ValueError as exc:
        LOGGER.warning("realtime.streaming_output config invalid (%s); a phone gets no voice.", exc)
        return None
    if realtime.get("enabled") is not True:
        LOGGER.warning("streaming speech is off; a phone gets no voice.")
        return None
    config = dataclasses.replace(
        base,
        canonical_sample_rate_hz=PHONE_SAMPLE_RATE_HZ,
        enable_macos_say_fallback=False,
        prefetch_network_lost_line=False,
    )
    volume_kwargs = _tts_volume_kwargs(realtime)
    event_log_path = runtime.runtime_paths.event_log

    def build(
        player: PhoneAudioStreamPlayer,
        speaks_turn: Callable[[sqlite3.Connection, str], bool],
        boot_high_water_id: int,
    ) -> voice_media.StreamingTTSPipeline | None:
        provider = _minimax_client(
            knobs, api_key, volume_kwargs,
            recorder=None, voice_settings=runtime.voice_settings,
        )
        try:
            return voice_media.StreamingTTSPipeline(
                provider=provider,
                player=player,
                conn_factory=lambda: open_runtime_event_log(event_log_path),
                boot_high_water_id=boot_high_water_id,
                config=config,
                foreground_decision_callable=make_foreground_decision_callable(
                    wait_for_lane=runtime.response_flags.slow_results,
                ),
                start_player=False,
                speaks_turn=speaks_turn,
            )
        except Exception:
            LOGGER.exception("a phone's media actor could not start")
            return None

    return PhoneSpeech(
        build=build, rows=rows, ring_seconds=config.ring_seconds, event_log=event_log_path,
    )


def _build_phone_hub(
    runtime: JarvisRuntime, knobs: _VoiceKnobs, *, set_quiet: Callable[[str], None] | None,
) -> tuple[PhoneHub, Callable[[str], int | None]] | None:
    """The phone's conversation socket (ADR 0209), on every host that listens.

    ``None`` where the host has nowhere to write a phone's words. A voice phone gets its own
    media actor when the host can speak; barge-in and cancel reach only that device's turns.
    Words it hears over her are judged with the Mac's own hooks (ADR 0216,
    :func:`_local_listen_ports`) and ``set_quiet``, the daemon's, which the caller passes in.
    The second value says which log row a device's actor was built at (``None``: it has none),
    for :func:`_phone_ends_watcher`.
    """
    events = runtime.phone_events
    if events is None:
        return None
    rows = _PhoneRows(runtime.conn)
    speech = _build_phone_speech(runtime, knobs, rows)
    registry = runtime.response_runs
    mine = _local_listen_ports(runtime, None)
    hub = PhoneHub(
        events=events,
        rows=rows,
        open_voice=None if speech is None else speech.open,
        barge_in=None if registry is None else functools.partial(_phone_barge_in, runtime),
        cancel_turn=None if registry is None else make_turn_cancel_callable(runtime),
        supersede_turn=None if registry is None else make_continuation_callable(runtime),
        pick_earlier=None if registry is None else make_compose_callable(runtime),
        ask_words=mine.ask_words,
        note_words=mine.note_words,
        begin_line=mine.begin_line,
        recent_speech=mine.recent_speech,
        set_quiet=set_quiet,
        heard_elsewhere=lambda device, _turn_id, text: device_voice.heard_elsewhere(
            runtime.runtime_paths.event_log, device, text,
        ),
        voiceprints=runtime.voiceprints,
        check_attachments=(
            None if runtime.attachments is None else runtime.attachments.check_for_turn
        ),
    )
    return hub, (lambda _device: None) if speech is None else speech.owner_boot


def _per_device[T](make: Callable[[str], T]) -> Callable[[str], T]:
    """``make`` for a device, built on first use and kept: a terminal's hooks are asked often."""
    made: dict[str, T] = {}

    def get(device: str) -> T:
        if device not in made:
            made[device] = make(device)
        return made[device]

    return get


def _build_brain_listening(
    runtime: JarvisRuntime,
    hub: TerminalHub,
    *,
    controls: voice_controls.VoiceControls,
    set_conversation: Callable[[bool, str], None],
    set_quiet: Callable[[str], None],
) -> terminal_listen.BrainListening | None:
    """What lets a brain take a voice terminal's listening: its words, its holds, its turns.

    The brain's half of each capture-session callback is the one a daemon runs for its own
    session (:func:`_local_listen_ports`); only supersede and relate differ, since the audio
    they drop or stop is on the terminal that heard the line (ADR 0172). The voice-driven
    cancels (interrupt, supersede, cancel_runs) and ``heard_elsewhere`` take the terminal's
    name and act on the turns it opened alone (ADR 0219); ``hold_runs`` stays global.
    ``None`` without the brain's log, where an utterance has nowhere to go.
    """
    if hub.events is None:
        return None
    listening = terminal_listen.BrainListening(hub, hub.events)
    mine = _local_listen_ports(runtime, None)
    registry = runtime.response_runs
    if runtime.oneshot is not None and registry is not None:
        # ADR 0139: a correction stops the answer that is audible, on the terminal that heard it.
        runtime.oneshot.relate = make_relation_supersede_callable(
            runtime, listening.drop_unspoken, lambda: listening.stop_output("correction"),
        )

    def _hold_runs(held: bool) -> None:  # noqa: FBT001 - the capture side's one bit
        if mine.hold_runs is not None:
            mine.hold_runs(held)
        if held and mine.warm is not None:
            mine.warm()  # his words will end in a request; have its connection open by then

    # ADR 0219: a terminal's voice acts on the turns that terminal opened, nothing else's.
    event_log_path = runtime.runtime_paths.event_log
    barge_in = _per_device(
        lambda device: make_barge_in_interrupt_callable(
            runtime, only_turns=device_voice.device_turns(event_log_path, device),
        ),
    )
    compose = make_compose_callable(runtime)
    supersede = _per_device(
        lambda device: make_supersede_unspoken_callable(
            runtime, listening.drop_unspoken,
            only_turns=device_voice.device_turns(event_log_path, device),
            # ADR 0278: the device's pending sentence is composed into the next one at its
            # utterance, so the sweep leaves it to that.
            leave=lambda turn_id: compose(device, turn_id, None),
        ),
    )
    cancel_runs = _per_device(
        lambda device: _make_cancel_voice_runs(
            runtime, only_turns=device_voice.device_turns(event_log_path, device),
        ),
    )

    listening.hooks = terminal_listen.ListenHooks(
        ask_words=mine.ask_words,
        note_words=mine.note_words,
        begin_line=mine.begin_line,
        answer_words=mine.answer_words,
        turn_working=mine.turn_working,
        recent_speech=mine.recent_speech,
        hold_runs=_hold_runs,
        interrupt=lambda device, source: barge_in(device)(source),
        supersede=(
            (lambda device, turn_id: supersede(device)(turn_id))
            if registry is not None
            else None
        ),
        cancel_runs=(
            (lambda device: cancel_runs(device)()) if registry is not None else None
        ),
        composes_into=compose if registry is not None else None,
        take_over=make_continuation_callable(runtime) if registry is not None else None,
        heard_elsewhere=lambda device, _turn_id, text: device_voice.heard_elsewhere(
            event_log_path, device, text,
        ),
        voiceprints=runtime.voiceprints,
        set_conversation=set_conversation,
        set_quiet=set_quiet,
        controls=controls.update,
        # ADR 0183: a terminal's dictation is polished here, with the key only the brain has.
        polish=local_polisher(
            runtime.config, runtime.runtime_paths.event_log,
            load_pricing_table(repo_root() / "data" / "pricing.json"),
        ),
    )
    return listening


def _build_voice_pipeline_callable(
    pipeline: voice_pipeline.VoicePipeline,
) -> Callable[[bytes, str, str, str], Event]:
    """Adapt :meth:`VoicePipeline.run_turn` to the PTT callable shape.

    ``_submit_asr_v2`` calls it with positional
    ``(audio_bytes, turn_id, channel, language)`` and gets back the
    emitted ``utterance.received`` :class:`Event`; the pipeline
    itself is keyword-only, so this thin closure does the rewrite.

    The closure forces ``broadcast=False`` — this callable is the PTT
    path (``/inherent/asr-submit/v2``), and per ADR-0005 §6 the client
    drives its state from the HTTP response body, not from
    WS ``op:voice`` envelopes. The shared :class:`VoicePipeline`
    instance keeps its broadcaster wired for the wake path; this
    adapter just silences phase envelopes for PTT.
    """

    def _call(audio_bytes: bytes, turn_id: str, channel: str, language: str) -> Event:
        return pipeline.run_turn(
            audio_bytes=audio_bytes,
            turn_id=turn_id,
            channel=channel,
            language=language,
            broadcast=False,
        )

    return _call


class _RuntimeRoot(Protocol):
    @property
    def root(self) -> Path: ...


class ListeningHost(_ConfigHost, Protocol):
    """What the capture session is built from: a daemon's runtime, or a voice terminal's seat."""

    @property
    def runtime_paths(self) -> _RuntimeRoot: ...


@dataclasses.dataclass(frozen=True)
class _ListenPorts:
    """What the capture session reaches in the turn's state, and how (ADR 0172).

    On one machine every port is a direct call into the runtime (:func:`_local_listen_ports`).
    On a voice terminal each is a call over the brain link, or absent. The local halves of
    hold-output, stop-speaking and the like (the player, the input model) stay in
    :func:`_spawn_single_ingress_session`, which runs wherever the microphone is.
    """

    answer_words: Callable[[str, str, str], None] | None = None
    turn_working: Callable[[], bool] | None = None
    recent_speech: Callable[[], str] | None = None
    ask_words: Callable[[str, str, str, bool, bool], str | None] | None = None
    note_words: Callable[[str, str, str, bool, bool], None] | None = None
    begin_line: Callable[[str, str, str, bool, bool], None] | None = None
    interrupt: Callable[[str], str] | None = None
    """Cancel the answer still being written (the generation half of stop-speaking)."""
    hold_runs: Callable[[bool], None] | None = None
    """The run-hold half of hold-output."""
    warm: Callable[[], None] | None = None
    """Open the model connection a request is about to need."""
    supersede_unspoken: Callable[[str], None] | None = None
    cancel_voice_runs: Callable[[], None] | None = None
    heard_elsewhere: Callable[[str, str], bool] | None = None
    """``(turn_id, text) -> bool``: another device recorded these words just now (ADR 0219)."""
    voiceprints: VoiceprintBook | None = None
    """The owner's profiles, for a session that judges its own words (ADR 0286)."""
    judged_on_send: bool = False
    """The brain judges the words as it receives them, so the ports above that only the judge
    reaches (``ask_words``, ``note_words``, ``begin_line``, ``recent_speech``,
    ``heard_elsewhere``, ``supersede_unspoken``) are the brain's own (ADR 0277)."""
    cues: VoiceCues | None = None
    ambient: AmbientSounds | None = None


def _local_listen_ports(
    runtime: JarvisRuntime,
    streaming: voice_media.StreamingTTSPipeline | None,
) -> _ListenPorts:
    """The ports of a daemon that holds the turn itself: direct calls into ``runtime``.

    Its voice acts on the host's own turns only (ADR 0219); ``hold_runs`` stays global.
    """
    oneshot = runtime.oneshot
    if oneshot is not None and streaming is not None and runtime.response_runs is not None:
        # ADR 0139: a supplement or correction of the line before it acts on that turn.
        oneshot.relate = make_relation_supersede_callable(
            runtime,
            streaming.drop_unspoken,
            lambda: streaming.stop_foreground_output(None, reason="correction"),
        )
    registry = runtime.response_runs
    # ADR 0219: the Mac's mic acts on the host's own turns; a paired phone's are never touched.
    host_turns = device_voice.host_turns(runtime.runtime_paths.event_log)

    def _hold_runs(held: bool) -> None:  # noqa: FBT001 - the capture side's one bit
        # ADR 0053: while Allen's words are coming in, no run completes.
        if registry is not None:
            registry.hold_completion(held=held)

    def _turn_working() -> bool:
        # ADR 0102: conversation mode's quiet clock does not run while a turn works.
        # Runs on the session's capture worker, never the audio callback; the
        # deadline bounds the open so a locked log cannot stall capture.
        try:
            conn = open_runtime_event_log(
                runtime.runtime_paths.event_log, deadline=time.monotonic() + 0.25,
            )
        except sqlite3.Error:
            return False
        try:
            return any_turn_in_flight(
                conn, since_ms=int(time.time() * 1000) - _TURN_WORKING_WINDOW_MS,
            )
        except sqlite3.Error:
            return False
        finally:
            conn.close()

    def _recent_speech() -> str:
        # What she said in the last minute, to tell her own voice in the mic
        # from Allen's words over her. Same worker and open as _turn_working.
        conn = open_runtime_event_log(
            runtime.runtime_paths.event_log, deadline=time.monotonic() + 0.25,
        )
        try:
            rows = conn.execute(
                "SELECT coalesce(json_extract(payload_json, '$.voice_text'), "
                "json_extract(payload_json, '$.text')) FROM events "
                "WHERE type IN ('surface.response_chunk', 'surface.response_emitted') "
                "AND ts_epoch_ms >= ? ORDER BY id",
                (int(time.time() * 1000) - _RECENT_SPEECH_WINDOW_MS,),
            )
            return " ".join(text for (text,) in rows if text)
        finally:
            conn.close()

    return _ListenPorts(
        answer_words=lambda turn_id, reason, text: threading.Thread(
            target=_say_conversation_line,
            args=(runtime, turn_id, reason, text),
            name="conversation-line",
            daemon=True,
        ).start(),
        turn_working=_turn_working,
        recent_speech=_recent_speech,
        ask_words=(
            oneshot.ask
            if oneshot is not None and oneshot.words_enabled
            else runtime.voice_words.ask if runtime.voice_words is not None else None
        ),
        note_words=runtime.voice_words.note if runtime.voice_words is not None else None,
        begin_line=oneshot.begin if oneshot is not None else None,
        interrupt=make_barge_in_interrupt_callable(runtime, only_turns=host_turns),
        hold_runs=_hold_runs,
        warm=lambda: runtime.llm_client.warm_stream(),  # noqa: PLW0108 — looked up per call.
        supersede_unspoken=(
            make_supersede_unspoken_callable(
                runtime, streaming.drop_unspoken, only_turns=host_turns,
            )
            if streaming is not None and registry is not None
            else None
        ),
        cancel_voice_runs=(
            _make_cancel_voice_runs(runtime, only_turns=host_turns)
            if registry is not None
            else None
        ),
        heard_elsewhere=lambda _turn_id, text: device_voice.heard_elsewhere(
            runtime.runtime_paths.event_log, MAC_NODE, text,
        ),
        voiceprints=runtime.voiceprints,
        cues=runtime.voice_cues,
        ambient=runtime.ambient,
    )


def _single_ingress_activation(
    runtime: _ConfigHost,
    *,
    tts: voice_media.StreamingTTSPipeline | None,
) -> _SingleIngressActivation:
    """Validate feature flags, Wave-1/2 dependencies, and strict input bounds."""
    realtime_raw = runtime.config.get("realtime")
    realtime = realtime_raw if isinstance(realtime_raw, Mapping) else {}
    ingress_raw = realtime.get("single_audio_ingress")
    ingress_values = ingress_raw if isinstance(ingress_raw, Mapping) else {}
    requested = ingress_values.get("enabled") is True
    if not requested:
        return _SingleIngressActivation(
            requested=False,
            capable=False,
            reason="feature_disabled",
        )
    if realtime.get("enabled") is not True:
        return _SingleIngressActivation(
            requested=True,
            capable=False,
            reason="realtime_parent_disabled",
        )
    if ingress_values.get("backend", "sounddevice") != "sounddevice":
        return _SingleIngressActivation(
            requested=True,
            capable=False,
            reason="unsupported_input_backend",
        )
    try:
        ingress_config = voice_audio.audio_ingress_config_from_mapping(ingress_values)
        session_config = voice_session.realtime_input_session_config_from_mapping(
            ingress_values,
        )
    except ValueError as exc:
        return _SingleIngressActivation(
            requested=True,
            capable=False,
            reason=f"invalid_input_config:{exc}",
        )
    if tts is None:
        return _SingleIngressActivation(
            requested=True,
            capable=False,
            reason="streaming_output_missing",
        )
    return _SingleIngressActivation(
        requested=True,
        capable=True,
        reason="validated",
        ingress_config=ingress_config,
        session_config=session_config,
    )


def _spawn_single_ingress_session(  # noqa: C901, PLR0911, PLR0913, PLR0915 - each pre/post-device downgrade has distinct ownership semantics
    *,
    runtime: ListeningHost,
    pipeline: voice_pipeline.VoicePipeline,
    broadcaster: InherentBroadcaster,
    silero_path: Path,
    tts: voice_media.StreamingTTSPipeline | None,
    voice: _VoiceKnobs | None = None,
    mic_muted: Callable[[], bool] | None = None,
    conversation: Callable[[], bool] | None = None,
    set_conversation: Callable[[bool, str], None] | None = None,
    set_quiet: Callable[[str], None] | None = None,
    echo_canceller: voice_aec.EchoCanceller | None = None,
    ports: _ListenPorts | None = None,
) -> tuple[voice_session.DuplexVoiceSession | None, bool]:
    """Start Wave 3 or return whether a device-open attempt was made.

    ``attempted=True`` means a device open was tried, even if startup failed: a
    timed-out foreign PortAudio open may still own the default microphone.
    ``attempted=False`` means no input owner was touched, so a failed
    prerequisite/config validation leaves the boot with text and PTT upload only.

    ``ports`` is how the session reaches the turn: by default direct calls into ``runtime``,
    which must then be a daemon's :class:`JarvisRuntime`; a voice terminal (ADR 0172) passes
    its calls over the brain link.
    """
    knobs = _VoiceKnobs() if voice is None else voice
    activation = _single_ingress_activation(runtime, tts=tts)
    if not activation.requested:
        return None, False
    if not activation.capable:
        LOGGER.warning(
            "realtime.single_audio_ingress validation failed (%s); "
            "text and PTT upload only, no input owner opened.",
            activation.reason,
        )
        record_realtime_trace(
            "audio_input_activation_downgraded",
            reason=activation.reason,
            fallback="text_ptt_upload",
            input_owner_attempted=False,
        )
        return None, False
    ingress_config = activation.ingress_config
    session_config = activation.session_config
    if ingress_config is None or session_config is None:
        msg = "validated single ingress activation lacks parsed config"
        raise RuntimeError(msg)
    engine = voice_wake.MicroWakeWordEngine()
    try:
        # Model construction/download happens before PortAudio owns the mic.
        engine.start()
    except Exception:
        LOGGER.exception(
            "realtime.single_audio_ingress wake model failed before device open; "
            "text and PTT upload only.",
        )
        with contextlib.suppress(Exception):
            engine.close()
        return None, False
    try:
        # Wave 3 requires final-ASR model/stream readiness before the sole
        # device open. Feature-off and validation downgrade never execute it.
        pipeline.prewarm_input_model()
    except Exception:
        LOGGER.exception(
            "realtime.single_audio_ingress SenseVoice prewarm failed before "
            "device open; text and PTT upload only.",
        )
        with contextlib.suppress(Exception):
            engine.close()
        return None, False

    def _capability_changed(snapshot: voice_audio.InputCapabilitySnapshot) -> None:
        log = LOGGER.info if snapshot.local_capture_available else LOGGER.warning
        log(
            "audio input capability state=%s epoch=%s reason=%s "
            "wake=%s local_capture=%s ptt_upload=%s text=%s",
            snapshot.state.value,
            snapshot.stream_epoch,
            snapshot.reason,
            snapshot.wake_available,
            snapshot.local_capture_available,
            snapshot.ptt_upload_available,
            snapshot.text_available,
        )
        broadcaster.broadcast_voice_capability_sync(
            version=snapshot.version,
            state=snapshot.state.value,
            stream_epoch=snapshot.stream_epoch,
            reason=snapshot.reason,
            wake_available=snapshot.wake_available,
            local_capture_available=snapshot.local_capture_available,
            ptt_upload_available=snapshot.ptt_upload_available,
            text_available=snapshot.text_available,
            route_kind=snapshot.route_kind,
            allowed_barge_mode=snapshot.allowed_barge_mode,
        )

    ingress: voice_audio.AudioIngress | None = None
    try:
        backend = voice_backend.SoundDeviceDuplexBackend(
            input_format=voice_backend.AudioInputFormat(
                sample_rate_hz=ingress_config.canonical_sample_rate_hz,
                # ADR 0103: open up to the wake channel; a mono device opens mono.
                channels=(ingress_config.wake_input_channel or 0) + 1,
                callback_frame_samples=ingress_config.canonical_frame_samples,
            ),
            open_timeout_s=ingress_config.backend_open_timeout_s,
            close_timeout_s=ingress_config.backend_close_timeout_s,
            # ADR 0052: the Settings page's microphone; null = the system default.
            device=(runtime.config.get("realtime") or {}).get("input_device"),
        )
        ingress = voice_audio.AudioIngress(
            backend=backend,
            config=ingress_config,
            capability_sink=_capability_changed,
            echo_canceller=echo_canceller,
        )
        ingress.set_output_device(
            _output_or_default((runtime.config.get("realtime") or {}).get("output_device")),
        )
        vad = voice_audio.SileroVad(
            mode="record",
            model_path=silero_path,
            profiles=knobs.vad_profiles,
        )
        listen = (
            _local_listen_ports(cast("JarvisRuntime", runtime), tts)
            if ports is None
            else ports
        )

        def _stop_speaking() -> None:
            # ADR 0041: Allen talked over Jarvis in conversation mode. Stop
            # what is audible now, then cancel an answer still being written
            # so it cannot start speaking over his new question.
            playback = (
                tts.stop_foreground_output(None, reason="barge_in")
                if tts is not None
                else "no_streaming_player"
            )
            generation = (
                listen.interrupt("conversation_speech") if listen.interrupt is not None
                else "no_open_run"
            )
            LOGGER.info(
                "conversation barge-in: playback=%s generation=%s", playback, generation,
            )
            if echo_canceller is not None:
                # One second on, so the recording also holds what followed the onset.
                threading.Timer(1.0, _dump_echo_history, args=(echo_canceller,)).start()

        def _hold_output(held: bool) -> None:  # noqa: FBT001 - the capture side's one bit
            # ADR 0053: while Allen's words are coming in, no run completes and
            # no queued answer starts playing.
            if listen.hold_runs is not None:
                listen.hold_runs(held)
            if tts is not None:
                tts.hold_output(held=held)
            if held:
                # His words will end in a request; have its connection open by then.
                if listen.warm is not None:
                    listen.warm()
                pipeline.warm_input_model()

        def _dump_echo_history(canceller: voice_aec.EchoCanceller) -> None:
            path = canceller.dump(runtime.runtime_paths.root / "aec-diagnostics")
            if path is not None:
                LOGGER.info("echo diagnostics written: %s (%s)", path, canceller.summary())

        session = voice_session.DuplexVoiceSession(
            ingress=ingress,
            wake_engine=engine,
            vad=vad,
            pipeline=pipeline,
            broadcaster=broadcaster,
            output_active=(tts.is_output_active if tts is not None else None),
            wake_threshold=knobs.wake_threshold,
            config=session_config,
            mic_muted=mic_muted,
            conversation=conversation,
            set_conversation=set_conversation,
            set_quiet=set_quiet,
            answer_words=listen.answer_words,
            turn_working=listen.turn_working,
            recent_speech=listen.recent_speech,
            ask_words=listen.ask_words,
            note_words=listen.note_words,
            cues=listen.cues,
            begin_line=listen.begin_line,
            stop_speaking=_stop_speaking,
            hold_output=_hold_output,
            supersede_unspoken=listen.supersede_unspoken,
            cancel_voice_runs=listen.cancel_voice_runs,
            heard_elsewhere=listen.heard_elsewhere,
            voiceprints=listen.voiceprints,
            judged_on_send=listen.judged_on_send,
            yield_speaking=tts.set_yield_gain if tts is not None else None,
            pause_speaking=tts.pause_speaking if tts is not None else None,
            ambient=listen.ambient,
        )
    except Exception:
        LOGGER.exception(
            "realtime.single_audio_ingress construction failed before device open; "
            "text and PTT upload only.",
        )
        if ingress is not None:
            with contextlib.suppress(Exception):
                ingress.close()
        with contextlib.suppress(Exception):
            engine.close()
        return None, False
    try:
        start_result = session.start()
    except voice_session.VoiceSessionPreDeviceError:
        LOGGER.exception(
            "realtime.single_audio_ingress Silero prepare failed before device "
            "open; text and PTT upload only.",
        )
        with contextlib.suppress(Exception):
            session.close()
        record_realtime_trace(
            "audio_input_activation_downgraded",
            reason="session_prepare_failed_before_device",
            fallback="text_ptt_upload",
            input_owner_attempted=False,
        )
        return None, False
    except Exception:
        LOGGER.exception(
            "realtime.single_audio_ingress failed after device ownership attempt; preserving "
            "text/PTT-upload only and refusing a second input owner.",
        )
        with contextlib.suppress(Exception):
            session.close()
        return None, True
    if not start_result.started:
        LOGGER.warning(
            "realtime.single_audio_ingress start failed state=%s reason=%s; "
            "preserving text/PTT-upload only and refusing a second input owner.",
            start_result.ingress.capability.state.value,
            start_result.ingress.capability.reason,
        )
        close_result = session.close()
        record_realtime_trace(
            "audio_input_activation_downgraded",
            reason=start_result.ingress.capability.reason,
            fallback="text_ptt_upload",
            input_owner_attempted=True,
            definitively_closed=close_result.definitively_closed,
        )
        return None, True
    return session, True


def _input_owner_reason(
    runtime: JarvisRuntime,
    *,
    tts: voice_media.StreamingTTSPipeline | None,
    duplex_session: voice_session.DuplexVoiceSession | None,
    attempted: bool,
) -> str:
    """Name why this boot ended up with the input owner it has.

    Re-runs :func:`_single_ingress_activation`, which is a pure read of
    ``runtime.config`` and ``tts``: cheaper once per boot than threading a
    third return value through a helper five tests construct directly.
    """
    reason = _single_ingress_activation(runtime, tts=tts).reason
    if duplex_session is not None:
        return reason
    if attempted:
        # A device open was tried and did not survive; the specific failure is
        # already an ``audio_input_activation_downgraded`` trace.
        return "single_ingress_start_failed"
    # Single ingress declined before it touched a device, for `reason`.
    return reason


def _spawn_voice_input_owners(  # noqa: PLR0913 - composition boundary dependencies
    *,
    runtime: JarvisRuntime,
    pipeline: voice_pipeline.VoicePipeline,
    broadcaster: InherentBroadcaster,
    silero_path: Path,
    tts: voice_media.StreamingTTSPipeline | None,
    voice: _VoiceKnobs | None = None,
    mic_muted: Callable[[], bool] | None = None,
    conversation: Callable[[], bool] | None = None,
    set_conversation: Callable[[bool, str], None] | None = None,
    set_quiet: Callable[[str], None] | None = None,
    echo_canceller: voice_aec.EchoCanceller | None = None,
) -> _VoiceInputOwners:
    """Start the single audio ingress, or leave the boot with text and PTT upload only."""
    knobs = _VoiceKnobs() if voice is None else voice
    duplex_session, attempted = _spawn_single_ingress_session(
        runtime=runtime,
        pipeline=pipeline,
        broadcaster=broadcaster,
        silero_path=silero_path,
        tts=tts,
        voice=knobs,
        mic_muted=mic_muted,
        conversation=conversation,
        set_conversation=set_conversation,
        set_quiet=set_quiet,
        echo_canceller=echo_canceller,
    )
    return _VoiceInputOwners(
        duplex_session=duplex_session,
        single_ingress_attempted=attempted,
        reason=_input_owner_reason(
            runtime,
            tts=tts,
            duplex_session=duplex_session,
            attempted=attempted,
        ),
    )


# --- ADR-0009 D4: supervisor sweep control plane ---------------------------


# --- ADR-0009 D5 repo observer -----------------------------------------------


def _today_local(ms: int, zone: tzinfo) -> str:
    """Epoch milliseconds as ISO 8601 in his zone, to the second."""
    return datetime.fromtimestamp(ms / 1000, zone).isoformat(timespec="seconds")


def _today_event(found: things.Occurrence, midnight_ms: int, zone: tzinfo) -> dict[str, Any]:
    """One occurrence as the Today card words it.

    An all-day one starts at the day's local midnight, so one over several days is on each.
    """
    row: dict[str, Any] = {"id": found.thing.thing_id, "title": found.thing.title}
    if found.all_day:
        row["start"] = _today_local(max(found.start_ms, midnight_ms), zone)
        row["all_day"] = True
    else:
        row["start"] = _today_local(found.start_ms, zone)
        row["end"] = _today_local(found.end_ms, zone)
    return row


def _today_todo(one: things.Thing, zone: tzinfo) -> dict[str, Any]:
    """An open thing with no hour and no day as the Today card words it.

    The id is the To Do id (``<listId>|<taskId>``) for a thing the mirror made from a task, so a
    tick on it writes To Do. A date-only due is by the end of that local day (23:59), as To Do
    keeps it.
    """
    row: dict[str, Any] = {
        "id": one.source_ref if one.source == "todo" else one.thing_id, "title": one.title,
    }
    if one.due:
        due = datetime.fromisoformat(one.due)
        if due.tzinfo is None:  # a bare date
            due = due.replace(hour=23, minute=59, tzinfo=zone)
        row["due"] = due.astimezone(zone).isoformat()
    return row


def _read_today(
    event_log: Path, zone: tzinfo, now_ms: int, weather: Callable[[], dict[str, Any] | None],
) -> dict[str, Any]:
    """``GET /inherent/today`` (ADR 0055, 0299): today's events, his open to-dos and the weather.

    Events and to-dos are the ledger of his things, read on a connection of its own, so this runs
    on a worker thread. ``events`` are the open occurrences of the local day ``now_ms`` is in,
    soonest first: ``{id, title, start, end}``, or ``{id, title, start, all_day: true}`` with
    ``start`` the day's midnight. ``todos`` are the open things with no ``start_at`` and no
    ``on_day`` (a due date, or no time), in :func:`jarvis.state.things.open_things` order:
    ``{id, title, due?}``. An id is the thing's, but a to-do the mirror made from To Do keeps its
    To Do id. A thing a mail made that he has not confirmed is left out of both: the card has no
    way to mark it as his to confirm. ``weather`` is the home's line, ``None`` when it has none.
    """
    start_ms, end_ms = day_window(datetime.fromtimestamp(now_ms / 1000, zone).date(), zone)
    with contextlib.closing(open_runtime_event_log(event_log)) as conn:
        found = [
            occ for occ in things.occurrences(conn, start_ms, end_ms, zone=zone)
            if occ.thing.confirmed
        ]
        untimed = [
            one for one in things.open_things(conn)
            if one.confirmed and not (one.start_at or one.on_day)
        ]
    return {
        "events": [_today_event(one, start_ms, zone) for one in found],
        "todos": [_today_todo(one, zone) for one in untimed],
        "weather": weather(),
    }


async def _serve_today(
    event_log: Path, zone: tzinfo, weather: Callable[[], dict[str, Any] | None],
) -> dict[str, Any]:
    """:func:`_read_today` off the loop thread, at the brain's clock."""
    return await asyncio.to_thread(_read_today, event_log, zone, int(time.time() * 1000), weather)


def _tick_thing(event_log: Path, zone: tzinfo, thing_id: str, done: bool) -> None:  # noqa: FBT001 — the route's body.
    """His tick on a thing of the Today card: done closes it, not done opens it again.

    Written by him as "ticked on the desktop"; a thing already as he asked is left alone and
    nothing is written. ``LookupError`` when no thing has the id.
    """
    with contextlib.closing(open_runtime_event_log(event_log)) as conn:
        state = things.get(conn, thing_id).state
        write: dict[str, Any] = {
            "by": "him", "evidence": "ticked on the desktop", "ref": thing_id, "zone": zone,
            "actor": "user",
        }
        if done and state == "open":
            things.close(conn, thing_id, "done", **write)
        elif not done and state != "open":
            things.reopen(conn, thing_id, **write)


async def _set_todo(  # noqa: PLR0913 — the route's body, and the log and zone a thing's tick needs.
    home: Home, mirror: ThingsMirror | None, event_log: Path, zone: tzinfo, todo_id: str,
    done: bool,  # noqa: FBT001 — the route's body.
) -> None:
    """``POST /inherent/today/todo``: a thing's tick in the ledger, or a To Do task's write.

    A ``thing-<hex>`` id is the ledger's alone (:func:`_tick_thing`). Any other id is a To Do
    task's (ADR 0299): To Do is written first and the route answers by that write, then the thing
    mirrored for it follows if it can, and the mirror's next pass makes it agree if it cannot.
    Both are off the loop thread.
    """
    if todo_id.startswith(things.ID_PREFIX):
        await asyncio.to_thread(_tick_thing, event_log, zone, todo_id, done)
        return
    await asyncio.to_thread(functools.partial(home.set_todo, todo_id, done=done))
    if mirror is not None:
        await asyncio.to_thread(functools.partial(mirror.tick, todo_id, done=done))


async def _archive_mail(home: Home, ids: list[str], archive: bool) -> None:  # noqa: FBT001 — the route's body.
    """``POST /inherent/mail/archive`` and ``/unarchive``: one Gmail label change, off the loop."""
    await asyncio.to_thread(functools.partial(home.archive, ids, undo=not archive))


def _draft_deps(runtime: JarvisRuntime, home: Home | None) -> dict[str, Any]:
    """The four draft routes' callables (ADR 0148), or none while the mail page is off."""
    drafts = runtime.mail_drafts
    if home is None or drafts is None:
        return {}

    async def read(letter_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(_draft_view, home, drafts, letter_id)

    async def save(letter_id: str, subject: str, body: str) -> dict[str, Any]:
        return await asyncio.to_thread(_save_draft, home, drafts, letter_id, subject, body)

    async def send(letter_id: str, subject: str, body: str) -> None:
        await asyncio.to_thread(_send_draft, runtime, home, drafts, letter_id, subject, body)

    async def discard(letter_id: str) -> None:
        await asyncio.to_thread(drafts.discard, letter_id)

    return {
        "mail_draft_read": read,
        "mail_draft_save": save,
        "mail_draft_send": send,
        "mail_draft_discard": discard,
    }


def _read_day(  # noqa: PLR0913 — the route's stores, its zone, the day asked and the clock.
    event_log: Path, configured: tzinfo | None, memory_db: Path | None, timesink: Path | None,
    day: date | None, now_ms: int, terminal: TerminalScreen | None = None,
    attachments: Path | None = None, shipped: Path | None = None,
    weather: Callable[[date], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """``GET /inherent/day`` (ADR 0199): the day asked for, or today in the owner's zone.

    The zone is ``work_state.timezone`` else the machine's, as every other day window is cut. The
    log is read on a connection of its own, so this runs on a worker thread. ``memory_db`` (his
    talks) and ``timesink`` (his Mac) are the configured stores or None; the ``ledger:`` block
    does not gate them, and a store that is not there leaves its source in ``missing``. On a
    brain ``terminal`` is his Mac's TimeSink. His Outlook is not read here: its events are things
    in the log (ADR 0299). ``line`` and ``highlights`` (ADR 0235) are the day's line to him and
    its marked items, read from ``memory_db``. ``spans`` and ``coverage`` (ADR 0302) are the day's
    fact spans and what he was doing, cut from the items and worded by the day's latest stored
    run and his answers; before them come the day before's stretches that run past midnight
    (ADR 0313).
    ``shipped`` is the imports directory (ADR 0294): what his Mac copied to it fills in his Mac's
    lane when TimeSink cannot be read live, and gives his Claude Code sessions.
    ``summary`` is the day's steps, resting heart rate and heart rate variability from the phone
    (``None`` if the log cannot be read); ``weather`` is the day's weather card
    (``Home.day_weather_card``, kept per day), ``None`` when it has none.
    """
    zone_name, zone = resolve_zone(None, configured)
    on = datetime.fromtimestamp(now_ms / 1000, zone).date() if day is None else day
    start_ms, end_ms = day_window(on, zone)
    before_ms, after_ms = page_reach(on, zone)
    with (
        contextlib.closing(open_runtime_event_log(event_log)) as conn,
        shared_reads(before_ms=before_ms, after_ms=after_ms),
    ):
        sources = DaySources(zone, timesink, memory_db, terminal, attachments, shipped)
        line = fold_day(conn, start_ms, end_ms, now_ms, sources)
        try:
            summary: dict[str, Any] | None = day_summary(conn, start_ms, end_ms)
        except Exception:
            LOGGER.exception("day: the day's body numbers could not be read")
            summary = None
        doing = read_day_doing(conn, memory_db, on, line.items, sources, now_ms)
    told, highlights = day_notes(memory_db, on.isoformat(), line.items, zone)
    served = doing.served({item["id"] for item in line.items})
    spans = sorted([*fact_spans(line.items), *served], key=lambda s: (s["start_ms"], s["id"]))
    return {
        "date": on.isoformat(),
        "tz": zone_name,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "now_ms": now_ms,
        "items": line.items,
        "labels": line.labels,
        "missing": line.missing,
        "screen_synced_ms": line.synced,
        "line": told,
        "highlights": highlights,
        "spans": spans,
        "coverage": doing.own.doing.coverage,
        "summary": summary,
        "weather": None if weather is None else weather(on),
    }


async def _serve_day(  # noqa: PLR0913 — the route's stores, its zone, the terminal and the day.
    event_log: Path, configured: tzinfo | None, memory_db: Path | None, timesink: Path | None,
    day: date | None, *, terminal: TerminalScreen | None = None, attachments: Path | None = None,
    shipped: Path | None = None, weather: Callable[[date], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """:func:`_read_day` off the loop thread, at the brain's clock."""
    return await asyncio.to_thread(
        _read_day, event_log, configured, memory_db, timesink, day, int(time.time() * 1000),
        terminal, attachments, shipped, weather,
    )


async def _set_highlight(
    marks: HighlightMarks, day: date, item_id: str, on: bool, label: str | None,  # noqa: FBT001 — the route's argument.
) -> list[dict[str, str]]:
    """``POST /inherent/day/highlight`` (ADR 0297): his mark, written off the loop thread."""
    return await asyncio.to_thread(marks.set_item, day, item_id, on, label)


async def _answer_doing(  # noqa: PLR0913 — the route's day, span or stretch, and his words.
    answers: DoingAnswers, day: date, span_id: str | None, choice: str | None, text: str | None,
    stretch: tuple[int, int] | None,
) -> dict[str, Any]:
    """``POST /inherent/day/answer`` (ADR 0302): his answer, written off the loop thread."""
    if stretch is not None and text is not None:
        return await asyncio.to_thread(answers.answer_stretch, day, stretch, text)
    if span_id is None:
        msg = "give span_id, or start_ms and end_ms with text"
        raise ValueError(msg)
    return await asyncio.to_thread(answers.answer, day, span_id, choice, text)


def _day_sources(runtime: JarvisRuntime, memory_db: Path) -> DaySources:
    """The stores a day is folded from, as the routes that write about a day's items read them."""
    return DaySources(
        resolve_zone(None, _work_state_timezone(runtime.config))[1],
        _timesink_db_path(runtime.config),
        memory_db,
        (
            None if runtime.terminal_hub is None
            else TerminalScreen(runtime.terminal_hub.call, wait_s=TURN_WAIT_S)
        ),
        None if runtime.attachments is None else runtime.attachments.directory,
        shipped=runtime.runtime_paths.root / IMPORTS_DIRNAME,
    )


def _commit_act(event_log_path: Path, zone: tzinfo, commitment_hex: str, action: str) -> bool:
    """He confirms or cancels a thing the day line shows: ``thing-<hex>`` in the ledger.

    Whether anything was written; ``LookupError`` when no open thing has the id (none such, or
    one already closed). A cancel is closed as ``cancelled`` with the reason "not mine".
    """
    thing_id = things.ID_PREFIX + commitment_hex
    conn = open_runtime_event_log(event_log_path)
    try:
        held = things.get(conn, thing_id)
        if held.state != "open":
            msg = f"{thing_id} is {held.state}"
            raise LookupError(msg)
        if action == "confirm":
            return things.confirm(
                conn, thing_id, by="him", evidence="confirmed on the phone", zone=zone,
                actor="user",
            )
        things.close(
            conn, thing_id, "cancelled", why="not mine", by="him",
            evidence="cancelled on the phone", zone=zone, actor="user",
        )
        return True
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()


async def _act_commitment(
    event_log_path: Path, zone: tzinfo, commitment_hex: str, action: str,
) -> bool:
    """``POST /inherent/commitments/{hex}`` (ADR 0297, 0299), off the loop thread."""
    return await asyncio.to_thread(_commit_act, event_log_path, zone, commitment_hex, action)


async def _ride_act(rides: Rides, body: dict[str, Any]) -> dict[str, Any]:
    """``POST /inherent/ride`` (ADR 0224): his answer to the boarding question, off the loop."""
    return await asyncio.to_thread(rides.act, body)


def _notice_deps(
    job_mail: JobMail | None, reminders: Reminders | None, moment: Moment | None,
    alerts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The notice routes: job mail's (ADR 0155) plus the owner's fired reminders (ADR 0179).

    ``alerts`` stands in for job mail's notice routes while job mail is off (see
    :func:`_alert_deps`), so a job-alert row that is not mail (the spend card) is still served.

    A reminder card is served whatever the quiet level and the hold, ahead of the job mail's, and
    ``POST /inherent/notices/{id}`` routes a ``reminder-`` id to the reminders.
    """
    deps = _job_mail_deps(job_mail) or alerts or {}
    if reminders is None:
        return deps
    mail_read, mail_act = deps.get("notices_read"), deps.get("notice_act")

    async def read() -> dict[str, Any]:
        if mail_read is not None:
            body: dict[str, Any] = await mail_read()
        else:  # no job mail: the same answer /inherent/moment gives, plus whether sound is private
            hold = None if moment is None else await _moment_hold(moment)
            private = bool((await asyncio.to_thread(reminders.output))["private"])
            body = {"notices": [], "audio_private": private, **(hold or {"hold": None})}
        cards = await asyncio.to_thread(reminders.notices)
        pin = await asyncio.to_thread(reminders.departures.view)
        return {**body, "notices": [*cards, *body["notices"]], "departure": pin}

    async def act(notice_id: str, action: str, reaction: str | None) -> None:
        if notice_id.startswith(PIN_PREFIX):
            await asyncio.to_thread(reminders.departures.unpin, notice_id)
        elif notice_id.startswith(ID_PREFIX):
            await asyncio.to_thread(reminders.acknowledge, notice_id, action)
        elif mail_act is not None:
            await mail_act(notice_id, action, reaction)
        else:
            msg = f"no such notice: {notice_id}"
            raise LookupError(msg)

    async def departure_act(body: dict[str, Any]) -> dict[str, Any]:
        """``POST /inherent/departure`` (ADR 0203, 0205): the bus card and the notch's."""
        return await asyncio.to_thread(reminders.departures.act, body)

    return {
        **deps, "notices_read": read, "notice_act": act,
        "departure_act": departure_act,
    }


def _settings_notice_deps(
    deps: dict[str, Any], drift: SettingsDrift | None, idle: Callable[[], bool],
) -> dict[str, Any]:
    """ADR 0227: the settings-differ cards of the terminals, served and answered beside the rest.

    Choosing the terminal's values writes them to this brain's files, which a restart brings in:
    it waits for ``idle`` (no conversation, nothing playing) and happens only where a supervisor
    brings the daemon back.
    """
    if drift is None:
        return deps
    mail_read, mail_act = deps.get("notices_read"), deps.get("notice_act")

    async def read() -> dict[str, Any]:
        body: dict[str, Any] = (
            await mail_read() if mail_read is not None
            else {"notices": [], "audio_private": False, "hold": None, "departure": None}
        )
        return {**body, "notices": [*await asyncio.to_thread(drift.notices), *body["notices"]]}

    async def act(notice_id: str, action: str, reaction: str | None) -> str | None:
        if not notice_id.startswith(SETTINGS_NOTICE):
            if mail_act is None:
                msg = f"no such notice: {notice_id}"
                raise LookupError(msg)
            await mail_act(notice_id, action, reaction)
            return None
        if not await asyncio.to_thread(drift.act, notice_id, action, reaction):
            return None
        if not supervised():
            LOGGER.warning("settings: restart Jarvis to take in the settings just saved")
            return "manual"
        task = asyncio.create_task(_restart_when_idle(idle), name="settings_restart")
        _restarting.add(task)
        task.add_done_callback(_restarting.discard)
        return "idle"

    return {**deps, "notices_read": read, "notice_act": act}


def _alert_deps(
    db: Path, quiet: Callable[[], str], moment: Moment | None,
    output: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """The notice routes over the job-alert rows alone, for a daemon with job mail off (ADR 0185).

    The same functions ``JobMail.notices`` and ``JobMail.act`` run, so the quiet level, the moment
    hold and the digest rules are one pipeline.
    """

    def read() -> dict[str, Any]:
        return served_notices(
            db, quiet(), datetime.now(UTC), moment, output,
        )

    async def act(notice_id: str, action: str, reaction: str | None) -> None:
        await asyncio.to_thread(
            settle_notice, db, quiet(), datetime.now(UTC), notice_id, action, reaction,
        )

    return {"notices_read": functools.partial(asyncio.to_thread, read), "notice_act": act}


def _job_mail_deps(job_mail: JobMail | None) -> dict[str, Any]:
    """The job-mail routes' callables (ADR 0155), or none while ``job_mail`` is off (404)."""
    if job_mail is None:
        return {}

    async def act(notice_id: str, action: str, reaction: str | None) -> None:
        await asyncio.to_thread(job_mail.act, notice_id, action, reaction)

    async def delete(message_id: str) -> None:
        await asyncio.to_thread(job_mail.delete, message_id)

    async def flag(message_id: str, reaction: str) -> None:
        await asyncio.to_thread(job_mail.flag, message_id, reaction)

    async def application_add(fields: dict[str, Any]) -> str:
        return await asyncio.to_thread(job_mail.add_application, fields)

    async def application_edit(app_id: str, fields: dict[str, Any]) -> None:
        await asyncio.to_thread(job_mail.edit_application, app_id, fields)

    async def application_cancel_reminders(app_id: str) -> None:
        await asyncio.to_thread(job_mail.cancel_reminders, app_id)

    return {
        "notices_read": functools.partial(asyncio.to_thread, job_mail.notices),
        "notice_act": act,
        "jobs_read": functools.partial(asyncio.to_thread, job_mail.ledger),
        "job_delete": delete,
        "job_flag": flag,
        "application_add": application_add,
        "application_edit": application_edit,
        "application_cancel_reminders": application_cancel_reminders,
    }


async def _moment_hold(moment: Moment) -> dict[str, Any]:
    """``GET /inherent/moment`` (ADR 0163): ``{hold}``, one read of TimeSink off the loop thread."""
    return {"hold": await asyncio.to_thread(moment.client_hold)}


async def _card_act(cards: CardFeedback, card_id: str, body: dict[str, Any]) -> None:
    """``POST /inherent/cards/{id}`` (ADR 0160): one snapshot or reaction, off the loop thread."""
    await asyncio.to_thread(cards.act, card_id, body)


def _say_job_line(runtime: JarvisRuntime) -> None:
    """ADR 0155: the one fixed line for an interview or offer email, said as a conversation line."""
    _say_conversation_line(runtime, _new_turn_id(), "job_speak", "")


def _her_state(controls: voice_controls.VoiceControls) -> str:
    """ADR 0210: the Live Activity's word for her: ``conversation``, else the quiet level."""
    if controls.conversation:
        return "conversation"
    return "idle" if controls.quiet == "off" else controls.quiet


def _plan_hooks(runtime: JarvisRuntime, controls: voice_controls.VoiceControls) -> Hooks:
    """ADR 0291: how the plan watcher tells him: the quiet level, the speakers, the phone.

    It speaks as a reminder does (speech on, no live conversation, not on a call) and pushes only
    while he is away from the Mac. All of it runs on the watcher's thread.
    """
    moment, push, whereabouts = runtime.moment, runtime.push, runtime.whereabouts
    return Hooks(
        quiet=lambda: controls.quiet,
        may_speak=lambda: (
            not controls.speech_muted and not controls.conversation
            and (moment is None or moment.hold() != "call")
        ),
        private_output=lambda: bool(runtime.sound_output()["private"]),
        at_mac=lambda: whereabouts is not None and whereabouts.at_mac(),
        say=functools.partial(_say_plan_line, runtime),
        push=push.send if push is not None and push.enabled else None,
    )


def _say_plan_line(runtime: JarvisRuntime, said: str) -> None:
    """ADR 0291: one line of the plan watcher, said as a conversation line (no model, no turn)."""
    _say_conversation_line(runtime, _new_turn_id(), "plan", "", phrase=said)


def _say_reminder(runtime: JarvisRuntime, said: str) -> None:
    """ADR 0179: one reminder line, said as a conversation line (no model, no turn)."""
    _say_conversation_line(runtime, _new_turn_id(), "reminder", "", phrase=said)


async def _mail_letter(home: Home, message_id: str) -> dict[str, Any]:
    """``GET /inherent/mail/{id}`` (ADR 0148): one Gmail read, off the loop thread."""
    return await asyncio.to_thread(home.letter, message_id)


async def _mail_summary(home: Home, message_id: str) -> dict[str, Any]:
    """``GET /inherent/mail/{id}/summary`` (ADR 0148): one model call, off the loop thread."""
    return {"summary": await asyncio.to_thread(home.summary, message_id)}


def _draft_view(home: Home, drafts: MailDrafts, letter_id: str) -> dict[str, Any]:
    """``{draft: {revision, to, subject, body, by} | null}``: the letter gives ``to`` and "Re:"."""
    draft = drafts.get(letter_id)
    if draft is None:
        return {"draft": None}
    letter = home.letter(letter_id)
    subject = letter["subject"]
    return {"draft": {
        "revision": draft.revision,
        "to": letter["address"],
        "subject": draft.subject or (
            subject if subject.lower().startswith("re:") else f"Re: {subject}"
        ),
        "body": draft.body,
        "by": draft.by,
    }}


def _save_draft(
    home: Home, drafts: MailDrafts, letter_id: str, subject: str, body: str,
) -> dict[str, Any]:
    """``POST /inherent/mail/{id}/draft``: Allen's edit becomes the draft."""
    drafts.edit(letter_id, subject, body)
    return _draft_view(home, drafts, letter_id)


def _send_draft(  # noqa: PLR0913 — the route's body plus what the card needs.
    runtime: JarvisRuntime, home: Home, drafts: MailDrafts, letter_id: str, subject: str, body: str,
) -> None:
    """``POST /inherent/mail/{id}/draft/send``: save the edit, raise the send card (no model turn).

    The card is the confirmed ``gmail_send`` ADR 0033 asks for, with the letter's thread; his
    button on it is what sends. A ValueError (empty body) is the route's 400, a missing
    gmail send tool its 404.
    """
    if not body.strip():
        msg = "the draft is empty"
        raise ValueError(msg)
    view = _save_draft(home, drafts, letter_id, subject, body)["draft"]
    args = {
        "to": view["to"], "subject": view["subject"], "body": view["body"],
        "threadId": home.letter(letter_id)["thread_id"],
    }
    conn = open_runtime_event_log(runtime.runtime_paths.event_log)
    try:
        asked = request_confirmation(
            conn, cast("ToolRegistryLike", runtime.tool_registry), "mcp__gmail__gmail_send", args,
            ttl_ms=_confirmation_ttl_ms(runtime.config), lang=lang.language(),
        )
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
    if asked is None:
        msg = "gmail send is not connected"
        raise LookupError(msg)


async def _mail_read(home: Home, ids: list[str], read: bool) -> None:  # noqa: FBT001 — the route's body.
    """``POST /inherent/mail/read`` and ``/unread`` (ADR 0148): one label change, off the loop."""
    await asyncio.to_thread(functools.partial(home.mark_read, ids, unread=not read))


async def _mail_trash(home: Home, ids: list[str], trash: bool) -> None:  # noqa: FBT001 — the route's body.
    """``POST /inherent/mail/trash`` and ``/untrash`` (ADR 0148): one label change, off the loop."""
    await asyncio.to_thread(functools.partial(home.trash, ids, undo=not trash))


async def _turn_end_asks(asks: TurnEndAsks, session_id: str, text: str) -> bool | None:
    """``POST /inherent/agents/turn-end``: Jev's answer, waited for off the loop thread."""
    return await asyncio.to_thread(asks.asks, session_id, text)


async def _save_settings(settings: Settings, changes: dict[str, Any]) -> dict[str, Any]:
    """``POST /inherent/settings``: one file write, off the loop thread."""
    return await asyncio.to_thread(settings.update, changes)


_DATA_SWEEP_INTERVAL_S = 3600.0
_ATTACHMENT_DAYS = 30  # ADR 0211: unless ``attachments.retention_days`` says otherwise


def _model_takes_images(config: Mapping[str, Any]) -> bool:
    """ADR 0211: whether pictures may go to the conversation model.

    OpenAI's and Anthropic's requests carry them, and only a preset that says ``images: true``
    (the model is known to read them) is trusted with one.
    """
    llm = config.get("llm")
    if not isinstance(llm, Mapping):
        return False
    presets = llm.get("presets")
    preset = presets.get(llm.get("default_preset")) if isinstance(presets, Mapping) else None
    if not isinstance(preset, Mapping):
        return False
    provider = str(preset.get("provider") or llm.get("provider") or "openai").lower()
    return provider in ("openai", "anthropic") and preset.get("images") is True


def _lifelog(runtime: JarvisRuntime) -> Lifelog:
    """ADR 0238: the archive of his recorded days, at ``lifelog.dir`` or in the runtime root."""
    section = runtime.config.get("lifelog")
    section = section if isinstance(section, dict) else {}
    where = section.get("dir")
    directory = (
        Path(where).expanduser() if isinstance(where, str) and where.strip()
        else runtime.runtime_paths.root / LIFELOG_DIRNAME
    )
    floor = section.get("min_free_gb")
    min_free = (
        int(floor * 1024 ** 3) if isinstance(floor, (int, float)) and floor >= 0
        else DEFAULT_MIN_FREE_BYTES
    )
    return Lifelog(directory, min_free_bytes=min_free)


def _lifelog_coverage(runtime: JarvisRuntime, lifelog: Lifelog) -> Callable[[], dict[str, Any]]:
    """ADR 0292: the facts the phone decides by, read at each ask from the hub and the places."""

    def read() -> dict[str, Any]:
        hub = runtime.terminal_hub
        names = [] if hub is None else [name for name, _tools in hub.connected()]
        home: dict[str, float] | None = None
        kept = [] if runtime.memory is None else places.read_kept(runtime.memory.db_path)
        for one in reversed(kept):  # when two share the role, the newer one is the one he means
            if one.role == "home" and one.lat is not None and one.lng is not None:
                home = {"lat": one.lat, "lng": one.lng, "radius_m": one.radius_m}
                break
        return lifelog.coverage(names, home, int(time.time() * 1000))

    return read


def _shipments(runtime: JarvisRuntime) -> Shipments:
    """ADR 0294: the intake for a terminal's scheduled copies, in the imports archive."""
    section = runtime.config.get("imports")
    floor = section.get("min_free_gb") if isinstance(section, dict) else None
    min_free = (
        int(floor * 1024 ** 3) if isinstance(floor, (int, float)) and floor >= 0
        else DEFAULT_MIN_FREE_BYTES
    )
    return Shipments(runtime.runtime_paths.root / IMPORTS_DIRNAME, min_free_bytes=min_free)


def _media_dirs(runtime: JarvisRuntime) -> dict[Path, int | None]:
    """ADR 0067: the recordings, screenshots and phone attachments, each with its days to keep."""
    tools = runtime.config.get("tools") or {}
    screen = tools.get("screen") if isinstance(tools, dict) else None
    days = retention_days(screen.get("retention_days")) if isinstance(screen, dict) else None
    dirs = {runtime.runtime_paths.artifacts_root / SCREEN_ARTIFACTS_DIRNAME: days}
    sent = runtime.config.get("attachments")
    if runtime.attachments is not None:
        dirs[runtime.attachments.directory] = retention_days(
            sent.get("retention_days", _ATTACHMENT_DAYS) if isinstance(sent, dict)
            else _ATTACHMENT_DAYS,
        )
    if runtime.memory is not None:
        dirs[runtime.memory.audio_dir] = runtime.memory.audio_retention_days
    return dirs


def _link_pictures(runtime: JarvisRuntime) -> int:
    """ADR 0255: link his earlier records to the files still kept (see ``picture_files``)."""
    if runtime.attachments is None or runtime.memory is None:
        return 0
    sent = runtime.config.get("attachments")
    days = retention_days(
        sent.get("retention_days", _ATTACHMENT_DAYS) if isinstance(sent, dict)
        else _ATTACHMENT_DAYS,
    )
    linked = picture_files.link_kept_pictures(
        runtime.memory.db_path, runtime.runtime_paths.event_log, runtime.attachments, days=days,
    )
    if runtime.picture_files is not None and (client := _upload_client(runtime)) is not None:
        for attachment_id in recent_image_ids(runtime.memory.db_path, HISTORY_PICTURES):
            runtime.picture_files.start(client, attachment_id)  # the backlog, newest first
    return linked


@functools.cache
def _default_anthropic(factory: LLMSessionFactory) -> LLMClient | None:
    return (
        factory.create(factory.snapshot(factory.default_preset), response_id="pictures")
        if factory.snapshot(factory.default_preset).provider == "anthropic" else None
    )


def _upload_client(runtime: JarvisRuntime) -> LLMClient | None:
    """The client that uploads pictures: on the default preset, when that is Anthropic's."""
    return _default_anthropic(runtime.llm_session_factory)


def _start_upload(runtime: JarvisRuntime, ref: AttachmentRef) -> None:
    """ADR 0255: a picture was just saved; upload it now so the next turn finds its id."""
    client = _upload_client(runtime)
    if runtime.picture_files is not None and client is not None:
        runtime.picture_files.start(client, ref.id)


def _files_client(runtime: JarvisRuntime) -> LLMClient | None:
    """A client on an Anthropic preset, whatever preset is active, or ``None`` without one."""
    if runtime.llm_client.provider == "anthropic":
        return runtime.llm_client
    factory = runtime.llm_session_factory
    for name, spec in runtime.llm_client.get_presets().items():
        if str(spec.get("provider", "")).lower() == "anthropic":
            return factory.create(factory.snapshot(name), response_id="pictures")
    return None


def _release_pictures(runtime: JarvisRuntime, *, everything: bool = False) -> int:
    """ADR 0255: delete the uploaded copies of pictures whose file is gone (or all of them)."""
    if runtime.picture_files is None:
        return 0
    try:
        return runtime.picture_files.release(_files_client(runtime), everything=everything)
    except Exception:
        LOGGER.exception("pictures: could not delete uploaded copies")
        return 0


def _sweep_data(dirs: dict[Path, int | None], logs: Path) -> None:
    """Delete what has passed its days and cut each log past its size (ADR 0067)."""
    for directory, days in dirs.items():
        if gone := data.delete_older_than(directory, days):
            LOGGER.info("retention: deleted %d files older than %s days from %s",
                        gone, days, directory.name)
    if cut := data.rotate_logs(logs):
        LOGGER.info("retention: started %d new log files", cut)


async def _data_sweep_task(  # noqa: PLR0913 — the brain's two hooks, a terminal's gate, the wait.
    dirs: dict[Path, int | None], logs: Path, link_pictures: Callable[[], object] | None = None,
    release_pictures: Callable[[], object] | None = None, *,
    swept_elsewhere: Callable[[], bool] | None = None,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> None:
    """At boot and then every hour; a failed pass waits for the next one.

    ``link_pictures`` runs once, first (ADR 0255): the files must be there to be linked.
    ``release_pictures`` runs after each sweep: the copies of the pictures it removed go too.
    ``swept_elsewhere`` (ADR 0334) is a terminal's: while it says a daemon holds this root, the
    daemon sweeps it and the pass is skipped; it is asked again at every pass.
    """
    if link_pictures is not None:
        try:
            await asyncio.to_thread(link_pictures)
        except (OSError, sqlite3.Error):
            LOGGER.exception("pictures: could not link earlier records to their files")
    while True:
        try:
            if swept_elsewhere is None or not await asyncio.to_thread(swept_elsewhere):
                await asyncio.to_thread(_sweep_data, dirs, logs)
                if release_pictures is not None:
                    await asyncio.to_thread(release_pictures)
        except OSError:
            LOGGER.exception("retention: sweep failed; next try in an hour")
        await sleep(_DATA_SWEEP_INTERVAL_S)


def _clear_media(runtime: JarvisRuntime) -> int:
    """``POST /inherent/data/clear-recordings``: the files and the pictures' notes go (ADR 0223)."""
    gone = data.clear_files(list(_media_dirs(runtime)))
    if runtime.memory is not None:
        picture_notes.delete_all(runtime.memory.db_path)
    _release_pictures(runtime)
    return gone


def _export_data(runtime: JarvisRuntime) -> Path:
    """``GET /inherent/data/export``: conversations, event log, settings, recordings, screenshots.

    Keys, sign-ins, the browser profile and the speech models stay out.
    """
    paths = runtime.runtime_paths
    memory = [runtime.memory.db_path] if runtime.memory is not None else []
    return data.export_zip(
        paths.root,
        [*memory, paths.event_log, paths.settings, paths.root / SETTINGS_FILE,
         *_media_dirs(runtime)],
    )


def _erase_data(runtime: JarvisRuntime) -> None:
    """``POST /inherent/data/erase``: everything goes at the next boot, which starts now.

    The uploaded copies of his pictures go first: the files that name them go at that boot.
    """
    _release_pictures(runtime, everything=True)
    data.request_erase(runtime.runtime_paths.root)
    _restart_soon()


async def _refresh_work_state_now(service: WorkStateService) -> dict[str, Any]:
    """``POST /inherent/work-state/refresh``: the single-flight analysis on its own connection."""
    return await asyncio.to_thread(service.refresh_in_own_connection, trigger="dashboard")


def _install_power_observer_or_degrade(
    conn: sqlite3.Connection,
    loop: asyncio.AbstractEventLoop,
    *,
    before_sleep_hook: Callable[[], object] | None = None,
    on_wake_hook: Callable[[], object] | None = None,
) -> PowerObserver | None:
    """Install the ADR-0009 D3 power observer; ``None`` if it cannot register.

    ``loop`` is mandatory here (unlike the one-shot CLI's install): the
    observer fires its callbacks on the CFRunLoop thread while ``conn``
    is loop-thread-only, so ``install_power_observer`` marshals the
    ``mac.sleeping`` / ``mac.awake`` emits back onto the loop.

    F4 — fail-open residency. A dead IOKit path costs live sleep/wake
    events, never the daemon; the bootstrap sweep closes actions
    orphaned by an unobserved sleep on the next restart instead.
    """
    try:
        return install_power_observer(
            conn,
            loop=loop,
            before_sleep_hook=before_sleep_hook,
            on_wake_hook=on_wake_hook,
        )
    except Exception:
        LOGGER.exception(
            "power observer install failed; serving without sleep/wake "
            "notifications (ADR-0009 F4).",
        )
        return None


def _shutdown_power_observer(power_observer: PowerObserver | None) -> None:
    """De-register the power observer. Idempotent, never raises.

    ADR-0009 D3 pins this FIRST in the serve teardown: ``shutdown()``
    sets the closed flag that a racing kernel notification consults, so
    a sleep landing mid-teardown returns instead of marshaling an emit
    onto a loop that is about to stop — and onto ``runtime.conn``, which
    the caller closes as soon as ``serve_inherent`` returns.
    """
    if power_observer is None:
        return
    try:
        power_observer.shutdown()
    except Exception:  # noqa: BLE001 — shutdown errors must not mask uvicorn return
        LOGGER.debug("power observer shutdown failed", exc_info=True)


def _shutdown_tts(
    tts_pipe: voice_media.StreamingTTSPipeline | None,
) -> None:
    """Bound TTS owners, then stop the player and release PortAudio.

    The caller has already installed the pipeline's close gate and cancelled
    the watcher. TTS provider/fallback work belongs to the media actor's daemon
    worker rather than asyncio's default executor; :meth:`StreamingTTSPipeline.close`
    cancels its owned task/process and joins that worker up to a hard bound.
    """
    if tts_pipe is None:
        return
    try:
        tts_pipe.close()
    except Exception:  # noqa: BLE001 — shutdown errors must not mask uvicorn return
        LOGGER.debug("tts pipeline close failed", exc_info=True)


def _request_tts_close(
    tts_pipe: voice_media.StreamingTTSPipeline | None,
) -> None:
    """Install the late-output gate before watcher cancellation; never raise."""
    if tts_pipe is None:
        return
    try:
        tts_pipe.request_close()
    except Exception:  # noqa: BLE001 — continue the remaining teardown.
        LOGGER.debug("tts pipeline close request failed", exc_info=True)


def _log_voice_startup(  # noqa: PLR0913 - the record's fields ARE its contract
    *,
    knobs: _VoiceKnobs,
    sensevoice_dir: Path,
    silero_vad_path: Path,
    models_ok: bool,
    owners: _VoiceInputOwners,
    reason: str,
) -> None:
    """Emit the one per-boot record naming every resolved voice value.

    ADR-0006 §5 — the point of moving these values into config is that one file
    decides them, which is only checkable if the daemon says what it resolved.
    JSON rather than ``key=value`` because there are twenty-nine fields.

    Also the place startup says whether an input owner is running at all.
    """
    input_owner = "single_ingress" if owners.duplex_session is not None else "none"
    payload: dict[str, Any] = {
        "sensevoice_dir": str(sensevoice_dir),
        "silero_vad_path": str(silero_vad_path),
        "models_ok": models_ok,
        "input_owner": input_owner,
        "reason": reason,
    }
    payload.update(
        {
            field.name: getattr(knobs, field.name)
            for field in dataclasses.fields(knobs)
            if field.name != "vad_profiles"
        },
    )
    for mode, profile in sorted(knobs.vad_profiles.items()):
        payload.update(
            {
                f"vad_{mode}_{key}": value
                for key, value in dataclasses.asdict(profile).items()
            },
        )
    LOGGER.info("voice startup config: %s", json.dumps(payload, sort_keys=True))


def _shutdown_duplex_voice_session(
    session: voice_session.DuplexVoiceSession | None,
) -> voice_session.VoiceSessionCloseResult | None:
    """Close Wave-3 input ownership and retain a typed shutdown trace."""
    if session is None:
        return None
    try:
        result = session.close()
    except Exception:
        LOGGER.exception("duplex voice session close raised")
        return None
    if not result.definitively_closed:
        LOGGER.error(
            "duplex voice session close incomplete: backend=%s workers=%s "
            "pending_detections=%d pending_commits=%d",
            result.ingress.backend_result,
            result.alive_threads,
            result.pending_detections,
            result.pending_commits,
        )
    return result


def _request_voice_input_branch_shutdown(
    owners: _VoiceInputOwners,
    tts_pipe: voice_media.StreamingTTSPipeline | None,
) -> None:
    """Revoke input before output, the order serve uses (ADR-0006 F14)."""
    if owners.duplex_session is not None:
        _shutdown_duplex_voice_session(owners.duplex_session)
    _request_tts_close(tts_pipe)


def _leave_unfinished_at_shutdown(
    runtime: JarvisRuntime, unfinished: Collection[asyncio.Task[None]], *, budget_s: float,
) -> None:
    """Record what the shutdown budget ran out on, and close the runs it leaves open.

    A task still running after the cancel is a turn in a model call that cannot be
    interrupted: its thread lives as long as the process does. Its response run is
    cancelled with the closed reason ``shutdown`` (ADR-0008 D10), so the log says why it
    ended instead of leaving it for the next boot to close as ``daemon_restart``.
    """
    names = sorted(task.get_name() for task in unfinished)
    LOGGER.warning(
        "serve_inherent: %d task(s) still running %.1f s into shutdown, left behind: %s",
        len(names), budget_s, ", ".join(names),
    )
    record_realtime_trace("shutdown_deadline", budget_s=budget_s, unfinished=", ".join(names))
    registry = runtime.response_runs
    if registry is None:
        return
    cancel = make_response_cancel_callable(runtime)
    for run in registry.open_runs():
        with contextlib.suppress(Exception):  # a locked log leaves it to the next boot
            cancel(run.response_id, "generation", "shutdown")


async def _stop_children(runtime: JarvisRuntime, *, timeout_s: float) -> None:
    """Stop what the daemon started: each stop gets ``timeout_s`` off the loop; log, never raise."""
    stops: list[tuple[str, Callable[[], object]]] = []
    if runtime.workers is not None:
        # ADR 0019: the codex app-server child goes with the daemon; its open worker_edges
        # rows become closed.
        stops.append(("workers", runtime.workers.stop))
    if runtime.plugin_connections is not None:
        # ADR 0031: every MCP client exits on its own task, then its loop thread ends.
        stops.append(("plugin connections", runtime.plugin_connections.stop))
    if runtime.mcp_servers is not None:
        stops.append(("mcp servers", runtime.mcp_servers.stop))
    for name, stop in stops:
        try:
            await asyncio.wait_for(asyncio.to_thread(stop), timeout=timeout_s)
        except TimeoutError:
            LOGGER.warning("serve_inherent: %s did not stop in %.1f s; leaving it", name, timeout_s)
        except Exception:
            LOGGER.exception("serve_inherent: stopping %s failed", name)


V2_PRINCIPAL: Final[str] = "inherent_v2"
"""The ADR-0014 D21 ``authenticated_principal``.

The v2 input routes authenticate one shared per-boot bearer token, not a user, so
this constant names that credential rather than a person.  The idempotency key
still separates clients through ``client_instance_id``; a per-user principal
arrives with a real identity mechanism.
"""


def _read_turn_timings(event_log_path: Path, since_ms: int, limit: int) -> dict[str, Any]:
    """``GET /inherent/turn-timings`` (ADR 0276): the newest ``limit`` timing facts, oldest first.

    ``since_ms`` is a time in epoch ms (0 for no lower bound); each fact is the event's payload
    with its ``event_uid`` and ``ts_epoch_ms`` (when the turn's answer was out) added. The log is
    read on a connection of its own, so this runs on a worker thread.
    """
    conn = open_runtime_event_log(event_log_path)
    try:
        rows = conn.execute(
            "SELECT event_uid, ts_epoch_ms, payload_json FROM events "
            "WHERE type = 'turn.timing' AND ts_epoch_ms >= ? ORDER BY id DESC LIMIT ?",
            (since_ms, limit),
        ).fetchall()
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
    turns = [
        {"event_uid": uid, "ts_epoch_ms": ts, **json.loads(payload)}
        for uid, ts, payload in reversed(rows)
    ]
    return {"now_ms": int(time.time() * 1000), "turns": turns}


def _read_track(event_log_path: Path, from_ms: int, to_ms: int) -> dict[str, Any]:
    """``GET /inherent/track`` (ADR 0312): the phones' dense track over a window.

    The log is read on a connection of its own, so this runs on a worker thread.
    """
    conn = open_runtime_event_log(event_log_path)
    try:
        tracks = phone_track.track(conn, from_ms, to_ms)
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
    return {
        "from_ms": from_ms, "to_ms": to_ms, "columns": list(phone_track.COLUMNS), "tracks": tracks,
    }


async def _serve_track(event_log_path: Path, from_ms: int, to_ms: int) -> dict[str, Any]:
    """:func:`_read_track` off the loop thread."""
    return await asyncio.to_thread(_read_track, event_log_path, from_ms, to_ms)


def _read_footprint(
    event_log: Path, configured: tzinfo | None, memory_db: Path | None, day: date | None,
    now_ms: int,
) -> dict[str, Any]:
    """``GET /inherent/footprint`` (ADR 0323): his footprint of the day asked for, or today.

    The day is cut in the owner's zone as ``GET /inherent/day`` cuts it, and the places he keeps
    are read from ``memory_db`` (none without it). The log is read on a connection of its own,
    so this runs on a worker thread.
    """
    zone_name, zone = resolve_zone(None, configured)
    on = datetime.fromtimestamp(now_ms / 1000, zone).date() if day is None else day
    start_ms, end_ms = day_window(on, zone)
    kept = [] if memory_db is None else places.read_kept(memory_db)
    conn = open_runtime_event_log(event_log)
    try:
        folded = footprint.fold(conn, start_ms, end_ms, now_ms, kept)
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
    return {
        "date": on.isoformat(),
        "tz": zone_name,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "now_ms": now_ms,
        **folded,
    }


async def _serve_footprint(
    event_log: Path, configured: tzinfo | None, memory_db: Path | None, day: date | None,
    *, clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """:func:`_read_footprint` off the loop thread, at the brain's ``clock``."""
    return await asyncio.to_thread(
        _read_footprint, event_log, configured, memory_db, day, int(clock() * 1000),
    )


async def _serve_turn_timings(event_log_path: Path, since_ms: int, limit: int) -> dict[str, Any]:
    """:func:`_read_turn_timings` off the loop thread."""
    return await asyncio.to_thread(_read_turn_timings, event_log_path, since_ms, limit)


def _live_wake_subscriber(live_voice: voice_live.LiveVoice) -> Callable[[Event], None]:
    """Bus subscriber: a response terminal wakes the delegation that owns its turn (D9).

    Runs on the committing thread; ``deliver`` only schedules onto the loop.
    """

    def _wake(event: Event) -> None:
        if event.type in _LIVE_TERMINAL_TYPES:
            turn_id = event.payload.get("turn_id")
            if isinstance(turn_id, str) and turn_id:
                live_voice.deliver(turn_id)

    return _wake


def _v2_accepted(receipt: InputReceipt) -> InputSubmissionOutcome:
    """Carry a durable receipt across the L2 -> L5 boundary as a plain value."""
    return InputSubmissionOutcome(
        outcome="accepted",
        request_id=receipt.request_id,
        input_event_uid=receipt.input_event_uid,
        turn_id=receipt.turn_id,
        session_id=receipt.session_id,
        utterance_id=receipt.utterance_id,
        text=receipt.text,
        emotion=receipt.emotion,
    )


def _submit_text_v2(
    event_log_path: Path,
    request_id: str,
    client_instance_id: str,
    text: str,
) -> InputSubmissionOutcome:
    """ADR-0014 D21 — the inbox behind ``POST /inherent/submit/v2``.

    Bound at daemon start and run on an ``asyncio.to_thread`` worker, so it
    opens its own connection exactly as ``submit_callable`` does.
    """
    key = SubmissionKey(V2_PRINCIPAL, client_instance_id, request_id)
    inner_conn = open_runtime_event_log(event_log_path)
    try:
        return _v2_accepted(submit_text_once(inner_conn, key=key, transcript=text))
    except PayloadConflictError:
        return InputSubmissionOutcome(outcome="payload_conflict")
    finally:
        with contextlib.suppress(sqlite3.Error):
            inner_conn.close()


def _submit_with_attachments(
    event_log_path: Path, text: str, attachment_ids: Sequence[str], *, device: str = MAC_NODE,
    about: dict[str, Any] | None = None,
) -> str:
    """ADR 0211: the turn of ``POST /inherent/submit`` whose words came with stored files.

    ``device``: the paired device that sent them (ADR 0212); the turn's opening row is written
    under its name, as the ones ``/phone/ws`` writes are. ``about``: the item its phone has open
    (ADR 0214), already checked, kept on that row.

    Bound at daemon start and run on an ``asyncio.to_thread`` worker, so it opens its own
    connection exactly as ``submit_callable`` does. Returns the minted ``turn_id``.
    """
    turn_id = _new_turn_id()
    inner_conn = open_runtime_event_log(event_log_path)
    try:
        emit_surface_user_intent(
            inner_conn, transcript=text, turn_id=turn_id, attachments=attachment_ids,
            ingestion_node=device, about=about,
        )
    finally:
        with contextlib.suppress(sqlite3.Error):
            inner_conn.close()
    return turn_id


def _submit_as_device(  # noqa: PLR0913 - the phone message's fields, as `say` has them.
    event_log_path: Path, device: str, text: str, attachment_ids: Sequence[str],
    about: dict[str, Any] | None, utterance_id: str | None = None,
) -> str:
    """ADR 0212: the turn of words a paired ``device`` sent over HTTP, with or without files.

    ADR 0262: with the phone's ``utterance_id`` the words are written as its ``/phone/ws``
    ``say`` is, under the same uid and turn id, so the same message sent again by either route
    returns its first turn and writes nothing.

    Raises:
        ValueError: ``utterance_id`` is given and the words are not an utterance this host takes.
    """
    if utterance_id is None:
        return _submit_with_attachments(
            event_log_path, text, attachment_ids, device=device, about=about,
        )
    inner_conn = open_runtime_event_log(event_log_path)
    try:
        return BrainEvents(inner_conn).record_phone_say(
            device, utterance_id, text, spoken=False, about=about, attachments=attachment_ids,
        )
    except sqlite3.IntegrityError:  # the same message's other route wrote it just now
        return phone_turn_id(device, utterance_id)
    finally:
        with contextlib.suppress(sqlite3.Error):
            inner_conn.close()


def _set_reaction(
    event_log_path: Path, memory_db: Path, target: str, emoji: str | None,
) -> bool:
    """ADR 0257: his reaction on a message, or taken off; whether anything changed.

    The line the model reads and the event are written here and no turn is started.
    """
    conn = open_runtime_event_log(event_log_path)
    try:
        return reactions.allen_reacts(conn, memory_db, target=target, emoji=emoji)
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()


def _keep_share(
    event_log_path: Path, fields: dict[str, Any], *, picture_notes: PictureNotes | None = None,
) -> str:
    """ADR 0211: keep a share he did not ask about as one ``user.shared`` event; its id.

    A saved picture is queued for its note at once (ADR 0223); if that is lost, the note is
    found again from the event.
    """
    share_id = uuid.uuid4().hex
    inner_conn = open_runtime_event_log(event_log_path)
    try:
        emit_event(
            inner_conn,
            type="user.shared",
            payload={"share_id": share_id, **{k: v for k, v in fields.items() if v}},
        )
    finally:
        with contextlib.suppress(sqlite3.Error):
            inner_conn.close()
    if picture_notes is not None and fields.get("attachments"):
        picture_notes.submit_saved(fields["attachments"], str(fields.get("note") or ""))
    return share_id


def _submit_asr_v2(  # noqa: PLR0913 — the bound path and pipeline plus the four request fields.
    event_log_path: Path,
    voice_pipeline_callable: Callable[[bytes, str, str, str], Event],
    pcm: bytes,
    request_id: str,
    client_instance_id: str,
    audio_sha256: str,
    language: str,
) -> InputSubmissionOutcome:
    """ADR-0014 D21 — the lease around ``POST /inherent/asr-submit/v2``.

    The lease is claimed and released on this connection while ASR itself runs
    outside any transaction, which is the whole point of the two-phase shape:
    a SenseVoice pass may take seconds and must never hold the write lock.
    A pipeline failure releases the lease so the same ``request_id`` can be
    retried at once rather than waiting out the TTL.
    """
    key = SubmissionKey(V2_PRINCIPAL, client_instance_id, request_id)
    inner_conn = open_runtime_event_log(event_log_path)
    try:
        try:
            claim = claim_asr_request(
                inner_conn, key=key, audio_sha256=audio_sha256, language=language,
            )
        except PayloadConflictError:
            return InputSubmissionOutcome(outcome="payload_conflict")
        except SubmissionInProgressError:
            return InputSubmissionOutcome(outcome="in_progress")
        if isinstance(claim, InputReceipt):
            return _v2_accepted(claim)
        try:
            event = voice_pipeline_callable(pcm, claim.turn_id, "inherent_ptt", language)
        except BaseException:
            release_asr_request(inner_conn, key=key)
            raise
        utterance_id = event.payload.get("utterance_id")
        return _v2_accepted(
            resolve_asr_request(
                inner_conn,
                key=key,
                audio_sha256=audio_sha256,
                language=language,
                turn_id=claim.turn_id,
                input_event_uid=event.event_uid,
                utterance_id=utterance_id if isinstance(utterance_id, str) else None,
                text=str(event.payload.get("transcript", "")),
                emotion=str(event.payload.get("emotion", "") or ""),
            ),
        )
    finally:
        with contextlib.suppress(sqlite3.Error):
            inner_conn.close()


class _Server(uvicorn.Server):
    """uvicorn whose stop signal ends ``serve`` and leaves the process to ``serve_inherent``.

    uvicorn re-raises the signal once it has shut down: SIGTERM then kills the process and
    SIGINT cancels the main task, both before the daemon's teardown has run.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        stops = (signal.SIGINT, signal.SIGTERM)
        before = {sig: signal.signal(sig, self.handle_exit) for sig in stops}
        try:
            yield
        finally:
            for sig, handler in before.items():
                signal.signal(sig, handler)


def _listen_sockets(host: str, port: int, addresses: tuple[str, ...]) -> list[socket.socket]:
    """Listening sockets for ``host`` and each private address (ADR 0170).

    uvicorn binds one host, so a brain with more addresses hands it the sockets.
    """
    sockets: list[socket.socket] = []
    for address in (host, *addresses):
        try:
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            sockets.append(socket.create_server((address, port), family=family, backlog=2048))
        except OSError as exc:
            for sock in sockets:
                sock.close()
            msg = f"cannot listen on {address}:{port}: {exc}"
            raise OSError(msg) from exc
    return sockets


async def serve_inherent(  # noqa: C901, PLR0912, PLR0915 — composition-root entrypoint; the keyword args ARE the daemon contract and the voice-wiring plus boot-reconciliation branches necessarily inflate body length + branch count.
    runtime: JarvisRuntime,
    *,
    host: str = "127.0.0.1",
    port: int = _DEFAULT_PORT,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    shutdown_budget_s: float = _SHUTDOWN_BUDGET_S,
) -> None:
    """Run the Inherent daemon (text + voice). Blocks until SIGINT / SIGTERM.

    The caller holds the daemon lock (:func:`jarvis.deployment.daemon_session`,
    ADR 0318): it is the OUTERMOST scope, so its release is the very LAST
    cleanup step, after this function has returned and the Event Log is closed.

    Lifecycle:

    1. (the caller) :func:`jarvis.deployment.daemon_session` — raises
       :class:`jarvis.deployment.process_lock.ProcessLockHeld` if
       another live daemon already holds it, before it erases or opens anything.
    2. Build :class:`InherentBroadcaster` and attach the running
       event loop via :meth:`InherentBroadcaster.attach_loop` (ADR-0005
       §4.2 worker-thread → broadcaster bridge); bind
       ``submit_callable`` to :func:`emit_surface_user_intent` with a
       fresh ``turn_id`` per HTTP submit.
    3. ADR-0005 §12 pre-flight — call :func:`_voice_models_preflight`
       on the configured SenseVoice + Silero paths. When models are
       present, construct :class:`VoicePipeline`, optionally
       :class:`~jarvis.surface.voice_media.StreamingTTSPipeline` (if
       ``MINIMAX_API_KEY`` is set), and (when ``JARVIS_VOICE_DISABLE_WAKE`` is
       unset) start the single audio ingress. Missing models or any construction
       failure: log ERROR and continue text-only — the text path stays
       healthy.
    4. Build the FastAPI app via :func:`create_app` with
       :class:`InherentDeps` (carries ``submit_asr`` so
       ``/inherent/asr-submit/v2`` can do PTT ASR even without a
       microphone owner; falls through to 501 when the pipeline is None).
    5. Configure :class:`uvicorn.Config` (``lifespan="off"`` because
       this module owns the lifecycle, ``log_level="warning"`` to
       avoid uvicorn's per-request stdout noise drowning the watcher
       logs).
    6. Spawn the intent pump and :func:`_response_watcher`
       as background tasks BEFORE :meth:`uvicorn.Server.serve` so an
       early intent post is observed; add :func:`_tts_watcher` when
       a TTS pipeline was constructed. ``_response_watcher`` polls all
       three Step-2 response event types in a single cursor and
       dispatches to the per-type broadcaster methods (D16); the
       ``_tts_watcher`` (parallel cursor) dispatches the same three
       types into the TTS state machine (ADR-0005 §5.3).
    6b. ADR-0009 D4 — :func:`_start_sweep_control_plane`: anchor
       :func:`_system_trigger_watcher` at the current ``MAX(events.id)``,
       wait for it to signal anchored, run the one-shot bootstrap sweep,
       then start the periodic :func:`_supervisor_sweep_task`. Both tasks
       join ``watchers`` so the same ``finally`` tears them down.
    6c. ADR-0009 D5 — :func:`_start_repo_observer`: recover the
       emit-on-change baselines from the log, then start one more
       watchers-list task polling ``observer.repos`` every
       ``observer.poll_interval_s``. Each poll collects via
       :func:`asyncio.to_thread` (``git`` subprocesses) and emits on the
       loop thread (``runtime.conn`` is ``check_same_thread``). Empty
       ``observer.repos`` starts nothing.
    7. ADR-0009 D3 — :func:`install_power_observer` on ``runtime.conn``
       with ``loop=`` the running loop, so the observer's CFRunLoop-thread
       notifications marshal their ``mac.sleeping`` / ``mac.awake`` emits
       onto this loop. Any failure is logged and swallowed (F4: the
       daemon serves text + voice without sleep/wake rather than dying).
    8. ``await server.serve()`` — blocks until uvicorn returns (signal
       received).
    9. ``_shutdown``, registered once the voice owners exist and run when
       serving ends or any step above raises: tear the power observer down
       FIRST (its closed flag must be set before the loop starts winding
       down), then stop the voice input (if running), cancel
       every watcher task and wait for them, so a watcher that crashed
       during runtime does not mask the shutdown path. The whole teardown
       is bounded by ``shutdown_budget_s``: a turn still in a model call at
       the deadline is left running, its response run cancelled as
       ``shutdown``, and the children are stopped anyway.

    Env vars:
        ``MINIMAX_API_KEY``         — gates the TTS subsystem.
        ``JARVIS_VOICE_DISABLE_WAKE`` — when ``"1"``, skip the
            microphone owner even if models are present (CI / smoke tests).

    Args:
        runtime: Assembled :class:`JarvisRuntime`.
        host: Bind interface for the FastAPI app.
        port: TCP port for the FastAPI app.
        poll_interval_s: Watcher poll cadence (default 10 ms).
        shutdown_budget_s: How long the teardown waits for the watchers (and the turns they
            drive) before it leaves them behind and stops the children regardless.
    """
    async with contextlib.AsyncExitStack() as unwind:
        if runtime.decision_state is not None:
            runtime.decision_state.warm()  # ADR 0164: the first turn then reads only a delta
        if runtime.ledger is not None:
            runtime.ledger.warm(lambda: datetime.now(UTC))  # its standing text is a 38-day read
        broadcaster = InherentBroadcaster()
        broadcaster.attach_loop(asyncio.get_running_loop())
        broadcaster.turn_device = _turn_devices(runtime.conn)  # ADR 0222

        def submit_callable(text: str) -> str:
            """Bound at daemon-start time. Mints a fresh ``turn_id`` per call.

            Runs on whatever thread :func:`asyncio.to_thread` dispatches
            it onto (see ``/inherent/submit`` handler in Step 7). The
            handler thread is NOT the event-loop thread, so to honor
            ``check_same_thread`` we open a fresh ``sqlite3.Connection``
            to the same DB file for the SQLite write.

            **Returns the minted id** (ADR-0009 D2): it is what
            ``POST /inherent/submit`` hands back, and what the one-shot
            CLI filters the WS stream on. Discarding it here — the Step-12
            state — left the response's ``turn_id`` an empty string in
            production, with the client falling back to matching the
            ``open`` envelope's ``q`` against its own utterance.
            """
            turn_id = _new_turn_id()
            inner_conn = open_runtime_event_log(runtime.runtime_paths.event_log)
            try:
                emit_surface_user_intent(
                    inner_conn,
                    transcript=text,
                    turn_id=turn_id,
                )
            finally:
                with contextlib.suppress(sqlite3.Error):
                    inner_conn.close()
            return turn_id

        images_ok = _model_takes_images(runtime.config)
        submit_attachments = functools.partial(
            _submit_with_attachments, runtime.runtime_paths.event_log,
        )
        submit_as_device = functools.partial(_submit_as_device, runtime.runtime_paths.event_log)
        share_callable = functools.partial(
            _keep_share, runtime.runtime_paths.event_log, picture_notes=runtime.picture_notes,
        )

        # ADR-0005 §12 pre-flight + voice subsystem wiring. Any failure
        # downgrades the daemon to text-only — text path must stay
        # healthy when models / SDKs / mics are missing (CI default).
        voice_pipe: voice_pipeline.VoicePipeline | None = None
        tts_pipe: voice_media.StreamingTTSPipeline | None = None
        duplex_voice_session: voice_session.DuplexVoiceSession | None = None
        # ADR-0015: the two mute switches the desktop surface flips over
        # POST /inherent/controls: mic on the input session, speech as the player's gain.
        # ADR 0153: the quiet level is the one switch kept across restarts.
        quiet_path = runtime.runtime_paths.root / "quiet-mode.json"
        controls = voice_controls.VoiceControls(quiet=quiet_mode.load(quiet_path))

        def _keep_quiet(level: str) -> None:
            quiet_mode.save(quiet_path, level)
            LOGGER.info("controls: quiet=%s", level)
            broadcaster.broadcast_op_sync("controls", **controls.update())

        controls.on_quiet = _keep_quiet
        if runtime.voice_settings is not None:
            # ADR 0174: a volume or speed said in a conversation ends with it.
            controls.on_conversation_end = runtime.voice_settings.end_conversation

        def _set_quiet(level: str) -> None:
            controls.update(quiet=level)

        def _set_conversation(on: bool, reason: str) -> None:  # noqa: FBT001 - session callback shape
            """ADR 0102: voice flips conversation mode; the surface hears it as a controls push."""
            state = controls.update(conversation=on)
            LOGGER.info("controls: conversation=%s (%s)", on, reason)
            broadcaster.broadcast_op_sync("controls", **state, reason=reason)

        voice_input_owners = _VoiceInputOwners(
            duplex_session=None,
            single_ingress_attempted=False,
        )
        voice_pipeline_callable: Any | None = None
        # ADR-0005 §5.1 / §5.3: ONE shared SystemAudioDucker counts the TTS
        # provider/playback output leases the night run waits on.
        shared_ducker: voice_ducking.SystemAudioDucker = voice_ducking.SystemAudioDucker()

        live_voice: voice_live.LiveVoice | None = None
        dictation: Dictation | None = None

        def _old_chain_input_blocked() -> bool:
            # ADR-0015 mic mute, plus: while GPT-Live owns speech the local chain
            # arms no new wake, so one utterance cannot be answered twice; and
            # while Allen dictates (ADR 0058) his words are text, not a turn.
            return (
                controls.mic_is_muted()
                or (live_voice is not None and live_voice.owns_speech)
                or (dictation is not None and dictation.active)
            )

        def _apply_speech_mute(muted: bool) -> None:  # noqa: FBT001 - Callable[[bool], None] shape
            # ADR-0015 D2: speech mute is the player's output gain, 0.0 muted and
            # 1.0 restored; synthesis, timing, phases and events run as unmuted.
            # The local player is also silent while GPT-Live owns speech.
            if tts_pipe is not None:
                local_silent = muted or (live_voice is not None and live_voice.owns_speech)
                tts_pipe.set_output_gain(0.0 if local_silent else 1.0)
            if live_voice is not None:
                live_voice.set_speech_muted(muted)

        controls.on_speech_muted = _apply_speech_mute
        voice_knobs = _voice_knobs(runtime.config)
        # A failed setup is a stopped daemon, not a half-alive one: the voice owners started
        # below hold non-daemon threads, and nothing else ends the process. The same teardown
        # runs on a setup failure and on a normal stop; what is not built yet is None.
        watchers: list[asyncio.Task[None]] = []
        power_coordinator: _VoicePowerCoordinator | None = None
        power_observer: PowerObserver | None = None

        async def _shutdown() -> None:
            LOGGER.info("serve_inherent: shutting down watchers")
            deadline = time.monotonic() + shutdown_budget_s

            def left() -> float:
                return max(0.0, deadline - time.monotonic())

            # Power observer FIRST: its closed flag must be set before the
            # loop starts winding down, so a notification racing this
            # teardown finds the flag instead of a dead loop. Everything
            # below (input / TTS / ducker / watcher cancel) is loop-thread
            # work that would otherwise be racing that notification.
            _shutdown_power_observer(power_observer)
            if power_coordinator is not None:
                power_coordinator.close(timeout_s=0.1)
            # ADR-0006 F14: input is revoked before output.
            # GPT-Live is billed per second: hang up before the microphone goes away.
            # The budget follows close_timeout_s and stays under launchd's default
            # 20 s ExitTimeOut, after which the agent is killed mid-hangup.
            if live_voice is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        live_voice.stop(reason="daemon_shutdown"),
                        timeout=min(live_voice.stop_budget_s, left()),
                    )
            _request_voice_input_branch_shutdown(voice_input_owners, tts_pipe)
            # Cancel/await watcher ownership before PortAudio teardown. A
            # provider thread may still exist, but the closed generation owns
            # no right to write or invoke fallback.
            for w in watchers:
                w.cancel()
            if watchers:
                _done, unfinished = await asyncio.wait(watchers, timeout=left())
                if unfinished:
                    _leave_unfinished_at_shutdown(runtime, unfinished, budget_s=shutdown_budget_s)
            await _stop_children(runtime, timeout_s=max(left(), _CHILD_STOP_FLOOR_S))
            _shutdown_tts(tts_pipe)
            LOGGER.info("serve_inherent: shutdown complete")

        unwind.push_async_callback(_shutdown)

        if runtime.terminal_hub is not None:
            runtime.terminal_hub.voice = _build_brain_voice(runtime, voice_knobs)
            runtime.terminal_hub.listening = _build_brain_listening(
                runtime, runtime.terminal_hub,
                controls=controls, set_conversation=_set_conversation, set_quiet=_set_quiet,
            )
        sensevoice_dir = runtime.sensevoice_dir
        silero_path = runtime.silero_vad_path
        model_fetch = models.Progress()
        brain = runtime.role == "brain"
        models_ok, missing = (
            (False, [])
            if brain
            else _voice_models_preflight(sensevoice_dir=sensevoice_dir, silero_path=silero_path)
        )
        voice_startup_reason = "models_missing"
        if brain:
            # ADR 0170: the microphone, wake, ASR and playback belong to a terminal.
            voice_startup_reason = "role_brain"
        elif not models_ok:
            LOGGER.error(
                "voice models missing; running text-only. Missing: %s",
                "; ".join(missing),
            )
            _fetch_models_then_restart(
                sensevoice_dir, silero_path, asyncio.get_running_loop(), model_fetch,
            )
        else:
            try:
                voice_pipe = _build_voice_pipeline(
                    runtime,
                    broadcaster=broadcaster,
                    sensevoice_dir=sensevoice_dir,
                    voiceprint_model=runtime.voiceprint_model,
                )
                voice_pipeline_callable = _build_voice_pipeline_callable(voice_pipe)
                echo_canceller = _build_echo_canceller(runtime)
                tts_pipe = _build_tts_pipeline(
                    runtime,
                    broadcaster,
                    ducker=shared_ducker,
                    voice=voice_knobs,
                    echo_canceller=echo_canceller,
                )
                if os.environ.get("JARVIS_VOICE_DISABLE_WAKE") == "1":
                    voice_startup_reason = "wake_disabled_env"
                    LOGGER.info(
                        "JARVIS_VOICE_DISABLE_WAKE=1; skipping the microphone owner.",
                    )
                else:
                    voice_input_owners = _spawn_voice_input_owners(
                        runtime=runtime,
                        pipeline=voice_pipe,
                        broadcaster=broadcaster,
                        silero_path=silero_path,
                        tts=tts_pipe,
                        voice=voice_knobs,
                        mic_muted=_old_chain_input_blocked,
                        conversation=controls.conversation_is_on,
                        set_conversation=_set_conversation,
                        set_quiet=_set_quiet,
                        echo_canceller=echo_canceller,
                    )
                    duplex_voice_session = voice_input_owners.duplex_session
                    voice_startup_reason = voice_input_owners.reason
            except Exception:
                voice_startup_reason = "voice_construction_failed"
                LOGGER.exception(
                    "voice subsystem construction failed; running text-only.",
                )
                voice_pipe = None
                voice_pipeline_callable = None
                tts_pipe = None
                duplex_voice_session = None
                voice_input_owners = _VoiceInputOwners(
                    duplex_session=None,
                    single_ingress_attempted=False,
                )

        # GPT-Live (ADR-0016). Building
        # the controller opens nothing: a session starts only from
        # POST /inherent/controls {"live": "start"}, which re-checks the single
        # audio ingress and the API key at that moment. Billed per second, so
        # the shutdown path below hangs up before the microphone goes away.
        realtime_raw = runtime.config.get("realtime")
        realtime_map = realtime_raw if isinstance(realtime_raw, Mapping) else {}
        gpt_live_raw = realtime_map.get("gpt_live")
        gpt_live_config: voice_live.GptLiveConfig | None = None
        if isinstance(gpt_live_raw, Mapping):
            try:
                gpt_live_config = voice_live.gpt_live_config_from_mapping(
                    {"assistant": _assistant_name(runtime.config), **gpt_live_raw},
                )
            except (TypeError, ValueError):
                LOGGER.exception("realtime.gpt_live is malformed; GPT-Live stays off this boot")
        if gpt_live_config is not None and gpt_live_config.enabled:

            def _live_ingress() -> voice_audio.AudioIngress | None:
                session = voice_input_owners.duplex_session
                return session.ingress if session is not None else None

            # ADR-0016 D9: the composition root is the only place that wires
            # the L5 session to L2 (inbox, memory.db, Event Log) and to the bus.
            live_backend = _LiveBackend(
                event_log_path=runtime.runtime_paths.event_log,
                memory=runtime.memory,
                session=runtime.session,
            )
            live_voice = voice_live.LiveVoice(
                config=gpt_live_config,
                broadcaster=broadcaster,
                ingress=_live_ingress,
                mic_muted=controls.mic_is_muted,
                speech_muted=lambda: controls.speech_muted,
                output_device=_output_or_default(realtime_map.get("output_device")),
                volume=float(realtime_map.get("playback_volume") or 1.0),
                on_owns_speech=lambda _owns: _apply_speech_mute(controls.speech_muted),
                delegate=live_backend.delegate,
                record=live_backend.record,
                brief=live_backend.brief,
                lookup_result=live_backend.lookup_result,
                undelivered=live_backend.undelivered_results,
                mark_delivered=live_backend.mark_delivered,
                record_usage=live_backend.record_usage,
            )
            controls.on_mic_muted = live_voice.set_mic_muted
            if runtime.committed_event_bus is not None:
                runtime.committed_event_bus.subscribe(_live_wake_subscriber(live_voice))
            else:
                LOGGER.info("gpt_live: no committed-event bus; delegations rely on the poll")
            if not os.environ.get(gpt_live_config.api_key_env):
                LOGGER.warning(
                    "realtime.gpt_live.enabled but %s is unset; start will be refused",
                    gpt_live_config.api_key_env,
                )

        # One record per boot on EVERY path above — models present, models
        # missing, construction failed — so "what did this daemon resolve, and
        # which input owner does it have" never needs a grep again.
        _log_voice_startup(
            knobs=voice_knobs,
            sensevoice_dir=sensevoice_dir,
            silero_vad_path=silero_path,
            models_ok=models_ok,
            owners=voice_input_owners,
            reason=voice_startup_reason,
        )

        # ADR-0008 F14 / §4.4 — close ResponseRuns a previous process
        # abandoned, once, inside the startup barrier and before any
        # watcher task exists.
        closed_runs = await asyncio.to_thread(
            _reconcile_open_responses_in_thread,
            runtime.runtime_paths.event_log,
            runtime.committed_event_bus,
        )
        if closed_runs:
            LOGGER.info(
                "boot reconciliation closed %d open response run(s)",
                closed_runs,
            )

        # ADR-0008 D9 / F23 — a previous process may have died holding a
        # repository: its action reached a canonical terminal but never wrote
        # a cleanup event, and the in-process lease table died with it. Re-take
        # those leases here, inside the startup barrier, so no new conflicting
        # work is accepted against a tree whose stash was never restored. The
        # re-taken lease has no execution context, so nothing in this process
        # can release it — clearing it is a human's job, which is the point.
        # ADR-0008 §4.4 — the live playback actor is the only writer of a
        # playback terminal, so a process killed mid-playback leaves its
        # generation open forever. Close each one here, third and last, in
        # the ADR's own bullet order (responses, actions, playback).
        # Ungated on purpose: the actor that PRODUCES a playback generation
        # is gated on realtime.enabled, so any gate here admits a legal config
        # whose orphans no boot ever closes. The fold appends nothing when no
        # generation is open.
        closed_playback = await asyncio.to_thread(
            _reconcile_open_playback_in_thread,
            runtime.runtime_paths.event_log,
            runtime.committed_event_bus,
        )
        if closed_playback:
            LOGGER.info(
                "boot reconciliation closed %d open playback generation(s)",
                closed_playback,
            )

        cancel_response_callable = (
            make_response_cancel_callable(
                runtime,
                # ADR-0006 D8: only a live playback actor can stop the tail
                # of a run the registry has already released.
                stop_foreground_output=(
                    tts_pipe.stop_foreground_output
                    if tts_pipe is not None
                    else None
                ),
            )
            if runtime.response_runs is not None
            else None
        )
        # ADR-0014 D5: a fresh 256-bit bearer token every boot, written
        # 0600 under the runtime root. Rotating INSIDE the process lock is
        # what makes it safe — no second daemon can be mid-read of the
        # file this one is replacing.
        v2_token = rotate_inherent_v2_token(runtime.runtime_paths.inherent_v2_token)
        # ADR-0018 — usage observer: constructed here (baselines recovered on
        # the loop thread) so the dashboard routes can hold its read model
        # and on-demand poll; its periodic task joins `watchers` below.
        usage_observer = _make_usage_observer(runtime)
        window_memory = runtime.memory

        def _read_conversation(after: int, limit: int, before: int) -> dict[str, Any]:
            """Spec §18.3: a page of the window's rows, the history floor, and ``has_more``."""
            since = runtime.session.history_since
            if window_memory is None:
                return {"since": since, "rows": [], "has_more": False}
            rows, has_more = conversation_rows(
                window_memory.db_path, since=since, after=after, before=before, limit=limit,
                attachments=runtime.attachments,
            )
            _add_steps(runtime.runtime_paths.event_log, rows)
            _add_places(runtime.runtime_paths.event_log, rows)
            _add_reactions(runtime.runtime_paths.event_log, rows)
            return {"since": since, "rows": rows, "has_more": has_more}

        def _read_card() -> dict[str, Any]:
            """ADR 0062: the card waiting for Allen's button, or ``None``.

            ``device`` (ADR 0218) is the one whose turn asked it, ``None`` when no turn did.
            """
            conn = open_runtime_event_log(runtime.runtime_paths.event_log)
            try:
                slot = PendingConfirmations.from_events(
                    iter_events_of_types(conn, _CARD_EVENT_TYPES),
                ).slot
                device = None if slot is None else confirmation_device(conn, slot.confirmation_id)
            finally:
                with contextlib.suppress(sqlite3.Error):
                    conn.close()
            if slot is None or not slot.is_live(int(time.time() * 1000)):
                return {"card": None}
            tool = str(slot.snapshot.get("tool_name", ""))
            args_raw = slot.snapshot.get("args_meta")
            args = dict(args_raw) if isinstance(args_raw, Mapping) else {}
            parts = tool.split("__")
            return {"card": {
                "id": slot.confirmation_id,
                "tool": tool,
                "action": lang.action(tool)[0],
                "source": parts[1] if len(parts) == 3 and parts[0] == "mcp" else "",  # noqa: PLR2004
                "letter": lang.letter_to(args) is not None,
                "args": args,
                "device": device,
            }}

        def _decide_card(
            confirmation_id: str, decision: str, edits: dict[str, str], device: str,
        ) -> str:
            """ADR 0062: a card's button, as an intent the turn pump runs like any other.

            The turn is written under ``device``, the one that pressed it (ADR 0218).
            """
            turn_id = _new_turn_id()
            conn = open_runtime_event_log(runtime.runtime_paths.event_log)
            try:
                emit_event(
                    conn,
                    type="surface.user_intent",
                    payload={
                        "transcript": "",
                        "turn_id": turn_id,
                        "channel": "card",
                        "confirmation_decision": {
                            "confirmation_id": confirmation_id,
                            "decision": decision,
                            "edits": edits,
                        },
                    },
                    correlation={"turn_id": turn_id},
                    ingestion_node=device,
                )
            finally:
                with contextlib.suppress(sqlite3.Error):
                    conn.close()
            return turn_id

        def _read_question() -> dict[str, Any]:
            """ADR 0066: the ask card waiting to be filled in, or ``None``.

            ``device`` (ADR 0218) is the one whose turn asked it, ``None`` when unknown.
            """
            conn = open_runtime_event_log(runtime.runtime_paths.event_log)
            try:
                slot = _visible_ask_card(conn, int(time.time() * 1000))
                device = None if slot is None else turn_origin(conn, slot.asked_turn_id)[1]
            finally:
                with contextlib.suppress(sqlite3.Error):
                    conn.close()
            if slot is None:
                return {"card": None}
            fields = [
                {k: f[k] for k in ("label", "choices", "value") if k in f} for f in slot.fields
            ]
            card: dict[str, Any] = {
                "id": slot.clarification_id, "question": slot.question, "fields": fields,
                "device": device,
            }
            if slot.trip is not None:  # ADR 0205: a bus card shows only the rows still catchable
                shown = live_card(slot.trip)
                if shown is None:
                    return {"card": None}
                card["trip"] = shown
            return {"card": card}

        def _answer_question(
            clarification_id: str, answers: dict[str, str] | None, device: str,
        ) -> str | None:
            """ADR 0066: filled-in answers are remembered and run as Allen's words.

            Dismissing the card (``answers is None``) closes it and runs nothing. The answer's
            turn is written under ``device``, the one that filled it in (ADR 0218).

            Returns the answer turn's id, or ``None`` for a dismissal. Raises
            ``LookupError`` once the card is not the one waiting.
            """
            conn = open_runtime_event_log(runtime.runtime_paths.event_log)
            try:
                slot = PendingClarification.from_events(
                    iter_events_of_types(conn, CLARIFICATION_EVENT_TYPES),
                )
                if slot is None or not slot.waiting or slot.clarification_id != clarification_id:
                    msg = "that card is no longer waiting"
                    raise LookupError(msg)
                if answers is None:
                    emit_event(
                        conn,
                        type="surface.dismissed",
                        payload={
                            "turn_id": slot.asked_turn_id,
                            "clarification_id": clarification_id,
                        },
                    )
                    return None
                filled = [
                    (f, answers.get(str(f["label"]), "").strip()) for f in slot.fields
                ]
                filled = [(f, value) for f, value in filled if value]
                if window_memory is not None:
                    for f, value in filled:
                        if f.get("remember", True):
                            remember_fact(window_memory.db_path, str(f["label"]), value)
                turn_id = _new_turn_id()
                emit_event(
                    conn,
                    type="surface.clarified",
                    payload={
                        "turn_id": turn_id,
                        "clarification_id": clarification_id,
                        "answers": {str(f["label"]): value for f, value in filled},
                    },
                    correlation={"turn_id": turn_id},
                    ingestion_node=device,
                )
                emit_event(
                    conn,
                    type="surface.user_intent",
                    payload={
                        "transcript": "\n".join(f"{f['label']}: {value}" for f, value in filled),
                        "turn_id": turn_id,
                        "channel": "clarify",
                    },
                    correlation={"turn_id": turn_id},
                    ingestion_node=device,
                )
            finally:
                with contextlib.suppress(sqlite3.Error):
                    conn.close()
            return turn_id

        setup: Setup | None = None
        if window_memory is not None:
            knobs = _voice_knobs(runtime.config)
            setup = Setup(
                root=runtime.runtime_paths.root,
                settings_path=runtime.runtime_paths.settings,
                memory_path=window_memory.db_path,
                config=runtime.config,
                tts_endpoint=knobs.tts_primary_endpoint,
                tts_model=knobs.tts_model,
                restart=_restart_soon if supervised() else None,
                voice_models=model_fetch,
            )

        # ADR 0058: dictation hears through the live mic and the voice path's ears.
        ingress = duplex_voice_session.ingress if duplex_voice_session is not None else None
        if ingress is not None and voice_pipe is not None:
            polisher = local_polisher(
                runtime.config, runtime.runtime_paths.event_log,
                load_pricing_table(repo_root() / "data" / "pricing.json"),
            )
            if polisher is not None:
                dictation = build_dictation(
                    config=runtime.config, ingress=ingress, silero_path=silero_path,
                    transcribe=voice_pipe.transcribe, polisher=polisher,
                    vocab_path=polisher.vocab_path,
                    recordings=(
                        runtime.memory.audio_dir
                        if runtime.memory is not None and runtime.memory.retain_audio
                        else None
                    ),
                )

        # ADR 0019: one Codex board, filled by the hooks' route and read by the night run.
        codex_board: dict[str, CodexSession] = {}
        # ADR 0147: the Dashboard's mail page exists only with ``dashboard.mail.enabled``.
        mail_home = None if runtime.mail_drafts is None else runtime.home
        if runtime.view is not None:  # ADR 0176: her ``present`` op reaches the companion
            runtime.view.push = lambda sent: broadcaster.broadcast_op_sync("present", **sent)
        spend = SpendCapSettings.from_config(runtime.config.get("spend_cap"))
        built_phone = (
            _build_phone_hub(runtime, voice_knobs, set_quiet=_set_quiet)
            if runtime.listen_addresses
            else None
        )
        phone_hub = None if built_phone is None else built_phone[0]
        if phone_hub is not None and runtime.locator is not None:
            # ADR 0281: a phone with a live conversation socket is asked for a fix over it.
            runtime.locator.ask_socket = phone_hub.ask_location
        if phone_hub is not None and runtime.whereabouts is not None:
            # ADR 0217: a device with its conversation socket open is a phone, and that is said.
            runtime.whereabouts.phone_open = phone_hub.connected
        local_zone = resolve_zone(None, _work_state_timezone(runtime.config))[1]
        deps = InherentDeps(
            submit_callable=submit_callable,
            attachments=runtime.attachments,
            attachment_saved=functools.partial(_start_upload, runtime),
            images_ok=images_ok,
            submit_attachments=submit_attachments,
            submit_as_device=submit_as_device,
            share_callable=share_callable,
            reaction_set=(
                None if runtime.memory is None
                else functools.partial(
                    _set_reaction, runtime.runtime_paths.event_log, runtime.memory.db_path,
                )
            ),
            broadcaster=broadcaster,
            usage_read=(
                None if usage_observer is None else functools.partial(latest_usage, runtime.conn)
            ),
            usage_refresh=(
                None
                if usage_observer is None
                else functools.partial(_refresh_usage_now, usage_observer, runtime.conn)
            ),
            usage_codex_reset=None if usage_observer is None else redeem_codex_reset,
            usage_record_balance=None if usage_observer is None else usage_observer.record_balance,
            usage_history=(
                None if usage_observer is None else functools.partial(usage_history, runtime.conn)
            ),
            work_state_read=(
                None
                if runtime.work_state is None
                else functools.partial(runtime.work_state.read, runtime.conn)
            ),
            work_state_refresh=(
                None
                if runtime.work_state is None
                else functools.partial(_refresh_work_state_now, runtime.work_state)
            ),
            projects_read=(
                None
                if runtime.projects is None
                else functools.partial(asyncio.to_thread, runtime.projects.read)
            ),
            projects_refresh=(
                None
                if runtime.projects is None
                else functools.partial(asyncio.to_thread, runtime.projects.refresh)
            ),
            conversation_read=None if window_memory is None else _read_conversation,
            ask_outcome=functools.partial(_ask_outcome, runtime.runtime_paths.event_log),
            turn_timings_read=functools.partial(
                _serve_turn_timings, runtime.runtime_paths.event_log,
            ),
            track_read=functools.partial(_serve_track, runtime.runtime_paths.event_log),
            footprint_read=functools.partial(
                _serve_footprint,
                runtime.runtime_paths.event_log,
                _work_state_timezone(runtime.config),
                None if runtime.memory is None else runtime.memory.db_path,
            ),
            card_read=_read_card,
            card_decide=_decide_card,
            question_read=_read_question,
            question_answer=_answer_question,
            think_read=(
                None if runtime.think_mode is None
                else functools.partial(runtime.think_mode.status, runtime.conn)
            ),
            day_read=functools.partial(
                _serve_day,
                runtime.runtime_paths.event_log,
                _work_state_timezone(runtime.config),
                None if runtime.memory is None else runtime.memory.db_path,
                _timesink_db_path(runtime.config),
                terminal=(
                    None if runtime.terminal_hub is None
                    else TerminalScreen(runtime.terminal_hub.call, wait_s=TURN_WAIT_S)
                ),
                attachments=None if runtime.attachments is None else runtime.attachments.directory,
                shipped=runtime.runtime_paths.root / IMPORTS_DIRNAME,
                weather=None if runtime.home is None else runtime.home.day_weather_card,
            ),
            highlight_set=(
                None if runtime.memory is None
                else functools.partial(
                    _set_highlight,
                    HighlightMarks(
                        runtime.runtime_paths.event_log,
                        _day_sources(runtime, runtime.memory.db_path),
                    ),
                )
            ),
            doing_answer=(
                None if runtime.memory is None
                else functools.partial(
                    _answer_doing,
                    DoingAnswers(
                        runtime.runtime_paths.event_log,
                        _day_sources(runtime, runtime.memory.db_path),
                    ),
                )
            ),
            doing_tidy=(
                None if runtime.doing is None
                else functools.partial(asyncio.to_thread, runtime.doing.tidy)
            ),
            commitment_act=functools.partial(
                _act_commitment, runtime.runtime_paths.event_log, local_zone,
            ),
            today_read=(
                None if runtime.home is None
                else functools.partial(
                    _serve_today, runtime.runtime_paths.event_log, local_zone,
                    runtime.home.weather,
                )
            ),
            todo_set=(
                None if runtime.home is None
                else functools.partial(
                    _set_todo, runtime.home, runtime.things_mirror,
                    runtime.runtime_paths.event_log, local_zone,
                )
            ),
            mail_read=(
                None if runtime.home is None
                else functools.partial(asyncio.to_thread, runtime.home.mail)
            ),
            mail_archive=(
                None if runtime.home is None else functools.partial(_archive_mail, runtime.home)
            ),
            mail_letter=None if mail_home is None else functools.partial(_mail_letter, mail_home),
            mail_summary=(
                None if mail_home is None else functools.partial(_mail_summary, mail_home)
            ),
            mail_mark_read=None if mail_home is None else functools.partial(_mail_read, mail_home),
            mail_trash=None if mail_home is None else functools.partial(_mail_trash, mail_home),
            view_set=None if runtime.view is None else runtime.view.set,
            **_draft_deps(runtime, mail_home),
            **_settings_notice_deps(
                _notice_deps(
                    runtime.job_mail, runtime.reminders, runtime.moment,
                    alerts=(
                        _alert_deps(
                            runtime.memory.db_path, lambda: controls.quiet, runtime.moment,
                            runtime.sound_output,
                        )
                        if spend is not None and runtime.memory is not None else None
                    ),
                ),
                runtime.settings_drift,
                lambda: not controls.conversation and not shared_ducker.outputting,
            ),
            moment_read=(
                None if runtime.moment is None else functools.partial(_moment_hold, runtime.moment)
            ),
            card_act=(
                None if window_memory is None
                else functools.partial(
                    _card_act,
                    CardFeedback(window_memory.db_path, lambda: controls.quiet, runtime.moment),
                )
            ),
            memory_page=(
                None if window_memory is None
                else MemoryPage(
                    window_memory.db_path,
                    runtime.settings,
                    section_caps=section_caps(runtime.config.get("core_memory")),
                    history_since=runtime.session.history_since,
                    event_log=runtime.runtime_paths.event_log,
                    zone=resolve_zone(None, _work_state_timezone(runtime.config))[1],
                )
            ),
            brief_read=(
                None if runtime.home is None
                else functools.partial(runtime.home.brief, runtime.conn)
            ),
            turn_end_asks=(
                None if runtime.turn_end_asks is None
                else functools.partial(_turn_end_asks, runtime.turn_end_asks)
            ),
            turn_end_peek=None if runtime.turn_end_asks is None else runtime.turn_end_asks.peek,
            turn_end_answered=(
                None if runtime.turn_end_asks is None else runtime.turn_end_asks.answered
            ),
            settings_read=(
                None if runtime.settings is None
                else functools.partial(asyncio.to_thread, runtime.settings.read)
            ),
            settings_update=(
                None if runtime.settings is None
                else functools.partial(_save_settings, runtime.settings)
            ),
            board_status=(
                None if runtime.settings is None or brain
                else functools.partial(asyncio.to_thread, respeaker_board.status)
            ),
            restart=_restart_soon if supervised() else None,
            data_export=functools.partial(asyncio.to_thread, _export_data, runtime),
            data_clear=functools.partial(asyncio.to_thread, _clear_media, runtime),
            data_erase=(
                functools.partial(_erase_data, runtime)
                if supervised() else None
            ),
            claude_sessions_read=_claude_sessions_served(runtime),
            agent_marks_path=runtime.runtime_paths.root / "agent-marks.json",
            plugin_read=runtime.plugin_connections.read if runtime.plugin_connections else None,
            plugin_action=runtime.plugin_connections.action if runtime.plugin_connections else None,
            plugin_authorize=(
                manager_authorize(
                    runtime.plugin_connections.settings.matches,
                    functools.partial(device_token_matches, runtime.runtime_paths.root)
                    if runtime.listen_addresses
                    else None,
                )
                if runtime.plugin_connections
                else None
            ),
            plugin_icon=runtime.plugin_connections.icon if runtime.plugin_connections else None,
            language_save=functools.partial(save_language, runtime.runtime_paths.settings),
            setup=setup,
            dictation=dictation,
            night=runtime.night,
            codex_board=codex_board,
            cancel_response_callable=cancel_response_callable,
            cancel_turn_callable=(
                make_turn_cancel_callable(runtime) if cancel_response_callable is not None else None
            ),
            controls=controls,
            dismiss_callable=(
                None if duplex_voice_session is None else duplex_voice_session.dismiss
            ),
            live=live_voice,
            terminals=runtime.terminal_hub if runtime.listen_addresses else None,
            phone_events=runtime.phone_events if runtime.listen_addresses else None,
            lifelog=(lifelog_archive := _lifelog(runtime) if runtime.listen_addresses else None),
            lifelog_coverage=(
                None if lifelog_archive is None else _lifelog_coverage(runtime, lifelog_archive)
            ),
            imports_intake=_shipments(runtime) if runtime.listen_addresses else None,
            phone=phone_hub,
            push_register=(
                functools.partial(register_body, runtime.runtime_paths.root)
                if runtime.listen_addresses
                else None
            ),
            ride_act=None if runtime.rides is None else functools.partial(_ride_act, runtime.rides),
            ride_read=None if runtime.rides is None else functools.partial(
                asyncio.to_thread, runtime.rides.view,
            ),
            claude_request=None if runtime.push is None else runtime.push.claude_request,
            claude_prompt_log=runtime.runtime_paths.root / "claude-prompts.jsonl",
            device_name=(
                functools.partial(device_name_for_token, runtime.runtime_paths.root)
                if runtime.listen_addresses
                else None
            ),
            # ADR 0196: routes on every daemon, but only a brain that listens can mint a code.
            pairing=DevicePairing(
                root=runtime.runtime_paths.root,
                codes=PairingCodes(runtime.runtime_paths.root),
                listens=bool(runtime.listen_addresses),
                brain_urls=brain_urls(runtime.listen_addresses, runtime.listen_hosts, port),
            ),
            v2=InherentV2Deps(
                token_matches=functools.partial(inherent_v2_token_matches, v2_token),
                device_token_matches=(
                    functools.partial(device_token_matches, runtime.runtime_paths.root)
                    if runtime.listen_addresses
                    else None
                ),
                submit_text=functools.partial(
                    _submit_text_v2,
                    runtime.runtime_paths.event_log,
                ),
                submit_asr=(
                    None
                    if voice_pipeline_callable is None
                    else functools.partial(
                        _submit_asr_v2,
                        runtime.runtime_paths.event_log,
                        voice_pipeline_callable,
                    )
                ),
            ),
        )
        app = create_app(deps)
        # The desktop, the CLI and the Claude Code / Codex hooks read this key
        # from the runtime root; nothing else on the machine can call the daemon.
        require_local_key(
            app,
            functools.partial(local_key_matches, local_key(runtime.runtime_paths.root)),
            extra_hosts=runtime.listen_hosts,
            device_token_matches=(
                functools.partial(device_token_matches, runtime.runtime_paths.root)
                if runtime.listen_addresses
                else None
            ),
            open_claim=bool(runtime.listen_addresses),
        )

        config = uvicorn.Config(
            app=app,
            host=host,
            port=port,
            log_level="warning",
            lifespan="off",
        )
        server = _Server(config)

        # ADR-0008 D8 (Step 4). Adoption and the recovery scan happen
        # inside the startup barrier, before any task exists that could
        # race them.
        watchers.extend(
            await _start_intent_pump(
                runtime,
                poll_interval_s=poll_interval_s,
                queue_capacity=_positive_int(
                    runtime.config,
                    section="input",
                    key="queue_capacity",
                    fallback=8,
                ),
                max_concurrent_turns=_positive_int(
                    runtime.config,
                    section="input",
                    key="max_concurrent_turns",
                    fallback=2,
                ),
            ),
        )
        watchers.append(
            asyncio.create_task(
                _response_watcher(
                    runtime,
                    broadcaster,
                    poll_interval_s=poll_interval_s,
                    voiced=tts_pipe is not None,
                ),
                name="response_watcher",
            ),
        )
        watchers.append(
            asyncio.create_task(
                _tool_status_watcher(runtime, broadcaster, poll_interval_s=poll_interval_s),
                name="tool_status_watcher",
            ),
        )
        if setup is not None:
            # The keys' boot check: a model list and one word of TTS, off the loop;
            # failures are logged and served by GET /inherent/setup.
            watchers.append(
                asyncio.create_task(asyncio.to_thread(setup.check_at_boot), name="setup_key_check"),
            )
        if tts_pipe is not None:
            watchers.append(
                asyncio.create_task(
                    _tts_watcher(
                        conn=runtime.conn,
                        pipeline=tts_pipe,
                        poll_interval_s=poll_interval_s,
                        broadcaster=broadcaster,
                        text_only=(
                            None if runtime.response_runs is None
                            else runtime.response_runs.turn_text_only
                        ),
                    ),
                    name="tts_watcher",
                ),
            )
        if built_phone is not None:
            # ADR 0222: a phone voice answer that ends with no actor for its device still ends.
            watchers.append(
                asyncio.create_task(
                    _phone_ends_watcher(
                        runtime.conn, built_phone[1], poll_interval_s=poll_interval_s,
                    ),
                    name="phone_ends_watcher",
                ),
            )
        if runtime.response_flags.lifecycle_commentary:
            # ADR-0008 D6 (Step 5). Flag off, no task exists and the event log
            # is byte-identical to a build without this observer.
            watchers.append(
                asyncio.create_task(
                    _commentary_watcher(runtime, poll_interval_s=poll_interval_s),
                    name="commentary_watcher",
                ),
            )

        # ADR-0009 D4 — the sweep control plane: anchor the system-trigger
        # watcher, THEN run the bootstrap sweep, THEN start the periodic
        # task. The ordering is load-bearing and lives inside the helper.
        # The session compaction rides the same tick: eligible only while
        # nothing is being written and no Live connection is open.
        compaction = (
            CompactionSweep(
                memory=runtime.memory,
                settings=runtime.session,
                llm_config=(
                    runtime.config["llm"]
                    if isinstance(runtime.config.get("llm"), Mapping)
                    else {}
                ),
                event_log_path=runtime.runtime_paths.event_log,
                pricing_table=load_pricing_table(repo_root() / "data" / "pricing.json"),
                context_length=lambda: preset_context_length(runtime.llm_client),
                live_open=lambda: (
                    live_voice is not None and live_voice.status().get("state") != "idle"
                ),
            )
            if runtime.memory is not None
            else None
        )
        watchers.extend(
            await _start_sweep_control_plane(
                runtime, poll_interval_s=poll_interval_s, compaction=compaction,
            ),
        )

        # ADR-0009 D5 — repo observer: baselines recovered from the log
        # first, then one more watchers-list task polling `observer.repos`
        # every `observer.poll_interval_s`. Nothing it emits is a trigger,
        # so it is wired after the sweep control plane without disturbing
        # that anchor-then-bootstrap ordering.
        watchers.extend(_start_repo_observer(runtime))
        watchers.extend(_start_usage_observer(usage_observer, runtime.config))
        watchers.extend(_start_timesink_observer(runtime))
        watchers.extend(_start_meaning_index(runtime))
        if runtime.job_mail is not None:
            # ADR 0155: she speaks only at quiet off, unmuted and outside a live conversation.
            job_mail = runtime.job_mail
            job_mail.quiet = lambda: controls.quiet
            job_mail.may_speak = lambda: not controls.speech_muted and not controls.conversation
            job_mail.say = functools.partial(_say_job_line, runtime)
            watchers.append(asyncio.create_task(job_mail.run(), name="job_mail"))
        if runtime.mail_codes is not None:
            watchers.append(asyncio.create_task(runtime.mail_codes.run(), name="mail_codes"))
        if runtime.commitments is not None:
            watchers.append(asyncio.create_task(runtime.commitments.run(), name="commitments"))
        if runtime.things is not None:
            # ADR 0299: the rings of his things for the next seven days, now and every 15 minutes.
            watchers.append(asyncio.create_task(runtime.things.run(), name="things"))
        if runtime.things_mirror is not None:
            # ADR 0299: Outlook and To Do read into the ledger, now and every 15 minutes.
            watchers.append(asyncio.create_task(runtime.things_mirror.run(), name="things_mirror"))
        if runtime.turn_things is not None:
            # ADR 0300: each record of his, once answered, read into the ledger.
            watchers.append(asyncio.create_task(runtime.turn_things.run(), name="turn_things"))
        if runtime.claude_daily is not None:
            watchers.append(asyncio.create_task(runtime.claude_daily.run(), name="claude_daily"))
        if spend is not None and runtime.memory is not None:
            # ADR 0185: the card is a job-alert row; _alert_deps serves it with job mail off too.
            watchers.append(asyncio.create_task(
                SpendCap(spend, runtime.runtime_paths.event_log, runtime.memory.db_path).run(),
                name="spend_cap",
            ))
        if runtime.work_state_auto and runtime.work_state and runtime.moment:
            # ADR 0180: a changed front app or site re-analyses the work state, silently.
            watchers.append(asyncio.create_task(
                auto_refresh(runtime.work_state, runtime.moment), name="work_state_auto",
            ))
        if runtime.reminders is not None:
            # ADR 0179: reminders speak when unmuted and out of a conversation, at any quiet level.
            reminders = runtime.reminders
            reminders.may_speak = lambda: not controls.speech_muted and not controls.conversation
            reminders.say = functools.partial(_say_reminder, runtime)
            watchers.append(asyncio.create_task(reminders.run(), name="reminders"))
        if runtime.rides is not None:
            # ADR 0224: the bus ride follower; it fetches a feed only while there is a reason.
            watchers.append(asyncio.create_task(runtime.rides.run(), name="rides"))
        if runtime.push is not None and runtime.push.enabled:
            # ADR 0210: a fired reminder pushes at any quiet level; waiting cards follow the level.
            push = runtime.push
            push.quiet = lambda: controls.quiet
            push.her_state = lambda: _her_state(controls)
            if phone_hub is not None:
                # ADR 0209: a device with a live conversation socket needs no push.
                push.phone_socket_open = phone_hub.connected
            if runtime.locator is not None:
                # ADR 0281: a phone with no socket is woken for a fix by a silent push.
                runtime.locator.ask_push = push.locate
            if runtime.whereabouts is not None:
                # ADR 0218: a request, or a card no turn asked, goes to the phone only away from
                # the Mac.
                push.at_mac = runtime.whereabouts.at_mac
            if runtime.plugin_connections is not None:
                # ADR 0232: an app's login link goes to the phone that asked for it.
                push.plugin_login = runtime.plugin_connections.login_waiting
            if runtime.reminders is not None:
                runtime.reminders.push = push.reminder
                push.next_reminder = runtime.reminders.next_due
            if runtime.rides is not None:
                runtime.rides.push = push
            watchers.append(asyncio.create_task(push.run(), name="push"))
        if runtime.night is not None:
            # ADR 0093: the night run mutes after the goodnight line, never under a spoken line.
            runtime.night.busy = lambda: shared_ducker.outputting
            # It holds the Mac past the deadline while a session it can see still works.
            runtime.night.watch = NightWatch(
                port=_agents_port(),
                key=functools.partial(local_key, runtime.runtime_paths.root),
                claude=ClaudeSessions() if _claude_sessions_read(runtime.config) else None,
                codex=codex_board,
            )
            watchers.append(asyncio.create_task(runtime.night.run(), name="night_run"))
        if runtime.daily_schedule is not None:
            watchers.append(
                asyncio.create_task(runtime.daily_schedule.run(), name="daily_report_schedule"),
            )
        day_summary = DaySummarySettings.from_config(runtime.config.get("day_summary"))
        if day_summary is not None and runtime.memory is not None:
            # ADR 0142: past days' conversation summaries, written once a day.
            llm_config = runtime.config.get("llm")
            watchers.append(
                asyncio.create_task(
                    DaySummarySchedule(
                        memory=runtime.memory,
                        settings=day_summary,
                        llm_config=llm_config if isinstance(llm_config, Mapping) else {},
                        event_log_path=runtime.runtime_paths.event_log,
                        pricing_table=load_pricing_table(repo_root() / "data" / "pricing.json"),
                        clock=runtime.clock,
                        core_memory=CoreMemorySettings.from_config(
                            runtime.config.get("core_memory"),
                        ),
                        jev_log=_jev_log(runtime.config, runtime.runtime_paths.root),
                        ledger=runtime.ledger,
                    ).run(),
                    name="day_summary_schedule",
                ),
            )
        if runtime.doing is not None:
            # ADR 0302, 0310: what he was doing, written for the days under way and just past.
            watchers.append(asyncio.create_task(runtime.doing.run(), name="doing_schedule"))
        day_experience =DayExperienceSettings.from_config(runtime.config.get("day_experience"))
        if day_experience is not None and runtime.memory is not None:
            # ADR 0280: past days' experience table, folded from the event log once a day.
            watchers.append(
                asyncio.create_task(
                    DayExperienceSchedule(
                        memory=runtime.memory,
                        settings=day_experience,
                        event_log_path=runtime.runtime_paths.event_log,
                    ).run(),
                    name="day_experience_schedule",
                ),
            )
        day_plan = DayPlanSettings.from_config(runtime.config.get("day_plan"))
        if day_plan is not None and runtime.memory is not None:
            # ADR 0288: the day's plan, drafted at `day_plan.at` and on a start with none for today.
            plan_zone = resolve_zone(None, _work_state_timezone(runtime.config))[1]
            home = runtime.home
            plan_sources = DayPlanSources(
                event_log_path=runtime.runtime_paths.event_log,
                memory_db=runtime.memory.db_path,
                zone=plan_zone,
                day_sources=DaySources(
                    plan_zone, _timesink_db_path(runtime.config),
                    runtime.memory.db_path,
                    None if runtime.terminal_hub is None
                    else TerminalScreen(runtime.terminal_hub.call, wait_s=TURN_WAIT_S),
                    None if runtime.attachments is None
                    else runtime.attachments.directory,
                ),
                weather=None if home is None else home.day_weather,
            )
            open_leg = (
                None if runtime.leg_planner is None else leg_session(
                    runtime.leg_planner, runtime.runtime_paths.event_log, runtime.runtime_paths,
                )
            )
            watchers.append(
                asyncio.create_task(
                    build_schedule(
                        day_plan,
                        config=runtime.config,
                        pricing_path=repo_root() / "data" / "pricing.json",
                        sources=plan_sources,
                        route_legs=(
                            None if open_leg is None
                            else make_route_legs(open_leg, runtime.memory.db_path)
                        ),
                    ).run(),
                    name="day_plan_schedule",
                ),
            )
            if day_plan.watch:
                # ADR 0291: notice what changed in the day, let her decide, tell him within limits.
                watchers.append(
                    asyncio.create_task(
                        build_watch(
                            day_plan,
                            config=runtime.config,
                            pricing_path=repo_root() / "data" / "pricing.json",
                            sources=plan_sources,
                            open_leg=open_leg,
                            hooks=_plan_hooks(runtime, controls),
                        ).run(),
                        name="plan_watch",
                    ),
                )
        if runtime.picture_notes is not None:
            runtime.picture_notes.start()  # recovers the notes still owed, then runs the queue
        watchers.append(asyncio.create_task(
            _data_sweep_task(
                _media_dirs(runtime), logs_dir(runtime.runtime_paths.root),
                functools.partial(_link_pictures, runtime),
                functools.partial(_release_pictures, runtime),
            ),
            name="data_sweep",
        ))
        if runtime.settings is not None and not brain:
            watchers.append(asyncio.create_task(_watch_board(runtime.settings), name="board_watch"))
        for watcher in watchers:
            watcher.add_done_callback(_log_watcher_death)

        # ADR-0009 D3 — power observer, installed after the lock (which
        # stays the outermost scope) and immediately before serving, so its
        # registered CFRunLoop thread is torn down first by the same
        # `_shutdown` that tears the watchers down; no path between install
        # and serve can leak a live registration.
        power_coordinator = (
            _VoicePowerCoordinator(
                session=duplex_voice_session,
                media=tts_pipe,
            )
            if duplex_voice_session is not None and tts_pipe is not None
            else None
        )
        if power_coordinator is not None and duplex_voice_session is not None:
            device_watch = _start_audio_device_watch(
                runtime.config, runtime.settings, power_coordinator,
                duplex_voice_session.ingress, live_voice,
            )
            device_watch.add_done_callback(_log_watcher_death)
            watchers.append(device_watch)
        before_sleep_hook = (
            power_coordinator.before_sleep if power_coordinator is not None else None
        )
        on_wake_hook = power_coordinator.on_wake if power_coordinator is not None else None
        # ADR 0170: a brain has no lid or sleep of its own to report.
        power_observer = (
            None
            if brain
            else _install_power_observer_or_degrade(
                runtime.conn,
                asyncio.get_running_loop(),
                before_sleep_hook=before_sleep_hook,
                on_wake_hook=on_wake_hook,
            )
        )

        await server.serve(
            sockets=_listen_sockets(host, port, runtime.listen_addresses)
            if runtime.listen_addresses
            else None,
        )


__all__ = ["serve_inherent"]
