"""ADR 0240: ``tool_exam run --guards`` applies the loop's guards to a first response.

Runs ``scripts.tool_exam._one`` with a scripted ``replay_once`` (no provider is called) over
both request shapes the exam replays, Responses ``input`` items and chat ``messages``, and
reads the follow-up request it sends and the result line it keeps.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, cast

from jarvis.decision.turn_guards import REPEATED_CALL, UNBACKED_NOTE
from scripts import tool_exam

if TYPE_CHECKING:
    import pytest

    from jarvis.decision.llm import LLMClient

READ_ONLY = {"job_ledger": True, "create_memo": False}
LANGUAGE = "[Reply language for this turn: Chinese]"
USAGE = {"input": 10, "output": 2, "cache_read": 0, "cache_write": 0, "status": "reported"}


def _responses(*items: dict[str, Any]) -> dict[str, Any]:
    ask = {"role": "user", "content": f"看看求职进展\n\n{LANGUAGE}"}
    return {"input": [ask, *items], "instructions": "stub"}


def _chat(*items: dict[str, Any]) -> dict[str, Any]:
    ask = {"role": "user", "content": f"看看求职进展\n\n{LANGUAGE}"}
    return {"messages": [{"role": "system", "content": "stub"}, ask, *items]}


LEDGER = {"type": "function_call", "call_id": "c0", "name": "job_ledger", "arguments": "{}"}
LEDGER_OUT = {"type": "function_call_output", "call_id": "c0", "output": '{"count": 3}'}
MEMO = {"type": "function_call", "call_id": "c1", "name": "create_memo",
        "arguments": '{"text": "x"}'}
MEMO_OUT = {"type": "function_call_output", "call_id": "c1", "output": '{"text": "x"}'}


def _answer(text: str = "", *calls: tuple[str, str]) -> dict[str, Any]:
    return {
        "first_token_s": 0.5, "total_s": 1.0, "text": text, "finish_reason": "stop",
        "tool_calls": [{"name": n, "arguments": a} for n, a in calls], "error": None,
        "usage": USAGE,
    }


def _run(
    monkeypatch: pytest.MonkeyPatch, request: dict[str, Any], *answers: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    sent: list[dict[str, Any]] = []
    script = list(answers)

    async def replay_once(
        _client: object, req: dict[str, Any], _catalog: object,
    ) -> dict[str, Any]:
        sent.append(req)
        return script.pop(0)

    monkeypatch.setattr(tool_exam, "replay_once", replay_once)
    base = {"kind": "run", "case": "c", "preset": "p", "run": 0, "model": "m"}
    line = asyncio.run(
        tool_exam._one(cast("LLMClient", None), request, {}, base, READ_ONLY)  # noqa: SLF001
    )
    return line, sent


def test_a_repeated_call_gets_the_note_and_one_more_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """Responses shape: the repeat's result is the note; the second step is what is judged."""
    request = _responses(LEDGER, LEDGER_OUT)
    line, sent = _run(
        monkeypatch, request, _answer("", ("job_ledger", "{}")), _answer("还是三条。"),
    )
    assert len(sent) == 2
    followed = sent[1]["input"][len(request["input"]) :]
    assert [i.get("type") or i["role"] for i in followed] == [
        "function_call", "function_call_output", "user",
    ]
    assert followed[1]["output"] == REPEATED_CALL
    assert followed[2]["content"] == LANGUAGE
    assert line["text"] == "还是三条。"
    assert line["calls"] == []
    assert line["guarded"] == {
        "kind": "repeat", "first_calls": [{"name": "job_ledger", "args": {}}],
        "first_text": "", "first_usage": USAGE,
    }
    assert line["total_s"] == 2.0
    expect = {"ok": ["text"], "no_identical_repeat": True}
    assert tool_exam.judge(expect, line, request) == (True, "pass(g)")


def test_a_repeat_after_a_write_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """The write between them may have changed the answer, so the repeat is the step."""
    line, sent = _run(
        monkeypatch, _responses(LEDGER, LEDGER_OUT, MEMO, MEMO_OUT),
        _answer("", ("job_ledger", "{}")),
    )
    assert len(sent) == 1
    assert "guarded" not in line


def test_a_response_with_a_call_that_would_run_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its result is unknown here, so the exam does not guess the follow-up."""
    line, sent = _run(
        monkeypatch, _responses(LEDGER, LEDGER_OUT),
        _answer("", ("job_ledger", "{}"), ("create_memo", '{"text": "y"}')),
    )
    assert len(sent) == 1
    assert "guarded" not in line


def test_an_unbacked_claim_gets_the_note_in_chat_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chat shape: the answer and the note follow, then the model's second step is judged."""
    request = _chat()
    line, sent = _run(
        monkeypatch, request, _answer("记下了。"), _answer("", ("create_memo", '{"text": "x"}')),
    )
    followed = sent[1]["messages"][len(request["messages"]) :]
    assert followed == [
        {"role": "assistant", "content": "记下了。"},
        {"role": "user", "content": UNBACKED_NOTE.format(said="did it")},
        {"role": "user", "content": LANGUAGE},
    ]
    assert line["calls"] == [{"name": "create_memo", "args": {"text": "x"}}]
    assert line["guarded"]["kind"] == "unbacked"
    assert line["guarded"]["first_text"] == "记下了。"
    expect = {"ok": ["create_memo"], "bad": []}
    assert tool_exam.judge(expect, line, request) == (True, "pass(g)")


def test_an_answer_backed_by_a_call_and_a_plain_answer_are_one_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write earlier in the turn backs the claim; plain talk trips nothing."""
    backed, sent = _run(monkeypatch, _responses(MEMO, MEMO_OUT), _answer("记下了。"))
    plain, sent_plain = _run(monkeypatch, _chat(), _answer("今天天气不错。"))
    assert len(sent) == len(sent_plain) == 1
    assert "guarded" not in backed
    assert "guarded" not in plain
    assert json.dumps(plain["text"], ensure_ascii=False) == '"今天天气不错。"'
