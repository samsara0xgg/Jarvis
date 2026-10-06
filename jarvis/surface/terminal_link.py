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

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Mapping

    from fastapi import WebSocket

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


class TerminalHub:
    """The brain's table of connected terminals, and the one place a device call is sent from.

    ``call`` is synchronous because tool handlers are; it must run on a worker thread, never
    the event loop that serves the sockets.
    """

    def __init__(self, *, call_timeout_s: float = CALL_TIMEOUT_S) -> None:
        """Start with no terminal connected."""
        self._call_timeout_s = call_timeout_s
        self._lock = threading.Lock()
        self._links: list[_Link] = []  # oldest connection first
        self._last_name: dict[str, str] = {}

    def attach(
        self, name: str, tools: frozenset[str], send: Callable[[str], Coroutine[Any, Any, None]],
    ) -> _Link:
        """Register a terminal that just said hello; call from the loop that owns ``send``."""
        link = _Link(name, tools, asyncio.get_running_loop(), send)
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

    def call(
        self, tool: str, arguments: Mapping[str, Any], target_entity_ref: str | None,
    ) -> dict[str, Any]:
        """Run ``tool`` on the most recently connected terminal that declared it.

        Raises:
            DeviceCallError: no such terminal, no answer in time, the terminal went away
                mid-call, or the terminal reported a failure (its own code and message).
        """
        deadline = time.monotonic() + self._call_timeout_s
        with self._lock:
            link = next((item for item in reversed(self._links) if tool in item.tools), None)
            if link is None:
                label = self._last_name.get(tool, "the terminal")
                msg = f"{label} is not connected right now, so {tool} cannot run"
                raise DeviceCallError(msg, code="device_not_connected")
            call_id = uuid.uuid4().hex
            future: concurrent.futures.Future[dict[str, Any]] = concurrent.futures.Future()
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
            msg = f"{link.name} did not answer {tool} within {self._call_timeout_s:g} s"
            raise DeviceCallError(msg, code="device_timeout") from None
        finally:
            with self._lock:
                link.pending.pop(call_id, None)

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


def _hello_tools(text: str) -> frozenset[str] | None:
    """The tool names a terminal's first frame declares; ``None`` if it is not a hello."""
    try:
        frame = json.loads(text)
    except ValueError:
        return None
    tools = frame.get("tools") if isinstance(frame, dict) and frame.get("type") == "hello" else None
    if not isinstance(tools, list) or not all(
        isinstance(item, str) and 0 < len(item) <= _MAX_TOOL_NAME_CHARS for item in tools
    ):
        return None
    return frozenset(tools)


async def serve_terminal(hub: TerminalHub, ws: WebSocket, name: str) -> None:
    """The brain's end of one terminal's socket, from accept to disconnect.

    ``name`` is the paired device the caller's token belongs to; the caller has already
    checked the token. A first frame that is not a hello within a few seconds closes the
    socket.
    """
    await ws.accept()
    try:
        tools = _hello_tools(await asyncio.wait_for(ws.receive_text(), HELLO_TIMEOUT_S))
    except (TimeoutError, WebSocketDisconnect):
        tools = None
    if tools is None:
        with contextlib.suppress(RuntimeError):
            await ws.close(code=1008)
        return
    link = hub.attach(name, tools, ws.send_text)
    try:
        await ws.send_text(json.dumps({"type": "ready", "device": name}))
        while True:
            text = await ws.receive_text()
            if len(text) > MAX_FRAME_CHARS:
                await ws.close(code=1009)
                return
            try:
                frame = json.loads(text)
            except ValueError:
                continue
            if isinstance(frame, dict) and frame.get("type") == "result":
                hub.deliver(link, frame)
    except WebSocketDisconnect:
        pass
    finally:
        hub.detach(link)


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


async def _serve_calls(ws: Any, execute: Execute) -> None:  # noqa: ANN401 — a websockets connection.
    """Answer calls until the socket closes; a call in flight is dropped with it."""
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
    finally:
        for task in running:
            task.cancel()


async def run_terminal_client(
    base_url: str, token: str, *, tools: frozenset[str], execute: Execute,
) -> None:
    """Hold a connection to the brain's ``/terminal/ws`` forever, answering its calls.

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
                additional_headers={"Authorization": f"Bearer {token}"},
            ) as ws:
                await ws.send(json.dumps({"type": "hello", "tools": sorted(tools)}))
                ready = json.loads(await asyncio.wait_for(ws.recv(), HELLO_TIMEOUT_S))
                if not isinstance(ready, dict) or ready.get("type") != "ready":
                    msg = "the brain did not accept this terminal"
                    raise OSError(msg)  # noqa: TRY301 — one place for "reconnect and log".
                LOGGER.info("connected to the brain as %s, runs %s", ready.get("device"),
                            sorted(tools))
                wait = _RECONNECT_FIRST_S
                await _serve_calls(ws, execute)
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
