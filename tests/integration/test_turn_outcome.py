"""How a turn ended, across the rows an Event Log can hold for it (ADR 0349).

One table, one row per shape of log: each case writes the rows a turn leaves and says what
``turn_outcome`` answers. The Live delegation and ``POST /inherent/ask`` both map from this one
answer, so a row here is a row for both. The log is a real on-disk Event Log; nothing is mocked.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import pytest

from jarvis.state.turn_outcome import Answered, Failed, TurnOutcome, turn_outcome
from tests.support.turn_rows import TurnLog

if TYPE_CHECKING:
    from pathlib import Path

T = "T1"
Writer = Callable[[TurnLog], object]


def _plain_answer(log: TurnLog) -> None:
    log.run(T, "R1")
    log.emitted(T, "R1", "It is noon.")


def _answer_with_voice_and_document(log: TurnLog) -> None:
    log.run(T, "R1")
    log.emitted(
        T, "R1", "Noon.", voice_text="Noon.", document_text="Noon in Vancouver, 12:00 PDT.",
        written_apart=True,
    )


def _answer_with_empty_voice(log: TurnLog) -> None:
    log.run(T, "R1")
    log.emitted(T, "R1", "It is noon.", voice_text="", document_text="")


def _wait_line_only(log: TurnLog) -> None:
    log.run(T, "W1", "commentary")
    log.emitted(T, "W1", "One moment.", "commentary")


def _wait_line_taken_back(log: TurnLog) -> None:
    log.run(T, "W1", "commentary")
    log.emitted(T, "W1", "One moment.", "commentary")
    log.run_ends("response.cancelled", T, "W1", "answer_started")


def _wait_line_then_answer(log: TurnLog) -> None:
    _wait_line_taken_back(log)
    log.run(T, "R1")
    log.emitted(T, "R1", "It is noon.", voice_text="It is noon.")


def _wait_line_fails_then_answer_pending(log: TurnLog) -> None:
    log.run(T, "W1", "commentary")
    log.run_ends("response.failed", T, "W1", "tts_error")
    log.run(T, "R1")


def _run_failed(log: TurnLog) -> None:
    log.run(T, "R1")
    log.run_ends("response.failed", T, "R1", "turn_raised")


def _run_cancelled(log: TurnLog) -> None:
    log.run(T, "R1")
    log.run_ends("response.cancelled", T, "R1", "user_stop")


def _turn_raised(log: TurnLog) -> None:
    log.turn_failed(T, reason="missing_key", exception="KeyError('k')")


def _turn_raised_without_reason(log: TurnLog) -> None:
    log.turn_failed(T)


def _streamed_run_superseded(log: TurnLog) -> None:
    log.run(T, "R1")
    log.run(T, "R2", corrects="R1")
    log.run_ends("response.cancelled", T, "R1", "superseded")


def _streamed_run_superseded_then_answered(log: TurnLog) -> None:
    _streamed_run_superseded(log)
    log.emitted(T, "R2", "Corrected.")


def _correcting_run_fails(log: TurnLog) -> None:
    _streamed_run_superseded(log)
    log.run_ends("response.failed", T, "R2", "turn_raised")


def _answer_then_late_failure(log: TurnLog) -> None:
    _plain_answer(log)
    log.run_ends("response.failed", T, "R1", "late")


def _failure_then_answer(log: TurnLog) -> None:
    _run_failed(log)
    log.run(T, "R2")
    log.emitted(T, "R2", "Second try.")


def _two_failures(log: TurnLog) -> None:
    _run_failed(log)
    log.turn_failed(T, reason="later")


def _another_turns_rows(log: TurnLog) -> None:
    log.run("T2", "R9")
    log.emitted("T2", "R9", "Not yours.")
    log.turn_failed("T3")


CASES: dict[str, tuple[Writer, TurnOutcome | None]] = {
    "no rows": (lambda _log: None, None),
    "a run that has only opened": (lambda log: log.run(T, "R1"), None),
    "a plain answer": (
        _plain_answer, Answered("It is noon.", None, None, written_apart=False),
    ),
    "an answer with a voice and a written part": (
        _answer_with_voice_and_document,
        Answered(
            "Noon.", "Noon.", "Noon in Vancouver, 12:00 PDT.", written_apart=True,
        ),
    ),
    "an answer whose voice and document are empty": (
        _answer_with_empty_voice, Answered("It is noon.", None, None, written_apart=False),
    ),
    "a wait line alone is not the answer": (_wait_line_only, None),
    "a wait line taken back leaves the turn going": (_wait_line_taken_back, None),
    "an answer after a wait line": (
        _wait_line_then_answer, Answered("It is noon.", "It is noon.", None, written_apart=False),
    ),
    "a wait line that fails leaves the turn going": (_wait_line_fails_then_answer_pending, None),
    "a run that failed": (_run_failed, Failed("response.failed", "turn_raised", None)),
    "a run that was cancelled": (_run_cancelled, Failed("response.cancelled", "user_stop", None)),
    "a turn that raised": (
        _turn_raised, Failed("turn.failed", "missing_key", "KeyError('k')"),
    ),
    "a turn that raised without a reason": (
        _turn_raised_without_reason, Failed("turn.failed", None, "KeyError('k')"),
    ),
    "a streamed run another run took over": (_streamed_run_superseded, None),
    "the run that took over answers": (
        _streamed_run_superseded_then_answered,
        Answered("Corrected.", None, None, written_apart=False),
    ),
    "the run that took over fails": (
        _correcting_run_fails, Failed("response.failed", "turn_raised", None),
    ),
    "an answer, then a late failure": (
        _answer_then_late_failure, Answered("It is noon.", None, None, written_apart=False),
    ),
    "a failure, then another run answers": (
        _failure_then_answer, Answered("Second try.", None, None, written_apart=False),
    ),
    "two failures: the first is the turn's": (
        _two_failures, Failed("response.failed", "turn_raised", None),
    ),
    "rows of other turns": (_another_turns_rows, None),
}


@pytest.mark.parametrize("case", list(CASES), ids=list(CASES))
def test_turn_outcome(tmp_path: Path, case: str) -> None:
    """The log a case writes ends the turn as the case says."""
    write, expected = CASES[case]
    log = TurnLog(tmp_path)
    write(log)
    assert turn_outcome(log.conn, T) == expected
