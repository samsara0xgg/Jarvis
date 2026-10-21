"""The intent pump: new intents in the event log become turns on worker threads."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import sqlite3
import time
from typing import TYPE_CHECKING, Final

from jarvis.decision.pre_route import typed_turn
from jarvis.decision.response_run import ResponseCancelledError
from jarvis.runtime.daemon.output_watchers import emit_turn_failed
from jarvis.runtime.daemon.polling import WATCHER_RETRY_S
from jarvis.runtime.turn.drive import drive_turn
from jarvis.runtime.turn.wait import (
    TriggerWaitTimeout,
    TurnSuspended,
    WaitingTurn,
    wait_for_next_trigger,
)
from jarvis.shared.realtime_trace import record_realtime_trace
from jarvis.state import reactions
from jarvis.state.event_log import (
    fetch_events_after,
    latest_id,
    open_event_log,
    open_runtime_event_log,
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
from jarvis.state.turn_overlap import turns_in_flight

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.runtime import JarvisRuntime
    from jarvis.shared import Event

LOGGER = logging.getLogger(__name__)


class TurnConnectionUnavailableError(Exception):
    """The driver did not take ownership because its connection could not open."""


def drive_turn_in_worker_thread(
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

    Every cross-thread emit-site opens its own SQLite connection so the
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


# Event types the intent pump folds into ONE
# cursor. Per ADR-0005 §5.1 "Important wiring detail", both the
# keyboard surface (``surface.user_intent`` from cli/__main__.py and
# the daemon /inherent/submit handler) AND the voice surface
# (``utterance.received`` from the ASR pipeline / voice session) drive
# a turn through the same composition-root code path. The downstream
# ``drive_turn`` (and thus L3 / L4 / L5) is identical regardless of
# which event type arrived — the pump passes the event verbatim as
# ``user_intent_event`` to :func:`drive_turn`, which reads
# ``payload["transcript"]`` and ``payload["turn_id"]`` (both schemas
# require these keys per the L2 event registry).
_USER_INTENT_TRIGGER_TYPES: tuple[str, ...] = (
    "surface.user_intent",
    "utterance.received",
)


@dataclasses.dataclass(frozen=True)
class IntentPumpBoot:
    """What the boot scan established before the pump started polling."""

    adoption_row_id: int
    live_cursor_id: int
    recovered: tuple[Event, ...]
    adopted_now: bool


def boot_intent_pump_in_thread(event_log_path: Path) -> IntentPumpBoot:
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
        live_cursor_id = latest_id(conn)
        recovered = recoverable_inputs(
            conn,
            adoption_row_id=adoption.adoption_row_id,
            trigger_types=_USER_INTENT_TRIGGER_TYPES,
        )
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
    return IntentPumpBoot(
        adoption_row_id=adoption.adoption_row_id,
        live_cursor_id=live_cursor_id,
        recovered=tuple(event for _row_id, event in recovered),
        adopted_now=adoption.adopted_now,
    )


def claim_intent_in_thread(event_log_path: Path, trigger_event: Event) -> bool:
    """Durably claim one trigger; return whether this process should drive it.

    ``True`` for a fresh claim and for a claim this process finds unfinished
    (crash after claim, before any milestone — the scan only offers those).
    ``False`` for a trigger that cannot be driven at all, which is only the
    conflicting-turn_id case: two different utterances asserting one turn
    identity is a bug upstream, and driving either would be a guess.
    """
    conn = open_runtime_event_log(event_log_path)
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


async def intent_pump_watcher(
    runtime: JarvisRuntime,
    queue: asyncio.Queue[Event | WaitingTurn],
    boot: IntentPumpBoot,
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

    A poll that raises (a claim that waited out the write lock) is logged and
    retried from the same cursor: this task is the only reader of new input.
    """
    after_id = boot.live_cursor_id
    recovered = list(boot.recovered)
    LOGGER.info(
        "intent_pump started (adoption_row_id=%d, after_id=%d, recovered=%d)",
        boot.adoption_row_id,
        after_id,
        len(recovered),
    )
    try:
        while True:
            try:
                while recovered:
                    await _offer_intent(runtime, queue, dispatched, recovered[0])
                    recovered.pop(0)
                for row_id, event in fetch_events_after(
                    runtime.conn,
                    after_id=after_id,
                    event_types=_USER_INTENT_TRIGGER_TYPES,
                ):
                    await _offer_intent(runtime, queue, dispatched, event)
                    after_id = max(after_id, row_id)
            except Exception:
                LOGGER.exception("intent_pump: poll failed at after_id=%d; retrying", after_id)
                await asyncio.sleep(WATCHER_RETRY_S)
                continue
            await asyncio.sleep(poll_interval_s)
    except asyncio.CancelledError:
        LOGGER.info("intent_pump cancelled")
        raise


_SEEN_WINDOW_MS: Final = 5 * 60 * 1000
"""A turn started longer ago than this is the supervisor's, not one he is still waiting on."""


def mark_seen(event_log_path: Path, event: Event) -> None:
    """ADR 0257: eyes on a message he typed while another of his turns is being answered.

    The program puts them, at once, on the message's record (written when its turn is driven);
    the model is not involved and nothing removes them. A log that cannot be written is only
    logged: the turn goes on without the mark.
    """
    if not typed_turn(event):
        return
    conn = open_runtime_event_log(event_log_path)
    try:
        if turns_in_flight(
            conn,
            trigger_event_uid=event.event_uid,
            since_ms=int(time.time() * 1000) - _SEEN_WINDOW_MS,
        ):
            reactions.emit(
                conn,
                target=str(event.payload.get("record_id") or event.event_uid),
                by=reactions.JARVIS,
                emoji=reactions.EYES,
            )
    except sqlite3.Error:
        LOGGER.warning("intent_pump: could not mark %s as seen", event.event_uid, exc_info=True)
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()


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
        claim_intent_in_thread,
        runtime.runtime_paths.event_log,
        event,
    ):
        return
    if isinstance(turn_id, str):
        dispatched.add(turn_id)
    await asyncio.to_thread(mark_seen, runtime.runtime_paths.event_log, event)
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
        drive_turn_in_worker_thread, runtime,
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


async def intent_worker(
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
                    emit_turn_failed(
                        runtime.conn, intent_event=event, exc=exc,
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
                emit_turn_failed(
                    runtime.conn,
                    intent_event=event,
                    exc=exc,
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
            event, cursor = wait_for_next_trigger(
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
            drive_turn_in_worker_thread, runtime,
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


async def start_intent_pump(
    runtime: JarvisRuntime,
    *,
    poll_interval_s: float,
    queue_capacity: int,
    max_concurrent_turns: int,
) -> list[asyncio.Task[None]]:
    """Adopt the input stream, recover what a crash owes, and start the pump."""
    boot = await asyncio.to_thread(
        boot_intent_pump_in_thread,
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
            intent_pump_watcher(
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
        asyncio.create_task(intent_worker(runtime, queue, waiting), name=f"intent_worker_{index}")
        for index in range(max_concurrent_turns)
    )
    tasks.append(asyncio.create_task(
        _waiting_turn_watcher(runtime, queue, waiting, poll_interval_s=poll_interval_s),
        name="waiting_turn_watcher",
    ))
    return tasks
