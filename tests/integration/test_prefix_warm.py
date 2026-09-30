"""After a turn, one capped request ends exactly where the next turn's prompt diverges.

OpenAI finds a cached prompt only where an earlier request ended. Before this,
no request ended at the end of the history, so each turn's first request found
only the system prompt and tools cached (~6k of ~45k tokens, 2026-09-30). Real
``drive_turn``/``decide()``, a real memory.db and the real OpenAI SDK against a
localhost /v1/responses peer that records every request body.
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from jarvis.decision import PREFIX_WARM_MAX_OUTPUT_TOKENS
from jarvis.runtime import _warm_next_prefix
from jarvis.state.memory_db import MemorySettings, append_record
from tests.integration.test_spoken_streaming import (
    _fixture_key,  # noqa: F401 — autouse fixture: the key and the phase labels
    _Peer,
    _spoken,
    _spoken_runtime,
)
from tests.integration.test_wire_routine_streaming import _drive, _payloads

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.runtime import JarvisRuntime


def _runtime(tmp_path: Path, url: str, *, warm: bool) -> tuple[JarvisRuntime, MemorySettings]:
    runtime = _spoken_runtime(tmp_path, url)
    memory = MemorySettings.from_config({}, runtime_root=tmp_path)
    for index in range(3):
        append_record(memory.db_path, record_id=f"a{index}", source="allen", text=f"问题{index}")
        append_record(memory.db_path, record_id=f"j{index}", source="jarvis", text=f"回答{index}")
    runtime = replace(
        runtime,
        memory=memory,
        system_prompt="You are Jarvis. " * 40,
        response_flags=replace(runtime.response_flags, prefix_warm=warm),
    )
    return runtime, memory


def _wait_for_requests(peer: _Peer, count: int) -> None:
    deadline = time.monotonic() + 10
    while len(peer.requests) < count:
        assert time.monotonic() < deadline, f"{len(peer.requests)} requests, wanted {count}"
        time.sleep(0.01)


def _assert_prefix_of(warm: dict[str, Any], turn: dict[str, Any]) -> None:
    assert warm["max_output_tokens"] == PREFIX_WARM_MAX_OUTPUT_TOKENS
    for key in ("model", "instructions", "tools", "store"):
        assert warm[key] == turn[key], key
    assert turn["input"][: len(warm["input"])] == warm["input"]
    assert len(turn["input"]) > len(warm["input"])


def test_the_next_turns_first_request_extends_the_warm_request(tmp_path: Path) -> None:
    """The warm ends on this turn's answer; the next request is it plus his new words."""
    outputs = [[("final_answer", "今天是晴天。")], [("final_answer", "好")],
               [("final_answer", "明天也是。")]]
    with _Peer(outputs) as peer:
        runtime, _memory = _runtime(tmp_path, peer.url, warm=True)
        _drive(runtime, _spoken(runtime.conn, "turn-1", "今天天气怎么样"))
        _wait_for_requests(peer, 2)
        _drive(runtime, _spoken(runtime.conn, "turn-2", "明天呢"))
    first, warm, second = (body for _path, body in peer.requests)
    _assert_prefix_of(warm, second)
    assert warm["input"][-2:] == [
        {"role": "user", "content": "今天天气怎么样"},
        {"role": "assistant", "content": "今天是晴天。"},
    ]
    # Without the warm request, the longest earlier request the second turn
    # starts with is none: turn 1 carried its state lines in its own message.
    assert second["input"][: len(first["input"])] != first["input"]
    kinds = [payload["kind"] for payload in _payloads(runtime.conn, "cost.recorded")]
    assert kinds.count("prefix_warm") == 1


def test_words_left_unanswered_are_left_to_the_next_turn(tmp_path: Path) -> None:
    """History ending on Allen's words: the next turn folds them into its own message."""
    with _Peer([[("final_answer", "好")], [("final_answer", "在的。")]]) as peer:
        runtime, memory = _runtime(tmp_path, peer.url, warm=True)
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
        _wait_for_requests(peer, 1)
        _drive(runtime, _spoken(runtime.conn, "turn-after", "你还在吗"))
    warm, turn = (body for _path, body in peer.requests)
    assert warm["input"][-1] == {"role": "assistant", "content": "回答2"}
    _assert_prefix_of(warm, turn)
    assert turn["input"][len(warm["input"])]["content"].startswith("停\n\n")


def test_off_sends_nothing_extra(tmp_path: Path) -> None:
    """The shipped default: two turns, two requests."""
    outputs = [[("final_answer", "晴天。")], [("final_answer", "也是。")]]
    with _Peer(outputs) as peer:
        runtime, _memory = _runtime(tmp_path, peer.url, warm=False)
        _drive(runtime, _spoken(runtime.conn, "turn-1", "今天天气怎么样"))
        _drive(runtime, _spoken(runtime.conn, "turn-2", "明天呢"))
        time.sleep(0.3)
    assert len(peer.requests) == 2
