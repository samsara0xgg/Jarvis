"""A spoken turn speaks the model's own words as it writes them.

docs/plans/speak-as-written-proposal.md. Real ``drive_turn``/``decide()``, a
real on-disk Event Log, the real OpenAI SDK and a localhost peer shaped like
/v1/responses; only the model's output is scripted. The peer can hold a stream
open so a test can prove a sentence was committed before the answer completed.
"""

# ruff: noqa: RUF001, RUF003 — the first clause ends at a fullwidth comma.
from __future__ import annotations

import asyncio
import contextlib
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Self

import pytest

from jarvis import runtime as runtime_module
from jarvis.shared.realtime import Wave4ResponseFlags
from jarvis.state.event_log import emit_event
from tests.integration.test_wire_routine_streaming import (
    _drive,
    _payloads,
    _rows,
    _runtime,
    _wait_for,
)

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from jarvis.runtime import JarvisRuntime
    from jarvis.shared import Event

_LINE = "我看一下你的备忘录。"


def _frames(items: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """One /v1/responses stream: ``(phase, text)`` messages and ``("call", name)`` calls."""
    response = {
        "id": "provider-response", "object": "response", "created_at": 1,
        "model": "fixture-model", "output": [], "tools": [],
        "tool_choice": "auto", "parallel_tool_calls": True,
    }
    frames: list[dict[str, Any]] = [
        {"type": "response.created", "response": {**response, "status": "in_progress"}},
    ]
    for index, (kind, text) in enumerate(items):
        if kind == "call":
            call = {"type": "function_call", "id": f"fc-{index}", "call_id": f"call-{index}",
                    "name": text}
            frames += [
                {"type": "response.output_item.added", "output_index": index,
                 "item": {**call, "arguments": "", "status": "in_progress"}},
                {"type": "response.output_item.done", "output_index": index,
                 "item": {**call, "arguments": "{}", "status": "completed"}},
            ]
            continue
        item = {"type": "message", "id": f"{kind}-{index}", "role": "assistant", "phase": kind}
        frames.append({"type": "response.output_item.added", "output_index": index,
                       "item": {**item, "status": "in_progress", "content": []}})
        frames += [
            {"type": "response.output_text.delta", "output_index": index,
             "item_id": item["id"], "content_index": 0, "delta": piece, "logprobs": []}
            for piece in re.findall(r".{1,6}", text)
        ]
        frames.append({"type": "response.output_item.done", "output_index": index, "item": {
            **item, "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }})
    usage = {
        "input_tokens": 12, "output_tokens": 6, "total_tokens": 18,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens_details": {"reasoning_tokens": 0},
    }
    frames.append({
        "type": "response.completed",
        "response": {**response, "status": "completed", "usage": usage},
    })
    return [{**frame, "sequence_number": number} for number, frame in enumerate(frames)]


class _Peer:
    """Localhost /v1/responses peer: request N streams ``outputs[N]``."""

    def __init__(
        self, outputs: list[list[tuple[str, str]]], *, hold: bool = False, hold_at: str = "。.",
    ) -> None:
        self.outputs = list(outputs)
        self.hold = hold
        self.hold_at = hold_at
        self.holding = threading.Event()
        self.release = threading.Event()
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.completed = 0
        self.url = ""
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)

    def __enter__(self) -> Self:
        self._thread.start()
        assert self._ready.wait(5)
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release.set()
        self._stop.set()
        self._thread.join(5)

    async def _main(self) -> None:
        server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1"
        self._ready.set()
        try:
            await asyncio.get_running_loop().run_in_executor(None, self._stop.wait)
        finally:
            server.close()
            await server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            lines = header.decode().split("\r\n")
            fields = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
            body = json.loads(await reader.readexactly(int(fields["Content-Length"])))
            self.requests.append((lines[0].split()[1], body))
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n"
            )
            answer = ""
            for frame in _frames(self.outputs.pop(0)):
                writer.write(f"event: {frame['type']}\ndata: {json.dumps(frame)}\n\n".encode())
                await writer.drain()
                if str(frame.get("item_id", "")).startswith("final_answer"):
                    answer += frame["delta"]
                if self.hold and re.search(self.hold_at, answer):
                    # Hold the answer open once its first sentence is followed by more.
                    self.hold = False
                    self.holding.set()
                    await asyncio.get_running_loop().run_in_executor(None, self.release.wait)
            self.completed += 1
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()


@pytest.fixture(autouse=True)
def _fixture_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROUTINE_STREAM_FIXTURE_KEY", "synthetic")
    # The peer is localhost; the route opens only where messages carry labels.
    monkeypatch.setattr(runtime_module, "_labels_phases", lambda _base_url: True)


def _spoken_runtime(tmp_path: Path, url: str) -> JarvisRuntime:
    runtime = _runtime(tmp_path, url, routine=False)
    return replace(runtime, response_flags=replace(runtime.response_flags, spoken_streaming=True))


def _spoken(conn: sqlite3.Connection, turn_id: str, transcript: str) -> Event:
    return emit_event(
        conn,
        type="surface.user_intent",
        payload={"transcript": transcript, "turn_id": turn_id, "channel": "inherent_ptt"},
        correlation={"turn_id": turn_id},
    )


def test_the_answer_speaks_sentence_by_sentence_from_one_request(tmp_path: Path) -> None:
    """The first sentence commits while the answer is still open; nothing rewrites it."""
    answer = "我能听见你。今天也辛苦了。"
    with _Peer([[("final_answer", answer)]], hold=True) as peer:
        runtime = _spoken_runtime(tmp_path, peer.url)
        intent = _spoken(runtime.conn, "turn-chat", "你能听见我吗")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_drive, runtime, intent)
            _wait_for(lambda: _rows(runtime.conn, "surface.response_chunk"))
            assert peer.completed == 0
            assert not _rows(runtime.conn, "response.completed")
            peer.release.set()
            result = future.result(timeout=20)
    conn = runtime.conn
    (started,) = _payloads(conn, "response.started")
    assert (started["route"], started["emission_mode"]) == ("spoken", "routine_stream")
    chunks = [payload["text"] for payload in _payloads(conn, "surface.response_chunk")]
    assert len(chunks) >= 2
    assert "".join(chunks) == result.response_plan.text == answer
    gates = _payloads(conn, "gate.evaluated")
    assert {(gate["outcome"], gate["classifier_rule_version"]) for gate in gates} == {
        ("permit", "spoken-v1"),
    }
    ((path, body),) = peer.requests
    assert path == "/v1/responses"
    assert body["stream"] is True
    assert body["tools"]
    assert "Your reply is spoken aloud" in json.dumps(body["input"])
    assert _rows(conn, "response.completed")[0][0] < _rows(conn, "turn.ended")[0][0]
    emitted = _payloads(conn, "surface.response_emitted")[0]
    assert emitted["voice_text"] == answer


@pytest.mark.parametrize("first_clause_chars", [6, 0])
def test_the_first_clause_is_said_before_the_first_sentence_ends(
    tmp_path: Path, first_clause_chars: int,
) -> None:
    """``first_clause_chars``: the first chunk ends at a clause; 0 waits for the sentence."""
    answer = "今天北京是晴天，气温二十度左右。明天也差不多。"
    config = {"spoken_streaming": {"enabled": True, "first_clause_chars": first_clause_chars}}
    chars = Wave4ResponseFlags.from_mapping(config).spoken_first_clause_chars
    with _Peer([[("final_answer", answer)]], hold=True, hold_at="，.") as peer:
        runtime = _spoken_runtime(tmp_path, peer.url)
        flags = replace(runtime.response_flags, spoken_first_clause_chars=chars)
        runtime = replace(runtime, response_flags=flags)
        intent = _spoken(runtime.conn, "turn-clause", "北京天气怎么样")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_drive, runtime, intent)
            assert peer.holding.wait(10)  # the model has written 「今天北京是晴天，气温二十」
            if first_clause_chars:
                _wait_for(lambda: _rows(runtime.conn, "surface.response_chunk"))
            else:
                time.sleep(0.5)
            held = [chunk["text"] for chunk in _payloads(runtime.conn, "surface.response_chunk")]
            peer.release.set()
            result = future.result(timeout=20)
    chunks = [payload["text"] for payload in _payloads(runtime.conn, "surface.response_chunk")]
    assert "".join(chunks) == result.response_plan.text == answer
    if first_clause_chars:
        assert held == ["今天北京是晴天，"]
        assert chunks == ["今天北京是晴天，", "气温二十度左右。", "明天也差不多。"]
    else:
        assert held == []
        assert chunks == ["今天北京是晴天，气温二十度左右。", "明天也差不多。"]


def test_the_line_before_a_call_rides_its_proposal_and_never_streams(tmp_path: Path) -> None:
    """The ``commentary`` line goes to ``action.proposed`` and back to the model, not a chunk."""
    answer = "你还没有记过备忘录。"
    outputs = [[("commentary", _LINE), ("call", "list_memos")], [("final_answer", answer)]]
    with _Peer(outputs) as peer:
        runtime = _spoken_runtime(tmp_path, peer.url)
        result = _drive(runtime, _spoken(runtime.conn, "turn-call", "我记过什么"))
    conn = runtime.conn
    (proposed,) = _payloads(conn, "action.proposed")
    assert (proposed["tool_name"], proposed["lead_in"]) == ("list_memos", _LINE)
    assert [payload["text"] for payload in _payloads(conn, "surface.response_chunk")] == [answer]
    assert result.response_plan.text == answer
    assert len(peer.requests) == 2
    second = peer.requests[1][1]["input"]
    assert {"role": "assistant", "content": _LINE, "phase": "commentary"} in second
    assert [item["call_id"] for item in second if item.get("type") == "function_call"] == [
        "call-1",
    ]
    assert [item["call_id"] for item in second if item.get("type") == "function_call_output"] == [
        "call-1",
    ]


def test_a_response_that_ends_on_its_line_gets_one_more_request(tmp_path: Path) -> None:
    """Asked once more to do what it said; a second line alone is then the answer."""
    with _Peer([[("commentary", _LINE)], [("commentary", _LINE)]]) as peer:
        runtime = _spoken_runtime(tmp_path, peer.url)
        result = _drive(runtime, _spoken(runtime.conn, "turn-line", "我记过什么"))
    assert len(peer.requests) == 2
    assert peer.requests[1][1]["input"][-1] == {
        "role": "assistant", "content": _LINE, "phase": "commentary",
    }
    assert result.response_plan.text == _LINE


def test_citation_markup_is_never_spoken(tmp_path: Path) -> None:
    """OpenAI's citation markup goes even when it arrives split across deltas."""
    cite = "\ue200cite\ue202turn0search0\ue202turn0search4\ue201"
    answer = f"这叫瑞利散射。{cite}天空因此是蓝色的。"
    with _Peer([[("final_answer", answer)]]) as peer:
        runtime = _spoken_runtime(tmp_path, peer.url)
        result = _drive(runtime, _spoken(runtime.conn, "turn-cite", "天空为什么是蓝的"))
    chunks = [payload["text"] for payload in _payloads(runtime.conn, "surface.response_chunk")]
    assert "".join(chunks) == result.response_plan.text == "这叫瑞利散射。天空因此是蓝色的。"


def test_an_answer_that_opens_with_markup_is_delivered_whole_unrewritten(tmp_path: Path) -> None:
    """Nothing streams past markup, so the answer goes out complete, as written."""
    answer = "**冰**从空气里吸热所以会融化。"
    cite = "\ue200cite\ue202turn0search0\ue201"
    with _Peer([[("final_answer", answer + cite)]]) as peer:
        runtime = _spoken_runtime(tmp_path, peer.url)
        result = _drive(runtime, _spoken(runtime.conn, "turn-markup", "冰为什么会化"))
    conn = runtime.conn
    assert not [gate for gate in _payloads(conn, "gate.evaluated") if gate["gate"] == "stream_emit"]
    completed_id = _rows(conn, "response.completed")[0][0]
    assert all(row[0] > completed_id for row in _rows(conn, "surface.response_chunk"))
    assert result.response_plan.text == answer
    assert len(peer.requests) == 1
