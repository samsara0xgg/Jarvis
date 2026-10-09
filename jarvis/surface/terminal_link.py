"""ADR 0170: the link between a brain and the terminals that run its device-bound tools.

A terminal connects outward to the brain's ``/terminal/ws`` with its device token, says which
tools it can run, and then answers calls. Every frame is one JSON text message::

    terminal -> brain   {"type": "hello", "tools": ["read_file", ...]}
    brain -> terminal   {"type": "ready", "device": "<paired name>"}
    brain -> terminal   {"type": "call", "id": "<hex>", "tool": "...",
                         "arguments": {...}, "target_entity_ref": "file:..." | null}
    terminal -> brain   {"type": "result", "id": "<hex>", "ok": true, "output": {...}}
    terminal -> brain   {"type": "result", "id": "<hex>", "ok": false,
                         "code": "...", "message": "..."}
    terminal -> brain   {"type": "event", ...}   an observer's event, see terminal_events
    brain -> terminal   {"type": "ack", ...}
    both ways           {"type": "row" | "tts", ...}   a voice terminal's speech, see terminal_voice
    terminal -> brain   {"type": "ask", ...}    a voice terminal's listening, see terminal_listen
    brain -> terminal   {"type": "reply", ...}  its answer

Results are data: the brain reads ``output`` as a JSON object and nothing in it is run. This
module holds the brain's side (:class:`TerminalHub`, :func:`serve_terminal`) and the terminal's
(:func:`run_terminal_client`); what a call does lives with the tools, and the layers above wire
the two together.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from fastapi import WebSocketDisconnect

from jarvis.shared.device_link import DeviceCallError
from jarvis.surface.terminal_events import OBSERVER_EVENT_TYPES, VOICE_TERMINAL_EVENT_TYPES

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Mapping

    from fastapi import WebSocket

    from jarvis.surface.terminal_events import BrainEvents, EventOutbox
    from jarvis.surface.terminal_listen import BrainListening, LinkState
    from jarvis.surface.terminal_speaker import VoiceLink
    from jarvis.surface.terminal_voice import BrainVoice, Peer

LOGGER = logging.getLogger("jarvis.surface.terminal_link")

TERMINAL_PATH: Final = "/terminal/ws"
CALL_TIMEOUT_S: Final = 30.0
"""How long the brain waits for one call's result; the slowest device tool takes ~20 s."""
HELLO_TIMEOUT_S: Final = 5.0
MAX_FRAME_CHARS: Final = 8 * 1024 * 1024
"""One frame's cap: a 4 MiB screenshot is ~5.6 MB of base64."""
_MAX_ERROR_CHARS: Final = 2000
_MAX_TOOL_NAME_CHARS: Final = 64
_RECONNECT_FIRST_S: Final = 1.0
_RECONNECT_LAST_S: Final = 30.0
_REFUSED_STATUSES: Final = frozenset({401, 403})

type Execute = Callable[[str, Mapping[str, Any], str | None], dict[str, Any]]
"""The terminal's runner: a blocking call that returns ``{"ok": True, "output": {...}}`` or
``{"ok": False, "code": ..., "message": ...}``."""


@dataclass(eq=False)
class _Link:
    """One connected terminal, as the brain sees it."""

    name: str
    tools: frozenset[str]
    loop: asyncio.AbstractEventLoop
    send: Callable[[str], Coroutine[Any, Any, None]]
    # call id -> (tool, future); touched from the loop thread and from tool worker threads.
    pending: dict[str, tuple[str, concurrent.futures.Future[dict[str, Any]]]] = field(
        default_factory=dict,
    )
    voice: bool = False  # the terminal declared it speaks (ADR 0172)
    speech: Peer | None = None  # its place in the brain's BrainVoice, once ready
    listen: LinkState | None = None  # what BrainListening keeps for it, once it asks


class TerminalHub:
    """The brain's table of connected terminals, and the one place a device call is sent from.

    ``call`` is synchronous because tool handlers are; it must run on a worker thread, never
    the event loop that serves the sockets.
    """

    def __init__(
        self, *, call_timeout_s: float = CALL_TIMEOUT_S, events: BrainEvents | None = None,
    ) -> None:
        """Start with no terminal connected; ``events`` is where their observers' events go."""
        self._call_timeout_s = call_timeout_s
        self.events = events
        self.voice: BrainVoice | None = None
        """What speaks to a voice terminal (ADR 0172); set once the brain can synthesize."""
        self.listening: BrainListening | None = None
        """What answers a voice terminal's capture session (ADR 0172); set by the runtime."""
        self._lock = threading.Lock()
        self._links: list[_Link] = []  # oldest connection first
        self._last_name: dict[str, str] = {}

    def attach(
        self,
        name: str,
        tools: frozenset[str],
        send: Callable[[str], Coroutine[Any, Any, None]],
        *,
        voice: bool = False,
    ) -> _Link:
        """Register a terminal that just said hello; call from the loop that owns ``send``."""
        link = _Link(name, tools, asyncio.get_running_loop(), send, voice=voice)
        with self._lock:
            self._links.append(link)
            for tool in tools:
                self._last_name[tool] = name
        LOGGER.info("terminal %s connected, runs %s", name, sorted(tools))
        return link

    def detach(self, link: _Link) -> None:
        """Drop a terminal and fail every call still waiting on it."""
        with self._lock:
            if link in self._links:
                self._links.remove(link)
            pending, link.pending = link.pending, {}
        for tool, future in pending.values():
            if not future.done():
                future.set_exception(
                    DeviceCallError(
                        f"{link.name} disconnected while running {tool}",
                        code="device_disconnected",
                    ),
                )
        LOGGER.info("terminal %s disconnected", link.name)

    def deliver(self, link: _Link, frame: Mapping[str, Any]) -> None:
        """Hand one ``result`` frame to the call waiting for it; an unknown id is dropped."""
        with self._lock:
            entry = link.pending.pop(str(frame.get("id")), None)
        if entry is None or entry[1].done():
            return
        output = frame.get("output")
        if frame.get("ok") is True and isinstance(output, dict):
            entry[1].set_result(output)
            return
        message = str(frame.get("message") or f"{link.name} could not run {entry[0]}")
        code = str(frame.get("code") or "device_error")
        entry[1].set_exception(
            DeviceCallError(message[:_MAX_ERROR_CHARS], code=code[:_MAX_TOOL_NAME_CHARS]),
        )

    def connected(self) -> list[tuple[str, frozenset[str]]]:
        """Each connected terminal as ``(name, tools)``, oldest first."""
        with self._lock:
            return [(link.name, link.tools) for link in self._links]

    def roster(self) -> list[tuple[str, bool, bool]]:
        """Each connected terminal as ``(name, speaks, listens)``, oldest first."""
        with self._lock:
            return [(link.name, link.voice, link.listen is not None) for link in self._links]

    def call(
        self, tool: str, arguments: Mapping[str, Any], target_entity_ref: str | None,
    ) -> dict[str, Any]:
        """Run ``tool`` on the most recently connected terminal that declared it.

        Raises:
            DeviceCallError: no such terminal, no answer in time, the terminal went away
                mid-call, or the terminal reported a failure (its own code and message).
        """
        with self._lock:
            link = next((item for item in reversed(self._links) if tool in item.tools), None)
            label = self._last_name.get(tool, "the terminal")
        if link is None:
            msg = f"{label} is not connected right now, so {tool} cannot run"
            raise DeviceCallError(msg, code="device_not_connected")
        return self.call_on(link, tool, arguments, target_entity_ref, self._call_timeout_s)

    def call_on(
        self, link: _Link, tool: str, arguments: Mapping[str, Any],
        target_entity_ref: str | None = None, timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Run ``tool`` on this terminal, whether or not it declared it.

        The brain's own commands to a voice terminal (ADR 0172) are not menu tools. Same errors
        as :meth:`call`.
        """
        timeout = self._call_timeout_s if timeout_s is None else timeout_s
        deadline = time.monotonic() + timeout
        call_id = uuid.uuid4().hex
        future: concurrent.futures.Future[dict[str, Any]] = concurrent.futures.Future()
        with self._lock:
            if link not in self._links:
                msg = f"{link.name} disconnected before it could run {tool}"
                raise DeviceCallError(msg, code="device_disconnected")
            link.pending[call_id] = (tool, future)
        frame = json.dumps(
            {
                "type": "call",
                "id": call_id,
                "tool": tool,
                "arguments": dict(arguments),
                "target_entity_ref": target_entity_ref,
            },
            ensure_ascii=False,
        )
        try:
            self._send(link, frame, deadline)
            return future.result(timeout=max(0.0, deadline - time.monotonic()))
        except concurrent.futures.TimeoutError:
            msg = f"{link.name} did not answer {tool} within {timeout:g} s"
            raise DeviceCallError(msg, code="device_timeout") from None
        finally:
            with self._lock:
                link.pending.pop(call_id, None)

    def is_connected(self, link: _Link) -> bool:
        """Whether ``link`` is still a connected terminal."""
        with self._lock:
            return link in self._links

    def voice_link_of(self, peer: Peer) -> _Link | None:
        """The connected terminal whose speech is ``peer``."""
        with self._lock:
            return next((item for item in self._links if item.speech is peer), None)

    @staticmethod
    def _send(link: _Link, frame: str, deadline: float) -> None:
        """Send ``frame`` on the terminal's own loop; a dead socket is a disconnect."""
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is link.loop:
            msg = "a device call must not run on the loop that serves the terminal"
            raise DeviceCallError(msg, code="device_call_on_loop")
        sent = asyncio.run_coroutine_threadsafe(link.send(frame), link.loop)
        try:
            sent.result(timeout=max(0.0, deadline - time.monotonic()))
        except concurrent.futures.TimeoutError:
            sent.cancel()
            raise
        except Exception:  # noqa: BLE001 — whatever the socket raised, the terminal is gone.
            msg = f"{link.name} disconnected before it could run the call"
            raise DeviceCallError(msg, code="device_disconnected") from None


@dataclass(frozen=True)
class _Hello:
    tools: frozenset[str]
    voice: bool
    rows_after: int | None


def _parse_hello(text: str) -> _Hello | None:
    """What a terminal's first frame declares; ``None`` if it is not a good hello."""
    try:
        frame = json.loads(text)
    except ValueError:
        return None
    if not isinstance(frame, dict) or frame.get("type") != "hello":
        return None
    tools, voice, after = frame.get("tools"), frame.get("voice", False), frame.get("rows_after")
    if not isinstance(tools, list) or not all(
        isinstance(item, str) and 0 < len(item) <= _MAX_TOOL_NAME_CHARS for item in tools
    ):
        return None
    if not isinstance(voice, bool):
        return None
    if after is not None and (type(after) is not int or after < 0):
        return None
    return _Hello(frozenset(tools), voice, after)


async def serve_terminal(hub: TerminalHub, ws: WebSocket, name: str) -> None:
    """The brain's end of one terminal's socket, from accept to disconnect.

    ``name`` is the paired device the caller's token belongs to; the caller has already
    checked the token. A first frame that is not a hello within a few seconds closes the
    socket.
    """
    await ws.accept()
    try:
        hello = _parse_hello(await asyncio.wait_for(ws.receive_text(), HELLO_TIMEOUT_S))
    except (TimeoutError, WebSocketDisconnect):
        hello = None
    if hello is None:
        with contextlib.suppress(RuntimeError):
            await ws.close(code=1008)
        return
    link = hub.attach(name, hello.tools, ws.send_text, voice=hello.voice)
    streaming: asyncio.Task[None] | None = None
    try:
        streaming = await _greet(hub, link, ws, hello)
        while True:
            text = await ws.receive_text()
            if len(text) > MAX_FRAME_CHARS:
                await ws.close(code=1009)
                return
            try:
                frame = json.loads(text)
            except ValueError:
                continue
            if isinstance(frame, dict):
                await _on_frame(hub, link, ws, frame, len(text))
    except WebSocketDisconnect:
        pass
    finally:
        if hub.listening is not None:
            await hub.listening.detach(link)
        await _end_speech(hub, link, streaming)
        hub.detach(link)


async def _greet(
    hub: TerminalHub, link: _Link, ws: WebSocket, hello: _Hello,
) -> asyncio.Task[None] | None:
    """Send ``ready``; for a voice terminal the brain can speak to, start its row stream."""
    if hello.voice and hub.voice is not None:
        link.speech = hub.voice.attach(link.name, ws.send_text, hello.rows_after)
    ready: dict[str, Any] = {"type": "ready", "device": link.name}
    if hub.events is not None:
        ready["baseline"] = hub.events.baseline()
    if hello.voice:
        ready["voice"] = hub.voice is not None
        ready["listen"] = hub.listening is not None
    await ws.send_text(json.dumps(ready, ensure_ascii=False))
    if link.speech is None or hub.voice is None:
        return None
    return asyncio.create_task(hub.voice.stream_rows(link.speech))


async def _end_speech(hub: TerminalHub, link: _Link, streaming: asyncio.Task[None] | None) -> None:
    """Stop a voice terminal's row stream and abort the provider sessions it held."""
    if streaming is not None:
        streaming.cancel()
        await asyncio.wait({streaming})
        if not streaming.cancelled():
            streaming.exception()  # a dead socket: read, so asyncio does not log it twice
    if link.speech is not None and hub.voice is not None:
        await hub.voice.detach(link.speech)


async def _on_frame(
    hub: TerminalHub, link: _Link, ws: WebSocket, frame: Mapping[str, Any], size: int,
) -> None:
    """A terminal's ``result`` goes to the call waiting for it; its ``event`` to the log.

    A voice terminal's ``tts`` request goes to its provider session, and its ``ask`` to
    :class:`~jarvis.surface.terminal_listen.BrainListening` (ADR 0172).
    """
    if frame.get("type") == "result":
        hub.deliver(link, frame)
    elif frame.get("type") == "event":
        await _record_event(hub, ws, link, frame, size)
    elif frame.get("type") == "tts":
        if link.speech is not None and hub.voice is not None:
            hub.voice.on_frame(link.speech, frame)
        elif type(frame.get("rid")) is int:  # a terminal that did not declare voice, or no provider
            refusal = {
                "type": "tts", "sid": frame.get("sid"), "rid": frame["rid"], "ok": False,
                "error": {"name": "Refused", "message": "this brain has no speech provider"},
            }
            await ws.send_text(json.dumps(refusal))
    elif frame.get("type") == "ask" and link.voice:
        if hub.listening is not None:
            hub.listening.on_frame(link, frame)
        elif isinstance(frame.get("id"), str):
            await ws.send_text(json.dumps({
                "type": "reply", "id": frame["id"], "ok": False, "code": "not_listening",
                "message": "this brain does not take a terminal's listening",
            }))


async def _record_event(
    hub: TerminalHub, ws: WebSocket, link: _Link, frame: Mapping[str, Any], size: int,
) -> None:
    """Append one event the terminal ``link`` sent, and answer it."""
    if hub.events is None:
        uid = frame.get("event_uid")
        ack: dict[str, Any] | None = {
            "type": "ack", "event_uid": uid if isinstance(uid, str) else None, "ok": False,
            "code": "events_not_accepted",
        }
    else:
        allowed = VOICE_TERMINAL_EVENT_TYPES if link.voice else OBSERVER_EVENT_TYPES
        ack = hub.events.record(link.name, frame, size, allowed)
    if ack is not None:
        await ws.send_text(json.dumps(ack))


class TerminalRefusedError(Exception):
    """The brain turned this terminal's token down; retrying cannot help."""


async def _answer(ws: Any, execute: Execute, frame: Mapping[str, Any]) -> None:  # noqa: ANN401 — a websockets connection.
    """Run one ``call`` frame on a worker thread and send its ``result``."""
    arguments = frame.get("arguments")
    target = frame.get("target_entity_ref")
    try:
        result = await asyncio.to_thread(
            execute,
            str(frame.get("tool")),
            arguments if isinstance(arguments, dict) else {},
            target if isinstance(target, str) else None,
        )
    except Exception as exc:  # a failed tool is an answer, not the end of the link
        LOGGER.exception("terminal: %s raised", frame.get("tool"))
        result = {"ok": False, "code": "terminal_error", "message": type(exc).__name__}
    with contextlib.suppress(Exception):  # the link may have dropped while the tool ran
        await ws.send(
            json.dumps({"type": "result", "id": frame.get("id"), **result}, ensure_ascii=False),
        )


async def _serve_calls(
    ws: Any, execute: Execute, events: EventOutbox | None,  # noqa: ANN401 — a websockets connection.
    voice: VoiceLink | None = None,
) -> None:
    """Answer calls and take acks until the socket closes; a call in flight is dropped with it.

    A voice terminal also takes the brain's rows, provider frames and replies (ADR 0172).
    """
    running: set[asyncio.Task[None]] = set()
    try:
        async for message in ws:
            try:
                frame = json.loads(message)
            except ValueError:
                continue
            if isinstance(frame, dict) and frame.get("type") == "call":
                task = asyncio.create_task(_answer(ws, execute, frame))
                running.add(task)
                task.add_done_callback(running.discard)
            elif events is not None and isinstance(frame, dict) and frame.get("type") == "ack":
                events.ack(frame)
            elif voice is not None and isinstance(frame, dict) and frame.get("type") in {
                "row", "tts", "reply",
            }:
                voice.on_frame(frame)
    finally:
        for task in running:
            task.cancel()


async def _hold(
    ws: Any,  # noqa: ANN401 — a websockets connection.
    ready: Mapping[str, Any],
    execute: Execute,
    events: EventOutbox | None,
    voice: VoiceLink | None,
) -> None:
    """Carry one accepted connection until it closes."""
    if voice is not None:
        voice.up(ws.send, asyncio.get_running_loop(), ready)
    try:
        if events is None:
            await _serve_calls(ws, execute, None, voice)
            return
        events.ready(ready)
        pump = asyncio.create_task(events.pump(ws))
        try:
            await _serve_calls(ws, execute, events, voice)
        finally:
            pump.cancel()
            await asyncio.wait({pump})
            if not pump.cancelled():
                pump.exception()  # a dead socket; the caller reconnects
    finally:
        if voice is not None:
            voice.down()


async def run_terminal_client(  # noqa: PLR0913 — one keyword per thing a terminal carries.
    base_url: str,
    token: str,
    *,
    tools: frozenset[str],
    execute: Execute,
    events: EventOutbox | None = None,
    voice: VoiceLink | None = None,
) -> None:
    """Hold a connection to the brain's ``/terminal/ws`` forever, answering its calls.

    With ``events`` it also sends the observers' events queued there and takes the brain's acks.
    With ``voice`` it declares that this terminal speaks and carries its speech frames.

    Reconnects with a doubling wait (1 s up to 30 s) whenever the link drops or the brain is
    down; a token the brain refuses ends it with :class:`TerminalRefusedError`. Cancel it to
    stop.
    """
    from websockets.asyncio.client import connect  # noqa: PLC0415 — only a terminal needs it.
    from websockets.exceptions import InvalidStatus, WebSocketException  # noqa: PLC0415

    url = f"ws{base_url.removeprefix('http')}{TERMINAL_PATH}"
    wait = _RECONNECT_FIRST_S
    while True:
        try:
            async with connect(
                url,
                open_timeout=HELLO_TIMEOUT_S,
                proxy=None,
                max_size=MAX_FRAME_CHARS,
                additional_headers={"Authorization": f"Bearer {token}"},
            ) as ws:
                hello: dict[str, Any] = {"type": "hello", "tools": sorted(tools)}
                if voice is not None:
                    hello.update(voice.hello())
                await ws.send(json.dumps(hello))
                ready = json.loads(await asyncio.wait_for(ws.recv(), HELLO_TIMEOUT_S))
                if not isinstance(ready, dict) or ready.get("type") != "ready":
                    msg = "the brain did not accept this terminal"
                    raise OSError(msg)  # noqa: TRY301 — one place for "reconnect and log".
                LOGGER.info("connected to the brain as %s, runs %s", ready.get("device"),
                            sorted(tools))
                wait = _RECONNECT_FIRST_S
                await _hold(ws, ready, execute, events, voice)
        except InvalidStatus as exc:
            status = exc.response.status_code
            if status in _REFUSED_STATUSES:
                msg = f"the brain refused this device's token (HTTP {status}); pair it again"
                raise TerminalRefusedError(msg) from None
            LOGGER.warning("the brain answered HTTP %s; retrying in %g s", status, wait)
        except (OSError, TimeoutError, ValueError, WebSocketException) as exc:
            LOGGER.warning(
                "the brain link is down (%s); retrying in %g s", exc or type(exc).__name__, wait,
            )
        await asyncio.sleep(wait)
        wait = min(wait * 2, _RECONNECT_LAST_S)
