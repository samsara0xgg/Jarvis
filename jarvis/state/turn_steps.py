"""ADR 0231: the steps of a turn, read back from the Event Log for the line under her answer.

The live ``step`` frames and this summary fold the same rows with the same
:class:`jarvis.shared.steps.StepFold`; this one reads them from the log, so it holds after a
restart and for any page of history.

Layer rules: stdlib, ``jarvis.shared`` and ``jarvis.state`` only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jarvis.shared.receipts import Receipt, said
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


def turn_receipts(
    conn: sqlite3.Connection, turn_ids: Iterable[str],
) -> dict[str, tuple[Receipt, ...]]:
    """``{turn id: its receipts in call order}`` (ADR 0319), read from the Event Log.

    The projection by turn, folded by the same :class:`~jarvis.shared.steps.StepFold` as the
    steps: a call has its receipt once it ended (a call still running has none yet), and an old
    row without one folds to a derived receipt. A turn with none is left out.
    """
    turns = list(dict.fromkeys(turn_ids))
    last: dict[str, dict[int, Step]] = {}
    fold = StepFold()
    for event in turn_action_events(conn, turns):
        for step in fold.feed(event):
            last.setdefault(step.turn_id, {})[step.n] = step
    return {
        turn: tuple(s.receipt for _, s in sorted(by_n.items()) if s.receipt is not None)
        for turn, by_n in last.items()
        if any(s.receipt is not None for s in by_n.values())
    }


def answer_receipts(
    conn: sqlite3.Connection, answer_uids: Iterable[str],
) -> dict[str, str]:
    """``{answer event uid: its turn's receipts as one line}`` for the answers whose turn has any.

    The line is each receipt's sentence, object and status (:func:`jarvis.shared.receipts.said`),
    in call order, always in English: the reader is the model (ADR 0319, which replaced the
    ``[You did: ...]`` of ADR 0267), whatever language he is spoken to in.
    """
    turns = turns_of_events(conn, answer_uids)
    by_turn = turn_receipts(conn, set(turns.values()))
    return {
        uid: "; ".join(said(r) for r in by_turn[turn])
        for uid, turn in turns.items()
        if turn in by_turn
    }


@dataclass(frozen=True)
class ObjectReceipt:
    """The latest receipt that touched an object: which turn, which call, and when."""

    object: str
    receipt: Receipt
    turn_id: str
    tool: str
    ts_ms: int


def object_receipts(conn: sqlite3.Connection) -> dict[str, ObjectReceipt]:
    """``{object: its latest receipt}`` (ADR 0319), the projection by object.

    Every result row whose receipt names an object, the later one winning, with the turn and the
    tool of the call it came from. A receipt that cannot be read is skipped.
    """
    rows = conn.execute(
        "SELECT o.payload_json, o.ts_epoch_ms, p.payload_json, p.correlation_id FROM events o "
        "JOIN events p ON p.type = 'action.proposed' "
        "AND json_extract(p.payload_json, '$.action_id') "
        "= json_extract(o.payload_json, '$.action_id') "
        "WHERE o.type = 'action.result_observed' "
        "AND json_extract(o.payload_json, '$.receipt.object') IS NOT NULL ORDER BY o.id",
    )
    latest: dict[str, ObjectReceipt] = {}
    for result, ts_ms, proposed, turn in rows:
        try:
            receipt = Receipt.from_json(json.loads(result)["receipt"])
        except (ValueError, KeyError):
            continue
        if receipt.object is not None:
            latest[receipt.object] = ObjectReceipt(
                receipt.object, receipt, str(turn or ""),
                str(json.loads(proposed).get("tool_name") or ""), int(ts_ms),
            )
    return latest


def answer_places(conn: sqlite3.Connection, answer_uids: Iterable[str]) -> dict[str, list[Any]]:
    """``{answer event uid: places}`` for the answers whose ``surface.response_emitted`` has any.

    The places are the row's own ``places`` (ADR 0279), as logged: the phone redraws the map card
    of an old answer from them. A uid with none, or that names no such row, is left out.
    """
    rows = conn.execute(
        "SELECT event_uid, json_extract(payload_json, '$.places') FROM events "
        "WHERE type = 'surface.response_emitted' "
        "AND event_uid IN (SELECT value FROM json_each(?))",
        (json.dumps(list(dict.fromkeys(answer_uids))),),
    )
    return {str(uid): json.loads(places) for uid, places in rows if places}
