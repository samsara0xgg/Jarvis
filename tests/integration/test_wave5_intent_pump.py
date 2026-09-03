"""Wave 5 acceptance: the adoption-watermarked durable-claim intent pump.

ADR-0008 §8 Step 4, second half of the row: "change the watcher into an
adoption-watermarked durable-claim intent pump", with the acceptance
properties "crash after claim", "historical inputs not replayed" and "no
double dispatch".

Every check runs the real pump against a real on-disk Event Log and the real
L2 claim primitives; only ``_drive_turn_in_worker_thread`` is a recording
stub, because the property under test is which triggers reach a turn and how
often — not what the turn then does. The stub is what a crash looks like from
the pump's side: the claim is durable, the work is not.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import threading
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from jarvis.deployment import bootstrap_runtime
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.runtime import JarvisRuntime, inherent_loop
from jarvis.runtime.inherent_loop import (
    _boot_intent_pump_in_thread,
    _intent_pump_watcher,
    _intent_worker,
    _IntentPumpBoot,
    _start_intent_pump,
)
from jarvis.shared.realtime import Wave5InputFlags
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.input_claim import (
    REALTIME_INTENT_CONSUMER,
    InputAlreadyClaimed,
    InputClaimed,
    adopt_consumer,
    claim_input_once,
    recoverable_inputs,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from jarvis.shared import Event


# --- harness ---------------------------------------------------------------


def _make_runtime(tmp_path: Path) -> JarvisRuntime:
    """Build a minimal runtime with a real Event Log and the pump switched on."""
    paths = bootstrap_runtime(tmp_path)
    conn = open_event_log(paths.event_log)
    return JarvisRuntime(
        config={},
        runtime_paths=paths,
        conn=conn,
        tool_registry=build_default_registry(),
        lifecycle=ActionLifecycle(),
        llm_client=None,  # type: ignore[arg-type]
        system_prompt="",
        input_flags=Wave5InputFlags(intent_pump=True),
    )


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[JarvisRuntime]:
    """A runtime whose Event Log is closed after the test."""
    built = _make_runtime(tmp_path)
    try:
        yield built
    finally:
        with contextlib.suppress(sqlite3.Error):
            built.conn.close()


def _emit_intent(runtime: JarvisRuntime, turn_id: str, transcript: str = "hi") -> Event:
    """Emit one keyboard-surface trigger."""
    return emit_event(
        runtime.conn,
        type="surface.user_intent",
        payload={
            "transcript": transcript,
            "turn_id": turn_id,
            "channel": "cli_stdin",
            "language": "zh-CN",
        },
        correlation={"turn_id": turn_id},
    )


def _count(conn: sqlite3.Connection, event_type: str) -> int:
    """Count rows of one event type."""
    row = conn.execute("SELECT COUNT(*) FROM events WHERE type = ?", (event_type,)).fetchone()
    assert row is not None
    return int(row[0])


class _Recorder:
    """Records every turn the pump hands to the worker thread."""

    def __init__(self) -> None:
        """Start with nothing driven."""
        self.driven: list[str] = []
        self._lock = threading.Lock()

    def __call__(
        self,
        _runtime: JarvisRuntime,
        *,
        user_intent_event: Event,
        **_: object,
    ) -> None:
        """Record one dispatched turn id."""
        with self._lock:
            self.driven.append(str(user_intent_event.payload["turn_id"]))


async def _drain(runtime: JarvisRuntime, recorder: _Recorder, *, expect: int) -> None:
    """Run the pump until it has driven ``expect`` turns, or the spin budget ends."""
    with patch.object(
        inherent_loop,
        "_drive_turn_in_worker_thread",
        side_effect=recorder,
    ):
        tasks = await _start_intent_pump(
            runtime,
            poll_interval_s=0.001,
            queue_capacity=8,
            max_concurrent_turns=2,
        )
        try:
            for _ in range(400):
                if len(recorder.driven) >= expect:
                    break
                await asyncio.sleep(0.005)
            # One more settle pass, so a double dispatch has time to show up.
            for _ in range(20):
                await asyncio.sleep(0.005)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


# --- historical inputs are not replayed -------------------------------------


def test_adoption_watermark_excludes_history(runtime: JarvisRuntime) -> None:
    """Utterances that predate adoption are history and never drive a turn."""
    _emit_intent(runtime, "T-old-1", "昨天说的")
    _emit_intent(runtime, "T-old-2", "前天说的")
    recorder = _Recorder()

    async def _body() -> None:
        await _drain(runtime, recorder, expect=1)

    asyncio.run(_body())

    assert recorder.driven == []
    # Exactly one watermark, recorded once, naming the log's end at adoption.
    adopted = _count(runtime.conn, "consumer.adopted")
    assert adopted == 1
    # And no claim was written for the pre-adoption rows.
    assert _count(runtime.conn, "turn.started") == 0


def test_adoption_is_recorded_exactly_once_across_boots(runtime: JarvisRuntime) -> None:
    """A second boot reuses the first watermark instead of moving it forward."""
    first = _boot_intent_pump_in_thread(runtime.runtime_paths.event_log)
    _emit_intent(runtime, "T-after")
    second = _boot_intent_pump_in_thread(runtime.runtime_paths.event_log)

    assert first.adopted_now is True
    assert second.adopted_now is False
    assert second.adoption_row_id == first.adoption_row_id
    assert _count(runtime.conn, "consumer.adopted") == 1
    # The row that landed after adoption is exactly what the second boot owes.
    assert [event.payload["turn_id"] for event in second.recovered] == ["T-after"]


# --- steady state -----------------------------------------------------------


def test_post_adoption_intents_are_claimed_and_driven_once(runtime: JarvisRuntime) -> None:
    """Each new trigger produces one durable claim and one dispatch."""
    recorder = _Recorder()

    async def _body() -> None:
        with patch.object(
            inherent_loop,
            "_drive_turn_in_worker_thread",
            side_effect=recorder,
        ):
            tasks = await _start_intent_pump(
                runtime,
                poll_interval_s=0.001,
                queue_capacity=8,
                max_concurrent_turns=2,
            )
            try:
                await asyncio.sleep(0.02)
                _emit_intent(runtime, "T-a", "第一句")
                _emit_intent(runtime, "T-b", "第二句")
                for _ in range(400):
                    if len(recorder.driven) >= 2:
                        break
                    await asyncio.sleep(0.005)
                for _ in range(20):
                    await asyncio.sleep(0.005)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(_body())

    assert sorted(recorder.driven) == ["T-a", "T-b"]
    assert _count(runtime.conn, "turn.started") == 2


# --- crash after claim ------------------------------------------------------


def test_crash_after_claim_is_adopted_once_on_restart(runtime: JarvisRuntime) -> None:
    """A turn claimed but never worked is re-driven on the next boot, once."""
    adoption = adopt_consumer(runtime.conn, name=REALTIME_INTENT_CONSUMER)
    assert adoption.adopted_now is True

    trigger = _emit_intent(runtime, "T-crashed", "跑一下那个任务")
    claim = claim_input_once(runtime.conn, trigger_event=trigger)
    assert isinstance(claim, InputClaimed)
    # This is the crash: the claim is durable and nothing else happened.
    assert _count(runtime.conn, "turn.started") == 1

    recorder = _Recorder()

    async def _body() -> None:
        await _drain(runtime, recorder, expect=1)

    asyncio.run(_body())

    assert recorder.driven == ["T-crashed"]
    # Recovery re-used the existing claim; it did not mint a second turn row.
    assert _count(runtime.conn, "turn.started") == 1


def test_a_claimed_turn_that_already_worked_is_not_replayed(runtime: JarvisRuntime) -> None:
    """A claimed turn with a milestone is never re-driven.

    ADR-0008 D8: only "claimed turn with no response/action milestone" is
    safe to re-enqueue. A turn that already dispatched an action must not be
    redispatched, and one that already answered must not answer twice.
    """
    adopt_consumer(runtime.conn, name=REALTIME_INTENT_CONSUMER)
    finished = _emit_intent(runtime, "T-done", "已经答过了")
    claim_input_once(runtime.conn, trigger_event=finished)
    emit_event(
        runtime.conn,
        type="turn.ended",
        payload={"turn_id": "T-done"},
        correlation={"turn_id": "T-done"},
    )
    in_flight = _emit_intent(runtime, "T-acting", "已经派过活了")
    claim_input_once(runtime.conn, trigger_event=in_flight)
    emit_event(
        runtime.conn,
        type="action.proposed",
        payload={
            "action_id": "A-orphan",
            "tool_name": "spawn_worker",
            "caller_principal": "jarvis_llm",
            "risk_level": "L2",
        },
        correlation={"turn_id": "T-acting"},
    )

    recorder = _Recorder()

    async def _body() -> None:
        await _drain(runtime, recorder, expect=1)

    asyncio.run(_body())

    assert recorder.driven == []


def test_a_row_landing_during_the_boot_scan_is_not_lost(runtime: JarvisRuntime) -> None:
    """The live cursor must be read BEFORE the recovery scan, never after.

    ``_boot_intent_pump_in_thread`` reads ``live_cursor_id`` first and only
    then scans for recoverable rows, so a trigger committed between the two is
    seen twice — once by the scan, once by the poll loop — and the pump's
    dispatched-turn set collapses that. Reading the cursor *after* the scan
    inverts the overlap into a gap: such a row is inside neither window and is
    silently never driven, which for a durable utterance is data loss.

    The interleaving is forced deterministically by letting the real scan run
    and only then committing a trigger, on its own connection, before the boot
    function reads whatever it reads next. That connection is opened inside the
    wrapper because the boot scan runs on an ``asyncio.to_thread`` worker and
    the Event Log's connections are ``check_same_thread``.
    """
    adopt_consumer(runtime.conn, name=REALTIME_INTENT_CONSUMER)
    real_scan = recoverable_inputs

    def _scan_then_commit(conn: sqlite3.Connection, **kwargs: Any) -> Any:  # noqa: ANN401 - delegates to the real signature.
        """Run the real scan, then land a row the scan could not have seen."""
        found = real_scan(conn, **kwargs)
        late = open_event_log(runtime.runtime_paths.event_log)
        try:
            emit_event(
                late,
                type="surface.user_intent",
                payload={
                    "transcript": "刚好挤进来的",
                    "turn_id": "T-mid-boot",
                    "channel": "cli_stdin",
                    "language": "zh-CN",
                },
                correlation={"turn_id": "T-mid-boot"},
            )
        finally:
            with contextlib.suppress(sqlite3.Error):
                late.close()
        return found

    recorder = _Recorder()

    async def _body() -> None:
        with patch.object(inherent_loop, "recoverable_inputs", _scan_then_commit):
            await _drain(runtime, recorder, expect=1)

    asyncio.run(_body())

    # Driven exactly once: the scan missed it, so the poll loop owes it.
    assert recorder.driven == ["T-mid-boot"]
    assert _count(runtime.conn, "turn.started") == 1


# --- no double dispatch -----------------------------------------------------


def test_the_boot_scan_and_the_poll_loop_do_not_both_dispatch(
    runtime: JarvisRuntime,
) -> None:
    """A row visible to both the recovery scan and the live cursor drives once.

    The pump reads its live cursor **before** the recovery scan, so a trigger
    that lands between the two is offered twice — once by the scan and once
    by the poll loop. That is the safe direction (reading the cursor after
    would let such a row fall through both), and the in-process
    dispatched-turn set is what turns "offered twice" into "driven once".

    The window is exercised directly rather than raced: the boot record is
    built with the cursor value the pump would have read a moment before the
    row landed, which is exactly the state the race produces.
    """
    adoption = adopt_consumer(runtime.conn, name=REALTIME_INTENT_CONSUMER)
    assert adoption.adopted_now is True
    overlap = _emit_intent(runtime, "T-overlap", "同一句")
    boot = _IntentPumpBoot(
        adoption_row_id=adoption.adoption_row_id,
        live_cursor_id=adoption.adoption_row_id,
        recovered=(overlap,),
        adopted_now=False,
    )

    recorder = _Recorder()

    async def _body() -> None:
        with patch.object(
            inherent_loop,
            "_drive_turn_in_worker_thread",
            side_effect=recorder,
        ):
            queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=8)
            dispatched: set[str] = set()
            tasks = [
                asyncio.create_task(
                    _intent_pump_watcher(
                        runtime,
                        queue,
                        boot,
                        dispatched,
                        poll_interval_s=0.001,
                    ),
                ),
                asyncio.create_task(_intent_worker(runtime, queue)),
            ]
            try:
                for _ in range(400):
                    if recorder.driven:
                        break
                    await asyncio.sleep(0.005)
                # Settle: a second dispatch would arrive in this window.
                for _ in range(40):
                    await asyncio.sleep(0.005)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(_body())

    assert recorder.driven == ["T-overlap"]
    assert _count(runtime.conn, "turn.started") == 1


def test_a_second_claim_of_one_trigger_returns_the_first(runtime: JarvisRuntime) -> None:
    """The durable claim is the cross-process guard, and it is idempotent."""
    trigger = _emit_intent(runtime, "T-once")
    first = claim_input_once(runtime.conn, trigger_event=trigger)
    second = claim_input_once(runtime.conn, trigger_event=trigger)

    assert isinstance(first, InputClaimed)
    assert isinstance(second, InputAlreadyClaimed)
    assert second.event.event_uid == first.event.event_uid
    assert _count(runtime.conn, "turn.started") == 1


def test_two_racing_claimers_produce_one_turn_started(runtime: JarvisRuntime) -> None:
    """Two connections claiming the same trigger resolve to one durable claim."""
    trigger = _emit_intent(runtime, "T-race")
    outcomes: list[Any] = []
    barrier = threading.Barrier(2)

    def _claim() -> None:
        conn = open_event_log(runtime.runtime_paths.event_log)
        try:
            barrier.wait(timeout=5)
            outcomes.append(claim_input_once(conn, trigger_event=trigger))
        finally:
            with contextlib.suppress(sqlite3.Error):
                conn.close()

    threads = [threading.Thread(target=_claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(outcomes) == 2
    assert sum(isinstance(outcome, InputClaimed) for outcome in outcomes) == 1
    assert _count(runtime.conn, "turn.started") == 1


# --- backpressure -----------------------------------------------------------


def test_a_full_queue_holds_the_cursor_instead_of_dropping(runtime: JarvisRuntime) -> None:
    """ADR-0008 §4.1: a durable utterance is never dropped, only delayed."""
    adopt_consumer(runtime.conn, name=REALTIME_INTENT_CONSUMER)
    for index in range(6):
        _emit_intent(runtime, f"T-{index}", f"第{index}句")

    recorder = _Recorder()
    started = threading.Event()
    release = threading.Event()

    def _blocking(
        _runtime: JarvisRuntime,
        *,
        user_intent_event: Event,
        **_: object,
    ) -> None:
        started.set()
        release.wait(timeout=10)
        recorder(_runtime, user_intent_event=user_intent_event)

    async def _body() -> None:
        with patch.object(
            inherent_loop,
            "_drive_turn_in_worker_thread",
            side_effect=_blocking,
        ):
            tasks = await _start_intent_pump(
                runtime,
                poll_interval_s=0.001,
                # One slot in the queue and one worker: the pump must block
                # rather than discard the four it cannot hold.
                queue_capacity=1,
                max_concurrent_turns=1,
            )
            try:
                for _ in range(200):
                    if started.is_set():
                        break
                    await asyncio.sleep(0.005)
                assert started.is_set()
                release.set()
                for _ in range(600):
                    if len(recorder.driven) >= 6:
                        break
                    await asyncio.sleep(0.005)
            finally:
                release.set()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(_body())

    assert sorted(recorder.driven) == [f"T-{index}" for index in range(6)]
    assert _count(runtime.conn, "turn.started") == 6
