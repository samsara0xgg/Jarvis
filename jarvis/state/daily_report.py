"""The whole of one local calendar day as keyed evidence (ADR 0028) and the report's save path.

The daily work report reads the same stores as the daily-loop tools, but as a
runtime job: whole-day queries served whole — every window, capture, record,
commit, state event and session turn — keys that map back to durable source
references, the originals behind a claim for the verification call, and one
save through the briefing revision store. The only limit is the model's
context, and when a day exceeds it the largest source falls back to one full
line per item and the header says so.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jarvis.shared.lang import TEXT, t
from jarvis.state import daily_store, device_reads, timesink
from jarvis.state.daily_contract import DailyError, fingerprint
from jarvis.state.daily_records import read_connection
from jarvis.state.daily_store import current_items, high_water

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from jarvis.shared.device_link import DeviceLink

ALLEN_SOURCE = "allen"
MAX_DETAILS = 10
MAX_HITS = 20
MATERIAL_BUDGET = 850_000
"""Characters of material (as JSON) one request may carry. OpenAI bills a request over 272K
input tokens at double rate; this material runs about 3.3 characters a token (2026-09-23/24:
~800K characters, ~244K tokens), so the first request stays near 260K. Nothing is cut below it;
above it, whole sources fall back to index lines in ``_FALLBACK_ORDER`` and the header says so."""
_FALLBACK_ORDER = ("screen", "agent_answers", "agent_asks", "records")
_NEAR_DUPLICATE = 0.9
"""Consecutive captures of one window whose text matches this closely are one screen state."""
_SNIPPET_BEFORE = 60
_SNIPPET_AFTER = 100
_ACTIVITY_TYPES = ("repo.state_observed", "project.commit_seen")
_INDEX_TEXT = 100
"""Characters of text an index line keeps once a source has fallen back."""
_SHIM_PREFIX = "The following is the Codex agent history whose request action you are assessing"
"""Codex's own approval sessions: two turns of boilerplate per permission request, not work."""
MILESTONES = (
    "application submitted",
    "application received",
    "thank you for applying",
    "received your application",
    "successfully submitted",
    "submitted successfully",
    "submission successful",
    "order placed",
    "order confirmed",
    "your order has been",
    "booking confirmed",
    "payment received",
    "payment successful",
    "message sent",
    "email sent",
    "registration complete",
    "you're registered",
    "已提交",
    "提交成功",
    "已发送",
    "发送成功",
    "报名成功",
    "申请已",
    "已收到你的",
    "预订成功",
    "支付成功",
)
"""Phrases a page shows when something happened in the world: every capture holding one is
listed again at the top of the material, so a 0.4-minute confirmation page cannot go unread.
ponytail: a fixed list; a page that says it differently is reached by search, not by the index."""
_LOCALTIME = Path("/etc/localtime")
_GIT_TIMEOUT_S = 5.0
_FIELD_SEP = "\x1f"
_MAIN = "main"
_GIT_WALK = "--max-count=5000"
"""The newest commits walked per repository; the day is filtered here, not by ``--since``.

``--since`` stops walking at the first commit older than the date, so one
rebased or amended commit with an old date hides everything beneath it.
ponytail: 5000 newest commits per walk; raise it if a repository ever holds
more than that in one day.
"""


def resolve_zone(name: str | None, configured: tzinfo | None) -> tuple[str, tzinfo]:
    """The report's zone: the argument, else the configured zone, else the system's IANA zone."""
    if name is not None:
        try:
            return name, ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            msg = f"timezone must be an IANA zone name: {name!r}"
            raise DailyError(msg) from exc
    if isinstance(configured, ZoneInfo) and configured.key:
        return configured.key, configured
    try:
        parts = _LOCALTIME.resolve().parts
        key = "/".join(parts[parts.index("zoneinfo") + 1 :])
        return key, ZoneInfo(key)
    except (OSError, ValueError, ZoneInfoNotFoundError):
        return "UTC", UTC


def resolve_day(local_date: str | None, zone: tzinfo, now: datetime) -> date:
    """Yesterday by the local calendar, or the given day, which cannot lie in the future."""
    today = now.astimezone(zone).date()
    if local_date is None:
        return today - timedelta(days=1)
    try:
        day = date.fromisoformat(local_date)
        if day.isoformat() != local_date:
            raise ValueError  # noqa: TRY301 — noncanonical dates share the message below.
    except ValueError as exc:
        msg = "local_date must be YYYY-MM-DD"
        raise DailyError(msg) from exc
    if day > today:
        msg = f"local_date {local_date} is after today ({today.isoformat()}) in that zone"
        raise DailyError(msg)
    return day


def day_window(day: date, zone: tzinfo, now: datetime) -> tuple[datetime, datetime, bool]:
    """The local calendar day [00:00, 24:00), cut at ``now`` while the day is still running."""
    start = datetime.combine(day, time(), tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time(), tzinfo=zone)
    # Every rendered bound is local: a day cut at "now" must not print in UTC.
    return start, min(end, now.astimezone(zone)), end > now


def existing_report(conn: sqlite3.Connection, day: str, zone: str) -> dict[str, Any] | None:
    """The saved briefing's first chunk and metadata, or None."""
    try:
        return daily_store.get_briefing(conn, {"local_date": day, "timezone": zone})
    except DailyError as exc:
        if exc.code == "not_found":
            return None
        raise


@dataclass(frozen=True)
class DayEvidence:
    """Everything the report model may read; keys map back to durable source refs."""

    day: str
    zone: str
    window: dict[str, Any]
    coverage: dict[str, str]
    counts: dict[str, int]
    limits: list[str]
    observed_until: str | None
    sections: dict[str, list[dict[str, Any]]]
    refs: dict[str, str] = field(default_factory=dict)
    """Every key the model may cite; every one is rendered, whole or as an index line."""
    stated: frozenset[str] = frozenset()
    commits: dict[str, str] = field(default_factory=dict)
    """Same-day commit refs and their short SHAs: what a proof label may name."""
    haystack: dict[str, str] = field(default_factory=dict)
    """Key -> searchable text for windows, records, commits and sessions; captures are searched
    in the store, whose text was never copied here."""
    commit_rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Every commit of the day by key, for the detail and the check behind a commit key."""
    served: dict[str, str] = field(default_factory=dict)
    """Per source, what the material holds: nothing recorded, unreadable, whole, or which part
    fell back to index lines and how much text that withheld."""

    @property
    def empty(self) -> bool:
        """True when the day holds no activity at all (context alone is not a day)."""
        return not any(
            self.sections.get(key) for key in ("windows", "screen", "records", "git", "agent")
        )


def _flat(text: str) -> str:
    return " ".join(str(text).split())


def _row_id(item: dict[str, Any]) -> str:
    return str(item["source_refs"][0].split(":")[2])


@dataclass
class _Gather:
    """Mutable scratch shared by the section builders of one day."""

    zone: tzinfo
    start: datetime
    end: datetime
    day: date
    refs: dict[str, str] = field(default_factory=dict)
    sections: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    coverage: dict[str, str] = field(default_factory=dict)
    limits: list[str] = field(default_factory=list)
    served: dict[str, str] = field(default_factory=dict)
    stated: set[str] = field(default_factory=set)
    commits: dict[str, str] = field(default_factory=dict)
    haystack: dict[str, str] = field(default_factory=dict)
    commit_rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    latest: datetime | None = None

    def clock(self, value: str) -> str:
        return datetime.fromisoformat(value).astimezone(self.zone).strftime("%H:%M")

    def day_clock(self, value: str) -> str:
        return datetime.fromisoformat(value).astimezone(self.zone).strftime("%m-%d %H:%M")

    def saw(self, value: str) -> None:
        moment = min(datetime.fromisoformat(value), self.end)
        if self.latest is None or moment > self.latest:
            self.latest = moment


def _span_sections(g: _Gather, items: list[dict[str, Any]]) -> None:
    minutes_by_app: dict[str, float] = defaultdict(float)
    spans_by_app: dict[str, int] = defaultdict(int)
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        g.saw(item["ended_at"])
        minutes = item["duration_seconds_in_window"] / 60
        minutes_by_app[item["app_name"]] += minutes
        spans_by_app[item["app_name"]] += 1
        group = groups.setdefault(
            (item["app_name"], item["summary"]),
            {
                "app": item["app_name"],
                "title": item["summary"],
                "minutes": 0.0,
                "spans": 0,
                "first": item["occurred_at"],
                "last": item["ended_at"],
                "_ref": item["source_refs"][0],
                "_longest": -1.0,
            },
        )
        group["minutes"] += minutes
        group["spans"] += 1
        group["first"] = min(group["first"], item["occurred_at"])
        group["last"] = max(group["last"], item["ended_at"])
        if minutes > group["_longest"]:
            group["_longest"] = minutes
            group["_ref"] = item["source_refs"][0]
    ranked_apps = sorted(minutes_by_app, key=lambda app: -minutes_by_app[app])
    g.sections["apps"] = [
        {"app": app, "minutes": round(minutes_by_app[app]), "spans": spans_by_app[app]}
        for app in ranked_apps
    ]
    windows = []
    for group in sorted(groups.values(), key=lambda x: (x["first"], x["_ref"])):
        key = f"a{group['_ref'].split(':')[2]}"
        g.refs[key] = group["_ref"]
        row = {
            "key": key,
            "app": group["app"],
            "title": group["title"],
            "minutes": round(group["minutes"], 1),
            "spans": group["spans"],
            "first": g.clock(group["first"]),
            "last": g.clock(group["last"]),
        }
        g.haystack[key] = (
            f"{row['first']}-{row['last']} {row['app']} — {row['title']} ({row['minutes']} min)"
        )
        windows.append(row)
    g.sections["windows"] = windows
    g.served["app"] = (
        t("report.served.app", spans=len(items), windows=len(windows))
        if items
        else t("report.served.app_none")
    )


def _milestone(text: str) -> str | None:
    folded = text.casefold()
    return next((phrase for phrase in MILESTONES if phrase in folded), None)


def _screen_section(g: _Gather, texts: dict[int, str], items: list[dict[str, Any]]) -> None:
    """Every capture of the day, whole, in time order; only a repeat of the same screen is folded.

    Consecutive captures of one window whose text is near-identical are one
    screen state and the longest stands for the run; every capture keeps its
    key. A capture whose text holds a milestone phrase is listed again at the
    top, so a confirmation page seen for half a minute is read.
    """
    kept: list[dict[str, Any]] = []
    last_in_window: dict[tuple[str, str | None], dict[str, Any]] = {}
    folded = 0
    for item in sorted(items, key=lambda x: (x["occurred_at"], _row_id(x))):
        g.saw(item["ended_at"])
        identity = int(_row_id(item))
        key = f"s{identity}"
        g.refs[key] = item["source_refs"][0]
        text = _flat(texts.get(identity, ""))
        row = {
            "key": key,
            "app": item["app_name"],
            "title": item["window_title"] or "",
            "first": g.clock(item["occurred_at"]),
            "last": g.clock(item["ended_at"]),
            "count": 1,
            "text": text,
            "chars": len(text),
        }
        window = (item["app_name"], item["window_title"])
        previous = last_in_window.get(window)
        if (
            previous is not None
            and SequenceMatcher(None, previous["text"], text).quick_ratio() >= _NEAR_DUPLICATE
        ):
            previous["count"] += 1
            previous["last"] = row["last"]
            folded += 1
            if len(text) > len(previous["text"]):
                previous.update(key=key, text=text, chars=len(text))
            continue
        kept.append(row)
        last_in_window[window] = row
    g.sections["screen"] = kept
    g.sections["milestones"] = [
        {**row, "phrase": phrase, "text": _around(row["text"], phrase)}
        for row in kept
        if (phrase := _milestone(row["text"])) is not None
    ]
    g.served["screen"] = (
        t("report.served.screen", total=len(items), folded=folded, kept=len(kept))
        if items
        else t("report.served.screen_none")
    )


def _around(text: str, phrase: str) -> str:
    at = text.casefold().find(phrase)
    return text[max(0, at - _SNIPPET_BEFORE) : at + len(phrase) + _SNIPPET_AFTER]


def _state_section(g: _Gather, state: dict[str, Any]) -> None:
    rows = [
        {"at": g.day_clock(e["at"]), "kind": e["kind"], "phase": "at start"}
        for e in state["at_start"]
    ]
    rows += [{"at": g.clock(e["at"]), "kind": e["kind"], "phase": ""} for e in state["events"]]
    g.sections["state_events"] = rows


def _timesink_sections(g: _Gather, snap: timesink.Reader | None, why: str | None = None) -> None:
    """The day's windows, screen text and state events; ``why`` is a device that was not there."""
    spans = timesink.query_spans(snap, g.start, g.end)
    captures = timesink.query_captures(snap, g.start, g.end)
    state = timesink.query_state(snap, g.start, g.end)
    # Everything is read before anything is built: a terminal that drops mid-read leaves no part.
    texts = (
        {}
        if snap is None
        else timesink.capture_texts(snap, (int(_row_id(item)) for item in captures["items"]))
    )
    # The store's enum says whether the source could be read; ``served`` says what it held.
    g.coverage["app"] = "unavailable" if snap is None else str(spans["coverage"]["status"])
    g.coverage["screen"] = "unavailable" if snap is None else "available"
    if snap is None:
        g.served["app"] = g.served["screen"] = (
            t("timesink.unreadable") if why is None else t("device.unavailable", why=why)
        )
        g.sections["windows"] = g.sections["apps"] = g.sections["screen"] = []
        g.sections["milestones"] = g.sections["state_events"] = []
        return
    _span_sections(g, spans["items"])
    _screen_section(g, texts, captures["items"])
    _state_section(g, state)


def _record_section(g: _Gather, memory_path: Path | None) -> None:
    try:
        with closing(read_connection(memory_path)) as memory:
            rows = memory.execute(
                "SELECT id,ts,source,text FROM records WHERE datetime(ts) >= datetime(?) "
                "AND datetime(ts) < datetime(?) ORDER BY rowid",
                (
                    g.start.astimezone(UTC).isoformat(timespec="seconds"),
                    g.end.astimezone(UTC).isoformat(timespec="seconds"),
                ),
            ).fetchall()
    except (DailyError, sqlite3.Error):
        g.coverage["records"] = "unavailable"
        g.served["records"] = t("report.served.records_unreadable")
        g.sections["records"] = []
        return
    g.coverage["records"] = "available"
    records = []
    for index, (identity, ts, source, text) in enumerate(rows):
        key = f"r{index + 1}"
        ref = f"record:{identity}"
        g.refs[key] = ref
        if source == ALLEN_SOURCE:
            g.stated.add(ref)
        row = {"key": key, "at": g.clock(str(ts)), "who": str(source), "text": _flat(text)}
        g.haystack[key] = f"{row['at']} {row['who']}: {row['text']}"
        records.append(row)
    g.sections["records"] = records
    g.served["records"] = (
        t("report.served.records", count=len(records))
        if records
        else t("report.served.records_none")
    )


def _fold_git_rows(
    g: _Gather, rows: list[Any], repos: Sequence[str]
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """One entry per commit and per repo; what the day never saw is written into the limits."""
    commits: dict[str, dict[str, Any]] = {}
    states: dict[str, dict[str, Any]] = {}
    unwatched: set[str] = set()
    dropped = 0
    for uid, kind, observed, raw in rows:
        payload = json.loads(raw)
        repo = str(payload.get("repo_path") or "")
        if repos and repo not in repos:
            # A repo observed that day but no longer configured is omitted, not invisible.
            unwatched.add(repo)
            continue
        if kind == "project.commit_seen":
            # The observer burst-caps a poll; what it never emitted is missing from this day.
            dropped += int(payload.get("skipped_count") or 0)
            sha = str(payload.get("commit_sha") or uid)
            entry = commits.setdefault(
                sha,
                {
                    "uid": uid,
                    "sha": sha,
                    "subject": _flat(payload.get("subject") or ""),
                    "committed_ms": int(payload.get("committed_at_ms") or observed),
                    "observed_ms": int(observed),
                    "paths": [],
                },
            )
            if repo not in entry["paths"]:
                entry["paths"].append(repo)
        else:
            states[repo] = {
                "uid": uid,
                "repo": repo,
                "branch": str(payload.get("branch") or "?"),
                "dirty": int(payload.get("dirty_file_count") or 0),
                "head": str(payload.get("head_sha") or "")[:7],
                "observed_ms": int(observed),
            }
    if unwatched:
        g.limits.append(
            t(
                "report.limit.unwatched",
                count=len(unwatched),
                repos=t("sep.list").join(sorted(unwatched)),
            )
        )
    if dropped:
        g.limits.append(t("report.limit.dropped", count=dropped))
    return commits, states


def _git(repo: str, args: Sequence[str]) -> str | None:
    """One read-only git command in a watched repository; None when it cannot run."""
    try:
        done = subprocess.run(  # noqa: S603 — fixed argv; `repo` is a configured path.
            ["git", "-C", repo, *args],  # noqa: S607 — git is resolved from PATH on purpose.
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def repositories(repos: Sequence[str]) -> dict[str, list[str]]:
    """The watched paths grouped by repository: a worktree shares its main checkout's git dir.

    Three configured checkouts of one repository must list a commit once,
    under one path, not as three repositories' worth of work.
    """
    groups: dict[str, list[str]] = {}
    for repo in repos:
        common = _git(repo, ("rev-parse", "--path-format=absolute", "--git-common-dir"))
        identity = common.strip() if common else repo
        groups.setdefault(identity, []).append(repo)
    return groups


def _local_commits(
    g: _Gather, repos: Sequence[str], device: DeviceLink | None
) -> dict[str, dict[str, Any]]:
    """Every commit on a local branch of a watched repository, committed inside the day.

    The observer only writes what it saw between polls and never backfills a
    repository it started watching that day, so the local repository is the
    authority on what was committed; the observer contributes when it saw it. On a brain the
    repositories are the terminal's, and it runs the same walk.
    """
    if device is None:
        found, unreadable = local_commits(repos, g.start, g.end)
    else:
        answer = device_reads.ask(
            device, device_reads.GIT_READ, "local_commits",
            since=g.start.isoformat(), until=g.end.isoformat(),
        )
        found, unreadable = answer["found"], answer["unreadable"]
    if unreadable:
        g.limits.append(
            t(
                "report.limit.unreadable_repos",
                count=len(unreadable),
                repos=t("sep.list").join(unreadable),
            )
        )
    g.coverage["git"] = "partial" if unreadable else "available"
    return found


def local_commits(
    repos: Sequence[str], since: datetime, until: datetime
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Commits on any local branch committed in [since, until), by SHA, plus unreadable paths."""
    start, end = int(since.timestamp()), int(until.timestamp())
    found: dict[str, dict[str, Any]] = {}
    unreadable: list[str] = []
    for paths in repositories(repos).values():
        repo = paths[0]
        log = _git(
            repo, ("log", "--branches", _GIT_WALK, f"--format=%H{_FIELD_SEP}%ct{_FIELD_SEP}%s")
        )
        if log is None:
            unreadable.extend(paths)
            continue
        merged = _git(repo, ("log", _MAIN, _GIT_WALK, "--format=%H"))
        on_main = set(merged.split()) if merged is not None else None
        for line in log.splitlines():
            sha, _, rest = line.partition(_FIELD_SEP)
            seconds, _, subject = rest.partition(_FIELD_SEP)
            if not sha or not seconds.isdigit() or not start <= int(seconds) < end:
                continue
            entry = found.setdefault(
                sha,
                {
                    "sha": sha,
                    "subject": _flat(subject),
                    "committed_ms": int(seconds) * 1000,
                    "paths": [],
                    "on_main": None,
                },
            )
            if repo not in entry["paths"]:
                entry["paths"].append(repo)
            if on_main is not None:
                entry["on_main"] = sha in on_main
    return found, unreadable


def _git_sections(
    g: _Gather, conn: sqlite3.Connection, repos: Sequence[str], device: DeviceLink | None = None
) -> None:
    repos, gap = device_reads.watched_repos(device, tuple(repos))
    if gap is not None:
        g.limits.append(t("device.unavailable", why=gap))
    rows = conn.execute(
        "SELECT event_uid,type,ts_epoch_ms,payload_json FROM events "
        "WHERE type IN (?,?) AND ts_epoch_ms>=? AND ts_epoch_ms<? ORDER BY ts_epoch_ms,id",
        (
            *_ACTIVITY_TYPES,
            int(g.start.timestamp() * 1000),
            int(g.end.timestamp() * 1000),
        ),
    ).fetchall()
    observed, states = _fold_git_rows(g, rows, repos)
    commits = {} if gap else _local_commits(g, repos, device)
    for sha, seen in observed.items():
        # The local log is the authority on what and when; the observer adds when it was seen.
        entry = commits.setdefault(sha, {**seen, "on_main": None})
        entry["uid"] = seen["uid"]
        entry["observed_ms"] = seen["observed_ms"]
        if not entry["paths"]:
            entry["paths"] = list(seen["paths"])
    ordered = sorted(commits.values(), key=lambda x: (x["committed_ms"], x["sha"]))
    git = []
    for index, entry in enumerate(ordered):
        key = f"g{index + 1}"
        ref = (
            f"event:{entry['uid']}"
            if "uid" in entry
            else f"git:{entry['paths'][0]}:{entry['sha']}"
        )
        g.refs[key] = ref
        committed = datetime.fromtimestamp(entry["committed_ms"] / 1000, UTC).astimezone(g.zone)
        late = committed.date() != g.day
        if not late:
            # An older commit only seen today is evidence of its own day, not of this one.
            g.commits[ref] = entry["sha"][:7]
        seen_at = (
            datetime.fromtimestamp(entry["observed_ms"] / 1000, UTC).astimezone(g.zone)
            if "observed_ms" in entry
            else None
        )
        g.haystack[key] = f"{entry['sha'][:7]} {entry['subject']}"
        row = {
            "key": key,
            "sha": entry["sha"][:7],
            "subject": entry["subject"],
            "committed": committed.strftime("%m-%d %H:%M"),
            "observed": seen_at.strftime("%H:%M") if seen_at else "not recorded by the observer",
            "paths": ", ".join(entry["paths"]),
            "late": late,
            "main": t(_MAIN_KEYS[entry["on_main"]]),
            "on_main": entry["on_main"],
        }
        g.commit_rows[key] = row
        git.append(row)
    g.sections["git"] = git
    same_day = sum(1 for row in git if not row["late"])
    g.served["git"] = (
        t("report.served.git", same=same_day, late=len(git) - same_day)
        if git
        else t("report.served.git_none")
    )
    repo_states = []
    ranked_states = sorted(states.values(), key=lambda x: x["repo"])
    for index, entry in enumerate(ranked_states):
        key = f"p{index + 1}"
        g.refs[key] = f"event:{entry['uid']}"
        repo_states.append(
            {
                "key": key,
                "repo": entry["repo"],
                "branch": entry["branch"],
                "dirty": entry["dirty"],
                "head": entry["head"],
                "at": datetime.fromtimestamp(entry["observed_ms"] / 1000, UTC)
                .astimezone(g.zone)
                .strftime("%H:%M"),
            }
        )
    g.sections["repo_states"] = repo_states
    if not repos:
        g.coverage["git"] = "unavailable"
        g.served["git"] = (
            t("report.served.git_no_repos")
            if gap is None
            else t("report.served.git_device", why=gap)
        )


_MAIN_KEYS = {True: "report.main.on", False: "report.main.off", None: "report.main.unknown"}
"""Whether a commit is on main, as the language table names it."""


def _knowledge_section(g: _Gather, conn: sqlite3.Connection) -> None:
    items = current_items(conn, "knowledge", high_water(conn))
    rows = []
    for index, item in enumerate(x for x in items if x["status"] == "active"):
        key = f"k{index + 1}"
        g.refs[key] = item["source_ref"]
        rows.append({"key": key, "statement": _flat(item["statement"]), "kind": item["kind"]})
    g.sections["knowledge"] = rows
    g.coverage["knowledge"] = "available"


def _plan_section(g: _Gather, plan: Mapping[str, Any] | None) -> None:
    """ADR 0036: Microsoft calendar (this day and the next) and To Do, context and not activity.

    ``plan`` is the runtime's read: ``events`` (aware ISO ``start``/``end``, or a
    ``date`` when ``all_day``) and ``todos`` (``due``/``completed`` are dates);
    None when no reader is wired, ``{"error": ...}`` when the read failed.
    """
    g.sections["calendar"], g.sections["todos"] = [], []
    if plan is None or plan.get("error"):
        g.coverage["calendar"] = g.coverage["todos"] = "unavailable"
        why = (
            t("report.plan.not_connected")
            if plan is None
            else t("report.plan.read_failed", error=plan["error"])
        )
        g.served["plan"] = t("report.served.plan_missing", why=why)
        return
    events = []
    for event in plan["events"]:
        row = {"subject": event["subject"], "location": event["location"]}
        if event["all_day"]:
            row.update(date=event["date"], time=t("report.all_day"))
        else:
            start = datetime.fromisoformat(event["start"]).astimezone(g.zone)
            end = datetime.fromisoformat(event["end"]).astimezone(g.zone)
            row.update(date=start.date().isoformat(), time=f"{start:%H:%M}-{end:%H:%M}")
        events.append(row)
    all_day = t("report.all_day")
    events.sort(key=lambda r: (r["date"], r["time"] != all_day, r["time"]))
    day = g.day.isoformat()
    todos = [t for t in plan["todos"] if not t["done"] or t["completed"] == day]
    todos.sort(key=lambda t: (t["done"], t["due"] is None, t["due"] or "", not t["important"]))
    g.sections["calendar"], g.sections["todos"] = events, todos
    g.coverage["calendar"] = g.coverage["todos"] = "available"
    done = sum(t["done"] for t in todos)
    g.served["plan"] = t(
        "report.served.plan", events=len(events), day=day, open=len(todos) - done, done=done
    )


def _previous_section(g: _Gather, conn: sqlite3.Connection, zone_name: str) -> None:
    """The previous day's served summary (its summary section), as context that is not today."""
    prior = (g.day - timedelta(days=1)).isoformat()
    item = existing_report(conn, prior, zone_name)
    if item is None:
        g.sections["previous"] = []
        return
    g.refs["b1"] = item["source_ref"]
    content = str(item["content"])
    summary = summary_section(content)
    summary = content if summary is None else summary
    g.sections["previous"] = [{"key": "b1", "date": prior, "text": _flat(summary)}]


def _message_text(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    return " ".join(str(x.get("text") or "") for x in content if isinstance(x, dict)).strip()


def codex_session(
    path: Path, start: datetime | None = None, end: datetime | None = None
) -> dict[str, Any]:
    """One Codex session file: who ran it where, and its user/assistant turns inside [start, end).

    Codex writes ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``, one JSON
    object per line; only ``session_meta`` and the user/assistant messages are
    read. Whatever the agent says about its own work is its self-report.
    """
    meta: dict[str, Any] = {}
    asks: list[str] = []
    answers: list[str] = []
    turns: list[tuple[str, str, str]] = []
    first = last = None
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            payload = row.get("payload") if isinstance(row, dict) else None
            if not isinstance(payload, dict):
                continue
            if row.get("type") == "session_meta":
                meta = payload
                continue
            if row.get("type") != "response_item" or payload.get("type") != "message":
                continue
            try:
                moment = datetime.fromisoformat(str(row.get("timestamp")))
            except ValueError:
                continue
            if (start and moment < start) or (end and moment >= end):
                continue
            text = _message_text(payload)
            role = str(payload.get("role"))
            if role == "user":
                asks.append(text)
            elif role == "assistant":
                answers.append(text)
            else:
                continue
            turns.append((moment.isoformat(timespec="seconds"), role, text))
            first = moment if first is None else first
            last = moment
    return {
        "id": str(meta.get("id") or path.stem),
        "cwd": str(meta.get("cwd") or ""),
        "originator": str(meta.get("originator") or "codex"),
        "asks": asks,
        "answers": answers,
        "turns": turns,
        "first": first,
        "last": last,
    }


def _first_prompt(asks: list[str]) -> str:
    """The first user text that is not an injected preamble (instructions, pasted files)."""
    return next((a for a in asks if a and not a.startswith(("<", "#"))), asks[0] if asks else "")


def _session_files(root: Path, day: date, start: datetime) -> list[Path]:
    """Session files that may hold a turn of the day: begun by then, still written since.

    Codex files a session under the local date it began and appends to it as
    long as it is used, so a session begun last week and continued today
    lives in last week's directory; its mtime is what says it was touched.
    """
    since = start.timestamp()
    found = []
    for path in root.glob("*/*/*/*.jsonl"):
        try:
            begun = date(*(int(part) for part in path.parts[-4:-1]))
            touched = path.stat().st_mtime
        except (ValueError, OSError):
            continue
        if begun <= day and touched >= since:
            found.append(path)
    return sorted(found)


def _agent_section(g: _Gather, sessions_root: Path | None) -> None:
    """Every Codex session with a turn in the day, every turn whole; approval shims folded."""
    if sessions_root is None or not sessions_root.is_dir():
        g.coverage["agent"] = "unavailable"
        g.sections["agent"] = []
        g.served["agent"] = t("report.served.agent_unreadable")
        return
    sessions = []
    for path in _session_files(sessions_root, g.day, g.start):
        try:
            session = codex_session(path, g.start, g.end)
        except OSError:
            continue
        if session["answers"]:
            session["path"] = path
            sessions.append(session)
    sessions.sort(key=lambda x: (x["first"], str(x["path"])))
    g.coverage["agent"] = "available"
    home = str(Path.home())
    rows = []
    shims = 0
    for index, session in enumerate(sessions):
        key = f"c{index + 1}"
        g.refs[key] = f"codex-session:{session['path']}"
        g.haystack[key] = _flat(" ".join([*session["asks"], *session["answers"]]))
        g.saw(session["last"].isoformat())
        if _first_prompt(session["asks"]).startswith(_SHIM_PREFIX):
            shims += 1
            continue
        rows.append(
            {
                "key": key,
                "first": session["first"].astimezone(g.zone).strftime("%H:%M"),
                "last": session["last"].astimezone(g.zone).strftime("%H:%M"),
                "app": session["originator"],
                "cwd": session["cwd"].replace(home, "~", 1),
                "turns": [
                    {
                        "at": datetime.fromisoformat(when).astimezone(g.zone).strftime("%H:%M"),
                        "role": role,
                        "text": _flat(text),
                    }
                    for when, role, text in session["turns"]
                ],
            }
        )
    g.sections["agent"] = rows
    if sessions:
        g.served["agent"] = t(
            "report.served.agent",
            sessions=len(sessions),
            rows=len(rows),
            shims=t("report.served.agent_shims", count=shims) if shims else "",
        )
    else:
        g.served["agent"] = t("report.served.agent_none")


def _index_rows(rows: Iterable[dict[str, Any]]) -> tuple[int, int]:
    """Cut each row's text to one index line; how many were cut and how much text that withheld."""
    withheld = count = 0
    for row in rows:
        if len(row["text"]) <= _INDEX_TEXT:
            continue
        withheld += len(row["text"]) - _INDEX_TEXT
        count += 1
        row["text"] = row["text"][:_INDEX_TEXT]
    return count, withheld


def _fallback_rows(g: _Gather, source: str) -> tuple[list[dict[str, Any]], str]:
    """The rows one fallback step cuts, and what the header calls them."""
    if source == "screen":
        return g.sections.get("screen", []), t("report.fallback.screen")
    if source == "records":
        return g.sections.get("records", []), t("report.fallback.records")
    role = "assistant" if source == "agent_answers" else "user"
    rows: list[dict[str, Any]] = []
    for session in g.sections.get("agent", []):
        turns = session["turns"]
        last_answer = max((i for i, t in enumerate(turns) if t["role"] == "assistant"), default=-1)
        rows += [t for i, t in enumerate(turns) if t["role"] == role and i != last_answer]
    return rows, t(f"report.fallback.agent_{'answers' if role == 'assistant' else 'prompts'}")


def _fit_budget(g: _Gather) -> None:
    """Whole text while the day fits the model; past the budget, one source at a time falls back.

    Order: screen text, then Codex assistant turns other than each session's
    last, then Codex prompts, then records. A fallen-back row keeps its key,
    its time and title and ``_INDEX_TEXT`` characters; the header says what
    was withheld and how much, and the originals stay searchable and readable.
    """
    for source in _FALLBACK_ORDER:
        if len(json.dumps(g.sections, ensure_ascii=False)) <= MATERIAL_BUDGET:
            return
        rows, what = _fallback_rows(g, source)
        count, withheld = _index_rows(rows)
        if count:
            g.limits.append(
                t(
                    "report.limit.budget",
                    budget=MATERIAL_BUDGET,
                    what=what,
                    count=count,
                    chars=_INDEX_TEXT,
                    withheld=withheld,
                )
            )


def gather_day(  # noqa: PLR0913 — the configured stores plus the day, its zone and the clock.
    conn: sqlite3.Connection,
    *,
    memory_path: Path | None,
    timesink_path: Path | None,
    repos: Sequence[str],
    day: date,
    zone_name: str,
    zone: tzinfo,
    now: datetime,
    codex_sessions_path: Path | None = None,
    plan: Mapping[str, Any] | None = None,
    device: DeviceLink | None = None,
) -> DayEvidence:
    """Read every configured source once for the whole local day, whole and keyed.

    On a brain (``device``) TimeSink and the repositories are the terminal's; one that is not
    there leaves their coverage ``unavailable`` and says why, as an unreadable store does.
    """
    start, end, partial = day_window(day, zone, now)
    g = _Gather(zone=zone, start=start, end=end, day=day)
    try:
        with timesink.snapshot(timesink_path, device) as snap:
            _timesink_sections(g, snap)
    except device_reads.DeviceUnavailable as exc:
        _timesink_sections(g, None, str(exc))
    _record_section(g, memory_path)
    _git_sections(g, conn, repos, device)
    _agent_section(g, codex_sessions_path)
    _plan_section(g, plan)
    _knowledge_section(g, conn)
    _previous_section(g, conn, zone_name)
    _fit_budget(g)
    return DayEvidence(
        day=day.isoformat(),
        zone=zone_name,
        window={
            "from": start.isoformat(timespec="seconds"),
            "to": end.isoformat(timespec="seconds"),
            "partial": partial,
        },
        coverage=g.coverage,
        counts={key: len(rows) for key, rows in g.sections.items()},
        limits=g.limits,
        observed_until=(
            g.latest.astimezone(zone).isoformat(timespec="seconds") if g.latest else None
        ),
        sections=g.sections,
        refs=g.refs,
        stated=frozenset(g.stated),
        commits=dict(g.commits),
        haystack=g.haystack,
        commit_rows=g.commit_rows,
        served=g.served,
    )


def _snippet(text: str, terms: Sequence[str]) -> str:
    """The first term's match with a little of what surrounds it, on one line."""
    flat = _flat(text)
    needle = terms[0]
    at = flat.casefold().find(needle)
    if at < 0:
        return flat[: _SNIPPET_BEFORE + _SNIPPET_AFTER]
    stop = at + len(needle) + _SNIPPET_AFTER
    head = "…" if at > _SNIPPET_BEFORE else ""
    tail = "…" if stop < len(flat) else ""
    return f"{head}{flat[max(0, at - _SNIPPET_BEFORE) : stop]}{tail}"


def _capture_hits(
    evidence: DayEvidence,
    terms: Sequence[str],
    *,
    timesink_path: Path | None,
    zone: tzinfo,
    device: DeviceLink | None = None,
) -> list[tuple[str, str]]:
    """Captures of the day whose full OCR text contains every term, oldest first."""
    ids = [int(k[1:]) for k in evidence.refs if k[:1] == "s" and k[1:].isdigit()]
    if not ids:
        return []
    hits = []
    with timesink.snapshot(timesink_path, device) as snap:
        if snap is None:
            return []
        rows = timesink.capture_matches(snap, ids, list(terms))
    for row in rows:
        when = datetime.fromisoformat(timesink.moment(row["at"])).astimezone(zone)
        line = (
            f"{when:%H:%M} {row['appName']} — {row['title'] or ''}: "
            f"{_snippet(str(row['text']), terms)}"
        )
        hits.append((f"s{row['id']}", line))
    return hits


def search_day(
    evidence: DayEvidence,
    query: str,
    *,
    timesink_path: Path | None,
    zone: tzinfo,
    device: DeviceLink | None = None,
) -> str:
    """Keys whose text contains every word of the query, listed or not, with context.

    Words match anywhere and in any order, case-insensitively: a model that
    asks for "RBC 申请 submitted" is looking for a page holding all three, not
    for that exact phrase.
    """
    terms = [word.casefold() for word in query.split()]
    if not terms:
        return "Give the keywords to search for."
    gap = ""
    try:
        hits = _capture_hits(evidence, terms, timesink_path=timesink_path, zone=zone, device=device)
    except device_reads.DeviceUnavailable as exc:
        hits, gap = [], f"\n{exc} The screen text was not searched."
    hits += [
        (key, _snippet(text, terms))
        for key, text in evidence.haystack.items()
        if all(term in text.casefold() for term in terms)
    ]
    if not hits:
        return (
            f"\"{query}\" does not appear in the day's searchable material (searched: all screen"
            " text, window titles, full conversation records, commit subjects and full Codex"
            " sessions)." + gap
        )
    shown = hits[:MAX_HITS]
    rest = len(hits) - len(shown)
    more = f"\n…{rest} more hits not listed; use more specific keywords." if rest else ""
    listed = "\n".join(f"[{k}] {line}" for k, line in shown)
    return f'"{query}": {len(hits)} hits:\n{listed}{more}{gap}'


def git_show(repo: str, sha: str, device: DeviceLink | None = None) -> str | None:
    """The commit's header, message and changed files; None when the repository cannot show it.

    With ``device`` (a brain) the terminal shows it from its own repository.
    """
    if device is not None:
        shown: str | None = device_reads.ask(
            device, device_reads.GIT_READ, "show", repo=repo, sha=sha
        )
        return shown
    return _git(repo, ("show", "--stat", "--format=%H%n%an %ci%n%n%B", sha))


def _commit_detail(key: str, evidence: DayEvidence, device: DeviceLink | None) -> str | None:
    """The commit itself, from the repository it was found in, for a commit key."""
    commit = evidence.commit_rows.get(key)
    if commit is None:
        return None
    shown = git_show(commit["paths"].split(", ")[0], commit["sha"], device)
    return None if shown is None else f"[{key}] {commit['main']}, late={commit['late']}\n{shown}"


def _session_detail(key: str, path: Path, start: datetime, end: datetime) -> str:
    """A Codex session's every turn inside the window, whole, marked as its own account."""
    session = codex_session(path, start, end)
    turns = "\n".join(f"{when} {role}: {_flat(text)}" for when, role, text in session["turns"])
    return (
        f"[{key}] Codex session {session['id']} ({session['originator']}, {session['cwd']})"
        f" — the agent's own account, not a verified result\n{turns}"
    )


def _stored_detail(
    key: str, ref: str, conn: sqlite3.Connection, memory_path: Path | None
) -> str | None:
    """A conversation record or an event-log row behind a key; None for other kinds."""
    identity = ref.partition(":")[2]
    if ref.startswith("record:"):
        with closing(read_connection(memory_path)) as memory:
            row = memory.execute(
                "SELECT ts,source,text FROM records WHERE id=?", (identity,)
            ).fetchone()
        if row is None:
            return f"[{key}] the record no longer exists"
        return f"[{key}] {row[0]} {row[1]}: {_flat(row[2])}"
    if ref.startswith("event:"):
        row = conn.execute(
            "SELECT type,payload_json FROM events WHERE event_uid=?", (identity,)
        ).fetchone()
        if row is None:
            return f"[{key}] the event no longer exists"
        return f"[{key}] {row[0]}: {row[1]}"
    return None


def read_detail(  # noqa: PLR0911, PLR0913 — one return per reference kind; the stores.
    key: str,
    evidence: DayEvidence,
    *,
    conn: sqlite3.Connection,
    memory_path: Path | None,
    timesink_path: Path | None,
    device: DeviceLink | None = None,
) -> str:
    """The whole original behind one material key; an unreadable source says so."""
    ref = evidence.refs.get(key)
    if ref is None:
        return f"[{key}] is not a key in the material"
    try:
        shown = _commit_detail(key, evidence, device)
        if shown is not None:
            return shown
        if ref.startswith("timesink-capture:"):
            row = timesink.read_capture(timesink_path, ref, device)
            when = f"{timesink.moment(row['at'])}..{row['endedAt']}"
            head = f"{row['appName']} — {row['title'] or ''} {when} (UTC)"
            return f"[{key}] {head}\n{_flat(row['text'] or '')}"
        if ref.startswith("timesink:"):
            original = timesink.read_span(timesink_path, ref, device)
            # The stored row keeps GRDB's bare UTC text; unlabelled it reads as a local clock.
            return f"[{key}] (raw row, times in UTC) {json.dumps(original, ensure_ascii=False)}"
        if ref.startswith("codex-session:"):
            start, end = (datetime.fromisoformat(evidence.window[k]) for k in ("from", "to"))
            return _session_detail(key, Path(ref.partition(":")[2]), start, end)
        shown = _stored_detail(key, ref, conn, memory_path)
    except (DailyError, sqlite3.Error, OSError) as exc:
        return f"[{key}] this entry cannot be read right now: {exc}"
    return shown if shown is not None else f"[{key}] has no readable original"


_QUESTION = re.compile(
    r"[?？]\s*$|(吗|呢|么)[?？。！]?\s*$|^(请|帮我|麻烦|能不能|可不可以|要不要)"
    r"|^(?i:please|can you|could you|would you|will you|help me)\b"
)


def summary_section(content: str) -> str | None:
    """The summary section of a saved report, whichever language it was saved in."""
    for heading in TEXT["report.h.summary"].values():
        _, found, rest = content.partition(heading)
        if found:
            return rest.split("\n## ", 1)[0]
    return None


def is_question(text: str) -> bool:
    """A record that asks or requests rather than states: not a confirmation of anything."""
    return _QUESTION.search(_flat(text)) is not None


def _session_original(ref: str, evidence: DayEvidence, terms: Sequence[str]) -> str:
    """A session's first turn, every turn naming one of ``terms``, and its last reply, whole."""
    start, end = (datetime.fromisoformat(evidence.window[k]) for k in ("from", "to"))
    session = codex_session(Path(ref.partition(":")[2]), start, end)
    turns = session["turns"]
    chosen = [t for t in turns if any(term in t[2].casefold() for term in terms)]
    if turns and turns[0] not in chosen:
        chosen.insert(0, turns[0])
    if session["answers"]:
        last = next(t for t in reversed(turns) if t[1] == "assistant")
        if last not in chosen:
            chosen.append(last)
    return "\n".join(f"{when} {role}: {_flat(body)}" for when, role, body in chosen)


def claim_originals(  # noqa: PLR0913 — the stores and the words that pick a session's turns.
    keys: Sequence[str],
    evidence: DayEvidence,
    *,
    terms: Sequence[str],
    conn: sqlite3.Connection,
    memory_path: Path | None,
    timesink_path: Path | None,
    device: DeviceLink | None = None,
) -> list[dict[str, Any]]:
    """The whole originals behind a claim's refs, typed, for the verification call.

    A commit is its header, message and changed files with its main and late
    flags; a record is its author and text; a capture is its window and OCR
    text; a session is its first prompt, its last reply and every turn that
    mentions one of ``terms`` (the item's title words), whole.
    """
    originals: list[dict[str, Any]] = []
    lowered = [t.casefold() for t in terms if t.strip()]
    for key in keys:
        ref = evidence.refs.get(key)
        if ref is None:
            continue
        kind, text = "other", ""
        try:
            if key in evidence.commit_rows:
                commit = evidence.commit_rows[key]
                shown = (
                    git_show(commit["paths"].split(", ")[0], commit["sha"], device)
                    or commit["subject"]
                )
                kind = "late_commit" if commit["late"] else "commit"
                text = f"{commit['main']}\n{shown}"
            elif ref.startswith("record:"):
                shown = _stored_detail(key, ref, conn, memory_path) or ""
                kind = "user_statement" if ref in evidence.stated else "jarvis_record"
                text = shown.partition("] ")[2]
            elif ref.startswith("timesink-capture:"):
                row = timesink.read_capture(timesink_path, ref, device)
                kind = "screen"
                text = f"{row['appName']} — {row['title'] or ''}\n{_flat(row['text'] or '')}"
            elif ref.startswith("codex-session:"):
                kind = "agent_session"
                text = _session_original(ref, evidence, lowered)
            elif ref.startswith("timesink:"):
                kind = "window"
                text = evidence.haystack.get(key, "")
            else:
                text = _stored_detail(key, ref, conn, memory_path) or evidence.haystack.get(key, "")
        except (DailyError, sqlite3.Error, OSError) as exc:
            kind, text = "unreadable", str(exc)
        originals.append({"key": key, "kind": kind, "text": text})
    return originals


def save_report(  # noqa: PLR0913 — the two source stores, the identity, the payload and the writer.
    conn: sqlite3.Connection,
    *,
    memory_path: Path | None,
    timesink_path: Path | None,
    day: str,
    zone: str,
    content: str,
    source_refs: Sequence[str],
    coverage: dict[str, str],
    expected_version: int,
    action_id: str,
    device: DeviceLink | None = None,
) -> dict[str, Any]:
    """Append the next briefing version through the shared revision store.

    The request id is derived from the content, so an exact retry returns the
    original receipt while a stale ``expected_version`` is a version conflict,
    never an overwrite.
    """
    args: dict[str, Any] = {
        "local_date": day,
        "timezone": zone,
        "content": content,
        "source_refs": list(source_refs),
        "coverage": dict(coverage),
        "request_id": (
            f"daily-work-report:{day}:{zone}:{expected_version + 1}:"
            f"{fingerprint([content, list(source_refs)])[:16]}"
        ),
    }
    if expected_version:
        args["expected_version"] = expected_version
    return daily_store.write_revision(
        conn, memory_path, "save_briefing", args, action_id, timesink_path=timesink_path,
        device=device,
    )
