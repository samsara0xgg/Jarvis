"""A spoken turn's requests share one connection, and Allen's voice opens it first.

Real ``drive_turn``/``decide()``, the real OpenAI SDK and a localhost peer that
keeps connections alive the way api.openai.com does; the peer counts the TCP
connections it accepts. Before the resident provider loop every streamed
request opened a client, a connection and a TLS handshake of its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from typing import TYPE_CHECKING, Any, Self

# The product imports its SDK on first use, on the provider loop; cold, under load, that takes
# longer than the waits below, so it is done here at collection.
import httpx  # noqa: F401
import openai  # noqa: F401

from jarvis.decision import llm_io
from jarvis.decision.llm import LLMClient
from jarvis.decision.llm_stream import LLMTextDelta
from jarvis.shared.realtime_trace import realtime_trace_snapshot, reset_realtime_trace
from tests.support.hold import HANG_S, never_waits_for
from tests.support.routine import drive_streaming_turn
from tests.support.spoken import (
    fixture_key,  # noqa: F401 — autouse fixture: the key and the phase labels
    response_frames,
    spoken_intent,
    spoken_runtime,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from pathlib import Path


class _KeepAlivePeer:
    """Localhost /v1 peer that answers many requests per connection.

    POST /v1/responses streams ``outputs[N]``; GET /v1/models/<id> answers the
    model, which is what a connection warm-up asks for, once ``hold_models`` is set.
    """

    def __init__(
        self,
        outputs: list[list[tuple[str, str]]],
        *,
        hang: bool = False,
        hold_models: threading.Event | None = None,
    ) -> None:
        self.outputs = list(outputs)
        self.hang = hang  # stop mid-stream until the client goes away
        self.hold_models = hold_models  # a GET is not answered until the test sets this
        self.hung_up = threading.Event()
        self.connections = 0
        self.requests: list[tuple[str, str]] = []
        self.url = ""
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)

    def __enter__(self) -> Self:
        self._thread.start()
        assert self._ready.wait(5)
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.hold_models is not None:
            self.hold_models.set()
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

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            while True:
                try:
                    header = await reader.readuntil(b"\r\n\r\n")
                except (asyncio.IncompleteReadError, ConnectionError):
                    return
                lines = header.decode().split("\r\n")
                method, path, _ = lines[0].split(" ", 2)
                fields = {
                    name.lower(): value
                    for name, value in (line.split(": ", 1) for line in lines[1:] if ": " in line)
                }
                await reader.readexactly(int(fields.get("content-length", "0")))
                self.requests.append((method, path))
                if method == "GET":
                    if self.hold_models is not None:
                        await asyncio.get_running_loop().run_in_executor(
                            None, self.hold_models.wait
                        )
                    model = {"id": path.rsplit("/", 1)[-1], "object": "model", "created": 1,
                             "owned_by": "fixture"}
                    body, kind = json.dumps(model).encode(), "application/json"
                else:
                    body = "".join(
                        f"event: {frame['type']}\ndata: {json.dumps(frame)}\n\n"
                        for frame in response_frames(self.outputs.pop(0))
                    ).encode()
                    kind = "text/event-stream"
                writer.write(
                    f"HTTP/1.1 200 OK\r\nContent-Type: {kind}\r\n"
                    f"Content-Length: {len(body)}\r\n\r\n".encode()
                    + (body[: len(body) // 2] if self.hang else body),
                )
                await writer.drain()
                if self.hang:
                    await reader.read()  # EOF: the client closed the connection
                    self.hung_up.set()
                    return
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()


def _wait_for(predicate: Any, timeout: float = HANG_S) -> None:  # noqa: ANN401 — a zero-arg callable
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def _on_the_resident_loop[T](make: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """Run a coroutine on the provider loop the product keeps its connections on."""
    loop = llm_io._IO.loop()  # noqa: SLF001 — the one loop; no public way to ask it anything.
    return asyncio.run_coroutine_threadsafe(make(), loop).result(HANG_S + 1)


def _requests_in_flight() -> frozenset[asyncio.Task[Any]]:
    """The requests the resident loop is running now."""

    async def read() -> frozenset[asyncio.Task[Any]]:
        return frozenset(asyncio.all_tasks()) - {asyncio.current_task()}

    return _on_the_resident_loop(read)


def _settle_requests_since(before: frozenset[asyncio.Task[Any]]) -> None:
    """Return once every request begun since ``before`` has been answered and read to its end.

    A peer sees a request before its client has read the answer and put the connection back in
    its pool, and a request sent in that gap opens a second connection. The product never waits
    for a warm-up (it must not), so a test that wants the warmed connection waits for it here.
    """

    async def settle() -> None:
        begun = asyncio.all_tasks() - before - {asyncio.current_task()}
        if begun:
            _, unfinished = await asyncio.wait(begun, timeout=HANG_S)
            assert not unfinished, "a request never ended"

    _on_the_resident_loop(settle)


def test_a_tool_turns_requests_share_one_connection(tmp_path: Path) -> None:
    """The request after a tool call rides the first request's connection."""
    outputs = [[("commentary", "我看一下。"), ("call", "kept")], [("final_answer", "没有。")]]
    reset_realtime_trace()
    with _KeepAlivePeer(outputs) as peer:
        runtime = spoken_runtime(tmp_path, peer.url)
        result = drive_streaming_turn(
            runtime, spoken_intent(runtime.conn, "turn-reuse", "我记过什么")
        )
    assert result.response_plan.text == "没有。"
    assert peer.requests == [("POST", "/v1/responses")] * 2
    assert peer.connections == 1
    # The provider's trace records still carry the turn they belong to.
    sent = [
        point
        for point in realtime_trace_snapshot()
        if point.name == "llm_sdk_request_call_started_upper_bound"
    ]
    assert [point.attributes.get("turn_id") for point in sent] == ["turn-reuse"] * 2


def test_the_warm_up_opens_the_connection_the_turn_then_uses(tmp_path: Path) -> None:
    """Warming when Allen starts talking leaves one open connection for his request."""
    answer = threading.Event()  # the peer answers the warm-up once this is set
    with _KeepAlivePeer([[("final_answer", "在。")]], hold_models=answer) as peer:
        runtime = spoken_runtime(tmp_path, peer.url)
        before = _requests_in_flight()
        with never_waits_for(answer):  # warming returns while the network has not answered
            runtime.llm_client.warm_stream()
            runtime.llm_client.warm_stream()  # a second onset right after is not a second warm-up
        _wait_for(lambda: peer.requests)
        answer.set()
        _settle_requests_since(before)
        result = drive_streaming_turn(runtime, spoken_intent(runtime.conn, "turn-warm", "你在吗"))
    assert result.response_plan.text == "在。"
    assert peer.requests == [("GET", "/v1/models/fixture-model"), ("POST", "/v1/responses")]
    assert peer.connections == 1


def test_a_cancelled_stream_drops_its_request_on_the_resident_loop() -> None:
    """Cancelling mid-answer closes that request's connection; nothing keeps reading it."""
    with _KeepAlivePeer([[("final_answer", "一二三四五六七八九十" * 20)]], hang=True) as peer:
        client = LLMClient(
            {
                "provider": "openai",
                "model": "fixture-model",
                "base_url": peer.url,
                "api_key_env": "ROUTINE_STREAM_FIXTURE_KEY",
                "max_tokens": 64,
                "max_retries": 0,
            }
        )
        settled: list[str] = []
        handle = client.stream_events(
            messages=[{"role": "user", "content": "数数"}],
            system="",
            on_settled=lambda result: settled.append(result.outcome),
            responses=True,
        )

        async def _read_then_cancel() -> None:
            events = handle.events()
            while not isinstance(await anext(events), LLMTextDelta):
                pass
            await handle.cancel("barge_in")

        asyncio.run(_read_then_cancel())
        assert peer.hung_up.wait(HANG_S)
    assert settled == ["cancelled"]
