"""A fake Jev decisions endpoint for the surrogate route, and the Tier 0 table."""

from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision.tier0 import load_tier0_table
from tests.canary._helpers import repo_root
from tests.support.net import start_http_server, stop_http_server

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator

TABLE = load_tier0_table(repo_root() / "config" / "tier0_patterns.yaml")


LLM_ANSWER = "the model's own answer"


KEY = "test-key-not-a-secret"


class SurrogateJev:
    """A fake decisions endpoint: records every request, answers by ``mode``."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.mode = "ok"
        self.choice = "time"
        self.confidence = 0.95
        self.delay_s = 0.0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                size = int(self.headers["Content-Length"])
                outer.requests.append(json.loads(self.rfile.read(size)))
                outer.headers.append(dict(self.headers))
                time.sleep(outer.delay_s)
                if outer.mode == "trickle":
                    # Every read lands inside the phase timeout; the whole never ends in time.
                    self.send_response(200)
                    self.send_header("Content-Length", "100")
                    self.end_headers()
                    for _ in range(30):
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        time.sleep(0.1)
                    return
                if outer.mode in ("error", "no_route"):
                    self.send_response(500 if outer.mode == "error" else 404)
                    self.end_headers()
                    return
                body = (
                    b"not json"
                    if outer.mode == "garbage"
                    else json.dumps(
                        {
                            "answers": {
                                "route": {
                                    "choice": outer.choice,
                                    "confidence": outer.confidence,
                                }
                            },
                            "usage": {"cost": 0.0000123},
                        }
                    ).encode()
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                return

        self.server = start_http_server(Handler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/decisions"


@pytest.fixture
def jev(monkeypatch: pytest.MonkeyPatch) -> Iterator[SurrogateJev]:
    """A fake endpoint, and a key for the daemon to find."""
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    fake = SurrogateJev()
    yield fake
    stop_http_server(fake.server)


def logged_events(conn: sqlite3.Connection, kind: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT payload_json FROM events WHERE type = ? ORDER BY id", (kind,))
    return [json.loads(row[0]) for row in rows]


def ran_tier0(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [p for p in logged_events(conn, "action.proposed") if p.get("routed_by") == "tier_0"]
