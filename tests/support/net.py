"""Local sockets for tests: a free port, and an HTTP server that stops at once.

``ThreadingHTTPServer.serve_forever`` looks for a shutdown request every half second by
default, so each ``shutdown()`` waited up to 0.5 s for the loop to notice. A fixture that stops
a fake endpoint after every test paid that on every test; ``start_http_server`` polls every
10 ms instead.
"""

from __future__ import annotations

import contextlib
import socket
import threading
from http.server import ThreadingHTTPServer
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from http.server import BaseHTTPRequestHandler

POLL_INTERVAL_S = 0.01
BEAT_S = 0.02
"""How often a held reply writes a byte; far inside any phase timeout a route sets."""


def free_port() -> int:
    """A port on 127.0.0.1 that was free a moment ago."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def start_http_server(
    handler: Callable[..., BaseHTTPRequestHandler],
) -> ThreadingHTTPServer:
    """Serve ``handler`` on a free 127.0.0.1 port from a daemon thread, until ``stop_http_server``.

    The port is ``server.server_port``.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": POLL_INTERVAL_S},
        daemon=True,
    ).start()
    return server


def stop_http_server(server: ThreadingHTTPServer) -> None:
    """Stop serving and release the port; returns within a poll interval."""
    server.shutdown()
    server.server_close()


@contextlib.contextmanager
def reply_stays_alive(handler: BaseHTTPRequestHandler, status: int = 200) -> Iterator[None]:
    """Answer ``status`` now and keep the reply from going quiet while its body is held back.

    httpx bounds each read in real time, and a route sets that bound to its deadline in seconds.
    A reply held back in silence therefore ends on that real timer whenever the caller is slow
    to wait on the fake clock, and the caller finds a timeout stamped at whatever the fake clock
    says by then. A space every :data:`BEAT_S` (JSON allows them before a value) keeps each read
    inside its phase, so only the fake clock can end the call. After the block the handler writes
    the body alone, with no ``Content-Length``: the reply ends when the connection closes.
    """
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.end_headers()
    handler.wfile.flush()
    stop = threading.Event()

    def beat() -> None:
        with contextlib.suppress(OSError):  # the client has given up on the call
            while not stop.wait(BEAT_S):
                handler.wfile.write(b" ")
                handler.wfile.flush()

    beating = threading.Thread(target=beat, name="test-reply-beat", daemon=True)
    beating.start()
    try:
        yield
    finally:
        stop.set()
        beating.join()
