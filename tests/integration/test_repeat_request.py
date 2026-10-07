"""「什么?」 / "say that again": the last spoken answer again, with no model request.

A repair request waited a whole model round (2.4 s median in the
2026-09-28 live test) for an answer that paraphrased, or talked about,
what Jarvis had just said. Now L3 says the last answer's voice text again,
word for word, and only a whole utterance that asks for nothing else counts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from jarvis.decision import decide
from jarvis.decision.stream_envelope import compose_envelope
from jarvis.state.event_log import emit_event
from tests.integration.test_conversational_turn_no_gate_downgrade import _build_ctx

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

_SAID = "现在是下午三点，会议四点开始。"  # noqa: RUF001 — a real Chinese answer's punctuation.


def _answered_aloud(conn: sqlite3.Connection) -> None:
    """Allen asked by voice and the answer's voice text was spoken."""
    turn = {"turn_id": "T1"}
    emit_event(
        conn,
        type="utterance.received",
        payload={**turn, "transcript": "现在几点，会议几点？", "channel": "inherent_wake"},  # noqa: RUF001
        correlation=turn,
    )
    response = {**turn, "response_id": "R1", "response_group_id": "G1", "phase": "final"}
    emit_event(
        conn,
        type="surface.response_open",
        payload={**response, "kind": "text", "query": "q", "channel": "both"},
        correlation=turn,
    )
    emit_event(
        conn,
        type="surface.response_emitted",
        payload={
            **response,
            "channel": "both",
            "text": compose_envelope(_SAID, "15:00；会议 16:00，二号会议室。"),  # noqa: RUF001
            "voice_text": _SAID,
        },
        correlation=turn,
    )


def _decide(tmp_path: Path, heard: str) -> tuple[str, int]:
    ctx, conn, llm = _build_ctx(tmp_path, draft_text="the model's own answer")
    try:
        _answered_aloud(conn)
        trigger = emit_event(
            conn,
            type="utterance.received",
            payload={"turn_id": "T2", "transcript": heard, "channel": "inherent_wake"},
            correlation={"turn_id": "T2"},
        )
        result = decide(trigger, ctx)
    finally:
        conn.close()
    assert result.response_plan is not None
    return result.response_plan.text, llm.chat_calls


@pytest.mark.parametrize(
    "heard",
    [
        "什么？",  # noqa: RUF001
        # How final ASR heard it in the 2026-09-29 live test.
        "什么。",
        "你刚才说什么？",  # noqa: RUF001
        "啊？",  # noqa: RUF001
        "再说一遍",
        "你再说一遍吧。",
        "我没听清。",
        "What?",
        "Pardon?",
        "Say that again, please.",
        "I didn't catch that.",
    ],
)
def test_asking_to_hear_it_again_repeats_the_last_spoken_answer(tmp_path: Path, heard: str) -> None:
    """Word for word, the voice text only, and the model is never asked."""
    assert _decide(tmp_path, heard) == (_SAID, 0)


@pytest.mark.parametrize(
    "heard",
    [
        "什么是量子计算？",  # noqa: RUF001
        "啊",
        "再说一遍你的名字",
        "What time is the meeting?",
        # A passage is named: the instant path would say the wrong (last) answer.
        "讲故事那段再说一遍",
        "刚才说天气那句再说一遍",
        "Repeat what you said about the interview.",
    ],
)
def test_anything_more_than_the_request_goes_to_the_model(tmp_path: Path, heard: str) -> None:
    """A question that only starts like a repair request is a question."""
    text, model_requests = _decide(tmp_path, heard)
    assert model_requests >= 1
    assert _SAID not in text
