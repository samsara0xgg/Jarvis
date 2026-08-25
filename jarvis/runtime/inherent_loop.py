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
import time
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import uvicorn

if TYPE_CHECKING:
    from collections.abc import Callable

    from jarvis.deployment.sleep_wake import PowerObserver

from jarvis.deployment.process_lock import acquire_exclusive
from jarvis.deployment.sleep_wake import install_power_observer, sweep_overdue_actions
from jarvis.execution.tools import live_action_ids
from jarvis.runtime import JarvisRuntime, _event_action_id, _new_turn_id, drive_turn
from jarvis.shared import Event
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.surface import (
    voice_asr,
    voice_audio,
    voice_ducking,
    voice_pipeline,
    voice_tts,
    voice_wake,
)
from jarvis.surface.cli import emit_surface_user_intent
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app

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


async def _tts_watcher(
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
    contract the watcher actually depends on.

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
    after_id = _latest_id(conn)
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
                after_id = max(after_id, row_id)
                try:
                    turn_id = str(ev.payload.get("turn_id", ""))
                    if _drop_for_silent_channel(
                        ev,
                        turn_id=turn_id,
                        silent_turns=silent_turns,
                        silent_channels=_TTS_SILENT_CHANNELS,
                        consumer="tts_watcher",
                    ):
                        continue
                    if ev.type == "surface.response_open":
                        gate_mode = ev.payload.get("required_gate_mode", "sentence")
                        await asyncio.to_thread(
                            pipeline.begin_turn,  # type: ignore[attr-defined]
                            turn_id,
                            gate_mode=gate_mode,
                        )
                    elif ev.type == "surface.response_chunk":
                        text = str(ev.payload.get("text", ""))
                        await asyncio.to_thread(
                            pipeline.handle_chunk,  # type: ignore[attr-defined]
                            turn_id,
                            text,
                        )
                    elif ev.type == "surface.response_emitted":
                        await asyncio.to_thread(
                            pipeline.handle_emitted,  # type: ignore[attr-defined]
                            turn_id,
                        )
                except Exception as exc:  # noqa: BLE001 — log + continue; TTS must not crash watcher.
                    LOGGER.warning(
                        "tts_watcher: dispatch raised on %s turn_id=%s: %r",
                        ev.type,
                        ev.payload.get("turn_id"),
                        exc,
                    )
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


def _build_tts_pipeline(
    broadcaster: InherentBroadcaster,
    *,
    ducker: voice_ducking.SystemAudioDucker | None = None,
) -> voice_tts.TTSPipeline | None:
    """Build the TTS subsystem when ``MINIMAX_API_KEY`` is present.

    ADR-0005 §5.3 — the env var is the sole credential source for the
    MiniMax WebSocket. Without it we skip the entire TTS pipeline
    (instead of falling through to ``macos_say_fallback`` only): the
    fallback is a per-call escape hatch from inside
    :class:`TTSPipeline`, not a standalone path, so wiring it on its
    own would lie about what the daemon can actually do.

    The optional ``ducker`` is the same :class:`SystemAudioDucker`
    instance shared with the WakeListener — refcounted nesting means
    wake-capture + TTS-playback can both demand mute simultaneously
    without stomping each other's restore.
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
    provider = voice_tts.MiniMaxWSClient(
        api_key=api_key,
        sample_rate_in=32000,
        sample_rate_out=_DEFAULT_TTS_SAMPLE_RATE_HZ,
    )
    # lazy_open=False so the PortAudio OutputStream is up before the first
    # MiniMax chunk lands; otherwise `write()` would fill the ring and
    # never drain, leaving `is_speaking()` permanently True and starving
    # the wake listener.
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
    tts: voice_tts.TTSPipeline | None,
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

    engine = voice_wake.WakeEngine(model_name="hey_jarvis_v0.1")
    # Without start(), the underlying openwakeword Model is never loaded:
    # predict() silently returns {} and the listener's threshold check is
    # always 0.0 — wake never fires. ADR §F1: a failure here downgrades to
    # text-only (no audio device / wheel missing in CI).
    try:
        engine.start()
    except Exception:
        LOGGER.exception(
            "wake: WakeEngine.start() failed; skipping wake listener.",
        )
        try:
            stream.close()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            LOGGER.debug("wake: stream close after engine start failure failed", exc_info=True)
        return None
    silero_vad = voice_audio.SileroVad(mode="record", model_path=silero_path)
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
    return listener, stream


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


def _positive_float(value: object, fallback: float) -> float:
    """Coerce a YAML scalar to a positive float; ``fallback`` on anything else.

    ``bool`` is excluded explicitly because it is an ``int`` subclass —
    ``sweep_interval_s: true`` would otherwise become a 1-second sweep.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return fallback
    return float(value) if value > 0 else fallback


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


def _install_power_observer_or_degrade(
    conn: sqlite3.Connection,
    loop: asyncio.AbstractEventLoop,
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
        return install_power_observer(conn, loop=loop)
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


def _shutdown_tts(tts_pipe: voice_tts.TTSPipeline | None) -> None:
    """Stop the TTS pipeline's audio player and release the PortAudio device.

    Idempotent. The AudioStreamPlayer is constructed with lazy_open=False,
    so its OutputStream + PortAudio callback thread are live as soon as
    _build_tts_pipeline runs. Without this teardown the device handle
    leaks past daemon exit, blocking clean re-launch and matching the
    historical bare-pytest segfault pattern.
    """
    if tts_pipe is None:
        return
    try:
        tts_pipe.close()
    except Exception:  # noqa: BLE001 — shutdown errors must not mask uvicorn return
        LOGGER.debug("tts pipeline close failed", exc_info=True)


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
            wake_stream.close()
        except Exception:  # noqa: BLE001 — shutdown errors must not mask uvicorn return
            LOGGER.debug("wake_stream close failed", exc_info=True)


async def serve_inherent(  # noqa: PLR0913, PLR0915 — composition-root entrypoint; the keyword args ARE the daemon contract and the voice-wiring branch necessarily inflates body length + branch count.
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

        def submit_callable(text: str) -> None:
            """Bound at daemon-start time. Mints a fresh ``turn_id`` per call.

            Runs on whatever thread :func:`asyncio.to_thread` dispatches
            it onto (see ``/inherent/submit`` handler in Step 7). The
            handler thread is NOT the event-loop thread, so to honor
            ``check_same_thread`` we open a fresh ``sqlite3.Connection``
            to the same DB file for the SQLite write.
            """
            inner_conn = open_event_log(runtime.runtime_paths.event_log)
            try:
                emit_surface_user_intent(
                    inner_conn,
                    transcript=text,
                    turn_id=_new_turn_id(),
                )
            finally:
                with contextlib.suppress(sqlite3.Error):
                    inner_conn.close()

        # ADR-0005 §12 pre-flight + voice subsystem wiring. Any failure
        # downgrades the daemon to text-only — text path must stay
        # healthy when models / SDKs / mics are missing (CI default).
        voice_pipe: voice_pipeline.VoicePipeline | None = None
        tts_pipe: voice_tts.TTSPipeline | None = None
        wake_listener: voice_wake.WakeListener | None = None
        wake_stream: Any | None = None
        voice_pipeline_callable: Any | None = None
        # ADR-0005 §5.1 / §5.3: ONE shared SystemAudioDucker between
        # wake-capture and TTS-playback so the refcount nests correctly.
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
                tts_pipe = _build_tts_pipeline(broadcaster, ducker=shared_ducker)
                if os.environ.get("JARVIS_VOICE_DISABLE_WAKE") == "1":
                    LOGGER.info(
                        "JARVIS_VOICE_DISABLE_WAKE=1; skipping WakeListener spawn.",
                    )
                else:
                    spawn_result = _spawn_wake_listener(
                        pipeline=voice_pipe,
                        broadcaster=broadcaster,
                        silero_path=silero_path,
                        tts=tts_pipe,
                        ducker=shared_ducker,
                    )
                    if spawn_result is not None:
                        wake_listener, wake_stream = spawn_result
            except Exception:
                LOGGER.exception(
                    "voice subsystem construction failed; running text-only.",
                )
                voice_pipe = None
                voice_pipeline_callable = None
                tts_pipe = None
                wake_listener = None
                wake_stream = None

        deps = InherentDeps(
            submit_callable=submit_callable,
            broadcaster=broadcaster,
            voice_pipeline_callable=voice_pipeline_callable,
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

        # ADR-0009 D3 — power observer, installed after the lock (which
        # stays the outermost scope) and immediately before the try/finally
        # that owns the serve lifetime, so its registered CFRunLoop thread
        # is bracketed by the same `finally` that tears the watchers down;
        # no path between install and try can leak a live registration.
        power_observer = _install_power_observer_or_degrade(
            runtime.conn, asyncio.get_running_loop(),
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
            # Shutdown order is load-bearing: wake first (releases the mic so
            # any ducker bracket the listener held is unwound), TTS second
            # (releases the speaker / PortAudio output stream), ducker last
            # (force-restore in case a duck escaped on the way down).
            _shutdown_wake(wake_listener, wake_stream)
            _shutdown_tts(tts_pipe)
            # Force-restore output volume in case a duck escaped a finally
            # block on the way down (best-effort; idempotent if depth == 0).
            try:
                shared_ducker.restore_all()
            except Exception:  # noqa: BLE001 — shutdown errors must not mask uvicorn return
                LOGGER.debug("shared_ducker.restore_all failed", exc_info=True)
            for w in watchers:
                w.cancel()
            await asyncio.gather(*watchers, return_exceptions=True)
            LOGGER.info("serve_inherent: shutdown complete")


__all__ = ["serve_inherent"]
