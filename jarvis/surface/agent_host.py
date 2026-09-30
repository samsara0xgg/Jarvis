"""The agent host's session list, read by the daemon (ADR 0073, ADR 0093).

The host answers the daemon's own key. ``GET /events`` opens with one
``hello`` event carrying every session row; the read takes that line and
closes. Opening the stream also makes the host read the daemon's session
marks again, which it does on every new window anyway.
"""

from __future__ import annotations

import http.client
import json
import logging
from http import HTTPStatus
from typing import Any, Final

LOGGER = logging.getLogger(__name__)

_HELLO_BYTES: Final = 8 * 1024 * 1024


def _first_data(response: http.client.HTTPResponse) -> bytes:
    """The first ``data:`` line of an event stream, past blank and comment lines; b"" at its end."""
    while line := response.readline(_HELLO_BYTES):
        if line.startswith(b"data: "):
            return line[len(b"data: "):]
        if line.strip() and not line.startswith(b":"):
            break
    return b""


def host_sessions(port: int, key: str, *, timeout: float = 3.0) -> list[dict[str, Any]] | None:
    """Every session row the host lists; None when it does not answer in ``timeout``."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", "/events", headers={"Authorization": f"Bearer {key}"})
        response = conn.getresponse()
        if response.status != HTTPStatus.OK:
            LOGGER.info("agent host: /events answered %s", response.status)
            return None
        event = json.loads(_first_data(response) or b"null")
    except (OSError, http.client.HTTPException, ValueError):
        return None
    finally:
        conn.close()
    hello = isinstance(event, dict) and event.get("t") == "hello"
    sessions = event.get("sessions") if hello else None
    if not isinstance(sessions, list):
        return None
    return [row for row in sessions if isinstance(row, dict)]


__all__ = ["host_sessions"]
