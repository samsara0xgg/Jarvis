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
     once, and drives the turn via :func:`jarvis.runtime.turn.drive.drive_turn` on a
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
import os
import signal
import socket
import sqlite3
import threading
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import uvicorn

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Generator, Sequence
    from datetime import date, tzinfo
    from pathlib import Path

    from jarvis.deployment.sleep_wake import PowerObserver
    from jarvis.runtime.picture_notes import PictureNotes
    from jarvis.state.committed_event_bus import CommittedEventBus
    from jarvis.surface.codex_sessions import CodexSession

from jarvis.decision.response_run import (
    ResponseTerminalizer,
    reconcile_open_responses,
)
from jarvis.deployment import inherent_v2_token_matches, models, rotate_inherent_v2_token
from jarvis.deployment.launchd import logs_dir, supervised
from jarvis.deployment.sleep_wake import install_power_observer
from jarvis.execution.transit_tool import live_card
from jarvis.runtime import JarvisRuntime, _jev_log, save_language
from jarvis.runtime.card_feedback import CardFeedback
from jarvis.runtime.core_memory import CoreMemorySettings, section_caps
from jarvis.runtime.daemon.commentary import (
    commentary_watcher,
)
from jarvis.runtime.daemon.conversation_lines import (
    plan_hooks,
    say_job_line,
    say_reminder,
)
from jarvis.runtime.daemon.devices import output_or_default as _output_or_default
from jarvis.runtime.daemon.devices import start_audio_device_watch as _start_audio_device_watch
from jarvis.runtime.daemon.devices import watch_board as _watch_board
from jarvis.runtime.daemon.input_session import spawn_voice_input_owners
from jarvis.runtime.daemon.intent_pump import start_intent_pump as _start_intent_pump
from jarvis.runtime.daemon.live_backend import LIVE_TERMINAL_TYPES as _LIVE_TERMINAL_TYPES
from jarvis.runtime.daemon.live_backend import LiveBackend as _LiveBackend
from jarvis.runtime.daemon.live_backend import ask_outcome as _ask_outcome
from jarvis.runtime.daemon.observers import agents_port as _agents_port
from jarvis.runtime.daemon.observers import claude_sessions_served as _claude_sessions_served
from jarvis.runtime.daemon.observers import make_usage_observer as _make_usage_observer
from jarvis.runtime.daemon.observers import refresh_usage_now as _refresh_usage_now
from jarvis.runtime.daemon.observers import start_meaning_index as _start_meaning_index
from jarvis.runtime.daemon.observers import start_repo_observer as _start_repo_observer
from jarvis.runtime.daemon.observers import start_timesink_observer as _start_timesink_observer
from jarvis.runtime.daemon.observers import start_usage_observer as _start_usage_observer
from jarvis.runtime.daemon.output_watchers import add_places as _add_places
from jarvis.runtime.daemon.output_watchers import add_reactions as _add_reactions
from jarvis.runtime.daemon.output_watchers import add_steps as _add_steps
from jarvis.runtime.daemon.output_watchers import log_watcher_death as _log_watcher_death
from jarvis.runtime.daemon.output_watchers import phone_ends_watcher as _phone_ends_watcher
from jarvis.runtime.daemon.output_watchers import response_watcher as _response_watcher
from jarvis.runtime.daemon.output_watchers import tool_status_watcher as _tool_status_watcher
from jarvis.runtime.daemon.output_watchers import tts_watcher as _tts_watcher
from jarvis.runtime.daemon.output_watchers import turn_devices as _turn_devices
from jarvis.runtime.daemon.phone_brain import build_brain_listening, build_phone_hub
from jarvis.runtime.daemon.phone_brain import build_brain_voice as _build_brain_voice
from jarvis.runtime.daemon.polling import DEFAULT_POLL_INTERVAL_S as _DEFAULT_POLL_INTERVAL_S
from jarvis.runtime.daemon.restart import fetch_models_then_restart as _fetch_models_then_restart
from jarvis.runtime.daemon.restart import restart_soon as _restart_soon
from jarvis.runtime.daemon.retention import clear_media as _clear_media
from jarvis.runtime.daemon.retention import data_sweep_task as _data_sweep_task
from jarvis.runtime.daemon.retention import erase_data as _erase_data
from jarvis.runtime.daemon.retention import export_data as _export_data
from jarvis.runtime.daemon.retention import link_earlier_pictures as _link_pictures
from jarvis.runtime.daemon.retention import media_dirs as _media_dirs
from jarvis.runtime.daemon.retention import release_uploaded_pictures as _release_pictures
from jarvis.runtime.daemon.retention import start_upload as _start_upload
from jarvis.runtime.daemon.routes import act_commitment as _act_commitment
from jarvis.runtime.daemon.routes import alert_deps as _alert_deps
from jarvis.runtime.daemon.routes import answer_doing as _answer_doing
from jarvis.runtime.daemon.routes import archive_mail as _archive_mail
from jarvis.runtime.daemon.routes import card_act as _card_act
from jarvis.runtime.daemon.routes import day_sources as _day_sources
from jarvis.runtime.daemon.routes import draft_deps as _draft_deps
from jarvis.runtime.daemon.routes import her_state as _her_state
from jarvis.runtime.daemon.routes import lifelog_coverage as _lifelog_coverage
from jarvis.runtime.daemon.routes import mail_letter as _mail_letter
from jarvis.runtime.daemon.routes import mail_summary as _mail_summary
from jarvis.runtime.daemon.routes import mail_trash as _mail_trash
from jarvis.runtime.daemon.routes import mark_mail_read as _mail_read
from jarvis.runtime.daemon.routes import model_takes_images as _model_takes_images
from jarvis.runtime.daemon.routes import moment_hold as _moment_hold
from jarvis.runtime.daemon.routes import notice_deps as _notice_deps
from jarvis.runtime.daemon.routes import open_lifelog as _lifelog
from jarvis.runtime.daemon.routes import refresh_work_state_now as _refresh_work_state_now
from jarvis.runtime.daemon.routes import ride_act as _ride_act
from jarvis.runtime.daemon.routes import save_settings as _save_settings
from jarvis.runtime.daemon.routes import serve_day as _serve_day
from jarvis.runtime.daemon.routes import serve_today as _serve_today
from jarvis.runtime.daemon.routes import set_highlight as _set_highlight
from jarvis.runtime.daemon.routes import set_todo as _set_todo
from jarvis.runtime.daemon.routes import settings_notice_deps as _settings_notice_deps
from jarvis.runtime.daemon.routes import shipments as _shipments
from jarvis.runtime.daemon.routes import turn_end_asks as _turn_end_asks
from jarvis.runtime.daemon.sweep import start_sweep_control_plane as _start_sweep_control_plane
from jarvis.runtime.daemon.voice_power import VoiceInputOwners as _VoiceInputOwners
from jarvis.runtime.daemon.voice_power import VoicePowerCoordinator as _VoicePowerCoordinator
from jarvis.runtime.daemon.voice_wiring import VoiceKnobs as _VoiceKnobs
from jarvis.runtime.daemon.voice_wiring import build_echo_canceller as _build_echo_canceller
from jarvis.runtime.daemon.voice_wiring import build_tts_pipeline as _build_tts_pipeline
from jarvis.runtime.daemon.voice_wiring import build_voice_pipeline as _build_voice_pipeline
from jarvis.runtime.daemon.voice_wiring import (
    build_voice_pipeline_callable as _build_voice_pipeline_callable,
)
from jarvis.runtime.daemon.voice_wiring import voice_knobs as _voice_knobs
from jarvis.runtime.daemon.voice_wiring import voice_models_preflight as _voice_models_preflight
from jarvis.runtime.day_experience import DayExperienceSchedule, DayExperienceSettings
from jarvis.runtime.day_plan import DayPlanSettings, DayPlanSources, build_schedule
from jarvis.runtime.day_summary import DaySummarySchedule, DaySummarySettings
from jarvis.runtime.dictation import (
    Dictation,
    build_dictation,
    local_polisher,
)
from jarvis.runtime.doing_answers import DoingAnswers
from jarvis.runtime.highlight_marks import HighlightMarks
from jarvis.runtime.memory_page import MemoryPage
from jarvis.runtime.night_watch import NightWatch
from jarvis.runtime.plan_run import build_watch, leg_session, make_route_legs
from jarvis.runtime.session_compaction import CompactionSweep, preset_context_length
from jarvis.runtime.setup import Setup
from jarvis.runtime.spend_cap import SpendCap, SpendCapSettings
from jarvis.runtime.turn.cancel import (
    make_response_cancel_callable,
    make_turn_cancel_callable,
)
from jarvis.runtime.turn.start import new_turn_id as _new_turn_id
from jarvis.runtime.work_state import auto_refresh
from jarvis.shared import Event, lang
from jarvis.shared.pricing import load_pricing_table
from jarvis.shared.realtime_trace import record_realtime_trace
from jarvis.state import (
    footprint,
    phone_track,
    places,
    quiet_mode,
    reactions,
)
from jarvis.state.daily_report import resolve_zone
from jarvis.state.day_line import DaySources, day_window
from jarvis.state.device_tokens import PairingCodes, device_name_for_token, device_token_matches
from jarvis.state.event_log import (
    MAC_NODE,
    confirmation_device,
    emit_event,
    iter_events_of_types,
    open_event_log,
    open_runtime_event_log,
    turn_origin,
)
from jarvis.state.imports import IMPORTS_DIRNAME
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
from jarvis.state.memory_db import (
    conversation_rows,
    remember_fact,
)
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.state.projections import (
    CLARIFICATION_EVENT_TYPES,
    PendingClarification,
    PendingConfirmations,
    PendingConfirmationSlot,
)
from jarvis.state.push_tokens import register_body
from jarvis.state.turn_facts import (
    clarification_asked_at,
    heard_as_barge_pause,
    recent_turn_timings,
)
from jarvis.surface import (
    respeaker_board,
    voice_audio,
    voice_controls,
    voice_ducking,
    voice_live,
    voice_media,
    voice_pipeline,
    voice_session,
)
from jarvis.surface.claude_sessions import ClaudeSessions
from jarvis.surface.cli import emit_surface_user_intent
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
from jarvis.surface.playback_recovery import reconcile_open_playback
from jarvis.surface.terminal_events import BrainEvents, phone_turn_id
from jarvis.surface.usage_observer import (
    latest_usage,
    redeem_codex_reset,
    usage_history,
)

LOGGER = logging.getLogger("jarvis.runtime.inherent_loop")

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


# ADR 0144: how long an ask card from a barge-pause fragment stays off the screen, so a
# continuation that folds the fragment in (ADR 0074) arrives before it ever shows.
_FRAGMENT_CARD_HOLD_MS: Final[int] = 1_500


def confirmation_card(slot: PendingConfirmationSlot, device: str | None) -> dict[str, Any]:
    """The waiting confirmation as the phone and the Mac read it (ADR 0062, 0321).

    A card of several calls keeps this shape: its question text is the calls' lines one to a
    line, and it has no single tool or arguments to show.
    """
    first = slot.calls[0]
    tool = str(first.get("tool_name", ""))
    args_raw = first.get("args_meta")
    args = dict(args_raw) if isinstance(args_raw, Mapping) else {}
    parts = tool.split("__")
    several = len(slot.calls) > 1
    return {
        "id": slot.confirmation_id,
        "tool": tool,
        "action": slot.template_line if several else lang.action(tool)[0],
        "source": parts[1] if len(parts) == 3 and parts[0] == "mcp" else "",  # noqa: PLR2004
        "letter": not several and lang.letter_to(args) is not None,
        "args": {} if several else args,
        "device": device,
    }


_CARD_EVENT_TYPES: Final[tuple[str, ...]] = (
    "confirmation.requested", "confirmation.accepted", "confirmation.rejected", "gate.evaluated",
)
"""What the pending card folds from (ADR 0062), the rows the answer path itself re-reads."""


def _visible_ask_card(conn: sqlite3.Connection, now_ms: int) -> PendingClarification | None:
    """ADR 0066: the ask card to show, or ``None``.

    ADR 0144: a card its turn put up from a barge-pause fragment stays hidden for
    ``_FRAGMENT_CARD_HOLD_MS``. ADR 0206: a continuation no longer closes it, so the hold
    only delays showing it.
    """
    slot = PendingClarification.from_events(iter_events_of_types(conn, CLARIFICATION_EVENT_TYPES))
    if slot is None or not slot.waiting:
        return None
    asked_at = clarification_asked_at(conn, slot.clarification_id)
    if (
        asked_at is not None
        and now_ms - asked_at < _FRAGMENT_CARD_HOLD_MS
        and heard_as_barge_pause(conn, slot.asked_turn_id)
    ):
        return None
    return slot


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
        timings = recent_turn_timings(conn, since_ms=since_ms, limit=limit)
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
    turns = [
        {"event_uid": event.event_uid, "ts_epoch_ms": event.ts_epoch_ms, **event.payload}
        for event in timings
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

        images_ok = _model_takes_images(runtime.config.raw)
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
        voice_knobs = _voice_knobs(runtime.config.raw)
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
            runtime.terminal_hub.listening = build_brain_listening(
                runtime, runtime.terminal_hub,
                controls=controls, set_conversation=_set_conversation, set_quiet=_set_quiet,
            )
        sensevoice_dir = runtime.config.models.sensevoice_dir
        silero_path = runtime.config.models.silero_vad_path
        model_fetch = models.Progress()
        brain = runtime.config.role == "brain"
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
                    voiceprint_model=runtime.config.models.voiceprint_model,
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
                    voice_input_owners = spawn_voice_input_owners(
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
        realtime_raw = runtime.config.raw.get("realtime")
        realtime_map = realtime_raw if isinstance(realtime_raw, Mapping) else {}
        gpt_live_raw = realtime_map.get("gpt_live")
        gpt_live_config: voice_live.GptLiveConfig | None = None
        if isinstance(gpt_live_raw, Mapping):
            try:
                gpt_live_config = voice_live.gpt_live_config_from_mapping(
                    {"assistant": runtime.config.assistant_name, **gpt_live_raw},
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
                session=runtime.config.session,
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
            since = runtime.config.session.history_since
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
            return {"card": confirmation_card(slot, device)}

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
                shown = live_card(slot.trip, runtime.clock.now())
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
            knobs = _voice_knobs(runtime.config.raw)
            setup = Setup(
                root=runtime.runtime_paths.root,
                settings_path=runtime.runtime_paths.settings,
                memory_path=window_memory.db_path,
                config=runtime.config.raw,
                tts_endpoint=knobs.tts_primary_endpoint,
                tts_model=knobs.tts_model,
                restart=_restart_soon if supervised() else None,
                voice_models=model_fetch,
            )

        # ADR 0058: dictation hears through the live mic and the voice path's ears.
        ingress = duplex_voice_session.ingress if duplex_voice_session is not None else None
        if ingress is not None and voice_pipe is not None:
            polisher = local_polisher(
                runtime.config.raw, runtime.runtime_paths.event_log,
                load_pricing_table(runtime.config.pricing_path),
            )
            if polisher is not None:
                dictation = build_dictation(
                    config=runtime.config.raw, ingress=ingress, silero_path=silero_path,
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
        spend = SpendCapSettings.from_config(runtime.config.raw.get("spend_cap"))
        built_phone = (
            build_phone_hub(runtime, voice_knobs, set_quiet=_set_quiet)
            if runtime.config.listen_addresses
            else None
        )
        phone_hub = None if built_phone is None else built_phone[0]
        if phone_hub is not None and runtime.locator is not None:
            # ADR 0281: a phone with a live conversation socket is asked for a fix over it.
            runtime.locator.ask_socket = phone_hub.ask_location
        if phone_hub is not None and runtime.whereabouts is not None:
            # ADR 0217: a device with its conversation socket open is a phone, and that is said.
            runtime.whereabouts.phone_open = phone_hub.connected
        local_zone = runtime.config.zone
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
                runtime.config.zone,
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
                runtime.config.zone,
                None if runtime.memory is None else runtime.memory.db_path,
                runtime.config.observer.timesink_db_path,
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
                    section_caps=section_caps(runtime.config.raw.get("core_memory")),
                    history_since=runtime.config.session.history_since,
                    event_log=runtime.runtime_paths.event_log,
                    zone=runtime.config.zone,
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
                    if runtime.config.listen_addresses
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
            terminals=runtime.terminal_hub if runtime.config.listen_addresses else None,
            phone_events=runtime.phone_events if runtime.config.listen_addresses else None,
            lifelog=(
                lifelog_archive := _lifelog(runtime) if runtime.config.listen_addresses else None
            ),
            lifelog_coverage=(
                None if lifelog_archive is None else _lifelog_coverage(runtime, lifelog_archive)
            ),
            imports_intake=_shipments(runtime) if runtime.config.listen_addresses else None,
            phone=phone_hub,
            push_register=(
                functools.partial(register_body, runtime.runtime_paths.root)
                if runtime.config.listen_addresses
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
                if runtime.config.listen_addresses
                else None
            ),
            # ADR 0196: routes on every daemon, but only a brain that listens can mint a code.
            pairing=DevicePairing(
                root=runtime.runtime_paths.root,
                codes=PairingCodes(runtime.runtime_paths.root),
                listens=bool(runtime.config.listen_addresses),
                brain_urls=brain_urls(
                    runtime.config.listen_addresses, runtime.config.listen_hosts, port,
                ),
            ),
            v2=InherentV2Deps(
                token_matches=functools.partial(inherent_v2_token_matches, v2_token),
                device_token_matches=(
                    functools.partial(device_token_matches, runtime.runtime_paths.root)
                    if runtime.config.listen_addresses
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
            extra_hosts=runtime.config.listen_hosts,
            device_token_matches=(
                functools.partial(device_token_matches, runtime.runtime_paths.root)
                if runtime.config.listen_addresses
                else None
            ),
            open_claim=bool(runtime.config.listen_addresses),
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
                queue_capacity=runtime.config.realtime.input_queue_capacity,
                max_concurrent_turns=runtime.config.realtime.input_max_concurrent_turns,
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
                    commentary_watcher(runtime, poll_interval_s=poll_interval_s),
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
                settings=runtime.config.session,
                llm_config=(
                    runtime.config.raw["llm"]
                    if isinstance(runtime.config.raw.get("llm"), Mapping)
                    else {}
                ),
                event_log_path=runtime.runtime_paths.event_log,
                pricing_table=load_pricing_table(runtime.config.pricing_path),
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
            job_mail.say = functools.partial(say_job_line, runtime)
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
            reminders.say = functools.partial(say_reminder, runtime)
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
                claude=ClaudeSessions() if runtime.config.observer.claude_sessions else None,
                codex=codex_board,
            )
            watchers.append(asyncio.create_task(runtime.night.run(), name="night_run"))
        if runtime.daily_schedule is not None:
            watchers.append(
                asyncio.create_task(runtime.daily_schedule.run(), name="daily_report_schedule"),
            )
        day_summary = DaySummarySettings.from_config(runtime.config.raw.get("day_summary"))
        if day_summary is not None and runtime.memory is not None:
            # ADR 0142: past days' conversation summaries, written once a day.
            llm_config = runtime.config.raw.get("llm")
            watchers.append(
                asyncio.create_task(
                    DaySummarySchedule(
                        memory=runtime.memory,
                        settings=day_summary,
                        llm_config=llm_config if isinstance(llm_config, Mapping) else {},
                        event_log_path=runtime.runtime_paths.event_log,
                        pricing_table=load_pricing_table(runtime.config.pricing_path),
                        clock=runtime.clock,
                        core_memory=CoreMemorySettings.from_config(
                            runtime.config.raw.get("core_memory"),
                        ),
                        jev_log=_jev_log(runtime.config.raw, runtime.runtime_paths.root),
                        ledger=runtime.ledger,
                    ).run(),
                    name="day_summary_schedule",
                ),
            )
        if runtime.doing is not None:
            # ADR 0302, 0310: what he was doing, written for the days under way and just past.
            watchers.append(asyncio.create_task(runtime.doing.run(), name="doing_schedule"))
        day_experience =DayExperienceSettings.from_config(runtime.config.raw.get("day_experience"))
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
        day_plan = DayPlanSettings.from_config(runtime.config.raw.get("day_plan"))
        if day_plan is not None and runtime.memory is not None:
            # ADR 0288: the day's plan, drafted at `day_plan.at` and on a start with none for today.
            plan_zone = runtime.config.zone
            home = runtime.home
            plan_sources = DayPlanSources(
                event_log_path=runtime.runtime_paths.event_log,
                memory_db=runtime.memory.db_path,
                zone=plan_zone,
                day_sources=DaySources(
                    plan_zone, runtime.config.observer.timesink_db_path,
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
                    runtime.clock,
                )
            )
            watchers.append(
                asyncio.create_task(
                    build_schedule(
                        day_plan,
                        config=runtime.config.raw,
                        pricing_path=runtime.config.pricing_path,
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
                            config=runtime.config.raw,
                            pricing_path=runtime.config.pricing_path,
                            sources=plan_sources,
                            open_leg=open_leg,
                            hooks=plan_hooks(runtime, controls),
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
                runtime.config.raw, runtime.settings, power_coordinator,
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
            sockets=_listen_sockets(host, port, runtime.config.listen_addresses)
            if runtime.config.listen_addresses
            else None,
        )


__all__ = ["serve_inherent"]
