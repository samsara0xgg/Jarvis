"""A spoken turn speaks the model's own words as it writes them.

docs/plans/speak-as-written-proposal.md. Real ``drive_turn``/``decide()``, a
real on-disk Event Log, the real OpenAI SDK and a localhost peer shaped like
/v1/responses; only the model's output is scripted. The peer can hold a stream
open so a test can prove a sentence was committed before the answer completed.
"""

# ruff: noqa: RUF001, RUF003 — the first clause ends at a fullwidth comma.
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from jarvis.shared.realtime import Wave4ResponseFlags
from tests.support.routine import (
    drive_streaming_turn,
    logged_payloads,
    logged_rows,
    wait_for,
)
from tests.support.spoken import (
    SpokenPeer,
    fixture_key,  # noqa: F401 - autouse fixture of the spoken-route peer
    spoken_intent,
    spoken_runtime,
)

if TYPE_CHECKING:
    from pathlib import Path


_LINE = "我看一下你的备忘录。"
_BLOCK = "<claims></claims>"  # ADR 0320: every answer opens with its claims block


def test_the_answer_speaks_sentence_by_sentence_from_one_request(tmp_path: Path) -> None:
    """The first sentence commits while the answer is still open; nothing rewrites it."""
    answer = "我能听见你。今天也辛苦了。"
    with SpokenPeer([[("final_answer", _BLOCK + answer)]], hold=True) as peer:
        runtime = spoken_runtime(tmp_path, peer.url)
        intent = spoken_intent(runtime.conn, "turn-chat", "你能听见我吗")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(drive_streaming_turn, runtime, intent)
            wait_for(lambda: logged_rows(runtime.conn, "surface.response_chunk"))
            assert peer.completed == 0
            assert not logged_rows(runtime.conn, "response.completed")
            peer.release.set()
            result = future.result(timeout=20)
    conn = runtime.conn
    (started,) = logged_payloads(conn, "response.started")
    assert (started["route"], started["emission_mode"]) == ("spoken", "routine_stream")
    chunks = [payload["text"] for payload in logged_payloads(conn, "surface.response_chunk")]
    assert len(chunks) >= 2
    assert "".join(chunks) == result.response_plan.text == answer
    gates = logged_payloads(conn, "gate.evaluated")
    assert {(gate["outcome"], gate["classifier_rule_version"]) for gate in gates} == {
        ("permit", "spoken-v1"),
    }
    ((path, body),) = peer.requests
    assert path == "/v1/responses"
    assert body["stream"] is True
    assert body["tools"]
    assert "Your reply is spoken aloud" in json.dumps(body["input"])
    assert logged_rows(conn, "response.completed")[0][0] < logged_rows(conn, "turn.ended")[0][0]
    emitted = logged_payloads(conn, "surface.response_emitted")[0]
    assert emitted["voice_text"] == answer


@pytest.mark.parametrize("first_clause_chars", [6, 0])
def test_the_first_clause_is_said_before_the_first_sentence_ends(
    tmp_path: Path, first_clause_chars: int,
) -> None:
    """``first_clause_chars``: the first chunk ends at a clause; 0 waits for the sentence."""
    answer = "今天北京是晴天，气温二十度左右。明天也差不多。"
    config = {"spoken_streaming": {"enabled": True, "first_clause_chars": first_clause_chars}}
    chars = Wave4ResponseFlags.from_mapping(config).spoken_first_clause_chars
    with SpokenPeer([[("final_answer", _BLOCK + answer)]], hold=True, hold_at="，.") as peer:
        runtime = spoken_runtime(tmp_path, peer.url)
        flags = replace(runtime.response_flags, spoken_first_clause_chars=chars)
        runtime = replace(runtime, response_flags=flags)
        intent = spoken_intent(runtime.conn, "turn-clause", "北京天气怎么样")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(drive_streaming_turn, runtime, intent)
            assert peer.holding.wait(10)  # the model has written 「今天北京是晴天，气温二十」
            if first_clause_chars:
                wait_for(lambda: logged_rows(runtime.conn, "surface.response_chunk"))
            else:
                time.sleep(0.5)
            held = [
                chunk["text"] for chunk in logged_payloads(runtime.conn, "surface.response_chunk")
            ]
            peer.release.set()
            result = future.result(timeout=20)
    chunks = [
        payload["text"] for payload in logged_payloads(runtime.conn, "surface.response_chunk")
    ]
    assert "".join(chunks) == result.response_plan.text == answer
    if first_clause_chars:
        assert held == ["今天北京是晴天，"]
        assert chunks == ["今天北京是晴天，", "气温二十度左右。", "明天也差不多。"]
    else:
        assert held == []
        assert chunks == ["今天北京是晴天，气温二十度左右。", "明天也差不多。"]


def test_the_text_before_a_call_is_never_spoken_and_never_goes_back_to_the_model(
    tmp_path: Path,
) -> None:
    """A reply with no block that ends in a call is dropped: not a chunk, not in the history."""
    answer = "你还没有记过备忘录。"
    outputs = [[("commentary", _LINE), ("call", "kept")], [("final_answer", _BLOCK + answer)]]
    with SpokenPeer(outputs) as peer:
        runtime = spoken_runtime(tmp_path, peer.url)
        result = drive_streaming_turn(
            runtime, spoken_intent(runtime.conn, "turn-call", "我记过什么")
        )
    conn = runtime.conn
    (proposed,) = logged_payloads(conn, "action.proposed")
    assert proposed["tool_name"] == "kept"
    assert "lead_in" not in proposed
    assert [payload["text"] for payload in logged_payloads(conn, "surface.response_chunk")] == [
        answer
    ]
    assert result.response_plan.text == answer
    assert len(peer.requests) == 2
    second = peer.requests[1][1]["input"]
    assert _LINE not in json.dumps(second, ensure_ascii=False)
    assert [item["call_id"] for item in second if item.get("type") == "function_call"] == [
        "call-1",
    ]
    assert [item["call_id"] for item in second if item.get("type") == "function_call_output"] == [
        "call-1",
    ]
    # ADR 0135: the language line is the last item once tool results sit above his words.
    assert second[-1]["role"] == "user"
    assert second[-1]["content"].startswith('[Not new words from the user: still answering "')
    assert second[-1]["content"].endswith("\n[Reply language for this turn: Chinese]")


def test_citation_markup_is_never_spoken(tmp_path: Path) -> None:
    """OpenAI's citation markup goes even when it arrives split across deltas."""
    cite = "\ue200cite\ue202turn0search0\ue202turn0search4\ue201"
    answer = f"这叫瑞利散射。{cite}天空因此是蓝色的。"
    with SpokenPeer([[("final_answer", _BLOCK + answer)]]) as peer:
        runtime = spoken_runtime(tmp_path, peer.url)
        result = drive_streaming_turn(
            runtime, spoken_intent(runtime.conn, "turn-cite", "天空为什么是蓝的")
        )
    chunks = [
        payload["text"] for payload in logged_payloads(runtime.conn, "surface.response_chunk")
    ]
    assert "".join(chunks) == result.response_plan.text == "这叫瑞利散射。天空因此是蓝色的。"


def test_an_answer_that_opens_with_markup_is_delivered_whole_unrewritten(tmp_path: Path) -> None:
    """Nothing streams past markup, so the answer goes out complete, as written."""
    answer = "**冰**从空气里吸热所以会融化。"
    cite = "\ue200cite\ue202turn0search0\ue201"
    with SpokenPeer([[("final_answer", _BLOCK + answer + cite)]]) as peer:
        runtime = spoken_runtime(tmp_path, peer.url)
        result = drive_streaming_turn(
            runtime, spoken_intent(runtime.conn, "turn-markup", "冰为什么会化")
        )
    conn = runtime.conn
    assert not [
        gate for gate in logged_payloads(conn, "gate.evaluated") if gate["gate"] == "stream_emit"
    ]
    completed_id = logged_rows(conn, "response.completed")[0][0]
    assert all(row[0] > completed_id for row in logged_rows(conn, "surface.response_chunk"))
    assert result.response_plan.text == answer
    assert len(peer.requests) == 1
