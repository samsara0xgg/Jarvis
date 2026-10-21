"""The supervisor sweep: overdue actions are closed, and system turns drive what they leave."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import TYPE_CHECKING

from jarvis.deployment.sleep_wake import sweep_overdue_actions
from jarvis.execution.tools import live_action_ids
from jarvis.runtime import JarvisRuntime, _event_action_id, _new_turn_id
from jarvis.runtime.daemon.intent_pump import drive_turn_in_worker_thread
from jarvis.runtime.daemon.output_watchers import emit_turn_failed
from jarvis.runtime.daemon.polling import DEFAULT_POLL_INTERVAL_S, WATCHER_RETRY_S
from jarvis.state.event_log import fetch_events_after, latest_id
from jarvis.state.trigger_consumption import trigger_was_consumed

if TYPE_CHECKING:
    from jarvis.runtime.session_compaction import CompactionSweep
    from jarvis.shared import Event

LOGGER = logging.getLogger(__name__)


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


def system_trigger_event(terminal_event: Event) -> Event:
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


async def system_trigger_watcher(
    runtime: JarvisRuntime,
    *,
    anchor_id: int,
    anchored: asyncio.Event,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
) -> None:
    """Background task: drive a system turn for each ORPHAN terminal event.

    Same cursor pattern as :func:`_intent_pump_watcher`, with two
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
            resume_id = after_id
            try:
                new_events = fetch_events_after(
                    runtime.conn,
                    after_id=after_id,
                    event_types=_SYSTEM_TRIGGER_TYPES,
                )
                for row_id, ev in new_events:
                    # drive_turn's own failures are caught below, so anything
                    # reaching the outer handler came from this row's checks:
                    # the next poll resumes at this row, not past the batch.
                    resume_id, after_id = after_id, max(after_id, row_id)
                    action_id = _event_action_id(ev)
                    if action_id is None:
                        continue
                    if action_id in live_action_ids():
                        # A live turn is driving it and will fold its own
                        # terminal; a system turn would double-handle it.
                        continue
                    if trigger_was_consumed(runtime.conn, ev.event_uid):
                        continue
                    trigger = system_trigger_event(ev)
                    try:
                        await asyncio.to_thread(
                            drive_turn_in_worker_thread,
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
                        emit_turn_failed(
                            runtime.conn,
                            intent_event=trigger,
                            exc=exc,
                        )
            except Exception:
                after_id = resume_id
                LOGGER.exception("system_trigger_watcher: poll failed at after_id=%d", after_id)
                await asyncio.sleep(WATCHER_RETRY_S)
                continue
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
    :func:`start_sweep_control_plane` already covered t=0, and a second
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


async def start_sweep_control_plane(
    runtime: JarvisRuntime,
    *,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    compaction: CompactionSweep | None = None,
) -> list[asyncio.Task[None]]:
    """Wire the ADR-0009 D4 sweep control plane. Returns its two tasks.

    The three statements below are ordered, and the order is the pin:

    1. Snapshot ``MAX(events.id)`` — BEFORE anything can write to the log.
    2. Start :func:`system_trigger_watcher` on that anchor and wait for
       it to signal ``anchored``.
    3. Only THEN run the one-shot bootstrap sweep.

    Inverted (sweep first, watcher second), the watcher's anchor would sit
    at or past the sweep's last emission and every orphan the bootstrap
    sweep just closed would drive no turn at all — the M4 acceptance row
    would no-op with no error anywhere. This function exists as one unit
    precisely so that ordering lives in a single readable place instead of
    inside ``serve_inherent``'s already-long body.
    """
    sweep_interval_s = runtime.config.supervisor.sweep_interval_s
    default_budget_s = runtime.config.supervisor.default_budget_s

    anchor_id = latest_id(runtime.conn)
    anchored = asyncio.Event()
    watcher = asyncio.create_task(
        system_trigger_watcher(
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
