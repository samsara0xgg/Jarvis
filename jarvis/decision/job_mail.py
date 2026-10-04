"""L3 job mail (ADR 0155): is a letter job-hunt mail, what kind, and who from, about what, when.

Two Jev questions, both choices, over a letter Allen has not been asked about: a header
question (sender display name, sender address domain, subject) that clears most mail as not
job mail, then, for what is left, a typing question over the same header and the first
``max_body_chars`` of the plain-text body. Jev answers only noul, choice and score questions,
so the company, role and event time are read locally by regexes (``extracted_by`` is
``local``). Nothing here sends, labels or deletes mail: the caller owns the Gmail reads.

Layer rules: stdlib + L3 siblings + ``jarvis.shared``; no wiring.
"""

from __future__ import annotations

import logging
import re
import threading
from concurrent.futures import Future, wait
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Final
from zoneinfo import ZoneInfo

from jarvis.decision.attention import ContextPack
from jarvis.shared import lang

if TYPE_CHECKING:
    from collections.abc import Sequence

    from jarvis.decision.surrogate_route import Reply, SurrogateRoute

LOGGER = logging.getLogger(__name__)

KINDS: Final[tuple[str, ...]] = ("offer", "interview", "rejection", "receipt", "job_other")
_NOT_JOB: Final[str] = "not_job"
_UNKNOWN_SENDER: Final[str] = "unknown"
_UNKNOWN_COMPANY: Final[str] = "Unknown"
_USE: Final[str] = "job_mail"

_CONTEXT: Final[str] = (
    "The text is an email sent to Allen, a university student in Victoria, BC who is job"
    " hunting for co-op, internship and developer roles."
)
_LANGUAGE: Final[str] = " The text may be in Chinese, English or both."
_HEADER_INSTRUCTIONS: Final[str] = (
    _CONTEXT + " It gives the sender's display name, the domain of the sender's address and the"
    " subject line. Is the email about his job hunt?" + _LANGUAGE
)
_HEADER_CRITERIA: Final[dict[str, str]] = {
    "job_related": "About his job hunt: an application or its acknowledgement, a recruiter, an"
    " interview or assessment, an offer or rejection, a Co-op or internship posting, a job alert"
    " or job board mail.",
    "maybe_job": "Could be about a job or career but the sender and subject do not make it clear.",
    _NOT_JOB: "Not about jobs: school, personal mail, receipts, shipping, banking, newsletters,"
    " promotions, security alerts, social media.",
}
_BODY_INSTRUCTIONS: Final[str] = (
    _CONTEXT + " It gives the sender's display name, the domain of the sender's address, the"
    " subject line and the start of the body. What kind of job-hunt email is it?" + _LANGUAGE
)
_BODY_CRITERIA: Final[dict[str, str]] = {
    "offer": "A job, co-op or internship offer to Allen, or an offer letter to accept.",
    "interview": "An invitation to, or scheduling or reminder of, an interview, phone screen or"
    " assessment.",
    "rejection": "The application was not successful: not moving forward, position filled.",
    "receipt": "An automatic acknowledgement that an application was received, with nothing else"
    " for Allen to do.",
    "job_other": "Other job-hunt mail: a recruiter reaching out, a job alert or posting, an"
    " application status that is not a decision, a request for documents.",
    _NOT_JOB: "Not about his job hunt.",
}


@dataclass(frozen=True)
class Head:
    """What of a letter's header is kept: never the address, only its domain."""

    message_id: str
    thread_id: str
    received_at: str
    name: str
    domain: str
    subject: str


@dataclass(frozen=True)
class Facts:
    """The typed facts read from a letter locally."""

    company: str
    role: str
    event_text: str | None
    event_at: str | None


@dataclass(frozen=True)
class Typed:
    """Jev's kind for a job letter, how sure it was, and the facts."""

    kind: str
    confidence: float
    facts: Facts
    p_job: float
    extracted_by: str = "local"


@dataclass(frozen=True)
class Skip:
    """The header question's answer: held back as not job mail or not, and P(job mail)."""

    skipped: bool
    p_job: float


def header_state(head: Head) -> str:
    """The text the header question is asked over: name (or unknown), domain, subject."""
    return f"From: {head.name or _UNKNOWN_SENDER}\nDomain: {head.domain}\nSubject: {head.subject}"


def body_state(head: Head, body: str, max_chars: int) -> str:
    """The header and the first ``max_chars`` of the plain-text body."""
    return f"{header_state(head)}\n\n{body[:max_chars]}"


class JobMailJev:
    """Jev's two questions over letters, with a daily cap on calls."""

    def __init__(
        self,
        route: SurrogateRoute,
        *,
        header_skip_at: float,
        body_min: float,
        max_body_chars: int,
        max_calls_per_day: int,
    ) -> None:
        """``route`` is the transport; its ``timeout_ms`` bounds one batch's wait."""
        self._route = route
        self._skip_at = header_skip_at
        self._body_min = body_min
        self._max_chars = max_body_chars
        self._cap = max_calls_per_day
        self._lock = threading.Lock()
        self._day = date.min
        self._calls = 0
        self.spent_usd = 0.0

    def room(self) -> int:
        """How many more calls today's cap allows."""
        self._roll()
        return max(0, self._cap - self._calls)

    def skips(self, heads: Sequence[Head]) -> dict[str, Skip | None]:
        """Per message id: whether Jev is sure it is not job mail, None with no answer."""
        batch = [
            (h.message_id, header_state(h), "job", _HEADER_INSTRUCTIONS, _HEADER_CRITERIA)
            for h in heads
        ]
        odds = self._ask(batch)
        out: dict[str, Skip | None] = {}
        for head in heads:
            answer = odds[head.message_id]
            out[head.message_id] = (
                None if answer is None else Skip(answer[_NOT_JOB] >= self._skip_at, _p_job(answer))
            )
        return out

    def types(self, letters: Sequence[tuple[Head, str]]) -> dict[str, Typed | None]:
        """Per message id: the typed letter, or None when Jev did not answer or it is not job mail.

        A letter Jev answers as not job mail is in the result as a kind of ``not_job``; one with no
        usable answer is None.
        """
        batch = [
            (
                h.message_id,
                body_state(h, body, self._max_chars),
                "kind",
                _BODY_INSTRUCTIONS,
                _BODY_CRITERIA,
            )
            for h, body in letters
        ]
        odds = self._ask(batch)
        out: dict[str, Typed | None] = {}
        for head, body in letters:
            probabilities = odds[head.message_id]
            if probabilities is None:
                out[head.message_id] = None
                continue
            top = max(probabilities, key=lambda name: probabilities[name])
            kind = (
                top
                if top == _NOT_JOB
                else (top if probabilities[top] >= self._body_min else "job_other")
            )
            facts = extract(head, body)
            out[head.message_id] = Typed(kind, probabilities[top], facts, _p_job(probabilities))
            self._route.note(
                "decision",
                _USE,
                head.message_id,
                typed=kind,
                probabilities=probabilities,
            )
        return out

    def _roll(self) -> None:
        today = datetime.now().astimezone().date()
        with self._lock:
            if today != self._day:
                self._day, self._calls = today, 0

    def _ask(
        self,
        batch: Sequence[tuple[str, str, str, str, dict[str, str]]],
    ) -> dict[str, dict[str, float] | None]:
        """One choice question per letter, sent together and waited for up to ``timeout_ms``."""
        self._roll()
        sent: dict[str, Future[Reply]] = {}
        for message_id, state, key, instructions, criteria in batch:
            question = {key: {"type": "choice", "instructions": instructions, "criteria": criteria}}
            future = self._route.post(state, question, _USE, message_id)
            if future is None:  # no key
                break
            with self._lock:
                self._calls += 1
            sent[message_id] = future
        done, _late = wait(sent.values(), timeout=self._route.timeout_ms / 1000)
        keys = {message_id: key for message_id, _s, key, _i, _c in batch}
        out: dict[str, dict[str, float] | None] = {message_id: None for message_id, *_ in batch}
        for message_id, future in sent.items():
            if future in done:
                out[message_id] = self._settle(message_id, keys[message_id], future)
        return out

    def _settle(self, message_id: str, key: str, done: Future[Reply]) -> dict[str, float] | None:
        """The choice's probabilities out of one finished call; a failure warns once per kind."""
        try:
            reply = done.result()
        except Exception as exc:  # noqa: BLE001 - a worker's failure is just an unanswered letter
            LOGGER.warning("job mail: %s: %s", type(exc).__name__, exc)
            return None
        if reply.error is not None:
            self._route.warn_once(reply.error, "job mail", "letters are asked again later")
            return None
        try:
            raw = reply.parsed["answers"][key]["probabilities"]
            odds = {
                name: float(raw.get(name, 0.0))
                for name in (*KINDS, _NOT_JOB, "job_related", "maybe_job")
            }
            cost = (reply.parsed.get("usage") or {}).get("cost")
        except (KeyError, TypeError, AttributeError, ValueError):
            self._route.warn_once("bad_json", "job mail", "letters are asked again later")
            return None
        paid = float(cost) if isinstance(cost, int | float) and not isinstance(cost, bool) else 0.0
        self.spent_usd += paid
        LOGGER.info(
            "job mail: one letter asked (%s), id %s, $%.6f (total $%.6f)",
            key,
            message_id,
            paid,
            self.spent_usd,
        )
        return odds


def _p_job(probabilities: dict[str, float]) -> float:
    """Jev's probability that a letter is job mail: what is not the ``not_job`` choice."""
    return min(1.0, max(0.0, 1.0 - probabilities[_NOT_JOB]))


# --- local extraction ---------------------------------------------------------------

_NOISE: Final = re.compile(
    r"\b(?:careers?|talent(?:\s+acquisition)?|recruit(?:ing|ment|ers?)|hr|human\s+resources|team|"
    r"hiring|jobs?|notifications?|no-?reply|do-?not-?reply|university\s+relations|co-?op|"
    r"campus|people|alerts?)\b",
    re.IGNORECASE,
)
_SUBDOMAINS: Final = frozenset(
    {
        "mail",
        "email",
        "e",
        "mg",
        "notifications",
        "notify",
        "jobs",
        "careers",
        "no-reply",
        "noreply",
        "info",
        "hello",
        "www",
        "send",
        "mailer",
    },
)
_ROLE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(?:for|to|offer\s+you)\s+the\s+(.{3,80}?)\s+(?:position|role|opening|opportunity)\b",
        r"\b(?:for|to)\s+the\s+(.{3,80}?(?:co-?op|intern(?:ship)?))\b",
        r"\bapplication\s+(?:for|to)\s+(?:the\s+)?(.{3,80}?)(?:\s+(?:position|role)\b|\s+at\b|\s+with\b|[.,;:!\n]|$)",
        r"^(?:re|fwd?):\s*(.{3,80})$",
        r"[-:|\u2013]\s*(.{3,60}?(?:co-?op|intern(?:ship)?|developer|engineer|analyst))\s*$",
    )
)
_EVENT_WORDS: Final = re.compile(
    r"\b(?:interview|meeting|call|chat|assessment|screen(?:ing)?|zoom|teams|session|conversation)\b",
    re.IGNORECASE,
)
_MONTHS: Final = (
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
)
_DATE_WORDS: Final = re.compile(
    r"\b(?:(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?\s+)?"
    r"(?P<month>January|February|March|April|May|June|July|August|September|October|November|"
    r"December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?"
    r"(?:,?\s+(?P<year>20\d\d))?\b",
    re.IGNORECASE,
)
_DATE_ISO: Final = re.compile(r"\b(?P<year>20\d\d)-(?P<month>\d\d)-(?P<day>\d\d)\b")
_TIME_AMPM: Final = re.compile(
    r"\b(?P<h>\d{1,2})(?::(?P<m>\d\d))?\s*(?P<ap>[ap])\.?m\b\.?", re.IGNORECASE
)
_TIME_24: Final = re.compile(r"\b(?P<h>[01]?\d|2[0-3]):(?P<m>[0-5]\d)\b")
_ZONES: Final[dict[str, str]] = {
    "PST": "America/Vancouver",
    "PDT": "America/Vancouver",
    "PT": "America/Vancouver",
    "Pacific": "America/Vancouver",
    "MST": "America/Edmonton",
    "MDT": "America/Edmonton",
    "MT": "America/Edmonton",
    "Mountain": "America/Edmonton",
    "CST": "America/Winnipeg",
    "CDT": "America/Winnipeg",
    "CT": "America/Winnipeg",
    "Central": "America/Winnipeg",
    "EST": "America/Toronto",
    "EDT": "America/Toronto",
    "ET": "America/Toronto",
    "Eastern": "America/Toronto",
    "UTC": "UTC",
    "GMT": "UTC",
}
_ZONE_RE: Final = re.compile(r"\b(" + "|".join(_ZONES) + r")\b")
_SENTENCE_SPLIT: Final = re.compile(r"(?<=[.!?])\s+|\n+")
_EVENT_CHARS: Final[int] = 200
_PLAUSIBLE_ROLE_CHARS: Final[int] = 80


def company_of(name: str, domain: str) -> str:
    """The sender's display name without department words, else the domain's own label.

    ponytail: a person's name as the display name reads as the company; the typed-company
    column can be fixed by hand in the ledger later, and Jev cannot extract free text yet.
    """
    cleaned = re.sub(r"\s+", " ", _NOISE.sub(" ", name)).strip(" -|·,:&")
    if len(cleaned) >= 2:  # noqa: PLR2004 - one letter is not a name
        return cleaned
    labels = [one for one in domain.lower().split(".") if one]
    if len(labels) >= 2:  # noqa: PLR2004 - a registrable name and a suffix
        label = labels[-2]
    else:
        label = next((one for one in labels if one not in _SUBDOMAINS), "")
    if not label:
        return _UNKNOWN_COMPANY
    return label.upper() if len(label) <= 3 else label.capitalize()  # noqa: PLR2004 - CGI, IBM


def role_of(subject: str, body: str) -> str:
    """The position a letter names, from its subject first, else its body; '' when none reads."""
    for text in (subject, body):
        for pattern in _ROLE_PATTERNS:
            found = pattern.search(text.strip() if text is subject else text)
            if found:
                role = re.sub(r"\s+", " ", found.group(1)).strip(" -|:.,")
                if 3 <= len(role) <= _PLAUSIBLE_ROLE_CHARS:  # noqa: PLR2004 - too short or too long reads as noise
                    return role
    return ""


def _day_of(sentence: str, received: datetime) -> date | None:
    """The date a sentence names (``March 4``, ``2026-03-04``); with no year, the next such day."""
    iso = _DATE_ISO.search(sentence)
    word = _DATE_WORDS.search(sentence)
    try:
        if iso is not None:
            return date(int(iso["year"]), int(iso["month"]), int(iso["day"]))
        if word is None:
            return None
        month = _MONTHS.index(word["month"][:3].lower()) + 1
        day = date(int(word["year"] or received.year), month, int(word["day"]))
    except ValueError:
        return None
    if word["year"] is None and day < received.date().replace(day=1):
        day = day.replace(year=day.year + 1)  # no year said: the next such day, not one long past
    return day


def _clock_of(sentence: str) -> tuple[int, int] | None:
    """(hour, minute) of a time in a sentence (``2:00 PM``, ``2pm``, ``14:00``)."""
    spoken = _TIME_AMPM.search(sentence)
    if spoken is not None:
        return int(spoken["h"]) % 12 + (12 if spoken["ap"].lower() == "p" else 0), int(
            spoken["m"] or 0
        )
    plain = _TIME_24.search(sentence)
    return None if plain is None else (int(plain["h"]), int(plain["m"]))


def _moment(sentence: str, received: datetime) -> str | None:
    """An ISO time out of a sentence that holds a clear date and time; else None."""
    day, clock = _day_of(sentence, received), _clock_of(sentence)
    if day is None or clock is None:
        return None
    zone = _ZONE_RE.search(sentence)
    try:
        moment = datetime(
            day.year,
            day.month,
            day.day,
            *clock,
            tzinfo=ZoneInfo(_ZONES[zone[1]]) if zone else None,
        )
    except ValueError:
        return None
    return moment.astimezone().isoformat(timespec="minutes")  # a bare time is Allen's own clock


def event_of(body: str, received: datetime) -> tuple[str | None, str | None]:
    """(sentence, ISO time) of the first sentence on an interview or meeting with a date or time.

    The time is None unless that sentence holds a clear date and time.
    """
    for raw in _SENTENCE_SPLIT.split(body):
        sentence = re.sub(r"\s+", " ", raw).strip()
        if not sentence or _EVENT_WORDS.search(sentence) is None:
            continue
        if not (
            _DATE_WORDS.search(sentence)
            or _DATE_ISO.search(sentence)
            or _TIME_AMPM.search(sentence)
            or _TIME_24.search(sentence)
        ):
            continue
        return sentence[:_EVENT_CHARS], _moment(sentence, received)
    return None, None


def extract(head: Head, body: str) -> Facts:
    """The company, role and event of a letter, read locally."""
    try:
        received = datetime.fromisoformat(head.received_at)
    except ValueError:
        received = datetime.now(UTC)
    text, at = event_of(body, received)
    return Facts(company_of(head.name, head.domain), role_of(head.subject, body), text, at)


# --- what the judge sees and what the card says ----------------------------------------


def pack_for(head: Head, typed: Typed, now: datetime, situation: dict[str, Any]) -> ContextPack:
    """The context pack of one typed letter: its typed facts (never the body) and the situation."""
    try:
        age_h = round((now - datetime.fromisoformat(head.received_at)).total_seconds() / 3600, 2)
    except ValueError:
        age_h = 0.0
    facts = typed.facts
    return ContextPack(
        source="job_mail",
        event_id=head.message_id,
        facts={
            "kind": typed.kind,
            "confidence": typed.confidence,
            "p_job": typed.p_job,
            "company": facts.company,
            "role": facts.role,
            "event_at": facts.event_at,
            "sender_domain": head.domain,
            "age_h": age_h,
        },
        situation=situation,
    )


def alert_text(kind: str, facts: Facts) -> tuple[str, str]:
    """The card's title and one sentence, from the typed facts only (never the body)."""
    title = lang.t(f"job.title.{kind}", company=facts.company)
    role = lang.t("job.part.role", role=facts.role) if facts.role else ""
    when = (
        lang.t("job.part.when", when=facts.event_at[:16].replace("T", " "))
        if facts.event_at
        else ""
    )
    return title, lang.t(f"job.line.{kind}", company=facts.company, role=role, when=when)


def as_row(head: Head, typed: Typed) -> dict[str, Any]:
    """The ``job_mail`` row's fields for a typed letter."""
    facts = typed.facts
    return {
        "message_id": head.message_id,
        "thread_id": head.thread_id,
        "received_at": head.received_at,
        "sender_name": head.name,
        "sender_domain": head.domain,
        "subject": head.subject,
        "kind": typed.kind,
        "company": facts.company,
        "role": facts.role,
        "event_at": facts.event_at,
        "event_text": facts.event_text,
        "confidence": typed.confidence,
        "extracted_by": typed.extracted_by,
    }
