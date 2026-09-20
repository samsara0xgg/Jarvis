"""Resident ``codex app-server`` and the JSON-RPC client that talks to it (ADR 0019).

Jarvis starts one ``codex app-server --listen unix://<socket>`` and keeps it
for the daemon's lifetime; every worker is a thread on that server. The
unix transport is a WebSocket (one JSON-RPC 2.0 message per text frame, no
compression, no auth — ``app-server-transport/src/transport/unix_socket.rs``),
so the client is ``websockets`` plus an id→reply map. Notifications and
server-initiated requests (approval prompts) are handed to callbacks on the
reader thread; the owner folds them into its own state and must not block.

The CLI is an external dependency that Allen keeps current; the version
found at start is logged so a wire change after an upgrade can be dated.
"""

from __future__ import annotations

import json
import logging
import queue
import re
import subprocess
import threading
import time
from typing import TYPE_CHECKING, Any

from websockets.sync.client import ClientConnection, unix_connect

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

LOGGER = logging.getLogger(__name__)

type Message = dict[str, Any]
type MessageCallback = Callable[[Message], None]


class CodexAppServerError(RuntimeError):
    """A JSON-RPC ``error`` reply, or the server going away mid-request."""

    def __init__(self, message: str, *, code: int | None = None, data: object = None) -> None:
        """Keep the wire ``code`` / ``data`` next to the message."""
        super().__init__(message)
        self.code = code
        self.data = data


def codex_version(codex_bin: str = "codex") -> str | None:
    """Return the installed CLI version (``codex --version`` → ``0.142.5``), or None."""
    try:
        out = subprocess.run(  # noqa: S603 — fixed argv, no shell.
            [codex_bin, "--version"], capture_output=True, text=True, timeout=15, check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"(\d+\.\d+\.\d+\S*)", out)
    return match.group(1) if match else None


class CodexAppServer:
    """One ``codex app-server`` child listening on a unix socket."""

    def __init__(
        self, socket_path: Path, *, codex_bin: str = "codex", log_path: Path | None = None,
    ) -> None:
        """``socket_path`` is the rendezvous path Codex advertises (it creates a symlink there)."""
        self.socket_path = socket_path
        self.codex_bin = codex_bin
        self.log_path = log_path or socket_path.with_suffix(".log")
        self._proc: subprocess.Popen[bytes] | None = None

    @property
    def running(self) -> bool:
        """True while the child process is alive."""
        return self._proc is not None and self._proc.poll() is None

    def start(self, *, timeout_s: float = 30.0) -> None:
        """Spawn the server and wait until its socket accepts connections."""
        if self.running:
            return
        version = codex_version(self.codex_bin)
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        with self.log_path.open("ab") as log:
            proc = subprocess.Popen(  # noqa: S603 — fixed argv, no shell.
                [self.codex_bin, "app-server", "--listen", f"unix://{self.socket_path}"],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            )
        self._proc = proc
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                msg = (
                    f"codex app-server exited with {proc.returncode} before listening; "
                    f"see {self.log_path}"
                )
                raise CodexAppServerError(msg)
            if self.socket_path.exists():
                LOGGER.info("codex app-server %s listening on %s", version, self.socket_path)
                return
            time.sleep(0.1)
        self.stop()
        msg = (
            f"codex app-server did not listen on {self.socket_path} within {timeout_s}s; "
            f"see {self.log_path}"
        )
        raise CodexAppServerError(msg)

    def stop(self, *, timeout_s: float = 5.0) -> None:
        """Terminate the child (SIGTERM, then SIGKILL after ``timeout_s``)."""
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        self._proc = None
        self.socket_path.unlink(missing_ok=True)


class CodexClient:
    """JSON-RPC 2.0 over the app-server's unix WebSocket; ``initialize`` is done in ``__init__``."""

    def __init__(
        self,
        socket_path: Path,
        *,
        client_name: str = "jarvis",
        on_notification: MessageCallback | None = None,
        on_server_request: MessageCallback | None = None,
    ) -> None:
        """Connect, start the reader thread and complete the ``initialize`` handshake."""
        self._ws: ClientConnection = unix_connect(
            str(socket_path), uri="ws://codex/", compression=None,
        )
        self._on_notification = on_notification
        self._on_server_request = on_server_request
        self._pending: dict[int, queue.Queue[Message]] = {}
        self._lock = threading.Lock()
        self._next_id = 0
        self._closed = False
        threading.Thread(target=self._read, name="codex-client-reader", daemon=True).start()
        self.request(
            "initialize",
            {"clientInfo": {"name": client_name, "version": "0"}, "capabilities": {}},
        )
        self.notify("initialized", {})

    def request(
        self, method: str, params: Mapping[str, Any], *, timeout_s: float = 30.0,
    ) -> Message:
        """Send a request and block for its reply; an ``error`` reply raises."""
        with self._lock:
            self._next_id += 1
            rid = self._next_id
            replies: queue.Queue[Message] = queue.Queue(maxsize=1)
            self._pending[rid] = replies
        self._send({"id": rid, "method": method, "params": dict(params)})
        try:
            reply = replies.get(timeout=timeout_s)
        except queue.Empty:
            with self._lock:
                self._pending.pop(rid, None)
            msg = f"codex app-server {method} timed out after {timeout_s}s"
            raise CodexAppServerError(msg) from None
        if "error" in reply:
            err = reply["error"] or {}
            msg = f"codex app-server {method}: {err.get('message', err)}"
            raise CodexAppServerError(msg, code=err.get("code"), data=err.get("data"))
        result: Message = reply.get("result") or {}
        return result

    def notify(self, method: str, params: Mapping[str, Any]) -> None:
        """Send a notification (no reply expected)."""
        self._send({"method": method, "params": dict(params)})

    def respond(self, request_id: object, result: Mapping[str, Any]) -> None:
        """Answer a server-initiated request (approval prompts)."""
        self._send({"id": request_id, "result": dict(result)})

    def close(self) -> None:
        """Close the socket; pending requests fail with :class:`CodexAppServerError`."""
        self._closed = True
        self._ws.close()

    def _send(self, message: Message) -> None:
        text = json.dumps(message)
        LOGGER.debug("codex >> %s", text)
        self._ws.send(text)

    def _read(self) -> None:
        try:
            for frame in self._ws:
                LOGGER.debug("codex << %s", frame)
                self._dispatch(json.loads(frame))
        except Exception:
            # The reader ends with the socket; pending callers see the error below.
            if not self._closed:
                LOGGER.exception("codex app-server connection lost")
        with self._lock:
            pending, self._pending = self._pending, {}
        for replies in pending.values():
            replies.put({"error": {"message": "connection closed"}})

    def _dispatch(self, message: Message) -> None:
        if "method" not in message:
            with self._lock:
                replies = self._pending.pop(message.get("id"), None)  # type: ignore[arg-type]
            if replies is not None:
                replies.put(message)
        elif "id" in message:
            if self._on_server_request is not None:
                self._on_server_request(message)
        elif self._on_notification is not None:
            self._on_notification(message)
