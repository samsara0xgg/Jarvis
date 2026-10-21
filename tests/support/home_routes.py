"""Fake connected servers (Graph, Gmail), their connections and a Jev decisions endpoint."""

from __future__ import annotations

import contextlib
import json
import threading
from http.server import BaseHTTPRequestHandler
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from jarvis.decision.mail_reply import MailReply
from jarvis.decision.surrogate_route import JevLog, SurrogateRoute
from jarvis.execution.tools import ToolError
from jarvis.runtime.home import Home
from tests.support.clock import FakeClock
from tests.support.net import start_http_server, stop_http_server

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from jarvis.runtime.weatherkit import WeatherKit

ZONE = "America/Vancouver"

_GUARD_S = 5.0
"""How long the endpoint holds an answer for the test before it gives up; not a time it keeps."""


LIST = {"displayName": "Tasks", "wellknownListName": "defaultList", "id": "AQMkADAw-list="}


TASKS = [
    {"status": "notStarted", "title": "repack jarvis", "id": "AQMkADAw-repack="},
    {"status": "completed", "title": "old one", "id": "AQMkADAw-old="},
    {
        "status": "notStarted",
        "title": "CSC370 A3",
        "id": "AQMkADAw-a3=",
        "dueDateTime": {"dateTime": "2026-09-26T00:00:00.0000000", "timeZone": "UTC"},
    },
]


# gmail_get's metadata answers; gmail_search lists them in this order, which is not
# newest first, so the route's own sort by Date shows.
LETTERS: list[dict[str, Any]] = [
    {
        "id": "199a1c0d4101",
        "threadId": "199a1c0d4101",
        "labelIds": ["UNREAD", "INBOX"],
        "subject": "Office hours move to Thursday",
        "from": '"Prof. Lee" <lee@uvic.ca>',
        "to": "allen@example.com",
        "date": "Thu, 25 Sep 2026 14:40:00 -0700",
        "body": "<div>Hi Allen,<br>Thursday 3pm.</div>",
    },
    {
        "id": "199a1c0d4102",
        "threadId": "199a1c0d4102",
        "labelIds": ["UNREAD", "INBOX"],
        "subject": "Your weekly digest",
        "from": "GitHub <noreply@github.com>",
        "to": "allen@example.com",
        "date": "Thu, 25 Sep 2026 13:00:00 -0700",
    },
    {
        "id": "199a1c0d4103",
        "threadId": "199a1c0d4103",
        "labelIds": ["UNREAD", "INBOX"],
        "subject": "今晚还来吗？",  # noqa: RUF001 — her words.
        "from": "妈妈 <mom@example.com>",
        "to": "allen@example.com",
        "date": "Thu, 25 Sep 2026 15:05:00 -0700",
    },
    {
        "id": "199a1c0d4104",
        "threadId": "199a1c0d4104",
        "labelIds": ["UNREAD", "INBOX"],
        "subject": "Your receipt",
        "from": "billing@shop.example",  # no display name
        "to": "allen@example.com",
        "date": "Thu, 25 Sep 2026 12:00:00 -0700",
    },
    {
        "id": "199a1c0d4105",
        "threadId": "199a1c0d4105",
        "labelIds": ["UNREAD", "INBOX"],
        "subject": "50% off everything this weekend",
        "from": "Shop Deals <deals@shop.example>",
        "to": "allen@example.com",
        "date": "Thu, 25 Sep 2026 11:00:00 -0700",
    },
    {
        "id": "199a1c0d4106",
        "threadId": "199a1c0d4106",
        "labelIds": ["UNREAD", "INBOX"],
        "subject": "Our weekly newsletter",
        "from": "Weekly Brew <hello@brew.example>",
        "to": "allen@example.com",
        "date": "Thu, 25 Sep 2026 10:00:00 -0700",
    },
    {
        "id": "199a1c0d4107",
        "threadId": "199a1c0d4107",
        "labelIds": ["UNREAD", "INBOX"],
        "subject": "Quick question about your listing",
        "from": "Sam <sam@example.com>",
        "to": "allen@example.com",
        "date": "Thu, 25 Sep 2026 09:00:00 -0700",
    },
]


# The calendar is empty on the real account: the mirror reads no event, so Today's events are
# the ones a test writes to the ledger.
ANSWERS = {
    "list-todo-task-lists": [LIST],
    "list-todo-tasks": TASKS,
}


class Microsoft:
    """The connected server: Graph's JSON as a text block, like ms-365-mcp-server answers."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, server: str, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
        assert server == "microsoft"
        self.calls.append((tool, dict(args)))
        return {"text": json.dumps({"value": ANSWERS.get(tool, [])})}


class Gmail:
    """The connected Workspace server: JSON in a text block, a failure as {"error": ...}."""

    def __init__(self, error: str | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, server: str, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
        assert server == "gmail"
        self.calls.append((tool, dict(args)))
        if self.error:
            return {"text": json.dumps({"error": self.error})}
        if tool == "gmail_search":
            hits = [{"id": one["id"], "threadId": one["threadId"]} for one in LETTERS]
            return {"text": json.dumps({"messages": hits, "resultSizeEstimate": len(hits)})}
        if tool == "gmail_batchModify":
            body = {"modifiedCount": len(args["messageIds"]), "status": "success"}
            return {"text": json.dumps(body)}
        letter = next((one for one in LETTERS if one["id"] == args["messageId"]), None)
        if letter is None:
            return {"text": json.dumps({"error": "Requested entity was not found."})}
        body = letter.get("body", "")
        return {"text": json.dumps({**letter, "snippet": "", "body": body, "attachments": []})}


class Connections:
    def __init__(self, server: Microsoft | None, gmail: Gmail | None = None) -> None:
        self.servers = {"microsoft": server, "gmail": gmail}

    def client_for(self, server: str) -> Microsoft | Gmail:
        found = self.servers.get(server)
        if found is None:
            msg = f"mcp server {server!r} is not connected"
            raise ToolError(msg, code="mcp_server")
        return found


LOCAL = ZoneInfo(ZONE)


def make_home(
    server: Microsoft | None,
    gmail: Gmail | None = None,
    kit: WeatherKit | None = None,
) -> Home:
    return Home(
        Connections(server, gmail),  # type: ignore[arg-type]
        (ZONE, LOCAL),
        {"latitude": 49.28, "longitude": -123.12, "country_code": "CA"},
        weatherkit=kit,
    )


class HomeJev:
    """A fake decisions endpoint (ADR 0123): answers P(needs a reply) by the subject line.

    It keeps its time on :attr:`clock`, which the routes it builds take. ``hold`` keeps every
    answer back until ``release`` is set, so a test can let a deadline pass on the clock while
    Jev is still working.
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self._arrived = threading.Condition()
        self.clock = FakeClock()
        # subject -> (P(needs a reply), P(junk))
        self.odds = {
            "Office hours move to Thursday": (0.97, 0.02),
            "今晚还来吗？": (0.04, 0.01),  # noqa: RUF001 — her words.
            "Your receipt": (0.5, 0.5),
            "50% off everything this weekend": (0.03, 0.95),
            "Our weekly newsletter": (0.05, 0.89),  # just under the junk bar
            "Quick question about your listing": (0.95, 0.99),  # junk-looking, but may need a reply
        }
        # subject -> probabilities of the importance choice; unlisted subjects are "normal"
        self.importance = {
            "Office hours move to Thursday": {
                "urgent": 0.5,
                "important": 0.3,
                "normal": 0.1,
                "low": 0.1,
            },
            "50% off everything this weekend": {
                "urgent": 0.0,
                "important": 0.0,
                "normal": 0.1,
                "low": 0.9,
            },
        }
        self.category = "school"
        self.status = 200
        self.hold = False
        self.release = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with outer._arrived:
                    outer.requests.append(request)
                    outer._arrived.notify_all()
                if outer.hold:
                    outer.release.wait(_GUARD_S)
                subject = request["state"].split("Subject: ", 1)[1]
                reply, junk = outer.odds[subject]
                answers: dict[str, Any] = {"reply": {"noul": reply}, "junk": {"noul": junk}}
                if "importance" in request["questions"]:
                    answers["importance"] = {
                        "type": "choice",
                        "choice": "normal",
                        "confidence": 0.5,
                        "probabilities": outer.importance.get(
                            subject,
                            {"urgent": 0.0, "important": 0.0, "normal": 1.0, "low": 0.0},
                        ),
                    }
                    answers["category"] = {
                        "type": "choice",
                        "choice": outer.category,
                        "confidence": 0.9,
                        "probabilities": {outer.category: 0.9},
                    }
                body = json.dumps({"answers": answers, "usage": {"cost": 0.00001}}).encode()
                self.send_response(outer.status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                with contextlib.suppress(BrokenPipeError):  # the client gave up on a slow call
                    self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                return

        self.server = start_http_server(Handler)

    def reply(
        self,
        *,
        timeout_ms: int = 1500,
        log: JevLog | None = None,
        importance: bool = False,
    ) -> MailReply:
        route = SurrogateRoute(
            model="typesafe/jev-1.13",
            min_confidence=1.0,
            timeout_ms=timeout_ms,
            clock=self.clock,
            url=f"http://127.0.0.1:{self.server.server_address[1]}/decisions",
            log=log,
        )
        return MailReply(route, 0.9, 0.1, 0.9, importance=importance)

    def arrived(self, count: int, timeout_s: float = 5.0) -> list[dict[str, Any]]:
        """The requests once ``count`` have arrived: a caller that gave up can be back first."""
        with self._arrived:
            self._arrived.wait_for(lambda: len(self.requests) >= count, timeout_s)
            return list(self.requests)


@pytest.fixture
def jev(monkeypatch: pytest.MonkeyPatch) -> Iterator[HomeJev]:
    """A fake endpoint, and a key for the daemon to find."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-a-secret")
    fake = HomeJev()
    yield fake
    fake.release.set()
    stop_http_server(fake.server)
