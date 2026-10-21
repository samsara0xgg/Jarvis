"""A terminal UI rig on its own server."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Self

import httpx
import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse
from websockets.sync.client import connect as ws_connect

from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.surface.terminal_ui import Brain, Device, UiBroadcaster, create_ui_app
from tests.support.terminal import free_port, wait_for

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

DEVICE_TOKEN = "device-token-for-the-tests"  # noqa: S105 — a fixture, not a secret


LOCAL = "http://127.0.0.1"


class UvicornServer:
    """A real uvicorn server on its own thread and loop, reachable by its port."""

    def __init__(self, app: Any, port: int | None = None) -> None:  # noqa: ANN401 — an ASGI app.
        self.port = port or free_port()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.server = uvicorn.Server(
            uvicorn.Config(
                app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="off"
            ),
        )
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        async def serve() -> None:
            self.loop = asyncio.get_running_loop()
            await self.server.serve()

        asyncio.run(serve())

    def __enter__(self) -> Self:
        self.thread.start()
        wait_for(lambda: self.server.started, "server never started")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)

    def stop(self) -> None:
        """End the server; its sockets close, as a brain going away does."""
        self.server.should_exit = True
        self.thread.join(timeout=10)

    @property
    def url(self) -> str:
        return f"{LOCAL}:{self.port}"

    def call(self, coro: Any) -> Any:  # noqa: ANN401
        """Run a coroutine on this server's loop and wait for it."""
        assert self.loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=10)


@dataclass
class _FakeBrain:
    """The routes the tests need, and a record of every request (never of a body's meaning)."""

    seen: list[dict[str, Any]] = field(default_factory=list)
    gate: threading.Event = field(default_factory=threading.Event)
    sockets: list[asyncio.Queue[str | None]] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)
    server: UvicornServer | None = None

    def app(self) -> FastAPI:  # noqa: C901 — one closed list of routes.
        app = FastAPI()

        @app.middleware("http")
        async def record(request: Request, call_next: Any) -> Any:  # noqa: ANN401
            body = await request.body()
            self.seen.append(
                {
                    "method": request.method,
                    "path": request.url.path,
                    "query": request.url.query,
                    "headers": dict(request.headers),
                    "body": body,
                }
            )
            if request.headers.get("authorization") != f"Bearer {DEVICE_TOKEN}":
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
            return await call_next(request)

        @app.get("/inherent/stream")
        async def stream() -> StreamingResponse:
            async def lines() -> AsyncIterator[bytes]:
                yield b'{"level": 0.1}\n'
                await asyncio.to_thread(self.gate.wait, 10)
                yield b'{"text": "done"}\n'

            return StreamingResponse(lines(), media_type="application/x-ndjson")

        @app.get("/inherent/refuse")
        async def refuse() -> JSONResponse:
            return JSONResponse({"detail": "that is for the brain's own key"}, status_code=403)

        @app.get("/inherent/settings")
        async def settings_read() -> dict[str, Any]:
            return self._page()

        @app.post("/inherent/settings")
        async def settings_save(request: Request) -> dict[str, Any]:
            self.settings.update((await request.json())["changes"])
            return self._page()

        @app.post("/inherent/controls")
        async def controls(request: Request) -> dict[str, Any]:
            body = await request.json()
            return {
                "mic_muted": False,
                "speech_muted": False,
                "conversation": body.get("conversation") is True,
                "quiet": "off",
            }

        @app.websocket("/inherent/ws")
        async def ws(socket_: WebSocket) -> None:
            if socket_.headers.get("authorization") != f"Bearer {DEVICE_TOKEN}":
                await socket_.close(code=1008)
                return
            await socket_.accept()
            queue: asyncio.Queue[str | None] = asyncio.Queue()
            self.sockets.append(queue)

            async def listen() -> None:  # a push socket reads, as the daemon's does
                with contextlib.suppress(WebSocketDisconnect):
                    while True:
                        await socket_.receive_text()

            listening = asyncio.create_task(listen())
            try:
                while not listening.done():
                    taking = asyncio.create_task(queue.get())
                    await asyncio.wait({taking, listening}, return_when=asyncio.FIRST_COMPLETED)
                    if not taking.done():
                        taking.cancel()
                        break
                    if (text := taking.result()) is None:
                        await socket_.close(code=1001)
                        break
                    await socket_.send_text(text)
            finally:
                listening.cancel()
                self.sockets.remove(queue)

        @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
        async def echo(request: Request) -> dict[str, Any]:
            body = await request.body()
            return {
                "method": request.method,
                "path": request.url.path,
                "query": request.url.query,
                "length": len(body),
                "sha": hashlib.sha256(body).hexdigest(),
            }

        return app

    def _page(self) -> dict[str, Any]:
        return {
            "values": {
                "tts_voice": "Warm Bestie",
                "input_device": "System default",
                "output_device": "System default",
                **self.settings,
            },
            "options": {"input_device": ["System default"], "output_device": ["System default"]},
            "defaults": {"input_device": None, "output_device": None},
            "restart_pending": False,
        }

    def push(self, op: str, **payload: object) -> None:
        """Send one op on every push socket the brain holds."""
        assert self.server is not None
        text = json.dumps({"op": op, "payload": payload})
        for queue in list(self.sockets):
            self.server.call(queue.put(text))

    def drop_sockets(self) -> None:
        """The brain closes its push sockets."""
        assert self.server is not None
        for queue in list(self.sockets):
            self.server.call(queue.put(None))

    def requests(self, path: str) -> list[dict[str, Any]]:
        return [one for one in self.seen if one["path"] == path]


@dataclass
class _Rig:
    """A fake brain, the terminal's UI app in front of it, and the local key a daemon would use."""

    root: Path
    brain: _FakeBrain
    brain_server: UvicornServer
    ui: UvicornServer
    broadcaster: UiBroadcaster
    key: str
    device: Device

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key}"}

    def get(self, path: str, **kwargs: Any) -> httpx.Response:  # noqa: ANN401
        kwargs.setdefault("headers", self.headers)
        return httpx.get(f"{self.ui.url}{path}", **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:  # noqa: ANN401
        kwargs.setdefault("headers", self.headers)
        return httpx.post(f"{self.ui.url}{path}", **kwargs)

    def socket(self, **kwargs: Any) -> Any:  # noqa: ANN401
        kwargs.setdefault("additional_headers", self.headers)
        return ws_connect(f"ws://127.0.0.1:{self.ui.port}/inherent/ws", **kwargs)


@contextlib.contextmanager
def terminal_rig(
    tmp_path: Path,
    device: Device | None = None,
    *,
    brain_up: bool = True,
) -> Any:  # noqa: ANN401 — a generator of _Rig.
    fake = _FakeBrain()
    brain_server = UvicornServer(fake.app())
    fake.server = brain_server
    key = local_key(tmp_path)
    broadcaster = UiBroadcaster()
    chosen = device or Device()
    if brain_up:
        brain_server.__enter__()
    try:
        app = create_ui_app(
            Brain(brain_server.url, DEVICE_TOKEN),
            authorize=functools.partial(local_key_matches, key),
            broadcaster=broadcaster,
            device=chosen,
        )
        with UvicornServer(app) as ui:
            yield _Rig(tmp_path, fake, brain_server, ui, broadcaster, key, chosen)
    finally:
        if brain_up:
            brain_server.__exit__()
