"""ADR 0231: the steps of a turn, read back from the Event Log for the line under her answer.

The live ``step`` frames and this summary fold the same rows with the same
:class:`jarvis.shared.steps.StepFold`; this one reads them from the log, so it holds after a
restart and for any page of history.

Layer rules: stdlib, ``jarvis.shared`` and ``jarvis.state`` only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jarvis.shared import lang
from jarvis.shared.steps import Step, StepFold, summarize
from jarvis.state.event_log import turn_action_events, turns_of_events

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable


def _folded(
    conn: sqlite3.Connection, answer_uids: Iterable[str],
) -> tuple[dict[str, str], dict[str, dict[int, Step]]]:
    """``({answer uid: turn id}, {turn id: {n: last state of step n}})`` for the answers' turns."""
    turns = turns_of_events(conn, answer_uids)
    last: dict[str, dict[int, Step]] = {}
    fold = StepFold()
    for event in turn_action_events(conn, set(turns.values())):
        for step in fold.feed(event):
            last.setdefault(step.turn_id, {})[step.n] = step
    return turns, last


def answer_steps(conn: sqlite3.Connection, answer_uids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """``{answer event uid: summary}`` for the answers whose turn ran a tool.

    The summary is :func:`jarvis.shared.steps.summarize`'s. An answer is the
    ``surface.response_emitted`` row whose uid is the memory record's id; a uid that names no
    such row, or whose turn ran no tool, is left out.
    """
    turns, last = _folded(conn, answer_uids)
    summaries = {turn: summarize(list(by_n.values())) for turn, by_n in last.items()}
    return {uid: summaries[turn] for uid, turn in turns.items() if summaries.get(turn)}


def answer_did(conn: sqlite3.Connection, answer_uids: Iterable[str]) -> dict[str, str]:
    """``{answer event uid: what the turn did}`` for the answers whose turn ran a tool that worked.

    The distinct past-tense phrases of the steps that succeeded, in order, always in English: the
    reader is the model (ADR 0267), whatever language he is spoken to in. A step that failed or
    never ended is left out, so is a turn with none left.
    """
    turns, last = _folded(conn, answer_uids)
    did = {
        turn: ", ".join(
            dict.fromkeys(
                lang.t(f"step.{s.kind}.done", lang="en") for s in by_n.values() if s.state == "done"
            ),
        )
        for turn, by_n in last.items()
    }
    return {uid: did[turn] for uid, turn in turns.items() if did.get(turn)}
