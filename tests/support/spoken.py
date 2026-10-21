"""A spoken turn through drive_turn against a localhost /v1/responses peer."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import threading
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Self

import pytest

from jarvis.runtime.turn import start as start_module
from jarvis.state.event_log import emit_event
from tests.support.routine import routine_runtime

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from jarvis.runtime import JarvisRuntime
    from jarvis.shared import Event


def response_frames(items: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """One /v1/responses stream: ``(phase, text)`` messages and ``("call", name)`` calls."""
    response = {
        "id": "provider-response",
        "object": "response",
        "created_at": 1,
        "model": "fixture-model",
        "output": [],
        "tools": [],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }
    frames: list[dict[str, Any]] = [
        {"type": "response.created", "response": {**response, "status": "in_progress"}},
    ]
    for index, (kind, text) in enumerate(items):
        if kind == "call":
            call = {
                "type": "function_call",
                "id": f"fc-{index}",
                "call_id": f"call-{index}",
                "name": text,
            }
            frames += [
                {
                    "type": "response.output_item.added",
                    "output_index": index,
                    "item": {**call, "arguments": "", "status": "in_progress"},
                },
                {
                    "type": "response.output_item.done",
                    "output_index": index,
                    "item": {**call, "arguments": "{}", "status": "completed"},
                },
            ]
            continue
        item = {"type": "message", "id": f"{kind}-{index}", "role": "assistant", "phase": kind}
        frames.append(
            {
                "type": "response.output_item.added",
                "output_index": index,
                "item": {**item, "status": "in_progress", "content": []},
            }
        )
        frames += [
            {
                "type": "response.output_text.delta",
                "output_index": index,
                "item_id": item["id"],
                "content_index": 0,
                "delta": piece,
                "logprobs": [],
            }
            for piece in re.findall(r".{1,6}", text)
        ]
        frames.append(
            {
                "type": "response.output_item.done",
                "output_index": index,
                "item": {
                    **item,
                    "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}],
                },
            }
        )
    usage = {
        "input_tokens": 12,
        "output_tokens": 6,
        "total_tokens": 18,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens_details": {"reasoning_tokens": 0},
    }
    frames.append(
        {
            "type": "response.completed",
            "response": {**response, "status": "completed", "usage": usage},
        }
    )
    return [{**frame, "sequence_number": number} for number, frame in enumerate(frames)]


class SpokenPeer:
    """Localhost /v1/responses peer: request N streams ``outputs[N]``."""

    def __init__(
        self,
        outputs: list[list[tuple[str, str]]],
        *,
        hold: bool = False,
        hold_at: str = "。.",
        first_event_delay_s: float = 0.0,
    ) -> None:
        self.outputs = list(outputs)
        self.first_event_delay_s = first_event_delay_s
        self.hold = hold
        self.hold_at = hold_at
        self.holding = threading.Event()
        self.release = threading.Event()
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.requested = threading.Event()  # set once the first request is in
        self.completed = 0
        self.url = ""
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._handlers: set[asyncio.Task[None]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._released = asyncio.Event()
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)

    def __enter__(self) -> Self:
        self._thread.start()
        assert self._ready.wait(5)
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release.set()
        self._stop.set()
        self._thread.join(5)

    def release_delayed(self) -> None:
        """Let the requests now waiting out ``first_event_delay_s`` go on at once."""
        assert self._loop is not None
        self._loop.call_soon_threadsafe(self._pulse)

    def _pulse(self) -> None:
        self._released.set()
        self._released.clear()

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1"
        self._ready.set()
        try:
            await asyncio.get_running_loop().run_in_executor(None, self._stop.wait)
        finally:
            server.close()
            # A request still waiting out its delay would hold wait_closed() until it ended.
            for handler in self._handlers:
                handler.cancel()
            await asyncio.gather(*self._handlers, return_exceptions=True)
            await server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        handler = asyncio.current_task()
        assert handler is not None
        self._handlers.add(handler)
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            lines = header.decode().split("\r\n")
            fields = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
            body = json.loads(await reader.readexactly(int(fields["Content-Length"])))
            self.requests.append((lines[0].split()[1], body))
            self.requested.set()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n"
            )
            answer = ""
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._released.wait(), self.first_event_delay_s)
            for frame in response_frames(self.outputs.pop(0)):
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
            self._handlers.discard(handler)
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()


@pytest.fixture(autouse=True)
def fixture_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROUTINE_STREAM_FIXTURE_KEY", "synthetic")
    # The peer is localhost; the route opens only where messages carry labels.
    monkeypatch.setattr(start_module, "labels_phases", lambda _base_url: True)


def spoken_runtime(
    tmp_path: Path, url: str, *, answer_hold_s: float | None = None
) -> JarvisRuntime:
    """A runtime for a spoken turn on the peer at ``url``; see ``routine_runtime`` for the hold."""
    runtime = routine_runtime(tmp_path, url, routine=False, answer_hold_s=answer_hold_s)
    return replace(runtime, response_flags=replace(runtime.response_flags, spoken_streaming=True))


def spoken_intent(conn: sqlite3.Connection, turn_id: str, transcript: str) -> Event:
    return emit_event(
        conn,
        type="surface.user_intent",
        payload={"transcript": transcript, "turn_id": turn_id, "channel": "inherent_ptt"},
        correlation={"turn_id": turn_id},
    )
