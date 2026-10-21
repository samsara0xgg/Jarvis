"""ADR 0240: a call the model repeats is not run again.

The repeat table is read over (calls, name, key) rows; the rest drives the real ``decide()``
batch loop, dispatcher and event log with a scripted model (no provider is called). What an
answer claims is checked against receipts instead (ADR 0320, ``test_claims_block.py``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from jarvis.decision.turn_guards import REPEATED_CALL, REPEATED_FAILED, TurnCall, repeated_call
from tests.support.model import ScriptedModel, tool_call
from tests.support.turn_guards import guard_batch, logged_guards, tool_texts

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


RECALL = TurnCall("recall", read_only=True, ok=True, key="a")
RECALL_FAILED = TurnCall("recall", read_only=True, ok=False, key="a")
LISTED = TurnCall("kept", read_only=True, ok=True)
MEMO_A = TurnCall("keep", read_only=False, ok=True, key="a")
MEMO_X = TurnCall("keep", read_only=False, ok=True, key="x")
MEMO_X_FAILED = TurnCall("keep", read_only=False, ok=False, key="x")


@pytest.mark.parametrize(
    ("calls", "name", "key", "expected"),
    [
        ([], "recall", "a", None),
        ([RECALL], "recall", "a", REPEATED_CALL),
        ([RECALL], "recall", "b", None),  # other arguments
        ([RECALL], "get_briefing", "a", None),  # other tool
        ([RECALL_FAILED], "recall", "a", REPEATED_FAILED),  # it failed: change something
        ([RECALL, MEMO_X], "recall", "a", None),  # a write since: the repeat may differ
        ([RECALL, MEMO_X_FAILED], "recall", "a", REPEATED_CALL),  # it failed, nothing changed
        ([RECALL, LISTED], "recall", "a", REPEATED_CALL),  # a read changes nothing
        ([MEMO_A, RECALL], "recall", "a", REPEATED_CALL),  # the write came before the read
        ([MEMO_A], "keep", "a", REPEATED_CALL),  # the same memo again
    ],
)
def test_the_repeat_table(
    calls: list[TurnCall], name: str, key: str, expected: str | None,
) -> None:
    """A call identical to an earlier one is refused unless a write succeeded in between."""
    assert repeated_call(calls, name, key) == expected


# --- decide() with a scripted model ------------------------------------------------------


def _count(conn: sqlite3.Connection, event_type: str) -> int:
    return int(
        conn.execute("SELECT COUNT(*) FROM events WHERE type=?", (event_type,)).fetchone()[0]
    )


MEMO = ("schedule", {"action": "add", "what": "明天开会"})


def test_an_identical_repeat_is_not_run_and_gets_the_note(tmp_path: Path) -> None:
    """(a) The same read twice in a turn: one run, and the second call's result is the note."""
    model = ScriptedModel(tool_call("kept"), tool_call("kept"), "没有备忘。")
    text, conn = guard_batch(tmp_path, model)
    try:
        assert text == "没有备忘。"
        assert _count(conn, "action.result_observed") == 1
        assert tool_texts(model.requests[-1].messages)[1] == REPEATED_CALL
        assert logged_guards(conn) == [{"turn_id": "T_guard", "guard": "repeat", "tool": "kept"}]
    finally:
        conn.close()


def test_a_repeat_of_a_call_that_failed_names_the_fix(tmp_path: Path) -> None:
    """The same failing call again is refused with the note that says to change what failed."""
    empty = ("keep", {"text": ""})
    model = ScriptedModel(tool_call(*empty), tool_call(*empty), "没记成。")
    _, conn = guard_batch(tmp_path, model)
    try:
        assert _count(conn, "action.result_observed") == 1
        failed, repeat = tool_texts(model.requests[-1].messages)
        assert "empty_text" in failed
        assert repeat == REPEATED_FAILED
    finally:
        conn.close()


def test_the_same_memo_twice_is_saved_once(tmp_path: Path) -> None:
    """A write repeated unchanged (schedule, the same words) is refused too."""
    model = ScriptedModel(tool_call(*MEMO), tool_call(*MEMO), "记好了。")
    _, conn = guard_batch(tmp_path, model)
    try:
        assert _count(conn, "thing.added") == 1
        assert tool_texts(model.requests[-1].messages)[1] == REPEATED_CALL
        assert _count(conn, "turn.guard_fired") == 1
    finally:
        conn.close()


def test_a_repeat_after_a_write_runs(tmp_path: Path) -> None:
    """(b) list, write, list: the second list may differ, so it runs."""
    model = ScriptedModel(tool_call("kept"), tool_call(*MEMO), tool_call("kept"), "有一条。")
    _, conn = guard_batch(tmp_path, model)
    try:
        assert _count(conn, "action.result_observed") == 3
        assert REPEATED_CALL not in tool_texts(model.requests[-1].messages)
        assert _count(conn, "turn.guard_fired") == 0
    finally:
        conn.close()
