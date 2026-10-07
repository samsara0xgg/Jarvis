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
# A job site's account or system notice: in the ledger, never an alert (ADR 0158).
ACCOUNT_KIND: Final[str] = "other"
# Bump when a stage's instructions or criteria change, so a logged decision names its wording.
_STAGE_VERSION: Final[str] = "v1"

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
    """What of a letter's header is read.

    ``address`` is for the local ``job_decision`` snapshot only (ADR 0162): no question to Jev,
    typed row, pack or alert may carry it; those use the name and the domain.
    """

    message_id: str
    thread_id: str
    received_at: str
    name: str
    domain: str
    subject: str
    address: str = ""


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
    probabilities: dict[str, float]
    extracted_by: str = "local"
    # A LinkedIn job-alert digest: ledger only unless ``job_mail.linkedin_alerts`` says otherwise.
    alert_digest: bool = False


@dataclass(frozen=True)
class Skip:
    """The header question's answer: held back as not job mail or not, and P(job mail)."""

    skipped: bool
    p_job: float
    probabilities: dict[str, float]


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

    def judge_id(self, stage: str) -> str:
        """Who judged a stage, as logged in ``job_decision`` (ADR 0157): ``jev-1.13/header-v1``."""
        return f"{self._route.model.rpartition('/')[2]}/{stage}-{_STAGE_VERSION}"

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
                None
                if answer is None
                else Skip(answer[_NOT_JOB] >= self._skip_at, _p_job(answer), answer)
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
            digest = kind == "job_other" and is_alert_digest(head.name, head.domain, head.subject)
            if kind == "job_other" and is_account_notice(head.subject):
                kind = ACCOUNT_KIND
            out[head.message_id] = Typed(
                kind,
                probabilities[top],
                facts,
                _p_job(probabilities),
                probabilities,
                alert_digest=digest,
            )
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


# --- local rules over sender and subject (ADR 0158) ----------------------------------

_LINKEDIN: Final = "linkedin.com"
_SOCIAL: Final = re.compile(
    r"\brecently posted\b|\bhired near you\b|\bis popular in your network\b"
    r"|\bsomeone at .+ you may know\b|\bstarted a new (?:position|job|role)\b"
    r"|\bwork anniversary\b|\bnew connections?\b|\bwants to connect\b|\bviewed your profile\b"
    r"|\bprofile was viewed\b|\bnew profile views?\b|\bappeared in \d+ search",
    re.IGNORECASE,
)
_SENT_TO: Final = re.compile(r"\byour application was sent to\s+(.+?)[\s.!]*$", re.IGNORECASE)
_ALERT_SUBJECT: Final = re.compile(r"\bis hiring\b|\bnew jobs?\b", re.IGNORECASE)
_ACCOUNT: Final = re.compile(
    r"\b(?:user information|password|account|profile update|verify your|verification code|"
    r"sign[- ]?in|security alert)\b",
    re.IGNORECASE,
)


def _is_linkedin(domain: str) -> bool:
    return domain == _LINKEDIN or domain.endswith("." + _LINKEDIN)


def is_excluded(domain: str, excluded: tuple[str, ...]) -> bool:
    """Whether a sender domain is one the owner keeps out of job mail (``exclude_domains``)."""
    return any(domain == one or domain.endswith("." + one) for one in excluded)


def is_application_sent(domain: str, subject: str) -> bool:
    """LinkedIn's Easy Apply confirmation ("<name>, your application was sent to <Company>").

    An application Allen made: the one LinkedIn mail that is not kept out of job mail (ADR 0177).
    """
    return _is_linkedin(domain) and _SENT_TO.search(subject) is not None


def is_social(domain: str, subject: str) -> bool:
    """LinkedIn's social news and profile-activity notices ("X recently posted"): not job mail."""
    return _is_linkedin(domain) and _SOCIAL.search(subject) is not None


def is_alert_digest(name: str, domain: str, subject: str) -> bool:
    """Whether a mail is a LinkedIn job-alert digest of new postings.

    From "Job Alerts", or from LinkedIn's own name with a "Y is hiring" or "new jobs" subject.
    A recruiter's InMail is neither.
    """
    if not _is_linkedin(domain):
        return False
    return "job alert" in name.casefold() or (
        name.casefold() == "linkedin" and _ALERT_SUBJECT.search(subject) is not None
    )


def is_account_notice(subject: str) -> bool:
    """An account or system notice from a job site ("CGI - User Information", a password mail)."""
    return _ACCOUNT.search(subject) is not None


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
# A capitalised company name and its possessive, kept out of the role that follows it.
_COMPANY_POSSESSIVE: Final = r"(?-i:[A-Z])[\w&.-]*(?:\s+(?-i:[A-Z])[\w&.-]*){0,3}['\u2019]s\s+"
# What ends a role in a subject: a bracket, a comma (a requisition id follows), an id like J0926.
_ROLE_END: Final = r"(?:\s+co-?op)?(?:\s*\(|\s*,|\s+[A-Z]\d{3,}|$)"
_ROLE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        rf"\b(?:winter|spring|summer|fall|autumn)\s+\d{{4}}\s*[-:\u2013]\s*(.{{3,60}}?){_ROLE_END}",
        rf"\backnowledg\w*\s*[-:\u2013]\s*(?:co-?op\s*[-:\u2013]\s*)?(.{{3,60}}?){_ROLE_END}",
        r"\b(?:for|to|offer\s+you)\s+the\s+(.{3,80}?)\s+(?:position|role|opening|opportunity)\b",
        r"\b(?:for|to)\s+the\s+(.{3,80}?(?:co-?op|intern(?:ship)?))\b",
        # "applying to Cambio Earth's QA & Test Automation Developer Co-op position"
        rf"\bapplying\s+(?:for|to)\s+(?:the\s+)?(?:{_COMPANY_POSSESSIVE})?(.{{3,80}}?)\s+(?:position|role|opening|job)\b",
        # "the QA Engineer position": a capitalised start keeps "the next role" out
        r"\bthe\s+((?-i:[A-Z])[^.\n]{2,79}?)\s+(?:position|role)\b",
        r"\bapplication\s+(?:for|to)\s+(?:the\s+)?(.{3,80}?)(?:\s+(?:position|role)\b|\s+at\b|\s+with\b|[.,;:!\n]|$)",
        r"[-:|\u2013]\s*(.{3,60}?(?:co-?op|intern(?:ship)?|developer|engineer|analyst))\s*(?:[-:|\u2013(]|$)",
    )
)
# Words that name no role: never stored as one.
GENERIC_ROLES: Final = frozenset(
    {
        "job",
        "jobs",
        "role",
        "position",
        "application",
        "opportunity",
        "opening",
        "posting",
        "vacancy",
        "co-op",
        "coop",
        "intern",
        "internship",
    },
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


# Applicant-tracking systems send for the employer: their domain is not the company.
_ATS: Final = frozenset(
    {
        "myworkday",
        "myworkdayjobs",
        "greenhouse",
        "greenhouse-mail",  # us.greenhouse-mail.io
        "clearcompany",
        "lever",
        "icims",
        "smartrecruiters",
        "ashby",
        "ashbyhq",
        "bamboohr",
    },
)
_SECOND_LEVEL: Final = frozenset({"co", "com", "org", "net", "gov", "ac", "edu"})
# ponytail: a lowercase domain label has no word boundaries to read; it is split only when it ends
# in one of these corporate words ("reliablecontrols" -> "Reliable Controls"). A dictionary
# would split more, at the cost of a dependency; the ledger shows the guess and Allen can see it.
_TAILS: Final = frozenset(
    {
        "controls",
        "systems",
        "technologies",
        "solutions",
        "software",
        "networks",
        "robotics",
        "labs",
        "energy",
        "group",
        "capital",
        "health",
        "digital",
        "industries",
    },
)
# A word that names a role or department: after a possessive it is not part of the company.
_ROLE_WORD: Final = re.compile(
    r"(?:qa|test(?:ing)?|engineering|software|hardware|firmware|data|backend|frontend|"
    r"full-?stack|product|design|marketing|sales|finance|research|platform|devops|security|"
    r"it|hr|operations|mechanical|electrical|summer|winter|fall|spring|co-?op|intern(?:ship)?|"
    r"developer|engineer|analyst|team|department|group)\b",
    re.IGNORECASE,
)
_POSSESSIVE: Final = re.compile(r"['\u2019]s\s+(.+)$")
_PERSON_WORD: Final = re.compile(r"[A-Z][a-z]+(?:[-'\u2019][A-Z]?[a-z]+)*\.?|[A-Z]\.")
# A subject's leading "Company - Invitation to Interview" segment is not a company when it says
# what the mail is, and a word after "at/from/to" is not one when it is one of these.
_SUBJECT_WORDS: Final = re.compile(
    r"\b(?:interview|invitation|application|acknowledg\w*|offer|update|thank|welcome|job|your|"
    r"virtual|schedul\w*|reminder|status|re|fwd?)\b",
    re.IGNORECASE,
)
_LEADING_SEGMENT: Final = re.compile(
    r"^(?:(?:re|fwd?)\s*:\s*)*([^-\u2013|:()]{2,40}?)\s+[-\u2013|]\s+\S", re.IGNORECASE
)
_AFTER_PREPOSITION: Final = re.compile(
    r"\b(?:[Aa]t|[Ff]rom|[Ww]ith|[Tt]o|[Jj]oin)\s+"
    r"([A-Z][\w&.'\u2019-]*(?:\s+[A-Z][\w&.'\u2019-]*){0,3})"
)


def _owner_label(domain: str) -> str:
    """The registrable label of a domain (``app.bamboohr.com`` -> ``bamboohr``), or ''."""
    labels = [one for one in domain.lower().split(".") if one]
    if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL:  # noqa: PLR2004 - co.uk, com.au
        return labels[-3]
    if len(labels) >= 2:  # noqa: PLR2004 - a registrable name and a suffix
        return labels[-2]
    return next((one for one in labels if one not in _SUBDOMAINS), "")


def _label_name(label: str) -> str:
    """A domain label as a name: hyphens or a closing corporate word split it, else one word."""
    words = label.replace("-", " ").split()
    if len(words) == 1:
        tail = next(
            (t for t in _TAILS if words[0].endswith(t) and len(words[0]) - len(t) >= 3),  # noqa: PLR2004
            None,
        )
        if tail:
            words = [words[0][: -len(tail)], tail]
    name = " ".join(words)
    return name.upper() if len(name) <= 3 else name.title()  # noqa: PLR2004 - CGI, IBM


def _is_person(name: str, label: str) -> bool:
    """Whether a display name reads as a person's.

    Two or three capitalised words, none an organisation word, and not the domain's own name
    ("Reliable Controls" at reliablecontrols.com).
    """
    words = name.split()
    if not 2 <= len(words) <= 3 or not all(_PERSON_WORD.fullmatch(w) for w in words):  # noqa: PLR2004
        return False
    if _NOISE.search(name) or words[-1].lower() in _TAILS:
        return False
    return not label or label not in re.sub(r"\W", "", name).lower()


def _unpossessive(name: str) -> str:
    """``Cambio Earth's QA`` -> ``Cambio Earth``: a possessive and the role word after it go."""
    found = _POSSESSIVE.search(name)
    if found and _ROLE_WORD.match(found[1]):
        return name[: found.start()]
    return name


def _named_in(text: str) -> str:
    """The company a subject or body names, or '' when none reads.

    Its leading ``Company - ...`` segment, else the capitalised words after ``at``, ``from``,
    ``with``, ``to`` or ``join``.
    """
    text = text.strip()
    lead = _LEADING_SEGMENT.match(text)
    if lead and not _SUBJECT_WORDS.search(lead[1]) and not _is_person(lead[1].strip(), ""):
        return lead[1].strip()
    for found in _AFTER_PREPOSITION.finditer(text):
        name = _unpossessive(found[1].strip(" .,-")).strip(" .,-")
        if not _SUBJECT_WORDS.search(name):
            return name
    return ""


def company_of(name: str, domain: str, subject: str = "", body: str = "") -> str:
    """Who the mail is from.

    The sender's display name without department words, unless that reads as a person, then the
    organisation of the sender's domain.

    When the domain is an applicant-tracking system the company is read from the subject, then
    the body, first. ponytail: a free-mail sender or a person's name that is not matched by
    the rules above still reads as the company; there is no hand-edit in the ledger yet.
    """
    if (sent := _SENT_TO.search(subject)) and _is_linkedin(domain):  # "sent to <Company>"
        return sent[1]
    label = _owner_label(domain)
    if label in _ATS:
        found = _named_in(subject) or _named_in(body)
        if found:
            return found
    cleaned = re.sub(r"\s+", " ", _NOISE.sub(" ", name)).strip(" -|\u00b7,:&")
    if len(cleaned) >= 2 and not _is_person(cleaned, label):  # noqa: PLR2004 - one letter is not a name
        return cleaned
    return _label_name(label) if label else _UNKNOWN_COMPANY


def role_of(subject: str, body: str) -> str:
    """The position a letter names, from its subject first, else its body; '' when none reads."""
    for text in (subject.strip(), body):
        for pattern in _ROLE_PATTERNS:
            for found in pattern.finditer(text):
                role = re.sub(r"\s+", " ", found.group(1)).strip(" -|:.,")
                if (
                    3 <= len(role) <= _PLAUSIBLE_ROLE_CHARS  # noqa: PLR2004 - too short or long is noise
                    and role.casefold() not in GENERIC_ROLES
                ):
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
    return Facts(
        company_of(head.name, head.domain, head.subject, body),
        role_of(head.subject, body),
        text,
        at,
    )


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
            "alert_digest": typed.alert_digest,
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
