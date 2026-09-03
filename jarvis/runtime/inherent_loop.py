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
import functools
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import uvicorn

if TYPE_CHECKING:
    from collections.abc import Callable

    from jarvis.deployment.sleep_wake import PowerObserver
    from jarvis.execution.action_runner import ActionRunner
    from jarvis.state.committed_event_bus import CommittedEventBus

from jarvis.decision.response_run import (
    ResponseCancelledError,
    ResponseTerminalizer,
    reconcile_open_responses,
)
from jarvis.deployment.process_lock import acquire_exclusive
from jarvis.deployment.sleep_wake import install_power_observer, sweep_overdue_actions
from jarvis.execution.tools import live_action_ids
from jarvis.runtime import (
    JarvisRuntime,
    _event_action_id,
    _new_turn_id,
    _observer_poll_interval_s,
    _observer_repo_paths,
    _positive_float,
    drive_turn,
    make_response_cancel_callable,
)
from jarvis.shared import Event
from jarvis.shared.realtime_trace import record_realtime_trace
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.surface import (
    voice_asr,
    voice_audio,
    voice_backend,
    voice_ducking,
    voice_media,
    voice_pipeline,
    voice_session,
    voice_tts,
    voice_wake,
)
from jarvis.surface.cli import emit_surface_user_intent
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app
from jarvis.surface.repo_observer import RepoObserver

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
_TTS_SILENT_CHANNELS: frozenset[str] = frozenset({"queue_review", "silent_log"})

# WS-broadcaster suppression set — deliberately NARROWER than the TTS
# set, and this asymmetry is load-bearing:
#
# - ``silent_log`` maps to ``()`` in ``ATTENTION_CHANNEL_TO_SURFACES``:
#   no physical surface at all. Dropping its envelopes is exactly what
#   aligns the wire with the routing table.
# - ``queue_review`` maps to ``("cli_stdout",)`` — it is a TEXT channel,
#   and it is ``attention_policy``'s DEFAULT verdict for an ordinary
#   user utterance (jarvis/decision/gates.py: the final ``return``).
#   The daemon passes ``available_surfaces=frozenset()``, so this WS is
#   the substitute for that ``cli_stdout``. Suppressing it would blank
#   the Inherent text surface for nearly every turn and hang ADR-0009
#   D2's forwarding CLI until its 120s timeout (exit 4).
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


# Default on-disk locations for the voice ASR / VAD model artifacts.
# ADR-0005 §12 (pre-flight) — ``serve_inherent`` checks these BEFORE
# spawning the WakeListener so a missing wheel surfaces as a single
# log line instead of a crashed daemon thread on first wake. Tests pin
# their own paths via :func:`_voice_models_preflight` kwargs.
_DEFAULT_SENSEVOICE_DIR = Path("data/sensevoice-small-int8")
_DEFAULT_SILERO_PATH = Path("data/silero_vad.onnx")

# Default voice ASR / capture knobs (ADR-0005 §5.1).
_DEFAULT_WAKE_THRESHOLD: float = 0.5
_DEFAULT_CAPTURE_MAX_DURATION_S: float = 5.0
_DEFAULT_CAPTURE_MIN_VOICED_S: float = 1.0
# macOS built-in default rate; MiniMax 32 kHz is resampled to 48 kHz via soxr
# (see ``_build_tts_pipeline``) so the OutputStream runs at the device-native
# rate and CoreAudio does not force a hardware-rate switch on every play.
_DEFAULT_TTS_SAMPLE_RATE_HZ: int = 48000

# Wake input stream params — ADR §5.1 (openwakeword expects 16 kHz mono PCM16
# at 1280-sample / 80 ms blocks). A SEPARATE stream from the recorder's per
# legacy ``core/inherent_wake_listener.py`` parity (the recorder's 32-ms VAD
# chunks would force openwakeword to buffer across reads).
_WAKE_SAMPLE_RATE_HZ: int = 16000
_WAKE_FRAME_SAMPLES: int = 1280
_WAKE_JOIN_TIMEOUT_S: float = 2.0


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


def _drop_for_silent_channel(
    event: Event,
    *,
    turn_id: str,
    silent_turns: set[str],
    silent_channels: frozenset[str],
    consumer: str,
) -> bool:
    """True when ``event`` belongs to a turn ``consumer`` must not deliver.

    ADR-0009 D4. Shared by :func:`_response_watcher` and
    :func:`_tts_watcher`, which differ only in their ``silent_channels``
    set (see the two constants' comments for why the sets differ).

    The L3 ``attention_channel`` verdict rides the ``surface.response_open``
    header only (ADR-0009 §4), so a suppressed turn id is remembered
    across its chunks and forgotten when its ``surface.response_emitted``
    row arrives — the whole open/chunk*/emitted triple is dropped or
    none of it is.

    A missing (or non-string) ``attention_channel`` is deliberately NOT
    silent: the pre-Step-8 behaviour — deliver and speak — stays the
    default, so an emitter that predates the field (legacy rows, direct
    ``emit_event`` callers) never loses its output. Suppression is
    opt-in by an explicit channel label.
    """
    if event.type == "surface.response_open":
        channel = event.payload.get("attention_channel")
        if not (isinstance(channel, str) and channel in silent_channels):
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
    if event.type == "surface.response_emitted":
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


def _reconcile_action_quarantine_in_thread(
    action_runner: ActionRunner,
    event_log_path: Path,
) -> tuple[str, ...]:
    """Re-establish repository quarantine at boot (ADR-0008 D9 / F23).

    Runs on an ``asyncio.to_thread`` worker with its OWN connection, for the
    same ``check_same_thread`` reason as the response reconciler above.
    Returns the action ids whose leases were re-taken.
    """
    conn = open_event_log(event_log_path)
    try:
        return action_runner.reconcile_quarantine(conn)
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()


def _drive_turn_in_worker_thread(
    runtime: JarvisRuntime,
    *,
    user_intent_event: Event,
) -> None:
    """Open a fresh SQLite connection and call :func:`drive_turn` on this thread.

    The watcher coroutine dispatches this function via
    :func:`asyncio.to_thread`. Because
    :func:`jarvis.state.event_log.open_event_log` opens connections with
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
    worker_conn = open_event_log(runtime.runtime_paths.event_log)
    try:
        worker_runtime = dataclasses.replace(runtime, conn=worker_conn)
        drive_turn(
            worker_runtime,
            user_intent_event=user_intent_event,
            available_surfaces=frozenset(),
            streaming_enabled=True,
        )
    finally:
        with contextlib.suppress(sqlite3.Error):
            worker_conn.close()


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

    Per-event dispatch is wrapped in a catch-all: a misbehaving TTS pipeline
    (e.g. MiniMax WebSocket drop, ``say`` subprocess error) must NOT crash
    the watcher because that would stall every subsequent turn. We log a
    warning and continue; the surface broadcaster keeps running on the
    parallel cursor, so the UI is unaffected.

    Channel filter (ADR-0009 D4 — "System turns are silent, enforced"):
    a turn whose ``surface.response_open`` header declares a channel in
    :data:`_TTS_SILENT_CHANNELS` never reaches the pipeline at all —
    ``begin_turn`` is not called, its chunks are dropped, and its
    ``emitted`` only clears the bookkeeping. The channel is known ONLY
    from the open header, so the suppressed turn ids are remembered
    until their ``emitted`` row closes them. This is a filter, not a
    switch: a ``voice_notify`` turn streams exactly as before.

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
    artifacts_dir = runtime.runtime_paths.artifacts_root / "voice_artifacts"
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
    api_key = os.environ.get("MINIMAX_API_KEY")
    if not api_key:
        LOGGER.warning(
            "MINIMAX_API_KEY unset; skipping TTS subsystem (text path only).",
        )
        return None
    # OutputStream runs at the macOS native rate (48 kHz). MiniMax is
    # asked for 32 kHz PCM in (highest it natively produces in our
    # config) and resampled to 48 kHz on the way out via soxr — running
    # the device at the system-native rate prevents CoreAudio from
    # forcing a hardware-rate switch on every play, which was producing
    # audible pops/clicks for any other app sharing the speaker.
    def _new_provider() -> voice_tts.MiniMaxWSClient:
        return voice_tts.MiniMaxWSClient(
            api_key=api_key,
            sample_rate_in=32000,
            sample_rate_out=_DEFAULT_TTS_SAMPLE_RATE_HZ,
        )

    provider = _new_provider()
    realtime_raw = runtime.config.get("realtime")
    realtime = realtime_raw if isinstance(realtime_raw, Mapping) else {}
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
    streaming_capable = (
        runtime.wave1_features.transactional_event_append
        and runtime.wave1_features.lifecycle_terminal_cas
        and provider.streaming_candidate_count > 0
        and media_config is not None
        and media_config.canonical_sample_rate_hz == _DEFAULT_TTS_SAMPLE_RATE_HZ
    )
    if streaming_requested and streaming_capable:
        player = voice_tts.AudioStreamPlayer(
            sample_rate_hz=_DEFAULT_TTS_SAMPLE_RATE_HZ,
            ring_seconds=2.0,
            lazy_open=True,
            generation_safe=True,
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
            sample_rate_hz=_DEFAULT_TTS_SAMPLE_RATE_HZ,
            # 30 s of headroom so the full-buffer write() of a long response
            # (typical 5-30 s of f32 PCM at 48 kHz) lands in one shot — the
            # 2 s default forces write() to block on the drain and hit its
            # 10 s timeout, dropping the tail of any response > ~10 s.
            ring_seconds=30.0,
            lazy_open=False,
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


def _spawn_wake_listener(
    *,
    pipeline: voice_pipeline.VoicePipeline,
    broadcaster: InherentBroadcaster,
    silero_path: Path,
    tts: voice_tts.TTSPipeline | voice_media.StreamingTTSPipeline | None,
    ducker: voice_ducking.SystemAudioDucker | None = None,
) -> tuple[voice_wake.WakeListener, Any | None] | None:
    """Construct + start a :class:`WakeListener` daemon thread.

    ADR-0005 §5.1 — the listener owns its own SileroVad (record mode)
    and a partial-bound :func:`voice_audio.capture_utterance`; both
    are constructed here so the L5 modules stay free of L6 wiring.

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
        silero_vad = voice_audio.SileroVad(mode="record", model_path=silero_path)
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
            threshold=_DEFAULT_WAKE_THRESHOLD,
            is_speaking_callable=(tts.is_speaking if tts is not None else None),
            frame_factory=_read_wake_frame,
            ducker=ducker,
        )
        listener.start()
    except Exception:
        LOGGER.exception(
            "wake: legacy listener construction/start failed; preserving PTT.",
        )
        if listener is not None:
            _shutdown_wake(listener, stream)
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


def _spawn_single_ingress_session(  # noqa: C901, PLR0911, PLR0915 - each pre/post-device downgrade has distinct ownership semantics
    *,
    runtime: JarvisRuntime,
    pipeline: voice_pipeline.VoicePipeline,
    broadcaster: InherentBroadcaster,
    silero_path: Path,
    tts: voice_tts.TTSPipeline | voice_media.StreamingTTSPipeline | None,
) -> tuple[voice_session.DuplexVoiceSession | None, bool]:
    """Start Wave 3 or return whether a device-open attempt was made.

    ``attempted=True`` forbids legacy wake fallback for this boot even when
    startup failed: a timed-out foreign PortAudio open may still own the
    default microphone.  ``attempted=False`` means no input owner was touched,
    so a failed prerequisite/config validation may explicitly use legacy wake.
    """
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
        vad = voice_audio.SileroVad(mode="record", model_path=silero_path)
        session = voice_session.DuplexVoiceSession(
            ingress=ingress,
            wake_engine=engine,
            vad=vad,
            pipeline=pipeline,
            broadcaster=broadcaster,
            output_active=(tts.is_output_active if tts is not None else None),
            wake_threshold=_DEFAULT_WAKE_THRESHOLD,
            config=session_config,
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


def _spawn_voice_input_owners(  # noqa: PLR0913 - composition boundary dependencies
    *,
    runtime: JarvisRuntime,
    pipeline: voice_pipeline.VoicePipeline,
    broadcaster: InherentBroadcaster,
    silero_path: Path,
    tts: voice_tts.TTSPipeline | voice_media.StreamingTTSPipeline | None,
    ducker: voice_ducking.SystemAudioDucker,
) -> _VoiceInputOwners:
    """Select Wave 3 or legacy wake without ever opening both input owners."""
    duplex_session, attempted = _spawn_single_ingress_session(
        runtime=runtime,
        pipeline=pipeline,
        broadcaster=broadcaster,
        silero_path=silero_path,
        tts=tts,
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
        )
        if legacy is not None:
            wake_listener, wake_stream = legacy
    return _VoiceInputOwners(
        duplex_session=duplex_session,
        wake_listener=wake_listener,
        wake_stream=wake_stream,
        single_ingress_attempted=attempted,
    )


# --- ADR-0009 D4: supervisor sweep control plane ---------------------------

# Terminal-failure event types an orphan closure lands on. Both route
# into L3's ``_handle_action_terminal_failure`` branch — the one that
# already produces the Limitation claim + limitation utterance, which is
# why the sweep stays an emitter rather than a second brain.
#
# The happy-path re-entries (``worker.reported`` /
# ``action.result_observed``) are deliberately absent: those are only
# ever written from inside a live driver mid-turn, so a system turn on
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
    ``_handle_action_terminal_failure`` branch folds the Limitation claim
    and the ADR-0002 amendment routes it to ``queue_review``. The sweep
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
            for row_id, ev in new_events:
                after_id = max(after_id, row_id)
                action_id = _event_action_id(ev)
                if action_id is None or action_id in live_action_ids():
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
) -> None:
    """Background task: run the supervisor sweep every ``interval_s`` seconds.

    Sleeps FIRST — the one-shot bootstrap sweep in
    :func:`_start_sweep_control_plane` already covered t=0, and a second
    pass one tick later would be pure noise.
    """
    LOGGER.info("supervisor_sweep started (interval=%.1fs)", interval_s)
    try:
        while True:
            await asyncio.sleep(interval_s)
            _run_supervisor_sweep(runtime, default_budget_s=default_budget_s)
    except asyncio.CancelledError:
        LOGGER.info("supervisor_sweep cancelled")
        raise


async def _start_sweep_control_plane(
    runtime: JarvisRuntime,
    *,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
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


def _shutdown_wake(
    wake_listener: voice_wake.WakeListener | None,
    wake_stream: Any | None,  # noqa: ANN401 — sounddevice stream is untyped third-party API
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
    wake_listener.join(timeout_s=_WAKE_JOIN_TIMEOUT_S)
    if wake_listener.is_alive():
        LOGGER.warning(
            "wake listener thread did not exit within %.1f s; "
            "proceeding with stream close (segfault risk reduced "
            "but not eliminated)",
            _WAKE_JOIN_TIMEOUT_S,
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
) -> None:
    """Select the exact Wave-3 or legacy shutdown order used by serve."""
    if owners.duplex_session is not None:
        _shutdown_duplex_voice_session(owners.duplex_session)
        _request_tts_close(tts_pipe)
        return
    _request_tts_close(tts_pipe)
    _shutdown_wake(owners.wake_listener, owners.wake_stream)


async def serve_inherent(  # noqa: C901, PLR0912, PLR0913, PLR0915 — composition-root entrypoint; the keyword args ARE the daemon contract and the voice-wiring plus boot-reconciliation branches necessarily inflate body length + branch count.
    runtime: JarvisRuntime,
    *,
    host: str = "127.0.0.1",
    port: int = _DEFAULT_PORT,
    lock_path: Path,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    sensevoice_dir: Path = _DEFAULT_SENSEVOICE_DIR,
    silero_path: Path = _DEFAULT_SILERO_PATH,
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
        sensevoice_dir: SenseVoice INT8 model directory (pre-flight).
        silero_path: Silero VAD ONNX path (pre-flight).

    Raises:
        jarvis.deployment.process_lock.ProcessLockHeld: Another daemon
            already holds the lock for this runtime root.
    """
    with acquire_exclusive(lock_path):
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
        voice_input_owners = _VoiceInputOwners(
            duplex_session=None,
            wake_listener=None,
            wake_stream=None,
            single_ingress_attempted=False,
        )
        voice_pipeline_callable: Any | None = None
        # ADR-0005 §5.1 / §5.3: ONE shared SystemAudioDucker arbitrates
        # wake-capture muting against TTS provider/playback output leases.
        shared_ducker: voice_ducking.SystemAudioDucker = (
            voice_ducking.SystemAudioDucker()
        )

        models_ok, missing = _voice_models_preflight(
            sensevoice_dir=sensevoice_dir,
            silero_path=silero_path,
        )
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
                )
                if os.environ.get("JARVIS_VOICE_DISABLE_WAKE") == "1":
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
                    )
                    duplex_voice_session = voice_input_owners.duplex_session
            except Exception:
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
        if runtime.action_runner is not None:
            quarantined = await asyncio.to_thread(
                _reconcile_action_quarantine_in_thread,
                runtime.action_runner,
                runtime.runtime_paths.event_log,
            )
            if quarantined:
                LOGGER.warning(
                    "boot reconciliation re-quarantined %d repository lease(s) "
                    "for action(s) that terminated without cleanup: %s",
                    len(quarantined),
                    ", ".join(quarantined),
                )

        deps = InherentDeps(
            submit_callable=submit_callable,
            broadcaster=broadcaster,
            voice_pipeline_callable=voice_pipeline_callable,
            cancel_response_callable=(
                make_response_cancel_callable(runtime)
                if runtime.response_flags.independent_response_cancel
                else None
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

        watchers: list[asyncio.Task[None]] = [
            asyncio.create_task(
                _user_intent_watcher(runtime, poll_interval_s=poll_interval_s),
                name="user_intent_watcher",
            ),
            asyncio.create_task(
                _response_watcher(runtime, broadcaster, poll_interval_s=poll_interval_s),
                name="response_watcher",
            ),
        ]
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

        # ADR-0009 D4 — the sweep control plane: anchor the system-trigger
        # watcher, THEN run the bootstrap sweep, THEN start the periodic
        # task. The ordering is load-bearing and lives inside the helper.
        watchers.extend(
            await _start_sweep_control_plane(runtime, poll_interval_s=poll_interval_s),
        )

        # ADR-0009 D5 — repo observer: baselines recovered from the log
        # first, then one more watchers-list task polling `observer.repos`
        # every `observer.poll_interval_s`. Nothing it emits is a trigger,
        # so it is wired after the sweep control plane without disturbing
        # that anchor-then-bootstrap ordering.
        watchers.extend(_start_repo_observer(runtime))

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
            _request_voice_input_branch_shutdown(voice_input_owners, tts_pipe)
            # Cancel/await watcher ownership before PortAudio teardown. A
            # provider thread may still exist, but the closed generation owns
            # no right to write or invoke fallback.
            for w in watchers:
                w.cancel()
            await asyncio.gather(*watchers, return_exceptions=True)
            _shutdown_tts(tts_pipe)
            # Force-restore output volume in case a duck escaped a finally
            # block on the way down (best-effort; idempotent if depth == 0).
            try:
                shared_ducker.restore_all()
            except Exception:  # noqa: BLE001 — shutdown errors must not mask uvicorn return
                LOGGER.debug("shared_ducker.restore_all failed", exc_info=True)
            LOGGER.info("serve_inherent: shutdown complete")


__all__ = ["serve_inherent"]
