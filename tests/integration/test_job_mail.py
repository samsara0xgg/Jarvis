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
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest
import yaml
from fastapi.testclient import TestClient

from jarvis.decision.attention import ContextPack, Judgement, replay, rule_judge_v1
from jarvis.decision.surrogate_route import SurrogateRoute
from jarvis.execution.tools import ToolError
from jarvis.runtime import RuntimeBootstrapError, _job_mail
from jarvis.runtime.inherent_loop import _job_mail_deps, _say_job_line
from jarvis.runtime.job_mail import JobMail, JobMailSettings, gmail_read
from jarvis.shared import lang
from jarvis.state import job_ledger
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

    def call(self, server: str, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
        assert server == "gmail"
        self.calls.append((tool, dict(args)))
        if tool == "gmail_search":
            if self.search_failures:
                self.search_failures -= 1
                return {"text": json.dumps({"error": self.search_error})}
            hits = [{"id": one["id"], "threadId": one["threadId"]} for one in self.mails]
            return {"text": json.dumps({"messages": hits})}
        assert tool == "gmail_get", f"job mail must only read: {tool}"
        letter = next(one for one in self.mails if one["id"] == args["messageId"])
        body = letter["body"] if args["format"] == "full" else ""
        return {"text": json.dumps({**letter, "snippet": "", "body": body})}

    def tools(self) -> set[str]:
        return {tool for tool, _args in self.calls}

    def full_reads(self) -> set[str]:
        return {a["messageId"] for t, a in self.calls if t == "gmail_get" and a["format"] == "full"}


class _Connections:
    def __init__(self, gmail: _Gmail | None) -> None:
        self.gmail = gmail

    def client_for(self, server: str) -> _Gmail:
        if server != "gmail" or self.gmail is None:
            msg = f"mcp server {server!r} is not connected"
            raise ToolError(msg, code="mcp_server")
        return self.gmail


class _Harness:
    def __init__(
        self,
        tmp_path: Path,
        jev: _Jev,
        gmail: _Gmail,
        settings: JobMailSettings,
        judge: Any = rule_judge_v1,  # noqa: ANN401 - any Judge
    ) -> None:
        self.db = tmp_path / "memory.db"
        self.jev, self.gmail = jev, gmail
        self.job = JobMail(
            settings,
            jev.route(),
            _Connections(gmail),
            self.db,
            judge,  # type: ignore[arg-type]
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
    **settings: Any,  # noqa: ANN401
) -> _Harness:
    fields = {**SETTINGS.__dict__, **settings}
    gmail = _Gmail(MAILS if mails is None else mails)
    return _Harness(tmp_path, jev, gmail, JobMailSettings(**fields), judge)


def test_a_cycle_types_the_mail_fills_the_ledger_and_alerts_by_rule(
    tmp_path: Path,
    jev: _Jev,
) -> None:
    """Receipt: ledger. Rejection: card. Other: card with sound. Interview or offer: speak."""
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
        "m-offer": "speak",
        "m-digest": "card_sound",
        "m-followup": "card_sound",
    }
    notices = {n["mail_kind"] + ":" + n["company"]: n for n in h.notices()}
    interview = notices["interview:Northwind"]
    assert interview["kind"] == "mail"
    assert interview["title"] == lang.t("job.title.interview", company="Northwind")
    assert interview["text"] == f"{interview['title']} {interview['line']}"
    assert interview["event_at"] == row[1]
    assert EVENT_SENTENCE not in json.dumps(h.notices())  # the line is typed facts, not body text
    assert interview["level"] == "speak"
    assert notices["rejection:Orbital"]["level"] == "card"
    assert notices["job_other:LinkedIn"]["level"] == "card_sound"

    # One spoken line, though two alerts are at the speak level: the gap holds the second.
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
    # The newsletter and the personal mail were cleared by the header alone.
    assert h.gmail.full_reads() == {
        f"m-{one}"
        for one in (
            "receipt",
            "interview",
            "reject",
            "offer",
            "digest",
            "followup",
            "promo",
            "fair",
            "old",
        )
    }

    headers = jev.asked("job")
    assert len(headers) == len(MAILS)
    assert len(jev.asked("kind")) == 9
    for request in jev.requests:
        assert request["model"] == "typesafe/jev-1.13"
        assert request["provider"] == {"zdr": True}
        assert "@" not in request["state"].split("\n\n", 1)[0]  # a domain, never an address
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
    quiet = h.notices()
    assert len(quiet) == 5
    assert {n["level"] for n in quiet} == {"card"}
    for level in ("no-pop", "dnd"):
        h.quiet = level
        assert h.notices() == []
    assert len(h.sql("SELECT id FROM job_alert WHERE state = 'pending'")) == 5

    h.age_alerts(timedelta(minutes=10))  # they waited while the level held them
    h.quiet = "off"
    (digest,) = h.notices()
    assert digest["kind"] == "digest"
    assert digest["title"] == lang.t("job.digest.title", n=5)
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
    assert gmail_read(gmail, "gmail_search", {"query": "x"}) == {"messages": []}  # type: ignore[arg-type]


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
    }.items() <= block.items()
    config_path, db = tmp_path / "jarvis.yaml", tmp_path / "memory.db"
    connections: Any = _Connections(None)

    assert _job_mail(shipped, config_path, None, connections, db) is None  # no watcher is built
    assert _job_mail({}, config_path, None, connections, db) is None
    on = {"job_mail": {**block, "enabled": True}}
    assert isinstance(_job_mail(on, config_path, None, connections, db), JobMail)
    for key, value in (
        ("poll_s", 0),
        ("backfill_days", "14"),
        ("header_skip_at", 1.5),
        ("body_min", 0),
        ("max_body_chars", True),
        ("model", " "),
        ("timeout_ms", 0),
        ("speak", "yes"),
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
    assert set(pack.situation) == {"hour", "weekday", "quiet", "speech_ok"}
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
    alert = next(n for n in h.notices() if n["mail_kind"] == "rejection")
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


def test_every_decision_keeps_the_snapshot_jev_saw_and_never_an_address(
    tmp_path: Path, jev: _Jev
) -> None:
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

    # A not-job at the header: no body was read, so none is kept.
    news = snap("m-news", "header")
    assert news[1] == "not_job"
    assert json.loads(news[6])["not_job"] == pytest.approx(0.97)
    assert news[7] is None
    assert h.sql("SELECT 1 FROM job_decision WHERE message_id = 'm-news' AND stage = 'body'") == []

    # A not-job at the body keeps the body Jev read.
    promo = snap("m-promo", "body")
    assert promo[1] == "not_job"
    assert promo[7] == "50% off"

    # No address, only display name and domain.
    stored = json.dumps(h.sql("SELECT sender_name, sender_domain, subject FROM job_decision"))
    assert "@" not in stored

    # An error (Jev down) is a snapshot too, with no probabilities.
    jev.status = 500
    failing = _harness(tmp_path / "e", jev, [m for m in MAILS if m["id"] == "m-offer"])
    failing.job.poll_once()
    assert failing.sql(
        "SELECT stage, verdict, subject, probabilities, judge FROM job_decision"
    ) == [("header", "error", "Offer of employment", None, "jev-1.13/header-v1")]


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
    h.job.poll_once()  # and the first good cycle is silent
    assert len(h.sql("SELECT id FROM job_alert")) == 1

    h.job.now = lambda: NOW + timedelta(hours=13)
    h.gmail.search_failures = 3
    for _ in range(3):
        fail()
    assert len(h.sql("SELECT id FROM job_alert")) == 2


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

    assert h.spoken == 2
    reply = h.client.get("/inherent/notices").json()
    assert reply["audio_private"] is True
    assert {n["level"] for n in reply["notices"]} == {"speak", "card", "card_sound"}
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
    assert {n["level"] for n in h.notices()} == {"speak", "card", "card_sound"}

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
