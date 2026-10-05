"""L2 moment facts (ADR 0161): what Allen is doing right now, read from TimeSink when asked.

One read-only transaction answers: the front app, its site and since when; present, idle, locked
or asleep; in a call or not; and today's time by activity. Every answer may be ``unknown``: a
store that cannot be read, or whose newest span is older than ``FRESH_S`` with no recorded
reason (idle, lock, sleep), makes everything unknown. Window titles and URLs are read here only to
spot a call and a job site; the result carries app names and domains, never a title or a URL.
Nothing is copied or stored.

Layer rules: stdlib + L2 siblings; no wiring.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit

from jarvis.state import job_time, timesink

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

UNKNOWN: Final[str] = "unknown"
# The newest span is "now" only if it ended this recently; older with no reason is unknown.
FRESH_S: Final[float] = 120.0
# A call window seen this recently (or, for an away state, just before it began) is a live call.
CALL_GRACE_S: Final[float] = 90.0
# An idle/lock/sleep mark older than this is not trusted (TimeSink may have died without a wake).
AWAY_TRUST_S: Final[float] = 8 * 3600.0
# TimeSink cuts a span at the instant it logs idle or lock; a span ending later contradicts it.
EVENT_SLACK_S: Final[float] = 2.0
# Spans of one app and site closer than this are one stretch for "since when".
SINCE_GAP_S: Final[float] = 10.0
RECENT_SPANS: Final[int] = 400
TOP_APPS: Final[int] = 3
MIN_APP_S: Final[float] = 60.0

_AWAY: Final[dict[str, str]] = {"idle": "idle", "lock": "locked", "sleep": "asleep"}
# Priority when several are open at once: a sleeping Mac is also locked, and so on.
_AWAY_ORDER: Final[tuple[str, ...]] = ("sleep", "lock", "idle")
_RESUMES: Final[dict[str, str]] = {"idle": "active", "lock": "unlock", "sleep": "wake"}
_TRACKING: Final[tuple[str, ...]] = ("start", "stop", "tracking_pause", "tracking_resume")

_ZOOM_LOBBY: Final[frozenset[str]] = frozenset(
    {"", "zoom", "zoom workplace", "settings", "login", "sign in"}
)
_MEET_CODE: Final[re.Pattern[str]] = re.compile(r"[a-z]{3}-[a-z]{4}-[a-z]{3}")
_TEAMS_HOSTS: Final[tuple[str, ...]] = (
    "teams.microsoft.com",
    "teams.cloud.microsoft",
    "teams.live.com",
)


def call_app(  # noqa: PLR0911 - one flat table of call windows
    bundle: str, title: str | None, domain: str | None, url: str | None
) -> str | None:
    """The call app this front window belongs to, or None.

    Patterns found in TimeSink's real rows (Zoom, Teams, Tencent Meeting) or the apps' known
    shapes (Meet, FaceTime, Webex); a lobby or chat window is not a call. Discord voice and screen
    sharing leave no trace in a window title, so they are not detected.
    """
    seen = (title or "").strip().casefold()
    host = (domain or "").lower()
    path = urlsplit(url or "").path.lower()
    if bundle == "us.zoom.xos":
        return None if seen in _ZOOM_LOBBY else "Zoom"
    if host.endswith("zoom.us") and path.startswith("/wc/"):
        return "Zoom"
    if host == "meet.google.com" and (
        _MEET_CODE.fullmatch(path.lstrip("/")) or seen.startswith("meet - ")
    ):
        return "Google Meet"
    if (bundle.startswith("com.microsoft.teams") or host in _TEAMS_HOSTS) and re.match(
        r"(meeting|call)\b", seen
    ):
        return "Microsoft Teams"
    if bundle == "com.apple.FaceTime" and seen not in ("", "facetime"):
        return "FaceTime"
    if bundle == "com.webex.meetingmanager" or (
        bundle == "Cisco-Systems.Spark" and "meeting" in seen
    ):
        return "Webex"
    if bundle == "com.tencent.meeting" and title is not None and seen != "腾讯会议":
        return "腾讯会议"
    return None


def _unknown(read: str, age: float | None) -> dict[str, Any]:
    return {
        "read": read,
        "data_age_s": age,
        "front_app": UNKNOWN,
        "site_domain": UNKNOWN,
        "since": UNKNOWN,
        "presence": UNKNOWN,
        "in_call": UNKNOWN,
        "call_app": None,
        "screen_share": UNKNOWN,
        "today": UNKNOWN,
    }


def _latest(conn: sqlite3.Connection, kinds: Iterable[str]) -> tuple[datetime, str] | None:
    names = tuple(kinds)
    row = conn.execute(
        f"SELECT at,kind FROM stateEvent WHERE kind IN ({','.join('?' * len(names))}) "  # noqa: S608 - placeholders only
        "ORDER BY at DESC,id DESC LIMIT 1",
        names,
    ).fetchone()
    return None if row is None else (timesink._moment(row["at"]), row["kind"])  # noqa: SLF001


def _away(conn: sqlite3.Connection, now: datetime, newest_end: datetime | None) -> str | None:
    """Idle, locked or asleep when TimeSink's own marks say so and nothing contradicts them."""
    tracking = _latest(conn, _TRACKING)
    for kind in _AWAY_ORDER:
        mark = _latest(conn, (kind, _RESUMES[kind]))
        if mark is None or mark[1] != kind:
            continue
        at = mark[0]
        if (now - at).total_seconds() > AWAY_TRUST_S:
            continue
        if newest_end is not None and (newest_end - at).total_seconds() > EVENT_SLACK_S:
            continue  # a span ended after the mark: he was active again
        if tracking is not None and tracking[0] > at:
            continue  # TimeSink stopped or restarted since: the mark may be stale
        return _AWAY[kind]
    return None


def _front(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], datetime]:
    """The newest span and when its uninterrupted stretch (same app and site) began."""
    head = rows[0]
    since, key = head["start"], (head["appBundleID"], head["domain"])
    for older in rows[1:]:
        if (older["appBundleID"], older["domain"]) != key:
            break
        if (since - older["end"]).total_seconds() > SINCE_GAP_S:
            break
        since = min(since, older["start"])
    return head, since


def _today(
    snap: timesink.Snapshot, now: datetime, known: Iterable[job_time.Company]
) -> dict[str, Any]:
    """Job-site seconds and the top apps by seconds since local midnight; names only."""
    start = job_time.local_midnight(now)
    books = list(known)
    apps: Counter[str] = Counter()
    job = 0.0
    for row in job_time._spans(snap, start, now):  # noqa: SLF001
        seconds = (min(row["end"], now) - max(row["start"], start)).total_seconds()
        apps[row["appName"]] += seconds
        if job_time.job_site(row["domain"], row["url"], row["title"], books)[0]:
            job += seconds
    top = [[name, round(s)] for name, s in apps.most_common() if s >= MIN_APP_S][:TOP_APPS]
    return {"job_s": round(job), "apps": top}


def moment_facts(
    path: Path | None, now: datetime, known: Iterable[job_time.Company] = ()
) -> dict[str, Any]:
    """The facts of this instant, each possibly ``unknown``; read-only, never a title or URL.

    ``known`` are the ledger companies, so career pages count as job-site time today.
    """
    with timesink.snapshot(path) as snap:
        if snap is None:
            return _unknown("unreadable", None)
        try:
            return _facts(snap, now, known)
        except (sqlite3.Error, ValueError, TypeError, OverflowError):
            return _unknown("unreadable", None)


def _facts(
    snap: timesink.Snapshot, now: datetime, known: Iterable[job_time.Company]
) -> dict[str, Any]:
    conn = snap.conn
    rows = [
        {**dict(r), "start": timesink._moment(r["start"]), "end": timesink._moment(r["end"])}  # noqa: SLF001
        for r in conn.execute(
            "SELECT start,end,appBundleID,appName,title,url,domain FROM span "
            "WHERE deviceID IS NULL AND end>start ORDER BY end DESC,id DESC LIMIT ?",
            (RECENT_SPANS,),
        )
    ]
    if not rows:
        return _unknown("stale", None)
    newest_end = rows[0]["end"]
    age = (now - newest_end).total_seconds()
    away = _away(conn, now, newest_end)
    if away is None and age > FRESH_S:
        return _unknown("stale", age)
    head, since = _front(rows)
    # While active the live window is now; away, it is the moment the activity stopped.
    reference = now if away is None else head["end"]
    cutoff = reference - timedelta(seconds=CALL_GRACE_S)
    calls = [
        name
        for r in rows
        if r["end"] >= cutoff
        and (name := call_app(r["appBundleID"], r["title"], r["domain"], r["url"])) is not None
    ]
    return {
        "read": "ok",
        "data_age_s": round(age),
        "front_app": head["appName"],
        "site_domain": head["domain"],
        "since": since.isoformat(timespec="seconds"),
        "presence": away or "active",
        "in_call": "yes" if calls else "no",
        "call_app": calls[0] if calls else None,
        "screen_share": UNKNOWN,
        "today": _today(snap, now, known),
    }
