"""A seeded random mix of everything the projections fold."""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, Any

from jarvis.state.event_log import EventTypeRegistry, emit_event

if TYPE_CHECKING:
    import sqlite3

    from jarvis.shared import Event

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


def emit_row(
    conn: sqlite3.Connection,
    type_: str,
    source: str | None = None,
    **payload: Any,  # noqa: ANN401 - payload values are whatever the event type takes
) -> Event:
    """Emit with every registry-required field present, so the test names only what folds."""
    for key in EventTypeRegistry.requires(type_):
        payload.setdefault(key, "x")
    return emit_event(conn, type=type_, payload=payload, source_event_id=source)


class RandomLog:
    """Appends a seeded random mix of everything the projections fold."""

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
        emit_row(
            self.conn,
            self.rng.choice(("utterance.received", "surface.user_intent")),
            turn_id=turn,
            transcript="hello " + turn,
        )
        if self.rng.random() < 0.05:
            emit_row(self.conn, "utterance.received", turn_id=turn, transcript="again")

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
        event = emit_row(self.conn, type_, source, **fields)
        if type_ == "surface.playback_started":
            self.activations.append(event)

    def _board(self) -> None:
        rng = self.rng
        repo = "/r/" + str(rng.randrange(3))
        kind = rng.randrange(4)
        if kind == 0:
            emit_row(
                self.conn,
                "repo.state_observed",
                repo_path=repo,
                dirty_file_count=rng.randrange(9),
                observed_at_ms=rng.randrange(10**6),
            )
        elif kind == 1:
            emit_row(
                self.conn,
                "project.commit_seen",
                repo_path=repo,
                committed_at_ms=rng.randrange(10**6),
            )
        elif kind == 2:
            emit_row(self.conn, "mac.sleeping", ts_epoch_ms=rng.randrange(10**6))
        else:
            emit_row(self.conn, "mac.awake", ts_epoch_ms=rng.randrange(10**6), slept_for_ms=5)

    def _action(self) -> None:
        action = self.rng.choice(self.actions)
        if self.rng.random() < 0.6:
            gate = self.rng.choice([None, *self.requests, None])
            emit_row(
                self.conn, "action.dispatched", gate.event_uid if gate else None, action_id=action
            )
        else:
            emit_row(self.conn, self.rng.choice(_TERMINAL), action_id=action, semantics="x")

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
        emit_row(self.conn, "gate.evaluated", source, **fields)

    def _confirmation(self) -> None:
        malformed = self.rng.random() < 0.15
        event = emit_row(
            self.conn,
            "confirmation.requested",
            confirmation_id=self._id("C") if self.rng.random() < 0.95 else "C1",
            action_snapshot="bad" if malformed else {"tool_name": "open_path", "args_meta": {}},
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
        emit_row(self.conn, type_, request.event_uid, confirmation_id=cid, **extra)

    def _clarification(self) -> None:
        rng = self.rng
        card = "K" + str(rng.randrange(3))
        kind = rng.randrange(3)
        if kind == 0:
            emit_row(
                self.conn,
                "clarification.requested",
                clarification_id=card,
                question="q",
                fields=[{"label": "a"}],
                turn_id=self._id("T"),
            )
        else:
            emit_row(
                self.conn,
                rng.choice(("surface.clarified", "surface.dismissed", "clarification.withdrawn")),
                clarification_id=card,
                turn_id=self._id("T"),
                answers={"a": 1} if kind == 1 else {},
            )
