"""The daemon's uvicorn server: its stop signal, and the sockets of a brain that listens."""

from __future__ import annotations

import contextlib
import signal
import socket
import threading
from typing import TYPE_CHECKING

import uvicorn

if TYPE_CHECKING:
    from collections.abc import Generator


class Server(uvicorn.Server):
    """uvicorn whose stop signal ends ``serve`` and leaves the process to ``serve_inherent``.

    uvicorn re-raises the signal once it has shut down: SIGTERM then kills the process and
    SIGINT cancels the main task, both before the daemon's teardown has run.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        """Take SIGINT and SIGTERM for ``handle_exit`` while serving, and give them back."""
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        stops = (signal.SIGINT, signal.SIGTERM)
        before = {sig: signal.signal(sig, self.handle_exit) for sig in stops}
        try:
            yield
        finally:
            for sig, handler in before.items():
                signal.signal(sig, handler)


def listen_sockets(host: str, port: int, addresses: tuple[str, ...]) -> list[socket.socket]:
    """Listening sockets for ``host`` and each private address (ADR 0170).

    uvicorn binds one host, so a brain with more addresses hands it the sockets.
    """
    sockets: list[socket.socket] = []
    for address in (host, *addresses):
        try:
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            sockets.append(socket.create_server((address, port), family=family, backlog=2048))
        except OSError as exc:
            for sock in sockets:
                sock.close()
            msg = f"cannot listen on {address}:{port}: {exc}"
            raise OSError(msg) from exc
    return sockets
