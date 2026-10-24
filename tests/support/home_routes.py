"""Fake connected servers (Graph, Gmail), their connections and a Jev decisions endpoint."""

from __future__ import annotations

import contextlib
import json
import threading
from datetime import timedelta
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from jarvis.decision.mail_reply import MailReply
from jarvis.decision.surrogate_route import JevLog, SurrogateRoute
from jarvis.execution.tools import ToolError
from jarvis.runtime.home import Home
from tests.support import gmail_fake
from tests.support.clock import FakeClock
from tests.support.gmail_fake import PRIMARY, SENT
from tests.support.net import reply_stays_alive, start_http_server, stop_http_server

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


# His unread Primary mail, as Gmail's metadata answers carry it; gmail_search lists them in this
# order, which is not newest first, so the route's own sort by Date shows. ``headers`` are the
# ones that mark a message bulk or automated (ADR 0364); the others are a person's letters.
LETTERS: list[dict[str, Any]] = [
    {
        "id": "199a1c0d4101",
        "threadId": "199a1c0d4101",
        "subject": "Office hours move to Thursday",
        "from": '"Prof. Lee" <lee@uvic.ca>',
        "to": "allen@example.com",
        "date": "Thu, 25 Sep 2026 14:40:00 -0700",
        "body": "<div>Hi Allen,<br>Thursday 3pm.</div>",
    },
    {
        "id": "199a1c0d4102",
        "threadId": "199a1c0d4102",
        "subject": "Your weekly digest",
        "from": "GitHub <noreply@github.com>",
        "to": "allen@example.com",
        "date": "Thu, 25 Sep 2026 13:00:00 -0700",
        "headers": {"Auto-Submitted": "auto-generated"},
    },
    {
        "id": "199a1c0d4103",
        "threadId": "199a1c0d4103",
        "subject": "今晚还来吗？",  # noqa: RUF001 — her words.
        "from": "妈妈 <mom@example.com>",
        "to": "allen@example.com",
        "date": "Thu, 25 Sep 2026 15:05:00 -0700",
    },
    {
        "id": "199a1c0d4104",
        "threadId": "199a1c0d4104",
        "subject": "Your receipt",
        "from": "billing@shop.example",  # no display name
        "to": "allen@example.com",
        "date": "Thu, 25 Sep 2026 12:00:00 -0700",
        "headers": {"Precedence": "bulk"},
    },
    {
        "id": "199a1c0d4105",
        "threadId": "199a1c0d4105",
        "subject": "50% off everything this weekend",
        "from": "Shop Deals <deals@shop.example>",
        "to": "allen@example.com",
        "date": "Thu, 25 Sep 2026 11:00:00 -0700",
        "headers": {"List-Unsubscribe": "<mailto:stop@shop.example>"},
    },
    {
        "id": "199a1c0d4106",
        "threadId": "199a1c0d4106",
        "subject": "Our weekly newsletter",
        "from": "Weekly Brew <hello@brew.example>",
        "to": "allen@example.com",
        "date": "Thu, 25 Sep 2026 10:00:00 -0700",
        "headers": {"List-Id": "<news.brew.example>"},
    },
    {
        "id": "199a1c0d4107",
        "threadId": "199a1c0d4107",
        "subject": "Quick question about your listing",
        "from": "Sam <sam@example.com>",
        "to": "allen@example.com",
        "date": "Thu, 25 Sep 2026 09:00:00 -0700",
    },
]

ME = "Allen <allen@example.com>"
ANSWERED = "199a1c0d4103"
"""The letter whose conversation he answered last: his reply is in his Sent mail."""
READ = "199a1c0d9999"
"""A letter in the inbox he has read: it is not listed, but a tap may act on it once it is open."""


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


class Gmail(gmail_fake.Gmail):
    """The connected Workspace server over his invented mailbox (``tests.support.gmail_fake``).

    It holds the unread Primary :data:`LETTERS`, his reply to the one named by :data:`ANSWERED`
    (so he wrote last there) and one read letter in the inbox. A failure answers as
    ``{"error": ...}``.
    """

    def __init__(self, error: str | None = None) -> None:
        super().__init__(error=error)
        for one in LETTERS:
            self.add(
                one["id"], one["subject"], sender=one["from"], to=one["to"],
                at=parsedate_to_datetime(one["date"]), thread=one["threadId"], labels=PRIMARY,
                html=one.get("body"), headers=one.get("headers"),
            )
        answered = self.messages[ANSWERED]
        self.add(
            "199a1c0d4110", f"Re: {answered.message()['Subject']}", sender=ME,
            to=str(answered.message()["From"]), at=answered.at + timedelta(minutes=30),
            thread=answered.thread, labels=SENT,
        )
        self.add(READ, "Lunch", sender="Sam Lee <sam@example.com>", to=ME, labels=("INBOX",))


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
    """A fake decisions endpoint (ADR 0124): answers P(junk) by the subject line.

    It keeps its time on :attr:`clock`, which the routes it builds take. ``hold`` keeps every
    answer back until ``release`` is set, so a test can let a deadline pass on the clock while
    Jev is still working.
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self._arrived = threading.Condition()
        self.clock = FakeClock()
        # subject -> P(junk)
        self.odds = {
            "Office hours move to Thursday": 0.02,
            "Your weekly digest": 0.4,
            "今晚还来吗？": 0.01,  # noqa: RUF001 — her words.
            "Your receipt": 0.5,
            "50% off everything this weekend": 0.95,
            "Our weekly newsletter": 0.89,  # just under the junk bar
            "Quick question about your listing": 0.99,  # junk-looking, but it waits on him
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
                held = outer.hold  # a test may end the hold while this call is in it
                if held:
                    with reply_stays_alive(self, outer.status):
                        outer.release.wait(_GUARD_S)
                subject = request["state"].split("Subject: ", 1)[1]
                answers: dict[str, Any] = {"junk": {"noul": outer.odds[subject]}}
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
                if not held:  # a held reply was started, and its status sent, while it waited
                    self.send_response(outer.status)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                with contextlib.suppress(OSError):  # the client gave up on a slow call
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
        return MailReply(route, 0.9, importance=importance)

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
