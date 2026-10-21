"""Boot reconciliation: close the response runs and playback a dead process left open."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from typing import TYPE_CHECKING

from jarvis.decision.response_run import ResponseTerminalizer, reconcile_open_responses
from jarvis.state.event_log import open_event_log
from jarvis.surface.playback_recovery import reconcile_open_playback

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.runtime import JarvisRuntime
    from jarvis.state.committed_event_bus import CommittedEventBus

LOGGER = logging.getLogger(__name__)


def reconcile_open_responses_in_thread(
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


def reconcile_open_playback_in_thread(
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


async def reconcile_at_boot(runtime: JarvisRuntime) -> None:
    """Close what a previous process left open, once, before any watcher task exists."""
    # ADR-0008 F14 / §4.4 — close ResponseRuns a previous process
    # abandoned, once, inside the startup barrier and before any
    # watcher task exists.
    closed_runs = await asyncio.to_thread(
        reconcile_open_responses_in_thread,
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
        reconcile_open_playback_in_thread,
        runtime.runtime_paths.event_log,
        runtime.committed_event_bus,
    )
    if closed_playback:
        LOGGER.info(
            "boot reconciliation closed %d open playback generation(s)",
            closed_playback,
        )
