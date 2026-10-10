"""The ledger: the day, week and month facts about the user, computed from his own data.

Pure reads of memory.db (day summaries, job mail, records), the Event Log (commits, work-state,
reminders, daily reports), and TimeSink's spans, calls and project verdicts. Nothing here calls a
model or writes. :func:`standing_text` is blocks B-D (recent days, this week, last week, the last
30 days, the job hunt) as of a local midnight: complete days only, so the text is the same for every
turn of a day and sits in the cached prompt prefix. :func:`today_text` and :func:`since_text` are
blocks E-F, the part of today that midnight cannot hold; they are rendered per turn.

Every number is arithmetic over rows. A day's working hours are the union of its spans (two apps at
once count once). A working day starts at the first run of activity that ends after 05:00 and stops
at the last run that begins before 05:00 the next morning; a run is activity separated from the next
by less than four hours. Only the day's "The day" prose line, which the decision layer writes once a
night from these numbers, is model-made (ADR 0200).
"""

from __future__ import annotations

import re
import sqlite3
from bisect import bisect_left
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from typing import TYPE_CHECKING, Any, Final, NamedTuple

from jarvis.state import reminders, timesink
from jarvis.state.event_log import iter_events_of_types, open_runtime_event_log

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from pathlib import Path

# Bundle ids TimeSink spells two ways, folded onto one display name.
_APP_ALIAS: Final[dict[str, str]] = {
    "com.tencent.xinWeChat": "WeChat",
    "us.zoom.xos": "Zoom",
    "com.google.Chrome": "Chrome",
    "com.anthropic.claudefordesktop": "Claude",
    "com.mitchellh.ghostty": "Ghostty",
}
_RUN_GAP: Final[timedelta] = timedelta(hours=4)
_DAY_STARTS_AT: Final[time] = time(5)
_TOP_FLOOR_S: Final[float] = 180.0  # a project or app under three minutes is not named
_SESSION_FLOOR_S: Final[float] = 300.0
_CALL_FLOOR_S: Final[float] = 120.0
# A commit may be seen long after it was made.
_COMMIT_LOOKBACK: Final[timedelta] = timedelta(days=7)
_SESSION_APPS: Final[frozenset[str]] = frozenset({"Claude", "Ghostty"})
# Mail from these senders names the hiring platform, not the employer.
_ATS: Final[frozenset[str]] = frozenset(
    {
        "workable", "myworkday", "workday", "greenhouse", "lever", "icims", "smartrecruiters",
        "taleo", "ashby", "ashbyhq", "jobvite", "bamboohr", "clearco", "clearcompany", "njoyn",
        "linkedin", "indeed", "slfworkdaynotifications",
    },
)
_SUBJECT_COMPANY: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"thanks for applying to (.+?)(?:[.!]|$)",
        r"thank you for applying to (.+?)(?:[.!]|$)",
        r"thank you for your interest in (.+?)(?:[.!]|$)",
        r"applying at (.+?)(?:[.!]|$)",
        r"your application to (.+?)(?:[.!]|$)",
    )
)
_FILLER: Final[re.Pattern[str]] = re.compile(r"\b(the|inc|ltd|group|technologies|technology)\b")
_CLOCK: Final[re.Pattern[str]] = re.compile(
    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?", re.IGNORECASE,
)
_SECTIONS: Final[dict[str, str]] = {  # the headings of a day summary, in the order they are shown
    "Topics": "Talked about",
    "Decisions and facts the user stated": "He stated",
    "Unfinished": "Unfinished",
}
_RECORD_ID: Final[re.Pattern[str]] = re.compile(r"\s*\[record_id=[^\]]*\]")


@dataclass(frozen=True)
class LedgerSources:
    """Where the ledger reads, and the zone its days are cut in."""

    memory_db: Path
    event_log: Path
    timesink: Path | None
    zone: tzinfo


class _Span(NamedTuple):
    start: datetime
    end: datetime
    app: str
    title: str
    document: str
    project: str


class _Call(NamedTuple):
    start: datetime
    end: datetime
    app: str


@dataclass(frozen=True)
class _Mail:
    at: datetime
    sender: str
    subject: str
    kind: str
    company: str
    role: str
    event_at: str | None
    event_text: str | None
    message_id: str

    @property
    def cancelled(self) -> bool:
        return self.subject.lower().startswith("cancel")


def hm(seconds: float) -> str:
    """``95 minutes`` as ``1h35m``."""
    minutes = round(seconds / 60)
    return f"{minutes // 60}h{minutes % 60:02d}m"


def _merged(intervals: Iterable[tuple[datetime, datetime]]) -> float:
    """Seconds covered by the union of the intervals."""
    total, start, end = 0.0, None, None
    for begin, stop in sorted(intervals):
        if end is None or begin > end:
            if start is not None and end is not None:
                total += (end - start).total_seconds()
            start, end = begin, stop
        else:
            end = max(end, stop)
    if start is not None and end is not None:
        total += (end - start).total_seconds()
    return total


def _trim(text: str, size: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= size else text[: size - 1].rstrip() + "…"


def _top(
    seconds: Mapping[str, float], n: int, floor: float = _TOP_FLOOR_S,
) -> list[tuple[str, float]]:
    ranked = sorted(seconds.items(), key=lambda item: -item[1])
    return [(key, value) for key, value in ranked if key not in ("none", "") and value >= floor][:n]


def _fmt_top(items: Sequence[tuple[str, float]]) -> str:
    return ", ".join(f"{key} {hm(value)}" for key, value in items) or "none"


def _company_key(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9 ]", " ", _FILLER.sub(" ", name.lower()))
    return " ".join(cleaned.split())


def _real_company(company: str, subject: str, known: Mapping[str, str]) -> str:
    """The employer a mail is about: an ATS sender names the platform, so read the subject."""
    if company.lower() not in _ATS:
        return company
    for pattern in _SUBJECT_COMPANY:
        found = pattern.search(subject)
        if found:
            name = found.group(1).strip(" .!")
            return known.get(_company_key(name), name)
    return ""


def _start_from_text(event_text: str | None, event_at: str | None) -> str | None:
    """An interview's wall-clock start. The mail's own words beat the parsed ``event_at``."""
    if event_text:
        found = [
            (int(hour), int(minute or 0), (meridiem or "").lower()[:1])
            for hour, minute, meridiem in _CLOCK.findall(event_text)
            if 1 <= int(hour) <= 12 and (minute or meridiem)  # noqa: PLR2004 — 12-hour clock
        ]
        if found:
            shared = next((x[2] for x in found if x[2]), "")
            hour, minute, meridiem = found[0][0], found[0][1], found[0][2] or shared
            if meridiem == "p" and hour != 12:  # noqa: PLR2004
                hour += 12
            if meridiem == "a" and hour == 12:  # noqa: PLR2004
                hour = 0
            end = ""
            if len(found) > 1:
                hour2, minute2, meridiem2 = found[1][0], found[1][1], found[1][2] or shared
                if meridiem2 == "p" and hour2 != 12:  # noqa: PLR2004
                    hour2 += 12
                end = f"-{hour2:02d}:{minute2:02d}"
            return f"{hour:02d}:{minute:02d}{end}"
    if event_at:
        return datetime.fromisoformat(event_at).strftime("%H:%M")
    return None


def _app_name(bundle: str, name: str) -> str:
    return _APP_ALIAS.get(bundle, name)


class _Data:
    """Everything one render reads, loaded once for the window [lo, as_of]."""

    def __init__(
        self, src: LedgerSources, as_of: datetime, lo: datetime, *, screen: bool = True,
    ) -> None:
        self.src = src
        self.zone = src.zone
        self.as_of = as_of
        self.lo = lo
        self.spans, self.calls = self._timesink(src, lo, as_of) if screen else ([], [])
        self.starts = [span.start for span in self.spans]
        self.longest = max((span.end - span.start for span in self.spans), default=timedelta())
        self.union = self._union()
        self.mail = self._mail(src, as_of)
        self.commits = self._commits(src, lo, as_of)
        self.talks, self.seen = self._memory(src, lo, as_of)
        self._runs: list[tuple[datetime, datetime]] | None = None

    # --- loading ---------------------------------------------------------------------------

    @staticmethod
    def _timesink(
        src: LedgerSources, lo: datetime, hi: datetime,
    ) -> tuple[list[_Span], list[_Call]]:
        if src.timesink is None:
            return [], []
        with timesink.snapshot(src.timesink) as snap:
            if not isinstance(snap, timesink.Snapshot):
                return [], []
            try:
                projects = dict(snap.conn.execute("SELECT id,name FROM project").fetchall())
                verdict = {
                    (str(b), str(d or ""), str(t or ""), str(doc or "")): (
                        str(projects.get(p, "none"))
                    )
                    for b, d, t, doc, p in snap.conn.execute(
                        "SELECT appBundleID,domain,title,document,projectID FROM jevProjectVerdict",
                    )
                }
                rows = timesink.span_rows(snap, lo, hi)
                spans = sorted(
                    _Span(
                        row["start"], row["end"],
                        _app_name(str(row["appBundleID"]), str(row["appName"])),
                        str(row["title"] or ""), str(row["document"] or ""),
                        verdict.get(
                            (
                                str(row["appBundleID"]), str(row["domain"] or ""),
                                str(row["title"] or ""), str(row["document"] or ""),
                            ),
                            "none",
                        ),
                    )
                    for row in rows
                )
            except (sqlite3.Error, ValueError):
                return [], []
            try:  # callSpan is newer than span: an older TimeSink has none
                calls = [
                    _Call(
                        datetime.fromisoformat(timesink.moment(str(begin))),
                        datetime.fromisoformat(timesink.moment(str(end))),
                        _app_name(str(bundle), str(name)),
                    )
                    for begin, end, bundle, name in snap.conn.execute(
                        "SELECT start,end,appBundleID,appName FROM callSpan",
                    )
                ]
            except (sqlite3.Error, ValueError):
                calls = []
        return spans, calls

    def _union(self) -> list[tuple[datetime, datetime]]:
        out: list[list[datetime]] = []
        for begin, end in sorted((span.start, span.end) for span in self.spans):
            if out and begin <= out[-1][1]:
                out[-1][1] = max(out[-1][1], end)
            else:
                out.append([begin, end])
        return [(a, b) for a, b in out]

    @staticmethod
    def _mail(src: LedgerSources, as_of: datetime) -> list[_Mail]:
        with closing(sqlite3.connect(f"file:{src.memory_db}?mode=ro", uri=True)) as conn:
            try:
                rows = conn.execute(
                    "SELECT received_at,sender_name,subject,kind,company,role,event_at,event_text,"
                    "message_id,deleted FROM job_mail WHERE received_at IS NOT NULL "
                    "ORDER BY received_at",
                ).fetchall()
            except sqlite3.Error:
                return []
        parsed = [
            (datetime.fromisoformat(str(row[0])).astimezone(src.zone), row) for row in rows
        ]
        known: dict[str, str] = {}
        for at, row in parsed:
            if at <= as_of and row[4] and str(row[4]).lower() not in _ATS:
                known.setdefault(_company_key(str(row[4])), str(row[4]))
        out: list[_Mail] = []
        for at, row in parsed:
            if at > as_of or row[9]:
                continue
            sender, subject, company = str(row[1] or ""), str(row[2] or ""), str(row[4] or "")
            employer = _real_company(company, subject, known)
            if sender.lower() in _ATS or _company_key(sender) == _company_key(employer):
                sender = ""
            out.append(
                _Mail(
                    at, sender, subject, str(row[3] or ""), employer, str(row[5] or ""),
                    None if row[6] is None else str(row[6]),
                    None if row[7] is None else str(row[7]), str(row[8]),
                ),
            )
        return out

    @staticmethod
    def _commits(src: LedgerSources, lo: datetime, as_of: datetime) -> list[tuple[datetime, str]]:
        since = int((lo - _COMMIT_LOOKBACK).timestamp() * 1000)
        seen: set[str] = set()
        out: list[tuple[datetime, str]] = []
        with closing(open_runtime_event_log(src.event_log)) as conn:
            for event in iter_events_of_types(conn, ("project.commit_seen",), since_epoch_ms=since):
                sha = str(event.payload["commit_sha"])
                if sha in seen:
                    continue
                at = datetime.fromtimestamp(int(event.payload["committed_at_ms"]) / 1000, src.zone)
                if lo <= at <= as_of:
                    seen.add(sha)
                    out.append((at, str(event.payload["repo_path"])))
        return sorted(out)

    @staticmethod
    def _memory(
        src: LedgerSources, lo: datetime, as_of: datetime,
    ) -> tuple[list[datetime], list[tuple[datetime, str]]]:
        with closing(sqlite3.connect(f"file:{src.memory_db}?mode=ro", uri=True)) as conn:
            talks = [
                at
                for (ts,) in conn.execute("SELECT ts FROM records WHERE source='allen' ORDER BY ts")
                if lo <= (at := datetime.fromisoformat(str(ts)).astimezone(src.zone)) <= as_of
            ]
            try:
                seen = [
                    (at, str(verdict))
                    for ts, verdict in conn.execute(
                        "SELECT received_at,verdict FROM job_seen WHERE received_at IS NOT NULL",
                    )
                    if lo <= (at := datetime.fromisoformat(str(ts)).astimezone(src.zone)) <= as_of
                ]
            except sqlite3.Error:
                seen = []
        return talks, seen

    # --- spans -------------------------------------------------------------------------------

    def clip(self, begin: datetime, end: datetime) -> list[_Span]:
        """Spans overlapping [begin, end), cut to it."""
        end = min(end, self.as_of)
        out: list[_Span] = []
        first = bisect_left(self.starts, begin - self.longest)
        for span in self.spans[first:]:
            if span.start >= end:
                break
            if span.end > begin:
                out.append(span._replace(start=max(span.start, begin), end=min(span.end, end)))
        return out

    def day(self, day: date) -> tuple[datetime, datetime]:
        begin = datetime.combine(day, time.min, self.zone)
        return begin, datetime.combine(day + timedelta(days=1), time.min, self.zone)

    def runs(self) -> list[tuple[datetime, datetime]]:
        """Runs of activity separated by gaps of four hours or more, cut at ``as_of``."""
        if self._runs is None:
            out: list[tuple[datetime, datetime]] = []
            for begin, end in self.union:
                if begin >= self.as_of:
                    break
                stop = min(end, self.as_of)
                if out and begin - out[-1][1] < _RUN_GAP:
                    out[-1] = (out[-1][0], max(out[-1][1], stop))
                else:
                    out.append((begin, stop))
            self._runs = out
        return self._runs

    def bounds(self, day: date) -> tuple[datetime, datetime] | None:
        """(started, stopped) of the working day, which may have begun the evening before."""
        cut = datetime.combine(day, _DAY_STARTS_AT, self.zone)
        runs = [run for run in self.runs() if run[1] > cut and run[0] < cut + timedelta(days=1)]
        if not runs:
            return None
        return runs[0][0].astimezone(self.zone), runs[-1][1].astimezone(self.zone)

    def calls_seconds(self, begin: datetime, end: datetime) -> dict[str, float]:
        end = min(end, self.as_of)
        by: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
        for call in self.calls:
            if call.end > begin and call.start < end:
                by[call.app].append((max(call.start, begin), min(call.end, end)))
        return {app: _merged(iv) for app, iv in by.items()}

    def seconds_by(
        self, spans: Iterable[_Span], key: Callable[[_Span], str],
    ) -> dict[str, float]:
        by: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
        for span in spans:
            by[key(span)].append((span.start, span.end))
        return {k: _merged(iv) for k, iv in by.items()}

    def active(self, spans: Iterable[_Span]) -> float:
        return _merged((span.start, span.end) for span in spans)

    def commit_repos(self, begin: datetime, end: datetime) -> Counter[str]:
        return Counter(
            repo.rstrip("/").rsplit("/", 1)[-1] for at, repo in self.commits if begin <= at < end
        )

    def talks_between(self, begin: datetime, end: datetime) -> int:
        return sum(1 for at in self.talks if begin <= at < end)

    def mail_between(self, begin: datetime, end: datetime) -> list[_Mail]:
        return [mail for mail in self.mail if begin <= mail.at < end]


# ------------------------------------------------------------------ job mail text
def _event_lines(rows: Sequence[_Mail]) -> list[str]:
    """One phrase per job mail, a repeated phrase folded into ``x2``."""
    found: list[str] = []
    for mail in rows:
        who = f", from {mail.sender}" if mail.sender else ""
        role = mail.role + ")" * max(0, mail.role.count("(") - mail.role.count(")"))
        company = mail.company or "an ATS mail"
        if mail.cancelled:
            found.append(f"interview cancelled ({company}{who})")
        elif mail.kind == "receipt":
            found.append(f"application confirmed ({company}{', ' + role if role else ''}{who})")
        elif mail.kind == "rejection":
            found.append(f"rejection ({company}{who})")
        elif mail.kind == "interview":
            found.append(f"interview mail ({company}{who})")
        elif mail.kind == "offer":
            found.append(f"offer ({company}{who})")
    return [line if n == 1 else f"{line} x{n}" for line, n in Counter(found).items()]


# ------------------------------------------------------------------ one day
@dataclass(frozen=True)
class _Day:
    day: date
    active: float
    bounds: tuple[datetime, datetime] | None
    projects: list[tuple[str, float]]
    apps: list[tuple[str, float]]
    sessions: list[tuple[str, float]]
    calls: dict[str, float]
    commits: Counter[str]
    job_events: list[str]
    mail_total: int
    mail_job: int
    talks: int


def _day(data: _Data, day: date) -> _Day:
    begin, end = data.day(day)
    spans = data.clip(begin, end)
    named = [
        span for span in spans
        if span.document and span.document != "Claude" and span.app in _SESSION_APPS
    ]
    job = data.mail_between(begin, end)
    return _Day(
        day, data.active(spans), data.bounds(day),
        _top(data.seconds_by(spans, lambda s: s.project), 3),
        _top(data.seconds_by(spans, lambda s: s.app), 4),
        _top(data.seconds_by(named, lambda s: s.document), 4, _SESSION_FLOOR_S),
        data.calls_seconds(begin, end), data.commit_repos(begin, end), _event_lines(job),
        sum(1 for at, _ in data.seen if begin <= at < end), len(job),
        data.talks_between(begin, end),
    )


def _fmt_calls(calls: Mapping[str, float]) -> str:
    ranked = sorted(calls.items(), key=lambda item: -item[1])
    return ", ".join(
        f"{app} call {hm(seconds)}" for app, seconds in ranked if seconds >= _CALL_FLOOR_S
    )


def _fmt_bounds(
    day: date, bounds: tuple[datetime, datetime] | None, *, still_on: bool = False,
) -> str:
    if not bounds:
        return "no recorded activity"
    first, last = bounds
    start = f"{first:%H:%M} the evening before" if first.date() < day else f"{first:%H:%M}"
    if still_on:
        return f"started {start}, still active"
    stop = f"{last:%H:%M} (after midnight)" if last.date() > day else f"{last:%H:%M}"
    return f"started {start}, stopped {stop}"


def _day_text(row: _Day) -> str:
    calls = _fmt_calls(row.calls)
    lines = [
        f"{row.day.isoformat()} {row.day:%a}",
        f"  Computer: {_fmt_bounds(row.day, row.bounds)}, active {hm(row.active)}"
        + (f"; {calls}" if calls else ""),
    ]
    if row.projects:
        lines.append(f"  Projects (TimeSink): {_fmt_top(row.projects)}")
    if row.apps:
        lines.append(f"  Top apps: {_fmt_top(row.apps)}")
    if row.sessions:
        lines.append(
            "  Claude Code sessions: " + "; ".join(f"{k} {hm(v)}" for k, v in row.sessions),
        )
    if row.commits:
        lines.append("  Commits: " + ", ".join(f"{k} {v}" for k, v in row.commits.most_common()))
    if row.job_events:
        lines.append("  Job events: " + "; ".join(row.job_events))
    lines.append(
        f"  Mail: {row.mail_total} screened, {row.mail_job} job-related. "
        f"Talked to her {row.talks} times.",
    )
    return "\n".join(lines)


def _compact_text(row: _Day) -> str:
    if row.bounds is None:
        span = "no activity"
    else:
        first, last = row.bounds
        span = (
            f"{first:%H:%M}" + (" (eve before)" if first.date() < row.day else "")
            + f"-{last:%H:%M}" + (" (after midnight)" if last.date() > row.day else "")
        )
    calls = _fmt_calls(row.calls)
    return (
        f"{row.day:%m-%d} {row.day:%a}: {span}, active {hm(row.active)}; "
        f"projects {_fmt_top(row.projects[:2])}; commits {sum(row.commits.values())}; "
        f"jobs: {'; '.join(row.job_events) or 'none'}" + (f"; {calls}" if calls else "")
    )


def day_numbers_text(src: LedgerSources, day: date, as_of: datetime) -> str:
    """One day's numbers as the "Last seven days in full" block prints them (the prose input)."""
    begin = datetime.combine(day, time.min, src.zone)
    return _day_text(_day(_Data(src, as_of, begin - timedelta(days=1)), day))


def day_active(src: LedgerSources, day: date, as_of: datetime) -> bool:
    """Whether the day has any span, job mail or word of his: a "The day" line is worth writing."""
    lo = datetime.combine(day, time.min, src.zone) - timedelta(days=1)
    row = _day(_Data(src, as_of, lo), day)
    return bool(row.active or row.mail_total or row.job_events or row.talks or row.commits)


# ------------------------------------------------------------------ memory.db and the Event Log
def _day_summaries(src: LedgerSources, as_of: datetime) -> dict[str, str]:
    """The newest summary row of each day, as of ``as_of``."""
    out: dict[str, str] = {}
    with closing(sqlite3.connect(f"file:{src.memory_db}?mode=ro", uri=True)) as conn:
        rows = conn.execute("SELECT day,ts,summary FROM day_summaries ORDER BY day,ts")
        for day, ts, summary in rows:
            if datetime.fromisoformat(str(ts)) <= as_of:
                out[str(day)] = str(summary)
    return out


def _day_prose(src: LedgerSources, as_of: datetime) -> dict[str, str]:
    """The newest "The day" line of each day, as of ``as_of``; none before the table exists."""
    out: dict[str, str] = {}
    with closing(sqlite3.connect(f"file:{src.memory_db}?mode=ro", uri=True)) as conn:
        try:
            rows = conn.execute("SELECT day,ts,text FROM day_prose ORDER BY day,ts").fetchall()
        except sqlite3.Error:
            return out
    for day, ts, text in rows:
        if datetime.fromisoformat(str(ts)) <= as_of:
            out[str(day)] = str(text)
    return out


def _sections(summary: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    heading: str | None = None
    for line in summary.splitlines():
        if line.startswith("### "):
            heading = line[4:].strip()
            found[heading] = []
        elif heading and line.startswith("- "):
            found[heading].append(_RECORD_ID.sub("", line[2:]).strip())
    return found


_CAPS: Final[dict[str, tuple[int, int]]] = {  # heading: (items shown, characters per item)
    "Topics": (4, 150),
    "Decisions and facts the user stated": (4, 170),
    "Unfinished": (3, 150),
}


def day_report(src: LedgerSources, day: date, as_of: datetime) -> str | None:
    """The day's saved work report (``daily-report-<date>``), or None."""
    since = int((datetime.combine(day, time.min, src.zone) - timedelta(days=1)).timestamp() * 1000)
    cut = int(as_of.timestamp() * 1000)
    best: str | None = None
    with closing(open_runtime_event_log(src.event_log)) as conn:
        for event in iter_events_of_types(conn, ("briefing.revised",), since_epoch_ms=since):
            action = str(event.payload.get("action_id", ""))
            mine = action.startswith(f"daily-report-{day.isoformat()}") and "regen" not in action
            if event.ts_epoch_ms <= cut and mine:
                item = event.payload.get("item")
                if isinstance(item, dict) and isinstance(item.get("content"), str):
                    best = item["content"]
    return best


# ------------------------------------------------------------------ blocks B-D (standing)
def _recent_days(  # noqa: PLR0913 — the data, the window sizes and the two note maps.
    data: _Data, today: date, full: int, compact: int,
    summaries: Mapping[str, str], prose: Mapping[str, str],
) -> str:
    lines = [
        "[Recent days · numbers computed by the program from TimeSink, mail, commits and "
        "conversation; the prose lines were written from them]",
        "Earlier days, one line each "
        "(started/stopped = first and last activity of the working day):",
    ]
    lines += [
        "  " + _compact_text(_day(data, today - timedelta(days=k)))
        for k in range(full + compact, full, -1)
    ]
    lines.append(f"Last {'seven' if full == 7 else full} days in full:")  # noqa: PLR2004
    for k in range(full, 0, -1):
        day = today - timedelta(days=k)
        text = _day_text(_day(data, day))
        summary = summaries.get(day.isoformat())
        if summary:
            sections = _sections(summary)
            for heading, label in _SECTIONS.items():
                items = sections.get(heading) or []
                count, size = _CAPS[heading]
                if items:
                    shown = " | ".join(_trim(item, size) for item in items[:count])
                    text += f"\n  {label}: {shown}"
        line = prose.get(day.isoformat())
        if line:
            text += "\n  The day: " + " ".join(line.split())
        lines.append(text)
    return "\n".join(lines)


@dataclass(frozen=True)
class _Window:
    active: float
    projects: list[tuple[str, float]]
    commits: int
    applications: int
    interviews: int


def _window(data: _Data, begin: datetime, end: datetime) -> _Window:
    end = min(end, data.as_of)
    spans = data.clip(begin, end)
    mail = data.mail_between(begin, end)
    return _Window(
        data.active(spans), _top(data.seconds_by(spans, lambda s: s.project), 4),
        sum(1 for at, _ in data.commits if begin <= at < end),
        len({m.company for m in mail if m.kind == "receipt"}),
        len({m.company for m in mail if m.kind == "interview" and not m.cancelled}),
    )


def _window_text(window: _Window) -> str:
    n = window.interviews
    return (
        f"active {hm(window.active)}; projects {_fmt_top(window.projects)}; "
        f"commits {window.commits}; "
        f"applications confirmed {window.applications}; "
        f"interview mails from {n} {'company' if n == 1 else 'companies'}"
    )


def _day_brief(data: _Data, day: date) -> str:
    begin, end = data.day(day)
    spans = data.clip(begin, end)
    bounds = data.bounds(day)
    span = f"{bounds[0]:%H:%M}-{bounds[1]:%H:%M}" if bounds else "-"
    projects = _fmt_top(_top(data.seconds_by(spans, lambda s: s.project), 2))
    return f"{day:%a %m-%d} {hm(data.active(spans))} ({span}; {projects})"


def _week_extras(data: _Data, start: datetime, end: datetime) -> str:
    """Per-project hours of the week and its busiest and quietest complete day."""
    spans = data.clip(start, end)
    lines = [f"  Per project: {_fmt_top(_top(data.seconds_by(spans, lambda s: s.project), 8))}"]
    complete: list[tuple[date, float]] = []
    for i in range(7):
        day = start.date() + timedelta(days=i)
        begin, stop = data.day(day)
        if begin >= data.as_of:
            break
        if stop <= data.as_of:
            complete.append((day, data.active(data.clip(begin, stop))))
    if complete:
        high = max(complete, key=lambda x: x[1])
        low = min(complete, key=lambda x: x[1])
        lines.append(
            f"  Busiest day: {high[0]:%a %m-%d} {hm(high[1])}. "
            f"Quietest complete day: {low[0]:%a %m-%d} {hm(low[1])}.",
        )
    return "\n".join(lines)


def _weeks(data: _Data) -> str:
    local = data.as_of.astimezone(data.zone)
    monday = datetime.combine(local.date() - timedelta(days=local.weekday()), time.min, data.zone)
    prior = monday - timedelta(days=7)
    out: list[str] = []
    if data.as_of > monday:  # a Monday midnight has no complete day of its week yet
        this = _window(data, monday, data.as_of)
        same = _window(data, prior, prior + (data.as_of - monday))
        days = "; ".join(
            _day_brief(data, monday.date() + timedelta(days=i))
            for i in range((data.as_of - monday).days)
        )

        def delta(x: float, y: float) -> str:
            return f"{(x - y) / 3600:+.1f}h" if x or y else "n/a"

        out += [
            f"[This week so far · Mon {monday:%m-%d} "
            f"to {(data.as_of - timedelta(days=1)):%a %m-%d}; "
            "today is in Today so far]",
            f"  {_window_text(this)}",
            f"  Day by day (active time, started-stopped; top projects): {days}",
            _week_extras(data, monday, monday + timedelta(days=7)),
            f"  vs the same stretch of last week: active {delta(this.active, same.active)} "
            f"(last week then: {hm(same.active)}), commits {this.commits - same.commits:+d}, "
            f"applications {this.applications - same.applications:+d}",
        ]
    last = _window(data, prior, monday)
    last_days = "; ".join(_day_brief(data, prior.date() + timedelta(days=i)) for i in range(7))
    month = _window(data, data.as_of - timedelta(days=30), data.as_of)
    out += [
        f"[Last week · {prior:%m-%d} to {(monday - timedelta(days=1)):%m-%d}] {_window_text(last)}",
        f"  Day by day: {last_days}",
        _week_extras(data, prior, monday),
        f"[Last 30 days] {_window_text(month)}",
    ]
    return "\n".join(out)


def _by_company(mail: Sequence[_Mail]) -> dict[str, dict[str, list[_Mail]]]:
    comp: dict[str, dict[str, list[_Mail]]] = defaultdict(
        lambda: {"receipt": [], "rejection": [], "interview": [], "offer": []},
    )
    for m in mail:
        if m.company:
            kind = "rejection" if m.cancelled else m.kind
            if kind in comp[m.company]:
                comp[m.company][kind].append(m)
    return comp


def _invitation_line(mail: _Mail, as_of: datetime, zone: tzinfo, company: str | None = None) -> str:
    assert mail.event_at is not None  # noqa: S101 — callers pass dated mail only
    when = datetime.fromisoformat(mail.event_at).astimezone(zone)
    start = _start_from_text(mail.event_text, mail.event_at)
    state = "upcoming" if when > as_of else "past"
    return (
        f"  {company or mail.company} ({mail.role or 'co-op'}): latest dated invitation "
        f"{when:%a %Y-%m-%d} at {start} Pacific [{state}; from the mail, received "
        f"{mail.at:%m-%d %H:%M}; the mail text says: {_trim(mail.event_text or '', 80)}]"
    )


def _cancel_line(mail: _Mail) -> str:
    return (
        f"  {mail.company}: cancellation notice '{_trim(mail.subject, 70)}' "
        f"received {mail.at:%m-%d %H:%M}"
    )


def _job_hunt(data: _Data) -> str:
    comp = _by_company(data.mail)

    def who(company: str, kind: str) -> str:
        sender = next((m.sender for m in comp[company][kind] if m.sender), "")
        return f"{company} ({sender})" if sender else company

    applied = sorted(c for c, v in comp.items() if v["receipt"])
    rejected = sorted(
        c for c, v in comp.items()
        if v["rejection"] and not any(m.cancelled for m in v["rejection"])
    )
    interview = sorted(c for c, v in comp.items() if v["interview"])
    offers = sorted(c for c, v in comp.items() if v["offer"])
    every = sorted(c for c, v in comp.items() if any(v.values()))
    out = [
        "[Job hunt as of now · from job mail; ATS senders mapped to the real company; contact name "
        f"in brackets] {len(every)} companies have confirmation, rejection or interview mail.",
        f"  Application confirmed ({len(applied)}): "
        f"{', '.join(who(c, 'receipt') for c in applied) or 'none'}",
        f"  Rejected ({len(rejected)}): "
        f"{', '.join(who(c, 'rejection') for c in rejected) or 'none'}",
        f"  Interview mail ({len(interview)}): "
        f"{', '.join(who(c, 'interview') for c in interview) or 'none'}",
    ]
    if offers:
        out.append(f"  Offer mail ({len(offers)}): {', '.join(who(c, 'offer') for c in offers)}")
    for company in interview:
        dated = [m for m in comp[company]["interview"] if m.event_at]
        if dated:
            out.append(_invitation_line(max(dated, key=lambda m: m.at), data.as_of, data.zone))
        out += [_cancel_line(m) for m in data.mail if m.company == company and m.cancelled]
    return "\n".join(out)


def notes_stamp(src: LedgerSources) -> tuple[str, str]:
    """The newest ``ts`` of the day summaries and of the "The day" lines; "" for none or no table.

    Changes exactly when :func:`standing_text` has a new note to show, so it keys a cache.
    """
    newest: list[str] = []
    with closing(sqlite3.connect(f"file:{src.memory_db}?mode=ro", uri=True)) as conn:
        for table in ("day_summaries", "day_prose"):
            try:
                row = conn.execute(f"SELECT MAX(ts) FROM {table}").fetchone()  # noqa: S608 — fixed names
            except sqlite3.Error:
                row = None
            newest.append(str(row[0]) if row and row[0] else "")
    return newest[0], newest[1]


def standing_text(
    src: LedgerSources, midnight: datetime, *, full_days: int = 7, compact_days: int = 7,
    notes_as_of: datetime | None = None,
) -> str:
    """Blocks B-D as of a local midnight: complete days only, one text for every turn of the day.

    The day summaries and "The day" lines shown are those stored by ``notes_as_of`` (default: the
    midnight), so a note written after midnight reaches the text without moving its numbers.
    """
    today = midnight.astimezone(src.zone).date()
    reach = max(full_days + compact_days, 31) + 7  # 30-day window, two weeks of weekly blocks
    data = _Data(src, midnight, midnight - timedelta(days=reach))
    summaries = _day_summaries(src, notes_as_of or midnight)
    prose = _day_prose(src, notes_as_of or midnight)
    return "\n\n".join(
        (
            _recent_days(data, today, full_days, compact_days, summaries, prose),
            _weeks(data),
            _job_hunt(data),
        ),
    )


# ------------------------------------------------------------------ blocks E-F (per turn)
def _work_state(src: LedgerSources, as_of: datetime) -> str | None:
    cut = int(as_of.timestamp() * 1000)
    since = int((as_of - timedelta(days=14)).timestamp() * 1000)
    latest: Mapping[str, Any] | None = None
    with closing(open_runtime_event_log(src.event_log)) as conn:
        for event in iter_events_of_types(conn, ("work_state.revised",), since_epoch_ms=since):
            if event.ts_epoch_ms <= cut:
                item = event.payload.get("item")
                latest = item if isinstance(item, dict) else latest
    if latest is None:
        return None
    acts = [_trim(str(a.get("text", "")), 230) for a in latest.get("activities", [])[:3]]
    analysed = str(latest.get("analyzed_at", "?"))[:16]
    return f"  Latest work-state summary (analysed {analysed}): " + " | ".join(acts)


def _reminders(src: LedgerSources, as_of: datetime, day: date) -> list[str]:
    with closing(open_runtime_event_log(src.event_log)) as conn:
        found = reminders.fold(conn, until_ms=int(as_of.timestamp() * 1000))
    pending = sorted((r for r in found.values() if r.pending), key=lambda r: r.due_at_ms)

    def due(r: reminders.Reminder) -> datetime:
        return datetime.fromtimestamp(r.due_at_ms / 1000, src.zone)

    lines = [
        f"  Pending reminders ({len(pending)}): "
        + ("; ".join(f"{due(r):%a %m-%d %H:%M} {r.text}" for r in pending) or "none"),
    ]
    fired = sorted(
        (r for r in found.values() if r.fired_at_ms is not None and due(r).date() == day),
        key=lambda r: r.due_at_ms,
    )
    if fired:
        lines.append(
            "  Fired earlier today: " + "; ".join(f"{due(r):%H:%M} {r.text}" for r in fired),
        )
    return lines


def today_text(src: LedgerSources, now: datetime) -> str:
    """Block E: today since midnight, what the midnight text cannot hold."""
    local = now.astimezone(src.zone)
    today = local.date()
    midnight = datetime.combine(today, time.min, src.zone)
    monday = midnight - timedelta(days=local.weekday())
    data = _Data(src, now, min(midnight - timedelta(days=2), monday - timedelta(days=1)))
    spans = data.clip(midnight, now)
    out = [f"[Today so far · {local:%a %Y-%m-%d}, now {local:%H:%M}]"]
    bounds = data.bounds(today)
    if spans and bounds:
        calls = _fmt_calls(data.calls_seconds(midnight, now))
        apps = _fmt_top(_top(data.seconds_by(spans, lambda s: s.app), 3))
        out.append(
            f"  Computer: {_fmt_bounds(today, bounds, still_on=True)}, active today "
            f"{hm(data.active(spans))}; top apps {apps}" + (f"; {calls}" if calls else ""),
        )
    else:
        out.append("  Computer: no activity recorded yet today")
    if monday < midnight:
        week_spans = data.clip(monday, now)
        out.append(
            f"  This week including today: active {hm(data.active(week_spans))}; per project "
            f"{_fmt_top(_top(data.seconds_by(week_spans, lambda s: s.project), 8))}",
        )
    state = _work_state(src, now)
    if state:
        out.append(state)
    mail = data.mail_between(midnight, now)
    if mail:
        events = "; ".join(f"{m.at:%H:%M} {e}" for m in mail for e in _event_lines([m]))
        out.append(f"  Job mail today: {len(mail)}, {events}")
        for m in mail:
            if m.kind == "interview" and m.event_at and not m.cancelled:
                out.append(_invitation_line(m, now, src.zone))
            if m.cancelled:
                out.append(_cancel_line(m))
    seen = sum(1 for at, _ in data.seen if midnight <= at < now)
    out.append(f"  Mail screened today: {seen}")
    out += _reminders(src, now, today)
    return "\n".join(out)


def since_text(src: LedgerSources, now: datetime, last_talk: datetime | None) -> str:
    """Block F: what happened since he last talked to her; empty when he never has."""
    if last_talk is None:
        return ""
    data = _Data(src, now, last_talk - timedelta(seconds=1), screen=False)
    out = [f"[Since you last talked to her ({last_talk.astimezone(src.zone):%a %m-%d %H:%M})]"]
    for mail in data.mail:
        if mail.at > last_talk:
            out += [f"  Mail {mail.at:%m-%d %H:%M}: {line}" for line in _event_lines([mail])]
    screened = sum(1 for at, _ in data.seen if last_talk < at <= now)
    if screened:
        out.append(f"  Mail received since: {screened}")
    lo, hi = int(last_talk.timestamp() * 1000), int(now.timestamp() * 1000)
    with closing(open_runtime_event_log(src.event_log)) as conn:
        texts = {r.reminder_id: r.text for r in reminders.fold(conn, until_ms=hi).values()}
        for event in iter_events_of_types(conn, ("reminder.fired",), since_epoch_ms=lo):
            if lo < event.ts_epoch_ms <= hi:
                when = datetime.fromtimestamp(event.ts_epoch_ms / 1000, src.zone)
                what = texts.get(str(event.payload["reminder_id"]), "?")
                out.append(f"  Reminder fired {when:%H:%M}: {what}")
    commits = sum(1 for at, _ in data.commits if last_talk < at <= now)
    if commits:
        out.append(f"  Commits since: {commits}")
    if len(out) == 1:
        out.append("  Nothing new: no mail, reminders or commits.")
    return "\n".join(out)


__all__ = [
    "LedgerSources",
    "day_active",
    "day_numbers_text",
    "day_report",
    "hm",
    "notes_stamp",
    "since_text",
    "standing_text",
    "today_text",
]
