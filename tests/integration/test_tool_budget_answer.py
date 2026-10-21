"""ADR 0030: a spent tool budget still gets one no-tool request for the answer.

Drives the real ``decide()`` loop, dispatcher and event log with a scripted model that
keeps proposing a read-only tool until the loop bound, then answers only when asked
without tools. Asserts on the spoken text and on the persisted ``cost.recorded`` and
``action.result_observed`` rows.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from jarvis.decision import decide
from jarvis.decision.stream_envelope import split_envelope
from jarvis.runtime import _max_tool_iterations
from jarvis.state.event_log import emit_event, open_event_log
from tests.support.model import Failure, ScriptedModel, tool_call
from tests.support.runtime_config import runtime_config
from tests.support.turn_guards import guard_context

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

ANSWER = "查到了：今天没有网易云音乐的前台记录；微信聊天对象无法从截图确认。"  # noqa: RUF001 — fullwidth punctuation, a Chinese reply.
BUDGET = 3


def _tool_hungry(answer: str | Failure) -> ScriptedModel:
    """Kept on every budgeted request, other arguments each time (ADR 0240); then ``answer``."""
    rounds = [tool_call("kept", {"page": page}) for page in range(1, BUDGET + 1)]
    return ScriptedModel(*rounds, answer)


def _run(tmp_path: Path, llm: ScriptedModel) -> tuple[str, sqlite3.Connection]:
    conn = open_event_log(tmp_path / "events.db")
    trigger = emit_event(
        conn,
        type="surface.user_intent",
        payload={"transcript": "我今天微信和谁聊过天", "turn_id": "T_budget"},
        correlation={"turn_id": "T_budget"},
    )
    result = decide(trigger, guard_context(conn, tmp_path, llm, BUDGET))
    assert result.response_plan is not None
    return result.response_plan.text, conn


def _count(conn: sqlite3.Connection, event_type: str) -> int:
    return int(
        conn.execute("SELECT COUNT(*) FROM events WHERE type=?", (event_type,)).fetchone()[0]
    )


def test_spent_budget_gets_one_no_tool_request_for_the_answer(tmp_path: Path) -> None:
    """Every budgeted request proposed a tool; the answer comes from one extra request."""
    llm = _tool_hungry(ANSWER)
    text, conn = _run(tmp_path, llm)
    try:
        # The answer is plain and within the spoken form's own 60-character
        # limit, so ADR 0045 speaks it as written: no spoken-form request.
        assert split_envelope(text)[:2] == (ANSWER, ANSWER)
        assert len(llm.requests) == BUDGET + 1
        assert all(request.tools for request in llm.requests[:BUDGET])
        final = llm.requests[BUDGET]
        assert final.tools is None
        assert final.tool_choice is None
        assert final.messages[-1]["role"] == "user"
        assert "no more tools can be called" in final.messages[-1]["content"]
        assert "not the user's words" in final.messages[-1]["content"]
        # Every tool the model asked for ran and left its observation before the answer.
        assert _count(conn, "action.result_observed") == BUDGET
        results = conn.execute(
            "SELECT payload_json FROM events WHERE type='action.result_observed'"
        ).fetchall()
        assert all(json.loads(row[0])["semantics"] == "observation" for row in results)
        # The answer request is paid for like every other.
        assert _count(conn, "cost.recorded") == BUDGET + 1
        assert _count(conn, "turn.ended") == 1
    finally:
        conn.close()


def test_failed_answer_request_falls_back_to_a_chinese_limitation(tmp_path: Path) -> None:
    """A provider failure on the answer request ends the turn with fixed Chinese text."""
    llm = _tool_hungry(Failure(RuntimeError("provider down")))
    text, conn = _run(tmp_path, llm)
    try:
        assert text == "这一轮工具调用次数用完了，还没整理出答案。请把问题拆小一点再问一次。"  # noqa: RUF001 — fullwidth punctuation, the fixed Chinese limitation.
        assert "exhausted" not in text
        assert len(llm.requests) == BUDGET + 1
        # The request that failed is paid for like the others: one error row, after the three
        # that completed.
        costs = [
            json.loads(row[0])
            for row in conn.execute(
                "SELECT payload_json FROM events WHERE type='cost.recorded' ORDER BY id"
            )
        ]
        assert [cost["disposition"] for cost in costs] == ["completed"] * BUDGET + ["error"]
        assert costs[-1]["error_code"] == "RuntimeError"
        assert _count(conn, "turn.ended") == 1
    finally:
        conn.close()


def test_empty_answer_falls_back_too(tmp_path: Path) -> None:
    """An empty reply is not an answer; the same limitation is spoken."""
    llm = _tool_hungry("  ")
    text, conn = _run(tmp_path, llm)
    try:
        assert text.startswith("这一轮工具调用次数用完了")
        assert _count(conn, "cost.recorded") == BUDGET + 1
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("channel", "config", "expected"),
    [
        ("cli_stdin", {"max_tool_iterations": 40, "max_tool_iterations_voice": 8}, 40),
        ("dashboard", {"max_tool_iterations": 40, "max_tool_iterations_voice": 8}, 40),
        ("inherent_ptt", {"max_tool_iterations": 40, "max_tool_iterations_voice": 8}, 8),
        ("speech", {"max_tool_iterations": 40, "max_tool_iterations_voice": 8}, 8),
        ("inherent_wake", {"max_tool_iterations": 40}, 40),  # voice key unset: the same bound
        ("inherent_ptt", {"max_tool_iterations_voice": 8}, 8),
        ("inherent_ptt", {"max_tool_iterations": 40, "max_tool_iterations_voice": 0}, 40),
        ("cli_stdin", {"max_tool_iterations_voice": 8}, 5),  # typed turns never read the voice key
    ],
)
def test_only_a_spoken_turn_reads_the_voice_cap(
    tmp_path: Path, channel: str, config: dict[str, int], expected: int
) -> None:
    """The composition root picks the bound by channel; typed turns keep the full one."""
    conn = open_event_log(tmp_path / "events.db")
    try:
        trigger = emit_event(
            conn,
            type="surface.user_intent",
            payload={"transcript": "x", "turn_id": "T_cap", "channel": channel},
            correlation={"turn_id": "T_cap"},
        )
        assert _max_tool_iterations(runtime_config({"llm": config}), trigger) == expected
    finally:
        conn.close()
