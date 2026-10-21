"""A fake Jev decisions endpoint for the surrogate route, and the Tier 0 table.

The endpoint keeps its time on a :class:`~tests.support.clock.FakeClock`, which every route a
test builds against it takes as its own. Taking time moves that clock instead of sleeping, so a
slow endpoint costs a test none of it and a deadline passes exactly when the endpoint says.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision.tier0 import load_tier0_table
from tests.canary._helpers import repo_root
from tests.support.clock import FakeClock
from tests.support.net import reply_stays_alive, start_http_server, stop_http_server

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterator

TABLE = load_tier0_table(repo_root() / "config" / "tier0_patterns.yaml")


LLM_ANSWER = "the model's own answer"


KEY = "test-key-not-a-secret"

_GUARD_S = 5.0
"""How long the endpoint waits for the test before it gives up; not a time it keeps."""


class SurrogateJev:
    """A fake decisions endpoint: records every request, answers by ``mode``.

    Before it answers it can wait for a moment the test names, then take ``delay_s`` of
    :attr:`clock` time:

    - ``gate``: an event that must be set first (the model's request has reached its peer);
    - ``caller_waits``: the caller is waiting for this answer on the clock;
    - ``caller_gives_up``: ... and has stopped waiting, its deadline passed (needs the delay to
      pass it). The answer is then late for certain, however fast the sockets are;
    - ``on_answered``: called once the answer is written.
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.mode = "ok"
        self.choice = "time"
        self.confidence = 0.95
        self.clock = FakeClock()
        self.delay_s = 0.0
        self.gate: threading.Event | None = None
        self.caller_waits = False
        self.caller_gives_up = False
        self.on_answered: Callable[[], None] | None = None
        self.released = threading.Event()
        self._arrived = threading.Condition()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                size = int(self.headers["Content-Length"])
                body = json.loads(self.rfile.read(size))
                with outer._arrived:
                    outer.requests.append(body)
                    outer.headers.append(dict(self.headers))
                    outer._arrived.notify_all()
                if outer.mode in ("error", "no_route"):
                    outer.take_its_time()
                    self.send_response(500 if outer.mode == "error" else 404)
                    self.end_headers()
                    return
                with reply_stays_alive(self):
                    outer.take_its_time()
                    if outer.mode == "trickle":
                        outer.trickle()
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
                try:
                    self.wfile.write(body)
                except OSError:  # the client gave up on a slow call
                    return
                if outer.on_answered is not None:
                    outer.on_answered()

            def log_message(self, *_args: object) -> None:
                return

        self.server = start_http_server(Handler)

    def take_its_time(self) -> None:
        """Wait for what the test named, then let ``delay_s`` of the clock pass."""
        if self.gate is not None:
            assert self.gate.wait(_GUARD_S), "the gate never opened"
        if self.caller_waits or self.caller_gives_up:
            self.clock.wait_until_parked()
        if self.delay_s:
            self.clock.advance(self.delay_s)
        if self.caller_gives_up:
            self.clock.wait_until(lambda: self.clock.parked == 0, "the caller to give up")

    def trickle(self) -> None:
        """A reply that never ends and never goes quiet; the caller's deadline is the only end.

        Once the caller waits on the clock, the 3 s the reply would have taken pass on it. Nothing
        else moves the clock, so the call is cut at its own deadline however late the caller came.
        """
        self.clock.wait_until(lambda: self.clock.blocked >= 1, "the caller to wait on the clock")
        self.clock.advance(3.0)
        self.released.wait(_GUARD_S)

    def arrived(self, count: int, timeout_s: float = 5.0) -> list[dict[str, Any]]:
        """The requests once ``count`` have arrived.

        A caller that gave up on its deadline can return before the handler thread has read
        the request, so a check after a timeout waits for the arrival instead of racing it.
        """
        with self._arrived:
            self._arrived.wait_for(lambda: len(self.requests) >= count, timeout_s)
            return list(self.requests)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/decisions"


@pytest.fixture
def jev(monkeypatch: pytest.MonkeyPatch) -> Iterator[SurrogateJev]:
    """A fake endpoint, and a key for the daemon to find."""
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    fake = SurrogateJev()
    yield fake
    fake.released.set()
    stop_http_server(fake.server)


def logged_events(conn: sqlite3.Connection, kind: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT payload_json FROM events WHERE type = ? ORDER BY id", (kind,))
    return [json.loads(row[0]) for row in rows]


def ran_tier0(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [p for p in logged_events(conn, "action.proposed") if p.get("routed_by") == "tier_0"]
