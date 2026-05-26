"""Daemon entrypoint: serve the Inherent Step-1 FastAPI app + watcher tasks.

Composition root (``runtime/``). Owns the daemon lifecycle for the
text-only Inherent surface introduced by ADR-0003 Step 1:

1. Acquire the per-runtime-root process lock via
   :func:`jarvis.deployment.process_lock.acquire_exclusive` (ADR-0003
   D4 — refuses if another daemon already holds it).
2. Build the shared :class:`jarvis.surface.inherent_output.InherentBroadcaster`
   (Step 6) and a sync ``submit_callable`` bound to
   :func:`jarvis.surface.cli.emit_surface_user_intent` (mints a fresh
   ``turn_id`` per call).
3. Build the FastAPI app via
   :func:`jarvis.surface.inherent_server.create_app` (Step 7).
4. Spawn two background async tasks:

   - :func:`_user_intent_watcher` — polls the Event Log for new
     ``surface.user_intent`` rows; for each, drives the turn via
     :func:`jarvis.runtime.drive_turn` on a worker thread. ADR-0003
     D9 F3: an uncaught exception from ``drive_turn`` is caught, logged,
     and recorded as a ``turn.failed`` audit event; the watcher keeps
     polling.
   - :func:`_response_broadcaster` — polls the Event Log for new
     ``surface.response_emitted`` rows; for each, awaits
     :meth:`InherentBroadcaster.broadcast`.

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
import json
import logging
import sqlite3
import time
from typing import TYPE_CHECKING, Any

import uvicorn

from jarvis.deployment.process_lock import acquire_exclusive
from jarvis.runtime import JarvisRuntime, _new_turn_id, drive_turn
from jarvis.shared import Event
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.surface.cli import emit_surface_user_intent
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app

if TYPE_CHECKING:
    from pathlib import Path


LOGGER = logging.getLogger("jarvis.runtime.inherent_loop")


# Default poll cadence for both watchers; 10 ms balances CPU against
# first-byte latency between an L2 INSERT (Step 7 submit handler / L3
# render emission) and the watcher's reaction.
_DEFAULT_POLL_INTERVAL_S: float = 0.01

# Default port the Inherent client connects to. Picked to NOT collide
# with the legacy ``ui/web/server.py`` default (8000) or the
# inherent-swift dev server (8001).
_DEFAULT_PORT: int = 8006

# Watcher cursor SELECT — placeholders only, no user-controlled
# interpolation. Mirrors the column order of
# ``jarvis.state.event_log._SELECT_ALL_ORDERED_SQL``.
_SELECT_AFTER_ID_OF_TYPE_SQL = (
    "SELECT id, event_uid, type, schema_version, ts_epoch_ms, "
    "payload_json, source_event_id, correlation_json "
    "FROM events WHERE id > ? AND type = ? ORDER BY id ASC"
)


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
    event_type: str,
) -> list[tuple[int, Event]]:
    """SELECT every event of ``event_type`` with ``id > after_id``, oldest first.

    Returns a materialized list of ``(id, Event)`` tuples (not an
    iterator) because the watcher loop folds the cursor before its next
    poll; lifetime safety beats streaming for ~tens of rows. The ``id``
    is the SQLite row id used to advance the cursor.
    """
    cursor = conn.execute(
        _SELECT_AFTER_ID_OF_TYPE_SQL,
        (after_id, event_type),
    )
    return [_row_to_id_event(row) for row in cursor]


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
        )
    finally:
        with contextlib.suppress(sqlite3.Error):
            worker_conn.close()


async def _user_intent_watcher(
    runtime: JarvisRuntime,
    *,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
) -> None:
    """Background task: drive a turn for each new ``surface.user_intent`` row.

    Lifecycle:

    1. Anchor the cursor at ``MAX(events.id)`` so events that landed
       before the watcher started are not replayed.
    2. Poll every ``poll_interval_s`` seconds. For each new
       ``surface.user_intent`` row, dispatch
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
                event_type="surface.user_intent",
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


async def _response_broadcaster(
    runtime: JarvisRuntime,
    broadcaster: InherentBroadcaster,
    *,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
) -> None:
    """Background task: forward every new ``surface.response_emitted`` to broadcaster.

    Cursor anchoring mirrors :func:`_user_intent_watcher`. Each row's
    Event is passed through to
    :meth:`InherentBroadcaster.broadcast`, which handles the wire
    envelope translation (open/done) and the F4/F5 failure modes.
    """
    after_id = _latest_id(runtime.conn)
    LOGGER.info("response_broadcaster started (after_id=%d)", after_id)
    try:
        while True:
            new_events = _fetch_events_after(
                runtime.conn,
                after_id=after_id,
                event_type="surface.response_emitted",
            )
            for row_id, ev in new_events:
                after_id = max(after_id, row_id)
                await broadcaster.broadcast(ev)
            await asyncio.sleep(poll_interval_s)
    except asyncio.CancelledError:
        LOGGER.info("response_broadcaster cancelled")
        raise


async def serve_inherent(
    runtime: JarvisRuntime,
    *,
    host: str = "127.0.0.1",
    port: int = _DEFAULT_PORT,
    lock_path: Path,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
) -> None:
    """Run the Inherent Step-1 daemon. Blocks until SIGINT / SIGTERM.

    Lifecycle (the ``acquire_exclusive`` context manager is the
    OUTERMOST scope so the lock release is the very LAST cleanup step):

    1. :func:`acquire_exclusive` — raises
       :class:`jarvis.deployment.process_lock.ProcessLockHeld` if
       another live daemon already holds it.
    2. Build :class:`InherentBroadcaster` (Step 6) and ``submit_callable``
       (binds to :func:`emit_surface_user_intent` with a fresh
       ``turn_id`` per HTTP submit).
    3. Build the FastAPI app via :func:`create_app` (Step 7) with
       :class:`InherentDeps`.
    4. Configure :class:`uvicorn.Config` (``lifespan="off"`` because
       this module owns the lifecycle, ``log_level="warning"`` to
       avoid uvicorn's per-request stdout noise drowning the watcher
       logs).
    5. Spawn :func:`_user_intent_watcher` and :func:`_response_broadcaster`
       as background tasks BEFORE :meth:`uvicorn.Server.serve` so an
       early intent post is observed.
    6. ``await server.serve()`` — blocks until uvicorn returns (signal
       received).
    7. ``finally``: cancel both watchers and ``await`` them with
       ``return_exceptions=True`` so a watcher that crashed during
       runtime does not mask the shutdown path. Then the
       ``acquire_exclusive`` context manager unwinds and releases the
       lock file.

    Args:
        runtime: Assembled :class:`JarvisRuntime`.
        host: Bind interface for the FastAPI app.
        port: TCP port for the FastAPI app.
        lock_path: Per-runtime-root daemon lock file. Resolved by
            ``cli/__main__.py`` (Step 9) to ``${runtime_root}/jarvis.lock``.
        poll_interval_s: Watcher poll cadence (default 10 ms).

    Raises:
        jarvis.deployment.process_lock.ProcessLockHeld: Another daemon
            already holds the lock for this runtime root.
    """
    with acquire_exclusive(lock_path):
        broadcaster = InherentBroadcaster()

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

        deps = InherentDeps(
            submit_callable=submit_callable,
            broadcaster=broadcaster,
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
                _response_broadcaster(runtime, broadcaster, poll_interval_s=poll_interval_s),
                name="response_broadcaster",
            ),
        ]

        try:
            await server.serve()
        finally:
            LOGGER.info("serve_inherent: shutting down watchers")
            for w in watchers:
                w.cancel()
            await asyncio.gather(*watchers, return_exceptions=True)
            LOGGER.info("serve_inherent: shutdown complete")


__all__ = ["serve_inherent"]
