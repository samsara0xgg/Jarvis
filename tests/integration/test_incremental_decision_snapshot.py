"""The decision snapshot folds the log from a high-water mark and equals the whole-log fold."""

from __future__ import annotations

import contextlib
import itertools
import random
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.state.authorization_snapshot import AuthorizationFacts, read_authorization_snapshot
from jarvis.state.authorized_dispatch_outbox import ensure_authorized_dispatch_schema
from jarvis.state.decision_snapshot import read_decision_snapshot
from jarvis.state.event_log import (
    EventTypeRegistry,
    emit_event,
    iter_events,
    open_event_log,
)
from jarvis.state.projections import ProjectionFold, fold_projections
from tests.integration.test_authorization_snapshot import _seed_debt

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence
    from pathlib import Path

    from jarvis.shared import Event

_SEEDS = (1, 2, 3, 4, 5, 6)
_STEPS = 420
_TERMINAL = (
    "action.result_observed",
    "action.failed",
    "action.timeout_assumed",
    "action.cancelled",
)
_OUTPUT = (
    "response.started",
    "response.completed",
    "surface.response_open",
    "surface.response_chunk",
    "surface.response_emitted",
    "surface.playback_started",
    "surface.playback_segment_prepared",
    "surface.playback_alignment",
    "surface.playback_checkpoint",
    "surface.playback_completed",
    "surface.playback_interrupted",
    "surface.playback_failed",
    "surface.speech_dropped",
)


def _emit(
    conn: sqlite3.Connection,
    type_: str,
    source: str | None = None,
    **payload: Any,  # noqa: ANN401 - payload values are whatever the event type takes
) -> Event:
    """Emit with every registry-required field present, so the test names only what folds."""
    for key in EventTypeRegistry.requires(type_):
        payload.setdefault(key, "x")
    return emit_event(conn, type=type_, payload=payload, source_event_id=source)


class _Log:
    """Appends a seeded random mix of everything the projections and facts fold."""

    def __init__(self, conn: sqlite3.Connection, seed: int) -> None:
        self.conn = conn
        self.rng = random.Random(seed)  # noqa: S311 - fixed seed, not a secret
        self.turns: list[str] = []
        self.requests: list[Event] = []
        self.actions = [f"A{i}" for i in range(6)]
        self.counter = 0
        self.activations: list[Event] = []

    def _id(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}{self.counter}"

    def step(self) -> None:
        kinds = (
            (self._turn, 4),
            (self._output, 12),
            (self._board, 3),
            (self._action, 5),
            (self._gate, 3),
            (self._confirmation, 4),
            (self._answer, 3),
            (self._clarification, 2),
        )
        pick = self.rng.choices([k for k, _ in kinds], [w for _, w in kinds])[0]
        pick()

    def _turn(self) -> None:
        turn = self._id("T")
        self.turns.append(turn)
        # Repeats of an input must stay consistent when they are the same event, and
        # a second event for the same turn is the inconsistent case.
        _emit(
            self.conn,
            self.rng.choice(("utterance.received", "surface.user_intent")),
            turn_id=turn,
            transcript="hello " + turn,
        )
        if self.rng.random() < 0.05:
            _emit(self.conn, "utterance.received", turn_id=turn, transcript="again")

    def _output(self) -> None:
        rng = self.rng
        if self.turns and rng.random() < 0.85:
            turn = rng.choice(self.turns[-24:])
        else:
            turn = "O" + str(rng.randrange(4))  # an output whose turn never has an input
        response = "R" + turn + "-" + str(rng.randrange(10))
        fields: dict[str, Any] = {
            "turn_id": turn,
            "session_id": "S",
            "playback_generation_id": rng.randrange(1, 3),
            "sequence": rng.randrange(3),
            "heard_through_sequence": rng.randrange(3),
            "submitted_samples": rng.randrange(1000),
            "word_boundaries": [],
        }
        type_ = rng.choice(_OUTPUT)
        if rng.random() < 0.9:
            fields["response_id"] = response
        elif "response_id" in EventTypeRegistry.requires(type_) or rng.random() < 0.5:
            fields["response_id"] = ""  # the legacy id, one per turn
        if type_ in ("surface.response_emitted", "surface.response_chunk"):
            fields["text"] = rng.choice(("Ice.", "Ice melts.", " Water."))
        source = None
        if type_.startswith("surface.playback_") and type_ != "surface.playback_started":
            source = rng.choice(self.activations).event_uid if self.activations else None
        event = _emit(self.conn, type_, source, **fields)
        if type_ == "surface.playback_started":
            self.activations.append(event)

    def _board(self) -> None:
        rng = self.rng
        repo = "/r/" + str(rng.randrange(3))
        kind = rng.randrange(4)
        if kind == 0:
            _emit(
                self.conn,
                "repo.state_observed",
                repo_path=repo,
                dirty_file_count=rng.randrange(9),
                observed_at_ms=rng.randrange(10**6),
            )
        elif kind == 1:
            _emit(
                self.conn,
                "project.commit_seen",
                repo_path=repo,
                committed_at_ms=rng.randrange(10**6),
            )
        elif kind == 2:
            _emit(self.conn, "mac.sleeping", ts_epoch_ms=rng.randrange(10**6))
        else:
            _emit(self.conn, "mac.awake", ts_epoch_ms=rng.randrange(10**6), slept_for_ms=5)

    def _action(self) -> None:
        action = self.rng.choice(self.actions)
        if self.rng.random() < 0.6:
            gate = self.rng.choice([None, *self.requests, None])
            _emit(
                self.conn, "action.dispatched", gate.event_uid if gate else None, action_id=action
            )
        else:
            _emit(self.conn, self.rng.choice(_TERMINAL), action_id=action, semantics="x")

    def _gate(self) -> None:
        rng = self.rng
        fields: dict[str, Any] = {
            "gate": rng.choice(("pre_action", "pre_emit")),
            "outcome": rng.choice(("pass", "pass", "refuse")),
            "reasons": [],
            "action_id": rng.choice(self.actions),
        }
        if rng.random() < 0.5:
            fields["lease_id"] = rng.choice(("L1", "L2", "L3"))
        source = (
            rng.choice(self.requests).event_uid if self.requests and rng.random() < 0.3 else None
        )
        _emit(self.conn, "gate.evaluated", source, **fields)

    def _confirmation(self) -> None:
        malformed = self.rng.random() < 0.15
        event = _emit(
            self.conn,
            "confirmation.requested",
            confirmation_id=self._id("C") if self.rng.random() < 0.95 else "C1",
            action_snapshot="bad" if malformed else {"tool_name": "write_file", "args_meta": {}},
            template_line="ok",
            expires_at_ms=10**9,
        )
        self.requests.append(event)

    def _answer(self) -> None:
        if not self.requests:
            return
        rng = self.rng
        request = rng.choice(self.requests[-3:])
        cid = request.payload["confirmation_id"] if rng.random() < 0.9 else "other"
        type_ = rng.choice(
            ("confirmation.accepted", "confirmation.rejected", "confirmation.expired")
        )
        extra = {"expired_at_ms": 1} if type_ == "confirmation.expired" else {}
        _emit(self.conn, type_, request.event_uid, confirmation_id=cid, **extra)

    def _clarification(self) -> None:
        rng = self.rng
        card = "K" + str(rng.randrange(3))
        kind = rng.randrange(3)
        if kind == 0:
            _emit(
                self.conn,
                "clarification.requested",
                clarification_id=card,
                question="q",
                fields=[{"label": "a"}],
                turn_id=self._id("T"),
            )
        else:
            _emit(
                self.conn,
                rng.choice(("surface.clarified", "surface.dismissed")),
                clarification_id=card,
                turn_id=self._id("T"),
                answers={"a": 1} if kind == 1 else {},
            )


def _build(path: Path, seed: int) -> tuple[sqlite3.Connection, list[Event]]:
    conn = open_event_log(path)
    log = _Log(conn, seed)
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
        facts = AuthorizationFacts()
        for lo, hi in itertools.pairwise(cuts):
            stepped = stepped.advance(events[lo:hi])
            facts = facts.advance(events[lo:hi])
        assert stepped.projections() == whole
        conn.execute("BEGIN")
        expected = read_authorization_snapshot(conn, AuthorizationFacts().advance(events))
        assert read_authorization_snapshot(conn, facts) == expected
        for cut in rng.sample(cuts, 6):
            parts = [events[:cut]]
            mid = rng.randrange(cut, len(events) + 1)
            parts += [events[cut:mid], events[mid:]]
            fold, auth = ProjectionFold(), AuthorizationFacts()
            for part in parts:
                fold, auth = fold.advance(part), auth.advance(part)
            assert fold.projections() == whole
            assert read_authorization_snapshot(conn, auth) == expected
        conn.rollback()
        # The generator must have exercised the hard parts, or this proves little.
        assert len(whole.conversation_history.turns) == 20
        assert whole.conversation_history.truncated
        assert whole.pending_confirmations.consumed_lease_ids
        assert whole.action_admissions.by_action_id
        assert whole.status_board.repos
        assert len(expected.confirmations) > 5
        assert expected.errors


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
        log = _Log(conn, seed=9)
        state = None
        for _ in range(8):
            for _ in range(60):
                log.step()
            warm = read_decision_snapshot(conn, state)
            cold = read_decision_snapshot(conn)
            assert warm.event_cursor == cold.event_cursor
            assert warm.projections == cold.projections
            assert warm.authorizations == cold.authorizations
            assert warm.fold_state is not None
            assert warm.fold_state.cursor == warm.event_cursor
            state = warm.fold_state


def test_a_prior_from_the_future_or_another_log_is_not_used(tmp_path: Path) -> None:
    """A view older than the prior, or a different log, folds from the start."""
    first = open_event_log(tmp_path / "a.db")
    second = open_event_log(tmp_path / "b.db")
    other = open_event_log(tmp_path / "a.db")
    with contextlib.closing(first), contextlib.closing(second), contextlib.closing(other):
        log = _Log(first, seed=3)
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
        assert pinned.authorizations == old_view.authorizations
        other.rollback()
        # A different log with enough rows that the prior's cursor exists in it.
        other_log = _Log(second, seed=4)
        for _ in range(120):
            other_log.step()
        foreign = read_decision_snapshot(second, newer)
        assert foreign.projections == read_decision_snapshot(second).projections


def test_an_input_after_dropped_outputs_falls_back_to_the_whole_log(tmp_path: Path) -> None:
    """Outputs that precede their turn's input are what only the whole-log fold orders."""
    conn = open_event_log(tmp_path / "events.db")
    with contextlib.closing(conn):
        _emit(conn, "surface.response_chunk", turn_id="late", response_id="R", text="a", sequence=0)
        first = read_decision_snapshot(conn)
        assert first.fold_state is not None
        _emit(conn, "utterance.received", turn_id="late", transcript="hi")
        warm = read_decision_snapshot(conn, first.fold_state)
        events: Sequence[Event] = list(iter_events(conn))
        assert warm.projections == fold_projections(events)
        assert warm.projections.conversation_history.turns[0].responses
        assert warm.fold_state is None


def test_dispatch_debt_checks_agree_between_warm_and_cold_reads(tmp_path: Path) -> None:
    """The operational tables join the folded events afresh: every step, warm equals cold."""
    conn = open_event_log(tmp_path / "events.db")
    with contextlib.closing(conn):
        ensure_authorized_dispatch_schema(conn)
        dispatch = _seed_debt(conn)
        state = read_decision_snapshot(conn).fold_state
        assert state is not None
        steps = (
            ("action.dispatched", {"action_id": "foreign"}, dispatch.gate_event.event_uid),
            ("action.dispatched", {"action_id": dispatch.identity.action_id}, None),
            (
                "gate.evaluated",
                {"gate": "pre_action", "outcome": "pass", "reasons": [], "lease_id": "L"},
                dispatch.identity.source_confirmation_event_id,
            ),
        )
        seen: set[tuple[str, ...]] = set()
        for type_, payload, source in steps:
            emit_event(conn, type=type_, payload=payload, source_event_id=source)
            warm = read_decision_snapshot(conn, state)
            cold = read_decision_snapshot(conn)
            assert warm.authorizations == cold.authorizations
            assert warm.projections == cold.projections
            seen.add(warm.authorizations.errors)
            assert warm.fold_state is not None
            state = warm.fold_state
        assert len(seen) == len(steps)  # each step raised a different set of errors
