"""A fake Gmail, Outlook and decisions endpoint, and the job-mail harness that polls them."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from jarvis.decision.attention import ContextPack, Judgement, rule_judge_v1
from jarvis.decision.surrogate_route import SurrogateRoute
from jarvis.deployment import bootstrap_runtime
from jarvis.execution.tools import ToolError
from jarvis.runtime.daemon.routes import job_mail_deps
from jarvis.runtime.job_mail import JobMail, JobMailSettings
from jarvis.shared import lang
from jarvis.state.event_log import open_event_log
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server.app import create_app
from jarvis.surface.inherent_server.deps import InherentDeps
from tests.support.clock import FakeClock
from tests.support.net import start_http_server, stop_http_server

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

NOW = datetime(2026, 9, 30, 17, 30, tzinfo=UTC)
"""The moment job mail believes it is in every test: a fixed instant, not the day the suite runs."""


OWNER_ZONE = ZoneInfo("America/Vancouver")
"""Allen's zone as job mail is given it: a time a letter gives no zone for is his clock there."""


SETTINGS = JobMailSettings(
    poll_s=300,
    backfill_days=14,
    max_messages_per_cycle=25,
    header_skip_at=0.9,
    body_min=0.5,
    max_body_chars=3000,
    max_calls_per_day=400,
    speak=True,
    speak_gap_s=600,
    linkedin_alerts="card_sound",  # the older behaviour; ledger_only has its own tests
    exclude_domains=(),  # nothing kept out; the exclusion has its own tests
)


AIRPODS = {"name": "AirPods Pro", "transport": "coreaudio_device_type_bluetooth", "private": True}


EVENT_SENTENCE = "We would like to invite you to an interview on March 4, 2027 at 2:00 PM PST."


def mail_row(
    key: str,
    sender: str,
    subject: str,
    body: str,
    *,
    age: timedelta = timedelta(hours=3),
) -> dict[str, Any]:
    return {
        "id": f"m-{key}",
        "threadId": f"t-{key}",
        "from": sender,
        "subject": subject,
        "date": format_datetime(NOW - age),
        "body": body,
    }


MAILS = [
    mail_row(
        "receipt",
        "CGI Careers <no-reply@cgi.com>",
        "Thank you for applying to CGI",
        "We have received your application for the Software Developer Co-op position.",
    ),
    mail_row(
        "interview",
        "Northwind Talent <recruiting@northwind.example>",
        "Interview invitation: Software Developer Co-op",
        f"Hi Allen,\nThanks for applying. {EVENT_SENTENCE} Please reply to confirm.",
    ),
    mail_row(
        "reject",
        "Orbital Careers <jobs@orbital.example>",
        "Update on your application",
        "Thank you for your interest. We will not be moving forward with your application.",
    ),
    mail_row(
        "offer",
        "Helix HR <hr@helix.example>",
        "Offer of employment",
        "We are delighted to offer you the Backend Developer Intern position.",
    ),
    mail_row(
        "digest",
        "LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>",
        "5 new jobs for Software Developer Intern in Victoria",
        "Jobs you may like: ...",
    ),
    mail_row(
        "followup",
        "Acme Recruiting <talent@acme.example>",
        "Following up",
        "Are you still looking?",
    ),
    mail_row(
        "promo", "ResumeBoost <deals@resumeboost.example>", "Boost your resume today!", "50% off"
    ),
    mail_row(
        "fair",
        "UVic Events <events@uvic.example>",
        "Career fair next week",
        "Come and meet employers.",
    ),
    mail_row("news", "Weekly Brew <hello@brew.example>", "Our weekly newsletter", "Coffee news."),
    mail_row("mom", "Mom <mom@example.com>", "Dinner on Sunday?", "Are you coming?"),
    mail_row(
        "old",
        "Zenith Careers <careers@zenith.example>",
        "Your application status",
        "We regret to inform you that the position has been filled.",
        age=timedelta(days=5),
    ),
]


# subject -> (header probabilities, body probabilities); a body entry only for mail that gets read.
JOB, MAYBE, NOT = "job_related", "maybe_job", "not_job"


def _odds(**given: float) -> dict[str, float]:
    return {
        name: given.get(name, 0.0)
        for name in ("offer", "interview", "rejection", "receipt", "job_other", NOT, JOB, MAYBE)
    }


ANSWERS: dict[str, tuple[dict[str, float], dict[str, float] | None]] = {
    "Thank you for applying to CGI": (_odds(job_related=0.99), _odds(receipt=0.97)),
    "Interview invitation: Software Developer Co-op": (
        _odds(job_related=0.99),
        _odds(interview=0.98),
    ),
    "Update on your application": (_odds(job_related=0.9), _odds(rejection=0.95)),
    "Offer of employment": (_odds(job_related=0.99), _odds(offer=0.99)),
    "5 new jobs for Software Developer Intern in Victoria": (
        _odds(maybe_job=0.6, not_job=0.2),
        _odds(job_other=0.8, not_job=0.1),
    ),
    "Following up": (_odds(maybe_job=0.7, not_job=0.1), _odds(interview=0.4, job_other=0.3)),
    "Boost your resume today!": (_odds(maybe_job=0.5, not_job=0.3), _odds(not_job=0.9)),
    "Career fair next week": (
        _odds(maybe_job=0.4, not_job=0.6),
        _odds(not_job=0.7, job_other=0.3),
    ),
    "Our weekly newsletter": (_odds(not_job=0.97), None),
    "Dinner on Sunday?": (_odds(not_job=0.95), None),
    "Your application status": (_odds(job_related=0.95), _odds(rejection=0.9)),
    # ADR 0158: Allen's real LinkedIn and CGI mail. Social news is held back before its body.
    "Byron Kontou recently posted": (
        _odds(maybe_job=0.7, not_job=0.1),
        _odds(not_job=0.6, job_other=0.3),
    ),
    "Software Engineer: RBC hired near you": (_odds(maybe_job=0.7, not_job=0.1), None),
    "Machine Learning Engineer II at TD": (
        _odds(job_related=0.8, not_job=0.1),
        _odds(job_other=0.8),
    ),
    "CrowdStrike is hiring for a Remote role": (
        _odds(job_related=0.8, not_job=0.1),
        _odds(job_other=0.8),
    ),
    "CGI - User Information": (_odds(job_related=0.8, not_job=0.1), _odds(job_other=0.8)),
    "Jane, interview for the Software Developer Co-op": (
        _odds(job_related=0.9),
        _odds(interview=0.9),
    ),
    "You appeared in 5 searches": (_odds(maybe_job=0.7, not_job=0.1), None),
    "Your profile was viewed 3 times": (_odds(maybe_job=0.7, not_job=0.1), None),
    # ADR 0177: LinkedIn's Easy Apply confirmation is an application, not kept-out mail.
    "Allen, your application was sent to Cambio Earth": (
        _odds(job_related=0.95),
        _odds(receipt=0.9),
    ),
}


class JobJev:
    """A fake decisions endpoint: a choice answer by the subject line, for either question."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        self.timeout_ms = 5000
        self.answering = threading.Event()
        self.answering.set()  # clear it and no reply ends until it is set again
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with outer._lock:
                    outer.requests.append(request)
                subject = request["state"].split("Subject: ", 1)[1].split("\n", 1)[0]
                (key,) = request["questions"]
                header, body = ANSWERS[subject]
                odds = header if key == "job" else body
                assert odds is not None, f"{subject!r} was not to be read in full"
                outer.answering.wait(timeout=30)  # a guard against a test that never sets it
                top = max(odds, key=lambda name: odds[name])
                answer = {
                    "type": "choice",
                    "choice": top,
                    "confidence": odds[top],
                    "probabilities": odds,
                }
                reply = json.dumps({"answers": {key: answer}, "usage": {"cost": 0.00002}}).encode()
                self.send_response(outer.status)
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                with contextlib.suppress(BrokenPipeError):
                    self.wfile.write(reply)

            def log_message(self, *_args: object) -> None:
                return

        self.server = start_http_server(Handler)

    def route(self, clock: FakeClock) -> SurrogateRoute:
        return SurrogateRoute(
            model="typesafe/jev-1.13",
            min_confidence=1.0,
            timeout_ms=self.timeout_ms,
            clock=clock,
            url=f"http://127.0.0.1:{self.server.server_address[1]}/decisions",
        )

    def asked(self, key: str) -> list[dict[str, Any]]:
        return [one for one in self.requests if key in one["questions"]]


class Gmail:
    """The connected Workspace server: JSON in a text block, as home.py reads it."""

    def __init__(self, mails: list[dict[str, Any]]) -> None:
        self.mails = mails
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.search_failures = 0
        self.search_error = "backend down"
        self.full_failures: set[str] = set()
        self.page_size: int | None = None  # None: one page holds every mail

    def call(self, server: str, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
        assert server == "gmail"
        self.calls.append((tool, dict(args)))
        if tool == "gmail_search":
            if self.search_failures:
                self.search_failures -= 1
                return {"text": json.dumps({"error": self.search_error})}
            start = int(args.get("pageToken") or 0)
            size = self.page_size or len(self.mails)
            hits = [
                {"id": one["id"], "threadId": one["threadId"]}
                for one in self.mails[start : start + size]
            ]
            reply: dict[str, Any] = {"messages": hits, "resultSizeEstimate": len(self.mails)}
            if start + size < len(self.mails):
                reply["nextPageToken"] = str(start + size)
            return {"text": json.dumps(reply)}
        assert tool == "gmail_get", f"job mail must only read: {tool}"
        if args["format"] == "full" and args["messageId"] in self.full_failures:
            return {"text": json.dumps({"error": "message body unavailable"})}
        letter = next(one for one in self.mails if one["id"] == args["messageId"])
        body = letter["body"] if args["format"] == "full" else ""
        return {"text": json.dumps({**letter, "snippet": "", "body": body})}

    def tools(self) -> set[str]:
        return {tool for tool, _args in self.calls}

    def searches(self) -> list[dict[str, Any]]:
        return [args for tool, args in self.calls if tool == "gmail_search"]

    def full_reads(self) -> set[str]:
        return {a["messageId"] for t, a in self.calls if t == "gmail_get" and a["format"] == "full"}

    def full_read_count(self, message_id: str) -> int:
        return sum(
            1
            for t, a in self.calls
            if t == "gmail_get" and a["format"] == "full" and a["messageId"] == message_id
        )


class Outlook:
    """The connected ``microsoft`` server: keeps every call made to it (ADR 0299 makes none)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, server: str, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
        assert server == "microsoft"
        self.calls.append((tool, json.loads(json.dumps(args))))
        return {"text": "{}"}


class Connections:
    def __init__(self, gmail: Gmail | None, outlook: Outlook | None = None) -> None:
        self.gmail, self.outlook = gmail, outlook

    def client_for(self, server: str) -> Any:  # noqa: ANN401 - either fake
        found = {"gmail": self.gmail, "microsoft": self.outlook}.get(server)
        if found is None:
            msg = f"mcp server {server!r} is not connected"
            raise ToolError(msg, code="mcp_server")
        return found


class JobHarness:
    def __init__(  # noqa: PLR0913 - the poller's fakes
        self,
        tmp_path: Path,
        jev: JobJev,
        gmail: Gmail,
        settings: JobMailSettings,
        judge: Any = rule_judge_v1,  # noqa: ANN401 - any Judge
        outlook: Outlook | None = None,
    ) -> None:
        self.db = tmp_path / "memory.db"
        self.jev, self.gmail, self.outlook = jev, gmail, outlook
        self.clock = FakeClock(NOW, zone=OWNER_ZONE)
        self.event_log = bootstrap_runtime(tmp_path / "runtime").event_log
        open_event_log(self.event_log).close()
        connections: Any = Connections(gmail, outlook)  # JobMail only asks it for client_for
        self.job = JobMail(
            settings,
            jev.route(self.clock),
            connections,
            self.db,
            judge,
            clock=self.clock,
            event_log=self.event_log,
        )
        self.quiet = "off"
        self.spoken = 0
        self.speech_ok = True
        self.device: dict[str, Any] = dict(AIRPODS)
        self.job.output = lambda fresh=False: dict(self.device)  # noqa: ARG005
        self.job.quiet = lambda: self.quiet
        self.job.may_speak = lambda: self.speech_ok
        self.job.say = self._say
        self.client = TestClient(
            create_app(
                InherentDeps(
                    submit_callable=lambda _text: None,
                    broadcaster=InherentBroadcaster(),
                    **job_mail_deps(self.job),
                )
            )
        )

    def _say(self) -> None:
        self.spoken += 1

    def notices(self) -> list[dict[str, Any]]:
        reply = self.client.get("/inherent/notices")
        assert reply.status_code == 200
        notices: list[dict[str, Any]] = reply.json()["notices"]
        return notices

    def flat(self) -> list[dict[str, Any]]:
        """The notices with a summary replaced by the alerts it stands for."""
        return [one for n in self.notices() for one in n.get("items", [n])]

    def ledger(self) -> list[dict[str, Any]]:
        ledger: list[dict[str, Any]] = self.client.get("/inherent/jobs").json()["ledger"]
        return ledger

    def sql(self, query: str, *args: object) -> list[tuple[Any, ...]]:
        conn = sqlite3.connect(self.db)
        try:
            with conn:
                return conn.execute(query, args).fetchall()
        finally:
            conn.close()

    def age_alerts(self, delta: timedelta) -> None:
        stamp = (NOW - delta).isoformat(timespec="seconds")
        self.sql("UPDATE job_alert SET created_at = ?", stamp)


@pytest.fixture(autouse=True)
def fixed_zh_words(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fixed words come from the language table; a key makes the poller run."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-a-secret")
    before = lang.language()
    lang.set_language("zh")
    yield
    lang.set_language(before)


@pytest.fixture
def jev() -> Iterator[JobJev]:
    """A fake Jev endpoint for the test's life."""
    fake = JobJev()
    yield fake
    fake.answering.set()
    stop_http_server(fake.server)


def job_harness(
    tmp_path: Path,
    jev: JobJev,
    mails: list[dict[str, Any]] | None = None,
    judge: Any = rule_judge_v1,  # noqa: ANN401 - any Judge
    outlook: Outlook | None = None,
    **settings: Any,  # noqa: ANN401
) -> JobHarness:
    fields = {**SETTINGS.__dict__, **settings}
    gmail = Gmail(MAILS if mails is None else mails)
    return JobHarness(tmp_path, jev, gmail, JobMailSettings(**fields), judge, outlook)


def glow_for_rejections(pack: ContextPack) -> Judgement:
    """The rule table, except that a fresh rejection is a glow."""
    judged = rule_judge_v1(pack)
    if pack.facts.get("kind") == "rejection" and judged.level == "card":
        return Judgement("glow", "test: a rejection is a glow", "glow_test", "0")
    return judged


def only_rows(*ids: str) -> list[dict[str, Any]]:
    return [m for m in MAILS if m["id"] in ids]
