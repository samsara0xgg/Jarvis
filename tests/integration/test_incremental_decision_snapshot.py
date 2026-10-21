"""The decision snapshot folds the log from a high-water mark and equals the whole-log fold."""

from __future__ import annotations

import contextlib
import itertools
import random
from typing import TYPE_CHECKING

import pytest

from jarvis.state.decision_snapshot import read_decision_snapshot
from jarvis.state.event_log import (
    iter_events,
    open_event_log,
)
from jarvis.state.projections import ProjectionFold, fold_projections
from tests.support.random_log import RandomLog, emit_row

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence
    from pathlib import Path

    from jarvis.shared import Event

_SEEDS = (1, 2, 3, 4, 5, 6)
_STEPS = 420


def _build(path: Path, seed: int) -> tuple[sqlite3.Connection, list[Event]]:
    conn = open_event_log(path)
    log = RandomLog(conn, seed)
    for _ in range(_STEPS):
        log.step()
    return conn, list(iter_events(conn))


def _splits(rng: random.Random, size: int) -> list[int]:
    """Several split points, always including the extremes and a hand-off at every 40th."""
    return sorted({0, size, *rng.sample(range(size + 1), 12), *range(0, size, 40)})


@pytest.mark.parametrize("seed", _SEEDS)
def test_incremental_fold_equals_whole_log_fold_at_every_split(tmp_path: Path, seed: int) -> None:
    """Folding a log in any number of consecutive parts is folding it whole."""
    conn, events = _build(tmp_path / "events.db", seed)
    with contextlib.closing(conn):
        whole = fold_projections(events)
        rng = random.Random(seed)  # noqa: S311 - fixed seed, not a secret
        cuts = _splits(rng, len(events))
        # One fold hand-fed in many parts, and several folds each split once or twice.
        stepped = ProjectionFold()
        for lo, hi in itertools.pairwise(cuts):
            stepped = stepped.advance(events[lo:hi])
        assert stepped.projections() == whole
        for cut in rng.sample(cuts, 6):
            parts = [events[:cut]]
            mid = rng.randrange(cut, len(events) + 1)
            parts += [events[cut:mid], events[mid:]]
            fold = ProjectionFold()
            for part in parts:
                fold = fold.advance(part)
            assert fold.projections() == whole
        # The generator must have exercised the hard parts, or this proves little.
        assert len(whole.conversation_history.turns) == 20
        assert whole.conversation_history.truncated
        assert whole.pending_confirmations.consumed_lease_ids
        assert whole.status_board.open_actions
        assert whole.status_board.repos


@pytest.mark.parametrize("seed", _SEEDS[:3])
def test_advance_never_changes_the_fold_it_started_from(tmp_path: Path, seed: int) -> None:
    """A shared fold is safe to advance from any thread: the original keeps its value."""
    conn, events = _build(tmp_path / "events.db", seed)
    with contextlib.closing(conn):
        half = len(events) // 2
        base = ProjectionFold().advance(events[:half])
        before = base.projections()
        base.advance(events[half:])
        base.advance(events[half : half + 30])
        assert base.projections() == before
        assert base.projections() == fold_projections(events[:half])


def test_snapshot_reads_only_new_rows_and_equals_a_full_read(tmp_path: Path) -> None:
    """A reader with its prior state sees what a cold reader sees, at every step."""
    conn = open_event_log(tmp_path / "events.db")
    with contextlib.closing(conn):
        log = RandomLog(conn, seed=9)
        state = None
        for _ in range(8):
            for _ in range(60):
                log.step()
            warm = read_decision_snapshot(conn, state)
            cold = read_decision_snapshot(conn)
            assert warm.event_cursor == cold.event_cursor
            assert warm.projections == cold.projections
            assert warm.fold_state is not None
            assert warm.fold_state.cursor == warm.event_cursor
            state = warm.fold_state


def test_a_prior_from_the_future_or_another_log_is_not_used(tmp_path: Path) -> None:
    """A view older than the prior, or a different log, folds from the start."""
    first = open_event_log(tmp_path / "a.db")
    second = open_event_log(tmp_path / "b.db")
    other = open_event_log(tmp_path / "a.db")
    with contextlib.closing(first), contextlib.closing(second), contextlib.closing(other):
        log = RandomLog(first, seed=3)
        for _ in range(40):
            log.step()
        old_view = read_decision_snapshot(first)
        other.execute("BEGIN")
        other.execute("SELECT COUNT(*) FROM events").fetchone()  # pin the older view
        for _ in range(40):
            log.step()
        newer = read_decision_snapshot(first).fold_state
        assert newer is not None
        pinned = read_decision_snapshot(other, newer)
        assert pinned.event_cursor == old_view.event_cursor
        assert pinned.projections == old_view.projections
        other.rollback()
        # A different log with enough rows that the prior's cursor exists in it.
        other_log = RandomLog(second, seed=4)
        for _ in range(120):
            other_log.step()
        foreign = read_decision_snapshot(second, newer)
        assert foreign.projections == read_decision_snapshot(second).projections


def test_an_input_after_dropped_outputs_falls_back_to_the_whole_log(tmp_path: Path) -> None:
    """Outputs that precede their turn's input are what only the whole-log fold orders."""
    conn = open_event_log(tmp_path / "events.db")
    with contextlib.closing(conn):
        emit_row(
            conn, "surface.response_chunk", turn_id="late", response_id="R", text="a", sequence=0
        )
        first = read_decision_snapshot(conn)
        assert first.fold_state is not None
        emit_row(conn, "utterance.received", turn_id="late", transcript="hi")
        warm = read_decision_snapshot(conn, first.fold_state)
        events: Sequence[Event] = list(iter_events(conn))
        assert warm.projections == fold_projections(events)
        assert warm.projections.conversation_history.turns[0].responses
        assert warm.fold_state is None
