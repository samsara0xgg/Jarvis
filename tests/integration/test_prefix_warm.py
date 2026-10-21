"""After a turn, one capped request ends exactly where the next turn's prompt diverges.

OpenAI finds a cached prompt only where an earlier request ended. Before this,
no request ended at the end of the history, so each turn's first request found
only the system prompt and tools cached (~6k of ~45k tokens, 2026-09-30). Real
``drive_turn``/``decide()``, a real memory.db and the real OpenAI SDK against a
localhost /v1/responses peer that records every request body.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from jarvis.runtime import _warm_next_prefix
from jarvis.state.memory_db import append_record
from tests.support.prefix_warm import (
    assert_prefix_of,
    wait_for_requests,
    warm_costs,
    warm_runtime,
)
from tests.support.routine import drive_streaming_turn, wait_for
from tests.support.spoken import (
    SpokenPeer,
    fixture_key,  # noqa: F401 — autouse fixture: the key and the phase labels
    spoken_intent,
)

if TYPE_CHECKING:
    from pathlib import Path



def test_the_next_turns_first_request_extends_the_warm_request(tmp_path: Path) -> None:
    """The warm ends on this turn's answer; the next request is it plus his new words."""
    outputs = [[("final_answer", "今天是晴天。")], [("final_answer", "好")],
               [("final_answer", "明天也是。")]]
    with SpokenPeer(outputs) as peer:
        runtime, _memory = warm_runtime(tmp_path, peer.url, warm=True)
        drive_streaming_turn(runtime, spoken_intent(runtime.conn, "turn-1", "今天天气怎么样"))
        wait_for_requests(peer, 2)
        wait_for(lambda: warm_costs(runtime) >= 1)
        # One warm for the turn: its answer's request and the warm, and nothing else yet.
        assert len(peer.requests) == 2
        assert warm_costs(runtime) == 1
        drive_streaming_turn(runtime, spoken_intent(runtime.conn, "turn-2", "明天呢"))
    # Turn 2 warms the prefix for turn 3 in its own time, so it may or may not be a fourth request.
    first, warm, second = (body for _path, body in peer.requests[:3])
    assert_prefix_of(warm, second)
    assert warm["input"][-2:] == [
        {"role": "user", "content": "今天天气怎么样"},
        {"role": "assistant", "content": "今天是晴天。"},
    ]
    # Without the warm request, the longest earlier request the second turn
    # starts with is none: turn 1 carried its state lines in its own message.
    assert second["input"][: len(first["input"])] != first["input"]


def test_words_left_unanswered_are_left_to_the_next_turn(tmp_path: Path) -> None:
    """History ending on Allen's words: the next turn folds them into its own message."""
    with SpokenPeer([[("final_answer", "好")], [("final_answer", "在的。")]]) as peer:
        runtime, memory = warm_runtime(tmp_path, peer.url, warm=True)
        append_record(memory.db_path, record_id="a-open", source="allen", text="停")
        run_client = runtime.llm_session_factory
        assert run_client is not None
        _warm_next_prefix(
            runtime,
            memory,
            llm_client=run_client.create(run_client.snapshot(), response_id="warm-test"),
            system_prompt=runtime.system_prompt,
            responses=True,
        )
        wait_for_requests(peer, 1)
        drive_streaming_turn(runtime, spoken_intent(runtime.conn, "turn-after", "你还在吗"))
    warm, turn = (body for _path, body in peer.requests[:2])  # the turn warms the next one too
    assert warm["input"][-1] == {"role": "assistant", "content": "回答2"}
    assert_prefix_of(warm, turn)
    assert turn["input"][len(warm["input"])]["content"].startswith("停\n\n")


def test_off_sends_nothing_extra(tmp_path: Path) -> None:
    """The shipped default: two turns, two requests."""
    outputs = [[("final_answer", "晴天。")], [("final_answer", "也是。")]]
    with SpokenPeer(outputs) as peer:
        runtime, _memory = warm_runtime(tmp_path, peer.url, warm=False)
        drive_streaming_turn(runtime, spoken_intent(runtime.conn, "turn-1", "今天天气怎么样"))
        drive_streaming_turn(runtime, spoken_intent(runtime.conn, "turn-2", "明天呢"))
        time.sleep(0.3)
    assert len(peer.requests) == 2
