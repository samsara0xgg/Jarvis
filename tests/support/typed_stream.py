"""A localhost SSE peer and the typed-stream client built on it."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING, Any, Literal

from jarvis.decision.llm import LLMClient

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def openai_chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {
        "id": "provider-response",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "fixture-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def tool_call_delta(index: int, **function: str) -> dict[str, Any]:
    return {"tool_calls": [{"index": index, "function": function}]}


def stream_frames(provider: Literal["openai", "anthropic"]) -> list[dict[str, Any]]:
    if provider == "openai":
        return [
            openai_chunk({"content": "Hello. "}),
            openai_chunk(
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
            openai_chunk(
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
            openai_chunk(tool_call_delta(0, arguments='"ice"}')),
            openai_chunk(tool_call_delta(1, arguments='"UTC"}')),
            openai_chunk({}, "tool_calls"),
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


class SSEPeer:
    """A real localhost HTTP peer; observes socket EOF when Jarvis cancels."""

    def __init__(self, frames: list[dict[str, Any]], *, pause_after: int | None = None) -> None:
        self.frames = frames
        self.pause_after = pause_after
        self.release = asyncio.Event()
        self.received = asyncio.Event()
        self.peer_closed = asyncio.Event()
        self.body: dict[str, Any] = {}
        self.headers: dict[str, str] = {}
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
            self.headers = {name.lower(): value for name, value in fields.items()}
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


def stream_client(provider: str, base_url: str) -> LLMClient:
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
