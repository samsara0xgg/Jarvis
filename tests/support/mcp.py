"""The echo MCP server and an OAuth-protected Streamable HTTP server for one test."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.support.net import free_port

if TYPE_CHECKING:
    from collections.abc import Iterator

# The stand-in servers are scripts of their own, kept beside the integration tests.
SERVERS = Path(__file__).resolve().parents[1] / "integration"


ECHO = {"echo": {"command": sys.executable, "args": [str(SERVERS / "mcp_echo_server.py")]}}


def _wait_listening(port: int, timeout_s: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    msg = f"port {port} never came up"
    raise TimeoutError(msg)


@pytest.fixture
def oauth_url(request: pytest.FixtureRequest) -> Iterator[str]:
    """The OAuth-protected Streamable HTTP server, alive for one test."""
    port = free_port()
    proc = subprocess.Popen(  # noqa: S603 — our own test server script.
        [
            sys.executable,
            str(SERVERS / "mcp_oauth_server.py"),
            str(port),
            getattr(request, "param", "client_secret_post"),
        ]
    )
    try:
        _wait_listening(port)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        proc.terminate()
        proc.wait(timeout=10)
