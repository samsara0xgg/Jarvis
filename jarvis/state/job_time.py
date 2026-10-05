"""L2 job-site time (ADR 0161): which TimeSink spans were job hunting, and for which company.

A span is job-site time when its site is a LinkedIn jobs path, a known applicant-tracking host,
or a career page of a company already in the job ledger. Titles and URLs are read here only to
decide that and to name the company; they are never returned. Nothing is stored: callers get
seconds per company per local day, computed at read time.

Layer rules: stdlib + L2 siblings; no wiring.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit

from jarvis.state import timesink

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

# Applicant-tracking hosts: any span on them is job-site time.
ATS_DOMAINS: Final[tuple[str, ...]] = (
    "myworkdayjobs.com",
    "myworkdaysite.com",
    "myworkday.com",
    "greenhouse.io",
    "lever.co",
    "icims.com",
    "smartrecruiters.com",
    "ashbyhq.com",
)
# Hosts that carry many companies' mail or pages; never a company's own career domain.
_SHARED: Final[frozenset[str]] = frozenset(
    {*ATS_DOMAINS, "linkedin.com", "njoyn.com", "gmail.com", "outlook.com", "indeed.com"}
)
# Leading labels of a sender host that name a mail stream, not the company.
_STREAM_LABELS: Final[frozenset[str]] = frozenset(
    {
        "mail", "email", "e", "mg", "send", "smtp", "bounce", "em", "mailer", "notifications",
        "notification", "noreply", "no-reply", "jobs", "careers", "career", "talent",
        "recruiting", "recruit", "hr", "news", "info", "updates", "www",
    }
)  # fmt: skip
_CAREER_LABELS: Final[frozenset[str]] = frozenset({"careers", "career", "jobs", "talent"})
_SECOND_LEVEL: Final[frozenset[str]] = frozenset({"co", "com", "org", "net", "ac", "gov"})
_SUFFIX_WORDS: Final[frozenset[str]] = frozenset(
    {"inc", "ltd", "llc", "corp", "corporation", "limited", "co", "company"}
)
MIN_NAME_CHARS: Final[int] = 3
# A slug this long may sit inside a host label (sunlife in sunlife-careers); a shorter one must
# be a whole token of the host or path.
SLUG_INSIDE_CHARS: Final[int] = 5
# Ledger "companies" that are the job sites themselves (LinkedIn's own digests): never a match.
_PLATFORMS: Final[frozenset[str]] = frozenset(
    {"linkedin", "indeed", "glassdoor", "workday", "greenhouse", "lever", "icims", "ashby", "njoyn"}
)
# The ledger page and the 现况 doc count the last this many local days.
WINDOW_DAYS: Final[int] = 14


@dataclass(frozen=True)
class Company:
    """A ledger company: its display name and the base domains its mail came from."""

    name: str
    domains: frozenset[str]


def base_domain(host: str) -> str:
    """A sender host without mail-stream or ``www`` labels, cut to its registrable-ish name."""
    parts = host.lower().strip(".").split(".")
    while len(parts) > 2 and parts[0] in _STREAM_LABELS:  # noqa: PLR2004 - a name and a TLD
        parts.pop(0)
    if len(parts) > 2:  # noqa: PLR2004
        keep = 3 if parts[-2] in _SECOND_LEVEL and len(parts[-1]) == 2 else 2  # noqa: PLR2004
        parts = parts[-keep:]
    return ".".join(parts)


def companies(sites: Iterable[tuple[str, str]]) -> list[Company]:
    """Ledger (company, sender host) pairs as companies; shared mail and ATS hosts are skipped."""
    names: dict[str, str] = {}
    domains: dict[str, set[str]] = {}
    for name, host in sites:
        key = name.casefold()
        if not key.strip():
            continue
        names.setdefault(key, name)
        base = base_domain(host or "")
        if base and "." in base and base not in _SHARED:
            domains.setdefault(key, set()).add(base)
    return [Company(names[key], frozenset(domains.get(key, ()))) for key in names]


def _under(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _compact(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", text.casefold())


def _name_forms(name: str) -> tuple[str, str]:
    """The company name without a legal suffix: as words and as one slug (for hosts and paths)."""
    words = [w for w in re.findall(r"\w+", name.casefold()) if w not in _SUFFIX_WORDS]
    return " ".join(words), "".join(words)


def job_site(
    domain: str | None, url: str | None, title: str | None, known: Iterable[Company]
) -> tuple[bool, str | None]:
    """(is it job-site time, the company's name when one is named).

    ATS and LinkedIn-jobs spans name a company only when its name is in the title (as a word) or
    in the host/path (as a slug); a company's own career host or path names it by domain.
    """
    host = (domain or "").lower()
    path = urlsplit(url or "").path.lower()
    books = list(known)
    for company in books:
        if any(_under(host, d) for d in company.domains) and (
            host.split(".")[0] in _CAREER_LABELS or re.match(r"/(\w\w/)?(careers|jobs)\b", path)
        ):
            return True, company.name
    ats = any(_under(host, d) for d in ATS_DOMAINS)
    if not ats and not (_under(host, "linkedin.com") and path.startswith("/jobs")):
        return False, None
    seen = (title or "").casefold()
    tokens, slug_scope = set(re.split(r"[^0-9a-z]+", host + " " + path)), _compact(host + path)
    best: tuple[int, str] | None = None
    for company in books:
        words, slug = _name_forms(company.name)
        if len(slug) < MIN_NAME_CHARS or slug in _PLATFORMS:
            continue
        in_url = slug in tokens or (len(slug) >= SLUG_INSIDE_CHARS and slug in slug_scope)
        if re.search(rf"(?<!\w){re.escape(words)}(?!\w)", seen) or in_url:
            best = max(best or (0, ""), (len(slug), company.name))
    return True, best[1] if best else None


def _spans(snap: timesink.Snapshot, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """This Mac's spans overlapping [start, end), aware UTC; the caller handles sqlite errors."""
    rows = snap.conn.execute(
        "SELECT start,end,appName,domain,title,url FROM span "
        "WHERE deviceID IS NULL AND start<? AND end>? AND end>start",
        (timesink._sql_date(end, ceil=True), timesink._sql_date(start, ceil=False)),  # noqa: SLF001
    )
    return [
        {**dict(row), "start": timesink._moment(row["start"]), "end": timesink._moment(row["end"])}  # noqa: SLF001
        for row in rows
    ]


def local_midnight(now: datetime, days_back: int = 0) -> datetime:
    """Local midnight ``days_back`` days before the local day of ``now``, as aware UTC."""
    day = now.astimezone().date() - timedelta(days=days_back)
    return datetime.combine(day, time.min).astimezone(UTC)


def _per_day(start: datetime, end: datetime) -> Iterable[tuple[str, float]]:
    """(local day, seconds) pieces of [start, end), split at local midnights."""
    cursor = start
    while cursor < end:
        day = cursor.astimezone().date()
        midnight = datetime.combine(day + timedelta(days=1), time.min).astimezone(UTC)
        stop = min(end, midnight)
        yield day.isoformat(), (stop - cursor).total_seconds()
        cursor = stop


def job_time(
    snap: timesink.Snapshot | None,
    known: Iterable[Company],
    now: datetime,
    days: int = WINDOW_DAYS,
) -> dict[str, Any] | None:
    """Seconds on job sites by company and local day over the last ``days`` days, up to ``now``.

    ``{"by_company": {casefolded name: {day: seconds}}, "other_s": seconds}``: ATS and
    LinkedIn-jobs time that names no ledger company is ``other_s``. None when unreadable.
    """
    if snap is None:
        return None
    books = list(known)
    start = local_midnight(now, days - 1)
    try:
        rows = _spans(snap, start, now)
    except (sqlite3.Error, ValueError, TypeError, OverflowError):
        return None
    by_company: dict[str, dict[str, float]] = {}
    other = 0.0
    for row in rows:
        is_job, name = job_site(row["domain"], row["url"], row["title"], books)
        if not is_job:
            continue
        for day, seconds in _per_day(max(row["start"], start), min(row["end"], now)):
            if name is None:
                other += seconds
            else:
                days_of = by_company.setdefault(name.casefold(), {})
                days_of[day] = days_of.get(day, 0) + seconds
    return {"by_company": by_company, "other_s": other}


def spent_view(found: Mapping[str, Any] | None, company: str) -> dict[str, Any]:
    """``time_spent`` / ``time_total_s`` for one ledger group, days oldest first."""
    days = {} if found is None else found["by_company"].get(company.casefold(), {})
    spent = [{"day": day, "seconds": round(s)} for day, s in sorted(days.items()) if round(s) > 0]
    return {"time_spent": spent, "time_total_s": sum(one["seconds"] for one in spent)}
