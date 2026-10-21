"""A tool call whose arguments do not parse is not run: the model is told and may call again.

Drives the real ``decide()`` batch loop, dispatcher and event log with a scripted model whose
first call to a tool with only optional arguments (``kept``) is cut off mid-arguments. A call
dispatched with ``{}`` would run and answer as if the model had asked for it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from jarvis.decision import decide
from jarvis.state.event_log import emit_event, open_event_log
from tests.support.model import Call, Reply, ScriptedModel
from tests.support.turn_guards import guard_context

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

ANSWER = "好的，我再试一次。"  # noqa: RUF001 — fullwidth punctuation, a Chinese reply.


def _cut_off(arguments_json: str) -> ScriptedModel:
    """Calls ``kept`` with ``arguments_json`` on its first request, then answers in words."""
    return ScriptedModel(Reply(calls=(Call("kept", call_id="c1", raw=arguments_json),)), ANSWER)


def _told(llm: ScriptedModel) -> list[dict[str, str]]:
    """The tool messages the second request carried back to the model."""
    return [m for m in llm.requests[1].messages if m.get("role") == "tool"]


def _run(tmp_path: Path, llm: ScriptedModel) -> tuple[str, sqlite3.Connection]:
    conn = open_event_log(tmp_path / "events.db")
    trigger = emit_event(
        conn,
        type="surface.user_intent",
        payload={"transcript": "我记过什么", "turn_id": "T_cut"},
        correlation={"turn_id": "T_cut"},
    )
    result = decide(trigger, guard_context(conn, tmp_path, llm, 5))
    assert result.response_plan is not None
    return result.response_plan.text, conn


def _count(conn: sqlite3.Connection, event_type: str) -> int:
    return int(
        conn.execute("SELECT COUNT(*) FROM events WHERE type=?", (event_type,)).fetchone()[0]
    )


@pytest.mark.parametrize(
    "arguments_json", ['{"page": 2', "not json at all", "[1, 2]", '"page"', "3"],
)
def test_a_call_whose_arguments_do_not_parse_is_not_run_and_the_model_is_told(
    tmp_path: Path, arguments_json: str,
) -> None:
    """Nothing is proposed or dispatched; the next request carries the tool error for that call."""
    llm = _cut_off(arguments_json)
    text, conn = _run(tmp_path, llm)
    try:
        assert ANSWER in text
        for event_type in ("action.proposed", "action.dispatched", "action.result_observed"):
            assert _count(conn, event_type) == 0, event_type
        assert len(llm.requests) == 2
        told = _told(llm)
        assert [m["tool_call_id"] for m in told] == ["c1"]
        error = json.loads(told[0]["content"])["error"]
        assert error.startswith("arguments were not ")
    finally:
        conn.close()


def test_the_error_names_where_the_json_broke(tmp_path: Path) -> None:
    """A cut-off call's error carries the parser's own position, so the model can see why."""
    llm = _cut_off('{"page": 2')
    _, conn = _run(tmp_path, llm)
    try:
        told = _told(llm)
        assert "char 10" in json.loads(told[0]["content"])["error"]
    finally:
        conn.close()


@pytest.mark.parametrize("arguments_json", ["", "{}"])
def test_a_call_with_no_arguments_is_still_run(tmp_path: Path, arguments_json: str) -> None:
    """An empty argument string is a call with none: the tool runs and its result goes back."""
    llm = _cut_off(arguments_json)
    _, conn = _run(tmp_path, llm)
    try:
        assert _count(conn, "action.proposed") == 1
        assert _count(conn, "action.result_observed") == 1
        told = _told(llm)
        assert "error" not in json.loads(told[0]["content"])
    finally:
        conn.close()
