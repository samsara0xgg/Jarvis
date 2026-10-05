"""The shared decision-state cache tracks the log across threads and drops a wrong state."""

from __future__ import annotations

import contextlib
import dataclasses
import threading
from functools import partial
from typing import TYPE_CHECKING

from jarvis.decision.packet import assemble_packet
from jarvis.runtime import decision_state
from jarvis.runtime.decision_state import DecisionStateCache
from jarvis.state.decision_snapshot import read_decision_snapshot
from jarvis.state.event_log import (
    EventTypeRegistry,
    emit_event,
    iter_events_after,
    open_event_log,
)
from jarvis.state.projections import ProjectionFold, fold_projections
from jarvis.state.turn_overlap import turn_activity_since
from tests.integration.test_incremental_decision_snapshot import _Log

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    import pytest

    from jarvis.state.decision_snapshot import DecisionFoldState


def test_threads_with_their_own_connections_each_get_the_log_at_their_cursor(
    tmp_path: Path,
) -> None:
    """Two readers and a writer race; every snapshot equals a cold read of the same cursor."""
    path = tmp_path / "events.db"
    writer = open_event_log(path)
    cache = DecisionStateCache(partial(open_event_log, path), verify_every_reads=0)
    failures: list[str] = []
    done = threading.Event()

    def _read() -> None:
        conn = open_event_log(path)
        try:
            while not done.is_set():
                snapshot = cache.read(conn)
                # The log only grows, so its first rows up to this cursor never change.
                prefix = [e for _, e in iter_events_after(conn, 0, snapshot.event_cursor)]
                if snapshot.projections != fold_projections(prefix):
                    failures.append(f"differs at {snapshot.event_cursor}")
        finally:
            conn.close()

    with contextlib.closing(writer):
        log = _Log(writer, seed=11)
        readers = [threading.Thread(target=_read) for _ in range(2)]
        for thread in readers:
            thread.start()
        for _ in range(150):
            log.step()
        done.set()
        for thread in readers:
            thread.join(timeout=30)
        assert failures == []
        assert cache.read(writer).event_cursor == read_decision_snapshot(writer).event_cursor
        assert cache.cursor == read_decision_snapshot(writer).event_cursor
        assert cache.agrees_with_whole_log()


def test_a_state_that_differs_from_the_log_is_dropped_by_the_self_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fold that lost events fails the check, and the cache then folds the whole log."""
    path = tmp_path / "events.db"
    conn = open_event_log(path)
    with contextlib.closing(conn):
        log = _Log(conn, seed=5)
        for _ in range(60):
            log.step()
        original = read_decision_snapshot
        corrupt = [True]

        def lossy(conn: sqlite3.Connection, prior: DecisionFoldState | None = None) -> object:
            snapshot = original(conn, prior)
            if corrupt[0] and snapshot.fold_state is not None:
                # The same cursor and anchor, but the projections of an empty log.
                lost = dataclasses.replace(snapshot.fold_state, projections=ProjectionFold())
                return dataclasses.replace(snapshot, fold_state=lost)
            return snapshot

        monkeypatch.setattr(decision_state, "read_decision_snapshot", lossy)
        cache = DecisionStateCache(partial(open_event_log, path), verify_every_reads=0)
        held = cache.read(conn).event_cursor
        corrupt[0] = False
        assert cache.cursor == held
        assert not cache.agrees_with_whole_log()
        cache._verify()  # noqa: SLF001 - the background thread's body, run inline
        assert cache.cursor != held
        assert cache.read(conn).projections == original(conn).projections
        assert cache.agrees_with_whole_log()


def test_a_packet_reads_through_the_turns_reader_and_matches_the_cold_packet(
    tmp_path: Path,
) -> None:
    """The second packet of a turn costs a delta: same packet as a cold read, one shared state."""
    path = tmp_path / "events.db"
    conn = open_event_log(path)
    with contextlib.closing(conn):
        log = _Log(conn, seed=2)
        for _ in range(50):
            log.step()
        trigger = emit_event(
            conn, type="utterance.received", payload={"turn_id": "now", "transcript": "hi"}
        )
        cache = DecisionStateCache(partial(open_event_log, path), verify_every_reads=0)
        first = assemble_packet(trigger, conn, cache.read)
        emit_event(
            conn,
            type="response.started",
            payload={
                key: "now" if key == "turn_id" else "x"
                for key in EventTypeRegistry.requires("response.started")
            },
        )
        second = assemble_packet(trigger, conn, cache.read)
        cold = assemble_packet(trigger, conn)
        assert first.event_cursor is not None
        assert cold.event_cursor is not None
        assert first.event_cursor == cold.event_cursor - 1
        assert second == cold
        assert cache.cursor == cold.event_cursor


def test_the_self_check_waits_until_the_daemon_is_idle(tmp_path: Path) -> None:
    """A due check never compares while a turn works or speaks; it runs once idle."""
    path = tmp_path / "events.db"
    conn = open_event_log(path)
    with contextlib.closing(conn):
        log = _Log(conn, seed=8)
        for _ in range(40):
            log.step()
        busy = [True]
        probes = []
        compared = threading.Event()

        def is_idle() -> bool:
            probes.append(busy[0])
            return not busy[0]

        cache = DecisionStateCache(
            partial(open_event_log, path),
            verify_every_reads=2,
            is_idle=is_idle,
            idle_poll_s=0.01,
        )
        original = cache.agrees_with_whole_log

        def compare() -> bool:
            assert not busy[0], "compared while a turn was in flight"
            compared.set()
            return original()

        cache.agrees_with_whole_log = compare  # type: ignore[method-assign]
        cache.read(conn)
        cache.read(conn)  # the second read is due
        assert not compared.wait(0.3)
        assert len(probes) > 3  # it kept asking, and kept waiting
        busy[0] = False
        assert compared.wait(5)


def test_the_idle_probe_sees_turns_and_playback(tmp_path: Path) -> None:
    """In flight, or a row of a turn or its playback in the last seconds, is not idle."""
    conn = open_event_log(tmp_path / "events.db")
    with contextlib.closing(conn):
        assert not turn_activity_since(conn, since_ms=0)
        emit_event(
            conn,
            type="surface.playback_checkpoint",
            payload=dict.fromkeys(EventTypeRegistry.requires("surface.playback_checkpoint"), "x"),
        )
        assert turn_activity_since(conn, since_ms=0)
        assert not turn_activity_since(conn, since_ms=10**13)
