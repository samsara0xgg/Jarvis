"""Read TimeSink's existing SQLite spans without running or changing its collector."""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from jarvis.state.daily_contract import DailyError, fingerprint

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_REF = re.compile(r"timesink:([0-9a-f]{16}):([1-9][0-9]*):([0-9a-f]{32})")
_CAPTURE_REF = re.compile(r"timesink-capture:([0-9a-f]{16}):([1-9][0-9]*):([0-9a-f]{32})")
# The cited revision covers identity, time basis and text; image retention is not evidence.
_CAPTURE_VERSION_KEYS = (
    "id",
    "at",
    "lastSeenAt",
    "appBundleID",
    "appName",
    "windowID",
    "title",
    "spanID",
    "text",
)
# A stored stretch [at, lastSeenAt] written before TimeSink learned to break observation
# segments may cross one of these; the first such instant bounds when the content was really seen.
_CAPTURE_SELECT = (
    "SELECT id,at,lastSeenAt,appBundleID,appName,windowID,title,spanID,text,imagePath,"
    "(SELECT MIN(at) FROM stateEvent s WHERE s.at>c.at AND s.at<c.lastSeenAt "
    "AND s.kind IN ('lock','sleep','pause','stop')) AS interruptedAt,"
    "(SELECT MIN(start) FROM span p WHERE p.appBundleID!=c.appBundleID "
    "AND p.start>c.at AND p.start<c.lastSeenAt) AS displacedAt "
    "FROM capture c"
)
# Independent state dimensions; the latest event of each before `from` is the starting state.
_STATE_GROUPS = (
    ("start", "stop"),
    ("lock", "sleep", "unlock", "wake"),
    ("idle", "active"),
    ("pause", "resume", "screen_denied"),
)


class Snapshot:
    """One read transaction: spans, captures and state events are seen at the same instant."""

    def __init__(self, conn: sqlite3.Connection, identity: str) -> None:
        """Wrap an open read-only transaction and the store's identity."""
        self.conn = conn
        self.identity = identity


@contextmanager
def snapshot(path: Path | None) -> Iterator[Snapshot | None]:
    """Open a WAL-aware read transaction; None when the store is missing or unreadable."""
    if path is None:
        yield None
        return
    conn: sqlite3.Connection | None = None
    try:
        resolved = path.expanduser().resolve()
        stat = resolved.stat()
        identity = fingerprint([str(resolved), stat.st_dev, stat.st_ino])[:16]
        conn = sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True, timeout=1.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
    except (OSError, sqlite3.Error):
        if conn is not None:
            conn.close()
        yield None
        return
    try:
        yield Snapshot(conn, identity)
    finally:
        conn.close()


def _require(snap: Snapshot | None) -> Snapshot:
    if snap is None:
        message = "TimeSink database is missing, unreadable or incompatible"
        raise DailyError(message, "source_unavailable")
    return snap


def _moment(value: str) -> datetime:
    # GRDB's default datetime encoding is UTC without a suffix, with milliseconds.
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def moment(value: str) -> str:
    """ISO-8601 UTC rendering of a stored GRDB timestamp."""
    return _moment(value).isoformat(timespec="milliseconds")


def _sql_date(value: datetime, *, ceil: bool) -> str:
    """Render a bound in GRDB's own text format so SQL compares instants, not strings.

    Stored values have millisecond precision. For `col < X` and `col >= X` pass the
    millisecond ceiling, for `col > X` and `col <= X` the floor; both are then exact.
    """
    utc = value.astimezone(UTC)
    floor = utc.replace(microsecond=utc.microsecond // 1000 * 1000)
    if ceil and floor != utc:
        floor += timedelta(milliseconds=1)
    return floor.isoformat(sep=" ", timespec="milliseconds").removesuffix("+00:00")


def _reference(identity: str, row: dict[str, Any]) -> str:
    return f"timesink:{identity}:{row['id']}:{fingerprint(row)[:32]}"


def _capture_reference(identity: str, row: dict[str, Any]) -> str:
    version = fingerprint({key: row[key] for key in _CAPTURE_VERSION_KEYS})[:32]
    return f"timesink-capture:{identity}:{row['id']}:{version}"


def _unavailable(reason: str, watermark: int | None) -> dict[str, Any]:
    return {
        "items": [],
        "watermark": watermark or 0,
        "revision": 0,
        "coverage": {"status": "unavailable", "provider": "timesink", "reason": reason},
    }


def _item(row: dict[str, Any], identity: str, start: datetime, end: datetime) -> dict[str, Any]:
    began, ended = _moment(row["start"]), _moment(row["end"])
    reference = _reference(identity, row)
    return {
        "id": reference,
        "source": "app",
        "provider": "timesink",
        "kind": "app.usage_span",
        "observed_at": None,
        "occurred_at": began.isoformat(timespec="milliseconds"),
        "ended_at": ended.isoformat(timespec="milliseconds"),
        "time_basis": "span_overlap",
        "duration_seconds_in_window": max(
            0.0, (min(end, ended) - max(start, began)).total_seconds()
        ),
        "project": None,
        "project_basis": "not_inferred",
        "app_name": str(row["appName"])[:100],
        "app_bundle_id": str(row["appBundleID"])[:200],
        "summary": str(row["title"] or row["appName"])[:300],
        "url_excerpt": str(row["url"])[:500] if row["url"] is not None else None,
        "domain": str(row["domain"])[:200] if row["domain"] is not None else None,
        "source_refs": [reference],
        "detail_available": True,
    }


def query_spans(
    snap: Snapshot | None,
    start: datetime,
    end: datetime,
    *,
    watermark: int | None = None,
) -> dict[str, Any]:
    """Spans overlapping [start, end) as half-open intervals; fingerprint mutable revisions."""
    if snap is None:
        return _unavailable("TimeSink is disabled, missing, unreadable or incompatible.", watermark)
    try:
        conn = snap.conn
        high = int(conn.execute("SELECT COALESCE(MAX(id),0) FROM span").fetchone()[0])
        high = high if watermark is None else watermark
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT id,start,end,appBundleID,appName,title,url,domain FROM span "
                "WHERE id<=? AND start<? AND end>? "
                "AND end>start ORDER BY start,id",
                (high, _sql_date(end, ceil=True), _sql_date(start, ceil=False)),
            )
        ]
        latest = conn.execute("SELECT MAX(end) FROM span").fetchone()[0]
        rows = [row for row in rows if _moment(row["start"]) < end and _moment(row["end"]) > start]
        items = [_item(row, snap.identity, start, end) for row in rows]
        latest_end = _moment(latest).isoformat(timespec="milliseconds") if latest else None
    except sqlite3.Error:
        return _unavailable("TimeSink is disabled, missing, unreadable or incompatible.", watermark)
    except (ValueError, TypeError, OverflowError) as exc:
        message = "TimeSink contains invalid span data"
        raise DailyError(message, "source_invalid") from exc
    return {
        "items": items,
        "watermark": high,
        "revision": int(fingerprint([snap.identity, rows])[:15], 16),
        "coverage": {
            "status": "partial",
            "provider": "timesink",
            "readable_now": True,
            "observations_in_window": len(items),
            "latest_span_end": latest_end,
            "time_basis": "span_overlap",
            "gap_reason": "unknown",
            "reason": "Saved app/window/Chrome spans only; idle, lock, sleep and collector health "
            "are not persisted. Durations are TimeSink estimates, "
            "not attention or task completion.",
        },
    }


def _capture_end(row: dict[str, Any]) -> tuple[datetime, str]:
    """When the content was last really seen: lastSeenAt, unless an older row crossed a break."""
    ended = _moment(row["lastSeenAt"])
    breaks = [_moment(row[key]) for key in ("interruptedAt", "displacedAt") if row[key] is not None]
    if breaks and min(breaks) < ended:
        return min(breaks), "interrupted"
    return ended, "last_seen"


def _capture_item(
    row: dict[str, Any], identity: str, start: datetime, end: datetime
) -> dict[str, Any]:
    began = _moment(row["at"])
    ended, basis = _capture_end(row)
    reference = _capture_reference(identity, row)
    text = str(row["text"] or "")
    head = f"{row['appName']}: {row['title'] or ''}".strip(": ")
    return {
        "id": reference,
        "source": "screen",
        "provider": "timesink",
        "kind": "screen.capture",
        "observed_at": None,
        "occurred_at": began.isoformat(timespec="milliseconds"),
        "ended_at": ended.isoformat(timespec="milliseconds"),
        "ended_at_basis": basis,
        "last_seen_at": _moment(row["lastSeenAt"]).isoformat(timespec="milliseconds"),
        "time_basis": "capture_overlap",
        "duration_seconds_in_window": max(
            0.0, (min(end, ended) - max(start, began)).total_seconds()
        ),
        "project": None,
        "project_basis": "not_inferred",
        "app_name": str(row["appName"])[:100],
        "app_bundle_id": str(row["appBundleID"])[:200],
        "window_title": str(row["title"])[:300] if row["title"] is not None else None,
        "summary": f"{head} — {' '.join(text.split())}"[:300],
        "text_chars": len(text),
        "image_available": row["imagePath"] is not None,
        "source_refs": [reference],
        "detail_available": True,
    }


def query_captures(
    snap: Snapshot | None,
    start: datetime,
    end: datetime,
    *,
    watermark: int | None = None,
) -> dict[str, Any]:
    """Captures whose observation instants [at, ended_at] meet [start, end); same paging rules."""
    reason = "TimeSink is disabled, missing, unreadable, or predates screen capture."
    if snap is None:
        return _unavailable(reason, watermark)
    try:
        conn = snap.conn
        high = int(conn.execute("SELECT COALESCE(MAX(id),0) FROM capture").fetchone()[0])
        high = high if watermark is None else watermark
        rows = [
            dict(row)
            for row in conn.execute(
                f"{_CAPTURE_SELECT} WHERE id<=? AND at<? AND lastSeenAt>=? ORDER BY at,id",
                (high, _sql_date(end, ceil=True), _sql_date(start, ceil=True)),
            )
        ]
        latest = conn.execute("SELECT MAX(at) FROM capture").fetchone()[0]
        rows = [row for row in rows if _moment(row["at"]) < end and _capture_end(row)[0] >= start]
        items = [_capture_item(row, snap.identity, start, end) for row in rows]
        latest_at = _moment(latest).isoformat(timespec="milliseconds") if latest else None
    except sqlite3.Error:
        return _unavailable(reason, watermark)
    except (ValueError, TypeError, OverflowError) as exc:
        message = "TimeSink contains invalid capture data"
        raise DailyError(message, "source_invalid") from exc
    return {
        "items": items,
        "watermark": high,
        "revision": int(fingerprint([snap.identity, "capture", rows])[:15], 16),
        "coverage": {
            "status": "partial",
            "provider": "timesink",
            "readable_now": True,
            "observations_in_window": len(items),
            "latest_capture_at": latest_at,
            "time_basis": "capture_overlap",
            "reason": "One row per distinct front-window content (OCR text; image kept 7 days). "
            "A row spans occurred_at..ended_at while the content stayed in front; "
            "ended_at_basis=interrupted means an older row crossed a lock/sleep/pause/stop or "
            "another app, and ended_at is that recorded instant. "
            "Nothing is captured while locked, asleep, paused, or in excluded apps; "
            "see state_events and state_at_start for why a stretch is empty.",
        },
    }


def _event(row: sqlite3.Row) -> dict[str, Any]:
    return {"at": _moment(row["at"]).isoformat(timespec="milliseconds"), "kind": str(row["kind"])}


def query_state(
    snap: Snapshot | None,
    start: datetime,
    end: datetime,
    *,
    watermark: int | None = None,
) -> dict[str, Any]:
    """All state events inside [start, end) plus the latest of each kind group before start."""
    reason = "TimeSink is disabled, missing, unreadable, or predates state events."
    unavailable = {
        "events": [],
        "at_start": [],
        "watermark": watermark or 0,
        "coverage": {"status": "unavailable", "provider": "timesink", "reason": reason},
    }
    if snap is None:
        return unavailable
    try:
        conn = snap.conn
        high = int(conn.execute("SELECT COALESCE(MAX(id),0) FROM stateEvent").fetchone()[0])
        high = high if watermark is None else watermark
        rows = conn.execute(
            "SELECT at,kind FROM stateEvent WHERE id<=? AND at>=? AND at<? ORDER BY at,id",
            (high, _sql_date(start, ceil=True), _sql_date(end, ceil=True)),
        ).fetchall()
        events = [_event(row) for row in rows if start <= _moment(row["at"]) < end]
        at_start = []
        for kinds in _STATE_GROUPS:
            marks = ",".join("?" * len(kinds))
            row = conn.execute(
                f"SELECT at,kind FROM stateEvent WHERE id<=? AND at<? AND kind IN ({marks}) "  # noqa: S608 — placeholders only.
                "ORDER BY at DESC,id DESC LIMIT 1",
                (high, _sql_date(start, ceil=True), *kinds),
            ).fetchone()
            if row is not None and _moment(row["at"]) < start:
                at_start.append(_event(row))
        at_start.sort(key=lambda event: event["at"])
    except sqlite3.Error:
        return unavailable
    except (ValueError, TypeError, OverflowError) as exc:
        message = "TimeSink contains invalid state data"
        raise DailyError(message, "source_invalid") from exc
    return {
        "events": events,
        "at_start": at_start,
        "watermark": high,
        "coverage": {
            "status": "partial",
            "provider": "timesink",
            "events_in_window": len(events),
            "reason": "TimeSink's own stop/resume reasons (idle/active, lock/unlock, sleep/wake, "
            "pause/resume, start/stop, screen_denied); state_at_start is the latest of each "
            "kind before the window. Gaps without a reason remain unknown.",
        },
    }


def read_capture(path: Path | None, reference: str) -> dict[str, Any]:
    """Resolve exactly the cited capture revision; image_path is absolute when still kept."""
    match = _CAPTURE_REF.fullmatch(reference)
    if match is None or int(match[2]) >= 2**63:
        message = "Use the screen activity ID returned by query_activity"
        raise DailyError(message, "invalid_source")
    with snapshot(path) as opened:
        snap = _require(opened)
        if snap.identity != match[1]:
            message = "TimeSink database identity changed; query again"
            raise DailyError(message, "source_changed")
        try:
            row = snap.conn.execute(f"{_CAPTURE_SELECT} WHERE id=?", (int(match[2]),)).fetchone()
        except sqlite3.Error as exc:
            message = "TimeSink database is missing, unreadable or incompatible"
            raise DailyError(message, "source_unavailable") from exc
    if row is None:
        message = "TimeSink capture no longer exists"
        raise DailyError(message, "not_found")
    original = dict(row)
    if _capture_reference(snap.identity, original) != reference:
        message = "TimeSink capture was updated; query again for its current revision"
        raise DailyError(message, "source_changed")
    if original["imagePath"] is not None and path is not None:
        image = path.expanduser().resolve().parent / "captures" / str(original["imagePath"])
        original["imagePath"] = str(image) if image.is_file() else None
    ended, basis = _capture_end(original)
    original["endedAt"] = ended.isoformat(timespec="milliseconds")
    original["endedAtBasis"] = basis
    return original


def read_span(path: Path | None, reference: str) -> dict[str, Any]:
    """Resolve exactly the cited row revision, never silently substitute updated data."""
    match = _REF.fullmatch(reference)
    if match is None or int(match[2]) >= 2**63:
        message = "Use the TimeSink activity ID returned by query_activity"
        raise DailyError(message, "invalid_source")
    with snapshot(path) as opened:
        snap = _require(opened)
        if snap.identity != match[1]:
            message = "TimeSink database identity changed; query again"
            raise DailyError(message, "source_changed")
        try:
            row = snap.conn.execute(
                "SELECT id,start,end,appBundleID,appName,title,url,domain FROM span WHERE id=?",
                (int(match[2]),),
            ).fetchone()
        except sqlite3.Error as exc:
            message = "TimeSink database is missing, unreadable or incompatible"
            raise DailyError(message, "source_unavailable") from exc
    if row is None:
        message = "TimeSink span no longer exists"
        raise DailyError(message, "not_found")
    original = dict(row)
    if _reference(snap.identity, original) != reference:
        message = "TimeSink span was updated; query again for its current revision"
        raise DailyError(message, "source_changed")
    return original
