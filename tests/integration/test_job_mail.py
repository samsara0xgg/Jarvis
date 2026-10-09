"""ADR 0155 — the job-mail poller, ledger and routes over a fake Gmail server and a fake Jev.

The real poller (``JobMail.poll_once`` and ``run``) reads canned mail through a stand-in for the
shared ``gmail`` MCP client and asks a real ``SurrogateRoute`` pointed at a local HTTP server that
answers by subject. Each check asserts what the ledger holds, which alerts the routes serve at
which quiet level, what Jev was sent, or which Gmail tools were called.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
import threading
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest
import yaml
from fastapi.testclient import TestClient

from jarvis.decision import job_mail as triage
from jarvis.decision.attention import (
    CARD_LEVELS,
    LEVELS,
    ContextPack,
    Judgement,
    replay,
    rule_judge_v1,
)
from jarvis.decision.surrogate_route import SurrogateRoute
from jarvis.deployment import bootstrap_runtime
from jarvis.execution.tools import ToolError
from jarvis.runtime import RuntimeBootstrapError, _job_mail
from jarvis.runtime.inherent_loop import _job_mail_deps, _say_job_line
from jarvis.runtime.interview_reminders import InterviewSettings, outlook_write
from jarvis.runtime.job_mail import JobMail, JobMailSettings, gmail_read, repair
from jarvis.shared import lang
from jarvis.state import job_ledger
from jarvis.state import reminders as reminder_state
from jarvis.state.event_log import open_event_log, open_runtime_event_log
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app
from tests.canary._helpers import repo_root
from tests.integration.test_lifecycle_commentary import _make_runtime, _only_phrase

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

NOW = datetime.now(UTC)
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
SPEAKERS = {
    "name": "MacBook Pro Speakers",
    "transport": "coreaudio_device_type_builtin",
    "private": False,
}
EVENT_SENTENCE = "We would like to invite you to an interview on March 4, 2027 at 2:00 PM PST."


def _mail(
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
    _mail(
        "receipt",
        "CGI Careers <no-reply@cgi.com>",
        "Thank you for applying to CGI",
        "We have received your application for the Software Developer Co-op position.",
    ),
    _mail(
        "interview",
        "Northwind Talent <recruiting@northwind.example>",
        "Interview invitation: Software Developer Co-op",
        f"Hi Allen,\nThanks for applying. {EVENT_SENTENCE} Please reply to confirm.",
    ),
    _mail(
        "reject",
        "Orbital Careers <jobs@orbital.example>",
        "Update on your application",
        "Thank you for your interest. We will not be moving forward with your application.",
    ),
    _mail(
        "offer",
        "Helix HR <hr@helix.example>",
        "Offer of employment",
        "We are delighted to offer you the Backend Developer Intern position.",
    ),
    _mail(
        "digest",
        "LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>",
        "5 new jobs for Software Developer Intern in Victoria",
        "Jobs you may like: ...",
    ),
    _mail(
        "followup",
        "Acme Recruiting <talent@acme.example>",
        "Following up",
        "Are you still looking?",
    ),
    _mail(
        "promo", "ResumeBoost <deals@resumeboost.example>", "Boost your resume today!", "50% off"
    ),
    _mail(
        "fair",
        "UVic Events <events@uvic.example>",
        "Career fair next week",
        "Come and meet employers.",
    ),
    _mail("news", "Weekly Brew <hello@brew.example>", "Our weekly newsletter", "Coffee news."),
    _mail("mom", "Mom <mom@example.com>", "Dinner on Sunday?", "Are you coming?"),
    _mail(
        "old",
        "Zenith Careers <careers@zenith.example>",
        "Your application status",
        "We regret to inform you that the position has been filled.",
        age=timedelta(days=5),
    ),
]
LINKEDIN = [
    _mail("post", "LinkedIn <messages-noreply@linkedin.com>", "Byron Kontou recently posted", "Hi"),
    _mail(
        "near",
        "LinkedIn <notifications-noreply@linkedin.com>",
        "Software Engineer: RBC hired near you",
        "Hi",
    ),
    _mail(
        "alert",
        "LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>",
        "Machine Learning Engineer II at TD",
        "New jobs",
    ),
    _mail(
        "hiring",
        "LinkedIn <jobs-noreply@linkedin.com>",
        "CrowdStrike is hiring for a Remote role",
        "New jobs",
    ),
    _mail("account", "CGI <no-reply@njoyn.com>", "CGI - User Information", "Your account"),
    _mail(
        "inmail",
        "Jane Recruiter <inmail-hit-reply@linkedin.com>",
        "Jane, interview for the Software Developer Co-op",
        "Can we talk Tuesday?",
    ),
]
# ADR 0164: LinkedIn profile-activity notices are held back like social news; an interview InMail
# that mentions the profile in passing and a "Job Alerts" digest are not.
PROFILE = [
    _mail(
        "searches",
        "LinkedIn <notifications-noreply@linkedin.com>",
        "You appeared in 5 searches",
        "Hi",
    ),
    _mail(
        "views",
        "LinkedIn <notifications-noreply@linkedin.com>",
        "Your profile was viewed 3 times",
        "Hi",
    ),
    LINKEDIN[2],
    LINKEDIN[5],
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


class _Jev:
    """A fake decisions endpoint: a choice answer by the subject line, for either question."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 200
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

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def route(self) -> SurrogateRoute:
        return SurrogateRoute(
            model="typesafe/jev-1.13",
            min_confidence=1.0,
            timeout_ms=5000,
            url=f"http://127.0.0.1:{self.server.server_address[1]}/decisions",
        )

    def asked(self, key: str) -> list[dict[str, Any]]:
        return [one for one in self.requests if key in one["questions"]]


class _Gmail:
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


class _Outlook:
    """The connected ``microsoft`` server: keeps the events written to it, can fail on demand."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.events: dict[str, dict[str, Any]] = {}
        self.failures = 0
        self.gone = False  # answer a delete as Graph does for an event already removed

    def call(self, server: str, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
        assert server == "microsoft"
        self.calls.append((tool, json.loads(json.dumps(args))))
        if self.failures:
            self.failures -= 1
            msg = "microsoft: Graph is down"
            raise ToolError(msg, code="mcp_server")
        if tool == "create-calendar-event":
            event_id = f"ev-{len(self.events) + len(self.calls)}"
            self.events[event_id] = args["body"]
            return {"text": json.dumps({"id": event_id, **args["body"]})}
        if self.gone:
            msg = "microsoft: 404 ErrorItemNotFound"
            raise ToolError(msg, code="mcp_tool_error")
        if tool == "update-calendar-event":
            self.events[args["eventId"]] = args["body"]
        else:
            assert tool == "delete-calendar-event", f"only one event is written: {tool}"
            del self.events[args["eventId"]]
        return {"text": "{}"}

    def tools(self) -> list[str]:
        return [tool for tool, _args in self.calls]


class _Connections:
    def __init__(self, gmail: _Gmail | None, outlook: _Outlook | None = None) -> None:
        self.gmail, self.outlook = gmail, outlook

    def client_for(self, server: str) -> Any:  # noqa: ANN401 - either fake
        found = {"gmail": self.gmail, "microsoft": self.outlook}.get(server)
        if found is None:
            msg = f"mcp server {server!r} is not connected"
            raise ToolError(msg, code="mcp_server")
        return found


class _Harness:
    def __init__(  # noqa: PLR0913 - the poller's fakes
        self,
        tmp_path: Path,
        jev: _Jev,
        gmail: _Gmail,
        settings: JobMailSettings,
        judge: Any = rule_judge_v1,  # noqa: ANN401 - any Judge
        outlook: _Outlook | None = None,
    ) -> None:
        self.db = tmp_path / "memory.db"
        self.jev, self.gmail, self.outlook = jev, gmail, outlook
        self.event_log = bootstrap_runtime(tmp_path / "runtime").event_log
        open_event_log(self.event_log).close()
        self.job = JobMail(
            settings,
            jev.route(),
            _Connections(gmail, outlook),
            self.db,
            judge,  # type: ignore[arg-type]
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
                    **_job_mail_deps(self.job),
                )
            )
        )

    def _say(self) -> None:
        self.spoken += 1

    def notices(self) -> list[dict[str, Any]]:
        reply = self.client.get("/inherent/notices")
        assert reply.status_code == 200
        return reply.json()["notices"]

    def flat(self) -> list[dict[str, Any]]:
        """The notices with a summary replaced by the alerts it stands for."""
        return [one for n in self.notices() for one in n.get("items", [n])]

    def ledger(self) -> list[dict[str, Any]]:
        return self.client.get("/inherent/jobs").json()["ledger"]

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
def _zh(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fixed words come from the language table; a key makes the poller run."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-a-secret")
    before = lang.language()
    lang.set_language("zh")
    yield
    lang.set_language(before)


@pytest.fixture
def jev() -> Iterator[_Jev]:
    """A fake Jev endpoint for the test's life."""
    fake = _Jev()
    yield fake
    fake.server.shutdown()
    fake.server.server_close()


def _harness(
    tmp_path: Path,
    jev: _Jev,
    mails: list[dict[str, Any]] | None = None,
    judge: Any = rule_judge_v1,  # noqa: ANN401 - any Judge
    outlook: _Outlook | None = None,
    **settings: Any,  # noqa: ANN401
) -> _Harness:
    fields = {**SETTINGS.__dict__, **settings}
    gmail = _Gmail(MAILS if mails is None else mails)
    return _Harness(tmp_path, jev, gmail, JobMailSettings(**fields), judge, outlook)


def test_a_cycle_types_the_mail_fills_the_ledger_and_alerts_by_rule(
    tmp_path: Path,
    jev: _Jev,
) -> None:
    """Receipt: ledger. Rejection: card. Other: card with sound. Interview or offer: speak.

    The offer is the third alert within the burst window, so it is a card with sound (ADR 0158).
    """
    h = _harness(tmp_path, jev)
    assert h.job.poll_once() == len(MAILS)

    kinds = {
        row[0]: row[1:] for row in h.sql("SELECT message_id, kind, company, role FROM job_mail")
    }
    assert kinds == {
        "m-receipt": ("receipt", "CGI", "Software Developer Co-op"),
        "m-interview": ("interview", "Northwind", "Software Developer Co-op"),
        "m-reject": ("rejection", "Orbital", ""),
        "m-offer": ("offer", "Helix", "Backend Developer Intern"),
        "m-digest": ("job_other", "LinkedIn", ""),
        "m-followup": ("job_other", "Acme", ""),  # Jev's top choice was under body_min
        "m-old": ("rejection", "Zenith", ""),
    }
    verdicts = dict(h.sql("SELECT message_id, verdict FROM job_seen"))
    assert verdicts == {
        **{
            f"m-{one}": "job"
            for one in ("receipt", "interview", "reject", "offer", "digest", "followup", "old")
        },
        "m-promo": "not_job",
        "m-fair": "not_job",
        "m-news": "not_job",
        "m-mom": "not_job",
    }

    # The interview's own sentence is kept, with a time only because date and time are clear.
    row = h.sql(
        "SELECT event_text, event_at, extracted_by FROM job_mail WHERE message_id = 'm-interview'"
    )[0]
    assert row[0] == EVENT_SENTENCE
    assert datetime.fromisoformat(row[1]) == datetime(
        2027, 3, 4, 14, 0, tzinfo=ZoneInfo("America/Vancouver")
    )
    assert row[2] == "local"

    # The rule: no alert for the receipt or for mail older than 48 hours.
    levels = dict(h.sql("SELECT message_id, level FROM job_alert"))
    assert levels == {
        "m-interview": "speak",
        "m-reject": "card",
        "m-offer": "card_sound",
        "m-digest": "card_sound",
        "m-followup": "card_sound",
    }
    (summary,) = h.notices()  # five alerts at once are one summary; its items are the notices
    notices = {n["mail_kind"] + ":" + n["company"]: n for n in summary["items"]}
    interview = notices["interview:Northwind"]
    assert interview["kind"] == "mail"
    assert interview["title"] == lang.t("job.title.interview", company="Northwind")
    assert interview["text"] == f"{interview['title']} {interview['line']}"
    assert interview["event_at"] == row[1]
    assert EVENT_SENTENCE not in json.dumps(h.notices())  # the line is typed facts, not body text
    assert interview["level"] == "speak"
    assert notices["rejection:Orbital"]["level"] == "card"
    assert notices["job_other:LinkedIn"]["level"] == "card_sound"

    # One spoken line: the interview said it, and the later alerts of the burst never speak.
    assert h.spoken == 1
    assert sum(spoken for (spoken,) in h.sql("SELECT spoken FROM job_alert")) == 1

    # The ledger groups by company.
    ledger = {one["company"]: one for one in h.ledger()}
    assert set(ledger) == {"CGI", "Northwind", "Orbital", "Helix", "LinkedIn", "Acme", "Zenith"}
    assert ledger["Northwind"]["kind"] == "interview"
    assert ledger["Northwind"]["next_event_at"] == row[1]
    assert ledger["Northwind"]["mails"][0]["event_text"] == EVENT_SENTENCE


def test_what_jev_is_sent_and_what_is_read_from_gmail(tmp_path: Path, jev: _Jev) -> None:
    """Header first (name, domain, subject), body only for what is left; Gmail is only read."""
    h = _harness(tmp_path, jev)
    h.job.poll_once()

    assert h.gmail.tools() == {"gmail_search", "gmail_get"}
    search = next(args for tool, args in h.gmail.calls if tool == "gmail_search")
    assert search == {"query": "newer_than:14d -in:sent -in:drafts", "maxResults": 100}
    # Every scanned mail is read in full once for the local snapshot (ADR 0162), the newsletter
    # and the personal mail that the header cleared included, and never read twice.
    assert h.gmail.full_reads() == {one["id"] for one in MAILS}
    assert all(h.gmail.full_read_count(one["id"]) == 1 for one in MAILS)

    headers = jev.asked("job")
    assert len(headers) == len(MAILS)
    assert len(jev.asked("kind")) == 9
    for request in jev.requests:
        assert request["model"] == "typesafe/jev-1.13"
        assert request["provider"] == {"zdr": True}
        assert "@" not in request["state"].split("\n\n", 1)[0]  # a domain, never an address
    # The header question sees name, domain and subject only: no address, no body (ADR 0162).
    for request in headers:
        assert "@" not in request["state"]
        assert "\n\n" not in request["state"]
        assert not any(one["body"] in request["state"] for one in MAILS)
    cgi = next(r for r in headers if "applying to CGI" in r["state"])
    assert (
        cgi["state"] == "From: CGI Careers\nDomain: cgi.com\nSubject: Thank you for applying to CGI"
    )
    body = next(r for r in jev.asked("kind") if "Interview invitation" in r["state"])
    assert EVENT_SENTENCE in body["state"]
    assert not any(
        "Our weekly newsletter" in r["state"] and "Coffee" in r["state"] for r in jev.requests
    )

    # The body is never kept: the ledger holds typed facts only.
    stored = json.dumps(h.sql("SELECT * FROM job_mail"), ensure_ascii=False)
    assert "Hi Allen" not in stored
    assert "@" not in stored


def test_a_seen_mail_is_not_triaged_again(tmp_path: Path, jev: _Jev) -> None:
    """A second cycle asks Jev and reads Gmail for nothing already seen."""
    h = _harness(tmp_path, jev)
    h.job.poll_once()
    asked, gets = len(jev.requests), sum(1 for t, _a in h.gmail.calls if t == "gmail_get")

    assert h.job.poll_once() == 0
    assert len(jev.requests) == asked
    assert sum(1 for t, _a in h.gmail.calls if t == "gmail_get") == gets
    assert h.spoken == 1  # and nothing is said twice


def test_the_speak_line_is_only_for_quiet_off_unmuted_and_no_conversation(
    tmp_path: Path,
    jev: _Jev,
) -> None:
    """She speaks only at quiet off, unmuted, outside a conversation; decided once at creation."""
    interview_and_offer = [m for m in MAILS if m["id"] in ("m-interview", "m-offer")]

    # An alert made while quiet never speaks later, even once the level is off again.
    held = _harness(tmp_path / "a", jev, interview_and_offer)
    held.quiet = "quiet"
    held.job.poll_once()
    held.quiet = "off"
    held.job.poll_once()
    assert held.spoken == 0
    assert {level for _id, level in held.sql("SELECT id, level FROM job_alert")} == {"speak"}
    # The log says why nothing was said: held by the level, and suppressed.
    delivery = {r["event_id"]: set(r["delivery"]) for r in job_ledger.list_attention(held.db)}
    both = {"held", "suppressed", "audio"}
    assert delivery == {"m-interview": both, "m-offer": both}
    # Muted speech or a live conversation: no line either.
    silent = _harness(tmp_path / "b", jev, interview_and_offer)
    silent.speech_ok = False
    silent.job.poll_once()
    assert silent.spoken == 0
    # speak: false in the config.
    assert _harness(tmp_path / "c", jev, interview_and_offer, speak=False).job.poll_once() == 2
    # With no gap left to wait, both are said.
    both = _harness(tmp_path / "d", jev, interview_and_offer, speak_gap_s=0)
    both.job.poll_once()
    assert both.spoken == 2


def test_quiet_levels_hold_the_alerts_and_release_is_one_digest(tmp_path: Path, jev: _Jev) -> None:
    """quiet: cards without sound. no-pop and dnd: nothing, alerts stay. Back at off: one digest."""
    h = _harness(tmp_path, jev)
    h.job.poll_once()

    h.quiet = "quiet"
    (quiet,) = h.notices()  # five alerts made together are one summary card
    assert quiet["kind"] == "digest"
    assert quiet["level"] == "card"
    assert {n["level"] for n in quiet["items"]} == {"card"}
    for level in ("no-pop", "dnd"):
        h.quiet = level
        assert h.notices() == []
    assert len(h.sql("SELECT id FROM job_alert WHERE state = 'pending'")) == 5

    h.age_alerts(timedelta(minutes=10))  # they waited while the level held them
    h.quiet = "off"
    (digest,) = h.notices()
    assert digest["kind"] == "digest"
    assert digest["title"] == lang.t("job.digest.title_interviews", n=5, x=2)
    assert digest["link"] == "jobs"
    assert [item["mail_kind"] for item in digest["items"]] == [
        "offer",
        "interview",
        "rejection",
        "job_other",
        "job_other",
    ]
    assert digest["level"] == "card_sound"

    # One tap on the digest settles every alert in it.
    assert (
        h.client.post(f"/inherent/notices/{digest['id']}", json={"action": "seen"}).status_code
        == 200
    )
    assert h.notices() == []
    assert {state for (state,) in h.sql("SELECT state FROM job_alert")} == {"shown"}


def test_one_waiting_alert_is_a_card_and_old_ones_are_dropped(tmp_path: Path, jev: _Jev) -> None:
    """One waiting alert is its own card; one older than seven days is not served."""
    h = _harness(tmp_path, jev, [m for m in MAILS if m["id"] in ("m-reject", "m-offer")])
    h.job.poll_once()
    h.age_alerts(timedelta(minutes=10))
    h.sql(
        "UPDATE job_alert SET created_at = ? WHERE message_id = 'm-reject'",
        (NOW - timedelta(days=8)).isoformat(timespec="seconds"),
    )

    (only,) = h.notices()
    assert only["kind"] == "mail"
    assert only["mail_kind"] == "offer"


def _pending(h: _Harness, kinds: list[str], *, level: str, age: timedelta) -> None:
    """One job_mail row and one pending alert per kind, all made ``age`` before NOW (the clock)."""
    h.job.now = lambda: NOW
    made = NOW - age
    for i, kind in enumerate(kinds):
        row = {
            "message_id": f"p-{i}",
            "received_at": made.isoformat(),
            "subject": "s",
            "kind": kind,
            "company": f"Co{i}",
            "role": "",
        }
        job_ledger.upsert_mail(h.db, row, made)
        job_ledger.create_alert(h.db, f"p-{i}", level, f"T{i}", "line", made)


def _thread_mails(h: _Harness, mails: list[dict[str, Any]], *, age: timedelta) -> None:
    """One job_mail row and one pending alert per dict (company, thread, kind, role, event...)."""
    h.job.now = lambda: NOW
    made = NOW - age
    for i, mail in enumerate(mails):
        row = {"message_id": f"g-{i}", "subject": "s", "role": "", **mail}
        row["received_at"] = (made + timedelta(seconds=i)).isoformat()
        job_ledger.upsert_mail(h.db, row, made)
        at = made + timedelta(seconds=i)
        job_ledger.create_alert(h.db, f"g-{i}", "card", f"T{i}", "line", at)


def test_the_summary_is_one_row_per_company_and_thread(tmp_path: Path, jev: _Jev) -> None:
    """Allen's three Reliable Controls mails are one row: best kind, longest role, count, latest."""
    h = _harness(tmp_path, jev, [])
    rc = {"company": "Reliable Controls", "thread_id": "t-rc", "kind": "interview"}
    _thread_mails(
        h,
        [
            {**rc, "role": ""},
            {**rc, "role": "Firmware QA Analyst Co-op"},
            {**rc, "role": "Firmware QA Co-op"},
        ],
        age=timedelta(minutes=20),
    )
    (summary,) = h.notices()
    (row,) = summary["items"]
    assert summary["title"] == "Reliable Controls 面试有 3 封新邮件"
    assert summary["title"] == lang.t(
        "job.digest.title_company",
        company="Reliable Controls",
        kind=lang.t("job.digest.kind.interview"),
        n=3,
    )
    assert (row["company"], row["role"], row["mail_kind"]) == (
        "Reliable Controls",
        "Firmware QA Analyst Co-op",
        "interview",
    )
    assert row["count"] == 3
    assert row["at"] == (NOW - timedelta(minutes=20) + timedelta(seconds=2)).isoformat(
        timespec="seconds"
    )
    lang.set_language("en")
    (english,) = h.notices()
    assert english["title"] == "3 new interview emails from Reliable Controls"
    # A seen summary settles all three alerts.
    seen = h.client.post(f"/inherent/notices/{summary['id']}", json={"action": "seen"})
    assert seen.status_code == 200
    assert h.notices() == []
    assert {state for (state,) in h.sql("SELECT state FROM job_alert")} == {"shown"}


def test_a_mixed_summary_groups_by_thread_ranks_kinds_and_keeps_the_old_header(
    tmp_path: Path, jev: _Jev
) -> None:
    """Offer outranks interview in a thread; threads stay apart; a mail with no thread is alone."""
    h = _harness(tmp_path, jev, [])
    _thread_mails(
        h,
        [
            {"company": "Acme", "thread_id": "t-a", "kind": "rejection", "role": "ML Intern"},
            {"company": "Helix", "thread_id": "t-h", "kind": "interview", "role": "SWE"},
            {"company": "Helix", "thread_id": "t-h", "kind": "offer", "role": "SWE Co-op"},
            {"company": "Helix", "thread_id": "t-h2", "kind": "job_other"},
            {"company": "Helix", "thread_id": None, "kind": "job_other"},
        ],
        age=timedelta(minutes=20),
    )
    (summary,) = h.notices()
    assert [(i["mail_kind"], i["count"]) for i in summary["items"]] == [
        ("offer", 2),
        ("rejection", 1),
        ("job_other", 1),
        ("job_other", 1),
    ]
    assert summary["items"][0]["role"] == "SWE Co-op"
    # Mails, not rows: five mails, the offer and the interview count as interviews.
    assert summary["title"] == lang.t("job.digest.title_interviews", n=5, x=2)

    # One company but two kinds of mail is not the company header either.
    other = _harness(tmp_path / "two", jev, [])
    _thread_mails(
        other,
        [
            {"company": "Helix", "thread_id": "t-1", "kind": "receipt"},
            {"company": "Helix", "thread_id": "t-1", "kind": "job_other"},
        ],
        age=timedelta(minutes=20),
    )
    (summary,) = other.notices()
    assert summary["title"] == lang.t("job.digest.title", n=2)
    assert len(summary["items"]) == 1


def test_a_summary_row_carries_the_interview_time_when_one_was_read(
    tmp_path: Path, jev: _Jev
) -> None:
    """The time is the group's latest dated mail; a short sentence stands in; else nothing."""
    h = _harness(tmp_path, jev, [])
    long_sentence = "We would like to invite you to an interview next week, please reply."
    _thread_mails(
        h,
        [
            {
                "company": "Dated",
                "thread_id": "t-1",
                "kind": "interview",
                "event_at": "2027-10-08T13:00:00",
            },
            {
                "company": "Dated",
                "thread_id": "t-1",
                "kind": "interview",
                "event_at": "2027-10-09T09:30:00",
            },
            {"company": "Dated", "thread_id": "t-1", "kind": "job_other"},
            {
                "company": "Said",
                "thread_id": "t-2",
                "kind": "interview",
                "event_text": " Tuesday 10:00 ",
            },
            {
                "company": "Long",
                "thread_id": "t-3",
                "kind": "interview",
                "event_text": long_sentence,
            },
            {"company": "None", "thread_id": "t-4", "kind": "interview"},
        ],
        age=timedelta(minutes=20),
    )
    (summary,) = h.notices()
    rows = {item["company"]: item for item in summary["items"]}
    assert (rows["Dated"]["event_at"], rows["Dated"]["event_text"]) == ("2027-10-09T09:30:00", None)
    assert (rows["Said"]["event_at"], rows["Said"]["event_text"]) == (None, "Tuesday 10:00")
    assert (rows["Long"]["event_at"], rows["Long"]["event_text"]) == (None, None)
    assert (rows["None"]["event_at"], rows["None"]["event_text"]) == (None, None)


def test_a_summary_with_no_interview_has_no_interview_clause(tmp_path: Path, jev: _Jev) -> None:
    """The count of interviews is only said when it is more than zero, in both languages."""
    h = _harness(tmp_path, jev, [])
    _pending(h, ["job_other"] * 3, level="card", age=timedelta(minutes=20))
    for language in ("zh", "en"):
        lang.set_language(language)
        (summary,) = h.notices()
        assert summary["title"] == lang.t("job.digest.title", n=3)
        assert "{" not in summary["title"]
    assert "封面试" not in summary["title"]
    assert "interview" not in summary["title"]


def test_ten_pending_alerts_are_one_summary_that_never_speaks(tmp_path: Path, jev: _Jev) -> None:
    """Allen's backfill left 10 pending alerts, three at speak: one card, a link, no speech."""
    h = _harness(tmp_path, jev, [])
    kinds = ["interview"] * 3 + ["job_other"] * 6 + ["offer"]
    _pending(h, kinds, level="card_sound", age=timedelta(minutes=20))
    h.sql("UPDATE job_alert SET level = 'speak' WHERE id IN (SELECT id FROM job_alert LIMIT 3)")

    for quiet in ("off", "quiet"):
        h.quiet = quiet
        (summary,) = h.notices()
        assert summary["kind"] == "digest"
        assert summary["title"] == lang.t("job.digest.title_interviews", n=10, x=4)
        assert summary["link"] == "jobs"
        assert summary["level"] == ("card_sound" if quiet == "off" else "card")
        assert len(summary["items"]) == 10
    assert h.spoken == 0

    # A health alert is not job mail: it stays its own card and is not counted.
    job_ledger.create_alert(h.db, job_ledger.HEALTH_ID, "card_sound", "down", "x", NOW)
    h.quiet = "off"
    health, summary = h.notices()
    assert (health["kind"], summary["kind"], len(summary["items"])) == ("mail", "digest", 10)


def test_two_alerts_in_a_burst_are_two_cards_and_three_are_one_summary(
    tmp_path: Path, jev: _Jev
) -> None:
    """Fresh alerts: two stay separate cards; a third within the window merges all of them."""
    h = _harness(tmp_path, jev, [])
    _pending(h, ["rejection", "job_other"], level="card", age=timedelta(seconds=5))
    assert [n["kind"] for n in h.notices()] == ["mail", "mail"]

    h = _harness(tmp_path / "three", jev, [])
    _pending(h, ["rejection", "job_other", "receipt"], level="card", age=timedelta(seconds=5))
    (summary,) = h.notices()
    assert (summary["kind"], len(summary["items"])) == ("digest", 3)

    # One alert that waited 120 s and two fresh ones span more than the window: not a burst.
    assert timedelta(seconds=90) == job_ledger.BURST_WINDOW
    far = _harness(tmp_path / "far", jev, [])
    _pending(far, ["rejection"], level="card", age=timedelta(seconds=120))
    for i in (1, 2):
        made = NOW - timedelta(seconds=5)
        row = {"message_id": f"p-{i}", "kind": "job_other", "company": f"Co{i}", "role": ""}
        job_ledger.upsert_mail(far.db, row, made)
        job_ledger.create_alert(far.db, f"p-{i}", "card", "t", "l", made)
    assert [n["kind"] for n in far.notices()] == ["mail"] * 3


def test_a_single_live_interview_speaks_but_a_burst_of_interviews_does_not(
    tmp_path: Path, jev: _Jev
) -> None:
    """One interview at quiet off is spoken; the third alert within the window never is."""
    one = _harness(tmp_path / "one", jev, [m for m in MAILS if m["id"] == "m-interview"])
    one.job.poll_once()
    assert one.spoken == 1
    (notice,) = one.notices()
    assert (notice["kind"], notice["level"]) == ("mail", "speak")

    interviews = [
        {**m, "id": f"m-i{i}", "threadId": f"t-i{i}"}
        for i, m in enumerate([next(m for m in MAILS if m["id"] == "m-interview")] * 3)
    ]
    burst = _harness(tmp_path / "burst", jev, interviews, speak_gap_s=0)
    burst.job.poll_once()
    assert burst.spoken == 2  # the third is merged into the summary
    assert [level for (level,) in burst.sql("SELECT level FROM job_alert ORDER BY rowid")] == [
        "speak",
        "speak",
        "card_sound",
    ]
    (summary,) = burst.notices()
    assert (summary["kind"], summary["level"]) == ("digest", "card_sound")


@pytest.mark.parametrize(
    ("name", "domain", "subject", "company"),
    [
        # Allen's real mail: a recruiter's name is not the company.
        (
            "Jill Crowe",
            "app.bamboohr.com",
            "Re: Reliable Controls - Invitation to Interview",
            "Reliable Controls",
        ),
        (
            "Jill Crowe",
            "reliablecontrols.com",
            "Yilun (Allen) Shi - Firmware QA Co-op - Virtual interview",
            "Reliable Controls",
        ),
        ("CGI", "njoyn.com", "CGI - User Information", "CGI"),
        # An ATS speaks for the employer: the subject names it.
        ("Acme Careers", "myworkdayjobs.com", "Thank you for applying to Acme", "Acme"),
        ("", "greenhouse.io", "Application for Software Engineer at Acme Corp", "Acme Corp"),
        # Allen's real mail, 2026-10-06: the ATS's own name is never the company.
        (
            "Greenhouse Mail",
            "us.greenhouse-mail.io",
            "Thank you for applying to Later - Software Development Co-op",
            "Later",
        ),
        (
            "Greenhouse",
            "us.greenhouse-mail.io",
            "Security code for your application to Later",
            "Later",
        ),
        (
            "ClearCompany",
            "clearcompany.com",
            "Sign-in link for your application at Delta Intelligent Build",
            "Delta Intelligent Build",
        ),
        # Organisation names stay, however many capitalised words.
        ("Reliable Controls", "reliablecontrols.com", "Hello", "Reliable Controls"),
        ("Mary Kay", "marykay.com", "Hello", "Mary Kay"),
        ("Northwind Talent", "northwind.example", "Hello", "Northwind"),
        ("", "mail.example.co.uk", "Hello", "Example"),
        ("", "", "Hello", "Unknown"),
    ],
)
def test_the_company_is_the_organisation_not_the_person_who_wrote(
    name: str, domain: str, subject: str, company: str
) -> None:
    """A person's display name yields to the sender domain's organisation; an ATS to the subject."""
    assert triage.company_of(name, domain, subject) == company


@pytest.mark.parametrize(
    ("subject", "body", "role"),
    [
        # Allen's real CGI mail: never "job"; the program word and the requisition id go.
        ("Job Application Acknowledgement - Coop - AI Developer, J0926-0916", "", "AI Developer"),
        (
            "Job Application Acknowledgement - Winter 2027: AI Developer Co-op (8 months),"
            " J0926-1622",
            "",
            "AI Developer",
        ),
        ("Winter 2027: AI Developer", "", "AI Developer"),
        ("Yilun (Allen) Shi - Firmware QA Co-op - Virtual interview", "", "Firmware QA Co-op"),
        ("Interview invitation: Software Developer Co-op", "", "Software Developer Co-op"),
        ("Application for ML Intern at Acme", "", "ML Intern"),
        (
            "Offer",
            "We would like to offer you the Backend Developer Intern position.",
            "Backend Developer Intern",
        ),
        # No role in it: empty, and a generic word is never a role.
        ("Re: Reliable Controls - Invitation to Interview", "", ""),
        ("CGI - User Information", "", ""),
        ("Thanks", "We received your application for the job position.", ""),
    ],
)
def test_the_role_is_read_from_the_subject_or_body_and_never_a_generic_word(
    subject: str, body: str, role: str
) -> None:
    """A role comes from known patterns; with none, it is empty."""
    assert triage.role_of(subject, body) == role


CAMBIO_BODY = (
    "Hi Allen,\n\nThank you for applying to Cambio Earth's QA & Test Automation Developer Co-op"
    " position. We appreciate the time you took. While you were not selected for an interview"
    " this time, we will keep your resume on file."
)


@pytest.mark.parametrize(
    ("name", "domain", "subject", "body", "company"),
    [
        # Allen's real rejection mail: the possessive and the department word are not the company.
        ("Maren Ingle", "app.bamboohr.com", "Cambio Earth - Update", CAMBIO_BODY, "Cambio Earth"),
        (
            "Pat Doe",
            "greenhouse.io",
            "Update",
            "Please join Acme's Engineering team.",
            "Acme",
        ),
        (
            "Pat Doe",
            "greenhouse.io",
            "Update",
            "Thanks for applying to Cambio Earth.",
            "Cambio Earth",
        ),
        # A possessive that is the name stays.
        (
            "Pat Doe",
            "greenhouse.io",
            "Update",
            "Please join Lowe's Burgers today.",
            "Lowe's Burgers",
        ),
    ],
)
def test_a_possessive_and_the_role_word_after_it_are_not_the_company(
    name: str, domain: str, subject: str, body: str, company: str
) -> None:
    """The company is Cambio Earth, not Cambio Earth's QA: role words after the 's go."""
    assert triage.company_of(name, domain, subject, body) == company


@pytest.mark.parametrize(
    ("subject", "body", "company", "role"),
    [
        # Allen's real mail, 2026-10-06: the company's name is no role; "our", "- Applications" go.
        ("Security code for your application to Later", "", "Later", ""),
        ("Thank you for applying at Planview", "", "Planview", ""),
        (
            "Thank you for applying to Rivian and Volkswagen Group T...",
            "Thank you for applying to our Software Engineering Intern - Applications role.",
            "RV Tech",
            "Software Engineering Intern",
        ),
        ("Update", "Please review the the QA Engineer position details.", "Acme", "QA Engineer"),
    ],
)
def test_the_role_is_never_the_company_and_loses_our_the_and_applications(
    subject: str, body: str, company: str, role: str
) -> None:
    """A role equal to the company is dropped; a leading our/the and a trailing suffix go."""
    assert triage.role_of(subject, body, company) == role


@pytest.mark.parametrize(
    ("subject", "body", "role"),
    [
        ("Cambio Earth - Update", CAMBIO_BODY, "QA & Test Automation Developer Co-op"),
        ("Update", "Thank you for applying to the Data Analyst role at Acme.", "Data Analyst"),
        ("Update", "Please review the QA Engineer position details.", "QA Engineer"),
        # A lowercase word after "the" is no role, and the generic words stay out.
        ("Update", "We filled the next position. Thanks for the job role.", ""),
    ],
)
def test_the_role_is_read_from_applying_to_a_company_s_role_position(
    subject: str, body: str, role: str
) -> None:
    """The company's possessive is not part of the role; capitals and the ampersand are kept."""
    assert triage.role_of(subject, body) == role


@pytest.mark.parametrize(
    ("name", "domain", "subject", "social"),
    [
        ("LinkedIn", "linkedin.com", "You appeared in 5 searches", True),
        ("LinkedIn", "linkedin.com", "You appeared in 12 search appearances this week", True),
        ("LinkedIn", "linkedin.com", "Your profile was viewed 3 times", True),
        ("LinkedIn", "linkedin.com", "12 people viewed your profile", True),
        ("LinkedIn", "linkedin.com", "You have 4 new profile views", True),
        ("LinkedIn", "linkedin.com", "1 new profile view", True),
        # Not profile activity: job alerts, digests, interview and offer InMail, other senders.
        ("LinkedIn Job Alerts", "linkedin.com", "Machine Learning Engineer II at TD", False),
        ("LinkedIn Job Alerts", "linkedin.com", "5 new jobs for Software Developer", False),
        (
            "Jane Recruiter",
            "linkedin.com",
            "Jane, interview for the Software Developer Co-op",
            False,
        ),
        ("Jane Recruiter", "linkedin.com", "Offer of employment at Acme", False),
        ("Acme", "acme.example", "You appeared in 5 searches", False),
    ],
)
def test_linkedin_profile_activity_is_social_and_nothing_else_is(
    name: str, domain: str, subject: str, *, social: bool
) -> None:
    """The profile-activity subjects join the social rule; real job mail does not."""
    assert triage.is_social(domain, subject) is social
    assert not social or not triage.is_alert_digest(name, domain, subject)


def test_the_repair_pass_fixes_stored_rows_offline_and_is_idempotent(
    tmp_path: Path, jev: _Jev
) -> None:
    """Company and role are recomputed from the stored header; no Gmail, nothing changes twice."""
    h = _harness(tmp_path, jev, [])
    reliable = "Reliable Controls - Invitation to Interview"
    rows = [
        # key, name, domain, subject, stored role, company and role after repair
        (
            "a",
            "Jill Crowe",
            "app.bamboohr.com",
            f"Re: {reliable}",
            reliable,
            ("Reliable Controls", ""),
        ),
        # read from the body once: the subject does not hold it, so it stays
        (
            "b",
            "Jill Crowe",
            "app.bamboohr.com",
            reliable,
            "Firmware QA Analyst Co-op",
            ("Reliable Controls", "Firmware QA Analyst Co-op"),
        ),
        (
            "c",
            "Jill Crowe",
            "reliablecontrols.com",
            "Yilun (Allen) Shi - Firmware QA Co-op - Virtual interview",
            "",
            ("Reliable Controls", "Firmware QA Co-op"),
        ),
        (
            "d",
            "CGI",
            "njoyn.com",
            "Job Application Acknowledgement - Coop - AI Developer, J0926-0916",
            "job",
            ("CGI", "AI Developer"),
        ),
        ("e", "CGI", "njoyn.com", "CGI - User Information", "", ("CGI", "")),
    ]
    for key, name, domain, subject, role, _after in rows:
        mail = {
            "message_id": key,
            "received_at": NOW.isoformat(),
            "sender_name": name,
            "sender_domain": domain,
            "subject": subject,
            "kind": "interview",
            "company": name,
            "role": role,
        }
        job_ledger.upsert_mail(h.db, mail, NOW)
    assert repair(h.db, "ledger_only") == 4  # the account notice was right already
    assert {
        key: tuple(rest) for key, *rest in h.sql("SELECT message_id, company, role FROM job_mail")
    } == {key: after for key, *_mid, after in rows}
    assert [one["company"] for one in h.ledger()] == ["Reliable Controls", "CGI"]
    assert repair(h.db, "ledger_only") == 0
    assert h.gmail.calls == []


def test_local_rules_keep_linkedin_noise_and_account_notices_off_the_cards(
    tmp_path: Path, jev: _Jev
) -> None:
    """Social news is held back unread, an account notice is kind other, a digest is ledger only."""
    h = _harness(tmp_path, jev, LINKEDIN, linkedin_alerts="ledger_only")
    h.job.poll_once()

    assert dict(h.sql("SELECT message_id, verdict FROM job_seen")) == {
        "m-post": "not_job",
        "m-near": "not_job",
        "m-alert": "job",
        "m-hiring": "job",
        "m-account": "job",
        "m-inmail": "job",
    }
    # Every mail is read for the local snapshot, but Jev is asked about no social body.
    assert h.gmail.full_reads() == {one["id"] for one in LINKEDIN}
    assert len(jev.asked("kind")) == 4
    held = {one["subject"] for one in h.client.get("/inherent/jobs").json()["skipped"]}
    assert held == {"Byron Kontou recently posted", "Software Engineer: RBC hired near you"}
    assert dict(h.sql("SELECT message_id, kind FROM job_mail")) == {
        "m-alert": "job_other",
        "m-hiring": "job_other",
        "m-account": "other",
        "m-inmail": "interview",
    }
    # Only the recruiter's interview alerts; the logs say what held the rest.
    assert h.sql("SELECT message_id, level FROM job_alert") == [("m-inmail", "speak")]
    rows = {r["event_id"]: r for r in job_ledger.list_attention(h.db, "job_mail")}
    assert rows["m-alert"]["reason"] == "LinkedIn job-alert digest: ledger only"
    assert set(rows["m-account"]["delivery"]) == {"ledger_only"}
    rule = h.sql("SELECT judge, verdict FROM job_decision WHERE stage = 'rule'")
    assert rule == [("local-rule/social-v1", "not_job")] * 2

    # The ledger page is told the standing rule, not left to hardcode it.
    jobs = h.client.get("/inherent/jobs").json()
    assert jobs["rules"] == [{"id": "linkedin_alerts", "value": "ledger_only"}]

    # card_sound brings the digests back as cards with sound; the account notice stays quiet.
    # The interview is the third alert of the burst, so it is a card with sound, not a line.
    on = _harness(tmp_path / "on", jev, LINKEDIN, linkedin_alerts="card_sound")
    on.job.poll_once()
    assert dict(on.sql("SELECT message_id, level FROM job_alert")) == {
        "m-alert": "card_sound",
        "m-hiring": "card_sound",
        "m-inmail": "card_sound",
    }


def test_the_repair_pass_hides_noise_drops_its_alerts_and_respects_a_flag(
    tmp_path: Path, jev: _Jev
) -> None:
    """Stored social mail is held back, notices retyped, alerts done; idempotent; flags win."""
    h = _harness(tmp_path, jev, LINKEDIN, linkedin_alerts="card_sound")
    h.job.poll_once()  # card_sound: digests alerted, the social mail never entered the ledger
    # Rows as the first backfill left them: social mail typed job_other, with alerts pending.
    for key, subject in (
        ("old1", "Byron Kontou recently posted"),
        ("old2", "Flagged recently posted"),
    ):
        mail = {
            "message_id": key,
            "received_at": NOW.isoformat(),
            "sender_name": "LinkedIn",
            "sender_domain": "linkedin.com",
            "subject": subject,
            "kind": "job_other",
            "company": "LinkedIn",
            "role": "",
        }
        job_ledger.upsert_mail(h.db, mail, NOW)
        job_ledger.record_seen(h.db, key, "job", NOW, p_job=0.8)
        job_ledger.create_alert(h.db, key, "card_sound", "t", "l", NOW)
    job_ledger.add_flag(h.db, "old2", NOW)

    def pending() -> set[str]:
        return {m for (m,) in h.sql("SELECT message_id FROM job_alert WHERE state = 'pending'")}

    assert {"old1", "old2", "m-alert", "m-hiring"} <= pending()

    assert repair(h.db, "ledger_only") > 0
    assert h.sql("SELECT deleted FROM job_mail WHERE message_id = 'old1'") == [(1,)]
    assert h.sql("SELECT deleted FROM job_mail WHERE message_id = 'old2'") == [(0,)]  # flagged
    assert h.sql("SELECT verdict, p_job FROM job_seen WHERE message_id = 'old1'") == [
        ("not_job", 0.8)
    ]
    assert h.sql("SELECT stage, verdict, judge FROM job_decision WHERE message_id = 'old1'") == [
        ("rule", "not_job", "local-rule/social-v1")
    ]
    assert "Byron Kontou recently posted" in {
        one["subject"] for one in h.client.get("/inherent/jobs").json()["skipped"]
    }
    assert h.sql("SELECT kind FROM job_mail WHERE message_id = 'm-account'") == [("other",)]
    assert not pending() & {"old1", "m-alert", "m-hiring", "m-account"}  # done, not deleted
    assert pending() >= {"old2", "m-inmail"}
    assert repair(h.db, "ledger_only") == 0  # a second run changes nothing

    # With card_sound the digests keep their alerts.
    keep = _harness(tmp_path / "keep", jev, LINKEDIN, linkedin_alerts="card_sound")
    keep.job.poll_once()
    repair(keep.db, "card_sound")
    assert {m for (m,) in keep.sql("SELECT message_id FROM job_alert WHERE state = 'pending'")} == {
        "m-alert",
        "m-hiring",
        "m-inmail",
    }


def test_excluded_domains_never_reach_jev_the_ledger_or_an_alert(tmp_path: Path, jev: _Jev) -> None:
    """Mail from an excluded domain is held back by a local rule before Jev, the rest lands."""
    inmail = LINKEDIN[5]
    h = _harness(tmp_path, jev, [inmail, MAILS[1]], exclude_domains=("linkedin.com",))
    h.job.poll_once()

    assert [one["state"].split("Subject: ", 1)[1].split("\n", 1)[0] for one in jev.requests] == [
        "Interview invitation: Software Developer Co-op"
    ] * 2  # the header and body questions of the one letter that was not excluded
    assert h.sql("SELECT message_id FROM job_mail") == [("m-interview",)]
    assert h.sql("SELECT message_id FROM job_alert") == [("m-interview",)]
    assert h.sql("SELECT verdict FROM job_seen WHERE message_id = 'm-inmail'") == [("not_job",)]
    decision = "SELECT stage, verdict, judge FROM job_decision WHERE message_id = 'm-inmail'"
    assert h.sql(decision) == [("rule", "not_job", "local-rule/exclude-v1")]
    # Its body is still read and kept locally, like any scanned mail (ADR 0162).
    assert h.gmail.full_read_count("m-inmail") == 1
    status = "SELECT body_status FROM job_decision WHERE message_id = 'm-inmail'"
    assert h.sql(status) == [("read",)]
    jobs = h.client.get("/inherent/jobs").json()
    assert [one["message_id"] for one in jobs["skipped"]] == ["m-inmail"]
    assert jobs["rules"][-1] == {"id": "exclude_domains", "value": "linkedin.com"}


def test_the_repair_pass_hides_an_existing_excluded_row_and_respects_a_flag(
    tmp_path: Path, jev: _Jev
) -> None:
    """A visible row of an excluded domain is hidden, its alerts done; a flagged one stays."""
    h = _harness(tmp_path, jev, [])
    for key in ("old1", "old2"):
        mail = {
            "message_id": key,
            "received_at": NOW.isoformat(),
            "sender_name": "Jane Recruiter",
            "sender_domain": "mail.linkedin.com",
            "subject": "Interview for the Software Developer Co-op",
            "kind": "interview",
            "company": "LinkedIn",
            "role": "",
        }
        job_ledger.upsert_mail(h.db, mail, NOW)
        job_ledger.record_seen(h.db, key, "job", NOW, p_job=0.9)
        job_ledger.create_alert(h.db, key, "speak", "t", "l", NOW)
    job_ledger.add_flag(h.db, "old2", NOW)

    repair(h.db, "ledger_only")  # nothing excluded: nothing is hidden
    assert h.sql("SELECT deleted FROM job_mail") == [(0,), (0,)]
    assert repair(h.db, "ledger_only", ("linkedin.com",)) > 0
    assert h.sql("SELECT message_id, deleted FROM job_mail ORDER BY message_id") == [
        ("old1", 1),
        ("old2", 0),
    ]
    assert h.sql("SELECT message_id FROM job_alert WHERE state = 'pending'") == [("old2",)]
    assert h.sql("SELECT stage, verdict, judge FROM job_decision WHERE message_id = 'old1'") == [
        ("rule", "not_job", "local-rule/exclude-v1")
    ]
    assert [one["message_id"] for one in h.client.get("/inherent/jobs").json()["skipped"]] == [
        "old1"
    ]
    assert repair(h.db, "ledger_only", ("linkedin.com",)) == 0  # idempotent


def test_flagging_a_hidden_social_mail_brings_it_back_to_the_ledger(
    tmp_path: Path, jev: _Jev
) -> None:
    """Allen's flag wins over the local rule: the mail is job mail, in the ledger, with an alert."""
    h = _harness(tmp_path, jev, LINKEDIN[:1], linkedin_alerts="ledger_only")
    h.job.poll_once()
    assert h.sql("SELECT 1 FROM job_mail") == []
    assert (
        h.client.post("/inherent/jobs/m-post/flag", json={"reaction": "should_alert"}).status_code
        == 200
    )
    assert h.sql("SELECT kind, deleted FROM job_mail WHERE message_id = 'm-post'") == [
        ("job_other", 0)
    ]
    assert h.sql("SELECT level FROM job_alert") == [("card_sound",)]
    assert "m-post" not in {
        one["message_id"] for one in h.client.get("/inherent/jobs").json()["skipped"]
    }


def test_feedback_is_recorded_and_an_untouched_alert_ends_ignored(
    tmp_path: Path, jev: _Jev
) -> None:
    """Reactions land in job_feedback; a card shown and left alone for 30 minutes is ignored."""
    h = _harness(
        tmp_path, jev, [m for m in MAILS if m["id"] in ("m-reject", "m-digest", "m-offer")]
    )
    h.job.poll_once()
    ids = {mail_id: alert_id for alert_id, mail_id in h.sql("SELECT id, message_id FROM job_alert")}
    post = h.client.post

    h.quiet = "quiet"  # what Allen saw was a card without sound
    assert (
        post(
            f"/inherent/notices/{ids['m-digest']}", json={"action": "feedback", "reaction": "right"}
        ).status_code
        == 200
    )
    level = lang.JOB_LEVEL_NAMES[2]
    assert (
        post(
            f"/inherent/notices/{ids['m-offer']}",
            json={"action": "feedback", "reaction": f"level:{level}"},
        ).status_code
        == 200
    )
    assert post(f"/inherent/notices/{ids['m-reject']}", json={"action": "seen"}).status_code == 200
    assert h.sql("SELECT alert_id, level_shown, reaction FROM job_feedback ORDER BY id") == [
        (ids["m-digest"], "card", "right"),
        (ids["m-offer"], "card", f"level:{level}"),
    ]
    assert dict(h.sql("SELECT id, state FROM job_alert")) == {
        ids["m-digest"]: "done",
        ids["m-offer"]: "done",
        ids["m-reject"]: "shown",
    }

    # Dismissing is feedback too; a made-up reaction or id is refused.
    assert (
        post(f"/inherent/notices/{ids['m-reject']}", json={"action": "dismissed"}).status_code
        == 200
    )
    assert h.sql("SELECT reaction FROM job_feedback WHERE alert_id = ?", ids["m-reject"]) == [
        ("dismissed",)
    ]
    for reaction in ("loved-it", "level:nonsense", None):
        bad = post(
            f"/inherent/notices/{ids['m-offer']}", json={"action": "feedback", "reaction": reaction}
        )
        assert bad.status_code == 400
    assert post("/inherent/notices/nope", json={"action": "seen"}).status_code == 404

    # Shown and left alone for 30 minutes: the daemon writes "ignored" at its next cycle.
    h2 = _harness(tmp_path / "ignore", jev, [m for m in MAILS if m["id"] == "m-reject"])
    h2.job.poll_once()
    (alert,) = h2.notices()
    h2.client.post(f"/inherent/notices/{alert['id']}", json={"action": "seen"})
    h2.sql(
        "UPDATE job_alert SET shown_at = ?",
        (NOW - timedelta(minutes=31)).isoformat(timespec="seconds"),
    )
    h2.job.poll_once()
    assert h2.sql("SELECT reaction, level_shown FROM job_feedback") == [("ignored", "card")]


def test_feedback_on_a_summary_is_logged_for_every_alert_it_holds(
    tmp_path: Path, jev: _Jev
) -> None:
    """A level or 对 on the summary writes one row per alert at the summary's level."""
    level = lang.JOB_LEVEL_NAMES[3]
    for quiet, shown in (("off", "card_sound"), ("quiet", "card")):
        h = _harness(tmp_path / quiet, jev, [])
        _pending(
            h, ["interview", "job_other", "rejection"], level="card", age=timedelta(minutes=20)
        )
        h.sql("UPDATE job_alert SET level = 'speak' WHERE message_id = 'p-0'")
        h.sql("UPDATE job_alert SET level = 'card_sound' WHERE message_id = 'p-1'")
        h.quiet = quiet
        (summary,) = h.notices()
        assert summary["level"] == shown
        ids = dict(h.sql("SELECT message_id, id FROM job_alert"))
        for reaction in (f"level:{level}", "right"):
            reply = h.client.post(
                f"/inherent/notices/{summary['id']}",
                json={"action": "feedback", "reaction": reaction},
            )
            assert reply.status_code == 200
            rows = h.sql("SELECT alert_id, level_shown, reaction FROM job_feedback ORDER BY id")
            assert sorted(rows) == sorted((ids[f"p-{i}"], shown, reaction) for i in range(3))
            assert {state for (state,) in h.sql("SELECT state FROM job_alert")} == {"done"}
            assert h.notices() == []
            h.sql("DELETE FROM job_feedback")
            h.sql("UPDATE job_alert SET state = 'pending'")

    # Dismissed ends every alert in the summary, logged at the summary's level.
    h = _harness(tmp_path / "gone", jev, [])
    _pending(h, ["interview", "job_other", "rejection"], level="card", age=timedelta(minutes=20))
    (summary,) = h.notices()
    post = h.client.post
    assert (
        post(f"/inherent/notices/{summary['id']}", json={"action": "dismissed"}).status_code == 200
    )
    assert h.sql("SELECT level_shown, reaction FROM job_feedback") == [("card", "dismissed")] * 3
    assert h.notices() == []
    assert {state for (state,) in h.sql("SELECT state FROM job_alert")} == {"done"}


def test_deleting_a_mail_hides_it_from_the_ledger_and_its_alert(tmp_path: Path, jev: _Jev) -> None:
    """Deleting hides the mail and its alert, and a later cycle does not bring it back."""
    h = _harness(tmp_path, jev)
    h.job.poll_once()

    assert h.client.post("/inherent/jobs/m-offer/delete").status_code == 200
    assert "Helix" not in {one["company"] for one in h.ledger()}
    assert "offer" not in {n["mail_kind"] for n in h.notices()}
    assert h.sql("SELECT deleted FROM job_mail WHERE message_id = 'm-offer'") == [(1,)]
    assert h.client.post("/inherent/jobs/m-nope/delete").status_code == 404
    h.job.poll_once()  # it is seen, so it does not come back
    assert "Helix" not in {one["company"] for one in h.ledger()}


def test_the_daily_cap_stops_jev_calls_and_leaves_the_rest_unseen(
    tmp_path: Path, jev: _Jev
) -> None:
    """At the daily cap no more Jev calls are made and the rest stay unseen."""
    h = _harness(tmp_path, jev, max_calls_per_day=3)
    h.job.poll_once()
    assert len(jev.requests) == 3
    assert h.job.poll_once() == 0
    assert len(jev.requests) == 3
    assert len(h.sql("SELECT message_id FROM job_seen")) < len(MAILS)


def test_a_jev_failure_is_an_error_retried_three_times_then_kept(tmp_path: Path, jev: _Jev) -> None:
    """A Jev failure is recorded as an error, retried three times, then kept."""
    jev.status = 500
    h = _harness(tmp_path, jev, [m for m in MAILS if m["id"] == "m-offer"])
    for _ in range(3):
        assert h.job.poll_once() == 1
    assert h.sql("SELECT verdict, tries FROM job_seen") == [("error", 3)]
    asked = len(jev.requests)
    assert h.job.poll_once() == 0
    assert len(jev.requests) == asked
    assert h.sql("SELECT * FROM job_mail") == []


def test_only_gmail_search_and_get_can_be_called() -> None:
    """Any Gmail tool other than search and get is refused before it is sent."""
    gmail = _Gmail([])
    for tool in (
        "gmail_modify",
        "gmail_batchModify",
        "gmail_send",
        "gmail_trash",
        "gmail_sendDraft",
    ):
        with pytest.raises(ValueError, match="only read"):
            gmail_read(gmail, tool, {})  # type: ignore[arg-type]
    assert gmail.calls == []
    assert gmail_read(gmail, "gmail_search", {"query": "x"}) == {  # type: ignore[arg-type]
        "messages": [],
        "resultSizeEstimate": 0,
    }


def test_the_loop_polls_and_survives_a_failed_cycle(tmp_path: Path, jev: _Jev) -> None:
    """The run loop polls and goes on after a cycle that raised."""
    h = _harness(tmp_path, jev, [m for m in MAILS if m["id"] == "m-reject"], poll_s=0.05)
    h.gmail.search_failures = 1

    async def run() -> None:
        task = asyncio.create_task(h.job.run())
        for _ in range(100):
            if job_ledger.list_ledger(h.db, NOW):
                break
            await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert h.sql("SELECT kind FROM job_mail") == [("rejection",)]
    assert [tool for tool, _a in h.gmail.calls].count("gmail_search") >= 2


def test_off_by_default_no_routes_no_poller_and_bad_values_stop_boot(tmp_path: Path) -> None:
    """Off by default: no poller, routes answer 404, and bad values stop boot."""
    shipped = yaml.safe_load((repo_root() / "config" / "jarvis.yaml").read_text(encoding="utf-8"))
    block = shipped["job_mail"]
    assert block["enabled"] is False
    assert {
        "poll_s": 300,
        "backfill_days": 14,
        "max_messages_per_cycle": 25,
        "header_skip_at": 0.9,
        "body_min": 0.5,
        "max_body_chars": 3000,
        "model": "typesafe/jev-1.13",
        "timeout_ms": 8000,
        "max_calls_per_day": 400,
        "speak": True,
        "speak_gap_s": 600,
        "linkedin_alerts": "ledger_only",
        "exclude_domains": ["linkedin.com"],
        "backfill_since": date(2026, 9, 10),
    }.items() <= block.items()
    config_path, db = tmp_path / "jarvis.yaml", tmp_path / "memory.db"
    connections: Any = _Connections(None)

    assert _job_mail(shipped, config_path, None, connections, db) is None  # no watcher is built
    assert _job_mail({}, config_path, None, connections, db) is None
    on = {"job_mail": {**block, "enabled": True}}
    built = _job_mail(on, config_path, None, connections, db)
    assert isinstance(built, JobMail)
    spaced = {"job_mail": {**on["job_mail"], "exclude_domains": [" LinkedIn.com "]}}
    built = _job_mail(spaced, config_path, None, connections, db)
    assert built is not None
    assert built._settings.exclude_domains == ("linkedin.com",)  # noqa: SLF001
    assert built._settings.backfill_since == date(2026, 9, 10)  # noqa: SLF001
    undated = {"job_mail": {**on["job_mail"], "backfill_since": None}}
    built = _job_mail(undated, config_path, None, connections, db)
    assert built is not None
    assert built._settings.backfill_since is None  # noqa: SLF001
    for key, value in (
        ("poll_s", 0),
        ("backfill_days", "14"),
        ("header_skip_at", 1.5),
        ("body_min", 0),
        ("max_body_chars", True),
        ("model", " "),
        ("timeout_ms", 0),
        ("speak", "yes"),
        ("linkedin_alerts", "sometimes"),
        ("exclude_domains", "linkedin.com"),
        ("exclude_domains", [" "]),
        ("backfill_since", "2026-09-10"),
        ("max_calls_per_day", None),
    ):
        bad = {"job_mail": {**block, "enabled": True, key: value}}
        with pytest.raises(RuntimeBootstrapError, match=f"job_mail.{key}"):
            _job_mail(bad, config_path, None, connections, db)

    # The daemon wires no route for it, so every one answers 404.
    assert _job_mail_deps(None) == {}
    client = TestClient(
        create_app(
            InherentDeps(
                submit_callable=lambda _text: None,
                broadcaster=InherentBroadcaster(),
            )
        )
    )
    assert client.get("/inherent/notices").status_code == 404
    assert client.post("/inherent/notices/x", json={"action": "seen"}).status_code == 404
    assert client.get("/inherent/jobs").status_code == 404
    assert client.post("/inherent/jobs/x/delete").status_code == 404
    assert (
        client.post("/inherent/jobs/x/flag", json={"reaction": "should_alert"}).status_code == 404
    )


def test_the_spoken_line_is_one_fixed_phrase_through_the_conversation_machinery(
    tmp_path: Path,
) -> None:
    """The daemon's speak wrapper says one fixed phrase through the conversation-line machinery."""
    runtime = _make_runtime(tmp_path, commentary=False)

    _say_job_line(runtime)

    assert _only_phrase(runtime.conn) in lang.variants("conversation.job_speak", "zh")


def test_each_decision_is_logged_with_its_pack_delivery_and_feedback(
    tmp_path: Path,
    jev: _Jev,
) -> None:
    """Each decision is logged with its full pack, delivery states and Allen's reactions."""
    h = _harness(tmp_path, jev)
    h.job.poll_once()
    rows = {r["event_id"]: r for r in job_ledger.list_attention(h.db, "job_mail")}
    assert set(rows) == {
        f"m-{one}"
        for one in ("receipt", "interview", "reject", "offer", "digest", "followup", "old")
    }

    interview = rows["m-interview"]
    pack = ContextPack.from_json(interview["pack_json"])
    assert pack.source == "job_mail"
    assert pack.facts["kind"] == "interview"
    assert pack.facts["company"] == "Northwind"
    assert pack.facts["sender_domain"] == "northwind.example"
    assert set(pack.situation) == {"hour", "weekday", "quiet", "speech_ok", "linkedin_alerts"}
    assert pack.situation["quiet"] == "off"
    assert "Hi Allen" not in interview["pack_json"]  # typed facts, never the body
    assert (interview["judge_id"], interview["judge_version"], interview["level"]) == (
        "rule_judge",
        "1",
        "speak",
    )
    assert interview["reason"] == "rule: interview -> speak"
    assert set(interview["delivery"]) == {"spoken", "audio"}
    assert set(rows["m-offer"]["delivery"]) == {"suppressed", "audio"}  # the gap held the second
    assert (rows["m-receipt"]["level"], set(rows["m-receipt"]["delivery"])) == (
        "ledger",
        {"ledger_only"},
    )
    assert (rows["m-old"]["level"], rows["m-old"]["reason"]) == ("ledger", "older than 48 hours")

    # What Allen did with the card is appended to the decision as it arrives.
    alert = next(n for n in h.flat() if n["mail_kind"] == "rejection")
    h.client.post(f"/inherent/notices/{alert['id']}", json={"action": "seen"})
    h.client.post(
        f"/inherent/notices/{alert['id']}",
        json={"action": "feedback", "reaction": "right"},
    )
    (reject,) = [r for r in job_ledger.list_attention(h.db) if r["event_id"] == "m-reject"]
    assert set(reject["delivery"]) == {"shown"}
    assert [(f["level_shown"], f["reaction"]) for f in reject["feedback"]] == [("card", "right")]


def test_the_judge_is_the_one_seam_and_replay_runs_another_over_the_stored_packs(
    tmp_path: Path,
    jev: _Jev,
) -> None:
    """Delivery follows the injected judge; replay runs another judge over the stored packs."""

    def always_card(pack: ContextPack) -> Judgement:
        return Judgement("card", f"{pack.facts['kind']} is a card", "always_card", "0")

    h = _harness(tmp_path, jev, judge=always_card)
    h.job.poll_once()
    # Delivery followed the injected judge: even the receipt and the interview are plain cards.
    assert {level for (level,) in h.sql("SELECT level FROM job_alert")} == {"card"}
    assert len(h.sql("SELECT id FROM job_alert")) == 7
    assert h.spoken == 0

    rows = job_ledger.list_attention(h.db)
    assert {r["judge_id"] for r in rows} == {"always_card"}
    # Replay is pure: the rule table over the same packs, and nothing is written.
    before = h.sql("SELECT * FROM attention_log")
    again = replay(rule_judge_v1, rows)
    assert {r["event_id"]: j.level for r, j in zip(rows, again, strict=True)} == {
        "m-receipt": "ledger",
        "m-interview": "speak",
        "m-reject": "card",
        "m-offer": "speak",
        "m-digest": "card_sound",
        "m-followup": "card_sound",
        "m-old": "ledger",
    }
    assert h.sql("SELECT * FROM attention_log") == before


def test_the_audit_list_shows_the_newest_held_back_mail_at_any_probability(
    tmp_path: Path, jev: _Jev
) -> None:
    """The audit list is the newest 50 held-back mails, from any job-likelihood."""
    h = _harness(tmp_path, jev)
    h.job.poll_once()

    skipped = h.client.get("/inherent/jobs").json()["skipped"]
    assert {one["message_id"] for one in skipped} == {"m-promo", "m-fair", "m-news", "m-mom"}
    by_id = {one["message_id"]: one for one in skipped}
    assert {k: by_id["m-fair"][k] for k in ("sender_name", "sender_domain", "subject")} == {
        "sender_name": "UVic Events",
        "sender_domain": "uvic.example",
        "subject": "Career fair next week",
    }
    assert by_id["m-fair"]["p_job"] == pytest.approx(0.3)
    assert by_id["m-promo"]["p_job"] == pytest.approx(0.1)
    assert by_id["m-news"]["p_job"] == pytest.approx(0.03)  # under the old 0.2 bar, now listed
    assert all(one["received_at"] for one in skipped)

    # Fifty at most, newest first.
    for i in range(60):
        job_ledger.record_seen(
            h.db,
            f"x{i}",
            "not_job",
            NOW,
            p_job=0.01,
            audit={
                "received_at": (NOW + timedelta(minutes=60 - i)).isoformat(),
                "name": "n",
                "domain": "d",
                "subject": f"s{i}",
            },
        )
    skipped = h.client.get("/inherent/jobs").json()["skipped"]
    assert len(skipped) == 50
    assert skipped[0]["message_id"] == "x0"
    assert skipped[-1]["message_id"] == "x49"
    stamps = [one["received_at"] for one in skipped]
    assert stamps == sorted(stamps, reverse=True)


def test_every_decision_keeps_the_snapshot_jev_saw(tmp_path: Path, jev: _Jev) -> None:
    """A job, a not-job and an error each leave header and body snapshots in memory.db."""
    h = _harness(tmp_path, jev)
    h.job.poll_once()
    columns = (
        "stage, verdict, sender_name, sender_domain, subject, judge, probabilities, body_excerpt"
    )

    def snap(message_id: str, stage: str) -> tuple[Any, ...]:
        (row,) = h.sql(
            f"SELECT {columns} FROM job_decision WHERE message_id = ? AND stage = ?",  # noqa: S608
            message_id,
            stage,
        )
        return row

    # One header snapshot per mail, a body snapshot for the nine that were read.
    assert h.sql("SELECT count(*) FROM job_decision WHERE stage = 'header'") == [(len(MAILS),)]
    assert h.sql("SELECT count(*) FROM job_decision WHERE stage = 'body'") == [(len(MAILS) - 2,)]

    # A job: the header passed, the body was typed, and what Jev saw is kept.
    assert snap("m-offer", "header")[:6] == (
        "header",
        "pass",
        "Helix HR",
        "helix.example",
        "Offer of employment",
        "jev-1.13/header-v1",
    )
    offer = snap("m-offer", "body")
    assert offer[:2] == ("body", "job")
    assert offer[5] == "jev-1.13/body-v1"
    assert json.loads(offer[6])["offer"] == pytest.approx(0.99)
    assert offer[7] == "We are delighted to offer you the Backend Developer Intern position."

    # A not-job at the header never reaches the body question but its body is kept (ADR 0162).
    news = snap("m-news", "header")
    assert news[1] == "not_job"
    assert json.loads(news[6])["not_job"] == pytest.approx(0.97)
    assert news[7] == "Coffee news."
    assert h.sql("SELECT 1 FROM job_decision WHERE message_id = 'm-news' AND stage = 'body'") == []

    # A not-job at the body keeps the body Jev read.
    promo = snap("m-promo", "body")
    assert promo[1] == "not_job"
    assert promo[7] == "50% off"

    # The columns that never held an address still hold none.
    stored = json.dumps(h.sql("SELECT sender_name, sender_domain, subject FROM job_decision"))
    assert "@" not in stored

    # An error (Jev down) is a snapshot too, with no probabilities.
    jev.status = 500
    failing = _harness(tmp_path / "e", jev, [m for m in MAILS if m["id"] == "m-offer"])
    failing.job.poll_once()
    assert failing.sql(
        "SELECT stage, verdict, subject, probabilities, judge FROM job_decision"
    ) == [("header", "error", "Offer of employment", None, "jev-1.13/header-v1")]


def test_every_scanned_mail_keeps_its_sender_address_and_body_locally(
    tmp_path: Path, jev: _Jev
) -> None:
    """Header-dropped mail keeps name, address, subject, id, date and body, all local."""
    h = _harness(tmp_path, jev)
    h.job.poll_once()
    columns = (
        "message_id, stage, verdict, sender_name, sender_address, sender_domain, subject,"
        " received_at, body_excerpt, body_status"
    )
    rows = h.sql(f"SELECT {columns} FROM job_decision ORDER BY id")  # noqa: S608
    news = next(r for r in rows if r[0] == "m-news")
    assert news[1:7] == (
        "header",
        "not_job",
        "Weekly Brew",
        "hello@brew.example",
        "brew.example",
        "Our weekly newsletter",
    )
    assert news[7] == (NOW - timedelta(hours=3)).replace(microsecond=0).isoformat()
    assert news[8:] == ("Coffee news.", "read")
    # Every row of every scanned mail has its address, its body and the status 'read'.
    assert {r[0] for r in rows} == {one["id"] for one in MAILS}
    for row in rows:
        letter = next(one for one in MAILS if one["id"] == row[0])
        assert row[4] == letter["from"].split("<")[1].rstrip(">")
        assert row[8] == letter["body"]
        assert row[9] == "read"
    assert {r[1] for r in rows} == {"header", "body"}
    # The rule row (LinkedIn social news, held back unread by Jev) carries them too.
    social = _harness(tmp_path / "s", jev, LINKEDIN)
    social.job.poll_once()
    assert social.sql(
        "SELECT sender_address, body_excerpt, body_status FROM job_decision"
        " WHERE message_id = 'm-post' AND stage = 'rule'"
    ) == [("messages-noreply@linkedin.com", "Hi", "read")]

    # The address lives in job_decision only: not in the other tables, the routes or Jev's text.
    for table in ("job_mail", "job_seen", "job_alert", "job_feedback", "attention_log"):
        assert "@" not in json.dumps(h.sql(f"SELECT * FROM {table}"), ensure_ascii=False)  # noqa: S608
    routes = [h.client.get(path).text for path in ("/inherent/jobs", "/inherent/notices")]
    assert not any("@" in text for text in routes)
    assert not any("@" in request["state"] for request in jev.requests)
    assert not any(one["body"] in request["state"] for one in MAILS for request in jev.asked("job"))


def test_an_unreadable_body_is_unavailable_and_never_breaks_the_cycle(
    tmp_path: Path, jev: _Jev
) -> None:
    """A failed body read leaves body NULL and 'unavailable'; the triage verdict is unchanged."""
    h = _harness(tmp_path, jev)
    h.gmail.full_failures = {"m-news", "m-offer"}
    h.job.poll_once()
    news = h.sql(
        "SELECT verdict, sender_address, body_excerpt, body_status FROM job_decision"
        " WHERE message_id = 'm-news'"
    )
    # Held back by the header alone: kept as not_job, not an error, only its body is missing.
    assert news == [("not_job", "hello@brew.example", None, "unavailable")]
    assert h.sql("SELECT verdict FROM job_seen WHERE message_id = 'm-news'") == [("not_job",)]
    # A job mail whose body cannot be read cannot be typed: an error, as before, with a snapshot.
    assert h.sql("SELECT verdict FROM job_seen WHERE message_id = 'm-offer'") == [("error",)]
    assert h.sql(
        "SELECT stage, verdict, sender_address, body_excerpt, body_status FROM job_decision"
        " WHERE message_id = 'm-offer' ORDER BY id"
    ) == [
        ("header", "pass", "hr@helix.example", None, "unavailable"),
        ("body", "error", "hr@helix.example", None, "unavailable"),
    ]
    # The rest of the cycle went on: others were read, typed and kept.
    assert h.sql(
        "SELECT body_status FROM job_decision WHERE message_id = 'm-receipt' AND stage = 'body'"
    ) == [("read",)]


def test_a_mail_never_read_is_not_read_and_the_flag_keeps_its_body(
    tmp_path: Path, jev: _Jev
) -> None:
    """A mail whose header cannot be read has 'not_read'; a flag stores the address and body too."""
    h = _harness(tmp_path, jev)
    original = h.gmail.call

    def broken_header(server: str, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
        if tool == "gmail_get" and args["format"] == "metadata" and args["messageId"] == "m-news":
            return {"text": json.dumps({"error": "no such header"})}
        return original(server, tool, args)

    h.gmail.call = broken_header  # type: ignore[method-assign]
    h.job.poll_once()
    assert h.sql(
        "SELECT stage, verdict, sender_address, body_excerpt, body_status FROM job_decision"
        " WHERE message_id = 'm-news'"
    ) == [("header", "error", None, None, "not_read")]
    assert h.gmail.full_read_count("m-news") == 0

    h.gmail.call = original  # type: ignore[method-assign]
    assert h.client.post("/inherent/jobs/m-fair/flag", json={"reaction": "should_alert"}).is_success
    assert h.sql(
        "SELECT sender_address, body_excerpt, body_status FROM job_decision"
        " WHERE message_id = 'm-fair' ORDER BY id DESC LIMIT 1"
    ) == [("events@uvic.example", "Come and meet employers.", "read")]


def test_an_old_job_decision_table_gets_the_snapshot_columns(tmp_path: Path) -> None:
    """A memory.db made before ADR 0162 is upgraded in place and old rows stay readable."""
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(db)
    with conn:
        conn.execute(
            "CREATE TABLE job_decision (id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT,"
            " at TEXT, stage TEXT, sender_name TEXT, sender_domain TEXT, subject TEXT,"
            " received_at TEXT, probabilities TEXT, judge TEXT, body_excerpt TEXT, verdict TEXT)"
        )
        conn.execute("INSERT INTO job_decision (message_id, stage) VALUES ('old', 'header')")
    conn.close()
    job_ledger.record_decision(
        db, "new", "header", "pass", NOW, body_status="read", body_excerpt="x"
    )
    job_ledger.record_decision(db, "newer", "header", "pass", NOW)
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT message_id, sender_address, body_status FROM job_decision ORDER BY id"
        ).fetchall() == [("old", None, None), ("new", None, "read"), ("newer", None, "not_read")]
    finally:
        conn.close()


def test_flagging_a_held_back_mail_makes_it_job_mail_once(tmp_path: Path, jev: _Jev) -> None:
    """A flag reads the mail again, types it past the skip, alerts under quiet, logs feedback."""
    h = _harness(tmp_path, jev)
    h.job.poll_once()
    h.quiet = "quiet"
    reads = len(h.gmail.calls)
    assert h.sql("SELECT 1 FROM job_mail WHERE message_id = 'm-fair'") == []

    reply = h.client.post("/inherent/jobs/m-fair/flag", json={"reaction": "should_alert"})
    assert reply.status_code == 200
    # Jev typed it not_job; his flag wins as job_other.
    assert h.sql("SELECT kind, company FROM job_mail WHERE message_id = 'm-fair'") == [
        ("job_other", "UVic Events")
    ]
    assert h.sql("SELECT verdict FROM job_seen WHERE message_id = 'm-fair'") == [("job",)]
    assert "m-fair" not in {
        one["message_id"] for one in h.client.get("/inherent/jobs").json()["skipped"]
    }
    # The quiet level still holds the sound off.
    notice = next(n for n in h.flat() if n["company"] == "UVic Events")
    assert notice["level"] == "card"
    assert h.sql("SELECT level FROM job_alert WHERE message_id = 'm-fair'") == [("card_sound",)]
    assert h.sql("SELECT reaction FROM job_feedback WHERE alert_id = 'm-fair'") == [
        ("flag:should_alert",)
    ]
    (log,) = h.sql("SELECT feedback_json FROM attention_log WHERE event_id = 'm-fair'")
    assert json.loads(log[0])[0]["reaction"] == "flag:should_alert"
    assert {t for t, _a in h.gmail.calls[reads:]} == {"gmail_get"}  # read, never changed

    # Flagging again changes nothing and reads nothing.
    after = len(h.gmail.calls)
    assert (
        h.client.post("/inherent/jobs/m-fair/flag", json={"reaction": "should_alert"}).status_code
        == 200
    )
    assert len(h.gmail.calls) == after
    assert h.sql("SELECT count(*) FROM job_alert WHERE message_id = 'm-fair'") == [(1,)]
    assert h.sql("SELECT count(*) FROM job_feedback WHERE reaction = 'flag:should_alert'") == [(1,)]

    assert (
        h.client.post("/inherent/jobs/m-promo/flag", json={"reaction": "nope"}).status_code == 400
    )


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        ("needs_auth: token expired sk-secret-123", "job.health.auth"),
        ("request timed out", "job.health.timeout"),
        ("backend down", "job.health.down"),
    ],
)
def test_a_down_mail_channel_raises_one_alert_and_recovery_is_silent(
    tmp_path: Path,
    jev: _Jev,
    error: str,
    reason: str,
) -> None:
    """Three failed cycles raise one health alert per 12 hours under the quiet gate."""
    h = _harness(tmp_path, jev, [])
    h.gmail.search_error, h.gmail.search_failures = error, 4

    def fail() -> None:
        with pytest.raises(ToolError):
            h.job.poll_once()

    fail()
    fail()
    assert h.notices() == []  # two in a row is not yet down
    fail()
    (note,) = h.notices()
    assert note["kind"] == "mail"
    assert note["mail_kind"] == "health"
    assert note["level"] == "card_sound"
    assert note["title"] == lang.t("job.health.title")
    assert note["line"] == lang.t(reason)
    assert "secret" not in json.dumps(note)  # the reason is a fixed line, never the error text
    # It is the same quiet gate as the mail alerts.
    h.quiet = "quiet"
    assert [n["level"] for n in h.notices()] == ["card"]
    for level in ("no-pop", "dnd"):
        h.quiet = level
        assert h.notices() == []
    h.quiet = "off"

    fail()  # a fourth failure within 12 hours says nothing more
    h.job.poll_once()  # and the first good cycle is silent, but the old down card is done
    assert h.sql("SELECT state FROM job_alert") == [("done",)]
    assert h.notices() == []

    h.job.now = lambda: NOW + timedelta(hours=13)
    h.gmail.search_failures = 3
    for _ in range(3):
        fail()
    assert h.sql("SELECT state FROM job_alert ORDER BY created_at") == [("done",), ("pending",)]
    assert [n["mail_kind"] for n in h.notices()] == ["health"]


def test_a_recovery_does_not_touch_a_health_card_already_shown(tmp_path: Path, jev: _Jev) -> None:
    """Only a pending health card is cleared by a good cycle; a shown one keeps its state."""
    h = _harness(tmp_path, jev, [])
    h.gmail.search_error, h.gmail.search_failures = "backend down", 3
    for _ in range(3):
        with pytest.raises(ToolError):
            h.job.poll_once()
    (alert_id,) = (row[0] for row in h.sql("SELECT id FROM job_alert"))
    job_ledger.mark_alert(h.db, alert_id, "shown", NOW)
    h.job.poll_once()
    assert h.sql("SELECT state FROM job_alert") == [("shown",)]

    # ADR 0160: the health card keeps a snapshot, and his answer lands beside it.
    (snap,) = job_ledger.list_attention(h.db, "job_health")
    assert (snap["event_id"], snap["level"], snap["judge_id"]) == (
        "health",
        "card_sound",
        "card_rule",
    )
    assert ContextPack.from_json(snap["pack_json"]).facts["kind"] == "health"
    assert snap["delivery"].keys() >= {"shown"}
    h.client.post(f"/inherent/notices/{alert_id}", json={"action": "feedback", "reaction": "right"})
    (snap,) = job_ledger.list_attention(h.db, "job_health")
    assert [f["reaction"] for f in snap["feedback"]] == ["right"]


def test_two_hours_without_a_good_cycle_is_down_even_after_one_failure(
    tmp_path: Path,
    jev: _Jev,
) -> None:
    """Two hours without a good cycle is down even after one failure."""
    h = _harness(tmp_path, jev, [])
    h.gmail.search_failures = 1
    h.job.now = lambda: NOW + timedelta(hours=3)
    with pytest.raises(ToolError):
        h.job.poll_once()
    (note,) = h.notices()
    assert note["title"] == lang.t("job.health.title")


def test_on_speakers_sound_levels_are_silent_cards_and_the_log_says_why(
    tmp_path: Path,
    jev: _Jev,
) -> None:
    """Unprompted audio only on private output: speakers turn speak and card_sound into cards."""
    h = _harness(tmp_path, jev)
    h.device = dict(SPEAKERS)
    h.job.poll_once()

    assert h.spoken == 0
    levels = dict(h.sql("SELECT message_id, level FROM job_alert"))
    assert set(levels.values()) == {"card"}
    assert len(levels) == 5
    reply = h.client.get("/inherent/notices").json()
    assert reply["audio_private"] is False
    assert {n["level"] for n in reply["notices"]} == {"card"}
    rows = {r["event_id"]: r for r in job_ledger.list_attention(h.db, "job_mail")}
    # The judge's own level is kept; delivery says what the guard did and on which device.
    assert rows["m-interview"]["level"] == "speak"
    assert set(rows["m-interview"]["delivery"]) == {"audio", "silenced"}
    assert rows["m-interview"]["delivery"]["audio"] == {
        "device": "MacBook Pro Speakers",
        "private": False,
    }
    assert set(rows["m-digest"]["delivery"]) == {"audio", "silenced"}  # card_sound
    assert set(rows["m-reject"]["delivery"]) == set()  # a card would not have sounded
    assert "audio" not in rows["m-receipt"]["delivery"]


def test_on_private_output_the_levels_stay_and_the_log_names_the_device(
    tmp_path: Path,
    jev: _Jev,
) -> None:
    """On Bluetooth headphones speak and card_sound are served as before, with the device logged."""
    h = _harness(tmp_path, jev, speak_gap_s=0)
    h.job.poll_once()

    assert h.spoken == 1  # the offer is the third of the burst: no line
    reply = h.client.get("/inherent/notices").json()
    assert reply["audio_private"] is True
    assert {n["level"] for n in h.flat()} == {"speak", "card", "card_sound"}
    rows = {r["event_id"]: r for r in job_ledger.list_attention(h.db, "job_mail")}
    assert rows["m-interview"]["delivery"]["audio"] == {"device": "AirPods Pro", "private": True}
    assert rows["m-digest"]["delivery"]["audio"] == {"device": "AirPods Pro", "private": True}
    assert "silenced" not in rows["m-interview"]["delivery"]


def test_a_device_that_flips_to_speakers_before_the_line_is_said_means_no_speech(
    tmp_path: Path,
    jev: _Jev,
) -> None:
    """Private at decision time, speakers a moment later: the fresh look before speaking wins."""
    h = _harness(tmp_path, jev, [m for m in MAILS if m["id"] == "m-interview"])
    h.job.output = lambda fresh=False: dict(SPEAKERS if fresh else AIRPODS)
    h.job.poll_once()

    assert h.spoken == 0
    assert dict(h.sql("SELECT message_id, level FROM job_alert")) == {"m-interview": "speak"}
    (row,) = job_ledger.list_attention(h.db, "job_mail")
    assert set(row["delivery"]) == {"audio", "suppressed"}
    assert sum(spoken for (spoken,) in h.sql("SELECT spoken FROM job_alert")) == 0


def test_a_disconnect_between_polls_downgrades_what_the_route_serves(
    tmp_path: Path,
    jev: _Jev,
) -> None:
    """Alerts made on headphones are served as cards the moment the output is not private."""
    h = _harness(tmp_path, jev)
    h.job.poll_once()
    assert {n["level"] for n in h.flat()} == {"speak", "card", "card_sound"}

    h.device = dict(SPEAKERS)
    reply = h.client.get("/inherent/notices").json()
    assert reply["audio_private"] is False
    assert {n["level"] for n in reply["notices"]} == {"card"}

    # A digest and the items inside it are silent too.
    h.age_alerts(timedelta(minutes=10))
    (digest,) = h.client.get("/inherent/notices").json()["notices"]
    assert digest["kind"] == "digest"
    assert digest["level"] == "card"
    assert {item["level"] for item in digest["items"]} == {"card"}
    h.device = dict(AIRPODS)
    (digest,) = h.notices()
    assert digest["level"] == "card_sound"
    assert {item["level"] for item in digest["items"]} == {"speak", "card", "card_sound"}

    # A health alert (card_sound) is silenced the same way.
    health = _harness(tmp_path / "h", jev, [])
    health.gmail.search_error, health.gmail.search_failures = "backend down", 3
    for _ in range(3):
        with pytest.raises(ToolError):
            health.job.poll_once()
    assert [n["level"] for n in health.notices()] == ["card_sound"]
    health.device = dict(SPEAKERS)
    reply = health.client.get("/inherent/notices").json()
    assert [n["level"] for n in reply["notices"]] == ["card"]


# --- ADR 0187: glow, the lightest level that reaches Allen -------------------------------------
# No rule judges glow yet, so these drive it with a judge that does: the judge is the one seam.


def glow_for_rejections(pack: ContextPack) -> Judgement:
    """The rule table, except that a fresh rejection is a glow."""
    judged = rule_judge_v1(pack)
    if pack.facts.get("kind") == "rejection" and judged.level == "card":
        return Judgement("glow", "test: a rejection is a glow", "glow_test", "0")
    return judged


def _always_glow(pack: ContextPack) -> Judgement:
    return Judgement("glow", f"test: {pack.facts['kind']} is a glow", "glow_test", "0")


def _only(*ids: str) -> list[dict[str, Any]]:
    return [m for m in MAILS if m["id"] in ids]


def test_the_level_lists_agree_on_glow() -> None:
    """Glow sits between ledger and card in every list that names the levels."""
    assert LEVELS == ("ledger", "glow", "card", "card_sound", "speak")
    assert tuple(level for level in LEVELS if level != "ledger") == job_ledger.LEVELS
    assert set(LEVELS) <= set(CARD_LEVELS)  # whatever a judge says, Allen may rate
    assert len(lang.JOB_LEVEL_NAMES) == len(LEVELS)  # 记下, 亮一下, 卡片, 卡片带声, 开口


def test_a_glow_judgement_is_an_alert_served_as_a_glow(tmp_path: Path, jev: _Jev) -> None:
    """A glow is logged as judged, makes a pending alert (not ledger_only) and nothing else."""
    h = _harness(tmp_path, jev, _only("m-reject"), judge=glow_for_rejections)
    h.job.poll_once()

    assert h.sql("SELECT message_id, level, state FROM job_alert") == [
        ("m-reject", "glow", "pending")
    ]
    (row,) = job_ledger.list_attention(h.db, "job_mail")
    assert (row["level"], row["judge_id"], row["reason"]) == (
        "glow",
        "glow_test",
        "test: a rejection is a glow",
    )
    assert row["delivery"] == {}  # not ledger_only, not held, no device noted, nothing said
    (notice,) = h.notices()
    assert (notice["kind"], notice["level"], notice["mail_kind"], notice["company"]) == (
        "mail",
        "glow",
        "rejection",
        "Orbital",
    )
    assert notice["text"] == f"{notice['title']} {notice['line']}"
    assert h.spoken == 0


@pytest.mark.parametrize("device", [AIRPODS, SPEAKERS], ids=["private", "speakers"])
def test_a_glow_never_sounds_never_speaks_and_is_never_folded(
    tmp_path: Path, jev: _Jev, device: dict[str, Any]
) -> None:
    """Every letter a glow, on any output with no speak gap: no line, no cue, no summary."""
    h = _harness(tmp_path, jev, judge=_always_glow, speak_gap_s=0)
    h.device = dict(device)
    h.job.poll_once()

    assert {level for (level,) in h.sql("SELECT level FROM job_alert")} == {"glow"}
    assert len(h.sql("SELECT id FROM job_alert")) == 7
    assert h.spoken == 0
    assert sum(spoken for (spoken,) in h.sql("SELECT spoken FROM job_alert")) == 0
    for row in job_ledger.list_attention(h.db, "job_mail"):
        assert row["delivery"] == {}, row["event_id"]  # no device noted, no line suppressed

    # Seven alerts made together, then all of them waiting: still seven glows, never a digest.
    for waited in (timedelta(0), timedelta(minutes=10)):
        h.age_alerts(waited)
        reply = h.client.get("/inherent/notices").json()
        assert reply["audio_private"] is device["private"]
        assert [(n["kind"], n["level"]) for n in reply["notices"]] == [("mail", "glow")] * 7


def test_a_glow_is_not_counted_by_the_speak_burst_rule(tmp_path: Path, jev: _Jev) -> None:
    """Three glows, then an interview: it is the first card alert in the window, so it speaks."""
    rejects = [
        _mail(
            f"rj{i}",
            "Orbital Careers <jobs@orbital.example>",
            "Update on your application",
            "We will not be moving forward with your application.",
        )
        for i in range(3)
    ]
    h = _harness(
        tmp_path, jev, [*rejects, *_only("m-interview")], judge=glow_for_rejections, speak_gap_s=0
    )
    h.job.poll_once()

    assert [level for (level,) in h.sql("SELECT level FROM job_alert ORDER BY rowid")] == [
        "glow",
        "glow",
        "glow",
        "speak",
    ]
    assert job_ledger.recent_alerts(h.db, h.job.now()) == 1
    assert h.spoken == 1
    # And the one card is its own notice, not a summary of the four.
    assert sorted((n["kind"], n["level"]) for n in h.notices()) == [("mail", "glow")] * 3 + [
        ("mail", "speak")
    ]


@pytest.mark.parametrize(
    ("quiet", "served"),
    [
        ("off", ["glow", "speak"]),
        ("quiet", ["card", "glow"]),  # quiet takes the sound off the card, the glow had none
        ("no-pop", ["glow"]),  # a mark, not a card: the card waits
        ("dnd", []),
    ],
)
def test_a_glow_is_a_mark_so_only_dnd_holds_it(
    tmp_path: Path, jev: _Jev, quiet: str, served: list[str]
) -> None:
    """A glow and an interview card made at off: what each quiet level serves, as what level."""
    h = _harness(tmp_path, jev, _only("m-reject", "m-interview"), judge=glow_for_rejections)
    h.job.poll_once()
    h.quiet = quiet
    assert sorted(n["level"] for n in h.notices()) == served
    assert {state for (state,) in h.sql("SELECT state FROM job_alert")} == {"pending"}


def test_a_glow_made_under_dnd_waits_and_comes_back_as_a_glow(tmp_path: Path, jev: _Jev) -> None:
    """Only dnd notes a glow as held; once dnd lifts it is served, long waited, still a glow."""
    h = _harness(tmp_path, jev, _only("m-reject"), judge=glow_for_rejections)
    h.quiet = "dnd"
    h.job.poll_once()
    (row,) = job_ledger.list_attention(h.db, "job_mail")
    assert set(row["delivery"]) == {"held"}
    assert h.notices() == []

    h.age_alerts(timedelta(minutes=10))
    for quiet in ("no-pop", "quiet", "off"):
        h.quiet = quiet
        assert [(n["kind"], n["level"]) for n in h.notices()] == [("mail", "glow")]

    # No-pop holds a card but not a glow: the card says held, the glow does not.
    for judge, held in ((glow_for_rejections, set()), (rule_judge_v1, {"held"})):
        other = _harness(tmp_path / judge.__name__, jev, _only("m-reject"), judge=judge)
        other.quiet = "no-pop"
        other.job.poll_once()
        (made,) = job_ledger.list_attention(other.db, "job_mail")
        assert set(made["delivery"]) == held


def test_a_glow_stays_served_until_it_is_seen_or_dismissed(tmp_path: Path, jev: _Jev) -> None:
    """Polling does not end a glow; seen and dismissed do, as for a card, and ignored follows."""
    h = _harness(tmp_path, jev, _only("m-reject"), judge=glow_for_rejections)
    h.job.poll_once()
    for _ in range(3):
        assert [n["level"] for n in h.notices()] == ["glow"]
    (notice,) = h.notices()
    seen = h.client.post(f"/inherent/notices/{notice['id']}", json={"action": "seen"})
    assert seen.status_code == 200
    assert h.notices() == []
    assert h.sql("SELECT state FROM job_alert") == [("shown",)]
    # Shown and left alone for 30 minutes: the daemon writes "ignored", at the level it was.
    h.sql(
        "UPDATE job_alert SET shown_at = ?",
        (NOW - timedelta(minutes=31)).isoformat(timespec="seconds"),
    )
    h.job.poll_once()
    assert h.sql("SELECT state FROM job_alert") == [("done",)]
    assert h.sql("SELECT reaction, level_shown FROM job_feedback") == [("ignored", "glow")]

    for action, body in (
        ("dismissed", {"action": "dismissed"}),
        ("right", {"action": "feedback", "reaction": "right"}),
        ("level", {"action": "feedback", "reaction": f"level:{lang.JOB_LEVEL_NAMES[1]}"}),
    ):
        gone = _harness(tmp_path / action, jev, _only("m-reject"), judge=glow_for_rejections)
        gone.job.poll_once()
        (shown,) = gone.notices()
        assert gone.client.post(f"/inherent/notices/{shown['id']}", json=body).status_code == 200
        assert gone.notices() == []
        assert gone.sql("SELECT state FROM job_alert") == [("done",)]
        assert gone.sql("SELECT level_shown, reaction FROM job_feedback") == [
            ("glow", body.get("reaction", "dismissed"))
        ]
        (logged,) = job_ledger.list_attention(gone.db, "job_mail")
        assert [f["level_shown"] for f in logged["feedback"]] == ["glow"]


def test_profile_activity_mail_is_held_back_unread_and_job_mail_is_not(
    tmp_path: Path, jev: _Jev
) -> None:
    """A profile-activity notice is a rule hold: not asked, not in the ledger, in 被拦下."""
    h = _harness(tmp_path, jev, PROFILE, linkedin_alerts="ledger_only")
    h.job.poll_once()

    assert dict(h.sql("SELECT message_id, verdict FROM job_seen")) == {
        "m-searches": "not_job",
        "m-views": "not_job",
        "m-alert": "job",
        "m-inmail": "job",
    }
    assert len(jev.asked("kind")) == 2  # only the digest and the InMail were typed
    assert {one["subject"] for one in h.client.get("/inherent/jobs").json()["skipped"]} == {
        "You appeared in 5 searches",
        "Your profile was viewed 3 times",
    }
    assert {m for (m,) in h.sql("SELECT message_id FROM job_mail")} == {"m-alert", "m-inmail"}
    assert (
        h.sql("SELECT message_id FROM job_alert WHERE message_id IN ('m-searches', 'm-views')")
        == []
    )
    assert (
        h.sql("SELECT judge, verdict FROM job_decision WHERE stage = 'rule'")
        == [("local-rule/social-v1", "not_job")] * 2
    )
    assert h.sql("SELECT kind FROM job_mail WHERE message_id = 'm-inmail'") == [("interview",)]


def test_the_repair_pass_hides_a_stored_profile_activity_row_and_rereads_the_cambio_row(
    tmp_path: Path, jev: _Jev
) -> None:
    """Both of today's misreads are mended offline, once, and a flagged mail is left alone."""
    h = _harness(tmp_path, jev, [])
    head = {
        "received_at": NOW.isoformat(),
        "name": "Maren Ingle",
        "domain": "app.bamboohr.com",
        "subject": "Cambio Earth - Update",
    }
    for key, name, domain, subject, kind, company in (
        (
            "cambio",
            "Maren Ingle",
            "app.bamboohr.com",
            "Cambio Earth - Update",
            "rejection",
            "Cambio Earth's QA",
        ),
        (
            "searches",
            "LinkedIn",
            "linkedin.com",
            "You appeared in 5 searches",
            "job_other",
            "LinkedIn",
        ),
        (
            "flagged",
            "LinkedIn",
            "linkedin.com",
            "You appeared in 9 searches",
            "job_other",
            "LinkedIn",
        ),
    ):
        job_ledger.upsert_mail(
            h.db,
            {
                "message_id": key,
                "received_at": NOW.isoformat(),
                "sender_name": name,
                "sender_domain": domain,
                "subject": subject,
                "kind": kind,
                "company": company,
                "role": "",
            },
            NOW,
        )
        job_ledger.record_seen(h.db, key, "job", NOW, p_job=0.8)
        job_ledger.create_alert(h.db, key, "card_sound", "t", "l", NOW)
    job_ledger.record_decision(
        h.db, "cambio", "body", "job", NOW, head=head, body_excerpt=CAMBIO_BODY, body_status="read"
    )
    job_ledger.add_flag(h.db, "flagged", NOW)

    assert repair(h.db, "ledger_only") > 0
    assert h.sql("SELECT company, role, deleted FROM job_mail WHERE message_id = 'cambio'") == [
        ("Cambio Earth", "QA & Test Automation Developer Co-op", 0)
    ]
    assert h.sql("SELECT deleted FROM job_mail WHERE message_id = 'searches'") == [(1,)]
    assert h.sql("SELECT deleted FROM job_mail WHERE message_id = 'flagged'") == [(0,)]
    assert h.sql("SELECT verdict FROM job_seen WHERE message_id = 'searches'") == [("not_job",)]
    assert h.sql(
        "SELECT stage, verdict, judge FROM job_decision WHERE message_id = 'searches'"
    ) == [("rule", "not_job", "local-rule/social-v1")]
    assert "You appeared in 5 searches" in {
        one["subject"] for one in h.client.get("/inherent/jobs").json()["skipped"]
    }
    pending = {m for (m,) in h.sql("SELECT message_id FROM job_alert WHERE state = 'pending'")}
    assert pending == {"cambio", "flagged"}  # the hidden row's alert ended as done
    assert repair(h.db, "ledger_only") == 0
    assert h.gmail.calls == []


def test_the_search_pages_back_to_a_fixed_day_and_stops_at_a_cycles_worth(
    tmp_path: Path, jev: _Jev
) -> None:
    """``backfill_since`` replaces the window; pages are read until a cycle's worth is unseen."""
    h = _harness(tmp_path, jev, max_messages_per_cycle=5, backfill_since=date(2026, 9, 10))
    h.gmail.page_size = 4
    h.job.poll_once()

    searches = h.gmail.searches()
    assert [one.get("pageToken") for one in searches] == [None, "4"]  # 4 + 4 unseen: it stops
    assert {one["query"] for one in searches} == {"after:2026/09/10 -in:sent -in:drafts"}
    assert h.gmail.full_reads() == {one["id"] for one in MAILS[:5]}  # newest first, five only
    assert len(h.sql("SELECT 1 FROM job_seen")) == 5

    h.job.poll_once()  # the first page is seen now: this cycle reads on to the pages after it
    assert [one.get("pageToken") for one in h.gmail.searches()[2:]] == [None, "4", "8"]
    assert len(h.sql("SELECT 1 FROM job_seen")) == 10  # five more; the eleventh waits
    h.job.poll_once()
    assert len(h.sql("SELECT 1 FROM job_seen")) == len(MAILS)


def test_a_linkedin_application_sent_mail_is_job_mail_and_the_rest_of_linkedin_is_not(
    tmp_path: Path, jev: _Jev
) -> None:
    """The Easy Apply confirmation is typed and named by its subject; repair does not hide it."""
    sent = _mail(
        "sent",
        "LinkedIn <jobs-noreply@linkedin.com>",
        "Allen, your application was sent to Cambio Earth",
        "Your application was sent to Cambio Earth.",
    )
    assert triage.is_application_sent("linkedin.com", sent["subject"])
    assert triage.is_application_sent("mail.linkedin.com", "Your application was sent to X Ltd.")
    assert not triage.is_application_sent("cgi.com", sent["subject"])
    assert not triage.is_application_sent("linkedin.com", "Jane, interview for the Co-op")
    assert triage.company_of("LinkedIn", "linkedin.com", sent["subject"]) == "Cambio Earth"
    h = _harness(tmp_path, jev, [sent, LINKEDIN[5]], exclude_domains=("linkedin.com",))
    h.job.poll_once()

    assert h.sql("SELECT message_id, kind, company FROM job_mail") == [
        ("m-sent", "receipt", "Cambio Earth")
    ]
    assert [one["message_id"] for one in h.client.get("/inherent/jobs").json()["skipped"]] == [
        "m-inmail"
    ]

    # An old row (company "LinkedIn") is renamed, not hidden; another LinkedIn row still is.
    for key, subject in (("old1", sent["subject"]), ("old2", "Jane, interview for the Co-op")):
        mail = {
            "message_id": key,
            "received_at": NOW.isoformat(),
            "sender_name": "LinkedIn",
            "sender_domain": "linkedin.com",
            "subject": subject,
            "kind": "receipt",
            "company": "LinkedIn",
            "role": "",
        }
        job_ledger.upsert_mail(h.db, mail, NOW)
    assert repair(h.db, "ledger_only", ("linkedin.com",)) > 0
    assert h.sql("SELECT message_id, company, deleted FROM job_mail ORDER BY message_id") == [
        ("m-sent", "Cambio Earth", 0),
        ("old1", "Cambio Earth", 0),
        ("old2", "LinkedIn", 1),
    ]
    assert repair(h.db, "ledger_only", ("linkedin.com",)) == 0


def _stored(  # noqa: PLR0913 - a ledger row's fields
    h: _Harness, key: str, company: str, role: str, kind: str, age: timedelta, event_at: str = ""
) -> None:
    job_ledger.upsert_mail(
        h.db,
        {
            "message_id": key,
            "received_at": (NOW - age).isoformat(),
            "sender_name": company,
            "sender_domain": "x.example",
            "subject": f"{kind} {key}",
            "kind": kind,
            "company": company,
            "role": role,
            "event_at": event_at or None,
        },
        NOW,
    )


def _apps(h: _Harness) -> dict[tuple[str, str], dict[str, Any]]:
    reply = h.client.get("/inherent/jobs").json()
    assert reply["ledger"] is not None  # older clients keep reading it
    return {(one["company"], one["role"]): one for one in reply["applications"]}


def test_applications_group_by_company_and_the_status_follows_the_mail(
    tmp_path: Path, jev: _Jev
) -> None:
    """Receipt, then interview is interviewing; one company is one application, whatever role."""
    h = _harness(tmp_path, jev, [])
    soon = (NOW + timedelta(days=2)).isoformat()
    _stored(h, "a1", "Acme", "QA Co-op", "receipt", timedelta(days=10))
    _stored(h, "a2", "Acme", "QA Co-op", "interview", timedelta(days=5), event_at=soon)
    _stored(h, "a3", "ACME", "", "job_other", timedelta(days=4))  # no role: joins QA Co-op
    _stored(h, "b1", "Beta", "", "receipt", timedelta(days=22))  # silent for over 21 days
    _stored(h, "c1", "Gamma", "Dev", "receipt", timedelta(days=2))
    _stored(h, "c2", "Gamma", "Platform Dev", "receipt", timedelta(days=2))  # the longest role
    _stored(h, "d1", "Delta", "", "receipt", timedelta(days=3))
    _stored(h, "d2", "Delta", "", "offer", timedelta(days=2))
    _stored(h, "d3", "Delta", "", "receipt", timedelta(days=1))  # a later receipt: applied again
    _stored(h, "c3", "Gamma", "", "rejection", timedelta(days=1))
    h.client.post("/inherent/jobs/c3/delete")  # a deleted mail is not counted
    _stored(h, "e1", "Epsilon", "", "receipt", timedelta(days=1))

    apps = _apps(h)
    acme = apps[("ACME", "QA Co-op")]  # the newest mail names the company; case does not split it
    assert (acme["status"], acme["status_auto"], acme["count"]) == ("interviewing", True, 3)
    assert acme["source"] == "mail"
    assert acme["applied_at"] == (NOW - timedelta(days=10)).isoformat()
    assert acme["last_at"] == (NOW - timedelta(days=4)).isoformat()
    assert acme["next_event_at"] == soon
    assert [m["message_id"] for m in acme["mails"]] == ["a3", "a2", "a1"]  # newest first
    assert set(acme["mails"][0]) == {
        "message_id",
        "thread_id",
        "kind",
        "received_at",
        "subject",
        "event_at",
        "event_text",
    }
    assert apps[("Beta", "")]["status"] == "no_reply"
    gamma = apps[("Gamma", "Platform Dev")]  # three roles read are one row; the deleted mail is out
    assert (gamma["status"], gamma["count"]) == ("applied", 2)
    assert apps[("Delta", "")]["status"] == "applied"
    assert [one["status"] for one in h.client.get("/inherent/jobs").json()["applications"]] == [
        "interviewing",
        "applied",
        "applied",
        "applied",
        "no_reply",
    ]

    _stored(h, "c4", "Gamma", "", "rejection", timedelta(days=1))
    apps = _apps(h)
    assert apps[("Gamma", "Platform Dev")]["status"] == "rejected"
    assert list(apps.values())[-1]["status"] == "rejected"  # rejected sorts last


def test_the_repair_pass_clears_a_role_that_is_the_company_and_cleans_a_kept_one(
    tmp_path: Path, jev: _Jev
) -> None:
    """Later/Later and Planview/Planview lose the role; a kept role is cleaned as read."""
    h = _harness(tmp_path, jev, [])
    for key, name, domain, subject, company, role in (
        (
            "a",
            "Greenhouse",
            "us.greenhouse-mail.io",
            "Security code for your application to Later",
            "Later",
            "Later",
        ),
        (
            "b",
            "Planview",
            "talent.icims.com",
            "Thank you for applying at Planview",
            "Planview",
            "Planview",
        ),
        (
            "c",
            "RV Tech",
            "rivianvw.tech",
            "Thank you for applying",
            "RV Tech",
            "our Software Engineering Intern - Applications",
        ),
    ):
        mail = {
            "message_id": key,
            "received_at": NOW.isoformat(),
            "sender_name": name,
            "sender_domain": domain,
            "subject": subject,
            "kind": "receipt",
            "company": company,
            "role": role,
        }
        job_ledger.upsert_mail(h.db, mail, NOW)
    assert repair(h.db, "ledger_only") > 0
    assert h.sql("SELECT message_id, company, role FROM job_mail ORDER BY message_id") == [
        ("a", "Later", ""),
        ("b", "Planview", ""),
        ("c", "RV Tech", "Software Engineering Intern"),
    ]
    assert repair(h.db, "ledger_only") == 0


def test_reliable_controls_mails_are_one_application_and_an_ats_name_joins_by_role(
    tmp_path: Path, jev: _Jev
) -> None:
    """Allen's real rows: five mails, four role spellings, one application; Bamboohr merges in."""
    h = _harness(tmp_path, jev, [])
    rc, role = "Reliable Controls", "Firmware QA Analyst Co-op"
    _stored(h, "r1", rc, role, "interview", timedelta(days=5))
    _stored(h, "r2", rc, "Firmware QA Co-op", "interview", timedelta(days=4))
    _stored(h, "r3", rc, "", "interview", timedelta(days=3))
    _stored(h, "r4", rc, "", "interview", timedelta(days=2))
    _stored(h, "t1", "Bamboohr", role, "job_other", timedelta(days=1))  # the ATS label, same role
    _stored(h, "l1", "Later", "Software Development Co-op", "receipt", timedelta(days=1))
    _stored(h, "l2", "Greenhouse", "Software Development Co-op", "job_other", timedelta(days=1))
    _stored(h, "x1", "Bamboohr", "Another Role", "job_other", timedelta(days=1))  # no match

    apps = _apps(h)
    one = apps[(rc, role)]
    assert (one["status"], one["count"]) == ("interviewing", 5)
    assert (one["company"], one["last_at"]) == (rc, (NOW - timedelta(days=1)).isoformat())
    assert apps[("Later", "Software Development Co-op")]["count"] == 2
    assert apps[("Bamboohr", "Another Role")]["count"] == 1
    assert len(apps) == 3

    # An edit row written when the application was keyed by company and role still applies.
    h.sql(
        "INSERT INTO job_application (id, company, role, source, note, hidden, created_at,"
        " updated_at) VALUES (?, ?, ?, 'edit', ?, 0, ?, ?)",
        "old-id-1",
        rc,
        "Firmware QA Co-op",
        "older",
        "2026-10-01T00:00:00+00:00",
        "2026-10-01T00:00:00+00:00",
    )
    h.sql(
        "INSERT INTO job_application (id, company, role, source, note, hidden, created_at,"
        " updated_at) VALUES (?, ?, ?, 'edit', ?, 0, ?, ?)",
        "old-id-2",
        rc.upper(),
        role,
        "newest",
        "2026-10-02T00:00:00+00:00",
        "2026-10-02T00:00:00+00:00",
    )
    assert _apps(h)[(rc, role)]["note"] == "newest"


def test_allens_own_rows_and_edits_go_through_the_routes(tmp_path: Path, jev: _Jev) -> None:
    """Add a manual row, edit it and a mail-derived one; a newer deciding mail beats an old edit."""
    h = _harness(tmp_path, jev, [])
    _stored(h, "e1", "Epsilon", "", "receipt", timedelta(days=3))
    _stored(h, "f1", "Zeta", "", "receipt", timedelta(days=3))
    post = h.client.post

    made = post(
        "/inherent/jobs/applications",
        json={"company": " Hand Co ", "role": "Dev", "applied_at": "2026-09-12", "note": "n"},
    )
    assert made.status_code == 200
    mine = _apps(h)[("Hand Co", "Dev")]
    assert mine["id"] == made.json()["id"]
    assert (mine["source"], mine["status"], mine["applied_at"], mine["count"]) == (
        "manual",
        "applied",
        "2026-09-12",
        0,
    )
    assert mine["status_auto"] is False
    assert mine["note"] == "n"
    for bad in (
        {"company": "X", "status": "withdrawn"},
        {"company": "  "},
        {"company": "X", "applied_at": "last week"},
    ):
        assert post("/inherent/jobs/applications", json=bad).status_code == 400
    assert len(_apps(h)) == 3

    edit = f"/inherent/jobs/applications/{mine['id']}"
    assert post(edit, json={"status": "offer", "note": "call back"}).status_code == 200
    assert (_apps(h)[("Hand Co", "Dev")]["status"], _apps(h)[("Hand Co", "Dev")]["note"]) == (
        "offer",
        "call back",
    )
    assert post(edit, json={"status": "maybe"}).status_code == 400
    assert post("/inherent/jobs/applications/nope", json={"status": "offer"}).status_code == 404

    # A mail-derived application gets an edit row; its status is his until a deciding mail comes.
    zeta = _apps(h)[("Zeta", "")]
    url = f"/inherent/jobs/applications/{zeta['id']}"
    assert post(url, json={"status": "bogus"}).status_code == 400
    assert h.sql("SELECT count(*) FROM job_application WHERE source = 'edit'") == [(0,)]
    assert post(url, json={"status": "rejected", "applied_at": "2026-09-11"}).status_code == 200
    again = _apps(h)[("Zeta", "")]
    assert (again["id"], again["status"], again["status_auto"]) == (zeta["id"], "rejected", False)
    assert (again["applied_at"], again["source"], again["count"]) == ("2026-09-11", "mail", 1)
    assert h.sql("SELECT source FROM job_application WHERE id = ?", zeta["id"]) == [("edit",)]
    _stored(h, "f2", "Zeta", "", "job_other", -timedelta(minutes=1))  # not a deciding kind
    assert _apps(h)[("Zeta", "")]["status"] == "rejected"
    assert post(url, json={"note": "chased"}).status_code == 200  # a note does not renew the status
    _stored(h, "f3", "Zeta", "", "interview", -timedelta(hours=1))  # newer than his edit
    newer = _apps(h)[("Zeta", "")]
    assert (newer["status"], newer["status_auto"]) == ("interviewing", True)
    assert newer["note"] == "chased"

    assert post(url, json={"hidden": True}).status_code == 200
    assert post(edit, json={"hidden": True}).status_code == 200
    assert set(_apps(h)) == {("Epsilon", "")}


_TEAMS_MAIL = """Hi Allen,

We would like to invite you to a virtual interview with Jill Crowe on Thursday, October 9
at 2:00 PM.
Join Microsoft Teams: https://teams.microsoft.com/l/meetup-join/19%3Ameeting_abc/0?context=x.
"""
_PORTAL_MAIL = """Thanks for applying. Check your status any time:
https://reliable.wd3.myworkdayjobs.com/en-US/careers/userHome
The posting: https://reliablecontrols.com/careers/firmware-qa-analyst-co-op
"""


def test_a_body_gives_the_interview_mode_platform_place_people_and_links() -> None:
    """ADR 0182: read from the words, left empty when the mail does not say."""
    teams = triage.mail_details(_TEAMS_MAIL)
    assert (teams["mode"], teams["platform"]) == ("online", "Teams")
    assert teams["join_url"] == (
        "https://teams.microsoft.com/l/meetup-join/19%3Ameeting_abc/0?context=x"
    )
    assert teams["interviewers"] == ["Jill Crowe"]
    assert teams["location"] is None

    onsite = triage.mail_details(
        "Your interview is in person.\nLocation: 120 Government Street, Victoria, BC\n"
        "Interviewers: Jill Crowe, Sam Lee and Pat Kim (hiring manager)"
    )
    assert (onsite["mode"], onsite["join_url"]) == ("onsite", None)
    assert onsite["location"] == "120 Government Street, Victoria, BC"
    assert onsite["interviewers"] == ["Jill Crowe", "Sam Lee", "Pat Kim"]
    # A street in a footer is not where the interview is.
    footer = triage.mail_details("Thanks for applying.\nReliable Controls, 120 Government Street")
    assert (footer["mode"], footer["location"]) == (None, None)

    # No names read, none made up: a team, an organisation, a platform and a day are not people.
    for text in (
        "We would like to schedule a virtual interview with our hiring team.",
        "Your interview with Reliable Controls is set.",
        "Your interview with Jill on Thursday.",
    ):
        assert triage.mail_details(text)["interviewers"] == []
    # Allen's real confirmation mail, 2026-10-06: "Teams meeting" names the platform.
    confirmed = triage.mail_details(
        "Thank you for confirming receipt of the Teams meeting invitation!\n"
        "We are looking forward to chatting with you on Thursday"
    )
    assert confirmed["platform"] == "Teams"
    assert triage.mail_details("Our teams will review it.")["platform"] is None
    assert triage.mail_details("Interview with Jill Crowe Thursday at 2pm.")["interviewers"] == [
        "Jill Crowe"
    ]

    links = triage.mail_details(_PORTAL_MAIL)
    assert links["portal_url"] == "https://reliable.wd3.myworkdayjobs.com/en-US/careers/userHome"
    assert links["posting_url"] == "https://reliablecontrols.com/careers/firmware-qa-analyst-co-op"
    posting_only = triage.mail_details(
        "See https://boards.greenhouse.io/acme/jobs/123 or https://jobs.lever.co/acme/abc"
    )
    assert posting_only["portal_url"] is None
    assert posting_only["posting_url"] == "https://boards.greenhouse.io/acme/jobs/123"

    # Only https counts, and a host that merely starts like a platform is not it.
    plain = triage.mail_details(
        "Join http://zoom.us/j/1 or https://zoom.us@evil.example/j/2 or http://x.example/careers/a"
    )
    assert (plain["join_url"], plain["posting_url"], plain["portal_url"]) == (None, None, None)
    assert plain["mode"] is None


def test_an_application_carries_its_timeline_interview_and_links(
    tmp_path: Path, jev: _Jev
) -> None:
    """The route gives each application its steps, interview and links from the kept bodies."""
    h = _harness(tmp_path, jev, [])
    rc = "Reliable Controls"
    soon = (NOW + timedelta(days=2)).isoformat()
    _stored(h, "r1", rc, "Firmware QA", "receipt", timedelta(days=9))
    _stored(h, "r2", rc, "Firmware QA", "interview", timedelta(days=5), event_at=soon)
    _stored(h, "r3", rc, "Firmware QA", "job_other", timedelta(days=1))
    # His reply and their confirmation in the invitation thread are not more invitations.
    _stored(h, "r4", rc, "Firmware QA", "interview", timedelta(days=4))
    _stored(h, "r5", rc, "Firmware QA", "interview", timedelta(days=3))
    for key, body in (("r1", _PORTAL_MAIL), ("r2", _TEAMS_MAIL), ("r3", "nothing here")):
        job_ledger.record_decision(
            h.db, key, "body", "job", NOW, head={"received_at": NOW.isoformat()}, body_excerpt=body
        )
    _stored(h, "z1", "Zed", "", "rejection", timedelta(days=4), event_at=soon)

    apps = _apps(h)
    one = apps[(rc, "Firmware QA")]
    assert [(s["kind"], s["future"]) for s in one["timeline"]] == [
        ("applied", False),
        ("interview_invite", False),
        ("interview", True),
    ]
    assert one["timeline"][0]["at"] == (NOW - timedelta(days=9)).isoformat()
    assert one["timeline"][2]["at"] == soon
    # Each step names the mail it came from; the interview is the mail whose event_at it is.
    assert [s["message_id"] for s in one["timeline"]] == ["r1", "r2", "r2"]
    assert one["interview"] == {
        "at": soon,
        "mode": "online",
        "platform": "Teams",
        "join_url": "https://teams.microsoft.com/l/meetup-join/19%3Ameeting_abc/0?context=x",
        "location": None,
        "interviewers": ["Jill Crowe"],
    }
    assert one["links"] == {
        "portal_url": "https://reliable.wd3.myworkdayjobs.com/en-US/careers/userHome",
        "posting_url": "https://reliablecontrols.com/careers/firmware-qa-analyst-co-op",
    }
    # A rejected application shows no interview still ahead, and no invitation means no details.
    zed = apps[("Zed", "")]
    assert [s["kind"] for s in zed["timeline"]] == ["applied", "rejection"]
    assert [s["message_id"] for s in zed["timeline"]] == ["z1", "z1"]
    assert zed["interview"] is None
    assert zed["links"] == {"portal_url": None, "posting_url": None}

    # A row he added by hand has only the day he applied.
    h.client.post(
        "/inherent/jobs/applications", json={"company": "Hand", "applied_at": "2026-09-12"}
    )
    hand = _apps(h)[("Hand", "")]
    assert hand["timeline"] == [
        {"kind": "applied", "at": "2026-09-12", "future": False, "message_id": None}
    ]
    assert hand["interview"] is None


_TEAMS_INVITE = """Hi Yilun,

You are invited to a Microsoft Teams meeting.

Thursday, October 8, 2026 2:00 PM (PDT)

Microsoft Teams meeting
Join: https://teams.microsoft.com/l/meetup-join/19%3Ameeting_abc/0?context=x
"""


def test_event_of_reads_a_bare_teams_time_line_only_for_a_known_interview() -> None:
    """A Teams invitation's time line has no event word: it counts when the mail is an interview."""
    received = NOW - timedelta(days=4)
    assert triage.event_of(_TEAMS_INVITE, received) == (None, None)
    sentence, at = triage.event_of(_TEAMS_INVITE, received, dated=True)
    assert sentence == "Thursday, October 8, 2026 2:00 PM (PDT)"
    assert at == "2026-10-08T14:00-07:00"
    # A line with a date but no clock is not a time, even for a known interview.
    assert triage.event_of("Sometime in October 8, 2026.", received, dated=True) == (None, None)


_RC_INVITE = """Thank you again for applying for the Firmware QA Analyst Co-op position \
at Reliable Controls.
We would like to invite you to an interview.

Date: Thursday October 8th
Time: 1:00pm - 2:00pm
Interview Panel: Myself, Matthew Clarkson; Firmware Manager, Logen De Bruyne; Firmware QA Analyst
Please reply to confirm that you\u2019ve received this invitation and that the proposed time \
works for you.
"""
_RC_REPLY = """Confirmed, thank you.

From: Jill Crowe <jill@reliablecontrols.com>
Sent: Friday, October 2, 2026 4:45 PM
To: Allen Shi <allen@example.com>
Subject: Re: Invitation to Interview

From: Jill Crowe <jill@reliablecontrols.com>
Date: Friday, October 2, 2026 4:45 PM
Subject: Invitation to Interview
> Sent: Friday, October 2, 2026 4:45 PM
"""
_RC_RECEIVED = datetime(2026, 10, 2, 9, 0, tzinfo=ZoneInfo("America/Vancouver"))


@pytest.fixture
def vancouver(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A bare time is the machine's clock: pin it to Allen's."""
    monkeypatch.setenv("TZ", "America/Vancouver")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.mark.usefixtures("vancouver")
def test_event_of_reads_the_date_and_time_lines_and_never_a_quoted_header() -> None:
    """A labelled Date and Time is the interview; a quoted Sent header or quote line is not."""
    for dated in (False, True):
        sentence, at = triage.event_of(_RC_INVITE, _RC_RECEIVED, dated=dated)
        assert at == "2026-10-08T13:00-07:00"
        assert sentence == "Thursday October 8th, 1:00pm - 2:00pm"
    assert triage.event_of("When: Oct 8 at 13:00 PDT\nCall me.", _RC_RECEIVED, dated=True)[1] == (
        "2026-10-08T13:00-07:00"
    )
    # A mail that is no interview and names no event takes no Date and Time lines.
    assert triage.event_of("Date: October 8\nTime: 1pm", _RC_RECEIVED) == (None, None)
    # The old mail's header gives no event in either mode; the reply holds nothing else.
    for dated in (False, True):
        assert triage.event_of(_RC_REPLY, _RC_RECEIVED, dated=dated) == (None, None)
    both = _RC_REPLY + _RC_INVITE.split("\n\n", 1)[1]
    assert triage.event_of(both, _RC_RECEIVED, dated=True)[1] == "2026-10-08T13:00-07:00"


def test_a_range_with_one_am_pm_starts_at_its_start() -> None:
    """``1:00 - 2:00pm`` is 1 PM, not 2 PM; ``11 - 1pm`` is 11 AM."""
    for sentence, at in (
        ("Would 1:00 - 2:00pm on Tuesday October 13th work for you?", "2026-10-13T13:00-07:00"),
        ("Interview 11 - 1pm Tuesday October 13th", "2026-10-13T11:00-07:00"),
        ("Interview 10:30 to 11:30am Tuesday October 13th", "2026-10-13T10:30-07:00"),
        ("Interview 1:00pm - 2:00pm Tuesday October 13th", "2026-10-13T13:00-07:00"),
        ("Interview at 2pm Tuesday October 13th", "2026-10-13T14:00-07:00"),
    ):
        assert triage.event_of(sentence, _RC_RECEIVED, dated=True)[1] == at, sentence
    labelled = "Date: Tuesday October 13th\nTime: 1:00 - 2:00pm"
    assert triage.event_of(labelled, _RC_RECEIVED, dated=True)[1] == "2026-10-13T13:00-07:00"


def test_interviewers_of_a_panel_line_are_persons_not_titles() -> None:
    """Myself is the sender (not known here, so left out); a role item is dropped."""
    people = triage.mail_details(_RC_INVITE)["interviewers"]
    assert people == ["Matthew Clarkson", "Logen De Bruyne"]


@pytest.mark.usefixtures("vancouver")
def test_the_repair_pass_corrects_a_wrong_event_time_from_the_kept_body(
    tmp_path: Path, jev: _Jev
) -> None:
    """An interview row's event is read again from its body: replaced, or cleared if none."""
    h = _harness(tmp_path, jev, [])
    wrong = "2026-10-02T16:45-07:00"
    for key, kind in (("fix", "interview"), ("none", "interview"), ("other", "rejection")):
        job_ledger.upsert_mail(
            h.db,
            {
                "message_id": key,
                "received_at": _RC_RECEIVED.isoformat(),
                "sender_name": "Jill Crowe",
                "sender_domain": "reliablecontrols.com",
                "subject": "Invitation to Interview",
                "kind": kind,
                "company": "Reliable Controls",
                "role": "Firmware QA Analyst Co-op",
                "event_at": wrong,
            },
            NOW,
        )
        job_ledger.set_event(h.db, key, wrong, "Sent: Friday, October 2, 2026 4:45 PM")
    body = {"fix": _RC_INVITE, "none": "See you soon.", "other": _RC_INVITE}
    for key, text in body.items():
        job_ledger.record_decision(
            h.db, key, "body", "job", NOW, head={"received_at": NOW.isoformat()}, body_excerpt=text
        )
    assert repair(h.db, "ledger_only") == 2  # fix and none
    assert h.sql("SELECT message_id, event_at, event_text FROM job_mail ORDER BY message_id") == [
        ("fix", "2026-10-08T13:00-07:00", "Thursday October 8th, 1:00pm - 2:00pm"),
        ("none", None, None),  # the rules read no time from the body: the quoted one goes
        ("other", wrong, "Sent: Friday, October 2, 2026 4:45 PM"),  # not interview or offer
    ]
    assert repair(h.db, "ledger_only") == 0
    assert h.gmail.calls == []


def _bodyless(h: _Harness, key: str, kind: str, **fields: Any) -> None:  # noqa: ANN401
    _stored(h, f"m-{key}", "Reliable Controls", "Firmware QA", kind, timedelta(days=4), **fields)


def test_old_interview_mail_without_a_kept_body_is_read_again_once(
    tmp_path: Path, jev: _Jev
) -> None:
    """ADR 0184: one gmail_get, the body kept as a reread row, the time filled, never twice."""
    mails = [
        _mail(key, "Jill <jill@rc.example>", f"Interview {key}", _TEAMS_INVITE)
        for key in ("rc", "kept", "offer", "reject", "gone")
    ]
    h = _harness(tmp_path, jev, mails)
    _bodyless(h, "rc", "interview")
    _bodyless(h, "kept", "interview")
    job_ledger.record_decision(
        h.db, "m-kept", "body", "job", NOW, head={"received_at": NOW.isoformat()}, body_excerpt="x"
    )
    _bodyless(h, "offer", "offer", event_at="2026-11-01T10:00:00+00:00")  # has a time already
    _bodyless(h, "reject", "rejection")  # not interview or offer
    _bodyless(h, "gone", "interview")
    job_ledger.update_mail(h.db, "m-gone", {"deleted": 1})  # hidden: not read

    assert h.job.reread() == 2
    assert h.gmail.full_reads() == {"m-rc", "m-offer"}
    assert h.gmail.tools() == {"gmail_get"}
    rows = h.sql("SELECT message_id, stage, verdict, judge, body_status FROM job_decision")
    assert sorted(r for r in rows if r[0] != "m-kept") == [
        ("m-offer", "reread", "job", "local-reread/body-v1", "read"),
        ("m-rc", "reread", "job", "local-reread/body-v1", "read"),
    ]
    assert job_ledger.body_excerpt(h.db, "m-rc").startswith("Hi Yilun")
    event = "SELECT event_at, event_text FROM job_mail WHERE message_id = ?"
    assert h.sql(event, "m-rc") == [
        ("2026-10-08T14:00-07:00", "Thursday, October 8, 2026 2:00 PM (PDT)")
    ]
    assert h.sql(event, "m-offer") == [("2026-11-01T10:00:00+00:00", None)]  # never overwritten
    # A reread row is no verdict: nothing is held back, alerted or listed because of it.
    assert h.sql("SELECT count(*) FROM job_seen") == [(0,)]
    assert h.sql("SELECT count(*) FROM job_alert") == [(0,)]
    assert h.client.get("/inherent/jobs").json()["skipped"] == []

    one = _apps(h)[("Reliable Controls", "Firmware QA")]["interview"]
    assert one is not None
    assert (one["platform"], one["mode"]) == ("Teams", "online")
    assert one["join_url"].startswith("https://teams.microsoft.com/l/meetup-join/")

    before = len(h.gmail.calls)
    assert h.job.reread() == 0  # every one has a kept body now
    assert len(h.gmail.calls) == before


def test_a_failed_or_impossible_reread_is_skipped_and_the_poller_goes_on(
    tmp_path: Path, jev: _Jev
) -> None:
    """A Gmail failure on one mail is logged and the next is read; no Gmail means no reads."""
    mails = [_mail(k, "Jill <jill@rc.example>", "Interview", _TEAMS_INVITE) for k in ("bad", "ok")]
    h = _harness(tmp_path, jev, mails)
    _bodyless(h, "bad", "interview")
    _bodyless(h, "ok", "interview")
    h.gmail.full_failures = {"m-bad"}

    assert h.job.reread() == 1
    assert h.sql("SELECT message_id FROM job_decision") == [("m-ok",)]
    assert h.job.reread() == 0  # the failed one is tried again at the next start, nothing else
    assert h.gmail.full_read_count("m-bad") == 2
    assert h.gmail.full_read_count("m-ok") == 1

    h.job._connections = _Connections(None)  # type: ignore[assignment]  # noqa: SLF001
    assert h.job.reread() == 0


def test_the_loop_reads_old_bodies_after_the_repair_and_a_start_reads_at_most_twenty(
    tmp_path: Path, jev: _Jev, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run loop calls the re-read once at start; the cap keeps a start small."""
    monkeypatch.delenv("OPENROUTER_API_KEY")  # the polls do nothing: only the re-read reads
    keys = [f"{i:02d}" for i in range(23)]
    h = _harness(
        tmp_path,
        jev,
        [_mail(k, "Jill <jill@rc.example>", "Interview", _TEAMS_INVITE) for k in keys],
        poll_s=0.05,
    )
    for key in keys:
        _bodyless(h, key, "interview")

    async def run() -> None:
        task = asyncio.create_task(h.job.run())
        for _ in range(100):
            if len(h.gmail.full_reads()) >= 20:
                break
            await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert len(h.gmail.full_reads()) == 20
    assert h.sql("SELECT count(*) FROM job_decision WHERE stage = 'reread'") == [(20,)]


# --- ADR 0186: reminders and an Outlook event for each interview time -----------------------

_VANCOUVER = ZoneInfo("America/Vancouver")
_TUE_NOON = datetime(2026, 10, 6, 12, 0, tzinfo=_VANCOUVER)  # two days before the interview
_INTERVIEW = "2026-10-08T13:00-07:00"
_RANGE = "Thursday October 8th, 1:00pm - 2:30pm"


def _reminding(
    tmp_path: Path, jev: _Jev, *, event_text: str = _RANGE, **settings: Any  # noqa: ANN401
) -> _Harness:
    """A harness with interview reminders on, a fixed clock and Reliable Controls' invitation."""
    h = _harness(
        tmp_path,
        jev,
        [],
        outlook=_Outlook(),
        interview_reminders=InterviewSettings(evening_at=clock(20, 0), before_min=30, outlook=True),
        **settings,
    )
    assert h.job.interviews is not None
    h.job.now = lambda: _TUE_NOON.astimezone(UTC)
    h.job.interviews.zone = "America/Vancouver"
    _stored(
        h, "m-rc", "Reliable Controls", "Firmware QA", "interview", timedelta(days=4),
        event_at=_INTERVIEW,
    )
    h.sql("UPDATE job_mail SET event_text = ?", event_text)
    job_ledger.record_decision(
        h.db, "m-rc", "body", "job", NOW, head={"received_at": NOW.isoformat()},
        body_excerpt=_TEAMS_INVITE,
    )
    return h


def _rung(h: _Harness) -> list[reminder_state.Reminder]:
    """Every reminder ever scheduled in the log, with what became of it."""
    with contextlib.closing(open_runtime_event_log(h.event_log)) as conn:
        return list(reminder_state.fold(conn).values())


def _utc(text: str) -> int:
    return int(datetime.fromisoformat(text).timestamp() * 1000)


_EVENT = {
    "subject": "面试：Reliable Controls — Firmware QA",  # noqa: RUF001 - Allen's own wording
    "start": {"dateTime": "2026-10-08T13:00:00", "timeZone": "America/Vancouver"},
    "end": {"dateTime": "2026-10-08T14:30:00", "timeZone": "America/Vancouver"},
    "body": {
        "contentType": "text",
        "content": "平台：Teams\n"  # noqa: RUF001 - Allen's own wording
        "链接：https://teams.microsoft.com/l/meetup-join/19%3Ameeting_abc/0?context=x\n"  # noqa: RUF001
        "由 Jarvis 根据面试邮件添加。",
    },
    "isReminderOn": True,
    "reminderMinutesBeforeStart": 30,
    "isOnlineMeeting": False,
}


def test_an_interview_time_arms_two_reminders_and_one_outlook_event_once(
    tmp_path: Path, jev: _Jev
) -> None:
    """The evening before at 20:00 and 30 minutes ahead, one event; a second pass does nothing."""
    h = _reminding(tmp_path, jev)
    assert h.outlook is not None

    h.job.remind()

    evening, before = sorted(_rung(h), key=lambda r: r.due_at_ms)
    assert evening.due_at_ms == _utc("2026-10-07T20:00:00-07:00")
    assert before.due_at_ms == _utc("2026-10-08T12:30:00-07:00")
    assert evening.due_at_local == "2026-10-07T20:00:00-07:00"
    assert evening.text == "明天下午1点整 Reliable Controls 面试，线上 Teams。"  # noqa: RUF001
    assert before.text == "30 分钟后 Reliable Controls 面试，Teams 链接在 Jobs 页。"  # noqa: RUF001
    assert h.outlook.calls == [("create-calendar-event", {"body": _EVENT})]
    app = _apps(h)[("Reliable Controls", "Firmware QA")]
    assert app["reminders"] == {
        "at": _INTERVIEW,
        "evening": True,
        "before": True,
        "evening_at": "20:00",
        "before_min": 30,
        "outlook": True,
        "cancelled": False,
    }

    h.job.remind()
    h.job.remind()
    assert len(_rung(h)) == 2
    assert all(one.pending for one in _rung(h))
    assert h.outlook.tools() == ["create-calendar-event"]


def test_an_interview_with_no_end_runs_an_hour_and_the_text_is_english_in_english(
    tmp_path: Path, jev: _Jev, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No range in the invitation: start plus 60 minutes; the words follow Allen's language."""
    monkeypatch.setattr(lang, "_current", "en")
    h = _reminding(tmp_path, jev, event_text="Thursday, October 8, 2026 1:00 PM (PDT)")
    assert h.outlook is not None

    h.job.remind()

    event = h.outlook.calls[0][1]["body"]
    assert event["end"]["dateTime"] == "2026-10-08T14:00:00"
    assert event["subject"] == "Interview: Reliable Controls — Firmware QA"
    evening, before = sorted(_rung(h), key=lambda r: r.due_at_ms)
    assert evening.text == "Tomorrow at 1 PM: interview with Reliable Controls, online Teams."
    assert before.text == (
        "In 30 minutes: interview with Reliable Controls, the Teams link is on the Jobs page."
    )


def test_event_minutes_reads_only_a_clear_range() -> None:
    """A range of two clocks is its length; one clock, a zone or a nonsense range is None."""
    for sentence, minutes in (
        ("Thursday October 8th, 1:00pm - 2:00pm", 60),
        ("Thursday, 10:00 AM \u2013 11:30 AM PST", 90),
        ("October 8 from 9am to 10am", 60),
        ("October 8, 13:00 - 14:15", 75),
        ("October 8, 2:00 PM (PDT)", None),
        ("October 8, 3:00pm - 2:00pm", None),
    ):
        assert triage.event_minutes(sentence) == minutes, sentence


def test_a_changed_time_cancels_the_old_reminders_and_moves_the_event(
    tmp_path: Path, jev: _Jev
) -> None:
    """New time: the two old reminders are cancelled, two new ones set, the one event updated."""
    h = _reminding(tmp_path, jev)
    assert h.outlook is not None
    h.job.remind()
    old = {one.reminder_id for one in _rung(h)}

    h.sql(
        "UPDATE job_mail SET event_at = ?, event_text = ?",
        "2026-10-09T10:00-07:00",
        "Friday October 9th, 10:00am - 11:00am",
    )
    h.job.remind()

    rung = _rung(h)
    assert {one.reminder_id for one in rung if one.cancelled} == old
    fresh = sorted((one for one in rung if one.pending), key=lambda r: r.due_at_ms)
    assert [one.due_at_ms for one in fresh] == [
        _utc("2026-10-08T20:00:00-07:00"),
        _utc("2026-10-09T09:30:00-07:00"),
    ]
    assert h.outlook.tools() == ["create-calendar-event", "update-calendar-event"]
    args = h.outlook.calls[1][1]
    assert args["eventId"] == next(iter(h.outlook.events))
    assert args["body"]["start"] == {
        "dateTime": "2026-10-09T10:00:00",
        "timeZone": "America/Vancouver",
    }
    assert args["body"]["end"]["dateTime"] == "2026-10-09T11:00:00"
    assert len(h.outlook.events) == 1
    h.job.remind()
    assert len(_rung(h)) == 4
    assert len(h.outlook.calls) == 2


def test_a_rejected_or_hidden_application_cancels_its_reminders_and_deletes_the_event(
    tmp_path: Path, jev: _Jev
) -> None:
    """A rejection (or hiding it) takes everything back; a time already past is left alone."""
    h = _reminding(tmp_path, jev)
    assert h.outlook is not None
    _stored(h, "b-1", "Beta", "Dev", "interview", timedelta(days=3), event_at=_INTERVIEW)
    h.job.remind()
    assert len(_rung(h)) == 4
    assert len(h.outlook.events) == 2
    beta = job_ledger.application_id("Beta")

    _stored(h, "m-no", "Reliable Controls", "", "rejection", timedelta(days=1))
    hide = h.client.post(f"/inherent/jobs/applications/{beta}", json={"hidden": True})
    assert hide.status_code == 200
    h.job.remind()

    assert [one.cancelled for one in _rung(h)] == [True] * 4
    assert h.outlook.tools() == ["create-calendar-event"] * 2 + ["delete-calendar-event"] * 2
    assert h.outlook.events == {}
    assert job_ledger.interview_rows(h.db) == {}
    h.job.remind()
    assert len(h.outlook.calls) == 4

    # An interview whose time has passed is not undone: its event stays in the calendar.
    past = _reminding(tmp_path / "past", jev)
    assert past.outlook is not None
    past.job.remind()
    past.job.now = lambda: datetime(2026, 10, 9, 9, 0, tzinfo=UTC)
    past.job.remind()
    assert past.outlook.tools() == ["create-calendar-event"]
    assert job_ledger.interview_rows(past.db) == {}


def test_a_slot_already_past_is_skipped_not_set_late(tmp_path: Path, jev: _Jev) -> None:
    """Mail read after 20:00 the evening before: only the 30-minute reminder is set."""
    h = _reminding(tmp_path, jev)
    assert h.outlook is not None
    h.job.now = lambda: datetime(2026, 10, 7, 21, 0, tzinfo=_VANCOUVER).astimezone(UTC)

    h.job.remind()

    assert [one.due_at_ms for one in _rung(h)] == [_utc("2026-10-08T12:30:00-07:00")]
    assert h.outlook.tools() == ["create-calendar-event"]
    reminders = _apps(h)[("Reliable Controls", "Firmware QA")]["reminders"]
    assert (reminders["evening"], reminders["before"]) == (False, True)
    h.job.remind()
    assert len(_rung(h)) == 1

    h.job.now = lambda: datetime(2026, 10, 8, 12, 45, tzinfo=_VANCOUVER).astimezone(UTC)
    h.job.remind()  # the 30 minute mark has gone too: nothing to set, the event stays
    assert len(_rung(h)) == 1


def test_the_cancel_route_undoes_both_and_that_time_is_not_armed_again(
    tmp_path: Path, jev: _Jev
) -> None:
    """Cancel: reminders cancelled, event deleted, the card says so; a new time arms again."""
    h = _reminding(tmp_path, jev)
    assert h.outlook is not None
    h.job.remind()
    app_id = job_ledger.application_id("Reliable Controls")
    url = f"/inherent/jobs/applications/{app_id}/cancel-reminders"

    assert h.client.post(url).status_code == 200

    assert all(one.cancelled for one in _rung(h))
    assert h.outlook.tools() == ["create-calendar-event", "delete-calendar-event"]
    assert h.outlook.events == {}
    reminders = _apps(h)[("Reliable Controls", "Firmware QA")]["reminders"]
    assert (reminders["cancelled"], reminders["outlook"]) == (True, False)
    h.job.remind()
    assert len(_rung(h)) == 2
    assert len(h.outlook.calls) == 2

    h.sql("UPDATE job_mail SET event_at = ?", "2026-10-09T10:00-07:00")
    h.job.remind()
    assert len([one for one in _rung(h) if one.pending]) == 2
    assert h.outlook.tools()[2:] == ["create-calendar-event"]
    assert _apps(h)[("Reliable Controls", "Firmware QA")]["reminders"]["cancelled"] is False

    assert h.client.post("/inherent/jobs/applications/nothere/cancel-reminders").status_code == 404


def test_the_outlook_writer_refuses_any_other_tool_and_any_event_it_did_not_create() -> None:
    """Only the three event writes go through, and only on an id this module stored."""
    outlook = _Outlook()
    for tool in ("send-mail", "delete-calendar", "list-calendar-events", "get-calendar-view"):
        with pytest.raises(ValueError, match="may only write one Outlook event"):
            outlook_write(outlook, tool, {}, set())  # type: ignore[arg-type]
    for tool in ("update-calendar-event", "delete-calendar-event"):
        with pytest.raises(ValueError, match="not an event Jarvis created"):
            outlook_write(outlook, tool, {"eventId": "someone-elses"}, {"mine"})  # type: ignore[arg-type]
    assert outlook.calls == []


def test_an_outlook_failure_keeps_the_reminders_and_is_retried_next_pass(
    tmp_path: Path, jev: _Jev
) -> None:
    """Graph down: the reminders stand, nothing raises; the next pass writes the event once."""
    h = _reminding(tmp_path, jev)
    assert h.outlook is not None
    h.outlook.failures = 1

    h.job.remind()

    assert len(_rung(h)) == 2
    assert h.outlook.events == {}
    assert _apps(h)[("Reliable Controls", "Firmware QA")]["reminders"]["outlook"] is False
    h.job.remind()
    h.job.remind()
    assert len(_rung(h)) == 2
    # The failed try, then the one that held.
    assert h.outlook.tools() == ["create-calendar-event"] * 2
    assert len(h.outlook.events) == 1
    assert _apps(h)[("Reliable Controls", "Firmware QA")]["reminders"]["outlook"] is True

    # An event deleted in Outlook by hand is gone for good: cancelling still finishes.
    h.outlook.gone = True
    app_id = job_ledger.application_id("Reliable Controls")
    cancel = h.client.post(f"/inherent/jobs/applications/{app_id}/cancel-reminders")
    assert cancel.status_code == 200
    assert job_ledger.interview_rows(h.db)[app_id]["outlook_id"] is None

    # No Microsoft connection at all: the reminders are still set.
    bare = _reminding(tmp_path / "bare", jev)
    bare.job._connections = _Connections(None)  # type: ignore[assignment]  # noqa: SLF001
    bare.job.remind()
    assert len(_rung(bare)) == 2


def test_the_loop_arms_at_start_and_after_each_cycle_without_gmail(
    tmp_path: Path, jev: _Jev, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reminders pass does not wait on Gmail or a key: the run loop calls it by itself."""
    monkeypatch.delenv("OPENROUTER_API_KEY")
    h = _reminding(tmp_path, jev, poll_s=0.05)

    async def run() -> None:
        task = asyncio.create_task(h.job.run())
        for _ in range(100):
            if len(_rung(h)) == 2:
                break
            await asyncio.sleep(0.05)
        later = "2026-10-09T10:00-07:00"
        _stored(h, "m-b", "Beta", "Dev", "interview", timedelta(days=3), event_at=later)
        for _ in range(100):
            if len(_rung(h)) == 4:
                break
            await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert len(_rung(h)) == 4


def test_the_interview_reminders_block_is_validated_like_the_other_job_mail_keys(
    tmp_path: Path,
) -> None:
    """The shipped block is on; each bad value stops boot naming its key; off builds nothing."""
    shipped = yaml.safe_load((repo_root() / "config" / "jarvis.yaml").read_text())
    block = shipped["job_mail"]
    assert block["interview_reminders"] == {
        "enabled": True,
        "evening_at": "20:00",
        "before_min": 30,
        "outlook": True,
    }
    config_path, db = tmp_path / "jarvis.yaml", tmp_path / "memory.db"
    connections: Any = _Connections(None)
    on = {"job_mail": {**block, "enabled": True}}

    built = _job_mail(on, config_path, None, connections, db, event_log=tmp_path / "log.db")
    assert built is not None
    assert built.interviews is not None
    off = {"job_mail": {**on["job_mail"], "interview_reminders": {"enabled": False}}}
    built = _job_mail(off, config_path, None, connections, db, event_log=tmp_path / "log.db")
    assert built is not None
    assert built.interviews is None

    for value in (
        None,
        {"enabled": "yes"},
        {**block["interview_reminders"], "evening_at": "evening"},
        {**block["interview_reminders"], "evening_at": 20},
        {**block["interview_reminders"], "before_min": 0},
        {**block["interview_reminders"], "before_min": 1.5},
        {**block["interview_reminders"], "outlook": "yes"},
    ):
        bad = {"job_mail": {**on["job_mail"], "interview_reminders": value}}
        with pytest.raises(RuntimeBootstrapError, match=r"job_mail\.interview_reminders"):
            _job_mail(bad, config_path, None, connections, db)
