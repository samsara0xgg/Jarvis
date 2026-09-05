"""Actual SDK/SSE sockets and SQLite accounting for the typed stream contract."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import sqlite3
import time
from typing import TYPE_CHECKING, Any, Literal

import pytest

from jarvis.decision.cost_guard import CostRecorder
from jarvis.decision.llm import LLMClient
from jarvis.decision.llm_stream import (
    LLMResponseCompleted,
    LLMResponseFailed,
    LLMTextDelta,
    LLMToolCallCompleted,
    LLMToolCallStarted,
    LLMUsageCompleted,
    StreamProtocolError,
)
from jarvis.decision.stream_sentences import SemanticAssembler
from jarvis.state.event_log import open_event_log
from tests.integration.test_stream_emission_gate import _context, _Run

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from jarvis.decision.llm_stream import LLMStreamEvent


def _oai(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {
        "id": "provider-response",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "fixture-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def _tool(index: int, **function: str) -> dict[str, Any]:
    return {"tool_calls": [{"index": index, "function": function}]}


def _frames(provider: Literal["openai", "anthropic"]) -> list[dict[str, Any]]:
    if provider == "openai":
        return [
            _oai({"content": "Hello. "}),
            _oai(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-a",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": '{"q":'},
                        }
                    ]
                }
            ),
            _oai(
                {
                    "tool_calls": [
                        {
                            "index": 1,
                            "id": "call-b",
                            "type": "function",
                            "function": {"name": "clock", "arguments": '{"zone":'},
                        }
                    ]
                }
            ),
            _oai(_tool(0, arguments='"ice"}')),
            _oai(_tool(1, arguments='"UTC"}')),
            _oai({}, "tool_calls"),
            {
                "id": "provider-response",
                "choices": [],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 6,
                    "total_tokens": 18,
                    "prompt_tokens_details": {"cached_tokens": 4},
                },
            },
        ]
    return [
        {
            "type": "message_start",
            "message": {
                "id": "provider-response",
                "type": "message",
                "role": "assistant",
                "model": "fixture-model",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 1,
                    "cache_read_input_tokens": 4,
                    "cache_creation_input_tokens": 2,
                },
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Hello. "},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {
                "type": "tool_use",
                "id": "call-a",
                "name": "lookup",
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {
                "type": "input_json_delta",
                "partial_json": '{"q":',
            },
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {
                "type": "input_json_delta",
                "partial_json": '"ice"}',
            },
        },
        {"type": "content_block_stop", "index": 1},
        {
            "type": "content_block_start",
            "index": 2,
            "content_block": {
                "type": "tool_use",
                "id": "call-b",
                "name": "clock",
                "input": {"zone": "UTC"},
            },
        },
        {"type": "content_block_stop", "index": 2},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use", "stop_sequence": None},
            "usage": {"output_tokens": 6},
        },
        {"type": "message_stop"},
    ]


class _SSE:
    """A real localhost HTTP peer; observes socket EOF when Jarvis cancels."""

    def __init__(self, frames: list[dict[str, Any]], *, pause_after: int | None = None) -> None:
        self.frames = frames
        self.pause_after = pause_after
        self.release = asyncio.Event()
        self.received = asyncio.Event()
        self.peer_closed = asyncio.Event()
        self.body: dict[str, Any] = {}
        self.path = ""
        self._tasks: set[asyncio.Task[None]] = set()

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        eof: asyncio.Task[bytes] | None = None
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            lines = header.decode().split("\r\n")
            self.path = lines[0].split()[1]
            fields = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
            body = await reader.readexactly(int(fields["Content-Length"]))
            self.body = json.loads(body)
            self.received.set()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            eof = asyncio.create_task(reader.read())
            for index, frame in enumerate(self.frames):
                if index == self.pause_after:
                    release = asyncio.create_task(self.release.wait())
                    done, _pending = await asyncio.wait(
                        {release, eof}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if eof in done:
                        release.cancel()
                        await asyncio.gather(release, return_exceptions=True)
                        self.peer_closed.set()
                        return
                prefix = f"event: {frame['type']}\n" if "type" in frame else ""
                writer.write((prefix + "data: " + json.dumps(frame) + "\n\n").encode())
                await writer.drain()
                await asyncio.sleep(0)
            if self.path.endswith("/chat/completions"):
                writer.write(b"data: [DONE]\n\n")
                await writer.drain()
        finally:
            if eof is not None:
                eof.cancel()
                await asyncio.gather(eof, return_exceptions=True)
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            self._tasks.discard(task)

    @contextlib.asynccontextmanager
    async def running(self) -> AsyncIterator[str]:
        server = await asyncio.start_server(self.serve, "127.0.0.1", 0)
        try:
            yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1"
        finally:
            self.release.set()
            server.close()
            await server.wait_closed()
            for task in tuple(self._tasks):
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)


def _client(provider: str, base_url: str) -> LLMClient:
    return LLMClient(
        {
            "provider": provider,
            "base_url": base_url if provider == "openai" else base_url.removesuffix("/v1"),
            "model": "fixture-model",
            "api_key_env": "TYPED_STREAM_FIXTURE_KEY",
            "max_tokens": 256,
            "timeout_s": 5,
            "max_retries": 0,
        }
    )


def _costs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        json.loads(row[0])
        for row in conn.execute(
            "SELECT payload_json FROM events WHERE type = 'cost.recorded' ORDER BY id",
        )
    ]


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_semantic_candidate_arrives_before_stalled_provider_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: Literal["openai", "anthropic"],
) -> None:
    """Actual SDK text forms a candidate before EOF; cancel closes the same stream."""
    monkeypatch.setenv("TYPED_STREAM_FIXTURE_KEY", "synthetic")
    frames = _frames(provider)
    if provider == "openai":
        frames[0]["choices"][0]["delta"]["content"] = "Ice absorbs heat. Then"
        frames[1:] = [_oai({}, "stop")]
        pause_after = 1
    else:
        frames[2]["delta"]["text"] = "Ice absorbs heat. Then"
        frames[4:] = [
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 6},
            },
            {"type": "message_stop"},
        ]
        pause_after = 3

    async def scenario() -> None:
        peer = _SSE(frames, pause_after=pause_after)
        conn = open_event_log(tmp_path / "events.db")
        try:
            async with peer.running() as url:
                cost_recorder = CostRecorder(conn)
                handle = cost_recorder.stream_events(
                    _client(provider, url),
                    messages=[],
                    system="synthetic",
                    kind="decision",
                    turn_id="typed-semantic-candidate",
                )
                assembler = SemanticAssembler()
                candidate_ready = asyncio.Event()
                candidates: list[str] = []
                seen: list[LLMStreamEvent] = []

                async def consume() -> None:
                    async for event in handle.events():
                        seen.append(event)
                        if isinstance(event, LLMTextDelta):
                            candidates.extend(part.text for part in assembler.feed(event.text))
                            if candidates:
                                candidate_ready.set()

                consumer = asyncio.create_task(consume())
                await asyncio.wait_for(candidate_ready.wait(), 2)
                assert candidates == ["Ice absorbs heat."]
                assert not any(isinstance(event, LLMResponseCompleted) for event in seen)
                assert handle.result is None
                await handle.cancel("test_stop")
                await consumer
                await asyncio.wait_for(peer.peer_closed.wait(), 1)
                assert candidates == ["Ice absorbs heat."]
                assert len(_costs(conn)) == 1
                assert _costs(conn)[0]["disposition"] == "cancelled"
                # No safety gate is called, so a candidate must not become a
                # surface event or any claim of permitted/physical output.
                assert (
                    conn.execute(
                        "SELECT COUNT(*) FROM events WHERE type='surface.response_chunk'",
                    ).fetchone()[0]
                    == 0
                )
        finally:
            conn.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_sdk_stream_assembles_tools_and_final_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: Literal["openai", "anthropic"],
) -> None:
    """Actual SDK SSE decoding preserves tools, request IDs and usage-only tails."""
    monkeypatch.setenv("TYPED_STREAM_FIXTURE_KEY", "synthetic")

    async def scenario() -> None:
        peer = _SSE(_frames(provider))
        conn = open_event_log(tmp_path / "events.db")
        try:
            async with peer.running() as url:
                cost_recorder = CostRecorder(conn)
                messages = [{"role": "user", "content": "synthetic question"}]
                handle = cost_recorder.stream_events(
                    _client(provider, url),
                    messages=messages,
                    system="synthetic system",
                    kind="decision",
                    turn_id="typed-complete",
                )
                messages[0]["content"] = "must not reach the provider"
                events = [event async for event in handle.events()]
                await handle.aclose()
                await handle.aclose()
                proposals = [event for event in events if isinstance(event, LLMToolCallCompleted)]
                assert [json.loads(event.arguments_json) for event in proposals] == [
                    {"q": "ice"},
                    {"zone": "UTC"},
                ]
                assert [event.call_id for event in proposals] == ["call-a", "call-b"]
                assert isinstance(events[-1], LLMResponseCompleted)
                assert len({event.llm_request_id for event in events}) == 1
                usage = [event for event in events if isinstance(event, LLMUsageCompleted)]
                assert len(usage) == 1
                assert usage[0].usage_status == "provider_final"
                assert (usage[0].input_tokens, usage[0].output_tokens) == (12, 6)
                assert usage[0].cache_read_tokens == 4
                assert peer.body["messages"][-1]["content"] == "synthetic question"
                assert peer.path == (
                    "/v1/chat/completions" if provider == "openai" else "/v1/messages"
                )
                costs = _costs(conn)
                assert len(costs) == 1
                assert costs[0]["disposition"] == "completed"
                assert costs[0]["usage_status"] == "provider_final"
                assert costs[0]["provider_response_id"] == "provider-response"
                assert (
                    conn.execute(
                        "SELECT COUNT(*) FROM events WHERE type = 'action.dispatched'"
                    ).fetchone()[0]
                    == 0
                )
                with pytest.raises(StreamProtocolError, match="stream_already_claimed"):
                    handle.events()
        finally:
            conn.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_terminal_usage_must_be_observed_not_inferred(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: Literal["openai", "anthropic"],
) -> None:
    """Initial counts remain partial even when generation has a valid finish."""
    monkeypatch.setenv("TYPED_STREAM_FIXTURE_KEY", "synthetic")
    frames = _frames(provider)
    if provider == "openai":
        frames[0]["usage"] = frames[-1]["usage"]
        frames.pop()
    else:
        frames[-2]["usage"] = {}

    async def scenario() -> None:
        conn = open_event_log(tmp_path / "events.db")
        try:
            async with _SSE(frames).running() as url:
                cost_recorder = CostRecorder(conn)
                handle = cost_recorder.stream_events(
                    _client(provider, url),
                    messages=[],
                    system="synthetic",
                    kind="decision",
                    turn_id="typed-initial-usage",
                )
                events = [event async for event in handle.events()]
                assert isinstance(events[-1], LLMResponseCompleted)
                usage = [event for event in events if isinstance(event, LLMUsageCompleted)]
                assert usage[0].usage_status == "partial"
                assert _costs(conn)[0]["usage_status"] == "partial"
                assert usage[0].output_tokens == (6 if provider == "openai" else 1)
        finally:
            conn.close()

    asyncio.run(scenario())


def test_tool_signal_precedes_text_in_same_provider_chunk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A segment consumer sees the route-freezing signal before co-located text."""
    monkeypatch.setenv("TYPED_STREAM_FIXTURE_KEY", "synthetic")
    frames = _frames("openai")[1:]
    frames[0]["choices"][0]["delta"]["content"] = "I sent it."

    async def scenario() -> None:
        conn = open_event_log(tmp_path / "events.db")
        try:
            async with _SSE(frames).running() as url:
                cost_recorder = CostRecorder(conn)
                handle = cost_recorder.stream_events(
                    _client("openai", url),
                    messages=[],
                    system="synthetic",
                    kind="decision",
                    turn_id="typed-mixed-delta",
                )
                events = [event async for event in handle.events()]
                assert isinstance(events[0], LLMToolCallStarted)
                assert isinstance(events[-1], LLMResponseCompleted)
                assert (
                    next(event for event in events if isinstance(event, LLMTextDelta)).text
                    == "I sent it."
                )
        finally:
            conn.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "problem",
    [
        "missing_message_stop",
        "missing_block_stop",
        "duplicate_finish",
        "late_text",
        "tool_delta_after_stop",
        "duplicate_call_id",
        "provider_error",
    ],
)
def test_anthropic_incomplete_or_inconsistent_stream_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    problem: str,
) -> None:
    """Actual Anthropic SDK never turns a malformed stream into a valid proposal."""
    monkeypatch.setenv("TYPED_STREAM_FIXTURE_KEY", "synthetic")
    frames = _frames("anthropic")
    if problem == "missing_message_stop":
        frames.pop()
    elif problem == "missing_block_stop":
        frames.pop(9)
    elif problem == "duplicate_finish":
        frames.insert(11, copy.deepcopy(frames[10]))
    elif problem == "late_text":
        frames.insert(
            11,
            {
                "type": "content_block_start",
                "index": 3,
                "content_block": {"type": "text", "text": "too late"},
            },
        )
    elif problem == "tool_delta_after_stop":
        frames.insert(10, copy.deepcopy(frames[6]))
    elif problem == "duplicate_call_id":
        frames[8]["content_block"]["id"] = "call-a"
    elif problem == "provider_error":
        frames[3:] = [
            {
                "type": "error",
                "error": {
                    "type": "overloaded_error",
                    "message": "synthetic provider overload",
                },
            }
        ]

    async def scenario() -> None:
        conn = open_event_log(tmp_path / "events.db")
        try:
            async with _SSE(frames).running() as url:
                cost_recorder = CostRecorder(conn)
                handle = cost_recorder.stream_events(
                    _client("anthropic", url),
                    messages=[],
                    system="synthetic",
                    kind="decision",
                    turn_id="typed-anthropic-failure",
                )
                events = [event async for event in handle.events()]
                assert isinstance(events[-1], LLMResponseFailed)
                assert not any(isinstance(event, LLMToolCallCompleted) for event in events)
                await handle.aclose()
                assert len(_costs(conn)) == 1
                assert _costs(conn)[0]["disposition"] == "error"
                assert _costs(conn)[0]["usage_status"] == "partial"
        finally:
            conn.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_cancel_before_first_read_never_connects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: Literal["openai", "anthropic"],
) -> None:
    """A handle stopped before consumption cannot reopen a request on subscription."""
    monkeypatch.setenv("TYPED_STREAM_FIXTURE_KEY", "synthetic")

    async def scenario() -> None:
        conn = open_event_log(tmp_path / "events.db")
        peer = _SSE(_frames(provider))
        try:
            async with peer.running() as url:
                cost_recorder = CostRecorder(conn)
                handle = cost_recorder.stream_events(
                    _client(provider, url),
                    messages=[],
                    system="synthetic",
                    kind="decision",
                    turn_id="typed-no-dispatch",
                )
                await handle.cancel("superseded_before_read")
                assert [event async for event in handle.events()] == []
                await handle.aclose()
                assert not peer.received.is_set()
                assert len(_costs(conn)) == 1
                assert _costs(conn)[0]["disposition"] == "cancelled"
                assert _costs(conn)[0]["usage_status"] == "unavailable"
        finally:
            conn.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "problem",
    [
        "incomplete_json",
        "duplicate_key",
        "missing_finish",
        "missing_arguments",
        "identity_changed",
        "nonfinite",
        "exponent_overflow",
        "late_text",
        "negative_usage",
        "unsupported_tool",
    ],
)
def test_malformed_stream_never_completes_any_tool(  # noqa: C901 — protocol mutation table.
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    problem: str,
) -> None:
    """A valid first proposal cannot escape a malformed second proposal or response."""
    monkeypatch.setenv("TYPED_STREAM_FIXTURE_KEY", "synthetic")
    frames = _frames("openai")
    if problem == "incomplete_json":
        frames[4] = _oai(_tool(1, arguments='"UTC"'))
    elif problem == "duplicate_key":
        frames[4] = _oai(_tool(1, arguments='"UTC","zone":"other"}'))
    elif problem == "missing_finish":
        frames.pop(5)
    elif problem == "missing_arguments":
        frames[2]["choices"][0]["delta"]["tool_calls"][0]["function"].pop("arguments")
        frames.pop(4)
    elif problem == "identity_changed":
        frames[3]["id"] = "foreign-response"
    elif problem == "nonfinite":
        frames[4] = _oai(_tool(1, arguments="NaN}"))
    elif problem == "exponent_overflow":
        frames[4] = _oai(_tool(1, arguments="1e9999}"))
    elif problem == "late_text":
        frames.insert(6, _oai({"content": "after terminal"}))
    elif problem == "negative_usage":
        frames[-1]["usage"]["prompt_tokens"] = -1
    elif problem == "unsupported_tool":
        frames[1]["choices"][0]["delta"]["tool_calls"][0]["type"] = "unknown"

    async def scenario() -> None:
        conn = open_event_log(tmp_path / "events.db")
        try:
            async with _SSE(frames).running() as url:
                cost_recorder = CostRecorder(conn)
                handle = cost_recorder.stream_events(
                    _client("openai", url),
                    messages=[],
                    system="fixture",
                    kind="decision",
                    turn_id="typed-malformed",
                )
                events = [event async for event in handle.events()]
                assert isinstance(events[-1], LLMResponseFailed)
                assert not any(isinstance(event, LLMToolCallCompleted) for event in events)
                assert len(_costs(conn)) == 1
                assert _costs(conn)[0]["disposition"] == "error"
                await handle.aclose()
                assert len(_costs(conn)) == 1
        finally:
            conn.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("consumer_cancel", [False, True])
def test_cancel_closes_a_real_stalled_sse_read_and_records_cost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: Literal["openai", "anthropic"],
    *,
    consumer_cancel: bool,
) -> None:
    """Cancellation reaches the TCP peer while it is withholding the next token."""
    monkeypatch.setenv("TYPED_STREAM_FIXTURE_KEY", "synthetic")

    async def scenario() -> None:
        peer = _SSE(_frames(provider), pause_after=1 if provider == "openai" else 3)
        conn = open_event_log(tmp_path / "events.db")
        try:
            async with peer.running() as url:
                cost_recorder = CostRecorder(conn)
                handle = cost_recorder.stream_events(
                    _client(provider, url),
                    messages=[],
                    system="synthetic",
                    kind="decision",
                    turn_id="typed-cancel",
                )
                seen: list[LLMStreamEvent] = []
                heard_text = asyncio.Event()

                async def consume() -> None:
                    async for event in handle.events():
                        seen.append(event)
                        if isinstance(event, LLMTextDelta):
                            heard_text.set()

                task = asyncio.create_task(consume())
                await asyncio.wait_for(heard_text.wait(), 2)
                started = time.monotonic()
                if consumer_cancel:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    result = await handle.cancel("operator_stop")
                    assert result.outcome == "cancelled"
                    await task
                await asyncio.wait_for(peer.peer_closed.wait(), 1)
                assert time.monotonic() - started < 1
                count_after_cancel = len(seen)
                await handle.aclose()
                assert len(seen) == count_after_cancel
                costs = _costs(conn)
                assert len(costs) == 1
                assert costs[0]["disposition"] == "cancelled"
                expected_usage = "partial" if provider == "anthropic" else "unavailable"
                assert costs[0]["usage_status"] == expected_usage
        finally:
            conn.close()

    asyncio.run(scenario())


def test_failed_accounting_retries_same_disposition_without_reopening_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cost commit failure cannot be rewritten as cancellation during cleanup."""
    monkeypatch.setenv("TYPED_STREAM_FIXTURE_KEY", "synthetic")

    async def scenario() -> None:
        conn = open_event_log(tmp_path / "events.db")
        fail_once = True

        def failure(stage: str) -> None:
            nonlocal fail_once
            if stage == "before_commit" and fail_once:
                fail_once = False
                message = "injected accounting rollback"
                raise sqlite3.OperationalError(message)

        try:
            async with _SSE(copy.deepcopy(_frames("openai"))).running() as url:
                cost_recorder = CostRecorder(conn, failure_injector=failure)
                handle = cost_recorder.stream_events(
                    _client("openai", url),
                    messages=[],
                    system="synthetic",
                    kind="decision",
                    turn_id="typed-cost-failure",
                )
                seen: list[LLMStreamEvent] = []

                async def consume() -> None:
                    async for event in handle.events():
                        seen.append(event)  # noqa: PERF401 — preserve events before the error.

                with pytest.raises(sqlite3.OperationalError, match="accounting rollback"):
                    await consume()
                assert not any(isinstance(event, LLMToolCallCompleted) for event in seen)
                assert _costs(conn) == []
                await handle.aclose()
                assert len(_costs(conn)) == 1
                assert _costs(conn)[0]["disposition"] == "completed"
                assert _costs(conn)[0]["usage_status"] == "provider_final"
        finally:
            conn.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_actual_sdk_commits_permitted_prefix_before_provider_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: Literal["openai", "anthropic"],
) -> None:
    """SSE socket -> assembler -> real risk gate -> L2 -> L5, while EOF is withheld."""
    monkeypatch.setenv("TYPED_STREAM_FIXTURE_KEY", "synthetic")

    async def scenario() -> None:
        text = "冰从周围吸收热量。"
        if provider == "openai":
            frames = [_oai({"content": text + "这些热量"}), _oai({}, "stop")]
            pause_after = 1
        else:
            frames = _frames("anthropic")[:4]
            frames[2]["delta"]["text"] = text + "这些热量"
            frames.extend(
                [
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn"},
                        "usage": {"output_tokens": 8},
                    },
                    {"type": "message_stop"},
                ]
            )
            pause_after = 3
        peer = _SSE(frames, pause_after=pause_after)
        with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
            async with peer.running() as url:
                run = _Run(
                    conn,
                    _context(),
                    {
                        "provider": provider,
                        "base_url": url if provider == "openai" else url.removesuffix("/v1"),
                        "model": "fixture-model",
                        "api_key_env": "TYPED_STREAM_FIXTURE_KEY",
                        "max_tokens": 256,
                        "timeout_s": 5,
                        "max_retries": 0,
                    },
                )
                cost_recorder = CostRecorder(conn)
                handle = cost_recorder.stream_events(
                    run.run.request_client,
                    messages=[{"role": "user", "content": run.context.user_request}],
                    system="Synthetic ice explanation.",
                    kind="decision",
                    turn_id=run.context.turn_id,
                    tools=None,
                )
                assembler = SemanticAssembler()
                emitted = asyncio.Event()
                completed = False

                async def consume() -> None:
                    nonlocal completed
                    async for event in handle.events():
                        if isinstance(event, LLMTextDelta):
                            for candidate in assembler.feed(event.text):
                                outcome = run.gate(candidate.text)
                                assert outcome.permit is not None
                                chunk = run.emit(outcome.permit, text=candidate.text)
                                assert chunk.source_event_id == outcome.event.event_uid
                                emitted.set()
                        elif isinstance(event, LLMResponseCompleted):
                            completed = True

                task = asyncio.create_task(consume())
                try:
                    await asyncio.wait_for(emitted.wait(), 2)
                    assert not completed
                    assert not peer.release.is_set()
                    assert not _costs(conn)
                    assert "tools" not in peer.body
                    run.terminalizer.cancel(
                        run.run.facts, reason="synthetic stop", cancel_scope="generation"
                    )
                    await handle.cancel("synthetic stop")
                    await task
                    await asyncio.wait_for(peer.peer_closed.wait(), 1)
                    assert not completed
                    assert len(_costs(conn)) == 1
                    assert _costs(conn)[0]["disposition"] == "cancelled"
                finally:
                    await handle.aclose()
                    await task

    asyncio.run(scenario())
