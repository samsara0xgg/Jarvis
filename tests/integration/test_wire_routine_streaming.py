"""ADR-0008 Step 8: a routine turn streams permitted sentences through drive_turn.

Real ``drive_turn``/``decide()``, a real on-disk Event Log and a real localhost
OpenAI-shaped provider peer; only the model text is scripted. The peer can hold
a stream open so the test can prove a chunk was committed before completion.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

import pytest

from jarvis import decision as decision_module
from jarvis.decision.gates import ResponsePlan
from jarvis.decision.llm import LLMClient
from jarvis.decision.llm_session import LLMSessionFactory
from jarvis.decision.pre_route import load_tool_cues
from jarvis.decision.response_run import ResponseCancelledError, ResponseRunRegistry
from jarvis.decision.stream_envelope import compose_envelope
from jarvis.deployment import bootstrap_runtime
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.runtime import (
    JarvisRuntime,
    _wave4_response_flags,
    drive_turn,
    make_response_cancel_callable,
)
from jarvis.shared.realtime import Wave1FeatureFlags
from jarvis.state.committed_event_bus import CommittedEventBus
from jarvis.state.event_log import emit_event, iter_events_of_types, open_event_log
from jarvis.surface import voice_media
from jarvis.surface.cli import SurfaceState, parse_response_channels, record_pre_emit_token
from jarvis.surface.cli_render import render_response
from tests.integration.test_wave2_streaming_media import (
    _CallbackPump,
    _config,
    _FakeProvider,
    _player,
    _submit_response,
)

if TYPE_CHECKING:
    from jarvis.shared import Event

_REPO = Path(__file__).resolve().parents[2]
_QUERY = "解释冰为什么融化。"
_SENTENCES = ("冰从周围吸收热量。", "这些热量来自空气。", "所以冰会变成水。")
_ANSWER = "".join(_SENTENCES)
_DELTA_CHARS = 6


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _plan(text: str) -> ResponsePlan:
    return ResponsePlan(
        text=text,
        permission="allow_completion_language",
        downgrade_required=False,
        active_claim_levels=(),
        response_hash=_sha256(text),
        output_risk_class="routine",
        required_gate_mode="sentence",
    )


class _Provider:
    """Localhost OpenAI-shaped peer: SSE for stream requests, JSON for batch ones."""

    def __init__(self, answers: list[str], *, pause_after_chars: int | None = None) -> None:
        self.answers = list(answers)
        self.pause_after_chars = pause_after_chars
        self.release = threading.Event()
        self.requests: list[dict[str, Any]] = []
        self.stream_completed_ms: list[int] = []
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
            fields = dict(
                line.split(": ", 1) for line in header.decode().split("\r\n")[1:] if ": " in line
            )
            body = json.loads(await reader.readexactly(int(fields["Content-Length"])))
            self.requests.append(body)
            text = self.answers.pop(0) if self.answers else "好的。"
            if body.get("stream"):
                await self._stream(writer, text)
            else:
                await self._batch(writer, text)
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def _stream(self, writer: asyncio.StreamWriter, text: str) -> None:
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n"
        )
        pause_at = self.pause_after_chars
        self.pause_after_chars = None
        sent = 0
        for start in range(0, len(text), _DELTA_CHARS):
            delta = text[start : start + _DELTA_CHARS]
            await self._frame(writer, {"content": delta})
            sent += len(delta)
            if pause_at is not None and sent >= pause_at:
                pause_at = None
                await asyncio.get_running_loop().run_in_executor(None, self.release.wait)
        await self._frame(writer, {}, "stop")
        usage = {
            "id": "provider-response",
            "choices": [],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 6,
                "total_tokens": 18,
            },
        }
        writer.write(("data: " + json.dumps(usage) + "\n\ndata: [DONE]\n\n").encode())
        await writer.drain()
        self.stream_completed_ms.append(int(time.time() * 1000))

    async def _frame(
        self, writer: asyncio.StreamWriter, delta: dict[str, Any], finish: str | None = None
    ) -> None:
        frame = {
            "id": "provider-response",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "fixture-model",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        writer.write(("data: " + json.dumps(frame) + "\n\n").encode())
        await writer.drain()

    async def _batch(self, writer: asyncio.StreamWriter, text: str) -> None:
        payload = json.dumps(
            {
                "id": "cmpl",
                "object": "chat.completion",
                "created": 1,
                "model": "fixture-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18},
            }
        ).encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
            + f"Content-Length: {len(payload)}\r\n\r\n".encode()
            + payload
        )
        await writer.drain()


def _runtime(tmp_path: Path, provider_url: str, *, routine: bool = True) -> JarvisRuntime:
    paths = bootstrap_runtime(tmp_path)
    config: dict[str, Any] = {
        "realtime": {
            "enabled": True,
            "concurrency_safety": {
                "transactional_event_append": True,
                "lifecycle_terminal_cas": True,
                "confirmation_dispatch_outbox": False,
                "exactly_once_cost_accounting": True,
            },
            "response": {
                "response_run_lifecycle": True,
                "independent_response_cancel": True,
                "cancel_timeout_ms": 500,
                "routine_streaming": {"enabled": routine},
            },
        },
    }
    llm_config: dict[str, Any] = {
        "provider": "openai",
        "model": "fixture-model",
        "base_url": provider_url,
        "api_key_env": "ROUTINE_STREAM_FIXTURE_KEY",
        "max_tokens": 256,
        "timeout_s": 5,
        "max_retries": 0,
    }
    return JarvisRuntime(
        config=config,
        runtime_paths=paths,
        conn=open_event_log(paths.event_log),
        tool_registry=build_default_registry(),
        lifecycle=ActionLifecycle(),
        llm_client=LLMClient(llm_config),
        system_prompt="",
        wave1_features=Wave1FeatureFlags(
            transactional_event_append=True,
            lifecycle_terminal_cas=True,
            exactly_once_cost_accounting=True,
        ),
        response_flags=_wave4_response_flags(config),
        llm_session_factory=LLMSessionFactory(llm_config),
        response_runs=ResponseRunRegistry(),
        committed_event_bus=CommittedEventBus(),
        tool_cues=load_tool_cues(_REPO / "config" / "tool_cues.yaml"),
    )


def _intent(conn: sqlite3.Connection, turn_id: str, transcript: str = _QUERY) -> Event:
    return emit_event(
        conn,
        type="surface.user_intent",
        payload={"transcript": transcript, "turn_id": turn_id, "channel": "cli_stdin"},
        correlation={"turn_id": turn_id},
    )


def _drive(runtime: JarvisRuntime, intent: Event) -> Any:  # noqa: ANN401 - RunTurnResult
    """Run drive_turn the way the daemon worker thread does, on its own connection."""
    conn = open_event_log(runtime.runtime_paths.event_log)
    try:
        return drive_turn(
            replace(runtime, conn=conn),
            user_intent_event=intent,
            available_surfaces=frozenset(),
            streaming_enabled=True,
        )
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()


def _rows(
    conn: sqlite3.Connection, *types: str
) -> list[tuple[int, str, dict[str, Any], str | None, str]]:
    """``(id, type, payload, source_event_id, event_uid)`` in append order."""
    placeholders = ",".join("?" for _ in types)
    return [
        (int(row[0]), str(row[1]), json.loads(row[2]), row[3], str(row[4]))
        for row in conn.execute(
            "SELECT id, type, payload_json, source_event_id, event_uid FROM events "  # noqa: S608 - fixed type names
            f"WHERE type IN ({placeholders}) ORDER BY id",
            types,
        )
    ]


def _payloads(conn: sqlite3.Connection, event_type: str) -> list[dict[str, Any]]:
    return [row[2] for row in _rows(conn, event_type)]


def _wait_for(predicate: Any, timeout_s: float = 10) -> None:  # noqa: ANN401 - callable
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting"
        time.sleep(0.02)


@pytest.fixture(autouse=True)
def _fixture_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROUTINE_STREAM_FIXTURE_KEY", "synthetic")


def test_routine_turn_streams_chunks_before_completion_then_finalizes(tmp_path: Path) -> None:
    """Chunks commit while the provider is still open; one open, one terminal, one cost."""
    pause = len(_SENTENCES[0])
    with _Provider([_ANSWER], pause_after_chars=pause) as provider:
        runtime = _runtime(tmp_path, provider.url)
        intent = _intent(runtime.conn, "turn-stream")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_drive, runtime, intent)
            _wait_for(lambda: _rows(runtime.conn, "surface.response_chunk"))
            first_chunk = _rows(runtime.conn, "surface.response_chunk")[0]
            assert not provider.stream_completed_ms
            assert not _rows(runtime.conn, "response.completed")
            provider.release.set()
            result = future.result(timeout=20)
    conn = runtime.conn
    started = _payloads(conn, "response.started")
    assert len(started) == 1
    assert started[0]["route"] == "casual_or_explanatory"
    assert started[0]["emission_mode"] == "routine_stream"
    response_id = started[0]["response_id"]
    chunks = _rows(conn, "surface.response_chunk")
    assert [row[2]["text"] for row in chunks] == list(_SENTENCES)
    assert all(row[2]["response_id"] == response_id for row in chunks)
    assert first_chunk[0] < _rows(conn, "response.completed")[0][0]
    gates = _rows(conn, "gate.evaluated")
    assert all(row[2]["gate"] == "stream_emit" and row[2]["outcome"] == "permit" for row in gates)
    assert [row[3] for row in chunks] == [row[4] for row in gates]
    assert len(_rows(conn, "surface.response_open")) == 1
    assert result.response_plan.text == _ANSWER
    assert result.response_plan.text == "".join(row[2]["text"] for row in chunks)
    completed, emitted = (
        _rows(conn, "response.completed")[0],
        _rows(conn, "surface.response_emitted")[0],
    )
    assert completed[0] < emitted[0]
    assert completed[2]["response_hash"] == result.response_plan.response_hash == _sha256(_ANSWER)
    assert emitted[2]["response_id"] == response_id
    assert emitted[2]["voice_text"] == emitted[2]["document_text"] == _ANSWER
    costs = _payloads(conn, "cost.recorded")
    assert len(costs) == 1
    assert costs[0]["disposition"] == "completed"
    ended = _rows(conn, "turn.ended")
    assert len(ended) == 1
    assert ended[0][3] == gates[-1][4]
    assert ended[0][2]["consumed_trigger_event_uid"] == intent.event_uid
    assert completed[0] < ended[0][0]
    assert provider.requests[0].get("tools") is None
    assert [
        row[1]
        for row in _rows(
            conn,
            "surface.response_open",
            "surface.response_chunk",
            "response.completed",
            "turn.ended",
            "surface.response_emitted",
        )
    ] == [
        "surface.response_open",
        "surface.response_chunk",
        "surface.response_chunk",
        "surface.response_chunk",
        "response.completed",
        "turn.ended",
        "surface.response_emitted",
    ]


def test_tool_cue_request_takes_the_full_text_path_with_tools(tmp_path: Path) -> None:
    """A cue-matched request is routed ``action``: tools offered, no early chunk."""
    with _Provider(["<voice>好的。</voice><document>文件说明。</document>"]) as provider:
        runtime = _runtime(tmp_path, provider.url)
        result = _drive(runtime, _intent(runtime.conn, "turn-cue", "帮我打开这个文件"))
        assert provider.requests[0].get("stream") is not True
        assert provider.requests[0]["tools"]
    conn = runtime.conn
    started = _payloads(conn, "response.started")[0]
    assert (started["route"], started["emission_mode"]) == ("action", "full_text")
    completed_id = _rows(conn, "response.completed")[0][0]
    assert all(row[0] > completed_id for row in _rows(conn, "surface.response_chunk"))
    assert result.response_plan.text.startswith("<voice>")


def test_enveloped_stream_keeps_chunks_tag_free_and_document_in_plan(tmp_path: Path) -> None:
    """Tags split across deltas never reach a chunk; the document lands in the plan."""
    voice = _SENTENCES[0] + _SENTENCES[1]
    document = "冰在 0 度以上融化。"
    answer = f"<voice>\n{voice}\n</voice>\n<document>\n{document}\n</document>"
    with _Provider([answer]) as provider:
        runtime = _runtime(tmp_path, provider.url)
        result = _drive(runtime, _intent(runtime.conn, "turn-envelope"))
    conn = runtime.conn
    chunks = [row[2]["text"] for row in _rows(conn, "surface.response_chunk")]
    assert chunks == [_SENTENCES[0], _SENTENCES[1]]
    assert all("<" not in text for text in chunks)
    assert result.response_plan.text == compose_envelope(voice, document)
    channels = parse_response_channels(result.response_plan.text)
    assert (channels.voice, channels.document) == (voice, document)
    emitted = _payloads(conn, "surface.response_emitted")[0]
    assert (emitted["voice_text"], emitted["document_text"]) == (voice, document)


def test_consequential_segment_buffers_seals_and_suffix_is_regenerated_once(tmp_path: Path) -> None:
    """A consequential sentence gets a durable buffer verdict; nothing after it streams."""
    consequential = "我已经删除了文件。"
    with _Provider([_SENTENCES[0] + consequential + _SENTENCES[2], _SENTENCES[2]]) as provider:
        runtime = _runtime(tmp_path, provider.url)
        result = _drive(runtime, _intent(runtime.conn, "turn-seal"))
        assert len(provider.requests) == 2
        assert provider.requests[1]["messages"][-1] == {
            "role": "assistant",
            "content": _SENTENCES[0],
        }
    conn = runtime.conn
    gates = [row[2] for row in _rows(conn, "gate.evaluated")]
    assert [gate["outcome"] for gate in gates] == ["permit", "buffer_full_text"]
    assert gates[1]["candidate_risk"] == "consequential_claim"
    assert [row[2]["text"] for row in _rows(conn, "surface.response_chunk")] == [_SENTENCES[0]]
    assert result.response_plan.text == _SENTENCES[0] + _SENTENCES[2]
    assert [cost["disposition"] for cost in _payloads(conn, "cost.recorded")] == [
        "completed",
        "completed",
    ]
    assert len(_rows(conn, "response.completed")) == 1


_CONSEQUENTIAL = "我已经删除了文件。"


def _seal_then_regenerate(tmp_path: Path, name: str, regenerated: str) -> Any:  # noqa: ANN401
    """Seal after one permit, then answer the one regeneration with ``regenerated``."""
    sealed = _SENTENCES[0] + _CONSEQUENTIAL + _SENTENCES[2]
    with _Provider([sealed, regenerated]) as provider:
        runtime = _runtime(tmp_path / name, provider.url)
        result = _drive(runtime, _intent(runtime.conn, f"turn-{name}"))
        assert len(provider.requests) == 2
    conn = runtime.conn
    assert [row[2]["text"] for row in _rows(conn, "surface.response_chunk")] == [_SENTENCES[0]]
    assert not _rows(conn, "response.failed")
    assert len(_rows(conn, "response.completed")) == 1
    return result, conn


def test_regenerated_suffix_repeating_the_committed_prefix_keeps_it_once(
    tmp_path: Path,
) -> None:
    """A3(b): the regeneration restates the exposed sentence; the plan says it once."""
    repeat = "冰从周围吸收热量，所以冰会变成水。"  # noqa: RUF001 — the prefix restated without its 。
    result, conn = _seal_then_regenerate(tmp_path, "dedup", repeat)
    assert result.response_plan.text == _SENTENCES[0] + _SENTENCES[2]
    assert result.response_plan.text.count(_SENTENCES[0]) == 1
    assert _payloads(conn, "surface.response_emitted")[0]["voice_text"].count(_SENTENCES[0]) == 1


def test_regeneration_that_only_starts_like_the_prefix_is_kept_whole(tmp_path: Path) -> None:
    """A restatement ends where the text does; a longer word is not one, and is not cut."""
    longer = "冰从周围吸收热量的过程是融化。"
    result, _ = _seal_then_regenerate(tmp_path, "nodedup", longer)
    assert result.response_plan.text == _SENTENCES[0] + longer


def test_regeneration_that_adds_nothing_leaves_the_exposed_sentence_alone(
    tmp_path: Path,
) -> None:
    """A regeneration that only restates the prefix ships the prefix, never twice."""
    result, conn = _seal_then_regenerate(tmp_path, "onlyrepeat", _SENTENCES[0])
    assert result.response_plan.text == _SENTENCES[0]
    assert _payloads(conn, "surface.response_emitted")[0]["voice_text"] == _SENTENCES[0]


def test_cancel_mid_stream_records_prefix_hash_and_one_cost(tmp_path: Path) -> None:
    """Cancel after the first sentence: no later chunk, one cancelled disposition."""
    with _Provider([_ANSWER], pause_after_chars=len(_SENTENCES[0])) as provider:
        runtime = _runtime(tmp_path, provider.url)
        intent = _intent(runtime.conn, "turn-cancel")
        cancel = make_response_cancel_callable(runtime)
        reader = sqlite3.connect(runtime.runtime_paths.event_log, timeout=5)
        with contextlib.closing(reader), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_drive, runtime, intent)
            _wait_for(lambda: _rows(reader, "surface.response_chunk"))
            response_id = _payloads(reader, "response.started")[0]["response_id"]
            assert cancel(response_id, "generation", "operator_request") == "cancelled"
            provider.release.set()
            with pytest.raises(ResponseCancelledError):
                future.result(timeout=20)
    conn = runtime.conn
    cancelled = _payloads(conn, "response.cancelled")
    assert len(cancelled) == 1
    assert cancelled[0]["committed_prefix_hash"] == _sha256(_SENTENCES[0])
    assert [row[2]["text"] for row in _rows(conn, "surface.response_chunk")] == [_SENTENCES[0]]
    assert not _rows(conn, "response.completed", "surface.response_emitted", "turn.ended")
    _wait_for(lambda: _payloads(conn, "cost.recorded"))
    costs = _payloads(conn, "cost.recorded")
    assert [cost["disposition"] for cost in costs] == ["cancelled"]


def test_policy_mismatch_fails_run_and_correction_run_keeps_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pinned channel the policy no longer agrees with fails the run; the correction continues."""
    monkeypatch.setattr(
        decision_module, "attention_policy", lambda *_args, **_kwargs: "queue_review"
    )
    with _Provider([_ANSWER, _SENTENCES[2]]) as provider:
        runtime = _runtime(tmp_path, provider.url)
        result = _drive(runtime, _intent(runtime.conn, "turn-correct"))
        assert provider.requests[1].get("stream") is not True
    conn = runtime.conn
    started = _payloads(conn, "response.started")
    assert len(started) == 2
    failed = _payloads(conn, "response.failed")
    assert len(failed) == 1
    assert failed[0]["response_id"] == started[0]["response_id"]
    assert failed[0]["reason"] == "policy_mismatch"
    assert failed[0]["committed_prefix_hash"] == _sha256(_ANSWER)
    assert started[1]["corrects_response_id"] == started[0]["response_id"]
    assert started[1]["emission_mode"] == "full_text"
    completed = _payloads(conn, "response.completed")
    assert [item["response_id"] for item in completed] == [started[1]["response_id"]]
    emitted = _payloads(conn, "surface.response_emitted")
    assert len(emitted) == 1
    assert emitted[0]["response_id"] == started[1]["response_id"]
    assert emitted[0]["text"].startswith(_ANSWER)
    assert result.response_plan.text == _ANSWER + _SENTENCES[2]
    assert (
        _payloads(conn, "turn.ended")[0]["final_response_hash"]
        == result.response_plan.response_hash
    )


def test_flag_off_keeps_the_full_text_path_and_records_no_route(tmp_path: Path) -> None:
    """With routine_streaming off the same question takes today's path byte for byte."""
    with _Provider([_ANSWER]) as provider:
        runtime = _runtime(tmp_path, provider.url, routine=False)
        _drive(runtime, _intent(runtime.conn, "turn-off"))
        assert provider.requests[0].get("stream") is not True
    started = _payloads(runtime.conn, "response.started")[0]
    assert "route" not in started
    assert started["emission_mode"] == "full_text"


_LONG_TAIL = (
    "冰在温度升高时会慢慢从固体变成液体这个过程叫做融化它需要从周围吸收热量"
    "因此冰块周围的空气会变凉一些这也是夏天冰饮让人觉得凉快的原因"
)


def test_blocked_routine_tail_is_the_approved_suffix(tmp_path: Path) -> None:
    """A tail the assembler cannot bound is never a chunk; finalize still approves it."""
    with _Provider([_SENTENCES[0] + _LONG_TAIL]) as provider:
        runtime = _runtime(tmp_path, provider.url)
        result = _drive(runtime, _intent(runtime.conn, "turn-tail"))
        assert len(provider.requests) == 1
    conn = runtime.conn
    assert [row[2]["text"] for row in _rows(conn, "surface.response_chunk")] == [_SENTENCES[0]]
    assert [row[2]["outcome"] for row in _rows(conn, "gate.evaluated")] == ["permit"]
    assert result.response_plan.text == _SENTENCES[0] + _LONG_TAIL
    emitted = _payloads(conn, "surface.response_emitted")[0]
    assert emitted["voice_text"] == _SENTENCES[0] + _LONG_TAIL
    assert len(_rows(conn, "response.completed")) == 1


def test_delivery_terminal_only_binds_the_run_without_the_streaming_flag(tmp_path: Path) -> None:
    """A CLI caller (streaming_enabled=False) still names the streamed run on emitted."""
    with _Provider([_ANSWER]) as provider:
        runtime = _runtime(tmp_path, provider.url)
        _intent(runtime.conn, "turn-bind")
    plan = _plan(_ANSWER)
    state = record_pre_emit_token(SurfaceState(last_gate_response_hash=None), plan.response_hash)
    _, event = render_response(
        state,
        plan,
        conn=runtime.conn,
        turn_id="turn-bind",
        attention_channel="voice_notify",
        available_surfaces=frozenset(),
        streaming_enabled=False,
        response_id="RESP-bind",
        response_group_id="RGRP-bind",
        delivery_terminal_only=True,
    )
    assert event.type == "surface.response_emitted"
    assert (event.payload["response_id"], event.payload["response_group_id"]) == (
        "RESP-bind",
        "RGRP-bind",
    )
    assert not _rows(runtime.conn, "surface.response_open", "surface.response_chunk")
    with pytest.raises(ValueError, match="response ids"):
        render_response(
            record_pre_emit_token(SurfaceState(last_gate_response_hash=None), plan.response_hash),
            plan,
            conn=runtime.conn,
            turn_id="turn-bind",
            attention_channel="voice_notify",
            available_surfaces=frozenset(),
            delivery_terminal_only=True,
        )


# --- A zero-permit seal degrades to full text on the same run ---------------

_COUNT_QUERY = "从一数到十五，用中文数字"  # noqa: RUF001 — the live Q1 verbatim, fullwidth comma included.
_COUNT = "一二三四五六七八九十。十一十二十三十四十五。"


def _media_rows(conn: sqlite3.Connection, response_id: str) -> list[tuple[int, Event]]:
    """The response's surface trail as ``(row_id, event)``, the media owner's input."""
    rows: list[tuple[int, Event]] = []
    for event in iter_events_of_types(
        conn,
        ("surface.response_open", "surface.response_chunk", "surface.response_emitted"),
    ):
        if event.payload.get("response_id") != response_id:
            continue
        found = conn.execute(
            "SELECT id FROM events WHERE event_uid = ?", (event.event_uid,)
        ).fetchone()
        assert found is not None
        rows.append((int(found[0]), event))
    return rows


def _play(db_path: Path, rows: list[tuple[int, Event]]) -> None:
    """Hand the trail to the real L5 media owner with the A3 test doubles."""
    provider = _FakeProvider(candidate_count=1)
    player = _player()
    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=replace(_config(), speak_from_segments=True),
        start_player=False,
    )
    try:
        with _CallbackPump(player):
            outcomes = asyncio.run(_submit_response(pipeline, rows))
            assert [outcome.status for outcome in outcomes] == ["accepted"] * len(rows)
            assert pipeline.wait_until_idle(timeout_s=5.0)
    finally:
        assert pipeline.close()


def test_zero_permit_seal_delivers_the_generated_text_as_full_text(tmp_path: Path) -> None:
    """A stream sealed before its first permit speaks its own text on the same run."""
    with _Provider([_COUNT]) as provider:
        runtime = _runtime(tmp_path, provider.url)
        intent = _intent(runtime.conn, "turn-degrade", _COUNT_QUERY)
        result = _drive(runtime, intent)
        assert len(provider.requests) == 1
        assert provider.requests[0].get("stream") is True
    conn = runtime.conn
    started = _payloads(conn, "response.started")
    assert len(started) == 1
    assert (started[0]["route"], started[0]["emission_mode"]) == (
        "casual_or_explanatory",
        "routine_stream",
    )
    response_id = started[0]["response_id"]
    assert not _rows(conn, "response.failed")
    gates = _rows(conn, "gate.evaluated")
    assert [(row[2]["gate"], row[2]["outcome"]) for row in gates] == [
        ("stream_emit", "buffer_full_text"),
        ("pre_emit", "allow_completion_language"),
    ]
    assert gates[0][2]["reasons"] == [
        "outside_evaluated_candidate_form",
        "routine_ceiling_not_met",
    ]
    assert gates[1][2]["attempt"] == 0
    opens = _payloads(conn, "surface.response_open")
    assert len(opens) == 1
    assert (opens[0]["kind"], opens[0]["response_id"]) == ("text", response_id)
    assert opens[0]["attention_channel"] == "voice_notify"
    chunks = _rows(conn, "surface.response_chunk")
    assert "".join(row[2]["text"] for row in chunks) == _COUNT
    assert all(row[2]["response_id"] == response_id for row in chunks)
    emitted = _rows(conn, "surface.response_emitted")
    assert len(emitted) == 1
    assert emitted[0][2]["voice_text"] == _COUNT
    assert emitted[0][2]["response_id"] == response_id
    assert result.response_plan.text == _COUNT
    assert [cost["disposition"] for cost in _payloads(conn, "cost.recorded")] == ["completed"]
    ended = _rows(conn, "turn.ended")
    assert len(ended) == 1
    assert ended[0][3] == gates[1][4]
    assert ended[0][2]["final_response_hash"] == result.response_plan.response_hash
    assert [row[1] for row in _rows(
        conn,
        "response.started",
        "response.completed",
        "response.failed",
        "surface.response_open",
        "turn.ended",
        "surface.response_emitted",
    )] == [
        "response.started",
        "turn.ended",
        "response.completed",
        "surface.response_open",
        "surface.response_emitted",
    ]

    _play(runtime.runtime_paths.event_log, _media_rows(conn, response_id))
    playback = _rows(conn, "surface.playback_started", "surface.playback_completed")
    assert [row[1] for row in playback] == [
        "surface.playback_started",
        "surface.playback_completed",
    ]
    assert all(row[2]["response_id"] == response_id for row in playback)
    assert playback[1][2]["heard_text"] == _COUNT
