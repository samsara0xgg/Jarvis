"""Unit tests for ``jarvis.runtime.inherent_loop`` — ADR-0003 Step 1 + Step 2.

LLM-free pure-asyncio tests covering the two background watchers
(``_user_intent_watcher``, ``_response_watcher``) plus the daemon
entrypoint's ``ProcessLockHeld`` surfacing path.

The watchers are exercised with a real on-disk SQLite Event Log (via
:func:`jarvis.state.event_log.open_event_log`) and a monkeypatched
``drive_turn`` / a fake :class:`InherentBroadcaster`. The full
``serve_inherent`` lifecycle (uvicorn boot + watcher coordination on
shutdown) is covered by the Step 1 / Step 2 integration smoke tests;
here we only assert that ``ProcessLockHeld`` propagates when the lock
is pre-held.

Step 2 (D16): ``_response_watcher`` is a SINGLE cursor over the three
``surface.response_{open,chunk,emitted}`` event types, dispatched to
``broadcast_open`` / ``broadcast_chunk`` / ``broadcast_done`` by
``event.type``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from jarvis.deployment import bootstrap_runtime
from jarvis.deployment.process_lock import ProcessLockHeld, acquire_exclusive
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.runtime import JarvisRuntime, inherent_loop
from jarvis.runtime.inherent_loop import (
    _fetch_events_after,
    _latest_id,
    _response_watcher,
    _user_intent_watcher,
)
from jarvis.state.event_log import emit_event, iter_events, open_event_log
from jarvis.surface.cli import emit_surface_user_intent

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path

    import uvicorn

    from jarvis.shared import Event


# ---------------------------------------------------------------------------
# Test doubles + fixtures
# ---------------------------------------------------------------------------


@dataclass
class _FakeBroadcaster:
    """Records every ``broadcast_*`` call. Stand-in for InherentBroadcaster.

    The watcher calls one of three methods per event row; the spy
    captures ``(method_name, event)`` tuples so tests can assert both
    dispatch correctness AND ordering across mixed event types.
    """

    received: list[tuple[str, Event]] = field(default_factory=list)

    async def broadcast_open(self, event: Event) -> None:
        self.received.append(("open", event))

    async def broadcast_chunk(self, event: Event) -> None:
        self.received.append(("chunk", event))

    async def broadcast_done(self, event: Event) -> None:
        self.received.append(("done", event))


def _make_runtime(tmp_path: Path) -> JarvisRuntime:
    """Build a minimal :class:`JarvisRuntime` with a real Event Log.

    Only ``conn``, ``runtime_paths``, ``tool_registry``, ``lifecycle`` are
    exercised by the watcher tests — the rest (config, llm_client,
    system_prompt) get sentinel values because ``drive_turn`` is
    monkeypatched to a stub in every test that runs it.
    """
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
    )


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[JarvisRuntime]:
    """Per-test runtime; closes the SQLite connection on teardown."""
    rt = _make_runtime(tmp_path)
    try:
        yield rt
    finally:
        rt.conn.close()


def _seed_user_intent(conn: sqlite3.Connection, *, turn_id: str = "T-watch") -> Event:
    """Emit one surface.user_intent and return the persisted Event."""
    return emit_surface_user_intent(conn, transcript="hi", turn_id=turn_id)


def _seed_response_emitted(
    conn: sqlite3.Connection,
    *,
    turn_id: str = "T-resp",
    text: str = "ok",
) -> Event:
    """Emit one surface.response_emitted and return the persisted Event."""
    return emit_event(
        conn,
        type="surface.response_emitted",
        payload={"turn_id": turn_id, "text": text},
        correlation={"turn_id": turn_id},
    )


def _seed_response_open(
    conn: sqlite3.Connection,
    *,
    turn_id: str = "T-open",
    query: str = "hi",
) -> Event:
    """Emit one surface.response_open and return the persisted Event."""
    return emit_event(
        conn,
        type="surface.response_open",
        payload={"turn_id": turn_id, "query": query, "kind": "text"},
        correlation={"turn_id": turn_id},
    )


def _seed_response_chunk(
    conn: sqlite3.Connection,
    *,
    turn_id: str = "T-chunk",
    text: str = "tok",
) -> Event:
    """Emit one surface.response_chunk and return the persisted Event."""
    return emit_event(
        conn,
        type="surface.response_chunk",
        payload={"turn_id": turn_id, "text": text},
        correlation={"turn_id": turn_id},
    )


# ---------------------------------------------------------------------------
# _user_intent_watcher
# ---------------------------------------------------------------------------


async def _spin_briefly() -> None:
    """Yield to the loop a few times so the watcher polls."""
    for _ in range(20):
        await asyncio.sleep(0.005)


async def _cancel_and_await(task: asyncio.Task[Any]) -> None:
    """Cancel a watcher task and swallow the cancellation."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def test_user_intent_watcher_drives_turn_on_surface_user_intent(
    runtime: JarvisRuntime,
) -> None:
    """A new surface.user_intent triggers drive_turn with frozenset() + streaming_enabled=True."""
    captured: list[dict[str, Any]] = []

    def _stub_drive_turn(
        rt: JarvisRuntime,
        *,
        user_intent_event: Event,
        available_surfaces: frozenset[str] | None = None,
        streaming_enabled: bool = False,
        **_: object,
    ) -> None:
        captured.append(
            {
                "runtime_id": id(rt),
                "user_intent_event": user_intent_event,
                "available_surfaces": available_surfaces,
                "streaming_enabled": streaming_enabled,
            },
        )

    async def _body() -> None:
        with patch.object(inherent_loop, "drive_turn", _stub_drive_turn):
            task = asyncio.create_task(
                _user_intent_watcher(runtime, poll_interval_s=0.001),
            )
            await asyncio.sleep(0.02)  # let cursor settle on empty log
            seeded = _seed_user_intent(runtime.conn, turn_id="T-w1")
            await _spin_briefly()
            await _cancel_and_await(task)

            assert len(captured) == 1
            call = captured[0]
            assert call["user_intent_event"].event_uid == seeded.event_uid
            assert call["available_surfaces"] == frozenset()
            # ADR-0003 Step 2: the daemon watcher MUST pass
            # streaming_enabled=True so render_response emits the
            # 3-event Inherent taxonomy before the audit event.
            assert call["streaming_enabled"] is True

    asyncio.run(_body())


def test_user_intent_watcher_skips_other_event_types(
    runtime: JarvisRuntime,
) -> None:
    """An event whose type != surface.user_intent never reaches drive_turn."""
    captured: list[Event] = []

    def _stub(_rt: JarvisRuntime, *, user_intent_event: Event, **__: object) -> None:
        captured.append(user_intent_event)

    async def _body() -> None:
        with patch.object(inherent_loop, "drive_turn", _stub):
            task = asyncio.create_task(
                _user_intent_watcher(runtime, poll_interval_s=0.001),
            )
            await asyncio.sleep(0.02)
            # Non-trigger types: emit two rows the watcher must ignore.
            emit_event(
                runtime.conn,
                type="turn.started",
                payload={"turn_id": "T-skip"},
            )
            emit_event(
                runtime.conn,
                type="surface.response_emitted",
                payload={"turn_id": "T-skip", "text": "hi"},
            )
            await _spin_briefly()
            await _cancel_and_await(task)

            assert captured == []

    asyncio.run(_body())


def test_user_intent_watcher_emits_turn_failed_on_drive_turn_exception(
    runtime: JarvisRuntime,
) -> None:
    """drive_turn raising => watcher emits turn.failed with the repr + turn_id."""

    def _raising(_rt: JarvisRuntime, *, user_intent_event: Event, **__: object) -> None:
        # Use the turn_id from the event to make the cross-check tight.
        del user_intent_event
        msg = "deliberate boom"
        raise RuntimeError(msg)

    async def _body() -> None:
        with patch.object(inherent_loop, "drive_turn", _raising):
            task = asyncio.create_task(
                _user_intent_watcher(runtime, poll_interval_s=0.001),
            )
            await asyncio.sleep(0.02)
            seeded = _seed_user_intent(runtime.conn, turn_id="T-fail-001")
            await _spin_briefly()
            await _cancel_and_await(task)

            failed = [e for e in iter_events(runtime.conn) if e.type == "turn.failed"]
            assert len(failed) == 1
            fp = failed[0].payload
            assert fp["turn_id"] == "T-fail-001"
            assert "deliberate boom" in fp["exception_repr"]
            assert "RuntimeError" in fp["exception_repr"]
            # trigger_event_id round-trips the originating event's uid.
            assert fp["trigger_event_id"] == seeded.event_uid

    asyncio.run(_body())


def test_user_intent_watcher_continues_after_failure(
    runtime: JarvisRuntime,
) -> None:
    """Two intents in a row: first raises, second succeeds; both reach drive_turn."""
    call_count = {"n": 0}

    def _flaky(_rt: JarvisRuntime, *, user_intent_event: Event, **__: object) -> None:
        del user_intent_event
        call_count["n"] += 1
        if call_count["n"] == 1:
            msg = "first call boom"
            raise RuntimeError(msg)
        # second call: no-op success

    async def _body() -> None:
        with patch.object(inherent_loop, "drive_turn", _flaky):
            task = asyncio.create_task(
                _user_intent_watcher(runtime, poll_interval_s=0.001),
            )
            await asyncio.sleep(0.02)
            _seed_user_intent(runtime.conn, turn_id="T-a")
            await asyncio.sleep(0.02)
            _seed_user_intent(runtime.conn, turn_id="T-b")
            await _spin_briefly()
            await _cancel_and_await(task)

            assert call_count["n"] == 2  # both events drove
            failed = [e for e in iter_events(runtime.conn) if e.type == "turn.failed"]
            assert len(failed) == 1  # only the first failed
            assert failed[0].payload["turn_id"] == "T-a"

    asyncio.run(_body())


def test_user_intent_watcher_only_processes_events_after_startup_cursor(
    runtime: JarvisRuntime,
) -> None:
    """A pre-startup surface.user_intent is NOT replayed (cursor anchors at MAX(id))."""
    captured: list[str] = []

    def _stub(_rt: JarvisRuntime, *, user_intent_event: Event, **__: object) -> None:
        captured.append(str(user_intent_event.payload["turn_id"]))

    async def _body() -> None:
        # Seed BEFORE the watcher starts.
        _seed_user_intent(runtime.conn, turn_id="T-prestart")

        with patch.object(inherent_loop, "drive_turn", _stub):
            task = asyncio.create_task(
                _user_intent_watcher(runtime, poll_interval_s=0.001),
            )
            await asyncio.sleep(0.02)
            # Now seed AFTER startup.
            _seed_user_intent(runtime.conn, turn_id="T-poststart")
            await _spin_briefly()
            await _cancel_and_await(task)

            assert captured == ["T-poststart"]

    asyncio.run(_body())


# ---------------------------------------------------------------------------
# _response_watcher — single cursor, type-dispatch (ADR-0003 D16)
# ---------------------------------------------------------------------------


def test_response_watcher_dispatches_response_open_to_broadcast_open(
    runtime: JarvisRuntime,
) -> None:
    """A new surface.response_open is forwarded to ``broadcast_open``."""
    fake = _FakeBroadcaster()

    async def _body() -> None:
        task = asyncio.create_task(
            _response_watcher(runtime, fake, poll_interval_s=0.001),  # type: ignore[arg-type]
        )
        await asyncio.sleep(0.02)
        seeded = _seed_response_open(runtime.conn, turn_id="T-o", query="hi there")
        await _spin_briefly()
        await _cancel_and_await(task)

        assert len(fake.received) == 1
        method, ev = fake.received[0]
        assert method == "open"
        assert ev.event_uid == seeded.event_uid
        assert ev.payload["query"] == "hi there"

    asyncio.run(_body())


def test_response_watcher_dispatches_response_chunk_to_broadcast_chunk(
    runtime: JarvisRuntime,
) -> None:
    """A new surface.response_chunk is forwarded to ``broadcast_chunk``."""
    fake = _FakeBroadcaster()

    async def _body() -> None:
        task = asyncio.create_task(
            _response_watcher(runtime, fake, poll_interval_s=0.001),  # type: ignore[arg-type]
        )
        await asyncio.sleep(0.02)
        seeded = _seed_response_chunk(runtime.conn, turn_id="T-c", text="tok")
        await _spin_briefly()
        await _cancel_and_await(task)

        assert len(fake.received) == 1
        method, ev = fake.received[0]
        assert method == "chunk"
        assert ev.event_uid == seeded.event_uid
        assert ev.payload["text"] == "tok"

    asyncio.run(_body())


def test_response_watcher_dispatches_response_emitted_to_broadcast_done(
    runtime: JarvisRuntime,
) -> None:
    """A new surface.response_emitted is forwarded to ``broadcast_done``."""
    fake = _FakeBroadcaster()

    async def _body() -> None:
        task = asyncio.create_task(
            _response_watcher(runtime, fake, poll_interval_s=0.001),  # type: ignore[arg-type]
        )
        await asyncio.sleep(0.02)
        seeded = _seed_response_emitted(runtime.conn, turn_id="T-d", text="final")
        await _spin_briefly()
        await _cancel_and_await(task)

        assert len(fake.received) == 1
        method, ev = fake.received[0]
        assert method == "done"
        assert ev.event_uid == seeded.event_uid

    asyncio.run(_body())


def test_response_watcher_skips_other_event_types(
    runtime: JarvisRuntime,
) -> None:
    """Events of other types never reach any broadcast_* method."""
    fake = _FakeBroadcaster()

    async def _body() -> None:
        task = asyncio.create_task(
            _response_watcher(runtime, fake, poll_interval_s=0.001),  # type: ignore[arg-type]
        )
        await asyncio.sleep(0.02)
        emit_event(
            runtime.conn,
            type="turn.started",
            payload={"turn_id": "T-noise"},
        )
        emit_event(
            runtime.conn,
            type="surface.user_intent",
            payload={
                "transcript": "noise",
                "turn_id": "T-noise",
                "channel": "cli_stdin",
                "language": "zh-CN",
            },
        )
        await _spin_briefly()
        await _cancel_and_await(task)

        assert fake.received == []

    asyncio.run(_body())


def test_response_watcher_only_processes_events_after_startup_cursor(
    runtime: JarvisRuntime,
) -> None:
    """Pre-startup response_{open,chunk,emitted} rows are NOT replayed."""
    fake = _FakeBroadcaster()

    async def _body() -> None:
        # Seed BEFORE the watcher starts (all three types).
        _seed_response_open(runtime.conn, turn_id="T-pre", query="old-q")
        _seed_response_chunk(runtime.conn, turn_id="T-pre", text="old-tok")
        _seed_response_emitted(runtime.conn, turn_id="T-pre", text="old-final")

        task = asyncio.create_task(
            _response_watcher(runtime, fake, poll_interval_s=0.001),  # type: ignore[arg-type]
        )
        await asyncio.sleep(0.02)
        # Now seed AFTER startup.
        _seed_response_emitted(runtime.conn, turn_id="T-post", text="new-final")
        await _spin_briefly()
        await _cancel_and_await(task)

        assert len(fake.received) == 1
        method, ev = fake.received[0]
        assert method == "done"
        assert ev.payload["turn_id"] == "T-post"

    asyncio.run(_body())


def test_response_watcher_single_cursor_preserves_l2_insertion_order(
    runtime: JarvisRuntime,
) -> None:
    """Mixed open/chunk/chunk/done rows are dispatched in their SQLite-id order.

    ADR-0003 D16: three sibling watchers (one per type) would race
    against asyncio's wakeup order; a single cursor + monotonic SQLite
    row id eliminates the race by construction. This test seeds all
    four rows in fast succession and asserts the broadcast_* method
    calls land in that exact ``[open, chunk, chunk, done]`` order.
    """
    fake = _FakeBroadcaster()

    async def _body() -> None:
        task = asyncio.create_task(
            _response_watcher(runtime, fake, poll_interval_s=0.001),  # type: ignore[arg-type]
        )
        await asyncio.sleep(0.02)
        # Fast succession, all on the watcher's runtime.conn (same
        # event-loop thread as the watcher's polling SELECTs).
        _seed_response_open(runtime.conn, turn_id="T-seq", query="hi")
        _seed_response_chunk(runtime.conn, turn_id="T-seq", text="hello ")
        _seed_response_chunk(runtime.conn, turn_id="T-seq", text="world")
        _seed_response_emitted(runtime.conn, turn_id="T-seq", text="hello world")
        await _spin_briefly()
        await _cancel_and_await(task)

        methods = [m for m, _ev in fake.received]
        assert methods == ["open", "chunk", "chunk", "done"]
        # Each event has the same turn_id so the dispatch belongs to one logical turn.
        for _m, ev in fake.received:
            assert ev.payload["turn_id"] == "T-seq"

    asyncio.run(_body())


# ---------------------------------------------------------------------------
# serve_inherent — task naming + watcher set
# ---------------------------------------------------------------------------


def test_serve_inherent_spawns_response_watcher_under_named_task(
    runtime: JarvisRuntime,
    tmp_path: Path,
) -> None:
    """``serve_inherent`` spawns ``_response_watcher`` (NOT ``_response_broadcaster``).

    Stubs ``uvicorn.Server.serve`` to a fast no-op so the entrypoint
    completes its setup, registers the named task, and then unwinds
    cleanly. Asserts the task name is ``"response_watcher"`` (not
    ``"response_broadcaster"``) and that ``_response_watcher`` is the
    target coroutine.
    """
    lock_path = tmp_path / "test-jarvis.lock"
    observed_task_names: list[str] = []

    async def _noop_serve(_self: uvicorn.Server) -> None:
        # Capture every running task whose name starts with a watcher
        # prefix, then return so the entrypoint's `finally` block tears
        # them down.
        for t in asyncio.all_tasks():
            name = t.get_name()
            if name in {"user_intent_watcher", "response_watcher", "response_broadcaster"}:
                observed_task_names.append(name)

    async def _body() -> None:
        with patch("uvicorn.Server.serve", _noop_serve):
            await inherent_loop.serve_inherent(
                runtime,
                host="127.0.0.1",
                port=0,
                lock_path=lock_path,
                poll_interval_s=0.001,
            )

        assert "response_watcher" in observed_task_names
        assert "user_intent_watcher" in observed_task_names
        assert "response_broadcaster" not in observed_task_names

    asyncio.run(_body())


# ---------------------------------------------------------------------------
# serve_inherent — lock contention surfaces ProcessLockHeld
# ---------------------------------------------------------------------------


def test_serve_inherent_raises_process_lock_held_when_lock_taken(
    runtime: JarvisRuntime,
    tmp_path: Path,
) -> None:
    """Pre-acquired lock => ProcessLockHeld propagates out of serve_inherent.

    The full uvicorn lifecycle is covered by Step 9's integration test.
    Here we only assert that the lock-contention branch surfaces the
    spec-mandated exception before any watchers are spawned.

    Because the test process already holds the flock via
    :func:`acquire_exclusive`, the second attempt inside
    :func:`serve_inherent` recovers stale-pid (our own pid IS live, so
    the stale-recovery branch yields ``ProcessLockHeld(holder_pid=our_pid)``).
    """
    lock_path = tmp_path / "test-jarvis.lock"

    async def _body() -> None:
        # Outer test-process holds the lock; serve_inherent must refuse.
        with acquire_exclusive(lock_path), pytest.raises(ProcessLockHeld) as exc_info:
            await inherent_loop.serve_inherent(
                runtime,
                host="127.0.0.1",
                port=0,  # avoid binding a real port — we never reach uvicorn
                lock_path=lock_path,
                poll_interval_s=0.001,
            )
        # holder_pid is the live process that owns the lock — ourselves.
        assert exc_info.value.holder_pid == os.getpid()

    asyncio.run(_body())


# ---------------------------------------------------------------------------
# _fetch_events_after smoke
# ---------------------------------------------------------------------------


def test_fetch_events_after_returns_only_matching_type_in_id_order(
    runtime: JarvisRuntime,
) -> None:
    """_fetch_events_after filters by type tuple and respects after_id.

    ADR-0005 §5.1: the helper now accepts ``event_types: tuple[str, ...]``
    so the inherent-loop user-intent watcher can fold both
    ``surface.user_intent`` and ``utterance.received`` into one cursor.
    A single-element tuple preserves the original single-type behavior.
    """
    e1 = emit_event(
        runtime.conn,
        type="surface.user_intent",
        payload={
            "transcript": "a",
            "turn_id": "T1",
            "channel": "cli_stdin",
            "language": "zh-CN",
        },
    )
    emit_event(  # noise type — must be skipped
        runtime.conn,
        type="turn.started",
        payload={"turn_id": "T1"},
    )
    e2 = emit_event(
        runtime.conn,
        type="surface.user_intent",
        payload={
            "transcript": "b",
            "turn_id": "T2",
            "channel": "cli_stdin",
            "language": "zh-CN",
        },
    )

    results = _fetch_events_after(
        runtime.conn,
        after_id=0,
        event_types=("surface.user_intent",),
    )
    assert [ev.event_uid for _id, ev in results] == [e1.event_uid, e2.event_uid]
    # id values are positive and strictly increasing.
    ids = [row_id for row_id, _ev in results]
    assert ids[0] < ids[1]
    assert ids[0] > 0

    # after_id past the first row => only the second is returned.
    # The first surface.user_intent has id 1 (turn.started has id 2,
    # second surface.user_intent has id 3); calling with after_id=1 must
    # return only the third row.
    latest = _latest_id(runtime.conn)
    only_second = _fetch_events_after(
        runtime.conn,
        after_id=latest - 1,
        event_types=("surface.user_intent",),
    )
    assert [ev.event_uid for _id, ev in only_second] == [e2.event_uid]
