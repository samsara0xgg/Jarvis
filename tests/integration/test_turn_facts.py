"""What the Event Log says about a turn, as L2 answers it (ADR 0349).

The runtime asks these questions while it drives, cancels and comments on turns. Each test writes
the rows a turn leaves in a real on-disk Event Log and asserts the answer; times are fixed epoch
milliseconds, never the wall clock.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.state.event_log import latest_id
from jarvis.state.turn_facts import (
    answer_started,
    answer_stopped_by_him,
    answer_superseded,
    card_requested_by_turn,
    clarification_asked_at,
    clarifications_asked_in_turn,
    commentary_runs_opened,
    heard_as_barge_pause,
    input_channels,
    latest_input_row,
    playback_ended,
    recent_turn_timings,
    spoken_since,
    turn_cancelled_or_failed,
    turn_dispatched_action,
    turn_ended_or_failed,
    turn_has_run,
    turn_is_over,
    turn_over_or_answering,
    turn_trigger,
    turns_closed_since,
    turns_dispatching_since,
    turns_of_his_words_since,
)
from tests.support.turn_rows import TurnLog

if TYPE_CHECKING:
    from pathlib import Path

T = "T1"
Writer = Callable[[TurnLog], object]


def _playback(log: TurnLog, turn_id: str, phase: str) -> None:
    log.put(
        "surface.playback_started",
        {
            "session_id": "S1", "response_id": "R1", "turn_id": turn_id,
            "playback_generation_id": "P1", "phase": phase, "channel": "both",
            "speech_text_hash": "h",
        },
        turn_id=turn_id,
    )


def _chunk(log: TurnLog, turn_id: str, phase: str) -> None:
    log.put(
        "surface.response_chunk", {"turn_id": turn_id, "text": "Hi", "phase": phase},
        turn_id=turn_id,
    )


def _dispatched(log: TurnLog, turn_id: str | None, at: int, action_id: str = "A1") -> None:
    log.put("action.dispatched", {"action_id": action_id}, turn_id=turn_id, at=at)


# Where a turn stands: each case is the rows of one turn, and what the log says of it.
# (ended or failed, answer stopped by him, answer started, over or answering, over)
STANDING: dict[str, tuple[Writer, tuple[bool, bool, bool, bool, bool]]] = {
    "nothing yet": (lambda _log: None, (False, False, False, False, False)),
    "a run open": (lambda log: log.run(T, "R1"), (False, False, False, False, False)),
    "turn ended": (lambda log: log.turn_ended(T), (True, False, False, True, True)),
    "turn failed": (lambda log: log.turn_failed(T), (True, False, False, True, True)),
    "answer stopped by his next words": (
        lambda log: (log.run(T, "R1"), log.run_ends("response.cancelled", T, "R1", "superseded")),
        (False, True, False, True, True),
    ),
    "answer stopped by his barge-in": (
        lambda log: (log.run(T, "R1"), log.run_ends("response.cancelled", T, "R1", "barge_in")),
        (False, True, False, True, True),
    ),
    "answer stopped by the stop button": (
        lambda log: (log.run(T, "R1"), log.run_ends("response.cancelled", T, "R1", "user_stop")),
        (False, True, False, True, True),
    ),
    "a wait line taken back is not his stop": (
        lambda log: (
            log.run(T, "W1", "commentary"),
            log.run_ends("response.cancelled", T, "W1", "answer_started"),
        ),
        (False, False, False, False, False),
    ),
    "answer opened on a surface": (
        lambda log: (log.run(T, "R1"), log.opened(T, "R1")),
        (False, False, True, True, False),
    ),
    "answer segment streamed": (
        lambda log: _chunk(log, T, "final"), (False, False, True, True, False),
    ),
    "answer heard": (
        lambda log: _playback(log, T, "final"), (False, False, True, True, False),
    ),
    "wait line opened": (
        lambda log: (log.run(T, "W1", "commentary"), log.opened(T, "W1", "commentary")),
        (False, False, False, False, False),
    ),
    "wait line streamed": (
        lambda log: _chunk(log, T, "commentary"), (False, False, False, False, False),
    ),
    "wait line heard": (
        lambda log: _playback(log, T, "commentary"), (False, False, False, False, False),
    ),
    "final run completed": (
        lambda log: (log.run(T, "R1"), log.run_ends("response.completed", T, "R1")),
        (False, False, False, False, True),
    ),
    "final run failed": (
        lambda log: (log.run(T, "R1"), log.run_ends("response.failed", T, "R1", "oops")),
        (False, False, False, False, True),
    ),
    "wait line completed, answer still open": (
        lambda log: (
            log.run(T, "W1", "commentary"), log.run_ends("response.completed", T, "W1"),
            log.run(T, "R1"),
        ),
        (False, False, False, False, False),
    ),
    "rows of another turn": (
        lambda log: (log.turn_ended("T2"), log.run("T2", "R9"), log.opened("T2", "R9")),
        (False, False, False, False, False),
    ),
}


@pytest.mark.parametrize("case", list(STANDING), ids=list(STANDING))
def test_where_a_turn_stands(tmp_path: Path, case: str) -> None:
    """The questions about one turn's lifecycle, answered for each shape of its rows."""
    write, expected = STANDING[case]
    log = TurnLog(tmp_path)
    write(log)
    assert (
        turn_ended_or_failed(log.conn, T),
        answer_stopped_by_him(log.conn, T),
        answer_started(log.conn, T),
        turn_over_or_answering(log.conn, T),
        turn_is_over(log.conn, T),
    ) == expected


def test_a_turn_has_run_once_a_run_opened_for_it(tmp_path: Path) -> None:
    """``response.started`` of the turn, and no other turn's, is what makes it have run."""
    log = TurnLog(tmp_path)
    log.run("T2", "R9")
    assert not turn_has_run(log.conn, T)
    log.run(T, "R1")
    assert turn_has_run(log.conn, T)


def test_commentary_runs_are_counted_per_turn(tmp_path: Path) -> None:
    """Wait-line runs of the turn count; its answer run and other turns' lines do not."""
    log = TurnLog(tmp_path)
    log.run(T, "R1")
    log.run("T2", "W9", "commentary")
    assert commentary_runs_opened(log.conn, T) == 0
    log.run(T, "W1", "commentary")
    log.run(T, "W2", "commentary")
    assert commentary_runs_opened(log.conn, T) == 2


def test_a_turn_is_working_once_it_dispatched_an_action(tmp_path: Path) -> None:
    """The dispatch is named by correlation; a dispatch with no turn belongs to none."""
    log = TurnLog(tmp_path)
    _dispatched(log, None, 1_000)
    _dispatched(log, "T2", 1_000, "A2")
    assert not turn_dispatched_action(log.conn, T)
    _dispatched(log, T, 1_000, "A3")
    assert turn_dispatched_action(log.conn, T)


def test_the_turn_is_claimed_from_the_row_that_started_it(tmp_path: Path) -> None:
    """The first ``turn.started`` names the trigger; no claim, or one with no source, names none."""
    log = TurnLog(tmp_path)
    assert turn_trigger(log.conn, T) is None
    log.claimed("T0", None)
    assert turn_trigger(log.conn, "T0") is None
    spoken = log.heard(T, "what time is it")
    log.claimed(T, spoken)
    log.claimed(T, log.heard("T9"))  # a second claim does not replace the first
    trigger = turn_trigger(log.conn, T)
    assert trigger is not None
    assert trigger.event_uid == spoken.event_uid
    assert trigger.type == "utterance.received"
    assert trigger.payload["transcript"] == "what time is it"


def test_the_channels_his_words_came_in_on(tmp_path: Path) -> None:
    """Voice and typed rows of the turn give their channel; a row with none gives ``None``."""
    log = TurnLog(tmp_path)
    assert input_channels(log.conn, T) == []
    log.heard(T, channel="voice")
    log.typed(T, "clarify")
    log.heard(T)
    log.typed("T2", "gpt_live")
    assert sorted(input_channels(log.conn, T), key=str) == [None, "clarify", "voice"]


def test_a_turn_that_began_at_a_barge_pause(tmp_path: Path) -> None:
    """Only the utterance row marked ``barge_pause`` makes the turn a fragment."""
    log = TurnLog(tmp_path)
    log.heard(T, endpoint_reason="silence")
    log.heard("T2", endpoint_reason="barge_pause")
    assert not heard_as_barge_pause(log.conn, T)
    log.heard(T, endpoint_reason="barge_pause")
    assert heard_as_barge_pause(log.conn, T)


def test_the_turns_of_a_window(tmp_path: Path) -> None:
    """Each window question names the turns whose rows are at or after ``since_ms``."""
    log = TurnLog(tmp_path)
    log.heard("T-old", at=900)
    log.heard("T-voice", at=1_000)
    log.typed("T-card", "clarify", at=2_000)
    log.typed("T-live", "gpt_live", at=2_000)
    _dispatched(log, "T-old", 900)
    _dispatched(log, "T-works", 1_000, "A2")
    _dispatched(log, None, 2_000, "A3")
    log.turn_ended("T-ended", at=2_000)
    log.run_ends("response.cancelled", "T-cut", "R1", at=1_500)
    log.run_ends("response.cancelled", "T-cut-long-ago", "R2", at=100)

    assert turns_of_his_words_since(log.conn, since_ms=1_000) == {"T-voice", "T-card"}
    assert turns_dispatching_since(log.conn, since_ms=1_000) == {"T-works"}
    assert turns_closed_since(log.conn, since_ms=1_000) == {"T-ended", "T-cut"}


def test_the_card_a_turn_put_up_last(tmp_path: Path) -> None:
    """The newest confirmation card of the turn is named; a turn with none names none."""
    log = TurnLog(tmp_path)
    assert card_requested_by_turn(log.conn, T) is None
    for card, turn in (("C1", T), ("C2", T), ("C9", "T2")):
        log.put(
            "confirmation.requested",
            {"confirmation_id": card, "template_line": "Send it?", "expires_at_ms": 9_000},
            turn_id=turn,
        )
    assert card_requested_by_turn(log.conn, T) == "C2"


def test_the_ask_cards_of_a_turn(tmp_path: Path) -> None:
    """The turn's ask cards come in the order put up with their log ids; each has a time."""
    log = TurnLog(tmp_path)
    assert clarification_asked_at(log.conn, "K1") is None
    for card, turn, at in (("K1", T, 1_111), ("K2", "T2", 2_222), ("K3", T, 3_333)):
        log.put(
            "clarification.requested",
            {"clarification_id": card, "question": "Which one?", "fields": [], "turn_id": turn},
            turn_id=turn,
            at=at,
        )
    asked = clarifications_asked_in_turn(log.conn, T)
    assert [event.payload["clarification_id"] for _row_id, event in asked] == ["K1", "K3"]
    assert asked[0][0] < asked[1][0]
    assert clarification_asked_at(log.conn, "K1") == 1_111
    assert clarification_asked_at(log.conn, "K3") == 3_333


def test_what_she_said_since(tmp_path: Path) -> None:
    """Voice text wins over text; segments with neither, and older ones, are left out."""
    log = TurnLog(tmp_path)
    log.put("surface.response_chunk", {"turn_id": T, "text": "too old"}, at=500)
    log.put("surface.response_chunk", {"turn_id": T, "text": "Hello"}, at=1_000)
    log.emitted(T, "R1", "Hello there. Details.", voice_text="Hello there.", at=1_100)
    log.emitted(T, "R2", "Written only.", voice_text="", at=1_200)
    log.emitted(T, "R3", "", at=1_300)
    assert spoken_since(log.conn, since_ms=1_000) == ["Hello", "Hello there."]


def test_the_newest_turn_timings_oldest_first(tmp_path: Path) -> None:
    """``limit`` newest ``turn.timing`` rows at or after ``since_ms``, returned oldest first."""
    log = TurnLog(tmp_path)
    for turn, at in (("T1", 100), ("T2", 1_000), ("T3", 2_000), ("T4", 3_000)):
        log.put("turn.timing", {"turn_id": turn, "done_ms": at}, turn_id=turn, at=at)
    log.heard("T5", at=4_000)
    assert [e.payload["turn_id"] for e in recent_turn_timings(
        log.conn, since_ms=0, limit=3)] == ["T2", "T3", "T4"]
    assert [e.payload["turn_id"] for e in recent_turn_timings(
        log.conn, since_ms=2_000, limit=10)] == ["T3", "T4"]
    newest = recent_turn_timings(log.conn, since_ms=0, limit=1)[0]
    assert (newest.ts_epoch_ms, newest.payload["done_ms"]) == (3_000, 3_000)


def test_a_turn_whose_answer_his_next_sentence_took_over(tmp_path: Path) -> None:
    """Only a run cancelled as ``superseded`` counts, and only for the turn it belongs to."""
    log = TurnLog(tmp_path)
    log.run(T, "R1")
    log.run_ends("response.cancelled", T, "R1", "barge_in")
    log.run_ends("response.cancelled", "T2", "R9", "superseded")
    assert not answer_superseded(log.conn, T)
    log.run_ends("response.cancelled", T, "R2", "superseded")
    assert answer_superseded(log.conn, T)


def test_a_turn_that_had_a_run_cancelled_or_failed(tmp_path: Path) -> None:
    """A cancelled or failed run, or a failed turn, whatever the reason; a finished one is not."""
    log = TurnLog(tmp_path)
    log.run(T, "R1")
    log.run_ends("response.completed", T, "R1")
    log.turn_ended(T)
    assert not turn_cancelled_or_failed(log.conn, T)
    assert not turn_cancelled_or_failed(log.conn, "T-cut")
    log.run_ends("response.cancelled", "T-cut", "R2", "user_stop")
    assert turn_cancelled_or_failed(log.conn, "T-cut")
    assert not turn_cancelled_or_failed(log.conn, "T-run")
    log.run_ends("response.failed", "T-run", "R3", "oops")
    assert turn_cancelled_or_failed(log.conn, "T-run")
    assert not turn_cancelled_or_failed(log.conn, "T-raised")
    log.turn_failed("T-raised")
    assert turn_cancelled_or_failed(log.conn, "T-raised")


def _playback_row(log: TurnLog, kind: str, response_id: str) -> None:
    """The row L5 writes for ``response_id``'s playback: ``kind`` is its name after ``surface.``."""
    lease = {"session_id": "S1", "response_id": response_id, "turn_id": T}
    heard = {
        **lease, "playback_generation_id": "P1", "heard_through_sequence": 0,
        "submitted_samples": 0,
    }
    payloads: dict[str, dict[str, Any]] = {
        "playback_started": {
            **lease, "playback_generation_id": "P1", "phase": "final", "channel": "both",
            "speech_text_hash": "h",
        },
        "playback_completed": {**heard, "speech_text_hash": "h"},
        "playback_interrupted": {**heard, "heard_text_hash": "h", "reason": "barge_in"},
        "playback_failed": {**heard, "heard_text_hash": "h", "reason": "device_lost"},
        "speech_dropped": {"response_id": response_id, "turn_id": T, "reason": "no_phone_voice"},
    }
    log.put(f"surface.{kind}", payloads[kind], turn_id=T)


@pytest.mark.parametrize(
    "kind",
    ["playback_completed", "playback_interrupted", "playback_failed", "speech_dropped"],
)
def test_an_answers_playback_ends_with_a_terminal_or_a_drop(tmp_path: Path, kind: str) -> None:
    """A lease's terminal or a dropped answer ends it; a start and others' answers do not."""
    log = TurnLog(tmp_path)
    _playback_row(log, "playback_completed", "R-other")
    _playback_row(log, "playback_started", "R1")
    assert not playback_ended(log.conn, "R1")
    _playback_row(log, kind, "R1")
    assert playback_ended(log.conn, "R1")


def test_the_newest_row_of_his_words(tmp_path: Path) -> None:
    """Typed and heard rows count; a plugin's resume echo, other rows and an empty log do not."""
    log = TurnLog(tmp_path)
    assert latest_input_row(log.conn) is None
    log.run(T, "R1")
    assert latest_input_row(log.conn) is None
    log.heard(T)
    assert latest_input_row(log.conn) == latest_id(log.conn)
    log.typed("T2", "gpt_live")
    typed_at = latest_id(log.conn)
    assert latest_input_row(log.conn) == typed_at
    log.typed("T3", "plugin_resume")
    log.run("T3", "R3")
    assert latest_input_row(log.conn) == typed_at
    log.put("surface.user_intent", {"transcript": "hi", "turn_id": "T4"}, turn_id="T4")
    assert latest_input_row(log.conn) == latest_id(log.conn)
