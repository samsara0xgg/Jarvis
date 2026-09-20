"""Read TimeSink's existing SQLite spans without running or changing its collector."""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from jarvis.state.daily_contract import DailyError, fingerprint

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_REF = re.compile(r"timesink:([0-9a-f]{16}):([1-9][0-9]*):([0-9a-f]{32})")
_CAPTURE_REF = re.compile(r"timesink-capture:([0-9a-f]{16}):([1-9][0-9]*):([0-9a-f]{32})")
_CAPTURE_COLUMNS = "id,at,lastSeenAt,appBundleID,appName,windowID,title,spanID,text,imagePath"
_STATE_EVENT_LIMIT = 200


@contextmanager
def _connection(path: Path | None) -> Iterator[tuple[sqlite3.Connection, str]]:
    """Read a WAL-aware transaction; missing stores are never created or migrated."""
    if path is None:
        message = "TimeSink is not configured"
        raise DailyError(message, "source_unavailable")
    try:
        resolved = path.expanduser().resolve()
        stat = resolved.stat()
        identity = fingerprint([str(resolved), stat.st_dev, stat.st_ino])[:16]
        conn = sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True, timeout=1.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            yield conn, identity
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        message = "TimeSink database is missing, unreadable or incompatible"
        raise DailyError(message, "source_unavailable") from exc


def _moment(value: str) -> datetime:
    # GRDB's default datetime encoding is UTC without a suffix, with milliseconds.
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def moment(value: str) -> str:
    """ISO-8601 UTC rendering of a stored GRDB timestamp."""
    return _moment(value).isoformat(timespec="milliseconds")


def _sql_date(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(sep=" ", timespec="microseconds").removesuffix("+00:00")


def _reference(identity: str, row: dict[str, Any]) -> str:
    return f"timesink:{identity}:{row['id']}:{fingerprint(row)[:32]}"


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
    path: Path | None,
    start: datetime,
    end: datetime,
    *,
    watermark: int | None = None,
) -> dict[str, Any]:
    """Query overlapping spans in one read snapshot; fingerprint mutable revisions."""
    try:
        with _connection(path) as (conn, identity):
            high = int(conn.execute("SELECT COALESCE(MAX(id),0) FROM span").fetchone()[0])
            high = high if watermark is None else watermark
            rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT id,start,end,appBundleID,appName,title,url,domain FROM span "
                    "WHERE id<=? AND start<? AND end>? "
                    "AND end>start ORDER BY start,id",
                    (high, _sql_date(end), _sql_date(start)),
                )
            ]
            latest = conn.execute("SELECT MAX(end) FROM span").fetchone()[0]
        # Text ordering may include a row exactly at the upper bound when timestamp
        # precision differs. Check exact instants before returning or fingerprinting.
        rows = [row for row in rows if _moment(row["start"]) < end and _moment(row["end"]) > start]
        items = [_item(row, identity, start, end) for row in rows]
        latest_end = _moment(latest).isoformat(timespec="milliseconds") if latest else None
    except DailyError:
        return {
            "items": [],
            "watermark": watermark or 0,
            "revision": int(fingerprint(["unavailable", str(path)])[:15], 16),
            "coverage": {
                "status": "unavailable",
                "provider": "timesink",
                "reason": "TimeSink is disabled, missing, unreadable or incompatible.",
            },
        }
    except (ValueError, TypeError, OverflowError) as exc:
        message = "TimeSink contains invalid span data"
        raise DailyError(message, "source_invalid") from exc
    return {
        "items": items,
        "watermark": high,
        "revision": int(fingerprint([identity, rows])[:15], 16),
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


def _capture_item(
    row: dict[str, Any], identity: str, start: datetime, end: datetime
) -> dict[str, Any]:
    began, ended = _moment(row["at"]), _moment(row["lastSeenAt"])
    reference = f"timesink-capture:{identity}:{row['id']}:{fingerprint(row)[:32]}"
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
    path: Path | None,
    start: datetime,
    end: datetime,
    *,
    watermark: int | None = None,
) -> dict[str, Any]:
    """Screen captures overlapping [start, end); same snapshot/revision rules as spans."""
    try:
        with _connection(path) as (conn, identity):
            high = int(conn.execute("SELECT COALESCE(MAX(id),0) FROM capture").fetchone()[0])
            high = high if watermark is None else watermark
            rows = [
                dict(row)
                for row in conn.execute(
                    f"SELECT {_CAPTURE_COLUMNS} FROM capture "  # noqa: S608 — column list is a module constant.
                    "WHERE id<=? AND at<? AND lastSeenAt>=? ORDER BY at,id",
                    (high, _sql_date(end), _sql_date(start)),
                )
            ]
            latest = conn.execute("SELECT MAX(at) FROM capture").fetchone()[0]
        rows = [
            row for row in rows if _moment(row["at"]) < end and _moment(row["lastSeenAt"]) >= start
        ]
        items = [_capture_item(row, identity, start, end) for row in rows]
        latest_at = _moment(latest).isoformat(timespec="milliseconds") if latest else None
    except DailyError:
        return {
            "items": [],
            "watermark": watermark or 0,
            "revision": int(fingerprint(["unavailable", "capture", str(path)])[:15], 16),
            "coverage": {
                "status": "unavailable",
                "provider": "timesink",
                "reason": "TimeSink is disabled, missing, unreadable, or predates screen capture.",
            },
        }
    except (ValueError, TypeError, OverflowError) as exc:
        message = "TimeSink contains invalid capture data"
        raise DailyError(message, "source_invalid") from exc
    return {
        "items": items,
        "watermark": high,
        "revision": int(fingerprint([identity, "capture", rows])[:15], 16),
        "coverage": {
            "status": "partial",
            "provider": "timesink",
            "readable_now": True,
            "observations_in_window": len(items),
            "latest_capture_at": latest_at,
            "time_basis": "capture_overlap",
            "reason": "One row per distinct front-window content (OCR text; image kept 7 days). "
            "A row spans occurred_at..ended_at while the content stayed the same. "
            "Nothing is captured while locked, asleep, paused, or in excluded apps; "
            "see state_events for why a stretch is empty.",
        },
    }


def state_events(path: Path | None, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Why TimeSink stopped or resumed inside [start, end); empty when unavailable."""
    try:
        with _connection(path) as (conn, _identity):
            rows = conn.execute(
                "SELECT at,kind FROM stateEvent WHERE at>=? AND at<? ORDER BY at,id LIMIT ?",
                (_sql_date(start), _sql_date(end), _STATE_EVENT_LIMIT),
            ).fetchall()
    except DailyError:
        return []
    return [
        {"at": _moment(row["at"]).isoformat(timespec="milliseconds"), "kind": str(row["kind"])}
        for row in rows
    ]


def read_capture(path: Path | None, reference: str) -> dict[str, Any]:
    """Resolve exactly the cited capture revision; image_path is absolute when still kept."""
    match = _CAPTURE_REF.fullmatch(reference)
    if match is None or int(match[2]) >= 2**63:
        message = "Use the screen activity ID returned by query_activity"
        raise DailyError(message, "invalid_source")
    with _connection(path) as (conn, identity):
        if identity != match[1]:
            message = "TimeSink database identity changed; query again"
            raise DailyError(message, "source_changed")
        row = conn.execute(
            f"SELECT {_CAPTURE_COLUMNS} FROM capture WHERE id=?",  # noqa: S608 — column list is a module constant.
            (int(match[2]),),
        ).fetchone()
    if row is None:
        message = "TimeSink capture no longer exists"
        raise DailyError(message, "not_found")
    original = dict(row)
    if f"timesink-capture:{identity}:{original['id']}:{fingerprint(original)[:32]}" != reference:
        message = "TimeSink capture was updated; query again for its current revision"
        raise DailyError(message, "source_changed")
    if original["imagePath"] is not None and path is not None:
        original["imagePath"] = str(
            path.expanduser().resolve().parent / "captures" / str(original["imagePath"])
        )
    return original


def read_span(path: Path | None, reference: str) -> dict[str, Any]:
    """Resolve exactly the cited row revision, never silently substitute updated data."""
    match = _REF.fullmatch(reference)
    if match is None or int(match[2]) >= 2**63:
        message = "Use the TimeSink activity ID returned by query_activity"
        raise DailyError(message, "invalid_source")
    with _connection(path) as (conn, identity):
        if identity != match[1]:
            message = "TimeSink database identity changed; query again"
            raise DailyError(message, "source_changed")
        row = conn.execute(
            "SELECT id,start,end,appBundleID,appName,title,url,domain FROM span WHERE id=?",
            (int(match[2]),),
        ).fetchone()
    if row is None:
        message = "TimeSink span no longer exists"
        raise DailyError(message, "not_found")
    original = dict(row)
    if _reference(identity, original) != reference:
        message = "TimeSink span was updated; query again for its current revision"
        raise DailyError(message, "source_changed")
    return original
