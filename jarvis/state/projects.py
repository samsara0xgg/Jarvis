"""Projects (ADR 0037): the kept catalog, the week's activities and their saved answers.

L2 only. An activity is one TimeSink (app bundle, domain, label); the answers
that sort activities into projects are ``project.activity_classified`` events,
valid only under the catalog fingerprint they were made for. The per-project
view is derived here at read time and never stored. The model request lives in
``jarvis.decision.projects``; the job that joins them in ``jarvis.runtime``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis.state import timesink
from jarvis.state.daily_contract import fingerprint
from jarvis.state.daily_report import local_commits
from jarvis.state.event_log import emit_event

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable, Mapping, Sequence

EVENT_TYPE = "project.activity_classified"
NONE = "none"
WINDOW_DAYS = 7
RECENT_MIN_SECONDS = 60
_MAX_RECENT = 8
_MAX_COMMITS = 8
_LABEL_CHARS = 200
_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")


@dataclass(frozen=True)
class Project:
    """One configured project; ``id`` is its only identity."""

    id: str
    name: str
    repos: tuple[str, ...] = ()
    hints: str = ""


def parse_catalog(raw: object) -> tuple[Project, ...]:
    """Config ``projects``: ``[{id, name, repos?, hints?}]``; malformed input fails at boot."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        msg = "projects must be a list of {id, name, repos?, hints?}"
        raise ValueError(msg)  # noqa: TRY004 — a config error, reported like the others at boot.
    catalog: list[Project] = []
    for entry in raw:
        pid = entry.get("id") if isinstance(entry, dict) else None
        name = entry.get("name") if isinstance(entry, dict) else None
        repos = (entry.get("repos") or []) if isinstance(entry, dict) else None
        hints = (entry.get("hints") or "") if isinstance(entry, dict) else None
        if (
            not isinstance(pid, str)
            or not _ID.fullmatch(pid)
            or pid == NONE
            or any(p.id == pid for p in catalog)
            or not isinstance(name, str)
            or not name.strip()
            or not isinstance(repos, list)
            or not all(isinstance(r, str) and r.strip() for r in repos)
            or not isinstance(hints, str)
        ):
            msg = f"projects entry is malformed or repeats an id: {entry!r}"
            raise ValueError(msg)
        catalog.append(
            Project(
                id=pid,
                name=name.strip(),
                repos=tuple(str(Path(r.strip()).expanduser()) for r in repos),
                hints=" ".join(hints.split()),
            )
        )
    return tuple(catalog)


def catalog_fingerprint(projects: Sequence[Project]) -> str:
    """What an answer depends on: ids, names and hints (repos only feed commits), in any order."""
    return fingerprint(sorted([p.id, p.name, p.hints] for p in projects))[:16]


@dataclass
class Activity:
    """One (app, domain, label) summed over the window."""

    key: str
    app: str
    domain: str | None
    label: str
    seconds: float = 0.0
    days: dict[str, float] = field(default_factory=dict)
    last_seen: datetime | None = None


@dataclass
class Window:
    """The last ``WINDOW_DAYS`` local days and every activity inside them."""

    days: list[date]
    start: datetime
    end: datetime
    activities: dict[str, Activity]


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _label(row: Mapping[str, Any]) -> str:
    for value in (row["document"], row["title"], row["domain"], row["appName"]):
        text = _clean(value)
        if text:
            return text[:_LABEL_CHARS]
    return ""


def empty_window(zone: tzinfo, now: datetime) -> Window:
    """The last ``WINDOW_DAYS`` local days, today included, with no activity yet."""
    today = now.astimezone(zone).date()
    days = [today - timedelta(days=n) for n in range(WINDOW_DAYS - 1, -1, -1)]
    start = datetime.combine(days[0], time(), tzinfo=zone)
    end = datetime.combine(today + timedelta(days=1), time(), tzinfo=zone)
    return Window(days, start, end, {})


def gather(snap: timesink.Snapshot, zone: tzinfo, now: datetime) -> Window:
    """Sum each span per activity and per local day (span overlap, split at midnight)."""
    window = empty_window(zone, now)
    start, end, found = window.start, window.end, window.activities
    for row in timesink.span_rows(snap, start, end):
        began, ended = max(row["start"], start), min(row["end"], end)
        if ended <= began:
            continue
        domain = _clean(row["domain"]) or None
        label = _label(row)
        key = fingerprint([row["appBundleID"], domain or "", label])[:16]
        activity = found.get(key)
        if activity is None:
            activity = found[key] = Activity(key, _clean(row["appName"]), domain, label)
        cursor = began
        while cursor < ended:
            day = cursor.astimezone(zone).date()
            piece = min(ended, datetime.combine(day + timedelta(days=1), time(), tzinfo=zone))
            iso = day.isoformat()
            activity.days[iso] = activity.days.get(iso, 0.0) + (piece - cursor).total_seconds()
            cursor = piece
        activity.seconds += (ended - began).total_seconds()
        if activity.last_seen is None or ended > activity.last_seen:
            activity.last_seen = ended
    return window


def answers(conn: sqlite3.Connection, catalog: str) -> tuple[dict[str, str | None], int | None]:
    """The latest answer per activity key under ``catalog``, and when the newest was saved (ms)."""
    # ponytail: folds every answer on each read; keep a max-id cache here if history makes it slow.
    found: dict[str, str | None] = {}
    newest = None
    for payload_json, ts in conn.execute(
        "SELECT payload_json, ts_epoch_ms FROM events "
        "WHERE type=? AND json_extract(payload_json,'$.catalog')=? ORDER BY id",
        (EVENT_TYPE, catalog),
    ):
        for item in json.loads(payload_json)["assignments"]:
            found[item["key"]] = item["project"]
        newest = ts
    return found, newest


def unsorted(window: Window, answered: Mapping[str, str | None]) -> list[Activity]:
    """Activities with no answer under the current catalog, longest first."""
    todo = [a for a in window.activities.values() if a.key not in answered]
    return sorted(todo, key=lambda a: (-a.seconds, a.key))


def save_answers(  # noqa: PLR0913 — the connection plus one keyword per payload field.
    conn: sqlite3.Connection,
    *,
    catalog: str,
    answered: Mapping[str, str | None],
    activities: Mapping[str, Activity],
    model: str,
    trigger: str,
) -> None:
    """Persist one batch's answers as one event."""
    emit_event(
        conn,
        type=EVENT_TYPE,
        payload={
            "catalog": catalog,
            "assignments": [
                {
                    "key": key,
                    "project": project,
                    "app": activities[key].app,
                    "label": activities[key].label,
                }
                for key, project in answered.items()
            ],
            "model": model,
            "trigger": trigger,
        },
        actor="jarvis_llm",
    )


@dataclass
class _Bucket:
    seconds: float = 0.0
    days: dict[str, float] = field(default_factory=dict)
    members: list[Activity] = field(default_factory=list)

    def add(self, activity: Activity) -> None:
        self.seconds += activity.seconds
        for day, seconds in activity.days.items():
            self.days[day] = self.days.get(day, 0.0) + seconds
        self.members.append(activity)

    def totals(self, day_keys: Sequence[str]) -> dict[str, Any]:
        return {
            "seconds": round(self.seconds),
            "days": [round(self.days.get(d, 0.0)) for d in day_keys],
            "count": len(self.members),
        }


def _local(value: datetime | None, zone: tzinfo) -> str | None:
    return None if value is None else value.astimezone(zone).isoformat(timespec="seconds")


def _commits(
    project: Project, window: Window, zone: tzinfo
) -> tuple[dict[str, Any], list[str]]:
    if not project.repos:
        return {"count": 0, "items": []}, []
    found, unreadable = local_commits(project.repos, window.start, window.end)
    newest = sorted(found.values(), key=lambda c: -c["committed_ms"])
    items = [
        {
            "sha": c["sha"][:12],
            "subject": c["subject"][:200],
            "committed_at": _local(datetime.fromtimestamp(c["committed_ms"] / 1000, UTC), zone),
            "on_main": c["on_main"],
            "repo": Path(c["paths"][0]).name,
        }
        for c in newest[:_MAX_COMMITS]
    ]
    return {"count": len(found), "items": items}, unreadable


def _recent(members: Iterable[Activity], zone: tzinfo) -> list[dict[str, Any]]:
    shown = [a for a in members if a.seconds >= RECENT_MIN_SECONDS and a.last_seen is not None]
    shown.sort(key=lambda a: a.last_seen or datetime.min.replace(tzinfo=UTC), reverse=True)
    return [
        {
            "app": a.app,
            "label": a.label,
            "seconds": round(a.seconds),
            "last_seen": _local(a.last_seen, zone),
        }
        for a in shown[:_MAX_RECENT]
    ]


def compose_view(  # noqa: PLR0913 — the window, its answers, the catalog and the clock.
    window: Window | None,
    answered: Mapping[str, str | None],
    sorted_at_ms: int | None,
    projects: Sequence[Project],
    zone: tzinfo,
    now: datetime,
) -> dict[str, Any]:
    """The dashboard's read model; ``window`` is None when TimeSink could not be read."""
    timesink_coverage = "unavailable" if window is None else "available"
    if window is None:
        window = empty_window(zone, now)
    day_keys = [d.isoformat() for d in window.days]
    buckets = {p.id: _Bucket() for p in projects}
    other, pending = _Bucket(), _Bucket()
    for activity in window.activities.values():
        if activity.key not in answered:
            pending.add(activity)
        else:
            buckets.get(answered[activity.key] or NONE, other).add(activity)
    rows: list[dict[str, Any]] = []
    unreadable: list[str] = []
    configured = any(p.repos for p in projects)
    for project in projects:
        bucket = buckets[project.id]
        totals = bucket.totals(day_keys)
        commits, missing = _commits(project, window, zone)
        unreadable.extend(missing)
        seen = [a.last_seen for a in bucket.members if a.last_seen is not None]
        rows.append(
            {
                "id": project.id,
                "name": project.name,
                "seconds": totals["seconds"],
                "today_seconds": totals["days"][-1],
                "days": totals["days"],
                "last_seen": _local(max(seen, default=None), zone),
                "commits": commits,
                "recent": _recent(bucket.members, zone),
            }
        )
    rows.sort(key=lambda r: -r["seconds"])  # stable: ties keep catalog order
    latest = max(
        (a.last_seen for a in window.activities.values() if a.last_seen is not None),
        default=None,
    )
    sorted_at = (
        None if sorted_at_ms is None else datetime.fromtimestamp(sorted_at_ms / 1000, UTC)
    )
    return {
        "days": day_keys,
        "projects": rows,
        "other": other.totals(day_keys),
        "unsorted": pending.totals(day_keys),
        "coverage": {
            "timesink": timesink_coverage,
            "git": "not_configured"
            if not configured
            else "partial"
            if unreadable
            else "available",
        },
        "latest_observed_at": _local(latest, zone),
        "sorted_at": _local(sorted_at, zone),
    }
