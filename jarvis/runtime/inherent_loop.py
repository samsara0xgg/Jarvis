"""Daemon entrypoint: serve the Inherent FastAPI app + watcher tasks.

Composition root (``runtime/``). Owns the daemon lifecycle for the
text Inherent surface introduced by ADR-0003 Step 1 (single-envelope)
and rewired for Step 2 (three-envelope ``open`` / ``append`` x N /
``done`` wire schema):

1. Acquire the per-runtime-root process lock via
   :func:`jarvis.deployment.process_lock.acquire_exclusive` (ADR-0003
   D4 — refuses if another daemon already holds it).
2. Build the shared :class:`jarvis.surface.inherent_output.InherentBroadcaster`
   and a sync ``submit_callable`` bound to
   :func:`jarvis.surface.cli.emit_surface_user_intent` (mints a fresh
   ``turn_id`` per call).
3. Build the FastAPI app via
   :func:`jarvis.surface.inherent_server.create_app`.
4. Spawn two background async tasks:

   - :func:`_user_intent_watcher` — polls the Event Log for new
     ``surface.user_intent`` rows; for each, drives the turn via
     :func:`jarvis.runtime.drive_turn` on a worker thread with
     ``streaming_enabled=True`` so :func:`render_response` emits the
     ADR-0003 Step 2 three-event Inherent taxonomy
     (``surface.response_open`` + ``surface.response_chunk`` x N) before
     the audit ``surface.response_emitted``. ADR-0003 D9 F3: an uncaught
     exception from ``drive_turn`` is caught, logged, and recorded as a
     ``turn.failed`` audit event; the watcher keeps polling.
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
6. On exit: cancel both watchers, await with ``return_exceptions=True``,
   then release the process lock as the OUTERMOST cleanup step (the
   ``acquire_exclusive`` context manager unwinds last).

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
import datetime
import functools
import json
import logging
import math
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final, Literal

import uvicorn

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from jarvis.deployment.sleep_wake import PowerObserver
    from jarvis.runtime.work_state import WorkStateService
    from jarvis.shared.realtime import PresentationIntent
    from jarvis.state.committed_event_bus import CommittedEventBus

from jarvis.decision.commentary import (
    COMMENTARY_ATTENTION_CHANNEL,
    commentary_intent_for,
    commentary_speech_text,
)
from jarvis.decision.gates import ResponsePlan, pre_emit_gate
from jarvis.decision.response_run import (
    ResponseCancelledError,
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
from jarvis.deployment import inherent_v2_token_matches, rotate_inherent_v2_token
from jarvis.deployment.launchd import repo_root
from jarvis.deployment.process_lock import acquire_exclusive
from jarvis.deployment.sleep_wake import install_power_observer, sweep_overdue_actions
from jarvis.execution.tools import live_action_ids
from jarvis.runtime import (
    JarvisRuntime,
    TriggerWaitTimeout,
    TurnSuspended,
    WaitingTurn,
    _confirmation_expiry_sweep_interval_s,
    _durable_confirmation_expiry_enabled,
    _event_action_id,
    _new_turn_id,
    _observer_poll_interval_s,
    _observer_repo_paths,
    _positive_float,
    _positive_int,
    _timesink_db_path,
    _timesink_poll_interval_s,
    _wait_for_next_trigger,
    drive_turn,
    make_barge_in_interrupt_callable,
    make_foreground_decision_callable,
    make_response_cancel_callable,
)
from jarvis.runtime.inherent_hub import start_inherent_view
from jarvis.runtime.session_compaction import CompactionSweep, preset_context_length
from jarvis.shared import Event
from jarvis.shared.pricing import load_pricing_table
from jarvis.shared.realtime import (
    AlreadyTerminal,
    StaleConfirmation,
    TerminalCommitted,
    TerminalOutcome,
    new_boot_id,
    new_connection_id,
    new_response_id,
)
from jarvis.shared.realtime_trace import record_realtime_trace
from jarvis.state.event_log import (
    emit_event,
    get_event,
    iter_events_for_turn,
    iter_events_of_types,
    open_event_log,
    open_runtime_event_log,
    read_log_epoch,
)
from jarvis.state.input_claim import (
    REALTIME_INTENT_CONSUMER,
    ConflictingTurnClaimError,
    InputClaimed,
    InputClaimError,
    adopt_consumer,
    claim_input_once,
    recoverable_inputs,
)
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
from jarvis.state.lifecycle_terminal import terminalize_confirmation
from jarvis.state.memory_db import (
    MemorySettings,
    SessionSettings,
    append_record,
    brief_note,
    conversation_rows,
)
from jarvis.state.projections import PendingConfirmations, rebuild_projections
from jarvis.state.trigger_consumption import trigger_was_consumed
from jarvis.surface import (
    voice_asr,
    voice_audio,
    voice_backend,
    voice_controls,
    voice_ducking,
    voice_live,
    voice_media,
    voice_pipeline,
    voice_session,
    voice_tts,
    voice_wake,
)
from jarvis.surface.cli import SurfaceState, emit_surface_user_intent, record_pre_emit_token
from jarvis.surface.cli_render import render_response
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_protocol import RuntimeCapabilities
from jarvis.surface.inherent_server import (
    InherentDeps,
    InherentV2Deps,
    InputSubmissionOutcome,
    create_app,
)
from jarvis.surface.playback_recovery import reconcile_open_playback
from jarvis.surface.repo_observer import RepoObserver
from jarvis.surface.timesink_observer import TimesinkHead, TimesinkObserver
from jarvis.surface.usage_observer import UsageConfig, UsageObserver, latest_usage

LOGGER = logging.getLogger("jarvis.runtime.inherent_loop")


# Default poll cadence for both watchers; 10 ms balances CPU against
# first-byte latency between an L2 INSERT (Step 7 submit handler / L3
# render emission) and the watcher's reaction.
_DEFAULT_POLL_INTERVAL_S: float = 0.01

# Default port the Inherent client connects to. Picked to NOT collide
# with the legacy ``ui/web/server.py`` default (8000) or the
# inherent-swift dev server (8001).
_DEFAULT_PORT: int = 8006

# --- ADR-0009 D4 "System turns are silent, enforced" ------------------------
#
# The daemon streams EVERY turn (``streaming_enabled=True``), so both
# response consumers below see every turn's open/chunk/emitted triple
# regardless of where L3 routed it. Step 8 gives them the L3 verdict:
# ``attention_channel`` now rides the ``surface.response_open`` header
# (ADR-0009 §4 registry amendment), which is the one event that arrives
# BEFORE any chunk — the only place a turn can be dropped before it is
# spoken.
#
# TTS suppression set. A supervisor-sweep orphan closure at 3am drives a
# system turn whose Limitation routes to ``queue_review`` (the ADR-0002
# Limitation-routing amendment); ``silent_log`` is the other non-speaking
# verdict ``attention_policy`` can return. Neither channel lists a voice
# surface in ``ATTENTION_CHANNEL_TO_SURFACES``, so feeding their chunks
# to TTS contradicts the routing table and spec §3.2.5 安静优先.
#
# ``gpt_live`` is the odd one out: it is an INTENT channel (where the turn was
# submitted from), not an attention channel, and it is matched against a
# different payload field — see :func:`_drop_for_silent_channel`. ADR-0016 D8:
# while a Live session owns the speaker, the local chain must not synthesize
# that turn at all. It is deliberately absent from the broadcaster set below,
# because Resonance still shows the full answer.
_TTS_SILENT_CHANNELS: frozenset[str] = frozenset(
    {"queue_review", "silent_log", "badge_card", "gpt_live"},
)

# WS-broadcaster suppression set — deliberately NARROWER than the TTS
# set, and this asymmetry is load-bearing:
#
# - ``silent_log`` maps to ``()`` in ``ATTENTION_CHANNEL_TO_SURFACES``:
#   no physical surface at all. Dropping its envelopes is exactly what
#   aligns the wire with the routing table.
# - ``queue_review`` maps to ``("cli_stdout",)`` — it is a TEXT channel:
#   the verdict for reconciliation terminals and needs-review worker
#   reports (jarvis/decision/gates.py ``attention_policy``; an ordinary
#   user utterance defaults to ``voice_notify``). The daemon passes
#   ``available_surfaces=frozenset()``, so this WS is the substitute for
#   that ``cli_stdout``. Suppressing it would blank the Inherent text
#   surface for those turns and hang ADR-0009 D2's forwarding CLI until
#   its 120s timeout (exit 4).
#
# "Silent" in D4 means no audio, not no text: the queue_review turn
# still lands on the card so Allen can read it when he comes back —
# which is what "queue for review" means.
_BROADCAST_SILENT_CHANNELS: frozenset[str] = frozenset({"silent_log"})

# Watcher cursor SELECT — placeholders only, no user-controlled
# interpolation. Mirrors the column order of
# ``jarvis.state.event_log._SELECT_ALL_ORDERED_SQL``. The type filter
# is an ``IN (...)`` list with a dynamically-built placeholder run so
# the watcher can fold multiple event types into one cursor (ADR-0005
# §5.1: the inherent-loop user-intent watcher folds BOTH
# ``surface.user_intent`` (keyboard) and ``utterance.received`` (voice)
# into one driver path). Placeholder count is interpolated from the
# tuple length passed in by the caller; tuple contents go through
# placeholders, never interpolation.
_SELECT_AFTER_ID_OF_TYPES_SQL_TEMPLATE = (
    "SELECT id, event_uid, type, schema_version, ts_epoch_ms, "
    "payload_json, source_event_id, correlation_json "
    "FROM events WHERE id > ? AND type IN ({placeholders}) ORDER BY id ASC"
)

# Single-cursor SELECT for the three Step-2 Inherent response event
# types. Static type list (no placeholders): asyncio's wakeup order
# does NOT guarantee that three sibling watchers (one per type) would
# fire in L2-insertion order, so we fold all three types into ONE
# cursor ordered by SQLite row id — D16 race elimination by
# construction.
_SELECT_RESPONSE_EVENTS_AFTER_ID_SQL = (
    "SELECT id, event_uid, type, schema_version, ts_epoch_ms, "
    "payload_json, source_event_id, correlation_json "
    "FROM events WHERE id > ? AND type IN ("
    "'surface.response_open', 'surface.response_chunk', "
    "'surface.response_emitted'"
    ") ORDER BY id ASC"
)


# The voice ASR / VAD artifact locations come from ``runtime.sensevoice_dir``
# / ``runtime.silero_vad_path`` (ADR-0006 §5), resolved once at the composition
# root against the config file's own directory. ADR-0005 §12 (pre-flight) —
# ``serve_inherent`` checks them BEFORE spawning the WakeListener so a missing
# wheel surfaces as a single log line instead of a crashed daemon thread on
# first wake. Tests pin their own paths via :func:`_voice_models_preflight`
# kwargs.

# Legacy wake capture bounds (ADR-0005 §5.1).  Deliberately NOT configurable:
# the single-ingress owner already has ``max_utterance_s`` / ``min_voiced_s``
# and is the path in production, so a key here would be honoured by one input
# owner and ignored by the other.
_DEFAULT_CAPTURE_MAX_DURATION_S: float = 5.0
_DEFAULT_CAPTURE_MIN_VOICED_S: float = 1.0
# Wake input stream params — ADR §5.1 (openwakeword expects 16 kHz mono PCM16
# at 1280-sample / 80 ms blocks). A SEPARATE stream from the recorder's per
# legacy ``core/inherent_wake_listener.py`` parity (the recorder's 32-ms VAD
# chunks would force openwakeword to buffer across reads).
_WAKE_SAMPLE_RATE_HZ: int = 16000
_WAKE_FRAME_SAMPLES: int = 1280

# The two ``SileroVad`` profiles the daemon can bind (``voice_audio``'s
# ``_MODE_THRESHOLDS`` keys).  A closed set: ADR-0006 D8 names both and
# ``realtime.single_audio_ingress.output_active_vad_mode`` selects between them.
_VAD_MODES: Final[tuple[str, ...]] = ("record", "tts")


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

    # openwakeword's detection probability gate.  BOTH input owners construct a
    # listener with it, which is why it is a flat key and is threaded to both
    # rather than living under ``realtime.single_audio_ingress``.
    wake_threshold: float = 0.5
    # Shutdown join deadline for the legacy wake thread.  The stream close that
    # follows it is the historical segfault site (see :func:`_shutdown_wake`),
    # so a machine that needs longer has a way to ask for it.
    wake_join_timeout_s: float = 2.0
    tts_voice: str = voice_tts.DEFAULT_TTS_VOICE
    tts_model: str = voice_tts.DEFAULT_TTS_MODEL
    tts_primary_endpoint: str = voice_tts.DEFAULT_TTS_PRIMARY_ENDPOINT
    tts_fallback_endpoint: str = voice_tts.DEFAULT_TTS_FALLBACK_ENDPOINT
    # MiniMax is asked for the highest rate it natively produces in our config
    # and resampled to the device rate via soxr on the way out.
    tts_sample_rate_in_hz: int = 32000
    # 30 s of headroom so the full-buffer write() of a long response (typical
    # 5-30 s of f32 PCM) lands in one shot; the player's own 2 s default forces
    # write() to block on the drain and hit its 10 s timeout, dropping the tail
    # of any response longer than ~10 s.
    tts_ring_seconds: float = 30.0
    tts_connect_timeout_s: float = voice_tts.DEFAULT_TTS_CONNECT_TIMEOUT_S
    tts_task_start_timeout_s: float = voice_tts.DEFAULT_TTS_TASK_START_TIMEOUT_S
    tts_first_chunk_timeout_s: float = voice_tts.DEFAULT_TTS_FIRST_CHUNK_TIMEOUT_S
    tts_between_chunk_timeout_s: float = voice_tts.DEFAULT_TTS_BETWEEN_CHUNK_TIMEOUT_S
    tts_total_timeout_s: float = voice_tts.DEFAULT_TTS_TOTAL_TIMEOUT_S
    tts_session_close_timeout_s: float = voice_tts.DEFAULT_TTS_SESSION_CLOSE_TIMEOUT_S
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


def _voice_knobs(config: Mapping[str, Any]) -> _VoiceKnobs:
    """Resolve every flat ``realtime:`` voice value once (ADR-0006 §5)."""
    block = config.get("realtime")
    values: Mapping[str, Any] = block if isinstance(block, Mapping) else {}
    d = _VoiceKnobs()
    return _VoiceKnobs(
        wake_threshold=_knob_number(values, "wake_threshold", d.wake_threshold),
        wake_join_timeout_s=_knob_number(
            values, "wake_join_timeout_s", d.wake_join_timeout_s,
        ),
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
        tts_ring_seconds=_knob_number(values, "tts_ring_seconds", d.tts_ring_seconds),
        tts_connect_timeout_s=_knob_number(
            values, "tts_connect_timeout_s", d.tts_connect_timeout_s,
        ),
        tts_task_start_timeout_s=_knob_number(
            values, "tts_task_start_timeout_s", d.tts_task_start_timeout_s,
        ),
        tts_first_chunk_timeout_s=_knob_number(
            values, "tts_first_chunk_timeout_s", d.tts_first_chunk_timeout_s,
        ),
        tts_between_chunk_timeout_s=_knob_number(
            values, "tts_between_chunk_timeout_s", d.tts_between_chunk_timeout_s,
        ),
        tts_total_timeout_s=_knob_number(
            values, "tts_total_timeout_s", d.tts_total_timeout_s,
        ),
        tts_session_close_timeout_s=_knob_number(
            values, "tts_session_close_timeout_s", d.tts_session_close_timeout_s,
        ),
        vad_profiles=_vad_profiles(values.get("vad")),
    )


def _voice_models_preflight(
    *,
    sensevoice_dir: Path,
    silero_path: Path,
) -> tuple[bool, list[str]]:
    """Verify on-disk model artifacts before spawning wake listener.

    ADR-0005 §12 — pre-flight semantic: do NOT attempt to construct a
    SenseVoice recognizer or Silero VAD when the underlying ``.onnx``
    files are not present. Returning ``(False, [...])`` lets
    :func:`serve_inherent` log one ERROR and keep the text path
    running; the alternative (constructing the recognizer eagerly)
    would let the wake thread crash on its first inference frame and
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


def _latest_id(conn: sqlite3.Connection) -> int:
    """Return MAX(events.id) or 0 if the table is empty.

    Used by each watcher at startup to anchor its cursor so events that
    landed BEFORE the daemon came up are not replayed.
    """
    cursor = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events")
    row = cursor.fetchone()
    if row is None:
        return 0
    return int(row[0])


def _row_to_id_event(row: tuple[Any, ...]) -> tuple[int, Event]:
    """Re-hydrate an events-table row into ``(id, Event)``.

    Mirrors :func:`jarvis.state.event_log._row_to_event` (module-private
    by L2 design — see the precedent comment in
    :func:`jarvis.runtime._hydrate_event_row`); also surfaces the
    SQLite ``id`` so the watcher can advance its cursor without a
    second SELECT.
    """
    (
        id_,
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
    return int(id_), Event(
        event_uid=event_uid,
        type=type_,
        schema_version=schema_version,
        ts_epoch_ms=ts_epoch_ms,
        payload=payload,
        source_event_id=source_event_id,
        correlation=correlation,
    )


def _fetch_events_after(
    conn: sqlite3.Connection,
    *,
    after_id: int,
    event_types: tuple[str, ...],
) -> list[tuple[int, Event]]:
    """SELECT every event whose type is in ``event_types`` with ``id > after_id``.

    ADR-0005 §5.1: callers may pass multiple event types so a single
    cursor can drive turns from both keyboard
    (``surface.user_intent``) and voice (``utterance.received``)
    surfaces with one polling loop. The SQL ``IN (...)`` placeholder
    list is built from ``len(event_types)``; the type values
    themselves pass through SQLite placeholders (no string
    interpolation of user data).

    Returns a materialized list of ``(id, Event)`` tuples (not an
    iterator) because the watcher loop folds the cursor before its next
    poll; lifetime safety beats streaming for ~tens of rows. The ``id``
    is the SQLite row id used to advance the cursor.
    """
    placeholders = ",".join("?" * len(event_types))
    sql = _SELECT_AFTER_ID_OF_TYPES_SQL_TEMPLATE.format(placeholders=placeholders)
    cursor = conn.execute(sql, (after_id, *event_types))
    return [_row_to_id_event(row) for row in cursor]


def _fetch_response_events_after(
    conn: sqlite3.Connection,
    *,
    after_id: int,
) -> list[tuple[int, Event]]:
    """SELECT every ``surface.response_{open,chunk,emitted}`` with ``id > after_id``.

    Returns ``(id, Event)`` tuples oldest-first, ordered by SQLite row
    id so the three event types are interleaved in their L2-insertion
    order — see :func:`_response_watcher` for the D16 rationale.
    """
    cursor = conn.execute(_SELECT_RESPONSE_EVENTS_AFTER_ID_SQL, (after_id,))
    return [_row_to_id_event(row) for row in cursor]


_SELECT_TURN_INTENT_CHANNEL_SQL = (
    "SELECT json_extract(payload_json, '$.channel') FROM events "
    "WHERE type = 'surface.user_intent' "
    "AND json_extract(payload_json, '$.turn_id') = ? LIMIT 1"
)


def _turn_intent_channel(conn: sqlite3.Connection, turn_id: str) -> str | None:
    """The channel ``turn_id`` was submitted on, from its ``surface.user_intent``.

    The intent channel (``gpt_live``, the v2 surface label, ...) never reaches
    the ``surface.response_open`` header. That row carries its own ``channel``
    key, but it holds the PRESENTATION split — ``both`` / ``speech`` /
    ``document``, computed in ``cli_render`` from which text slices are
    non-empty — so a consumer that must suppress a whole turn by where the turn
    came from has to read the submission row instead.

    ``None`` when the turn has no submission row at all (a reconciliation or
    supervisor-sweep turn), which keeps :func:`_drop_for_silent_channel`'s
    opt-in-by-explicit-label default: unknown origin is not silent.
    """
    if not turn_id:
        return None
    row = conn.execute(_SELECT_TURN_INTENT_CHANNEL_SQL, (turn_id,)).fetchone()
    channel = row[0] if row is not None else None
    return channel if isinstance(channel, str) else None


def _drop_for_silent_channel(  # noqa: PLR0913 - two verdict sources, one bookkeeping set
    event: Event,
    *,
    turn_id: str,
    silent_turns: set[str],
    silent_channels: frozenset[str],
    consumer: str,
    intent_channel: str | None = None,
) -> bool:
    """True when ``event`` belongs to a turn ``consumer`` must not deliver.

    ADR-0009 D4. Shared by :func:`_response_watcher` and
    :func:`_tts_watcher`, which differ only in their ``silent_channels``
    set (see the two constants' comments for why the sets differ).

    The L3 ``attention_channel`` verdict rides the ``surface.response_open``
    header only (ADR-0009 §4), so a suppressed turn id is remembered
    across its chunks and forgotten when its ``surface.response_emitted``
    row (or the run's ``response.cancelled`` / ``response.failed``
    terminal) arrives — the whole open/chunk*/emitted triple is dropped or
    none of it is.

    ``intent_channel`` is the second, parallel source of that verdict: the
    caller's :func:`_turn_intent_channel` lookup, supplied on the open only.
    Either label matching ``silent_channels`` suppresses the turn, so a
    consumer can silence a turn by its L3 routing verdict (ADR-0009 D4) or by
    where it was submitted from (ADR-0016 D8) through one mechanism. Callers
    that pass nothing keep the header-only behaviour exactly.

    A missing (or non-string) ``attention_channel`` is deliberately NOT
    silent: the pre-Step-8 behaviour — deliver and speak — stays the
    default, so an emitter that predates the field (legacy rows, direct
    ``emit_event`` callers) never loses its output. Suppression is
    opt-in by an explicit channel label.
    """
    if event.type == "surface.response_open":
        channel = next(
            (
                label
                for label in (event.payload.get("attention_channel"), intent_channel)
                if isinstance(label, str) and label in silent_channels
            ),
            None,
        )
        if channel is None:
            return False
        silent_turns.add(turn_id)
        LOGGER.info(
            "%s: turn_id=%s routed to %r — suppressed for this consumer (ADR-0009 D4).",
            consumer,
            turn_id,
            channel,
        )
        return True
    if turn_id not in silent_turns:
        return False
    if event.type in {"surface.response_emitted", "response.cancelled", "response.failed"}:
        silent_turns.discard(turn_id)
    return True


def _emit_turn_failed(
    conn: sqlite3.Connection,
    *,
    intent_event: Event,
    exception_repr: str,
) -> None:
    """Emit the watcher-level ``turn.failed`` audit event (ADR-0003 D9 F3).

    ``trigger_event_id`` is the originating ``surface.user_intent``
    event's ``event_uid`` so a future replay can join the failure back
    to the request that produced it.
    """
    turn_id = intent_event.payload.get("turn_id", "<unknown>")
    emit_event(
        conn,
        type="turn.failed",
        payload={
            "turn_id": str(turn_id),
            "exception_repr": exception_repr,
            "trigger_event_id": intent_event.event_uid,
        },
        # ADR-0016 D5: correlated so a Live delegation's lookup by turn finds it.
        correlation={"turn_id": str(turn_id)},
        ts_epoch_ms=int(time.time() * 1000),
    )


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
    same ``check_same_thread`` reason as the two reconcilers above. Returns
    the number of generations it closed.
    """
    conn = open_event_log(event_log_path)
    try:
        events = reconcile_open_playback(conn, committed_event_bus=committed_event_bus)
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
    return len(events)


class TurnConnectionUnavailableError(Exception):
    """The driver did not take ownership because its connection could not open."""


def _drive_turn_in_worker_thread(
    runtime: JarvisRuntime,
    *,
    user_intent_event: Event,
    continuation: WaitingTurn | None = None,
    suspend_when_waiting: bool = False,
) -> WaitingTurn | None:
    """Open a fresh SQLite connection and call :func:`drive_turn` on this thread.

    The watcher coroutine dispatches this function via
    :func:`asyncio.to_thread`. Because
    :func:`jarvis.state.event_log.open_runtime_event_log` opens connections with
    the default ``check_same_thread=True``, the parent ``runtime.conn``
    (opened on the event-loop thread) cannot be used here — we open a
    fresh per-call connection to the same DB file and pass a shallow
    :class:`JarvisRuntime` replacement to ``drive_turn``. The fresh
    connection is closed in ``finally`` regardless of the outcome.

    This mirrors the ``_bg_emit`` precedent in
    ``tests/unit/test_runtime_composition.py``:
    every cross-thread emit-site opens its own SQLite connection so the
    ``check_same_thread`` invariant holds.
    """
    try:
        worker_conn = open_runtime_event_log(runtime.runtime_paths.event_log)
    except sqlite3.Error as exc:
        raise TurnConnectionUnavailableError(str(exc)) from exc
    try:
        worker_runtime = dataclasses.replace(runtime, conn=worker_conn)
        drive_turn(
            worker_runtime,
            user_intent_event=user_intent_event,
            available_surfaces=frozenset(),
            streaming_enabled=True,
            suspend_when_waiting=suspend_when_waiting,
            continuation=continuation,
        )
    except TurnSuspended as suspended:
        return suspended.checkpoint
    finally:
        with contextlib.suppress(sqlite3.Error):
            worker_conn.close()
    return None


# Event types the inherent-loop user-intent watcher folds into ONE
# cursor. Per ADR-0005 §5.1 "Important wiring detail", both the
# keyboard surface (``surface.user_intent`` from cli/__main__.py and
# the daemon /inherent/submit handler) AND the voice surface
# (``utterance.received`` from the ASR pipeline / wake listener) drive
# a turn through the same composition-root code path. The downstream
# ``drive_turn`` (and thus L3 / L4 / L5) is identical regardless of
# which event type arrived — the watcher passes the event verbatim as
# ``user_intent_event`` to :func:`drive_turn`, which reads
# ``payload["transcript"]`` and ``payload["turn_id"]`` (both schemas
# require these keys per the L2 event registry).
_USER_INTENT_TRIGGER_TYPES: tuple[str, ...] = (
    "surface.user_intent",
    "utterance.received",
)


async def _user_intent_watcher(
    runtime: JarvisRuntime,
    *,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
) -> None:
    """Background task: drive a turn for each new user-intent row.

    Folds two event types into one cursor — ADR-0005 §5.1
    "Important wiring detail":

    - ``surface.user_intent``  — keyboard surface (cli/__main__.py
      and the daemon /inherent/submit HTTP handler).
    - ``utterance.received``    — voice surface (the ASR pipeline /
      wake listener emits this after VAD-segmented audio is
      normalized).

    Both events carry ``transcript`` + ``turn_id`` in their payload
    (event-log registry requirement), so :func:`drive_turn` accepts
    either verbatim as ``user_intent_event``; the L3 / L4 / L5
    downstream is identical regardless of input channel.

    Lifecycle:

    1. Anchor the cursor at ``MAX(events.id)`` so events that landed
       before the watcher started are not replayed.
    2. Poll every ``poll_interval_s`` seconds. For each new row of
       either trigger type, dispatch
       :func:`_drive_turn_in_worker_thread` via
       :func:`asyncio.to_thread`. The thread offload is mandatory:
       ``drive_turn`` blocks on :func:`jarvis.runtime._wait_for_next_trigger`'s
       ``time.sleep`` polling, which would freeze the event loop.
    3. On uncaught :class:`Exception` (ADR-0003 D9 F3): log a warning
       and emit ``turn.failed`` on the watcher's own ``runtime.conn``
       (event-loop thread). Continue iterating; the watcher does not
       crash on a single bad turn.

    Cancellation: re-raises :class:`asyncio.CancelledError` so
    :func:`serve_inherent`'s ``finally`` block can ``await`` the
    cancellation cleanly.
    """
    after_id = _latest_id(runtime.conn)
    LOGGER.info("user_intent_watcher started (after_id=%d)", after_id)
    try:
        while True:
            new_events = _fetch_events_after(
                runtime.conn,
                after_id=after_id,
                event_types=_USER_INTENT_TRIGGER_TYPES,
            )
            for row_id, ev in new_events:
                after_id = max(after_id, row_id)
                try:
                    await asyncio.to_thread(
                        _drive_turn_in_worker_thread,
                        runtime,
                        user_intent_event=ev,
                    )
                except ResponseCancelledError:
                    # ADR-0008 D10: a cancelled response is an operator
                    # decision, not a turn failure — it must not become
                    # turn.failed. The `continue` advances to the next
                    # trigger row, which is the right granularity.
                    LOGGER.info(
                        "user_intent_watcher: response cancelled for turn_id=%s",
                        ev.payload.get("turn_id"),
                    )
                    continue
                except Exception as exc:  # noqa: BLE001 — ADR-0003 D9 F3 catch-all: log + audit + continue.
                    LOGGER.warning(
                        "user_intent_watcher: drive_turn raised on turn_id=%s: %r",
                        ev.payload.get("turn_id"),
                        exc,
                    )
                    _emit_turn_failed(
                        runtime.conn,
                        intent_event=ev,
                        exception_repr=repr(exc),
                    )
            await asyncio.sleep(poll_interval_s)
    except asyncio.CancelledError:
        LOGGER.info("user_intent_watcher cancelled")
        raise


@dataclasses.dataclass(frozen=True)
class _IntentPumpBoot:
    """What the boot scan established before the pump started polling."""

    adoption_row_id: int
    live_cursor_id: int
    recovered: tuple[Event, ...]
    adopted_now: bool


def _boot_intent_pump_in_thread(event_log_path: Path) -> _IntentPumpBoot:
    """Record the adoption watermark and scan for inputs a restart still owes.

    ADR-0008 D8. Runs on an ``asyncio.to_thread`` worker with its OWN
    connection, for the same ``check_same_thread`` reason as the other boot
    reconcilers, and inside the startup barrier so no watcher can race it.

    The live cursor is read **before** the recovery scan. A row that lands
    between the two is then seen twice — once by the scan and once by the
    poll loop — and the pump's own dispatched-turn set collapses that, which
    is the safe direction; reading it after would let such a row fall through
    both and be lost.
    """
    conn = open_event_log(event_log_path)
    try:
        adoption = adopt_consumer(conn, name=REALTIME_INTENT_CONSUMER)
        live_cursor_id = _latest_id(conn)
        recovered = recoverable_inputs(
            conn,
            adoption_row_id=adoption.adoption_row_id,
            trigger_types=_USER_INTENT_TRIGGER_TYPES,
        )
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
    return _IntentPumpBoot(
        adoption_row_id=adoption.adoption_row_id,
        live_cursor_id=live_cursor_id,
        recovered=tuple(event for _row_id, event in recovered),
        adopted_now=adoption.adopted_now,
    )


def _claim_intent_in_thread(event_log_path: Path, trigger_event: Event) -> bool:
    """Durably claim one trigger; return whether this process should drive it.

    ``True`` for a fresh claim and for a claim this process finds unfinished
    (crash after claim, before any milestone — the scan only offers those).
    ``False`` for a trigger that cannot be driven at all, which is only the
    conflicting-turn_id case: two different utterances asserting one turn
    identity is a bug upstream, and driving either would be a guess.
    """
    conn = open_event_log(event_log_path)
    try:
        outcome = claim_input_once(conn, trigger_event=trigger_event)
    except ConflictingTurnClaimError:
        LOGGER.warning(
            "intent_pump: refusing trigger %s — its turn_id is claimed by another input",
            trigger_event.event_uid,
        )
        return False
    except InputClaimError:
        LOGGER.warning(
            "intent_pump: trigger %s cannot be claimed",
            trigger_event.event_uid,
            exc_info=True,
        )
        return False
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
    record_realtime_trace(
        "intent_claimed",
        turn_id=outcome.turn_id,
        trigger_event_uid=trigger_event.event_uid,
        fresh=isinstance(outcome, InputClaimed),
    )
    return True


async def _intent_pump_watcher(
    runtime: JarvisRuntime,
    queue: asyncio.Queue[Event | WaitingTurn],
    boot: _IntentPumpBoot,
    dispatched: set[str],
    *,
    poll_interval_s: float,
) -> None:
    """Claim every trigger durably, then hand it to a bounded turn queue.

    ADR-0008 D8's four ordered steps: poll the next committed trigger, claim
    it, enqueue the claimed turn, and advance the cursor **only after both**.
    The cursor therefore never runs ahead of durable work, and a full queue
    is backpressure — ``queue.put`` blocks here rather than dropping an
    utterance that is already on disk.

    ``dispatched`` is the in-process guard against driving one turn twice:
    the boot scan and the poll loop deliberately overlap, and a restart
    re-examines everything after the adoption watermark.
    """
    after_id = boot.live_cursor_id
    LOGGER.info(
        "intent_pump started (adoption_row_id=%d, after_id=%d, recovered=%d)",
        boot.adoption_row_id,
        after_id,
        len(boot.recovered),
    )
    try:
        for event in boot.recovered:
            await _offer_intent(runtime, queue, dispatched, event)
        while True:
            for row_id, event in _fetch_events_after(
                runtime.conn,
                after_id=after_id,
                event_types=_USER_INTENT_TRIGGER_TYPES,
            ):
                await _offer_intent(runtime, queue, dispatched, event)
                after_id = max(after_id, row_id)
            await asyncio.sleep(poll_interval_s)
    except asyncio.CancelledError:
        LOGGER.info("intent_pump cancelled")
        raise


async def _offer_intent(
    runtime: JarvisRuntime,
    queue: asyncio.Queue[Event | WaitingTurn],
    dispatched: set[str],
    event: Event,
) -> None:
    """Claim one trigger and enqueue it, unless this process already drove it."""
    turn_id = event.payload.get("turn_id")
    if isinstance(turn_id, str) and turn_id in dispatched:
        return
    if not await asyncio.to_thread(
        _claim_intent_in_thread,
        runtime.runtime_paths.event_log,
        event,
    ):
        return
    if isinstance(turn_id, str):
        dispatched.add(turn_id)
    await queue.put(event)
    record_realtime_trace(
        "intent_queue_accepted",
        turn_id=turn_id if isinstance(turn_id, str) else None,
        source="intent_pump",
        queue_depth=queue.qsize(),
    )


async def _advance_intent_step(
    runtime: JarvisRuntime, event: Event, continuation: WaitingTurn | None,
) -> WaitingTurn | None:
    """Keep thread ownership through coroutine cancellation and settle its handoff."""
    kwargs = {} if continuation is None else {"continuation": continuation}
    step = asyncio.create_task(asyncio.to_thread(
        _drive_turn_in_worker_thread, runtime,
        user_intent_event=event, suspend_when_waiting=True, **kwargs,
    ))
    try:
        return await asyncio.shield(step)
    except asyncio.CancelledError:
        # Cancelling to_thread does not stop its OS thread. Retain ownership
        # until the step hands back or settles itself.
        with contextlib.suppress(Exception):
            checkpoint = await step
            if checkpoint is not None:
                await _close_waiting_turn(runtime, checkpoint)
        raise


async def _intent_worker(
    runtime: JarvisRuntime,
    queue: asyncio.Queue[Event | WaitingTurn],
    waiting: dict[str, WaitingTurn] | None = None,
) -> None:
    """Drive claimed turns off the queue, one at a time, forever.

    One task per configured concurrent turn. Each drives its turn on a
    thread, which is what lets a second utterance be answered while a
    background worker from an earlier turn is still running — the property
    ADR-0008 Step 4 is built for.
    """
    try:
        while True:
            item = await queue.get()
            continuation = item if isinstance(item, WaitingTurn) else None
            event = item.intent if isinstance(item, WaitingTurn) else item
            if continuation is not None and waiting is not None:
                waiting.pop(str(event.payload["turn_id"]), None)
            try:
                checkpoint = await _advance_intent_step(runtime, event, continuation)
                if checkpoint is not None:
                    if waiting is None:
                        message = "suspended turn requires a continuation scheduler"
                        raise RuntimeError(message)  # noqa: TRY301 — invalid runtime wiring
                    waiting[str(event.payload["turn_id"])] = checkpoint
            except TurnConnectionUnavailableError as exc:
                if continuation is None or waiting is None:
                    _emit_turn_failed(
                        runtime.conn, intent_event=event, exception_repr=repr(exc),
                    )
                else:
                    # No driver finally ran: retain all response/action ownership
                    # and retry the same ready trigger without repeating dispatch.
                    waiting[str(event.payload["turn_id"])] = dataclasses.replace(
                        continuation, queued=False,
                    )
            except ResponseCancelledError:
                # ADR-0008 D10: an operator's cancel is not a turn failure.
                LOGGER.info(
                    "intent_pump: response cancelled for turn_id=%s",
                    event.payload.get("turn_id"),
                )
            except Exception as exc:  # noqa: BLE001 — ADR-0003 D9 F3 catch-all: log + audit + continue.
                LOGGER.warning(
                    "intent_pump: drive_turn raised on turn_id=%s: %r",
                    event.payload.get("turn_id"),
                    exc,
                )
                _emit_turn_failed(
                    runtime.conn,
                    intent_event=event,
                    exception_repr=repr(exc),
                )
            finally:
                queue.task_done()
    except asyncio.CancelledError:
        LOGGER.info("intent_pump worker cancelled")
        raise


def _poll_waiting_turn(runtime: JarvisRuntime, waiting: WaitingTurn) -> WaitingTurn | None:
    """Poll once on a fresh thread-local connection; never park an executor thread."""
    if waiting.run is not None and waiting.run.cancellation_token.is_cancelled:
        return dataclasses.replace(waiting, failure=ResponseCancelledError("response cancelled"))
    conn = open_runtime_event_log(runtime.runtime_paths.event_log)
    try:
        try:
            event, cursor = _wait_for_next_trigger(
                conn, after_id=waiting.after_id, action_ids=waiting.action_ids,
                lifecycle=runtime.lifecycle, timeout=0,
            )
        except TriggerWaitTimeout as exc:
            if time.monotonic() >= waiting.deadline:
                return dataclasses.replace(waiting, failure=exc)
            return None
        return dataclasses.replace(waiting, trigger=event, after_id=cursor)
    finally:
        conn.close()


async def _close_waiting_turn(runtime: JarvisRuntime, checkpoint: WaitingTurn) -> None:
    """Settle one suspended driver through its normal exception and cleanup path."""
    failed = dataclasses.replace(checkpoint, failure=RuntimeError("daemon shutdown"))
    with contextlib.suppress(Exception):
        await asyncio.to_thread(
            _drive_turn_in_worker_thread, runtime,
            user_intent_event=checkpoint.intent, continuation=failed,
        )


async def _waiting_turn_watcher(
    runtime: JarvisRuntime,
    queue: asyncio.Queue[Event | WaitingTurn],
    waiting: dict[str, WaitingTurn],
    *,
    poll_interval_s: float,
) -> None:
    """Requeue ready turns without consuming input workers during action waits."""
    try:
        while True:
            for turn_id, checkpoint in tuple(waiting.items()):
                if checkpoint.queued:
                    continue
                ready: WaitingTurn | None
                try:
                    if checkpoint.trigger is not None or checkpoint.failure is not None:
                        ready = checkpoint
                    else:
                        ready = await asyncio.to_thread(_poll_waiting_turn, runtime, checkpoint)
                except sqlite3.Error as exc:
                    # A transient read failure must not kill the sole scheduler
                    # or close unrelated turns. The existing wait deadline bounds retries.
                    if time.monotonic() < checkpoint.deadline:
                        continue
                    ready = dataclasses.replace(checkpoint, failure=exc)
                except Exception as exc:  # noqa: BLE001 — fail this turn, keep the scheduler alive.
                    ready = dataclasses.replace(checkpoint, failure=exc)
                if ready is not None:
                    waiting[turn_id] = dataclasses.replace(ready, queued=True)
                    await queue.put(ready)
            await asyncio.sleep(poll_interval_s)
    finally:
        # These turns still own live-action and response claims. Settle them
        # through the same driver finally; the runner retains physical cleanup.
        for checkpoint in tuple(waiting.values()):
            await _close_waiting_turn(runtime, checkpoint)
        waiting.clear()


async def _start_intent_pump(
    runtime: JarvisRuntime,
    *,
    poll_interval_s: float,
    queue_capacity: int,
    max_concurrent_turns: int,
) -> list[asyncio.Task[None]]:
    """Adopt the input stream, recover what a crash owes, and start the pump."""
    boot = await asyncio.to_thread(
        _boot_intent_pump_in_thread,
        runtime.runtime_paths.event_log,
    )
    if boot.adopted_now:
        LOGGER.info(
            "intent_pump adopted the input stream at events.id=%d; "
            "earlier utterances are history and are never replayed",
            boot.adoption_row_id,
        )
    queue: asyncio.Queue[Event | WaitingTurn] = asyncio.Queue(maxsize=queue_capacity)
    dispatched: set[str] = set()
    waiting: dict[str, WaitingTurn] = {}
    tasks = [
        asyncio.create_task(
            _intent_pump_watcher(
                runtime,
                queue,
                boot,
                dispatched,
                poll_interval_s=poll_interval_s,
            ),
            name="intent_pump_watcher",
        ),
    ]
    tasks.extend(
        asyncio.create_task(_intent_worker(runtime, queue, waiting), name=f"intent_worker_{index}")
        for index in range(max_concurrent_turns)
    )
    tasks.append(asyncio.create_task(
        _waiting_turn_watcher(runtime, queue, waiting, poll_interval_s=poll_interval_s),
        name="waiting_turn_watcher",
    ))
    return tasks


async def _response_watcher(
    runtime: JarvisRuntime,
    broadcaster: InherentBroadcaster,
    *,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
) -> None:
    """Background task: forward every new response event to the broadcaster.

    Single watcher with ONE cursor (``WHERE type IN
    ('surface.response_open', 'surface.response_chunk',
    'surface.response_emitted') ORDER BY id``) so the three event
    types are dispatched in their L2-insertion order. Per ADR-0003
    Step 2 D16: three sibling watchers would race against asyncio's
    wakeup order and could send ``op:append`` before ``op:open``;
    single-cursor + monotonic SQLite row id eliminates the race by
    construction.

    Dispatch by ``event.type``:

    - ``surface.response_open``    -> :meth:`InherentBroadcaster.broadcast_open`
    - ``surface.response_chunk``   -> :meth:`InherentBroadcaster.broadcast_chunk`
    - ``surface.response_emitted`` -> :meth:`InherentBroadcaster.broadcast_done`

    The broadcaster handles per-envelope wire translation and the F4 /
    F5 failure modes (per-client send failure isolation + no-clients
    warning).

    Cursor anchoring mirrors :func:`_user_intent_watcher` — events
    that landed BEFORE this watcher started are not replayed.

    Channel filter (ADR-0009 D4): a turn whose ``surface.response_open``
    header declares a channel in :data:`_BROADCAST_SILENT_CHANNELS` is
    dropped whole — open, chunks and done — so the wire never carries a
    turn the L3 routing table gives no surface to. The turn id is
    remembered from the open until its ``emitted`` closes it, because
    only the open header carries the channel. See the constant's
    comment for why this set is narrower than the TTS one.
    """
    after_id = _latest_id(runtime.conn)
    silent_turns: set[str] = set()
    LOGGER.info("response_watcher started (after_id=%d)", after_id)
    try:
        while True:
            new_events = _fetch_response_events_after(
                runtime.conn,
                after_id=after_id,
            )
            for row_id, ev in new_events:
                after_id = max(after_id, row_id)
                turn_id = str(ev.payload.get("turn_id", ""))
                if _drop_for_silent_channel(
                    ev,
                    turn_id=turn_id,
                    silent_turns=silent_turns,
                    silent_channels=_BROADCAST_SILENT_CHANNELS,
                    consumer="response_watcher",
                ):
                    continue
                if ev.type == "surface.response_open":
                    await broadcaster.broadcast_open(ev)
                elif ev.type == "surface.response_chunk":
                    await broadcaster.broadcast_chunk(ev)
                else:  # surface.response_emitted
                    await broadcaster.broadcast_done(ev)
            await asyncio.sleep(poll_interval_s)
    except asyncio.CancelledError:
        LOGGER.info("response_watcher cancelled")
        raise


async def _tts_watcher(  # noqa: C901, PLR0912 - ordered durable dispatch FSM
    *,
    conn: sqlite3.Connection,
    pipeline: object,  # voice_tts.TTSPipeline protocol; loosely typed to avoid cycles
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
) -> None:
    """Background task: feed every surface.response_* row into the TTSPipeline.

    Single cursor, three dispatch targets (``begin_turn`` / ``handle_chunk`` /
    ``handle_emitted``) per ADR-0005 §5.3 + §4.3 row ``_tts_watcher``. Runs
    in parallel with :func:`_response_watcher`; both are read-only polls on
    the same event stream so the D16 race-elimination argument that motivates
    a single in-order cursor for the broadcaster applies here verbatim — TTS
    must observe ``open`` before ``chunk`` and ``chunk`` before ``emitted``,
    which the single-cursor + monotonic SQLite row id gives us by construction.

    The ``pipeline`` parameter is typed as :class:`object` (rather than
    ``voice_tts.TTSPipeline``) so this composition-root module stays free
    of an L5 import cycle; the three method calls below pin the structural
    contract the watcher actually depends on. Production TTSPipeline methods
    only mutate state/enqueue owned work, so dispatch stays on the event-loop
    thread without blocking and never creates a default-executor TTS worker.

    Dispatch by ``event.type``:

    - ``surface.response_open``    -> ``pipeline.begin_turn(turn_id, gate_mode=...)``
      (``required_gate_mode`` from the payload, default ``"sentence"`` so a
      missing field — older event rows or non-voice-aware emitters — falls
      back to the safe full-sentence path).
    - ``surface.response_chunk``   -> ``pipeline.handle_chunk(turn_id, text)``
    - ``surface.response_emitted`` -> ``pipeline.handle_emitted(turn_id)``
    - ``response.cancelled`` / ``response.failed`` -> the streaming media
      owner only, which stops or drops that response's playback; the legacy
      pipeline has no cancel entry point and ignores them.

    Per-event dispatch is wrapped in a catch-all: a misbehaving TTS pipeline
    (e.g. MiniMax WebSocket drop, ``say`` subprocess error) must NOT crash
    the watcher because that would stall every subsequent turn. We log a
    warning and continue; the surface broadcaster keeps running on the
    parallel cursor, so the UI is unaffected.

    Channel filter (ADR-0009 D4 — "System turns are silent, enforced"):
    a turn whose ``surface.response_open`` header declares a channel in
    :data:`_TTS_SILENT_CHANNELS`, or which was submitted on one (ADR-0016
    D8 — a ``gpt_live`` delegation), never reaches the pipeline at all —
    ``begin_turn`` is not called, its chunks are dropped, and its
    ``emitted`` only clears the bookkeeping. Both labels are read once,
    at the open, so the suppressed turn ids are remembered until their
    ``emitted`` row closes them. This is a filter, not a switch: a
    ``voice_notify`` turn streams exactly as before.

    Cancellation: re-raises :class:`asyncio.CancelledError` so the daemon
    shutdown path (Task 19) can await the watcher cleanly.
    """
    streaming_pipeline = (
        pipeline if isinstance(pipeline, voice_media.StreamingTTSPipeline) else None
    )
    after_id = (
        streaming_pipeline.boot_high_water_event_log_id
        if streaming_pipeline is not None
        else _latest_id(conn)
    )
    silent_turns: set[str] = set()
    LOGGER.info("tts_watcher started (after_id=%d)", after_id)
    try:
        while True:
            new_events = _fetch_events_after(
                conn,
                after_id=after_id,
                event_types=(
                    "surface.response_open",
                    "surface.response_chunk",
                    "surface.response_emitted",
                    "response.cancelled",
                    "response.failed",
                ),
            )
            for row_id, ev in new_events:
                advance_cursor = True
                try:
                    turn_id = str(ev.payload.get("turn_id", ""))
                    if _drop_for_silent_channel(
                        ev,
                        turn_id=turn_id,
                        silent_turns=silent_turns,
                        silent_channels=_TTS_SILENT_CHANNELS,
                        consumer="tts_watcher",
                        intent_channel=(
                            _turn_intent_channel(conn, turn_id)
                            if ev.type == "surface.response_open"
                            else None
                        ),
                    ):
                        after_id = max(after_id, row_id)
                        continue
                    if streaming_pipeline is not None:
                        outcome = await streaming_pipeline.submit_event(
                            row_id=row_id,
                            event=ev,
                            origin="watcher",
                        )
                        if outcome.status == "overloaded":
                            # The Event Log is the durable queue. Retain the
                            # cursor so even the final committed row is retried
                            # after bounded media capacity becomes available.
                            advance_cursor = False
                            break
                        continue
                    if ev.type == "surface.response_open":
                        gate_mode = ev.payload.get("required_gate_mode", "sentence")
                        pipeline.begin_turn(  # type: ignore[attr-defined]
                            turn_id,
                            gate_mode=gate_mode,
                        )
                    elif ev.type == "surface.response_chunk":
                        text = str(ev.payload.get("text", ""))
                        pipeline.handle_chunk(  # type: ignore[attr-defined]
                            turn_id,
                            text,
                        )
                    elif ev.type == "surface.response_emitted":
                        pipeline.handle_emitted(  # type: ignore[attr-defined]
                            turn_id,
                        )
                except Exception as exc:  # noqa: BLE001 — log + continue; TTS must not crash watcher.
                    if streaming_pipeline is not None:
                        advance_cursor = False
                    LOGGER.warning(
                        "tts_watcher: dispatch raised on %s turn_id=%s: %r",
                        ev.type,
                        ev.payload.get("turn_id"),
                        exc,
                    )
                finally:
                    if advance_cursor:
                        after_id = max(after_id, row_id)
                if not advance_cursor:
                    break
            await asyncio.sleep(poll_interval_s)
    except asyncio.CancelledError:
        LOGGER.info("tts_watcher cancelled")
        raise


# --- ADR-0008 D6 lifecycle commentary ---------------------------------------
#
# The observer below is the whole of the D6 wiring. It is deliberately a
# durable-cursor watcher rather than a `CommittedEventBus` subscriber:
# `jarvis/execution/tools.py` is never handed a bus, so `action.dispatched`
# and every inline synchronous `action.result_observed` commit unpublished,
# and a subscriber would silently lose the acknowledge row. The Event Log is
# the only place all four rows are guaranteed to appear.
#
# Nothing here re-enters `decide()`: `_RUNTIME_TRIGGER_TYPES` is untouched and
# no model is ever called to produce a phrase (ADR-0008 D6, "a deep model is
# never called only to generate 我在查").

_COMMENTARY_ACTION_TYPES: Final[tuple[str, ...]] = (
    "action.dispatched",
    "action.running",
    "action.result_observed",
    "action.failed",
)

_COMMENTARY_NON_TERMINAL_TYPES: Final[frozenset[str]] = frozenset(
    {"action.dispatched", "action.running"},
)
"""The two rows that assert work is still in flight.

Their commentary is only true while the action has no terminal, so it is
checked against the `action:` EntityRegistry kind, which
`_STATUS_BOARD_TERMINAL_ACTION_TYPES` evicts. The other two rows *are*
terminals, and the registry has already dropped them by the time they are
observed, so no such check applies to them.
"""

_COMMENTARY_SHUTDOWN_BUDGET_S: Final[float] = 0.5
"""Whole-budget SQLite wait the teardown cancel may spend on the event loop."""

_COMMENTARY_ORIGIN_TRIGGER_TYPES: Final[frozenset[str]] = frozenset(
    {"surface.user_intent", "utterance.received"},
)
"""What makes a turn user-originated — the predicate L2 already enforces for
ordinary streaming (`jarvis/state/stream_emission.py`). A reconciliation or
supervisor-sweep turn writes no `turn.started` at all, so "no claim row" and
"not user-originated" are the same condition.
"""


@dataclasses.dataclass(frozen=True)
class _OpenCommentary:
    """One commentary ResponseRun that rendered but has not been heard yet.

    It stays open on purpose. ``response.completed`` is written when the
    phrase reaches the speaker (`surface.playback_started`); until then a
    newer lifecycle row for the same action can still cancel it as
    ``superseded``, which is what keeps stale progress out of the media lane.
    A commentary already playing is past that point and finishes.
    """

    action_id: str
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


def _commentary_action_turn_id(conn: sqlite3.Connection, action_event: Event) -> str | None:
    """Return the turn an action row belongs to, following its own chain.

    The row's correlation is read first, but it is not always filled: a
    canonical terminal can reach
    :func:`jarvis.state.lifecycle_terminal.terminalize_action` with an empty
    correlation, so ``action.result_observed`` commits without a
    ``turn_id``. The action's own ``action.dispatched`` row
    always carries one (``_action_correlation`` fills it from the
    ActionRequest), and joining through it is the same causal chain A5's
    ``ActionAdmissions`` exposes — durable and unambiguous, unlike guessing
    from the surrounding rows.
    """
    correlation = action_event.correlation or {}
    turn_id = correlation.get("turn_id")
    if isinstance(turn_id, str) and turn_id:
        return turn_id
    action_id = action_event.payload.get("action_id")
    if not isinstance(action_id, str) or not action_id:
        return None
    dispatched = next(
        (
            event
            for event in iter_events_of_types(conn, ("action.dispatched",))
            if event.payload.get("action_id") == action_id
        ),
        None,
    )
    if dispatched is None or dispatched.correlation is None:
        return None
    dispatched_turn = dispatched.correlation.get("turn_id")
    return dispatched_turn if isinstance(dispatched_turn, str) and dispatched_turn else None


def _commentary_turn_id(conn: sqlite3.Connection, action_event: Event) -> str | None:
    """Return the user-originated turn this action belongs to, or ``None``.

    Durable reads and no heuristics: the action names its turn, that turn's
    ``turn.started`` names the trigger it was claimed from, and the trigger's
    own type decides whether Allen asked for this.
    """
    turn_id = _commentary_action_turn_id(conn, action_event)
    if turn_id is None:
        return None
    started = next(
        (
            event
            for event in iter_events_of_types(conn, ("turn.started",))
            if event.payload.get("turn_id") == turn_id
        ),
        None,
    )
    if started is None or started.source_event_id is None:
        return None
    trigger = get_event(conn, started.source_event_id)
    if trigger is None or trigger.type not in _COMMENTARY_ORIGIN_TRIGGER_TYPES:
        return None
    return turn_id


_SELECT_COMMENTARY_PLAYBACK_SQL = (
    "SELECT 1 FROM events WHERE type = 'surface.playback_started' "
    "AND json_extract(payload_json, '$.response_id') = ? LIMIT 1"
)


def _commentary_reached_the_speaker(conn: sqlite3.Connection, response_id: str) -> bool:
    """Return whether ``surface.playback_started`` already named this run.

    Read from the log rather than from the observer's cursor position, and
    that difference is the whole point. The action row that supersedes a
    commentary is written *before* that commentary's playback begins, so it
    always has the lower row id: by cursor order alone the observer would
    reach the supersede decision while still believing the earlier phrase had
    never been heard, and cut off speech that was already coming out of the
    speaker (observed live: `surface.playback_interrupted` mid-phrase). D6's
    "a commentary already playing finishes" is only true if this is a durable
    read.
    """
    return conn.execute(_SELECT_COMMENTARY_PLAYBACK_SQL, (response_id,)).fetchone() is not None


_SELECT_COMMENTARY_IN_TURN_SQL = (
    "SELECT 1 FROM events WHERE type = 'response.started' "
    "AND json_extract(payload_json, '$.phase') = 'commentary' "
    "AND json_extract(payload_json, '$.turn_id') = ? LIMIT 1"
)


def _turn_already_spoke_commentary(conn: sqlite3.Connection, turn_id: str) -> bool:
    """Return whether this turn has already opened its one commentary run.

    ``start_response_run`` writes ``response.started`` first, before the
    Pre-emit Gate and the three surface rows, so it is the earliest durable
    mark of "this turn already spoke" and the only one that beats a second
    action row racing in on the next 10 ms poll. Reading the log rather than
    in-memory observer state is what makes the cap survive a restart, and
    what makes it hold when two actions of the same turn open on different
    worker threads.
    """
    return conn.execute(_SELECT_COMMENTARY_IN_TURN_SQL, (turn_id,)).fetchone() is not None


def _retire_superseded_commentary(
    runtime: JarvisRuntime,
    conn: sqlite3.Connection,
    previous: _OpenCommentary,
) -> None:
    """Close the phrase a newer lifecycle row replaces, the right way."""
    if _commentary_reached_the_speaker(conn, previous.run.response_id):
        _complete_commentary(runtime, previous)
        return
    _cancel_unheard_commentary(runtime, previous, reason="superseded")


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
        # A cancel already won the CAS — playback of a superseded phrase that
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


def _render_commentary(
    runtime: JarvisRuntime,
    conn: sqlite3.Connection,
    *,
    intent: PresentationIntent,
    action_event: Event,
    turn_id: str,
) -> _OpenCommentary:
    """Open one ``phase="commentary"`` run and deliver its single segment.

    ``turn_id`` is the action's own turn, so ``response_group_id`` derives to
    that turn's group and voice_media appends the phrase to the lane instead
    of interrupting whatever else that group is saying. The trigger is the
    action event itself: D6's "truth derives from the durable action event"
    is literally this run's ``source_event_id``.

    A request client is built only because ``response.started`` carries the
    preset snapshot. No request is ever issued, so the run has no cost
    disposition to record.
    """
    factory = runtime.llm_session_factory
    if factory is None:  # pragma: no cover - the flag graph pairs the two
        msg = "lifecycle commentary requires the ResponseRun session factory"
        raise RuntimeError(msg)
    response_id = new_response_id()
    snapshot = factory.snapshot(None)
    run = start_response_run(
        conn,
        turn_id=turn_id,
        trigger_event_uid=action_event.event_uid,
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
    run.link_action(intent.subject_ref)
    # Same condition the normal response path registers under
    # (`jarvis/runtime/__init__.py`): with the operator cancel seam live the
    # commentary run is reachable by id like any final run; without it no run
    # of any phase is registered and the private registry keeps today's
    # behaviour.
    runtime_registry = runtime.response_runs
    registry = (
        runtime_registry
        if runtime.response_flags.independent_response_cancel and runtime_registry is not None
        else ResponseRunRegistry()
    )
    # No subject is in scope for a fixed lifecycle phrase, so the Pre-emit
    # Gate short-circuits to its routine pass-through and hands back the
    # token `render_response` demands.
    plan = pre_emit_gate(commentary_speech_text(intent))
    _emit_pre_emit_verdict(conn, plan=plan, turn_id=turn_id)
    render_response(
        record_pre_emit_token(
            SurfaceState(last_gate_response_hash=None),
            plan.response_hash,
        ),
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
        action_id=intent.subject_ref,
        intent_type=intent.intent_type,
    )
    # Registered last, once nothing above can still raise: an entry the
    # watcher never receives is an entry no close path can ever unregister,
    # and in the runtime registry that would be a permanently open run.
    registry.register(run)
    return _OpenCommentary(
        action_id=intent.subject_ref,
        run=run,
        registry=registry,
        response_hash=plan.response_hash,
    )


def _open_commentary_in_worker_thread(  # noqa: PLR0911 - one early return per suppression rule
    runtime: JarvisRuntime,
    *,
    action_event: Event,
    previous: _OpenCommentary | None,
) -> _OpenCommentary | None:
    """Decide and deliver one action row's commentary on a worker thread.

    Opens its own connection for the same ``check_same_thread`` reason
    :func:`_drive_turn_in_worker_thread` does, and for a second one: the
    projection rebuild and the four appends must not stall the event loop
    the TTS watcher polls on.

    Returns ``None`` — writing nothing at all — when the row maps to no D6
    intent, when the turn is not user-originated, when the turn came from
    GPT-Live, when this turn already opened its one commentary, when a
    confirmation is still awaiting an answer, or when a non-terminal row's
    action already reached its terminal.
    """
    intent = commentary_intent_for(action_event)
    if intent is None:
        return None
    conn = open_runtime_event_log(runtime.runtime_paths.event_log)
    try:
        turn_id = _commentary_turn_id(conn, action_event)
        if turn_id is None:
            return None
        if _turn_intent_channel(conn, turn_id) == LIVE_PRINCIPAL:
            # ADR-0016 D8. Suppressed at the source, not by the TTS filter:
            # the run this would open hard-codes its own channel and attention
            # channel, so nothing downstream could tell it came from Live.
            # Live already narrates its own progress through a thinking
            # append, so synthesizing "我去查一下" is a duplicate and a
            # MiniMax request the local chain must not make.
            LOGGER.info(
                "commentary: turn_id=%s submitted on %s — no commentary run opened for %s.",
                turn_id,
                LIVE_PRINCIPAL,
                action_event.type,
            )
            return None
        if _turn_already_spoke_commentary(conn, turn_id):
            # One phrase per turn. Checked here, after the origin filter and
            # before every write, so that the three suppression paths below
            # never consume the turn's only slot: a turn whose first
            # qualifying row is silenced still speaks on a later row.
            return None
        projections = rebuild_projections(conn)
        slot = projections.pending_confirmations.slot
        if slot is not None and slot.is_live(int(time.time() * 1000)):
            # ADR-0014: new commentary never overwrites an unresolved
            # confirmation. The slot is globally unique and its
            # `action_snapshot` carries no `action_id`, so this is enforced
            # at the only granularity the fold supports.
            return None
        if (
            action_event.type in _COMMENTARY_NON_TERMINAL_TYPES
            and projections.action_admissions.get(intent.subject_ref) is None
        ):
            return None
        if previous is not None:
            _retire_superseded_commentary(runtime, conn, previous)
        return _render_commentary(
            runtime,
            conn,
            intent=intent,
            action_event=action_event,
            turn_id=turn_id,
        )
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()


async def _commentary_watcher(
    runtime: JarvisRuntime,
    *,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
) -> None:
    """Background task: speak one deterministic phrase per action lifecycle row.

    One cursor over the four D6 action types plus ``surface.playback_started``,
    anchored at the boot high-water mark exactly like ``_tts_watcher`` — a
    historical action from a previous session must never speak fake progress.
    ``surface.playback_started`` shares the cursor because it is the signal
    that a commentary is past the point of being superseded; a single
    monotonic cursor makes "played" and "a newer row arrived" strictly
    ordered rather than a race between two pollers.

    Audibility is decided by the per-turn cap in
    :func:`_open_commentary_in_worker_thread`, not here: a turn speaks at
    most one phrase, and the first row of that turn that actually opens one
    wins. The ``spoken`` set below is the cheaper guard in front of it — one
    entry per ``(action_id, event type)``, so a repeated row on a turn that
    never opened anything (a system turn, a live confirmation slot, a stale
    non-terminal) is dropped before the thread hop and the projection
    rebuild. It decides work, not what is heard.

    Per-event dispatch is wrapped in a catch-all for the same reason
    ``_tts_watcher``'s is: commentary is a courtesy, and a failure to produce
    it must never stall the watcher or the turn it is commenting on.
    """
    after_id = _latest_id(runtime.conn)
    open_by_action: dict[str, _OpenCommentary] = {}
    spoken: set[tuple[str, str]] = set()
    LOGGER.info("commentary_watcher started (after_id=%d)", after_id)
    try:
        while True:
            new_events = _fetch_events_after(
                runtime.conn,
                after_id=after_id,
                event_types=(*_COMMENTARY_ACTION_TYPES, "surface.playback_started"),
            )
            for row_id, ev in new_events:
                after_id = max(after_id, row_id)
                try:
                    if ev.type == "surface.playback_started":
                        await _commentary_heard(runtime, open_by_action, ev)
                        continue
                    action_id = _event_action_id(ev)
                    if action_id is None or (action_id, ev.type) in spoken:
                        continue
                    spoken.add((action_id, ev.type))
                    opened = await asyncio.to_thread(
                        _open_commentary_in_worker_thread,
                        runtime,
                        action_event=ev,
                        previous=open_by_action.get(action_id),
                    )
                    if opened is not None:
                        open_by_action[action_id] = opened
                except Exception as exc:  # noqa: BLE001 — commentary must not crash the watcher.
                    LOGGER.warning(
                        "commentary_watcher: dispatch raised on %s action_id=%s: %r",
                        ev.type,
                        _event_action_id(ev),
                        exc,
                    )
            await asyncio.sleep(poll_interval_s)
    except asyncio.CancelledError:
        shutdown_deadline = time.monotonic() + _COMMENTARY_SHUTDOWN_BUDGET_S
        for entry in open_by_action.values():
            with contextlib.suppress(Exception):
                _cancel_unheard_commentary(
                    runtime,
                    entry,
                    reason="shutdown",
                    deadline=shutdown_deadline,
                )
        LOGGER.info("commentary_watcher cancelled")
        raise


async def _commentary_heard(
    runtime: JarvisRuntime,
    open_by_action: dict[str, _OpenCommentary],
    event: Event,
) -> None:
    """Close the commentary run this playback belongs to, if it is ours."""
    response_id = event.payload.get("response_id")
    entry = next(
        (item for item in open_by_action.values() if item.run.response_id == response_id),
        None,
    )
    if entry is None:
        return
    del open_by_action[entry.action_id]
    await asyncio.to_thread(_complete_commentary, runtime, entry)


def _build_voice_pipeline(
    runtime: JarvisRuntime,
    *,
    broadcaster: InherentBroadcaster,
    sensevoice_dir: Path,
) -> voice_pipeline.VoicePipeline:
    """Construct the L5 :class:`VoicePipeline` with a fresh-conn factory.

    ADR-0005 §4.2 / §5.1 — the pipeline runs on the wake or PTT worker
    thread, never the event loop, so the conn factory MUST open a new
    SQLite connection per call (``check_same_thread`` invariant). The
    L3 normalizer ships empty-population by default; config-driven
    aliases / corrections land in a follow-up.
    """
    recognizer = voice_asr.SenseVoiceRecognizer(model_dir=sensevoice_dir)
    normalizer = voice_asr.AsrNormalizer(
        corrections=[],
        aliases={},
        fuzzy_enabled=False,
    )
    db_path = runtime.runtime_paths.event_log
    memory = runtime.memory
    artifacts_dir = memory.audio_dir if memory is not None and memory.retain_audio else None
    return voice_pipeline.VoicePipeline(
        conn_factory=lambda: open_event_log(db_path),
        recognizer=recognizer,
        normalizer=normalizer,
        broadcaster=broadcaster,
        artifacts_dir=artifacts_dir,
    )


def _build_tts_pipeline(  # noqa: C901 - rollout/degradation capability boundary
    runtime: JarvisRuntime,
    broadcaster: InherentBroadcaster,
    *,
    ducker: voice_ducking.SystemAudioDucker | None = None,
    voice: _VoiceKnobs | None = None,
) -> voice_tts.TTSPipeline | voice_media.StreamingTTSPipeline | None:
    """Build the TTS subsystem when ``MINIMAX_API_KEY`` is present.

    ADR-0005 §5.3 — the env var is the sole credential source for the
    MiniMax WebSocket. Without it we skip the entire TTS pipeline
    (instead of falling through to ``macos_say_fallback`` only): the
    fallback is a per-call escape hatch from inside
    :class:`TTSPipeline`, not a standalone path, so wiring it on its
    own would lie about what the daemon can actually do.

    The optional ``ducker`` is the same :class:`SystemAudioDucker`
    instance shared with the WakeListener. TTS registers an output lease;
    wake capture refuses to mute while provider I/O or playback owns it.
    """
    knobs = _VoiceKnobs() if voice is None else voice
    api_key = os.environ.get("MINIMAX_API_KEY")
    if not api_key:
        LOGGER.warning(
            "MINIMAX_API_KEY unset; skipping TTS subsystem (text path only).",
        )
        return None
    realtime_raw = runtime.config.get("realtime")
    realtime = realtime_raw if isinstance(realtime_raw, Mapping) else {}
    # MiniMax `voice_setting.vol`.  Absent or null keeps the MiniMaxWSClient
    # signature default, the single place the calibrated value lives.
    # Deliberately unvalidated like `output_device` below, but with a wider
    # blast radius: this is read before the builder's own try, so a non-numeric
    # value raises out of `_build_tts_pipeline` and the caller drops voice input
    # with it, where a bad `output_device` only degrades TTS to text-only.
    tts_volume = realtime.get("tts_volume")
    volume_kwargs: dict[str, Any] = {} if tts_volume is None else {"volume": tts_volume}
    # Passed straight through to sd.OutputStream, which maps a name to a device
    # index itself; an unresolvable value raises there and `start()` fails closed.
    # Deliberately unvalidated: a type guard here would turn a mistyped key into
    # a silent fall back to the system default, out of the owner's speakers.
    output_device = realtime.get("output_device")
    streaming_raw = realtime.get("streaming_output")
    streaming = streaming_raw if isinstance(streaming_raw, Mapping) else {}
    streaming_requested = realtime.get("enabled") is True and streaming.get("enabled") is True
    try:
        media_config = voice_media.streaming_media_config_from_mapping(streaming)
    except ValueError as exc:
        media_config = None
        if streaming_requested:
            LOGGER.warning(
                "realtime.streaming_output config invalid (%s); downgraded to legacy TTS.",
                exc,
            )
    # THE output rate, for the provider's resampler and both players.
    # ``realtime.streaming_output.canonical_sample_rate_hz`` is its only source:
    # a second constant here could only ever agree with it or silently disable
    # streaming. The default (48 kHz) is the macOS built-in device rate, so
    # CoreAudio is not forced into a hardware-rate switch on every play — which
    # was producing audible pops for any other app sharing the speaker.
    streaming_defaults = voice_media.StreamingMediaConfig()
    output_sample_rate_hz = (
        streaming_defaults.canonical_sample_rate_hz
        if media_config is None
        else media_config.canonical_sample_rate_hz
    )
    streaming_ring_seconds = (
        streaming_defaults.ring_seconds
        if media_config is None
        else media_config.ring_seconds
    )

    def _new_provider() -> voice_tts.MiniMaxWSClient:
        return voice_tts.MiniMaxWSClient(
            api_key=api_key,
            voice=knobs.tts_voice,
            model=knobs.tts_model,
            primary_endpoint=knobs.tts_primary_endpoint,
            fallback_endpoint=knobs.tts_fallback_endpoint,
            sample_rate_in=knobs.tts_sample_rate_in_hz,
            sample_rate_out=output_sample_rate_hz,
            connect_timeout_s=knobs.tts_connect_timeout_s,
            task_start_timeout_s=knobs.tts_task_start_timeout_s,
            first_chunk_timeout_s=knobs.tts_first_chunk_timeout_s,
            between_chunk_timeout_s=knobs.tts_between_chunk_timeout_s,
            total_timeout_s=knobs.tts_total_timeout_s,
            session_close_timeout_s=knobs.tts_session_close_timeout_s,
            **volume_kwargs,
        )

    provider = _new_provider()
    streaming_capable = (
        runtime.wave1_features.transactional_event_append
        and runtime.wave1_features.lifecycle_terminal_cas
        and provider.streaming_candidate_count > 0
        and media_config is not None
    )
    if streaming_requested and streaming_capable:
        player = voice_tts.AudioStreamPlayer(
            sample_rate_hz=output_sample_rate_hz,
            ring_seconds=streaming_ring_seconds,
            lazy_open=True,
            generation_safe=True,
            device=output_device,
        )
        try:
            return voice_media.StreamingTTSPipeline(
                provider=provider,
                player=player,
                conn_factory=lambda: open_event_log(runtime.runtime_paths.event_log),
                boot_high_water_id=_latest_id(runtime.conn),
                config=media_config,
                broadcaster=broadcaster,
                ducker=ducker,
                foreground_decision_callable=make_foreground_decision_callable(),
            )
        except voice_media.StreamingMediaStartupError as exc:
            if not exc.legacy_fallback_safe:
                LOGGER.warning(
                    "realtime.streaming_output startup left output ownership "
                    "uncertain in %s; this boot is downgraded to text-only.",
                    exc.phase,
                )
                return None
            LOGGER.warning(
                "realtime.streaming_output startup failed closed in %s (%r); "
                "downgraded using an independent legacy provider.",
                exc.phase,
                exc,
            )
            provider = _new_provider()
        except Exception as exc:  # noqa: BLE001 - rollout must fail safe
            LOGGER.warning(
                "realtime.streaming_output startup failed (%r); downgraded using "
                "an independent legacy provider.",
                exc,
            )
            provider = _new_provider()
    if streaming_requested and media_config is not None and not streaming_capable:
        LOGGER.warning(
            "realtime.streaming_output capability/config validation failed; "
            "downgraded to legacy TTS.",
        )
    # lazy_open=False so the PortAudio OutputStream is up before the first
    # MiniMax chunk lands; otherwise `write()` would fill the ring and
    # never drain, leaving `is_speaking()` permanently True and starving
    # the wake listener.
    try:
        player = voice_tts.AudioStreamPlayer(
            sample_rate_hz=output_sample_rate_hz,
            ring_seconds=knobs.tts_ring_seconds,
            lazy_open=False,
            device=output_device,
        )
        return voice_tts.TTSPipeline(
            provider=provider,
            player=player,
            fallback=voice_tts.macos_say_fallback,
            broadcaster=broadcaster,
            ducker=ducker,
        )
    except Exception as exc:  # noqa: BLE001 - final voice degradation boundary
        LOGGER.warning("legacy TTS startup failed (%r); downgraded to text-only.", exc)
        return None


# Shift+Return on the card records a memo instead of asking a question: the
# ASR transcript gets the `/note ` prefix so the Tier 0 `note_capture` row
# (config/tier0_patterns.yaml) routes it straight to `create_memo`.
_TRANSCRIPT_PREFIX_BY_CHANNEL: Final[Mapping[str, str]] = {"inherent_note": "/note "}


def _build_voice_pipeline_callable(
    pipeline: voice_pipeline.VoicePipeline,
) -> Callable[[bytes, str, str, str], Event]:
    """Adapt :meth:`VoicePipeline.run_turn` to the InherentDeps callable shape.

    ``InherentDeps.voice_pipeline_callable`` takes positional
    ``(audio_bytes, turn_id, channel, language)`` and returns the
    emitted ``utterance.received`` :class:`Event`; the pipeline
    itself is keyword-only, so this thin closure does the rewrite.

    The closure forces ``broadcast=False`` — this callable is the PTT
    path (``/inherent/asr-submit``), and per ADR-0005 §6 the inherent-
    swift card drives its state from the HTTP response body, not from
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
            transcript_prefix=_TRANSCRIPT_PREFIX_BY_CHANNEL.get(channel, ""),
        )

    return _call


def _open_wake_input_stream() -> Any:  # noqa: ANN401 — sounddevice stream is untyped third-party API
    """Open the persistent 16 kHz / 1280-sample PortAudio input stream.

    Module-level so tests can :func:`unittest.mock.patch.object` it
    without needing PortAudio. ``sounddevice`` is lazy-imported so this
    module remains importable in environments without the wheel.

    The returned stream is the WakeListener's frame source — every
    frame_factory call does ``stream.read(_WAKE_FRAME_SAMPLES)[0]`` to
    pull exactly one 80 ms PCM16 frame. Legacy parity:
    ``core/inherent_wake_listener.py`` opens the same shape.
    """
    import sounddevice as sd  # noqa: PLC0415

    stream = sd.RawInputStream(
        samplerate=_WAKE_SAMPLE_RATE_HZ,
        channels=1,
        dtype="int16",
        blocksize=_WAKE_FRAME_SAMPLES,
    )
    stream.start()
    return stream


def _spawn_wake_listener(  # noqa: PLR0913 - composition boundary dependencies
    *,
    pipeline: voice_pipeline.VoicePipeline,
    broadcaster: InherentBroadcaster,
    silero_path: Path,
    tts: voice_tts.TTSPipeline | voice_media.StreamingTTSPipeline | None,
    ducker: voice_ducking.SystemAudioDucker | None = None,
    voice: _VoiceKnobs | None = None,
    mic_muted: Callable[[], bool] | None = None,
) -> tuple[voice_wake.WakeListener, Any | None] | None:
    """Construct + start a :class:`WakeListener` daemon thread.

    ADR-0005 §5.1 — the listener owns its own SileroVad (record mode)
    and a partial-bound :func:`voice_audio.capture_utterance`; both
    are constructed here so the L5 modules stay free of L6 wiring.

    ``voice`` carries the resolved ``realtime:`` knobs. It is a defaulted
    parameter rather than a ``JarvisRuntime``: this function must stay unable
    to read ``realtime.single_audio_ingress``, since a key placed there would
    then be honoured by one input owner and silently ignored by the other.

    Also opens the 16 kHz / 80 ms PortAudio input stream that backs the
    listener's ``frame_factory``. Without this stream the listener
    reads silent zero frames and openwakeword's probability never
    crosses threshold — wake silently never fires in production.

    Returns ``(listener, stream)`` on success so the daemon shutdown
    path can both :meth:`WakeListener.request_stop` AND
    :meth:`stream.close` (in that order). If the PortAudio stream
    fails to open (no audio device in CI / headless test env), logs
    ERROR and returns ``None`` — text path stays healthy. For
    test-only callers that patch ``_open_wake_input_stream`` to return
    a mock stream, this still works because the mock answers ``read``.
    """
    knobs = _VoiceKnobs() if voice is None else voice
    try:
        stream = _open_wake_input_stream()
    except Exception:
        LOGGER.exception(
            "wake: failed to open PortAudio input stream; skipping wake listener.",
        )
        return None

    def _read_wake_frame() -> bytes:
        """Pull one 80 ms PCM16 frame from the persistent wake stream."""
        data, _overflow = stream.read(_WAKE_FRAME_SAMPLES)
        return bytes(data)

    engine: voice_wake.WakeEngine | None = None
    listener: voice_wake.WakeListener | None = None
    try:
        engine = voice_wake.WakeEngine(model_name="hey_jarvis_v0.1")
        # Without start(), predict() silently returns no detection.
        engine.start()
        silero_vad = voice_audio.SileroVad(
            mode="record",
            model_path=silero_path,
            profiles=knobs.vad_profiles,
        )
        silero_vad.prepare_utterance()
        capture_callable = functools.partial(
            voice_audio.capture_utterance,
            vad=silero_vad,
            max_duration_s=_DEFAULT_CAPTURE_MAX_DURATION_S,
            min_voiced_s=_DEFAULT_CAPTURE_MIN_VOICED_S,
        )
        listener = voice_wake.WakeListener(
            engine=engine,
            pipeline=pipeline,
            broadcaster=broadcaster,
            capture_callable=capture_callable,
            threshold=knobs.wake_threshold,
            is_speaking_callable=(tts.is_speaking if tts is not None else None),
            frame_factory=_read_wake_frame,
            ducker=ducker,
            mic_muted=mic_muted,
        )
        listener.start()
    except Exception:
        LOGGER.exception(
            "wake: legacy listener construction/start failed; preserving PTT.",
        )
        if listener is not None:
            _shutdown_wake(listener, stream, join_timeout_s=knobs.wake_join_timeout_s)
        else:
            try:
                stream.stop()
            except Exception:  # noqa: BLE001 - continue exact close attempt
                LOGGER.debug("wake: stream stop during startup cleanup failed", exc_info=True)
            try:
                stream.close()
            except Exception:  # noqa: BLE001 - local startup failure stays isolated
                LOGGER.debug("wake: stream close during startup cleanup failed", exc_info=True)
        if engine is not None:
            try:
                engine.close()
            except Exception:  # noqa: BLE001 - local startup failure stays isolated
                LOGGER.debug("wake: engine startup cleanup failed", exc_info=True)
        return None
    return listener, stream


@dataclasses.dataclass(frozen=True)
class _SingleIngressActivation:
    """Validated Wave-3 activation decision before any input device open."""

    requested: bool
    capable: bool
    reason: str
    ingress_config: voice_audio.AudioIngressConfig | None = None
    session_config: voice_session.RealtimeInputSessionConfig | None = None


@dataclasses.dataclass(frozen=True)
class _VoiceInputOwners:
    """Composition-root input branch selected for this boot."""

    duplex_session: voice_session.DuplexVoiceSession | None
    wake_listener: voice_wake.WakeListener | None
    wake_stream: object | None
    single_ingress_attempted: bool
    # Why this branch was taken. The failing paths already record it as an
    # ``audio_input_activation_downgraded`` trace; carrying it out is what lets
    # the startup record name the successful choice too, which the wake log
    # line cannot: both owners construct an identical ``WakeEngine``.
    reason: str = "unknown"


@dataclasses.dataclass(frozen=True)
class _VoicePowerTransition:
    """Auditable ordered input/output transition at the power boundary."""

    input_result: object | None
    output_result: voice_media.MediaPowerTransitionResult | None
    deadline_monotonic: float
    elapsed_s: float
    input_skipped_reason: str | None = None


class _VoicePowerCoordinator:
    """Serialize power lifecycle without exposing a speech-cancel seam."""

    _TOTAL_TRANSITION_BOUND_S = 2.75

    def __init__(
        self,
        *,
        session: voice_session.DuplexVoiceSession,
        media: voice_media.StreamingTTSPipeline,
    ) -> None:
        self._session = session
        self._media = media
        self._lock = threading.Lock()
        self._intent_lock = threading.Lock()
        self._shutdown = threading.Event()
        self._pending_wake_generation = 0
        self._pending_wake_thread: threading.Thread | None = None
        self._pending_wake_done = threading.Event()
        self._pending_wake_done.set()
        self._pending_wake_state: Literal["idle", "in_flight", "recovered"] = "idle"

    @staticmethod
    def _input_resume_succeeded(result: object | None) -> bool:
        """Interpret the typed production port and explicit legacy test adapter."""
        if isinstance(result, voice_backend.BackendStartResult):
            return result.started
        return result is not None

    @staticmethod
    def _input_resume_reason(result: object | None) -> str:
        if isinstance(result, voice_backend.BackendStartResult):
            return result.status.value
        return "none" if result is None else "legacy_adapter_success"

    def before_sleep(self) -> _VoicePowerTransition:
        """Close input first, then terminalize and stop output."""
        started = time.monotonic()
        deadline = started + self._TOTAL_TRANSITION_BOUND_S
        # Revoke older wake work before waiting for the operation lock. A
        # continuation may be inside a foreign media wait, but it can no
        # longer restore input/output after this linearization point.
        with self._intent_lock:
            self._pending_wake_generation += 1
        if not self._lock.acquire(
            timeout=max(0.0, deadline - time.monotonic()),
        ):
            output_result = voice_media.MediaPowerTransitionResult(
                "uncertain",
                0,
                "coordinator_lock_timeout",
                deadline_exhausted=True,
            )
            return _VoicePowerTransition(
                None,
                output_result,
                deadline,
                time.monotonic() - started,
                "coordinator_lock_timeout",
            )
        try:
            self._pending_wake_state = "idle"
            input_result = self._session.ingress.stop_for_sleep(deadline=deadline)
            output_result = self._media.suspend_for_sleep(deadline=deadline)
            elapsed_s = time.monotonic() - started
            record_realtime_trace(
                "voice_power_before_sleep_completed",
                elapsed_s=elapsed_s,
                total_bound_s=self._TOTAL_TRANSITION_BOUND_S,
                deadline_exhausted=time.monotonic() >= deadline,
                output_status=output_result.status,
                output_reason=output_result.reason,
            )
            return _VoicePowerTransition(
                input_result,
                output_result,
                deadline,
                elapsed_s,
            )
        finally:
            self._lock.release()

    def on_wake(  # noqa: C901, PLR0912, PLR0915 - ordered cross-device CAS
        self,
    ) -> _VoicePowerTransition:
        """Create fresh output ownership before re-enabling input decisions."""
        started = time.monotonic()
        deadline = started + self._TOTAL_TRANSITION_BOUND_S
        if not self._lock.acquire(
            timeout=max(0.0, deadline - time.monotonic()),
        ):
            reason = "coordinator_lock_timeout"
            output_result = voice_media.MediaPowerTransitionResult(
                "uncertain",
                0,
                reason,
                deadline_exhausted=True,
            )
            return _VoicePowerTransition(
                None,
                output_result,
                deadline,
                time.monotonic() - started,
                reason,
            )
        lock_owned = True
        try:
            pending = self._pending_wake_thread
            if self._pending_wake_state == "in_flight" and not (
                pending is not None and pending.is_alive()
            ):
                self._pending_wake_state = "idle"
            if self._pending_wake_state in {"in_flight", "recovered"}:
                pending_alive = pending is not None and pending.is_alive()
                output_result = voice_media.MediaPowerTransitionResult(
                    (
                        "resumed"
                        if self._pending_wake_state == "recovered"
                        else "uncertain"
                    ),
                    self._pending_wake_generation,
                    (
                        "pending_wake_already_resumed"
                        if self._pending_wake_state == "recovered"
                        else "pending_wake_continuation_in_flight"
                    ),
                    helper_thread_alive=pending_alive,
                )
                return _VoicePowerTransition(
                    None,
                    output_result,
                    deadline,
                    time.monotonic() - started,
                    output_result.reason,
                )
            with self._intent_lock:
                wake_generation = self._pending_wake_generation
            self._lock.release()
            lock_owned = False
            output_result = self._media.resume_after_wake(deadline=deadline)
            if not self._lock.acquire(
                timeout=max(0.0, deadline - time.monotonic()),
            ):
                self._media.abort_wake_start(
                    attempt_id=output_result.attempt_id,
                    reason="wake_operation_lock_timeout",
                    deadline=deadline,
                )
                return _VoicePowerTransition(
                    None,
                    output_result,
                    deadline,
                    time.monotonic() - started,
                    "wake_operation_lock_timeout",
                )
            lock_owned = True
            skipped_reason: str | None = None
            with self._intent_lock:
                wake_current = (
                    wake_generation == self._pending_wake_generation
                    and not self._shutdown.is_set()
                )
            if output_result.status == "resumed" and output_result.succeeded and wake_current:
                input_result = self._session.ingress.resume_after_wake(deadline=deadline)
                with self._intent_lock:
                    wake_current = (
                        wake_generation == self._pending_wake_generation
                        and not self._shutdown.is_set()
                    )
                    admitted = (
                        wake_current
                        and self._input_resume_succeeded(input_result)
                        and self._media.admit_wake_start(
                            attempt_id=output_result.attempt_id,
                        )
                    )
            else:
                input_result = None
                admitted = False
            if output_result.status == "resumed" and output_result.succeeded:
                if not admitted:
                    if not wake_current:
                        if input_result is not None:
                            self._session.ingress.stop_for_sleep(deadline=deadline)
                        self._media.abort_wake_start(
                            attempt_id=output_result.attempt_id,
                            reason="wake_generation_revoked",
                            deadline=deadline,
                        )
                        skipped_reason = "wake_generation_revoked"
                    else:
                        input_reason = self._input_resume_reason(input_result)
                        if input_result is not None:
                            self._session.ingress.stop_for_sleep(deadline=deadline)
                        aborted = self._media.abort_wake_start(
                            attempt_id=output_result.attempt_id,
                            reason=f"input_resume_{input_reason}",
                            deadline=deadline,
                        )
                        skipped_reason = (
                            f"input_resume_{input_reason};output_{aborted.status}:"
                            f"{aborted.reason}"
                        )
            else:
                input_result = None
                if wake_current:
                    skipped_reason = f"output_{output_result.status}:{output_result.reason}"
                    self._session.ingress.report_output_unavailable(reason=skipped_reason)
                    if output_result.helper_thread_alive and not self._shutdown.is_set():
                        self._schedule_pending_wake_locked(wake_generation)
                else:
                    self._media.abort_wake_start(
                        attempt_id=output_result.attempt_id,
                        reason="wake_generation_revoked",
                        deadline=deadline,
                    )
                    skipped_reason = "wake_generation_revoked"
            elapsed_s = time.monotonic() - started
            record_realtime_trace(
                "voice_power_wake_completed",
                elapsed_s=elapsed_s,
                total_bound_s=self._TOTAL_TRANSITION_BOUND_S,
                deadline_exhausted=time.monotonic() >= deadline,
                output_status=output_result.status,
                output_reason=output_result.reason,
                input_skipped_reason=skipped_reason,
            )
            return _VoicePowerTransition(
                input_result,
                output_result,
                deadline,
                elapsed_s,
                skipped_reason,
            )
        finally:
            if lock_owned:
                self._lock.release()

    def _schedule_pending_wake_locked(  # noqa: C901, PLR0915 - one exact retry owner
        self,
        generation: int,
    ) -> None:
        """Keep one deduplicated wake continuation after an exact stop debt."""
        pending = self._pending_wake_thread
        if pending is not None and pending.is_alive():
            return
        self._pending_wake_state = "in_flight"
        done = threading.Event()
        self._pending_wake_done = done

        def _continue_wake() -> None:  # noqa: C901, PLR0912, PLR0915 - exact debt loop
            output_result: voice_media.MediaPowerTransitionResult | None = None
            input_result: object | None = None
            reason = ""
            deadline = time.monotonic()
            try:
                while not self._shutdown.is_set():
                    deadline = time.monotonic() + self._TOTAL_TRANSITION_BOUND_S
                    with self._intent_lock:
                        generation_current = (
                            generation == self._pending_wake_generation
                            and not self._shutdown.is_set()
                        )
                    if not generation_current:
                        reason = "pending_wake_revoked"
                        return
                    # Never hold the coordinator operation lock across the
                    # foreign media wait. A newer sleep can acquire it and
                    # complete while this exact debt remains joinable here.
                    output_result = self._media.resume_after_wake(deadline=deadline)
                    acquired = self._lock.acquire(
                        timeout=max(0.0, deadline - time.monotonic()),
                    )
                    if not acquired:
                        reason = "pending_wake_coordinator_lock_timeout"
                        self._media.abort_wake_start(
                            attempt_id=output_result.attempt_id,
                            reason=reason,
                            deadline=deadline,
                        )
                        continue
                    retry_exact_debt = False
                    try:
                        with self._intent_lock:
                            generation_current = (
                                generation == self._pending_wake_generation
                                and not self._shutdown.is_set()
                            )
                        if (
                            output_result.status == "resumed"
                            and output_result.succeeded
                            and generation_current
                        ):
                            input_result = self._session.ingress.resume_after_wake(
                                deadline=deadline,
                            )
                            with self._intent_lock:
                                generation_current = (
                                    generation == self._pending_wake_generation
                                    and not self._shutdown.is_set()
                                )
                                admitted = (
                                    generation_current
                                    and self._input_resume_succeeded(input_result)
                                    and self._media.admit_wake_start(
                                        attempt_id=output_result.attempt_id,
                                    )
                                )
                        else:
                            input_result = None
                            admitted = False
                        if output_result.status == "resumed" and output_result.succeeded:
                            if admitted:
                                self._pending_wake_state = "recovered"
                                reason = "pending_wake_resumed"
                                return
                            if not generation_current:
                                if input_result is not None:
                                    self._session.ingress.stop_for_sleep(deadline=deadline)
                                self._media.abort_wake_start(
                                    attempt_id=output_result.attempt_id,
                                    reason="pending_wake_generation_revoked",
                                    deadline=deadline,
                                )
                                reason = "pending_wake_generation_revoked"
                            else:
                                input_reason = self._input_resume_reason(input_result)
                                if input_result is not None:
                                    self._session.ingress.stop_for_sleep(deadline=deadline)
                                aborted = self._media.abort_wake_start(
                                    attempt_id=output_result.attempt_id,
                                    reason=f"input_resume_{input_reason}",
                                    deadline=deadline,
                                )
                                reason = (
                                    f"pending_input_resume_{input_reason};"
                                    f"output_{aborted.status}:{aborted.reason}"
                                )
                            return
                        if not generation_current:
                            self._media.abort_wake_start(
                                attempt_id=output_result.attempt_id,
                                reason="pending_wake_generation_revoked",
                                deadline=deadline,
                            )
                            reason = "pending_wake_generation_revoked"
                            return
                        reason = f"output_{output_result.status}:{output_result.reason}"
                        if not self._shutdown.is_set():
                            self._session.ingress.report_output_unavailable(reason=reason)
                        retry_exact_debt = output_result.helper_thread_alive
                    finally:
                        self._lock.release()
                    record_realtime_trace(
                        "voice_power_pending_wake_retry",
                        generation=generation,
                        output_status=output_result.status,
                        reason=reason,
                        retry_exact_debt=retry_exact_debt,
                        deadline_exhausted=time.monotonic() >= deadline,
                    )
                    if not retry_exact_debt:
                        return
                reason = "pending_wake_revoked"
            finally:
                with self._lock:
                    if (
                        generation == self._pending_wake_generation
                        and self._pending_wake_state != "recovered"
                    ):
                        self._pending_wake_state = "idle"
                record_realtime_trace(
                    "voice_power_pending_wake_completed",
                    generation=generation,
                    output_status=(
                        output_result.status if output_result is not None else "not_attempted"
                    ),
                    input_resumed=input_result is not None,
                    reason=reason,
                    deadline_exhausted=time.monotonic() >= deadline,
                )
                done.set()

        thread = threading.Thread(
            target=_continue_wake,
            name=f"jarvis-power-pending-wake-g{generation}",
            daemon=True,
        )
        self._pending_wake_thread = thread
        try:
            thread.start()
        except RuntimeError:
            done.set()
            LOGGER.exception("failed to start pending wake continuation")

    def close(self, *, timeout_s: float | None = None) -> bool:
        """Revoke any pending wake before input/output owner shutdown."""
        self._shutdown.set()
        with self._intent_lock:
            self._pending_wake_generation += 1
        self._media.revoke_wake_starts_for_shutdown()
        timeout = self._TOTAL_TRANSITION_BOUND_S if timeout_s is None else max(0.0, timeout_s)
        deadline = time.monotonic() + timeout
        if self._lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            try:
                self._pending_wake_state = "idle"
            finally:
                self._lock.release()
        pending = self._pending_wake_thread
        if pending is not None:
            pending.join(timeout=max(0.0, deadline - time.monotonic()))
        return pending is None or not pending.is_alive()


def _single_ingress_activation(  # noqa: PLR0911 - each fail-closed prerequisite has a named result
    runtime: JarvisRuntime,
    *,
    tts: voice_tts.TTSPipeline | voice_media.StreamingTTSPipeline | None,
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
    if not (
        runtime.wave1_features.transactional_event_append
        and runtime.wave1_features.lifecycle_terminal_cas
    ):
        return _SingleIngressActivation(
            requested=True,
            capable=False,
            reason="wave1_capability_missing",
        )
    streaming_raw = realtime.get("streaming_output")
    streaming = streaming_raw if isinstance(streaming_raw, Mapping) else {}
    if streaming.get("enabled") is not True or not isinstance(
        tts,
        voice_media.StreamingTTSPipeline,
    ):
        return _SingleIngressActivation(
            requested=True,
            capable=False,
            reason="wave2_streaming_output_missing",
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
    runtime: JarvisRuntime,
    pipeline: voice_pipeline.VoicePipeline,
    broadcaster: InherentBroadcaster,
    silero_path: Path,
    tts: voice_tts.TTSPipeline | voice_media.StreamingTTSPipeline | None,
    voice: _VoiceKnobs | None = None,
    mic_muted: Callable[[], bool] | None = None,
) -> tuple[voice_session.DuplexVoiceSession | None, bool]:
    """Start Wave 3 or return whether a device-open attempt was made.

    ``attempted=True`` forbids legacy wake fallback for this boot even when
    startup failed: a timed-out foreign PortAudio open may still own the
    default microphone.  ``attempted=False`` means no input owner was touched,
    so a failed prerequisite/config validation may explicitly use legacy wake.
    """
    knobs = _VoiceKnobs() if voice is None else voice
    activation = _single_ingress_activation(runtime, tts=tts)
    if not activation.requested:
        return None, False
    if not activation.capable:
        LOGGER.warning(
            "realtime.single_audio_ingress validation failed (%s); "
            "downgraded to legacy wake before opening an input owner.",
            activation.reason,
        )
        record_realtime_trace(
            "audio_input_activation_downgraded",
            reason=activation.reason,
            fallback="legacy_wake",
            input_owner_attempted=False,
        )
        return None, False
    ingress_config = activation.ingress_config
    session_config = activation.session_config
    if ingress_config is None or session_config is None:
        msg = "validated single ingress activation lacks parsed config"
        raise RuntimeError(msg)
    engine = voice_wake.WakeEngine(model_name="hey_jarvis_v0.1")
    try:
        # Model construction/download happens before PortAudio owns the mic.
        engine.start()
    except Exception:
        LOGGER.exception(
            "realtime.single_audio_ingress wake model failed before device open; "
            "downgraded to legacy wake.",
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
            "device open; downgraded to legacy wake/PTT.",
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
                channels=1,
                callback_frame_samples=ingress_config.canonical_frame_samples,
            ),
            open_timeout_s=ingress_config.backend_open_timeout_s,
            close_timeout_s=ingress_config.backend_close_timeout_s,
        )
        ingress = voice_audio.AudioIngress(
            backend=backend,
            config=ingress_config,
            capability_sink=_capability_changed,
        )
        vad = voice_audio.SileroVad(
            mode="record",
            model_path=silero_path,
            profiles=knobs.vad_profiles,
        )
        session = voice_session.DuplexVoiceSession(
            ingress=ingress,
            wake_engine=engine,
            vad=vad,
            pipeline=pipeline,
            broadcaster=broadcaster,
            output_active=(tts.is_output_active if tts is not None else None),
            wake_threshold=knobs.wake_threshold,
            config=session_config,
            barge_in_interrupt=make_barge_in_interrupt_callable(runtime),
            mic_muted=mic_muted,
        )
    except Exception:
        LOGGER.exception(
            "realtime.single_audio_ingress construction failed before device open; "
            "downgraded to legacy wake.",
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
            "open; downgraded to legacy wake/PTT.",
        )
        with contextlib.suppress(Exception):
            session.close()
        record_realtime_trace(
            "audio_input_activation_downgraded",
            reason="session_prepare_failed_before_device",
            fallback="legacy_wake",
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
            "preserving text/PTT-upload only and refusing legacy mic fallback.",
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
    tts: voice_tts.TTSPipeline | voice_media.StreamingTTSPipeline | None,
    duplex_session: voice_session.DuplexVoiceSession | None,
    attempted: bool,
    wake_listener: voice_wake.WakeListener | None,
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
    if wake_listener is not None:
        # Legacy wake owns the mic because single ingress declined for `reason`.
        return reason
    return f"{reason}:legacy_wake_unavailable"


def _spawn_voice_input_owners(  # noqa: PLR0913 - composition boundary dependencies
    *,
    runtime: JarvisRuntime,
    pipeline: voice_pipeline.VoicePipeline,
    broadcaster: InherentBroadcaster,
    silero_path: Path,
    tts: voice_tts.TTSPipeline | voice_media.StreamingTTSPipeline | None,
    ducker: voice_ducking.SystemAudioDucker,
    voice: _VoiceKnobs | None = None,
    mic_muted: Callable[[], bool] | None = None,
) -> _VoiceInputOwners:
    """Select Wave 3 or legacy wake without ever opening both input owners."""
    knobs = _VoiceKnobs() if voice is None else voice
    duplex_session, attempted = _spawn_single_ingress_session(
        runtime=runtime,
        pipeline=pipeline,
        broadcaster=broadcaster,
        silero_path=silero_path,
        tts=tts,
        voice=knobs,
        mic_muted=mic_muted,
    )
    wake_listener: voice_wake.WakeListener | None = None
    wake_stream: object | None = None
    if duplex_session is None and not attempted:
        legacy = _spawn_wake_listener(
            pipeline=pipeline,
            broadcaster=broadcaster,
            silero_path=silero_path,
            tts=tts,
            ducker=ducker,
            voice=knobs,
            mic_muted=mic_muted,
        )
        if legacy is not None:
            wake_listener, wake_stream = legacy
    return _VoiceInputOwners(
        duplex_session=duplex_session,
        wake_listener=wake_listener,
        wake_stream=wake_stream,
        single_ingress_attempted=attempted,
        reason=_input_owner_reason(
            runtime,
            tts=tts,
            duplex_session=duplex_session,
            attempted=attempted,
            wake_listener=wake_listener,
        ),
    )


# --- ADR-0009 D4: supervisor sweep control plane ---------------------------

# Terminal-failure event types an orphan closure lands on. Both route
# into L3's ``_handle_action_terminal_failure`` branch — the one that
# already produces the Limitation claim + limitation utterance, which is
# why the sweep stays an emitter rather than a second brain.
#
# The happy-path re-entry (``action.result_observed``) is deliberately
# absent: it is only ever written from inside a live driver mid-turn, so a system turn on
# one would re-drive work somebody else is already driving, and their L3
# branches expect mid-turn scratch state a fresh turn does not have.
_SYSTEM_TRIGGER_TYPES: tuple[str, ...] = (
    "action.timeout_assumed",
    "action.failed",
)

# Fallbacks for a runtime whose config carries no ``supervisor:`` block
# (hand-assembled test runtimes). ``config/jarvis.yaml`` is the real
# source of both numbers; mirroring them here means a missing block
# degrades to the shipped cadence instead of silently disabling the
# sweep.
_FALLBACK_SWEEP_INTERVAL_S: float = 30.0
_FALLBACK_SUPERVISOR_BUDGET_S: float = 700.0


def _supervisor_settings(config: Mapping[str, Any]) -> tuple[float, float]:
    """Return ``(sweep_interval_s, default_budget_s)`` from the ``supervisor:`` block."""
    block = config.get("supervisor")
    if not isinstance(block, Mapping):
        return (_FALLBACK_SWEEP_INTERVAL_S, _FALLBACK_SUPERVISOR_BUDGET_S)
    return (
        _positive_float(block.get("sweep_interval_s"), _FALLBACK_SWEEP_INTERVAL_S),
        _positive_float(block.get("default_budget_s"), _FALLBACK_SUPERVISOR_BUDGET_S),
    )


def _run_supervisor_sweep(runtime: JarvisRuntime, *, default_budget_s: float) -> int:
    """Run ONE supervisor sweep pass. Logs and swallows every failure.

    The active set is read FRESH here, on every call (ADR-0009 D4): a
    snapshot taken once at wiring time would keep protecting actions from
    turns that ended minutes ago, and would miss every action dispatched
    since. Both the one-shot bootstrap sweep and the periodic task go
    through this function, so neither can drift from that rule.

    Runs on the event-loop thread over ``runtime.conn`` (loop-thread-only
    by ``check_same_thread``); ``sweep_overdue_actions`` is a bounded
    typed fold over the log, not a blocking call.
    """
    try:
        closed = sweep_overdue_actions(
            runtime.conn,
            active_action_ids=live_action_ids(),
            default_budget_s=default_budget_s,
        )
    except Exception:
        LOGGER.exception("supervisor sweep pass failed; daemon continues.")
        return 0
    if closed:
        LOGGER.info("supervisor sweep closed %d overdue action(s)", closed)
    return closed


# --- ADR-0014 D14 durable confirmation expiry ---------------------------------

# What the sweep's fold reads. `gate.evaluated` is deliberately ABSENT even
# though `_fold_pending_confirmations` accepts it: its only effect on the slot
# is `accepted_unconsumed` -> `consumed`, and the sweep skips both states
# alike. Including it would make every tick re-scan the log's highest-volume
# event type and re-accumulate an unbounded `consumed_lease_ids` set that
# nothing here reads — a cost that grows with the log, on a timer.
_CONFIRMATION_FOLD_TYPES: tuple[str, ...] = (
    "confirmation.requested",
    "confirmation.accepted",
    "confirmation.rejected",
    "confirmation.expired",
)


def _run_confirmation_expiry_sweep(
    conn: sqlite3.Connection,
    *,
    now_ms: int,
    committed_event_bus: CommittedEventBus | None = None,
) -> TerminalOutcome | StaleConfirmation | None:
    """Run ONE expiry pass over the confirmation slot. Swallows every failure.

    ``now_ms`` is supplied by the caller rather than read here, which is what
    makes this body directly callable with an injected clock — the same
    property :func:`_run_supervisor_sweep` has, and the reason neither needs
    a task or a sleep to be exercised.

    Returns None when nothing was due (no slot, an answered slot, or a
    deadline still in the future), in which case no write transaction is
    opened; otherwise the terminalizer's outcome, which may be
    :class:`AlreadyTerminal` or :class:`StaleConfirmation` when a real answer
    or a fresher ask won the CAS.

    Unlike :func:`_run_supervisor_sweep` this must NOT run on the event-loop
    thread: it appends through ``BEGIN IMMEDIATE`` over the connection it is
    handed, and ``runtime.conn`` is loop-thread-only by ``check_same_thread``.
    :func:`_reconcile_confirmation_expiry_in_thread` is the offload both the
    periodic task and the boot reconciler go through.
    """
    try:
        events = tuple(iter_events_of_types(conn, _CONFIRMATION_FOLD_TYPES))
        slot = PendingConfirmations.from_events(events).slot
        if slot is None or slot.state != "pending" or now_ms < slot.expires_at_ms:
            return None
        row = conn.execute(
            "SELECT id, event_uid, json_extract(payload_json, '$.confirmation_id') "
            "FROM events WHERE type = 'confirmation.requested' ORDER BY id DESC LIMIT 1",
        ).fetchone()
        if row is None or str(row[2]) != slot.confirmation_id:
            # The single-slot fold cannot disagree with the newest request
            # row; if it somehow does, the slot this pass folded is not the
            # one a CAS would guard, so append nothing.
            return None
        outcome = terminalize_confirmation(
            conn,
            event_type="confirmation.expired",
            payload={
                "confirmation_id": slot.confirmation_id,
                "expired_at_ms": now_ms,
            },
            expected_revision=int(row[0]),
            source_event_id=str(row[1]),
            committed_event_bus=committed_event_bus,
        )
    except Exception:
        LOGGER.exception("confirmation expiry sweep pass failed; daemon continues.")
        return None
    if isinstance(outcome, TerminalCommitted):
        LOGGER.info(
            "confirmation expiry sweep expired %s (deadline %d, observed %d)",
            slot.confirmation_id,
            slot.expires_at_ms,
            now_ms,
        )
    return outcome


def _reconcile_confirmation_expiry_in_thread(
    event_log_path: Path,
    committed_event_bus: CommittedEventBus | None,
    now_ms: int,
) -> TerminalOutcome | StaleConfirmation | None:
    """Run one expiry pass on an ``asyncio.to_thread`` worker's OWN connection.

    Same offload as the two boot reconcilers above and for the same
    ``check_same_thread`` reason, and it is literally the same body the
    periodic task runs — a boot is just the tick that happens to be first,
    so two boots against one overdue ask leave one row by the terminalizer's
    CAS, not by a separate recovery rule.
    """
    conn = open_event_log(event_log_path)
    try:
        return _run_confirmation_expiry_sweep(
            conn,
            now_ms=now_ms,
            committed_event_bus=committed_event_bus,
        )
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()


def _system_trigger_event(terminal_event: Event) -> Event:
    """Wrap an orphan's terminal row as the trigger for a system turn.

    In-memory only — the append-only log row is never rewritten. The copy
    carries a freshly-minted ``turn_id`` in BOTH slots because the turn
    machinery reads it from two places: :func:`jarvis.runtime.drive_turn`
    from ``payload["turn_id"]``, and L3's
    ``_handle_action_terminal_failure`` from ``correlation["turn_id"]``
    (whose fallback, the packet's ``current_turn_id``, would be some
    unrelated earlier turn for a sweep-emitted row). ``event_uid`` is
    left untouched, so the Limitation claim's evidence still points at
    the real ``action.timeout_assumed`` row.
    """
    turn_id = _new_turn_id()
    return dataclasses.replace(
        terminal_event,
        payload={**terminal_event.payload, "turn_id": turn_id},
        correlation={**(terminal_event.correlation or {}), "turn_id": turn_id},
    )


async def _system_trigger_watcher(
    runtime: JarvisRuntime,
    *,
    anchor_id: int,
    anchored: asyncio.Event,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
) -> None:
    """Background task: drive a system turn for each ORPHAN terminal event.

    Same cursor pattern as :func:`_user_intent_watcher`, with two
    differences that are the whole point of ADR-0009 D4:

    1. **The cursor anchor is supplied, not computed.** The caller
       snapshots ``MAX(events.id)`` BEFORE the one-shot bootstrap sweep
       runs and hands it in; this task then sets ``anchored`` before its
       first poll so the caller knows it may release the sweep. Were the
       anchor computed here, the watcher would start after the sweep and
       its own ``MAX(id)`` would swallow every row the sweep just wrote —
       orphan closure would emit events and drive nothing, silently.
    2. **It handles only events NO live turn owns.** ``action_id`` in the
       live set means an in-flight turn's waiter is going to consume that
       row (:func:`jarvis.runtime._wait_for_next_trigger` filters on
       exactly the complement, through the same
       :func:`jarvis.runtime._event_action_id` key), so this watcher
       skips it and advances the cursor past it. The two predicates
       partition the terminal-event stream: no row is claimed twice, and
       no row with an ``action_id`` is dropped by both.

    The system turn itself is just ``drive_turn`` on a worker thread with
    the terminal row as its trigger: L3's
    ``_handle_action_terminal_failure`` branch answers with the canonical
    limitation text and the ADR-0002 amendment routes it to
    ``queue_review``. The sweep
    is an emitter, not a second brain.

    Cancellation: re-raises :class:`asyncio.CancelledError` so
    :func:`serve_inherent`'s ``finally`` can await it cleanly.
    """
    after_id = anchor_id
    LOGGER.info("system_trigger_watcher started (after_id=%d)", after_id)
    # Set BEFORE the first poll and before anything that could raise: the
    # caller is blocked on this event and will not run the bootstrap
    # sweep until it fires.
    anchored.set()
    try:
        while True:
            new_events = _fetch_events_after(
                runtime.conn,
                after_id=after_id,
                event_types=_SYSTEM_TRIGGER_TYPES,
            )
            for row_id, _ev in new_events:
                after_id = max(after_id, row_id)
            for _row_id, ev in new_events:
                action_id = _event_action_id(ev)
                if action_id is None:
                    continue
                if action_id in live_action_ids():
                    # A live turn is driving it and will fold its own
                    # terminal; a system turn would double-handle it.
                    continue
                if trigger_was_consumed(runtime.conn, ev.event_uid):
                    continue
                trigger = _system_trigger_event(ev)
                try:
                    await asyncio.to_thread(
                        _drive_turn_in_worker_thread,
                        runtime,
                        user_intent_event=trigger,
                    )
                except Exception as exc:  # noqa: BLE001 — ADR-0003 D9 F3 catch-all: log + audit + continue.
                    LOGGER.warning(
                        "system_trigger_watcher: drive_turn raised for "
                        "action_id=%s: %r",
                        action_id,
                        exc,
                    )
                    _emit_turn_failed(
                        runtime.conn,
                        intent_event=trigger,
                        exception_repr=repr(exc),
                    )
            await asyncio.sleep(poll_interval_s)
    except asyncio.CancelledError:
        LOGGER.info("system_trigger_watcher cancelled")
        raise


async def _supervisor_sweep_task(
    runtime: JarvisRuntime,
    *,
    interval_s: float,
    default_budget_s: float,
    compaction: CompactionSweep | None = None,
) -> None:
    """Background task: run the supervisor sweep every ``interval_s`` seconds.

    Sleeps FIRST — the one-shot bootstrap sweep in
    :func:`_start_sweep_control_plane` already covered t=0, and a second
    pass one tick later would be pure noise. Each tick also asks the
    session compaction whether it is due; the job itself runs off-loop.
    """
    LOGGER.info("supervisor_sweep started (interval=%.1fs)", interval_s)
    try:
        while True:
            await asyncio.sleep(interval_s)
            _run_supervisor_sweep(runtime, default_budget_s=default_budget_s)
            if compaction is not None:
                try:
                    compaction.tick()
                except Exception:
                    LOGGER.exception("compaction check failed; daemon continues.")
    except asyncio.CancelledError:
        LOGGER.info("supervisor_sweep cancelled")
        raise


async def _confirmation_expiry_sweep_task(
    runtime: JarvisRuntime,
    *,
    interval_s: float,
) -> None:
    """Background task: run the confirmation expiry sweep every ``interval_s``.

    Sleeps FIRST — the boot reconciler in :func:`serve_inherent` already
    covered t=0, exactly as :func:`_supervisor_sweep_task` defers to its own
    bootstrap pass. Every pass goes through ``asyncio.to_thread`` because it
    writes; the loop thread owns no part of it.
    """
    LOGGER.info("confirmation_expiry_sweep started (interval=%.1fs)", interval_s)
    try:
        while True:
            await asyncio.sleep(interval_s)
            await asyncio.to_thread(
                _reconcile_confirmation_expiry_in_thread,
                runtime.runtime_paths.event_log,
                runtime.committed_event_bus,
                int(time.time() * 1000),
            )
    except asyncio.CancelledError:
        LOGGER.info("confirmation_expiry_sweep cancelled")
        raise


async def _start_sweep_control_plane(
    runtime: JarvisRuntime,
    *,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    compaction: CompactionSweep | None = None,
) -> list[asyncio.Task[None]]:
    """Wire the ADR-0009 D4 sweep control plane. Returns its two tasks.

    The three statements below are ordered, and the order is the pin:

    1. Snapshot ``MAX(events.id)`` — BEFORE anything can write to the log.
    2. Start :func:`_system_trigger_watcher` on that anchor and wait for
       it to signal ``anchored``.
    3. Only THEN run the one-shot bootstrap sweep.

    Inverted (sweep first, watcher second), the watcher's anchor would sit
    at or past the sweep's last emission and every orphan the bootstrap
    sweep just closed would drive no turn at all — the M4 acceptance row
    would no-op with no error anywhere. This function exists as one unit
    precisely so that ordering lives in a single readable place instead of
    inside ``serve_inherent``'s already-long body.
    """
    sweep_interval_s, default_budget_s = _supervisor_settings(runtime.config)

    anchor_id = _latest_id(runtime.conn)
    anchored = asyncio.Event()
    watcher = asyncio.create_task(
        _system_trigger_watcher(
            runtime,
            anchor_id=anchor_id,
            anchored=anchored,
            poll_interval_s=poll_interval_s,
        ),
        name="system_trigger_watcher",
    )
    await anchored.wait()

    _run_supervisor_sweep(runtime, default_budget_s=default_budget_s)

    sweep = asyncio.create_task(
        _supervisor_sweep_task(
            runtime,
            interval_s=sweep_interval_s,
            default_budget_s=default_budget_s,
            compaction=compaction,
        ),
        name="supervisor_sweep",
    )
    return [watcher, sweep]


# --- ADR-0009 D5 repo observer -----------------------------------------------


async def _poll_one_repo(observer: RepoObserver, repo_path: str) -> None:
    """Poll ONE repo: collect off the loop thread, emit on it.

    The thread split is the pin, and it is the only reason this is a
    function rather than two lines inline. ``collect`` runs three ``git``
    subprocesses and touches no database, so it goes through
    :func:`asyncio.to_thread`; ``emit`` writes to ``runtime.conn``, which
    :func:`jarvis.state.event_log.open_event_log` opened with
    ``check_same_thread=True``, so it MUST stay on the loop thread. Move
    the ``emit`` inside the ``to_thread`` call and SQLite raises
    ``ProgrammingError`` on the first observed change — the failure would
    surface as "the observer sees nothing", one poll interval later.

    F5/F6 are already total inside ``collect`` (a hung, failing, or
    missing repo returns ``None``, never raises), so ``None`` here just
    means "skip this repo this cycle". The two ``except`` arms cover what
    ``collect`` does not own: an ``emit`` that fails on the log, and any
    unforeseen raise from the thread hop. Either way this repo is skipped
    and the next one in the cycle still runs.
    """
    try:
        poll = await asyncio.to_thread(observer.collect, repo_path)
    except Exception:  # one bad repo must not kill the observer task.
        LOGGER.exception("repo_observer: collect failed for %s; skipping cycle.", repo_path)
        return
    if poll is None:
        return
    try:
        observer.emit(poll)
    except Exception:  # an emit failure is logged, never fatal to the daemon.
        LOGGER.exception("repo_observer: emit failed for %s; baseline unchanged.", repo_path)


async def _repo_observer_task(observer: RepoObserver, *, interval_s: float) -> None:
    """Background task: poll every watched repo every ``interval_s`` seconds.

    Polls FIRST, then sleeps — the mirror image of
    :func:`_supervisor_sweep_task`, and for the mirror-image reason. No
    bootstrap pass covers t=0 here, and the headline case of ADR-0009 D5
    is exactly the delta that accumulated while the daemon was down; a
    sleep-first loop would sit on the overnight commits for a full
    interval after every restart.

    Cancellation: re-raises :class:`asyncio.CancelledError` so
    :func:`serve_inherent`'s ``finally`` can await it cleanly.
    """
    LOGGER.info(
        "repo_observer started (%d repo(s), interval=%.0fs)",
        len(observer.repo_paths),
        interval_s,
    )
    try:
        while True:
            for repo_path in observer.repo_paths:
                await _poll_one_repo(observer, repo_path)
            await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        LOGGER.info("repo_observer cancelled")
        raise


def _start_repo_observer(runtime: JarvisRuntime) -> list[asyncio.Task[None]]:
    """Wire the ADR-0009 D5 repo observer. Returns its task, or none at all.

    Two ordered statements, and the order is the pin: baselines are
    recovered from the event log BEFORE the task can run its first poll.
    Skip the recovery and every repo looks like a first-ever observation
    on restart — one spurious ``repo.state_observed`` per repo, and the
    ``project.commit_seen`` history for the down window is lost for good
    (a first-ever observation deliberately walks no commits).

    Runs on the loop thread, synchronously, so the ``recover_baselines``
    read of ``runtime.conn`` is on the connection's owning thread.

    An empty ``observer.repos`` starts no task: the observer is opt-in
    perception, and a zero-repo poll loop would be pure wakeups.
    """
    repo_paths = _observer_repo_paths(runtime.config)
    if not repo_paths:
        LOGGER.info("repo_observer: observer.repos is empty; observer not started.")
        return []
    observer = RepoObserver(runtime.conn, repo_paths)
    baselines = observer.recover_baselines()
    LOGGER.info(
        "repo_observer: %d baseline(s) recovered from the event log for %d watched repo(s)",
        len(baselines),
        len(repo_paths),
    )
    return [
        asyncio.create_task(
            _repo_observer_task(
                observer,
                interval_s=_observer_poll_interval_s(runtime.config),
            ),
            name="repo_observer",
        ),
    ]


# --- ADR-0018 usage observer ---------------------------------------------------

_FALLBACK_USAGE_POLL_INTERVAL_S: Final[float] = 300.0
_FALLBACK_MINIMAX_USD_PER_MILLION_CHARS: Final[float] = 60.0


def _usage_observer_block(config: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return ``observer.usage`` when ``enabled: true``; None means off."""
    block = config.get("observer")
    usage = block.get("usage") if isinstance(block, Mapping) else None
    if not isinstance(usage, Mapping) or usage.get("enabled") is not True:
        return None
    return usage


def _usage_observer_config(usage: Mapping[str, Any]) -> UsageConfig:
    """Translate the YAML block into :class:`UsageConfig` (bad values = unset)."""
    anchor_at_ms: int | None = None
    anchor_at = usage.get("minimax_anchor_at")
    if isinstance(anchor_at, str) and anchor_at.strip():
        try:
            anchor_at_ms = int(datetime.datetime.fromisoformat(anchor_at).timestamp() * 1000)
        except ValueError:
            LOGGER.warning(
                "usage_observer: unreadable minimax_anchor_at %r; estimate off", anchor_at
            )
    anchor_usd = usage.get("minimax_anchor_usd")
    return UsageConfig(
        minimax_anchor_usd=(
            float(anchor_usd)
            if isinstance(anchor_usd, int | float) and not isinstance(anchor_usd, bool)
            else None
        ),
        minimax_anchor_at_ms=anchor_at_ms,
        minimax_usd_per_million_chars=_positive_float(
            usage.get("minimax_usd_per_million_chars"),
            _FALLBACK_MINIMAX_USD_PER_MILLION_CHARS,
        ),
    )


def _make_usage_observer(runtime: JarvisRuntime) -> UsageObserver | None:
    """Build the observer with baselines recovered on the loop thread."""
    usage = _usage_observer_block(runtime.config)
    if usage is None:
        LOGGER.info("usage_observer: observer.usage disabled; observer not started.")
        return None
    observer = UsageObserver(runtime.conn, _usage_observer_config(usage))
    baselines = observer.recover_baselines()
    LOGGER.info("usage_observer: %d baseline(s) recovered from the event log", len(baselines))
    return observer


async def _poll_usage_once(observer: UsageObserver) -> None:
    """Collect off the loop thread, emit on it — the same split as the repo observer."""
    try:
        snapshots = await asyncio.to_thread(observer.collect)
    except Exception:  # one bad cycle must not kill the observer task.
        LOGGER.exception("usage_observer: collect failed; skipping cycle.")
        return
    try:
        observer.emit(snapshots)
    except Exception:  # an emit failure is logged, never fatal to the daemon.
        LOGGER.exception("usage_observer: emit failed; baselines unchanged.")


async def _refresh_usage_now(
    observer: UsageObserver, conn: sqlite3.Connection
) -> dict[str, Any]:
    """``POST /inherent/usage/refresh``: one poll, then the read model."""
    await _poll_usage_once(observer)
    return latest_usage(conn)


async def _usage_observer_task(observer: UsageObserver, *, interval_s: float) -> None:
    """Background task: poll every source every ``interval_s`` seconds, poll first."""
    LOGGER.info("usage_observer started (interval=%.0fs)", interval_s)
    try:
        while True:
            await _poll_usage_once(observer)
            await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        LOGGER.info("usage_observer cancelled")
        raise


def _start_usage_observer(
    observer: UsageObserver | None, config: Mapping[str, Any]
) -> list[asyncio.Task[None]]:
    """Start the periodic task for an observer :func:`_make_usage_observer` built."""
    usage = _usage_observer_block(config)
    if observer is None or usage is None:
        return []
    interval_s = _positive_float(usage.get("poll_interval_s"), _FALLBACK_USAGE_POLL_INTERVAL_S)
    return [
        asyncio.create_task(
            _usage_observer_task(observer, interval_s=interval_s),
            name="usage_observer",
        ),
    ]


async def _poll_timesink_once(
    observer: TimesinkObserver, on_checked: Callable[[TimesinkHead], None] | None = None
) -> None:
    """Read the head off the loop thread, emit on it — the usage observer's split.

    ``on_checked`` sees every read, changed or not: the work-state view's
    "checked" clock must move even when the head did not.
    """
    try:
        head = await asyncio.to_thread(observer.collect)
    except Exception:  # one bad cycle must not kill the observer task.
        LOGGER.exception("timesink_observer: collect failed; skipping cycle.")
        return
    if on_checked is not None:
        on_checked(head)
    try:
        observer.emit(head)
    except Exception:  # an emit failure is logged, never fatal to the daemon.
        LOGGER.exception("timesink_observer: emit failed; baseline unchanged.")


async def _timesink_observer_task(
    observer: TimesinkObserver,
    *,
    interval_s: float,
    on_checked: Callable[[TimesinkHead], None] | None = None,
) -> None:
    """ADR 0023 background sync: poll first, then every ``interval_s``; never a model call."""
    LOGGER.info("timesink_observer started (interval=%.0fs)", interval_s)
    try:
        while True:
            await _poll_timesink_once(observer, on_checked)
            await asyncio.sleep(interval_s)
    except asyncio.CancelledError:
        LOGGER.info("timesink_observer cancelled")
        raise


def _start_timesink_observer(runtime: JarvisRuntime) -> list[asyncio.Task[None]]:
    """Start the head poll when ``observer.timesink`` is enabled; baseline from the log."""
    path = _timesink_db_path(runtime.config)
    if path is None:
        LOGGER.info("timesink_observer: observer.timesink disabled; observer not started.")
        return []
    observer = TimesinkObserver(runtime.conn, path)
    observer.recover_baseline()
    return [
        asyncio.create_task(
            _timesink_observer_task(
                observer,
                interval_s=_timesink_poll_interval_s(runtime.config),
                on_checked=None if runtime.work_state is None else runtime.work_state.note_checked,
            ),
            name="timesink_observer",
        ),
    ]


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
    tts_pipe: voice_tts.TTSPipeline | voice_media.StreamingTTSPipeline | None,
) -> None:
    """Bound TTS owners, then stop the player and release PortAudio.

    The caller has already installed the pipeline's close gate and cancelled
    the watcher. TTS provider/fallback work belongs to the pipeline's daemon
    worker rather than asyncio's default executor; :meth:`TTSPipeline.close`
    cancels its owned task/process and joins that worker up to a hard bound.
    """
    if tts_pipe is None:
        return
    try:
        tts_pipe.close()
    except Exception:  # noqa: BLE001 — shutdown errors must not mask uvicorn return
        LOGGER.debug("tts pipeline close failed", exc_info=True)


def _request_tts_close(
    tts_pipe: voice_tts.TTSPipeline | voice_media.StreamingTTSPipeline | None,
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

    Also the only place startup distinguishes the two input owners:
    ``WakeEngine.start`` logs an identical line from either, which has already
    cost one investigation a wrong conclusion.
    """
    if owners.duplex_session is not None:
        input_owner = "single_ingress"
    elif owners.wake_listener is not None:
        input_owner = "legacy_wake"
    else:
        input_owner = "none"
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


def _shutdown_wake(
    wake_listener: voice_wake.WakeListener | None,
    wake_stream: Any | None,  # noqa: ANN401 — sounddevice stream is untyped third-party API
    *,
    join_timeout_s: float = _VoiceKnobs().wake_join_timeout_s,
) -> None:
    """Stop wake listener and close its input stream in the safe order.

    Order MUST be: request_stop -> join -> stream.stop -> stream.close.
    Closing the stream before the listener thread exits its blocking
    stream.read() is undefined PortAudio behaviour and was the historical
    segfault root cause (see jarvis-legacy/core/inherent_wake_listener.py:73-78).
    """
    if wake_listener is None:
        return
    wake_listener.request_stop()
    wake_listener.join(timeout_s=join_timeout_s)
    if wake_listener.is_alive():
        LOGGER.warning(
            "wake listener thread did not exit within %.1f s; "
            "proceeding with stream close (segfault risk reduced "
            "but not eliminated)",
            join_timeout_s,
        )
    if wake_stream is not None:
        try:
            wake_stream.stop()
        except Exception:  # noqa: BLE001 — shutdown errors must not mask uvicorn return
            LOGGER.debug("wake_stream stop failed", exc_info=True)
        try:
            wake_stream.close()
        except Exception:  # noqa: BLE001 — close still runs after stop failure
            LOGGER.debug("wake_stream close failed", exc_info=True)


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
    tts_pipe: voice_tts.TTSPipeline | voice_media.StreamingTTSPipeline | None,
    *,
    wake_join_timeout_s: float = _VoiceKnobs().wake_join_timeout_s,
) -> None:
    """Select the exact Wave-3 or legacy shutdown order used by serve."""
    if owners.duplex_session is not None:
        _shutdown_duplex_voice_session(owners.duplex_session)
        _request_tts_close(tts_pipe)
        return
    _request_tts_close(tts_pipe)
    _shutdown_wake(
        owners.wake_listener,
        owners.wake_stream,
        join_timeout_s=wake_join_timeout_s,
    )


def _v2_runtime_capabilities(
    *,
    voice_input: bool,
    response_interrupt: bool,
) -> RuntimeCapabilities:
    """Report what this daemon can actually do, for the D7 server hello.

    Discovered from the wiring rather than declared: ``image_input`` is
    false because ``/inherent/image-submit`` is still a 501 stub, and the
    action / confirmation controls are false because no route accepts them
    yet. ``aec_profile`` is ``headphones_only`` — there is no acoustic echo
    canceller, so barge-in over speakers is not offered.
    """
    return RuntimeCapabilities(
        text_input=True,
        image_input=False,
        voice_input=voice_input,
        response_interrupt=response_interrupt,
        action_cancel=False,
        confirmation_actions=False,
        natural_barge_in=False,
        aec_profile="headphones_only",
    )


V2_PRINCIPAL: Final[str] = "inherent_v2"
"""The ADR-0014 D21 ``authenticated_principal``.

The v2 socket authenticates one shared per-boot bearer token, not a user, so
this constant names that credential rather than a person.  The idempotency key
still separates clients through ``client_instance_id``; a per-user principal
arrives with a real identity mechanism.
"""


LIVE_PRINCIPAL: Final[str] = "gpt_live"
"""ADR-0016 D2: the D21 principal and ``channel`` of a Live delegation."""

_LIVE_TERMINAL_TYPES: Final[frozenset[str]] = frozenset(
    {
        "surface.response_emitted",
        "response.completed",
        "response.failed",
        "response.cancelled",
    },
)
"""Committed events that wake a delegation: the answer row and the run terminals.

``response.completed`` commits before the renderer writes ``surface.response_emitted``,
the only row ``lookup_result`` takes as an answer, so a wake on the terminal alone
left the bridge to its 5 s safety poll (measured 5.06 s and 5.9 s, 2026-09-12).
"""

_LIVE_OUTCOME_TYPES: Final[tuple[str, ...]] = (
    "surface.response_emitted",
    "response.failed",
    "response.cancelled",
    "turn.failed",
)


# ADR 0026: a new session is told at most this many finished delegations no
# session was told, none older than this; the rest stay in memory.db and the UI.
_UNDELIVERED_WINDOW_MS: Final[int] = 24 * 60 * 60 * 1000
_UNDELIVERED_MAX: Final[int] = 3


class _LiveBackend:
    """ADR-0016 D9 and ADR 0026: the blocking callables the composition root hands to LiveVoice.

    Every method opens its own SQLite connection because LiveVoice calls them
    through ``asyncio.to_thread``, never on the loop thread.
    """

    def __init__(
        self, *, event_log_path: Path, memory: MemorySettings | None, session: SessionSettings,
    ) -> None:
        self._event_log_path = event_log_path
        self._memory = memory
        self._session = session

    def delegate(self, text: str, delegation_id: str, session_id: str, record_id: str) -> str:
        """Submit one delegation through the D21 inbox; a replay returns the same turn."""
        key = SubmissionKey(LIVE_PRINCIPAL, session_id, delegation_id)
        conn = open_event_log(self._event_log_path)
        try:
            receipt = submit_text_once(
                conn,
                key=key,
                transcript=text,
                channel=LIVE_PRINCIPAL,
                source_surface=LIVE_PRINCIPAL,
                record_id=record_id,
            )
        finally:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
        LOGGER.info(
            "gpt_live delegate %s -> turn %s (replayed=%s)",
            delegation_id, receipt.turn_id, receipt.replayed,
        )
        return receipt.turn_id

    def record(self, source: str, text: str, record_id: str) -> None:
        """One memory.db row; a repeated id is a no-op (D3)."""
        if self._memory is None:
            return
        append_record(self._memory.db_path, record_id=record_id, source=source, text=text)

    def brief(self) -> str:
        """The budgeted startup history for ``session.start.input`` (D7)."""
        if self._memory is None:
            return ""
        return brief_note(self._memory.db_path, max_chars=self._session.live_brief_max_chars)

    def lookup_result(self, turn_id: str) -> voice_live.DelegationResult | None:
        """The turn's final answer, its failure, or None while it is still running (D4, D5)."""
        conn = open_event_log(self._event_log_path)
        try:
            return _turn_outcome(conn, turn_id)
        finally:
            with contextlib.suppress(sqlite3.Error):
                conn.close()

    def undelivered_results(self) -> list[voice_live.UndeliveredResult]:
        """Finished delegations of the last day that no session was told, oldest first."""
        since = int(time.time() * 1000) - _UNDELIVERED_WINDOW_MS
        conn = open_event_log(self._event_log_path)
        found: list[voice_live.UndeliveredResult] = []
        try:
            intents = [
                event
                for event in iter_events_of_types(
                    conn, ("surface.user_intent",), since_epoch_ms=since,
                )
                if event.payload.get("channel") == LIVE_PRINCIPAL
            ]
            for intent in reversed(intents):
                turn_id = str(intent.payload.get("turn_id", ""))
                if not turn_id or any(
                    iter_events_for_turn(conn, turn_id, ("live.result_delivered",)),
                ):
                    continue
                outcome = _turn_outcome(conn, turn_id)
                if outcome is None:
                    continue  # still running: no outcome yet; the next start looks again
                found.append(
                    voice_live.UndeliveredResult(
                        turn_id=turn_id,
                        request=str(intent.payload.get("transcript", "")),
                        result=outcome,
                    ),
                )
                if len(found) == _UNDELIVERED_MAX:
                    break
        finally:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
        found.reverse()
        return found

    def mark_delivered(self, turn_id: str, session_id: str, kind: str) -> None:
        """Persist that ``session_id`` was told ``turn_id``'s outcome, or that it was withheld."""
        self._emit(
            "live.result_delivered",
            {"turn_id": turn_id, "session_id": session_id, "kind": kind},
            correlation={"turn_id": turn_id},
        )

    def record_usage(
        self,
        session_id: str,
        seconds: float | None,
        reason: str,
        server_reason: str | None,
        final: bool,  # noqa: FBT001 - Callable shape fixed by LiveVoice
    ) -> None:
        """One ``live.session_usage`` row per closed session (ADR 0026)."""
        self._emit(
            "live.session_usage",
            {
                "session_id": session_id,
                "seconds": seconds,
                "final": final,
                "reason": reason,
                "server_reason": server_reason,
            },
        )

    def _emit(
        self,
        event_type: str,
        payload: dict[str, object],
        *,
        correlation: dict[str, str] | None = None,
    ) -> None:
        conn = open_event_log(self._event_log_path)
        try:
            emit_event(conn, type=event_type, payload=payload, correlation=correlation)
        finally:
            with contextlib.suppress(sqlite3.Error):
                conn.close()


def _turn_outcome(conn: sqlite3.Connection, turn_id: str) -> voice_live.DelegationResult | None:
    """The turn's final answer, its failure, or None while it is still running.

    Only the ``phase == "final"`` emission counts: the ADR-0008 D6 lifecycle
    commentary renders under the same ``turn_id`` with ``phase == "commentary"``.
    """
    events = list(iter_events_for_turn(conn, turn_id, _LIVE_OUTCOME_TYPES))
    for event in events:
        if (
            event.type == "surface.response_emitted"
            and event.payload.get("phase", "final") == "final"
        ):
            voice_text = event.payload.get("voice_text")
            return voice_live.DelegationResult(
                status="answered",
                text=str(event.payload.get("text", "")),
                voice_text=voice_text if isinstance(voice_text, str) and voice_text else None,
            )
    for event in events:
        if event.type in ("response.failed", "response.cancelled", "turn.failed"):
            reason = event.payload.get("reason") or event.payload.get("exception_repr")
            return voice_live.DelegationResult(
                status="failed", reason=str(reason or event.type),
            )
    return None


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
    inner_conn = open_event_log(event_log_path)
    try:
        return _v2_accepted(submit_text_once(inner_conn, key=key, transcript=text))
    except PayloadConflictError:
        return InputSubmissionOutcome(outcome="payload_conflict")
    finally:
        with contextlib.suppress(sqlite3.Error):
            inner_conn.close()


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
    inner_conn = open_event_log(event_log_path)
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


async def serve_inherent(  # noqa: C901, PLR0912, PLR0915 — composition-root entrypoint; the keyword args ARE the daemon contract and the voice-wiring plus boot-reconciliation branches necessarily inflate body length + branch count.
    runtime: JarvisRuntime,
    *,
    host: str = "127.0.0.1",
    port: int = _DEFAULT_PORT,
    lock_path: Path,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
) -> None:
    """Run the Inherent daemon (text + voice). Blocks until SIGINT / SIGTERM.

    Lifecycle (the ``acquire_exclusive`` context manager is the
    OUTERMOST scope so the lock release is the very LAST cleanup step):

    1. :func:`acquire_exclusive` — raises
       :class:`jarvis.deployment.process_lock.ProcessLockHeld` if
       another live daemon already holds it.
    2. Build :class:`InherentBroadcaster` and attach the running
       event loop via :meth:`InherentBroadcaster.attach_loop` (ADR-0005
       §4.2 worker-thread → broadcaster bridge); bind
       ``submit_callable`` to :func:`emit_surface_user_intent` with a
       fresh ``turn_id`` per HTTP submit.
    3. ADR-0005 §12 pre-flight — call :func:`_voice_models_preflight`
       on the configured SenseVoice + Silero paths. When models are
       present, construct :class:`VoicePipeline`, optionally
       :class:`TTSPipeline` (if ``MINIMAX_API_KEY`` is set), and (when
       ``JARVIS_VOICE_DISABLE_WAKE`` is unset) start a
       :class:`WakeListener` thread. Missing models or any construction
       failure: log ERROR and continue text-only — the text path stays
       healthy.
    4. Build the FastAPI app via :func:`create_app` with
       :class:`InherentDeps` (carries ``voice_pipeline_callable`` so
       ``/inherent/asr-submit`` can do PTT ASR even without a wake
       listener; falls through to 501 when the pipeline is None).
    5. Configure :class:`uvicorn.Config` (``lifespan="off"`` because
       this module owns the lifecycle, ``log_level="warning"`` to
       avoid uvicorn's per-request stdout noise drowning the watcher
       logs).
    6. Spawn :func:`_user_intent_watcher` and :func:`_response_watcher`
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
    9. ``finally``: tear the power observer down FIRST (its closed flag
       must be set before the loop starts winding down), then request the
       wake listener stop (if running), cancel every watcher task, and
       ``await`` them with ``return_exceptions=True`` so a watcher that
       crashed during runtime does not mask the shutdown path. Then the
       ``acquire_exclusive`` context manager unwinds and releases the
       lock file.

    Env vars:
        ``MINIMAX_API_KEY``         — gates the TTS subsystem.
        ``JARVIS_VOICE_DISABLE_WAKE`` — when ``"1"``, skip the wake
            thread even if models are present (CI / smoke tests).

    Args:
        runtime: Assembled :class:`JarvisRuntime`.
        host: Bind interface for the FastAPI app.
        port: TCP port for the FastAPI app.
        lock_path: Per-runtime-root daemon lock file. Resolved by
            ``jarvis.cli._main_serve`` to ``${runtime_root}/daemon.lock``.
        poll_interval_s: Watcher poll cadence (default 10 ms).

    Raises:
        jarvis.deployment.process_lock.ProcessLockHeld: Another daemon
            already holds the lock for this runtime root.
    """
    with acquire_exclusive(lock_path):
        # ADR-0014 D14 — read once, so the boot reconciler and the periodic
        # sweep can never disagree about whether this daemon writes durable
        # expiry rows.
        durable_confirmation_expiry = _durable_confirmation_expiry_enabled(runtime.config)
        broadcaster = InherentBroadcaster()
        broadcaster.attach_loop(asyncio.get_running_loop())

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
            inner_conn = open_event_log(runtime.runtime_paths.event_log)
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

        # ADR-0005 §12 pre-flight + voice subsystem wiring. Any failure
        # downgrades the daemon to text-only — text path must stay
        # healthy when models / SDKs / mics are missing (CI default).
        voice_pipe: voice_pipeline.VoicePipeline | None = None
        tts_pipe: voice_tts.TTSPipeline | voice_media.StreamingTTSPipeline | None = None
        duplex_voice_session: voice_session.DuplexVoiceSession | None = None
        # ADR-0015: the two mute switches the desktop surface flips over
        # POST /inherent/controls: mic on the wake threads, speech as the player's gain.
        controls = voice_controls.VoiceControls()
        voice_input_owners = _VoiceInputOwners(
            duplex_session=None,
            wake_listener=None,
            wake_stream=None,
            single_ingress_attempted=False,
        )
        voice_pipeline_callable: Any | None = None
        # ADR-0005 §5.1 / §5.3: ONE shared SystemAudioDucker arbitrates
        # wake-capture muting against TTS provider/playback output leases.
        shared_ducker: voice_ducking.SystemAudioDucker = voice_ducking.SystemAudioDucker()

        live_voice: voice_live.LiveVoice | None = None

        def _old_chain_input_blocked() -> bool:
            # ADR-0015 mic mute, plus: while GPT-Live owns speech the local chain
            # arms no new wake, so one utterance cannot be answered twice.
            return controls.mic_is_muted() or (live_voice is not None and live_voice.owns_speech)

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
        sensevoice_dir = runtime.sensevoice_dir
        silero_path = runtime.silero_vad_path
        models_ok, missing = _voice_models_preflight(
            sensevoice_dir=sensevoice_dir,
            silero_path=silero_path,
        )
        voice_startup_reason = "models_missing"
        if not models_ok:
            LOGGER.error(
                "voice models missing; running text-only. Missing: %s",
                "; ".join(missing),
            )
        else:
            try:
                voice_pipe = _build_voice_pipeline(
                    runtime,
                    broadcaster=broadcaster,
                    sensevoice_dir=sensevoice_dir,
                )
                voice_pipeline_callable = _build_voice_pipeline_callable(voice_pipe)
                tts_pipe = _build_tts_pipeline(
                    runtime,
                    broadcaster,
                    ducker=shared_ducker,
                    voice=voice_knobs,
                )
                if os.environ.get("JARVIS_VOICE_DISABLE_WAKE") == "1":
                    voice_startup_reason = "wake_disabled_env"
                    LOGGER.info(
                        "JARVIS_VOICE_DISABLE_WAKE=1; skipping WakeListener spawn.",
                    )
                else:
                    voice_input_owners = _spawn_voice_input_owners(
                        runtime=runtime,
                        pipeline=voice_pipe,
                        broadcaster=broadcaster,
                        silero_path=silero_path,
                        tts=tts_pipe,
                        ducker=shared_ducker,
                        voice=voice_knobs,
                        mic_muted=_old_chain_input_blocked,
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
                    wake_listener=None,
                    wake_stream=None,
                    single_ingress_attempted=False,
                )

        # GPT-Live phase A (docs/gpt-live-integration-planning.md §11). Building
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
                gpt_live_config = voice_live.gpt_live_config_from_mapping(gpt_live_raw)
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
                output_device=realtime_map.get("output_device"),
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
        # watcher task exists. Off unless the lifecycle flag is on.
        if runtime.response_flags.response_run_lifecycle:
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
        # is gated on realtime.streaming_output, not on response_run_lifecycle,
        # so any flag gate here admits a legal config whose orphans no boot
        # ever closes. The fold appends nothing when no generation is open.
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

        # ADR-0014 D14 — a confirmation may have passed `expires_at_ms`
        # while no process was running to notice. Re-drive the same expiry
        # sweep once here, after both reconcilers above, so a panel that
        # reconnects to this boot is cleared by a committed row. The
        # terminalizer's CAS is what makes a second boot append nothing.
        if durable_confirmation_expiry:
            await asyncio.to_thread(
                _reconcile_confirmation_expiry_in_thread,
                runtime.runtime_paths.event_log,
                runtime.committed_event_bus,
                int(time.time() * 1000),
            )

        cancel_response_callable = (
            make_response_cancel_callable(
                runtime,
                # ADR-0006 D8: only a live playback actor can stop the tail
                # of a run the registry has already released.
                stop_foreground_output=(
                    tts_pipe.stop_foreground_output
                    if isinstance(tts_pipe, voice_media.StreamingTTSPipeline)
                    else None
                ),
            )
            if runtime.response_flags.independent_response_cancel
            else None
        )
        # ADR-0014 D5: a fresh 256-bit bearer token every boot, written
        # 0600 under the runtime root. Rotating INSIDE the process lock is
        # what makes it safe — no second daemon can be mid-read of the
        # file this one is replacing.
        v2_token = rotate_inherent_v2_token(runtime.runtime_paths.inherent_v2_token)
        boot_id = new_boot_id()
        log_epoch = read_log_epoch(runtime.conn)
        # ADR-0014 D8/D9 — the v2 sequencer and client hub, behind
        # realtime.inherent.v2_sequencer.enabled. It drains the log through
        # the current high-water before returning; None leaves the v2 socket
        # exactly as card 1 left it (hello, then silence) and v1 untouched.
        inherent_view = await start_inherent_view(
            runtime,
            boot_id=boot_id,
            log_epoch=log_epoch,
            poll_interval_s=poll_interval_s,
        )
        # ADR-0018 — usage observer: constructed here (baselines recovered on
        # the loop thread) so the dashboard routes can hold its read model
        # and on-demand poll; its periodic task joins `watchers` below.
        usage_observer = _make_usage_observer(runtime)
        window_memory = runtime.memory

        def _read_conversation(after: int, limit: int) -> dict[str, Any]:
            """Spec §18.3: the window's rows past ``after`` and the history floor they start at."""
            since = runtime.session.history_since
            rows = (
                []
                if window_memory is None
                else conversation_rows(window_memory.db_path, since=since, after=after, limit=limit)
            )
            return {"since": since, "rows": rows}

        deps = InherentDeps(
            submit_callable=submit_callable,
            broadcaster=broadcaster,
            voice_pipeline_callable=voice_pipeline_callable,
            usage_read=(
                None if usage_observer is None else functools.partial(latest_usage, runtime.conn)
            ),
            usage_refresh=(
                None
                if usage_observer is None
                else functools.partial(_refresh_usage_now, usage_observer, runtime.conn)
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
            plugin_read=runtime.plugin_connections.read if runtime.plugin_connections else None,
            plugin_action=runtime.plugin_connections.action if runtime.plugin_connections else None,
            plugin_authorize=(
                runtime.plugin_connections.settings.matches if runtime.plugin_connections else None
            ),
            barge_in_confirm_callable=(
                duplex_voice_session.confirm_ptt_barge_in
                if duplex_voice_session is not None and duplex_voice_session.barge_in_armed
                else None
            ),
            cancel_response_callable=cancel_response_callable,
            controls=controls,
            live=live_voice,
            v2=InherentV2Deps(
                token_matches=functools.partial(inherent_v2_token_matches, v2_token),
                mint_connection_id=new_connection_id,
                boot_id=boot_id,
                log_epoch=log_epoch,
                high_water_cursor=functools.partial(_latest_id, runtime.conn),
                runtime_capabilities=functools.partial(
                    _v2_runtime_capabilities,
                    voice_input=voice_pipeline_callable is not None,
                    response_interrupt=cancel_response_callable is not None,
                ),
                attach_client=None if inherent_view is None else inherent_view.attach_client,
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

        config = uvicorn.Config(
            app=app,
            host=host,
            port=port,
            log_level="warning",
            lifespan="off",
        )
        server = uvicorn.Server(config)

        watchers: list[asyncio.Task[None]] = (
            [] if inherent_view is None else list(inherent_view.tasks)
        )
        if runtime.input_flags.intent_pump:
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
        else:
            watchers.append(
                asyncio.create_task(
                    _user_intent_watcher(runtime, poll_interval_s=poll_interval_s),
                    name="user_intent_watcher",
                ),
            )
        watchers.append(
            asyncio.create_task(
                _response_watcher(runtime, broadcaster, poll_interval_s=poll_interval_s),
                name="response_watcher",
            ),
        )
        if tts_pipe is not None:
            watchers.append(
                asyncio.create_task(
                    _tts_watcher(
                        conn=runtime.conn,
                        pipeline=tts_pipe,
                        poll_interval_s=poll_interval_s,
                    ),
                    name="tts_watcher",
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

        if durable_confirmation_expiry:
            # ADR-0014 D14 (Step 3). Flag off, no task exists and the event
            # log is byte-identical to a build without this sweep. Joins
            # `watchers` so the existing teardown cancels it with no new
            # teardown path.
            watchers.append(
                asyncio.create_task(
                    _confirmation_expiry_sweep_task(
                        runtime,
                        interval_s=_confirmation_expiry_sweep_interval_s(runtime.config),
                    ),
                    name="confirmation_expiry_sweep",
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

        # ADR-0009 D3 — power observer, installed after the lock (which
        # stays the outermost scope) and immediately before the try/finally
        # that owns the serve lifetime, so its registered CFRunLoop thread
        # is bracketed by the same `finally` that tears the watchers down;
        # no path between install and try can leak a live registration.
        power_coordinator = (
            _VoicePowerCoordinator(
                session=duplex_voice_session,
                media=tts_pipe,
            )
            if duplex_voice_session is not None
            and isinstance(tts_pipe, voice_media.StreamingTTSPipeline)
            else None
        )
        before_sleep_hook = (
            power_coordinator.before_sleep if power_coordinator is not None else None
        )
        on_wake_hook = power_coordinator.on_wake if power_coordinator is not None else None
        power_observer = _install_power_observer_or_degrade(
            runtime.conn,
            asyncio.get_running_loop(),
            before_sleep_hook=before_sleep_hook,
            on_wake_hook=on_wake_hook,
        )

        try:
            await server.serve()
        finally:
            LOGGER.info("serve_inherent: shutting down watchers")
            # Power observer FIRST: its closed flag must be set before the
            # loop starts winding down, so a notification racing this
            # teardown finds the flag instead of a dead loop. Everything
            # below (wake / TTS / ducker / watcher cancel) is loop-thread
            # work that would otherwise be racing that notification.
            _shutdown_power_observer(power_observer)
            if power_coordinator is not None:
                power_coordinator.close(timeout_s=0.1)
            # ADR-0006 F14: Wave 3 revokes input before output. Feature-off
            # retains the legacy output-gate-before-wake order.
            # GPT-Live is billed per second: hang up before the microphone goes away.
            # The budget follows close_timeout_s and stays under launchd's default
            # 20 s ExitTimeOut, after which the agent is killed mid-hangup.
            if live_voice is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        live_voice.stop(reason="daemon_shutdown"),
                        timeout=live_voice.stop_budget_s,
                    )
            _request_voice_input_branch_shutdown(
                voice_input_owners,
                tts_pipe,
                wake_join_timeout_s=voice_knobs.wake_join_timeout_s,
            )
            # Cancel/await watcher ownership before PortAudio teardown. A
            # provider thread may still exist, but the closed generation owns
            # no right to write or invoke fallback.
            for w in watchers:
                w.cancel()
            await asyncio.gather(*watchers, return_exceptions=True)
            # ADR 0019: the codex app-server child goes with the daemon; its
            # open worker_edges rows become closed.
            if runtime.workers is not None:
                await asyncio.to_thread(runtime.workers.stop)
            # ADR 0031: every MCP client exits on its own task, then its loop thread ends.
            if runtime.plugin_connections is not None:
                await asyncio.to_thread(runtime.plugin_connections.stop)
            if runtime.mcp_servers is not None:
                await asyncio.to_thread(runtime.mcp_servers.stop)
            _shutdown_tts(tts_pipe)
            # Force-restore output volume in case a duck escaped a finally
            # block on the way down (best-effort; idempotent if depth == 0).
            try:
                shared_ducker.restore_all()
            except Exception:  # noqa: BLE001 — shutdown errors must not mask uvicorn return
                LOGGER.debug("shared_ducker.restore_all failed", exc_info=True)
            LOGGER.info("serve_inherent: shutdown complete")


__all__ = ["serve_inherent"]
