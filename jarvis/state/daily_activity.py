"""Read saved Git and TimeSink activity; unavailable collectors remain explicit."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis.state import timesink
from jarvis.state.daily_contract import (
    PAGE_BUDGET,
    DailyError,
    cursor_position,
    encoded,
    fingerprint,
    fit,
    make_cursor,
    page_rows,
    text_chunk,
    window,
)
from jarvis.state.daily_report import codex_session, git_show
from jarvis.state.daily_store import high_water
from jarvis.state.event_log import read_log_epoch

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

_ACTIVITY_TYPES = ("repo.state_observed", "project.commit_seen")


def _iso(milliseconds: int) -> str:
    return datetime.fromtimestamp(milliseconds / 1000, UTC).isoformat(timespec="milliseconds")


def query_activity(
    conn: sqlite3.Connection,
    args: dict[str, Any],
    repos: tuple[str, ...],
    timesink_path: Path | None = None,
) -> dict[str, Any]:
    """Query by observed time, distinguishing late-observed commits from new commits."""
    start, end = window(args)
    if start is None or end is None:
        msg = "from and to are required"
        raise DailyError(msg)
    binding = fingerprint(
        [
            "activity",
            read_log_epoch(conn),
            {k: v for k, v in args.items() if k != "cursor"},
        ]
    )
    (
        snapshot,
        app_watermark,
        app_revision,
        screen_watermark,
        screen_revision,
        offset,
        state_watermark,
        state_offset,
    ) = cursor_position(args.get("cursor"), binding, [high_water(conn), 0, 0, 0, 0, 0, 0, 0])
    rows = conn.execute(
        "SELECT event_uid,type,ts_epoch_ms,payload_json FROM events "
        "WHERE type IN (?,?) AND id<=? AND ts_epoch_ms>=? AND ts_epoch_ms<? "
        "ORDER BY ts_epoch_ms,id",
        (*_ACTIVITY_TYPES, snapshot, int(start.timestamp() * 1000), int(end.timestamp() * 1000)),
    ).fetchall()
    items = []
    skipped = 0
    for uid, kind, observed, raw in rows:
        payload = json.loads(raw)
        repo = payload["repo_path"]
        # The repository path is an unambiguous project key; basenames can collide.
        if "project" in args and args["project"] != repo:
            continue
        skipped += int(payload.get("skipped_count", 0))
        items.append(
            {
                "id": f"activity:{uid}",
                "source": "git",
                "kind": kind,
                "observed_at": _iso(observed),
                "occurred_at": _iso(payload["committed_at_ms"])
                if kind == "project.commit_seen"
                else _iso(payload["observed_at_ms"]),
                "project": repo,
                "project_basis": "observed_repo_path",
                "ended_at": None,
                "summary": payload.get("subject", payload.get("last_commit_subject", "")),
                "source_refs": [f"event:{uid}"],
                "detail_available": True,
            }
        )
    sources = args.get("sources", ["git", "app", "screen", "agent"])
    coverage: dict[str, Any] = {
        source: {"status": "not_implemented"}
        for source in sources
        if source not in {"git", "app", "screen"}
    }
    if "git" in sources:
        enabled = bool(repos) if "project" not in args else args["project"] in repos
        coverage["git"] = {
            "status": "unknown",
            "configured_now": enabled,
            "observations_in_window": len(items),
            "skipped_count": skipped,
            "last_observed_at_in_window": items[-1]["observed_at"] if items else None,
            "reason": "Historical collector health is not recorded; absence is not inactivity.",
        }
    else:
        items = []
    uses_timesink = bool({"app", "screen"} & set(sources)) and timesink_path is not None
    # One read transaction, so spans, captures and state events describe the same instant.
    with timesink.snapshot(timesink_path if uses_timesink else None) as snap:
        if "app" in sources:
            app_watermark, app_revision = _timesink_source(
                timesink.query_spans,
                args,
                start,
                end,
                snap,
                (app_watermark, app_revision),
                items=items,
                coverage=coverage,
                name="app",
                noun="TimeSink spans",
            )
        if "screen" in sources:
            screen_watermark, screen_revision = _timesink_source(
                timesink.query_captures,
                args,
                start,
                end,
                snap,
                (screen_watermark, screen_revision),
                items=items,
                coverage=coverage,
                name="screen",
                noun="Screen captures",
            )
        state = timesink.query_state(
            snap, start, end, watermark=state_watermark if args.get("cursor") else None
        )
    if not uses_timesink:
        state["coverage"] = {"status": "unknown", "reason": "Reported with app or screen sources."}
    items.sort(key=lambda item: (item["observed_at"] or item["occurred_at"], item["id"]))
    result = page_rows(items, args, binding, snapshot, offset)
    # State events share the page budget with the items: never silently dropped, only paged.
    events = fit(state["events"][state_offset:], PAGE_BUDGET - len(encoded(result["items"])))
    state_end = state_offset + len(events)
    result["next_cursor"] = None
    if offset + result["count"] < len(items) or state_end < len(state["events"]):
        result["next_cursor"] = make_cursor(
            binding,
            [
                snapshot,
                app_watermark,
                app_revision,
                screen_watermark,
                screen_revision,
                offset + result["count"],
                state["watermark"],
                state_end,
            ],
        )
    result.update(
        coverage=coverage,
        time_basis="source_specific" if uses_timesink else "observed_at",
        timesink_snapshot=app_watermark,
        # Why TimeSink stopped/resumed: the latest lock/idle/pause/tracker event before the
        # window, then every event inside it (idle/active, lock/unlock, sleep/wake,
        # pause/resume, start/stop, screen_denied) in pages.
        state_at_start=state["at_start"],
        state_events=events,
        state_coverage=state["coverage"],
    )
    return result


def _timesink_source(  # noqa: PLR0913 — one paging pin per source, threaded from the cursor.
    query: Callable[..., dict[str, Any]],
    args: dict[str, Any],
    start: datetime,
    end: datetime,
    snap: timesink.Snapshot | None,
    pinned: tuple[int, int],
    *,
    items: list[dict[str, Any]],
    coverage: dict[str, Any],
    name: str,
    noun: str,
) -> tuple[int, int]:
    """Fold one TimeSink-backed source into the result, honouring the cursor's revision pin."""
    watermark, revision = pinned
    if "project" in args:
        found: dict[str, Any] = {
            "items": [],
            "watermark": 0,
            "revision": 0,
            "coverage": {
                "status": "unknown",
                "provider": "timesink",
                "reason": f"{noun} have no repository mapping; project filter excludes them.",
            },
        }
    else:
        found = query(snap, start, end, watermark=watermark if args.get("cursor") else None)
    if args.get("cursor") and revision != found["revision"]:
        message = "TimeSink results changed while paging; restart the query"
        raise DailyError(message, "invalid_cursor")
    items.extend(found["items"])
    coverage[name] = found["coverage"]
    return found["watermark"], found["revision"]


def _report_source(identity: str) -> tuple[str, str]:
    """The commit or the Codex session behind a daily-report reference, as text."""
    if identity.startswith("git:"):
        repo, _, sha = identity.partition(":")[2].rpartition(":")
        shown = git_show(repo, sha)
        if shown is None:
            msg = "Saved commit does not exist"
            raise DailyError(msg, "not_found")
        return shown, "git.commit"
    path = Path(identity.partition(":")[2])
    if not path.is_file():
        msg = "Saved Codex session does not exist"
        raise DailyError(msg, "not_found")
    session = codex_session(path)
    turns = "\n".join(f"{when} {who}: {text}" for when, who, text in session["turns"])
    head = (
        f"Codex session {session['id']} ({session['originator']}, {session['cwd']}); "
        "the agent's own account, not a verified result\n"
    )
    return head + turns, "codex.session"


def read_activity(
    conn: sqlite3.Connection,
    args: dict[str, Any],
    timesink_path: Path | None = None,
) -> dict[str, Any]:
    """Read only a saved observation, never capture the current screen or diff."""
    identity = args["activity_id"]
    if identity.startswith("timesink-capture:"):
        row = timesink.read_capture(timesink_path, identity)
        text = str(row["text"] or "")
        [offset] = cursor_position(args.get("cursor"), identity, [0])
        chunk, end = text_chunk(text, offset)
        return {
            "id": identity,
            "source_refs": [identity],
            "kind": "screen.capture",
            "observed_at": None,
            "occurred_at": timesink.moment(row["at"]),
            "ended_at": row["endedAt"],
            "ended_at_basis": row["endedAtBasis"],
            "last_seen_at": timesink.moment(row["lastSeenAt"]),
            "app_name": row["appName"],
            "app_bundle_id": row["appBundleID"],
            "window_title": row["title"],
            "span_id": row["spanID"],
            "image_available": row["imagePath"] is not None,
            "image_path": row["imagePath"],
            "content": chunk,
            "content_format": "text",
            "offset": offset,
            "total_chars": len(text),
            "complete": end == len(text),
            "next_cursor": make_cursor(identity, [end]) if end < len(text) else None,
        }
    if identity.startswith("timesink:"):
        original = encoded(timesink.read_span(timesink_path, identity))
        [offset] = cursor_position(args.get("cursor"), identity, [0])
        chunk, end = text_chunk(original, offset)
        return {
            "id": identity,
            "source_refs": [identity],
            "kind": "app.usage_span",
            "observed_at": None,
            "stored_timestamp_timezone": "UTC",
            "content": chunk,
            "content_format": "json",
            "offset": offset,
            "total_chars": len(original),
            "complete": end == len(original),
            "next_cursor": make_cursor(identity, [end]) if end < len(original) else None,
        }
    if identity.startswith(("git:", "codex-session:")):
        # ADR 0025: a daily report cites commits and Codex sessions; the original behind
        # either is read from where it lives, paged like every other saved observation.
        text, kind = _report_source(identity)
        [offset] = cursor_position(args.get("cursor"), identity, [0])
        chunk, end = text_chunk(text, offset)
        return {
            "id": identity,
            "source_refs": [identity],
            "kind": kind,
            "observed_at": None,
            "content": chunk,
            "content_format": "text",
            "offset": offset,
            "total_chars": len(text),
            "complete": end == len(text),
            "next_cursor": make_cursor(identity, [end]) if end < len(text) else None,
        }
    if not identity.startswith("activity:"):
        msg = "Use the activity ID returned by query_activity"
        raise DailyError(msg)
    uid = identity.removeprefix("activity:")
    row = conn.execute(
        "SELECT type,ts_epoch_ms,payload_json FROM events WHERE event_uid=? AND type IN (?,?)",
        (uid, *_ACTIVITY_TYPES),
    ).fetchone()
    if row is None:
        msg = "Saved activity does not exist"
        raise DailyError(msg, "not_found")
    binding = fingerprint([identity, row])
    [offset] = cursor_position(args.get("cursor"), binding, [0])
    chunk, end = text_chunk(row[2], offset)
    return {
        "id": identity,
        "source_refs": [f"event:{uid}"],
        "kind": row[0],
        "observed_at": _iso(row[1]),
        "content": chunk,
        "content_format": "json",
        "offset": offset,
        "total_chars": len(row[2]),
        "complete": end == len(row[2]),
        "next_cursor": make_cursor(binding, [end]) if end < len(row[2]) else None,
    }
