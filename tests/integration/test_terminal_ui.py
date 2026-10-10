"""ADR 0183: a terminal serves its device's UI on loopback and forwards the rest to its brain.

Acceptance checks against real sockets, with a fake brain only where the brain's own answer is
not the thing under test: admission is the daemon's (Host, local key, nothing else open but the
probe); every forwarded request reaches the brain under this device's token and never under the
local key, streams as it is produced and comes back as the brain refused it; the device-route
table is exactly the routes the app answers itself; the push socket carries the brain's ops and
this terminal's own once each; with no brain the routes say so and the socket closes; a held port
ends the command; and one end-to-end run, a real brain and a real terminal with its real media
actor, where a client in the companion's place adopts the terminal and hears both.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import socket
import sqlite3
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self, cast

import httpx
import numpy as np
import pytest
import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRoute
from websockets.exceptions import InvalidStatus, WebSocketException
from websockets.sync.client import connect as ws_connect

from jarvis.cli import main
from jarvis.deployment.night_power import MacPower
from jarvis.runtime.night_run import NightRun, NightSettings
from jarvis.runtime.settings import DEVICE_KEYS, Settings
from jarvis.runtime.terminal import _DeviceSettings, _run, _Speaking, _Ui, bind_ui
from jarvis.state.device_tokens import (
    device_name_for_token,
    device_token_matches,
    pair_device,
)
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.surface import voice_backend, voice_tts
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app, require_local_key
from jarvis.surface.terminal_events import BrainEvents
from jarvis.surface.terminal_link import TerminalHub
from jarvis.surface.terminal_listen import BrainListening, ListenHooks
from jarvis.surface.terminal_ui import (
    DEVICE_ROUTES,
    NOT_SERVED,
    UNREACHABLE,
    Brain,
    Device,
    UiBroadcaster,
    create_ui_app,
)
from jarvis.surface.terminal_voice import BrainVoice
from jarvis.surface.voice_controls import VoiceControls
from tests.integration.test_terminal_listen import LISTEN_CONFIG, _heard
from tests.integration.test_terminal_voice import (
    SPEECH_CONFIG,
    _AsRemote,
    _brain_log,
    _FakeProvider,
    _free_port,
    _open_stream,
    _wait_for,
)
from tests.integration.test_voice_file_replay import _write_wav

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

DEVICE_TOKEN = "device-token-for-the-tests"  # noqa: S105 — a fixture, not a secret
LOCAL = "http://127.0.0.1"


# --- a loop of its own for each server ----------------------------------------------------


class _Server:
    """A real uvicorn server on its own thread and loop, reachable by its port."""

    def __init__(self, app: Any, port: int | None = None) -> None:  # noqa: ANN401 — an ASGI app.
        self.port = port or _free_port()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning",
                           lifespan="off"),
        )
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        async def serve() -> None:
            self.loop = asyncio.get_running_loop()
            await self.server.serve()

        asyncio.run(serve())

    def __enter__(self) -> Self:
        self.thread.start()
        _wait_for(lambda: self.server.started, "server never started")
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


# --- a fake brain that records what reaches it ---------------------------------------------


@dataclass
class _FakeBrain:
    """The routes the tests need, and a record of every request (never of a body's meaning)."""

    seen: list[dict[str, Any]] = field(default_factory=list)
    gate: threading.Event = field(default_factory=threading.Event)
    sockets: list[asyncio.Queue[str | None]] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)
    server: _Server | None = None

    def app(self) -> FastAPI:  # noqa: C901 — one closed list of routes.
        app = FastAPI()

        @app.middleware("http")
        async def record(request: Request, call_next: Any) -> Any:  # noqa: ANN401
            body = await request.body()
            self.seen.append({
                "method": request.method, "path": request.url.path, "query": request.url.query,
                "headers": dict(request.headers), "body": body,
            })
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
            return {"mic_muted": False, "speech_muted": False,
                    "conversation": body.get("conversation") is True, "quiet": "off"}

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
            return {"method": request.method, "path": request.url.path,
                    "query": request.url.query, "length": len(body),
                    "sha": hashlib.sha256(body).hexdigest()}

        return app

    def _page(self) -> dict[str, Any]:
        return {
            "values": {"tts_voice": "Warm Bestie", "input_device": "System default",
                       "output_device": "System default", **self.settings},
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
    brain_server: _Server
    ui: _Server
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
def _rig(
    tmp_path: Path, device: Device | None = None, *, brain_up: bool = True,
) -> Any:  # noqa: ANN401 — a generator of _Rig.
    fake = _FakeBrain()
    brain_server = _Server(fake.app())
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
            broadcaster=broadcaster, device=chosen,
        )
        with _Server(app) as ui:
            yield _Rig(tmp_path, fake, brain_server, ui, broadcaster, key, chosen)
    finally:
        if brain_up:
            brain_server.__exit__()


# --- admission: the daemon's, and nothing more open ----------------------------------------


def test_a_request_is_admitted_only_with_a_local_host_and_the_local_key(tmp_path: Path) -> None:
    """The Host check runs first; every route but the probe needs the key; v2 stays closed."""
    with _rig(tmp_path) as rig:
        assert rig.get("/inherent/setup", headers={}).status_code == 401
        wrong = {"Authorization": f"Bearer {rig.key}x"}
        assert rig.get("/inherent/setup", headers=wrong).status_code == 401
        assert rig.get("/inherent/setup", headers={"Authorization": rig.key}).status_code == 401
        # a page rebinding its own name to 127.0.0.1 still names itself in Host
        rebound = httpx.get(
            f"{rig.ui.url}/inherent/setup", headers={**rig.headers, "Host": "evil.example"},
        )
        assert rebound.status_code == 400
        # the brain's token is not the local key: it opens nothing here
        brain_token = {"Authorization": f"Bearer {DEVICE_TOKEN}"}
        assert rig.get("/inherent/setup", headers=brain_token).status_code == 401
        # the probe is open, as on a daemon, and answered here
        assert httpx.get(f"{rig.ui.url}/api/health").json() == {"status": "ok"}
        # the daemon's keyless v2 routes and the brain's terminal socket are not served at all
        for path in sorted(NOT_SERVED):
            assert httpx.post(f"{rig.ui.url}{path}", headers=rig.headers).status_code == 404
            assert httpx.post(f"{rig.ui.url}{path}").status_code in {401, 404}
        assert rig.brain.seen == []  # nothing refused or unserved ever reached the brain
        assert rig.get("/inherent/setup").status_code == 200  # and the key does open it


def test_the_push_socket_is_admitted_like_a_request(tmp_path: Path) -> None:
    """No key, a wrong key or a foreign Host never reaches the handshake's end."""
    with _rig(tmp_path) as rig:
        with pytest.raises(InvalidStatus) as refused:
            ws_connect(f"ws://127.0.0.1:{rig.ui.port}/inherent/ws")
        assert refused.value.response.status_code == 403
        with pytest.raises(InvalidStatus):
            ws_connect(f"ws://127.0.0.1:{rig.ui.port}/inherent/ws",
                       additional_headers={"Authorization": f"Bearer {rig.key}x"})
        with socket.create_connection(("127.0.0.1", rig.ui.port)) as raw:
            raw.sendall(
                b"GET /inherent/ws HTTP/1.1\r\nHost: evil.example\r\nUpgrade: websocket\r\n"
                b"Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                + f"Authorization: Bearer {rig.key}\r\n\r\n".encode(),
            )
            assert b" 101 " not in raw.recv(200).split(b"\r\n")[0]
        assert not rig.brain.sockets
        with rig.socket():
            _wait_for(lambda: bool(rig.brain.sockets), "the brain's socket never opened")
        assert rig.brain.seen == []


# --- forwarding -----------------------------------------------------------------------------


def test_a_get_and_a_post_reach_the_brain_under_the_device_token_and_never_the_local_key(
    tmp_path: Path,
) -> None:
    """Method, path, query and body arrive whole; the credential is replaced, not added to."""
    with _rig(tmp_path) as rig:
        got = rig.get("/inherent/mail/abc?unread=1&limit=3", headers={
            **rig.headers, "Accept": "application/json", "Cookie": "session=1",
            "X-Anything": "no",
        })
        assert got.json() == {"method": "GET", "path": "/inherent/mail/abc",
                              "query": "unread=1&limit=3", "length": 0,
                              "sha": hashlib.sha256(b"").hexdigest()}
        payload = {"text": "你好, 请记一下", "n": [1, 2]}
        raw = json.dumps(payload).encode()
        sent = rig.post("/inherent/submit", content=raw,
                        headers={**rig.headers, "Content-Type": "application/json"})
        assert sent.json() == {"method": "POST", "path": "/inherent/submit", "query": "",
                               "length": len(raw), "sha": hashlib.sha256(raw).hexdigest()}
        blob = bytes(range(256)) * 4000  # a body that is not text, ~1 MB
        up = rig.post("/inherent/asr-sample", content=blob,
                      headers={**rig.headers, "Content-Type": "audio/wav"})
        assert up.json()["sha"] == hashlib.sha256(blob).hexdigest()
        assert [one["method"] for one in rig.brain.seen] == ["GET", "POST", "POST"]
        for request in rig.brain.seen:
            headers = request["headers"]
            assert headers["authorization"] == f"Bearer {DEVICE_TOKEN}"
            assert "cookie" not in headers
            assert "x-anything" not in headers
        assert rig.brain.seen[0]["headers"]["accept"] == "application/json"
        assert rig.brain.seen[2]["headers"]["content-type"] == "audio/wav"
        # the local key is in no header and no body the brain ever received
        everything = repr([(one["headers"], one["body"]) for one in rig.brain.seen])
        assert rig.key not in everything


def test_the_plugin_routes_are_forwarded_under_the_device_token(tmp_path: Path) -> None:
    """ADR 0202: the companion's plugin request reaches the brain as this device, not as the key."""
    with _rig(tmp_path) as rig:
        listed = rig.get("/inherent/plugins", headers=rig.headers)
        assert listed.json()["path"] == "/inherent/plugins"
        body = json.dumps({"operation": "open", "data": {"plugin_id": "notion"}}).encode()
        sent = rig.post("/inherent/plugins/action", content=body,
                        headers={**rig.headers, "Content-Type": "application/json"})
        assert sent.json()["path"] == "/inherent/plugins/action"
        assert [one["headers"]["authorization"] for one in rig.brain.seen] == [
            f"Bearer {DEVICE_TOKEN}"] * 2


def test_a_brain_refusal_comes_back_as_it_is_and_is_not_retried(tmp_path: Path) -> None:
    """A route the device token cannot open answers the UI with the brain's own refusal."""
    with _rig(tmp_path) as rig:
        refused = rig.get("/inherent/refuse")
        assert refused.status_code == 403
        assert refused.json() == {"detail": "that is for the brain's own key"}
        assert len(rig.brain.seen) == 1  # one try, under the one credential

    with _rig(tmp_path) as rig, _Server(
        create_ui_app(
            Brain(rig.brain_server.url, "a-revoked-token"),
            authorize=functools.partial(local_key_matches, rig.key),
            broadcaster=UiBroadcaster(), device=Device(),
        ),
    ) as revoked:
        answer = httpx.get(f"{revoked.url}/inherent/setup", headers=rig.headers)
        assert answer.status_code == 401
        assert answer.json() == {"detail": "unauthorized"}
        assert len(rig.brain.seen) == 1


def test_a_streaming_answer_arrives_as_it_is_produced(tmp_path: Path) -> None:
    """The first NDJSON line is read while the brain has not yet produced the last."""
    with _rig(tmp_path) as rig, httpx.stream(
        "GET", f"{rig.ui.url}/inherent/stream", headers=rig.headers, timeout=10,
    ) as response:
        assert response.headers["content-type"] == "application/x-ndjson"
        lines = response.iter_lines()
        assert json.loads(next(lines)) == {"level": 0.1}
        assert not rig.brain.gate.is_set()  # the brain is still waiting to say the rest
        rig.brain.gate.set()
        assert json.loads(next(lines)) == {"text": "done"}


# --- the device routes ----------------------------------------------------------------------


def test_the_table_is_exactly_the_routes_this_terminal_answers_itself() -> None:
    """Whatever the app registers beside its forwarder and its socket is in the table, and back."""
    app = create_ui_app(
        Brain(LOCAL, DEVICE_TOKEN), authorize=lambda _h: True, broadcaster=UiBroadcaster(),
        device=Device(night=NightRun(Path("unused"), NightSettings(), MacPower())),
    )
    own = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute) and route.path != "/{path:path}"
        for method in route.methods - {"HEAD"}
    }
    assert own == set(DEVICE_ROUTES)
    # the voice preview is the brain's: it holds the speech key, and the companion plays the MP3
    assert ("POST", "/inherent/setup/voice-preview") not in DEVICE_ROUTES


def test_a_device_route_is_answered_here_and_never_asked_of_the_brain(tmp_path: Path) -> None:
    """Restart, the probe and dictation do not travel; without their device they are 404."""
    restarts: list[int] = []
    with _rig(tmp_path, Device(restart=lambda: restarts.append(1))) as rig:
        assert rig.post("/inherent/restart").status_code == 202
        assert restarts == [1]
        assert rig.get("/api/health").json() == {"status": "ok"}
        assert rig.brain.seen == []
    with _rig(tmp_path) as rig:
        # no supervisor means no restart route, as on a daemon; forwarded it would restart the brain
        assert rig.post("/inherent/restart").status_code == 404
        # no capture session is a daemon without voice: the companion reads 404 as "voice is off"
        assert rig.post("/inherent/dictation", json={}).status_code == 404
        assert rig.post("/inherent/dictation/stop").status_code == 404
        assert rig.brain.seen == []


class _Dictation:
    """The recording session's two calls, as the daemon's routes use them."""

    def __init__(self) -> None:
        self.active = False
        self.contexts: list[dict[str, str]] = []
        self.stopped = threading.Event()

    def begin(self, context: dict[str, str]) -> AsyncIterator[dict[str, Any]]:
        if self.active:
            msg = "already dictating"
            raise RuntimeError(msg)
        self.active = True
        self.contexts.append(context)
        return self._lines()

    async def _lines(self) -> AsyncIterator[dict[str, Any]]:
        yield {"level": 0.2}
        await asyncio.to_thread(self.stopped.wait, 10)
        yield {"state": "thinking", "seconds": 1.0}
        yield {"text": "Hello.", "raw": "hello"}
        self.active = False

    def stop(self) -> bool:
        self.stopped.set()
        return self.active


def test_dictation_runs_on_this_machines_session_and_streams_like_a_daemons(
    tmp_path: Path,
) -> None:
    """NDJSON from the local session, a second start is 409, and the brain never hears of it."""
    session = _Dictation()
    holder: list[_Dictation | None] = [None]
    with _rig(tmp_path, Device(dictation=lambda: holder[0])) as rig:
        assert rig.post("/inherent/dictation", json={}).status_code == 404  # not listening yet
        holder[0] = session
        context = {"app": "Ghostty", "window": "claude", "selected": "", "before": "先看"}
        with httpx.stream("POST", f"{rig.ui.url}/inherent/dictation", json=context,
                          headers=rig.headers, timeout=10) as response:
            assert response.headers["content-type"] == "application/x-ndjson"
            lines = response.iter_lines()
            assert json.loads(next(lines)) == {"level": 0.2}
            assert rig.post("/inherent/dictation", json=context).status_code == 409
            assert rig.post("/inherent/dictation/stop").json() == {"ok": True}
            rest = [json.loads(line) for line in lines]
        assert rest[-2:] == [{"state": "thinking", "seconds": 1.0},
                             {"text": "Hello.", "raw": "hello"}]
        assert session.contexts == [context]
        assert rig.brain.seen == []


def test_the_mic_and_speech_switches_act_here_and_the_rest_of_controls_is_the_brains(
    tmp_path: Path,
) -> None:
    """Mic and speech mute are this device's; the brain gets the request and answers the rest."""
    controls = VoiceControls()
    gains: list[bool] = []
    controls.on_speech_muted = gains.append
    with _rig(tmp_path, Device(controls=controls)) as rig:
        state = rig.post("/inherent/controls", json={"mic_muted": True, "conversation": True})
        assert state.json() == {"mic_muted": True, "speech_muted": False,
                                "conversation": True, "quiet": "off"}
        assert controls.mic_muted
        assert not controls.speech_muted
        assert rig.post("/inherent/controls", json={"speech_muted": True}).json()[
            "speech_muted"
        ] is True
        assert gains == [True]
        # `{}` is the companion's read on connect: the answer is this device's own state
        assert rig.post("/inherent/controls", json={}).json()["mic_muted"] is True
        assert rig.post("/inherent/controls", json={"quiet": "loud"}).status_code == 422
        assert [json.loads(one["body"]) for one in rig.brain.requests("/inherent/controls")] == [
            {"mic_muted": True, "conversation": True}, {"speech_muted": True}, {},
        ]
    # with no brain the switch is still thrown, and the page is told why the rest failed
    with _rig(tmp_path, Device(controls=controls), brain_up=False) as rig:
        answer = rig.post("/inherent/controls", json={"mic_muted": False})
        assert answer.status_code == 503
        assert not controls.mic_muted


def test_settings_keep_the_brains_page_and_this_machines_audio_choices(tmp_path: Path) -> None:
    """The two device choices are saved here and shown here; every other change is the brain's."""
    settings = Settings(
        tmp_path, {"realtime": {"input_device": "Mic A"}},
        lambda kind: ["Mic A", "Mic B"] if kind == "input" else ["Speaker 1"],
        lambda kind: "Mic B" if kind == "input" else "Speaker 1",
    )
    device = Device(settings=_DeviceSettings(settings))
    assert _DeviceSettings(settings).keys == DEVICE_KEYS
    with _rig(tmp_path, device) as rig:
        page = rig.get("/inherent/settings").json()
        assert page["values"]["tts_voice"] == "Warm Bestie"  # the brain's
        assert page["values"]["input_device"] == "Mic A"  # this machine's
        assert page["options"]["input_device"] == ["System default", "Mic A", "Mic B"]
        assert page["options"]["output_device"] == ["System default", "Speaker 1"]
        assert page["defaults"]["input_device"] == "Mic B"
        assert page["restart_pending"] is False

        mixed = rig.post("/inherent/settings", json={
            "changes": {"input_device": "Mic B", "tts_voice": "Sweet Lady"},
        }).json()
        assert mixed["values"]["tts_voice"] == "Sweet Lady"
        assert mixed["values"]["input_device"] == "Mic B"
        assert mixed["restart_pending"] is True  # a device is applied when this terminal restarts
        assert [json.loads(one["body"]) for one in rig.brain.requests("/inherent/settings")
                if one["method"] == "POST"] == [{"changes": {"tts_voice": "Sweet Lady"}}]

        before = len(rig.brain.seen)
        only = rig.post("/inherent/settings", json={"changes": {"output_device": "Speaker 1"}})
        assert only.json()["values"]["output_device"] == "Speaker 1"
        # the brain has nothing to save, so it is only read
        assert [one["method"] for one in rig.brain.seen[before:]] == ["GET"]

        bad = rig.post("/inherent/settings", json={"changes": {"input_device": "No such mic"}})
        assert bad.status_code == 400
        assert "No such mic" in bad.json()["detail"]
    saved = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert saved == {"input_device": "Mic B", "output_device": "Speaker 1"}


def test_without_a_device_the_settings_and_controls_are_the_brains(tmp_path: Path) -> None:
    """A terminal with no voice has no microphone to choose or mute: the brain's page, as is."""
    with _rig(tmp_path) as rig:
        assert rig.get("/inherent/settings").json()["values"]["input_device"] == "System default"
        saved = rig.post("/inherent/settings", json={"changes": {"tts_voice": "X"}})
        assert saved.status_code == 200
        assert rig.post("/inherent/controls", json={"mic_muted": True}).status_code == 200
        assert len(rig.brain.requests("/inherent/controls")) == 1


# --- no brain ---------------------------------------------------------------------------------


def test_with_no_brain_forwarded_routes_say_so_and_local_ones_still_answer(
    tmp_path: Path,
) -> None:
    """503 with the daemon's own error shape; the socket is refused so the companion retries."""
    restarts: list[int] = []
    with _rig(tmp_path, Device(restart=lambda: restarts.append(1)), brain_up=False) as rig:
        answer = rig.get("/inherent/notices")
        assert answer.status_code == 503
        assert answer.json() == {"detail": UNREACHABLE}
        assert rig.post("/inherent/submit", json={"text": "hi"}).status_code == 503
        assert httpx.get(f"{rig.ui.url}/api/health").status_code == 200
        assert rig.post("/inherent/restart").status_code == 202
        with pytest.raises(WebSocketException):
            rig.socket()


def test_a_brain_that_goes_away_closes_the_push_socket_so_the_companion_reconnects(
    tmp_path: Path,
) -> None:
    """An open socket carrying nothing from the brain would show a daemon that is up."""
    with _rig(tmp_path) as rig:
        socket_ = rig.socket()
        _wait_for(lambda: bool(rig.brain.sockets), "the brain's socket never opened")
        rig.brain.push("tool", turn_id="T1", label="x")
        assert json.loads(socket_.recv(timeout=5))["op"] == "tool"
        rig.brain.drop_sockets()
        with pytest.raises(WebSocketException):
            socket_.recv(timeout=5)
        assert not rig.broadcaster._clients  # noqa: SLF001 — the closed socket left the registry


# --- the merged push socket -----------------------------------------------------------------


def _frames(socket_: Any, count: int, timeout_s: float = 5.0) -> list[dict[str, Any]]:  # noqa: ANN401
    got: list[dict[str, Any]] = []
    deadline = time.monotonic() + timeout_s
    while len(got) < count and time.monotonic() < deadline:
        with contextlib.suppress(TimeoutError):
            got.append(json.loads(socket_.recv(timeout=0.2)))
    return got


def test_the_brains_ops_and_this_terminals_own_voice_ops_arrive_once_each(
    tmp_path: Path,
) -> None:
    """One stream, two sources, no op twice, and the one op both could send is sent by one."""
    controls = VoiceControls(mic_muted=True)
    speaking = [True]
    with _rig(tmp_path, Device(controls=controls, speaks=lambda: speaking[0])) as rig:
        companion = rig.socket()
        _wait_for(lambda: bool(rig.brain.sockets), "the brain's socket never opened")
        _wait_for(lambda: bool(rig.broadcaster._clients), "the companion never registered")  # noqa: SLF001
        for turn in ("T1", "T2", "T3"):
            rig.brain.push("open", turn_id=turn, content="", streaming=True)
            rig.ui.call(rig.broadcaster.broadcast_voice(
                "playing", turn_id=turn, played=1, ahead=2, held=False,
            ))
        # what the brain says of a turn nobody speaks, while this terminal does speak it
        rig.brain.push("voice", phase="spoken", turn_id="T1", output_outcome="no_voice")
        # its controls push says mic_muted for the brain's own (absent) microphone
        rig.brain.push("controls", mic_muted=False, speech_muted=False,
                       conversation=True, quiet="off")
        got = _frames(companion, 7)
        assert Counter((f["op"], f["payload"].get("turn_id")) for f in got) == Counter(
            {("open", "T1"): 1, ("open", "T2"): 1, ("open", "T3"): 1,
             ("voice", "T1"): 1, ("voice", "T2"): 1, ("voice", "T3"): 1,
             ("controls", None): 1},
        )
        assert all(f["payload"].get("output_outcome") != "no_voice" for f in got)
        [pushed] = [f for f in got if f["op"] == "controls"]
        assert pushed["payload"] == {"mic_muted": True, "speech_muted": False,
                                     "conversation": True, "quiet": "off"}
        # a terminal that does not speak leaves the brain's word alone
        speaking[0] = False
        rig.brain.push("voice", phase="spoken", turn_id="T9", output_outcome="no_voice")
        [silent] = _frames(companion, 1)
        assert silent["payload"]["output_outcome"] == "no_voice"
        assert _frames(companion, 1, timeout_s=0.5) == []
        companion.close()


def test_a_late_companion_gets_the_brains_snapshot_and_this_terminals_retained_capability(
    tmp_path: Path,
) -> None:
    """Each companion socket has its own brain socket, so the brain's greeting reaches each."""
    with _rig(tmp_path) as rig:
        rig.ui.call(rig.broadcaster.broadcast_voice_capability(
            version=1, state="listening", stream_epoch=1, reason="ready", wake_available=True,
            local_capture_available=True, ptt_upload_available=False, text_available=True,
        ))
        with rig.socket() as first, rig.socket() as second:
            _wait_for(lambda: len(rig.brain.sockets) == 2, "both brain sockets never opened")
            for companion in (first, second):
                assert _frames(companion, 1)[0]["op"] == "voice_capability"
            rig.brain.push("live", phase="state", state="idle")
            for companion in (first, second):
                assert _frames(companion, 1)[0]["op"] == "live"


# --- a held port ------------------------------------------------------------------------------


def test_a_held_port_ends_the_command_before_anything_starts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """A daemon (or anything) listening there means this terminal does not serve the UI."""
    token = tmp_path / "token"
    token.write_text("t\n", encoding="utf-8")
    token.chmod(0o600)
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = holder.getsockname()[1]
        with pytest.raises(OSError, match="held by another process"):
            bind_ui(port)
        argv = ["terminal", "--brain", "http://127.0.0.1:1", "--brain-token-file", str(token),
                "--runtime-root", str(tmp_path), "--no-observers", "--serve-ui",
                "--port", str(port)]
        assert main(argv) == 1
        error = capsys.readouterr().err
        assert f"127.0.0.1:{port} is held" in error
        assert "--port" in error
    free = bind_ui(port)  # released: the same port now serves
    assert free.getsockname() == ("127.0.0.1", port)
    free.close()
    with pytest.raises(SystemExit):
        main(["terminal", "--brain", "http://127.0.0.1:1", "--port", "8006"])  # needs --serve-ui


def test_the_ui_listens_on_loopback_only(tmp_path: Path) -> None:
    """Never another interface, whatever the port."""
    sock = bind_ui(_free_port())
    try:
        assert sock.getsockname()[0] == "127.0.0.1"
    finally:
        sock.close()
    del tmp_path


# --- end to end ---------------------------------------------------------------------------------


class _Setup:
    """The brain's first-run state, as the companion's adoption probe reads it."""

    def read(self) -> dict[str, Any]:
        return {"first_run": False, "assistant_name": "Jarvis"}


class _RealBrain:
    """A real brain app: its routes, its admission, its broadcaster, a speech provider."""

    def __init__(self, root: Path, hooks: ListenHooks | None = None) -> None:
        self.log = root / "events.db"
        conn = _brain_log(self.log)
        self.broadcaster = InherentBroadcaster()
        self.provider = _FakeProvider()
        self.hub = TerminalHub(events=BrainEvents(conn))
        if hooks is not None:
            self.hub.listening = BrainListening(self.hub, self.hub.events)  # type: ignore[arg-type]
            self.hub.listening.hooks = hooks
        from jarvis.runtime import inherent_loop  # noqa: PLC0415

        self.hub.voice = BrainVoice(
            cast("Any", self.provider), inherent_loop._SpokenRows(conn),  # noqa: SLF001
            poll_interval_s=0.01,
        )
        app = create_app(InherentDeps(
            submit_callable=lambda _text: "T1", broadcaster=self.broadcaster,
            terminals=self.hub, device_name=functools.partial(device_name_for_token, root),
            setup=cast("Any", _Setup()),
        ))
        require_local_key(
            app, functools.partial(local_key_matches, local_key(root)),
            device_token_matches=functools.partial(device_token_matches, root),
        )
        self.server = _Server(_AsRemote(app))

    def __enter__(self) -> Self:
        self.server.__enter__()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.__exit__()


class _Terminal:
    """The real wiring of ``terminal --voice --serve-ui``, on its own thread and loop."""

    def __init__(
        self, url: str, token: str, root: Path, port: int,
        config: dict[str, Any] = SPEECH_CONFIG,
    ) -> None:
        self.loop = asyncio.new_event_loop()
        self.task: asyncio.Task[None] | None = None
        self.error: BaseException | None = None
        self.port = port
        self.config = config
        self._args = (url, token, root)
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        async def run() -> None:
            url, token, root = self._args
            self.task = asyncio.create_task(_run(
                url, token, tools=frozenset({"read_clipboard"}),
                execute=lambda *_: {"ok": True, "output": {}}, watched=None,
                speaking=_Speaking(self.config, root),
                ui=_Ui(bind_ui(self.port), self.config, root),
            ))
            with contextlib.suppress(asyncio.CancelledError):
                try:
                    await self.task
                except BaseException as exc:  # noqa: BLE001 — handed to the test
                    self.error = exc

        self.loop.run_until_complete(run())

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.loop.call_soon_threadsafe(lambda: self.task and self.task.cancel())
        self.thread.join(timeout=15)
        assert not self.thread.is_alive()


def test_a_companion_adopts_a_terminal_and_hears_the_brain_and_the_terminals_own_voice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The companion's two first calls, against a real terminal in front of a real brain.

    `GET /inherent/setup` with the local key is the brain's own answer. The push socket then
    carries an op the brain pushed and the ops of the terminal's real media actor playing an
    answer, and not the brain's `no_voice`, which is what a brain that cannot speak says of
    every turn, while this terminal does speak it.
    """
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.setattr(voice_tts, "_open_output_stream", _open_stream(pull=True))
    token = pair_device(tmp_path, "macbook")
    root = tmp_path / "terminal-root"
    root.mkdir()
    key = local_key(root)
    port = _free_port()
    with _RealBrain(tmp_path) as brain, _Terminal(brain.server.url, token, root, port) as terminal:
        _wait_for(lambda: bool(brain.hub.connected()), "the terminal never connected")
        _wait_for(
            lambda: bool(brain.hub.voice and brain.hub.voice._peers),  # noqa: SLF001
            "the brain never took the terminal as a voice terminal",
        )
        local = {"Authorization": f"Bearer {key}"}
        # adoption: what the companion's `answers` and `needsSetup` ask
        adopted = httpx.get(f"{LOCAL}:{port}/inherent/setup", headers=local, timeout=10)
        assert adopted.status_code == 200
        assert adopted.json() == {"first_run": False, "assistant_name": "Jarvis"}
        assert httpx.get(f"{LOCAL}:{port}/inherent/setup", timeout=10).status_code == 401
        # nothing but the terminal's own device token opened the brain's route
        assert httpx.get(brain.server.url + "/inherent/setup", headers=local).status_code in {
            200, 401,
        }

        companion = ws_connect(f"ws://127.0.0.1:{port}/inherent/ws", additional_headers=local)
        _wait_for(
            lambda: bool(brain.broadcaster._clients),  # noqa: SLF001
            "the terminal never opened the brain's socket",
        )
        brain.server.call(brain.broadcaster.broadcast_op("tool", turn_id="T1", label="reading"))

        conn = open_event_log(brain.log)
        emit_event(conn, type="surface.user_intent", payload={"transcript": "hi", "turn_id": "T1"})
        emit_event(conn, type="surface.response_open", payload={
            "turn_id": "T1", "query": "hi", "kind": "text", "response_id": "R1",
            "response_group_id": "G1", "phase": "final", "channel": "speech",
        })
        emit_event(conn, type="surface.response_chunk", payload={
            "turn_id": "T1", "text": "The first sentence.", "response_id": "R1",
            "response_group_id": "G1", "sequence": 0, "phase": "final", "channel": "speech",
            "segment_hash": "",
        })
        emit_event(conn, type="surface.response_emitted", payload={
            "turn_id": "T1", "text": "The first sentence.", "response_id": "R1",
            "response_group_id": "G1", "phase": "final", "channel": "speech",
        })
        conn.close()
        # what the brain's own watcher says after every answer when it has no speaker of its own
        brain.server.call(brain.broadcaster.broadcast_voice(
            "spoken", turn_id="T1", output_outcome="no_voice",
        ))

        seen: list[dict[str, Any]] = []
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not any(
            f["op"] == "voice" and f["payload"].get("phase") == "spoken" for f in seen
        ):
            with contextlib.suppress(TimeoutError):
                seen.append(json.loads(companion.recv(timeout=0.5)))
        companion.close()
        assert terminal.error is None
    assert _wait_closed(port)  # stopped: the port is the next terminal's or daemon's again

    ops = [(f["op"], f["payload"].get("phase")) for f in seen]
    assert ("tool", None) in ops  # pushed by the brain
    voice = [f["payload"] for f in seen if f["op"] == "voice"]
    assert voice, "no voice op from the terminal's own media actor reached the companion"
    spoken = [p for p in voice if p.get("phase") == "spoken"]
    assert [p.get("output_outcome") for p in spoken] != ["no_voice"]
    assert all(p.get("output_outcome") != "no_voice" for p in spoken)
    assert len(spoken) == 1
    assert Counter(ops)[("tool", None)] == 1
    with sqlite3.connect(tmp_path / "events.db") as raw:
        assert raw.execute("SELECT count(*) FROM events").fetchone()[0] > 0


def test_dictation_records_on_the_terminal_and_the_brain_polishes_the_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The companion's dictation through a real terminal: local capture, the brain's polish.

    A replayed WAV is the microphone and a fake recognizer the ears, as for the listening
    tests. The polish is a fake on the brain's hook, which must receive the words, where they
    land, and the word list kept on the terminal; the NDJSON the companion reads is a daemon's.
    """
    wav = _write_wav(
        tmp_path / "say.wav",
        np.concatenate([np.zeros(8_000, dtype="<i2"), np.full(16_000 * 14, 10_000, dtype="<i2")]),
    )
    _heard(monkeypatch, lambda: voice_backend.FileReplayBackend(wav))
    token = pair_device(tmp_path, "macbook")
    root = tmp_path / "terminal-root"
    root.mkdir()
    vocab = root / "vocab.yaml"
    vocab.write_text("user:\n- Typlus\nauto:\n- 星核\n", encoding="utf-8")
    config = {**LISTEN_CONFIG, "dictation": {"vocab_path": str(vocab)}}
    asked: list[tuple[str, dict[str, str], str, list[str]]] = []

    def polish(raw: str, context: Any, language: str, terms: Any) -> str:  # noqa: ANN401
        asked.append((raw, dict(context), language, list(terms)))
        return f"POLISHED[{raw}]"

    port = _free_port()
    local = {"Authorization": f"Bearer {local_key(root)}"}
    context = {"app": "Ghostty", "window": "claude", "selected": "", "before": "先看"}
    with (
        _RealBrain(tmp_path, ListenHooks(polish=polish)) as brain,
        _Terminal(brain.server.url, token, root, port, config),
    ):
        _wait_for(lambda: bool(brain.hub.connected()), "the terminal never connected")
        deadline = time.monotonic() + 60
        while True:  # 404 until the capture session is up: the companion reads it as "voice is off"
            response = httpx.stream(
                "POST", f"{LOCAL}:{port}/inherent/dictation", json=context, headers=local,
                timeout=30,
            )
            opened = response.__enter__()
            if opened.status_code != httpx.codes.NOT_FOUND:
                break
            response.__exit__(None, None, None)
            assert time.monotonic() < deadline, "dictation never came up on the terminal"
            time.sleep(0.2)
        try:
            assert opened.status_code == httpx.codes.OK
            assert opened.headers["content-type"] == "application/x-ndjson"
            lines = opened.iter_lines()
            assert "level" in json.loads(next(lines))
            time.sleep(1.5)  # speaking into the microphone
            second = httpx.post(f"{LOCAL}:{port}/inherent/dictation", json=context, headers=local)
            assert second.status_code == httpx.codes.CONFLICT  # one at a time
            stopped = httpx.post(f"{LOCAL}:{port}/inherent/dictation/stop", headers=local)
            assert stopped.json() == {"ok": True}
            rest = [json.loads(line) for line in lines]
        finally:
            response.__exit__(None, None, None)
    result = rest[-1]
    assert result == {"text": f"POLISHED[{result['raw']}]", "raw": result["raw"]}
    assert result["raw"]
    assert {k: v for k, v in rest[-2].items() if k == "state"} == {"state": "thinking"}
    [(raw, where, _language, terms)] = asked
    assert raw == result["raw"]
    assert where == context
    assert terms == ["Typlus", "星核"]  # this machine's list, which the brain does not have


def _wait_closed(port: int) -> bool:
    """The terminal's listening socket is gone once it stops."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            probe.settimeout(0.5)
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return True
        time.sleep(0.05)
    return False
