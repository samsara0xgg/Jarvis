"""ADR 0183: a terminal serves its device's UI on loopback and is its only path to the brain.

``python -m jarvis terminal --serve-ui`` listens on 127.0.0.1 where a daemon would, so the
desktop companion adopts it unchanged. The app here admits what a daemon admits (a Host that
names this machine, and the local key of the runtime root on every route but the liveness
probe) and then does one of three things with a request:

* **forward** it to the brain under this device's token, whatever it is. The local key is never
  sent on, a refusal comes back as it is, and nothing is retried under another credential;
* **answer** it from this machine, for the few routes whose subject is the device itself (the
  table :data:`DEVICE_ROUTES`);
* **merge**: a route whose document is the brain's except for the device's own part.

Unclassed routes are forwarded. While the brain is unreachable a forwarded route answers 503
``{"detail": "the brain is not reachable"}``, the ``detail`` shape every route of the daemon
uses for an error and the companion reads. The push socket ``/inherent/ws`` carries the brain's
ops and this terminal's own ``voice`` ops, and closes at once when the brain is not reachable:
the companion reconnects on its own ladder and shows its error face meanwhile, which it would
not if a socket that carries nothing from the brain stayed open.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Protocol

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response, StreamingResponse

from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import (
    ControlsRequest,
    DictationRequest,
    DictationRoutes,
    SettingsRequest,
    dictation_stream,
    require_local_key,
)
from jarvis.surface.terminal_link import MAX_FRAME_CHARS, TERMINAL_PATH

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping

    from jarvis.surface.voice_controls import VoiceControls

LOGGER = logging.getLogger("jarvis.surface.terminal_ui")

UI_SOCKET: Final = "/inherent/ws"
UNREACHABLE: Final = "the brain is not reachable"
CONNECT_TIMEOUT_S: Final = 5.0
_METHODS: Final = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
_SOCKET_UNREACHABLE: Final = 1013  # "try again later": the companion's reconnect ladder takes it

DEVICE_ROUTES: Final[Mapping[tuple[str, str], str]] = {
    ("GET", "/api/health"): "answered: the liveness of this process, which the UI probes",
    ("POST", "/inherent/restart"): (
        "answered: the daemon's restart ends the process whose settings it applies; forwarded, "
        "it would restart the brain. 404 unless a supervisor brings this process back"
    ),
    ("POST", "/inherent/dictation"): (
        "answered: records on this microphone and hears with this machine's recognizer; the "
        "polish asks the brain. 404 (voice is off) without a running capture session"
    ),
    ("POST", "/inherent/dictation/stop"): "answered: finishes that recording",
    ("GET", "/inherent/settings"): (
        "merged: the brain's page with this machine's microphone and speaker choices"
    ),
    ("POST", "/inherent/settings"): (
        "merged: those two choices are saved here and applied at this terminal's restart; the "
        "rest of the changes go to the brain"
    ),
    ("POST", "/inherent/controls"): (
        "merged: the mic and speech switches act on this machine's microphone and speaker; "
        "the whole request also goes to the brain, which holds conversation, quiet and live"
    ),
}
"""Every route this terminal does not simply forward, and why. Anything else is forwarded.

The voice preview of first-run setup is not here: the brain holds the speech key and the
companion plays the MP3 it gets back, so forwarding is right (ADR 0183 named it a device route
before the code was read).
"""

NOT_SERVED: Final = frozenset(
    {TERMINAL_PATH, "/inherent/ws/v2", "/inherent/submit/v2", "/inherent/asr-submit/v2"},
)
"""Never forwarded: ``/terminal/ws`` would let any local process register as this device, and
the v2 routes are the daemon's own keyless ones, which the companion does not use."""

_FORWARDED_REQUEST_HEADERS: Final = frozenset(
    {
        "accept", "accept-language", "cache-control", "content-length", "content-type",
        "if-modified-since", "if-none-match", "range",
    },
)
_DROPPED_RESPONSE_HEADERS: Final = frozenset(
    {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
     "transfer-encoding", "upgrade", "server", "date"},
)


class DeviceSettings(Protocol):
    """This machine's own part of the Settings page: its microphone and speaker choices."""

    @property
    def keys(self) -> frozenset[str]:
        """The Settings keys that are this machine's own."""
        ...

    def read(self) -> dict[str, Any]:
        """The page's document as this machine alone would show it."""
        ...

    def update(self, changes: Mapping[str, Any]) -> dict[str, Any]:
        """Save the given choices; ``ValueError`` names a bad one."""
        ...


@dataclass
class Device:
    """What this machine answers itself. Each ``None`` is a route this terminal cannot serve.

    A missing ``restart`` or ``dictation`` is a 404, as on a daemon without them; a missing
    ``controls`` or ``settings`` leaves those routes to the brain.
    """

    controls: VoiceControls | None = None
    settings: DeviceSettings | None = None
    restart: Callable[[], None] | None = None
    dictation: Callable[[], DictationRoutes | None] | None = None
    """The recording session, or ``None`` while there is none (not built yet, or no microphone)."""
    speaks: Callable[[], bool] = lambda: False
    """Whether this machine's own media actor plays the brain's answers."""


@dataclass
class Brain:
    """Where the brain is and the token that opens it for this device."""

    base_url: str
    token: str = field(repr=False)
    transport: httpx.AsyncBaseTransport | None = None
    _client: httpx.AsyncClient | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        """Open the one connection pool; no proxy from the environment, as for the link."""
        self._client = httpx.AsyncClient(
            base_url=self.base_url, transport=self.transport, trust_env=False,
            timeout=httpx.Timeout(None, connect=CONNECT_TIMEOUT_S),
        )

    @property
    def socket_url(self) -> str:
        """The brain's push socket."""
        return f"ws{self.base_url.removeprefix('http')}{UI_SOCKET}"

    @property
    def bearer(self) -> dict[str, str]:
        """The only credential that goes to the brain."""
        return {"Authorization": f"Bearer {self.token}"}

    async def close(self) -> None:
        """Close the pool."""
        if self._client is not None:
            await self._client.aclose()

    async def open(
        self, method: str, url: str, headers: Mapping[str, str],
        content: AsyncIterator[bytes] | bytes | None,
    ) -> httpx.Response:
        """Send one request and leave its body unread; the caller closes the response."""
        assert self._client is not None  # noqa: S101 — set in __post_init__
        request = self._client.build_request(
            method, url, headers={**headers, **self.bearer, "Accept-Encoding": "identity"},
            content=content,
        )
        return await self._client.send(request, stream=True)

    async def call(
        self, method: str, path: str, body: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        """Send one small JSON request and read its answer whole."""
        response = await self.open(
            method, path, {"Content-Type": "application/json"} if body is not None else {},
            None if body is None else json.dumps(body).encode(),
        )
        try:
            await response.aread()
        finally:
            await response.aclose()
        return response


def _unreachable() -> JSONResponse:
    return JSONResponse({"detail": UNREACHABLE}, status_code=503)


def _passed(response: httpx.Response) -> Response:
    """A brain's whole answer, as it was."""
    headers = {
        key: value for key, value in response.headers.items()
        if key.lower() not in _DROPPED_RESPONSE_HEADERS | {"content-length"}
    }
    return Response(response.content, status_code=response.status_code, headers=headers)


class UiBroadcaster(InherentBroadcaster):
    """The registry of the companion's sockets, written to by this terminal's own voice.

    The capture session and the media actor broadcast into it exactly as into a daemon's; the
    brain's ops come in through :meth:`relay`, under the same lock, so one socket never has two
    sends at once and a socket sees one order. With no companion connected an op is dropped
    quietly: the base class's warning is for a daemon whose surface should be there.
    """

    async def _send_all_locked(self, msg: dict[str, object], *, turn_id: str) -> None:
        if self._clients:
            await super()._send_all_locked(msg, turn_id=turn_id)

    async def relay(self, ws: WebSocket, text: str) -> None:
        """Send the brain's frame ``text`` to one companion socket, in order with the rest."""
        async with self._send_lock:
            await ws.send_text(text)


def _own_voice(device: Device, text: str) -> str | None:
    """The brain's frame as this device's companion should see it; ``None`` drops it.

    Two of the brain's ops describe a thing this device has its own say on:

    * ``voice`` / ``spoken`` / ``no_voice`` is the brain saying nobody speaks the answer, since
      it has no speaker of its own. While this terminal's media actor plays it, that is false
      and would end the turn on the companion before the playback does; the media actor says
      ``spoken`` itself, with the real outcome. This is the one op both ends could send for the
      same turn, and the only one dropped.
    * ``controls`` carries ``mic_muted`` and ``speech_muted``, which are this device's.
    """
    try:
        frame = json.loads(text)
    except ValueError:
        return text
    payload = frame.get("payload") if isinstance(frame, dict) else None
    if not isinstance(payload, dict):
        return text
    if frame.get("op") == "voice":
        dropped = payload.get("phase") == "spoken" and payload.get("output_outcome") == "no_voice"
        return None if dropped and device.speaks() else text
    if frame.get("op") == "controls" and device.controls is not None:
        payload["mic_muted"] = device.controls.mic_muted
        payload["speech_muted"] = device.controls.speech_muted
        return json.dumps(frame, ensure_ascii=False)
    return text


async def _relay(
    upstream: Any, broadcaster: UiBroadcaster, ws: WebSocket, device: Device,  # noqa: ANN401 — a websockets connection.
) -> None:
    """Carry the brain's frames to one companion socket until either end closes."""
    async for message in upstream:
        text = message if isinstance(message, str) else message.decode("utf-8", "replace")
        frame = _own_voice(device, text)
        if frame is not None:
            await broadcaster.relay(ws, frame)


def _register_socket(
    app: FastAPI, brain: Brain, broadcaster: UiBroadcaster, device: Device,
) -> None:
    @app.websocket(UI_SOCKET)
    async def ui_socket(ws: WebSocket) -> None:
        """The companion's push socket: the brain's ops and this terminal's own, one stream.

        The brain's socket is opened first and the companion's accepted only once it is: a
        companion that finds nothing here retries, and one that finds a socket with nothing
        behind it would show a daemon that is up. Each companion socket has its own brain
        socket, so the brain's snapshot on connect (the ``live`` state) reaches each of them as
        it does on a daemon.
        """
        from websockets.asyncio.client import connect  # noqa: PLC0415 — as the link does.
        from websockets.exceptions import WebSocketException  # noqa: PLC0415

        try:
            upstream = await connect(
                brain.socket_url, open_timeout=CONNECT_TIMEOUT_S, proxy=None,
                max_size=MAX_FRAME_CHARS, additional_headers=brain.bearer,
            )
        except (OSError, TimeoutError, ValueError, WebSocketException) as exc:
            LOGGER.warning("the brain's push socket did not open (%s)", type(exc).__name__)
            await ws.close(code=_SOCKET_UNREACHABLE)
            return
        await ws.accept()
        await broadcaster.register(ws)
        relaying = asyncio.create_task(_relay(upstream, broadcaster, ws, device))
        reading = asyncio.create_task(_drain(ws))
        try:
            await asyncio.wait({relaying, reading}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (relaying, reading):
                task.cancel()
            await asyncio.wait({relaying, reading})
            for task in (relaying, reading):
                if not task.cancelled():
                    task.exception()  # a dropped socket: read, so asyncio does not log it twice
            await broadcaster.unregister(ws)
            await upstream.close()
            with contextlib.suppress(Exception):  # the companion may have closed it already
                await ws.close(code=_SOCKET_UNREACHABLE)


async def _drain(ws: WebSocket) -> None:
    """The companion's socket is push-only; read until it goes away."""
    with contextlib.suppress(WebSocketDisconnect):
        while True:
            await ws.receive_text()


async def _forward(request: Request, brain: Brain) -> Response:
    """Send ``request`` on to the brain under this device's token and stream the answer back."""
    path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    headers = {
        key: value for key, value in request.headers.items()
        if key.lower() in _FORWARDED_REQUEST_HEADERS
    }
    has_body = request.headers.get("content-length", "0") != "0" or (
        "transfer-encoding" in request.headers
    )
    try:
        upstream = await brain.open(
            request.method, path, headers, request.stream() if has_body else None,
        )
    except httpx.TransportError as exc:
        LOGGER.warning("%s %s: the brain did not answer (%s)", request.method, request.url.path,
                       type(exc).__name__)
        return _unreachable()

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    headers_back = {
        key: value for key, value in upstream.headers.items()
        if key.lower() not in _DROPPED_RESPONSE_HEADERS
    }
    return StreamingResponse(body(), status_code=upstream.status_code, headers=headers_back)


def _register_device_routes(app: FastAPI, brain: Brain, device: Device) -> None:  # noqa: C901 — one closed table of routes.
    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/inherent/restart", status_code=202)
    async def restart() -> dict[str, bool]:
        if device.restart is None:
            raise HTTPException(status_code=404, detail="Not Found")
        device.restart()
        return {"ok": True}

    @app.post("/inherent/dictation")
    async def dictate(req: DictationRequest) -> StreamingResponse:
        dictation = None if device.dictation is None else device.dictation()
        if dictation is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return dictation_stream(dictation, req)

    @app.post("/inherent/dictation/stop")
    async def dictate_stop() -> dict[str, bool]:
        dictation = None if device.dictation is None else device.dictation()
        if dictation is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return {"ok": dictation.stop()}

    @app.get("/inherent/settings")
    async def settings_read(request: Request) -> Response:
        if device.settings is None:
            return await _forward(request, brain)
        try:
            answer = await brain.call("GET", "/inherent/settings")
        except httpx.TransportError:
            return _unreachable()
        return _with_devices(answer, device.settings)

    @app.post("/inherent/settings")
    async def settings_save(req: SettingsRequest, request: Request) -> Response:
        own = device.settings
        if own is None:
            return await _forward(request, brain)
        mine = {key: value for key, value in req.changes.items() if key in own.keys}
        rest = {key: value for key, value in req.changes.items() if key not in own.keys}
        if mine:
            try:
                await asyncio.to_thread(own.update, mine)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)[:300]) from None
        try:
            answer = (
                await brain.call("POST", "/inherent/settings", {"changes": rest})
                if rest else await brain.call("GET", "/inherent/settings")
            )
        except httpx.TransportError:
            return _unreachable()
        return _with_devices(answer, own)

    @app.post("/inherent/controls")
    async def controls(req: ControlsRequest, request: Request) -> Response:
        own = device.controls
        if own is None:
            return await _forward(request, brain)
        own.update(mic_muted=req.mic_muted, speech_muted=req.speech_muted)
        try:
            answer = await brain.call(
                "POST", "/inherent/controls", req.model_dump(exclude_none=True),
            )
        except httpx.TransportError:
            return _unreachable()
        state = _json(answer)
        if answer.status_code != httpx.codes.OK or state is None:
            return _passed(answer)
        state["mic_muted"], state["speech_muted"] = own.mic_muted, own.speech_muted
        return JSONResponse(state)


def _json(answer: httpx.Response) -> dict[str, Any] | None:
    try:
        body = answer.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _with_devices(answer: httpx.Response, own: DeviceSettings) -> Response:
    """The brain's Settings document with this machine's device choices in place of its own."""
    page = _json(answer)
    if answer.status_code != httpx.codes.OK or page is None:
        return _passed(answer)
    mine = own.read()
    for part in ("values", "options", "defaults"):
        if isinstance(page.get(part), dict) and isinstance(mine.get(part), dict):
            page[part].update({key: mine[part][key] for key in own.keys if key in mine[part]})
    page["restart_pending"] = bool(page.get("restart_pending")) or bool(mine["restart_pending"])
    return JSONResponse(page)


def create_ui_app(
    brain: Brain,
    *,
    authorize: Callable[[str | None], bool],
    broadcaster: UiBroadcaster,
    device: Device,
) -> FastAPI:
    """The app a terminal serves on loopback; ``authorize`` is the daemon's local-key check."""
    app = FastAPI(title="Jarvis terminal UI", docs_url=None, redoc_url=None, openapi_url=None)
    _register_device_routes(app, brain, device)
    _register_socket(app, brain, broadcaster, device)

    @app.api_route("/{path:path}", methods=_METHODS)
    async def forward(request: Request) -> Response:
        if request.url.path in NOT_SERVED:
            raise HTTPException(status_code=404, detail="Not Found")
        return await _forward(request, brain)

    # The daemon's own gate: the Host check, then the local key on every route but the probe.
    require_local_key(app, authorize)
    return app


__all__ = [
    "DEVICE_ROUTES",
    "NOT_SERVED",
    "Brain",
    "Device",
    "DeviceSettings",
    "UiBroadcaster",
    "create_ui_app",
]
