"""Local sockets for tests: a free port, and an HTTP server that stops at once.

``ThreadingHTTPServer.serve_forever`` looks for a shutdown request every half second by
default, so each ``shutdown()`` waited up to 0.5 s for the loop to notice. A fixture that stops
a fake endpoint after every test paid that on every test; ``start_http_server`` polls every
10 ms instead.
"""

from __future__ import annotations

import socket
import threading
from http.server import ThreadingHTTPServer
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from http.server import BaseHTTPRequestHandler

POLL_INTERVAL_S = 0.01


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
