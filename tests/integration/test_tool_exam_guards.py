"""ADR 0240: ``tool_exam run --guards`` applies the loop's guards to a first response.

Runs ``scripts.tool_exam._one`` with a scripted ``replay_once`` (no provider is called) over
both request shapes the exam replays, Responses ``input`` items and chat ``messages``, and
reads the follow-up request it sends and the result line it keeps.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, cast

import pytest

from jarvis.decision.turn_guards import REPEATED_CALL, UNBACKED_NOTE
from scripts import tool_exam

if TYPE_CHECKING:
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


def test_a_claimed_lookup_with_no_call_gets_the_note_in_responses_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exam applies the loop's third case: 「我查了」 and no call is sent back (ADR 0250)."""
    request = _responses()
    claim = "我查了你的日历，明天没有安排。"  # noqa: RUF001 - fullwidth comma is Chinese punctuation
    line, sent = _run(
        monkeypatch, request, _answer(claim), _answer("", ("job_ledger", "{}")),
    )
    followed = sent[1]["input"][len(request["input"]) :]
    assert followed[-2]["content"] == UNBACKED_NOTE.format(said="looked it up")
    assert line["guarded"]["kind"] == "unbacked"
    assert line["guarded"]["first_text"] == claim
    assert line["calls"] == [{"name": "job_ledger", "args": {}}]


CAL_LINE = 'Next on his calendar: 3:30 pm "Dentist".'
STATE = "[Current state | from the program, not the user's words]"
SENT = "[State when this was said | from the program, not the user's words]"


def _with_state(*lines: str, shape: str = "chat", older: str = "") -> dict[str, Any]:
    """A request whose live user message carries a state block of these lines (ADR 0249)."""
    live = {"role": "user", "content": "\n".join([STATE, *lines]) + f"\n\n明天呢\n\n{LANGUAGE}"}
    history = [{"role": "user", "content": older}] if older else []
    if shape == "responses":
        return {"input": [*history, live], "instructions": "stub"}
    return {"messages": [{"role": "system", "content": "stub"}, *history, live]}


@pytest.mark.parametrize("shape", ["chat", "responses"])
@pytest.mark.parametrize(
    ("answer", "lines", "guarded"),
    [
        # The calendar line backs what his calendar holds, and only that.
        ("你的日历明天是空的。", (CAL_LINE,), False),
        ("你的日历明天是空的。", ("Doing now (as of 5 min ago): X.",), True),
        ("你的日历明天是空的。", (), True),  # a log from before the line existed
        ("我查了日历，明天没有安排。", (CAL_LINE,), True),  # noqa: RUF001 - Chinese comma
        ("Your calendar is clear tomorrow.", (CAL_LINE,), False),
        ("I checked your calendar and you are free.", (CAL_LINE,), True),
    ],
)
def test_the_exam_reads_the_calendar_line_in_the_logged_request(
    monkeypatch: pytest.MonkeyPatch, shape: str, answer: str, lines: tuple[str, ...],
    guarded: bool,  # noqa: FBT001 - a parametrized table cell
) -> None:
    """Chat and Responses shapes: the guard sees the line the way the decision loop does."""
    request = _with_state(*lines, shape=shape)
    line, sent = _run(monkeypatch, request, _answer(answer), _answer("没有。"))
    assert ("guarded" in line) is guarded
    assert len(sent) == (2 if guarded else 1)


def test_a_calendar_line_in_an_earlier_replayed_block_backs_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only this turn's block counts: a replayed one in the history is an older reading."""
    older = f"{SENT}\n{CAL_LINE}\n\n昨天呢"
    request = _with_state("Doing now (as of 5 min ago): X.", older=older)
    line, _ = _run(monkeypatch, request, _answer("你的日历明天是空的。"), _answer("没有。"))
    assert line["guarded"]["kind"] == "unbacked"


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
