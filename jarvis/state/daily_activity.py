"""Read saved Git and TimeSink activity as one compact table; unavailable collectors stay explicit.

The table is rendered by code, never by a model: shared facts (date, UTC offset, store,
app dictionary, per-app totals, coverage) are stated once per page and every row is a
short array. Nothing is dropped: a page ends when the character budget is full and the
cursor continues from the next row.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis.state import timesink
from jarvis.state.daily_contract import (
    ACTIVITY_PAGE_BUDGET,
    DailyError,
    cursor_position,
    encoded,
    fingerprint,
    make_cursor,
    text_chunk,
    window,
)
from jarvis.state.daily_report import codex_session, git_show
from jarvis.state.daily_store import high_water
from jarvis.state.event_log import read_log_epoch

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterable

_ACTIVITY_TYPES = ("repo.state_observed", "project.commit_seen")
_GIT_ROW_REF = re.compile(r"g([0-9a-f]{32})")
COLUMNS = ("ref", "start", "end", "app", "text")
SPAN_TEXT_CHARS = 120
CAPTURE_TEXT_CHARS = 200
_ALL_SOURCES = ("git", "app", "screen", "agent")
# Stated once, on the first page of a query; continuation pages repeat only the data.
_NOTES = (
    "text: app rows = window title (and site domain); screen rows = window title — the "
    "start of TimeSink's OCR text, an excerpt, not the full text. read_activity(activity_id="
    "<ref>) returns the full OCR text of a screen row or the full app span row.",
    "refs: s<id>:<rev> app span, c<id>:<rev> screen capture, g<uid> Git observation. Full "
    "source_refs for saving: timesink:<store>:<id>:<rev>, timesink-capture:<store>:<id>:<rev>, "
    "event:<uid>; read_activity also returns them.",
    "times: local clock at utc_offset, MM-DD prefix when not on date; an end time ending in ! "
    "is an interruption bound (lock, sleep, pause, another app): the content was seen no later "
    "than that.",
    "totals: foreground_s = seconds the app's window was frontmost per TimeSink spans, clipped "
    "to [from,to) with overlapping spans merged. TimeSink closes spans when idle, so idle time "
    "is excluded; background playback and windows behind others are not observed. A screen row "
    "proves the content was on screen, not that a message was sent or read.",
)


def _iso(milliseconds: int) -> str:
    return datetime.fromtimestamp(milliseconds / 1000, UTC).isoformat(timespec="milliseconds")


def _letters(index: int) -> str:
    """A, B, ... Z, AA, AB, ...: stable dictionary keys for any number of apps."""
    label = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        label = chr(ord("A") + rem) + label
    return label


class _Clock:
    """Render instants in the offset of ``from``, relative to that instant's local date."""

    def __init__(self, start: datetime) -> None:
        self.tz = timezone(start.utcoffset() or timedelta())
        self.date = start.astimezone(self.tz).date()

    def offset_text(self) -> str:
        delta = self.tz.utcoffset(None) or timedelta()
        sign = "-" if delta < timedelta() else "+"
        minutes = abs(int(delta.total_seconds())) // 60
        return f"{sign}{minutes // 60:02d}:{minutes % 60:02d}"

    def render(self, iso_value: str) -> str:
        moment = datetime.fromisoformat(iso_value).astimezone(self.tz)
        clock = moment.strftime("%H:%M:%S")
        if moment.date() == self.date:
            return clock
        prefix = f"{moment:%m-%d}" if moment.year == self.date.year else f"{moment:%Y-%m-%d}"
        return f"{prefix} {clock}"


def _git_items(
    conn: sqlite3.Connection, args: dict[str, Any], snapshot: int, start: datetime, end: datetime
) -> tuple[list[dict[str, Any]], int]:
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
                "source": "git",
                "uid": uid,
                "kind": kind,
                "observed_at": _iso(observed),
                "occurred_at": _iso(payload["committed_at_ms"])
                if kind == "project.commit_seen"
                else _iso(payload["observed_at_ms"]),
                "project": repo,
                "summary": payload.get("subject", payload.get("last_commit_subject", "")),
            }
        )
    return items, skipped


def _app_matches(item: dict[str, Any], needle: str) -> bool:
    return (
        needle in str(item["app_name"]).casefold()
        or needle in str(item["app_bundle_id"]).casefold()
    )


def _timesink_source(  # noqa: PLR0913 — one paging pin per source, threaded from the cursor.
    query: Callable[..., dict[str, Any]],
    args: dict[str, Any],
    start: datetime,
    end: datetime,
    snap: timesink.Snapshot | None,
    pinned: tuple[int, int],
    *,
    coverage: dict[str, Any],
    name: str,
    noun: str,
) -> tuple[list[dict[str, Any]], int, int]:
    """One TimeSink-backed source, honouring the cursor's revision pin and the app filter."""
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
        message = (
            "TimeSink rows changed while paging (a row was corrected or the store was "
            "replaced); repeat the query without a cursor"
        )
        raise DailyError(message, "invalid_cursor")
    items = found["items"]
    if "app" in args:
        needle = str(args["app"]).casefold()
        items = [item for item in items if _app_matches(item, needle)]
        found["coverage"]["app_filter_matches"] = len(items)
    coverage[name] = found["coverage"]
    return items, found["watermark"], found["revision"]


def _merged_seconds(intervals: Iterable[tuple[datetime, datetime]]) -> float:
    total = 0.0
    current: tuple[datetime, datetime] | None = None
    for began, ended in sorted(intervals):
        if current is None or began > current[1]:
            if current is not None:
                total += (current[1] - current[0]).total_seconds()
            current = (began, ended)
        elif ended > current[1]:
            current = (current[0], ended)
    if current is not None:
        total += (current[1] - current[0]).total_seconds()
    return total


def _app_key(item: dict[str, Any]) -> tuple[str, str]:
    return (str(item["app_name"]), str(item["app_bundle_id"]))


def _app_dictionary(
    items: list[dict[str, Any]], start: datetime, end: datetime, clock: _Clock
) -> tuple[dict[tuple[str, str], str], dict[str, list[str]], dict[str, dict[str, Any]]]:
    """Letters, names and whole-window totals per app, ordered by foreground time."""
    per_app: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        if item["source"] == "git":
            continue
        key = _app_key(item)
        entry = per_app.setdefault(
            key, {"intervals": [], "spans": 0, "screen_rows": 0, "first": None, "last": None}
        )
        if item["source"] == "app":
            began = max(start, datetime.fromisoformat(item["occurred_at"]))
            ended = min(end, datetime.fromisoformat(item["ended_at"]))
            if ended > began:
                entry["intervals"].append((began, ended))
            entry["spans"] += 1
            entry["first"] = began if entry["first"] is None else min(entry["first"], began)
            entry["last"] = ended if entry["last"] is None else max(entry["last"], ended)
        else:
            entry["screen_rows"] += 1
    ordered = sorted(
        per_app.items(),
        key=lambda kv: (-_merged_seconds(kv[1]["intervals"]), kv[0][0], kv[0][1]),
    )
    letters: dict[tuple[str, str], str] = {}
    apps: dict[str, list[str]] = {}
    totals: dict[str, dict[str, Any]] = {}
    for index, ((name, bundle), entry) in enumerate(ordered):
        letter = _letters(index)
        letters[name, bundle] = letter
        apps[letter] = [name, bundle]
        total: dict[str, Any] = {
            "foreground_s": round(_merged_seconds(entry["intervals"])),
            "spans": entry["spans"],
            "screen_rows": entry["screen_rows"],
        }
        if entry["first"] is not None:
            total["first"] = clock.render(entry["first"].isoformat())
            total["last"] = clock.render(entry["last"].isoformat())
        totals[letter] = total
    return letters, apps, totals


def _short_ref(reference: str) -> str:
    parsed = timesink.parse_reference(reference)
    if parsed is None:
        return reference
    kind, _, row_id, revision = parsed
    return f"{kind}{row_id}:{revision[: timesink.SHORT_REVISION_CHARS]}"


def _row(item: dict[str, Any], letters: dict[tuple[str, str], str], clock: _Clock) -> list[str]:
    if item["source"] == "git":
        committed = ""
        if item["kind"] == "project.commit_seen" and item["occurred_at"] != item["observed_at"]:
            committed = f" (committed {clock.render(item['occurred_at'])})"
        text = f"{item['project']}: {item['summary']}{committed}"
        return [f"g{item['uid']}", clock.render(item["observed_at"]), "", "git", text[:300]]
    letter = letters[_app_key(item)]
    ended = clock.render(item["ended_at"])
    text = str(item["summary"])
    if item["source"] == "app":
        domain = item.get("domain")
        if domain and domain not in text:
            text = f"{text} ({domain})"
        text = text[:SPAN_TEXT_CHARS]
    else:
        # The app is the column; the excerpt keeps window title — OCR head.
        text = text.removeprefix(f"{item['app_name']}: ")[:CAPTURE_TEXT_CHARS]
        if item.get("ended_at_basis") == "interrupted":
            ended += "!"
    began = clock.render(item["occurred_at"])
    return [_short_ref(item["source_refs"][0]), began, ended, letter, text]


def _sort_key(item: dict[str, Any]) -> tuple[str, str]:
    moment = item["observed_at"] if item["source"] == "git" else item["occurred_at"]
    return (moment, item["uid"] if item["source"] == "git" else item["id"])


def _take(rows: list[Any], budget: int) -> list[Any]:
    """Whole leading rows whose JSON stays within budget (linear in the rows taken)."""
    used = 2  # the enclosing brackets
    taken: list[Any] = []
    for row in rows:
        cost = len(encoded(row)) + (2 if taken else 0)
        if used + cost > budget:
            break
        used += cost
        taken.append(row)
    return taken


def query_activity(
    conn: sqlite3.Connection,
    args: dict[str, Any],
    repos: tuple[str, ...],
    timesink_path: Path | None = None,
) -> dict[str, Any]:
    """One compact page of activity in [from,to): dictionary and totals first, then rows."""
    start, end = window(args)
    if start is None or end is None:
        msg = "from and to are required"
        raise DailyError(msg)
    clock = _Clock(start)
    binding = fingerprint(
        [
            "activity-table",
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
    sources = args.get("sources", list(_ALL_SOURCES))
    coverage: dict[str, Any] = {
        source: {"status": "not_implemented"}
        for source in sources
        if source not in {"git", "app", "screen"}
    }
    items: list[dict[str, Any]] = []
    if "git" in sources and "app" not in args:
        items, skipped = _git_items(conn, args, snapshot, start, end)
        enabled = bool(repos) if "project" not in args else args["project"] in repos
        coverage["git"] = {
            "status": "unknown",
            "configured_now": enabled,
            "observations_in_window": len(items),
            "skipped_count": skipped,
            "last_observed_at_in_window": items[-1]["observed_at"] if items else None,
            "reason": "Historical collector health is not recorded; absence is not inactivity.",
        }
    elif "git" in sources:
        coverage["git"] = {"status": "unknown", "reason": "app filter excludes Git observations."}
    uses_timesink = bool({"app", "screen"} & set(sources)) and timesink_path is not None
    store: str | None = None
    # One read transaction, so spans, captures and state events describe the same instant.
    with timesink.snapshot(timesink_path if uses_timesink else None) as snap:
        store = snap.identity if snap is not None else None
        if "app" in sources:
            found, app_watermark, app_revision = _timesink_source(
                timesink.query_spans,
                args,
                start,
                end,
                snap,
                (app_watermark, app_revision),
                coverage=coverage,
                name="app",
                noun="TimeSink spans",
            )
            items.extend(found)
        if "screen" in sources:
            found, screen_watermark, screen_revision = _timesink_source(
                timesink.query_captures,
                args,
                start,
                end,
                snap,
                (screen_watermark, screen_revision),
                coverage=coverage,
                name="screen",
                noun="Screen captures",
            )
            items.extend(found)
        state = timesink.query_state(
            snap, start, end, watermark=state_watermark if args.get("cursor") else None
        )
    if not uses_timesink:
        state["coverage"] = {"status": "unknown", "reason": "Reported with app or screen sources."}
    items.sort(key=_sort_key)
    letters, apps, totals = _app_dictionary(items, start, end, clock)
    rows = [_row(item, letters, clock) for item in items]
    if args.get("summary_only"):
        page: list[Any] = []
        row_end = len(rows)
    else:
        limit = args.get("limit", len(rows))
        page = _take(rows[offset : offset + limit], ACTIVITY_PAGE_BUDGET)
        row_end = offset + len(page)
    # State events share the page budget with the rows: never dropped, only paged.
    events = [[clock.render(e["at"]), e["kind"]] for e in state["events"]]
    events_page = _take(events[state_offset:], ACTIVITY_PAGE_BUDGET - len(encoded(page)))
    state_end = state_offset + len(events_page)
    next_cursor = None
    if row_end < len(rows) or state_end < len(events):
        next_cursor = make_cursor(
            binding,
            [
                snapshot,
                app_watermark,
                app_revision,
                screen_watermark,
                screen_revision,
                row_end,
                state["watermark"],
                state_end,
            ],
        )
    result: dict[str, Any] = {
        "date": clock.date.isoformat(),
        "utc_offset": clock.offset_text(),
        "store": store,
        "apps": apps,
        "totals": totals,
        "columns": list(COLUMNS),
        "rows": page,
        "count": len(page),
        "total": len(rows),
        "offset": offset,
        "state_at_start": [[clock.render(e["at"]), e["kind"]] for e in state["at_start"]],
        "state_events": events_page,
        "state_events_total": len(events),
        "coverage": coverage,
        "state_coverage": state["coverage"],
        "time_basis": "source_specific" if uses_timesink else "observed_at",
        "snapshot": snapshot,
        "timesink_snapshot": app_watermark,
        "next_cursor": next_cursor,
    }
    if not args.get("cursor"):
        result["notes"] = list(_NOTES)
    return result


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


def _paged_text(identity: str, text: str, args: dict[str, Any], binding: str) -> dict[str, Any]:
    [offset] = cursor_position(args.get("cursor"), binding, [0])
    chunk, end = text_chunk(text, offset)
    return {
        "content": chunk,
        "offset": offset,
        "total_chars": len(text),
        "complete": end == len(text),
        "next_cursor": make_cursor(binding, [end]) if end < len(text) else None,
        "id": identity,
    }


def read_activity(
    conn: sqlite3.Connection,
    args: dict[str, Any],
    timesink_path: Path | None = None,
) -> dict[str, Any]:
    """Read only a saved observation, never capture the current screen or diff.

    Accepts the compact row references of query_activity (``s<id>:<rev>``, ``c<id>:<rev>``,
    ``g<uid>``) as well as the full ``timesink:`` / ``timesink-capture:`` / ``activity:``
    forms; the result's ``source_refs`` always carries the full reference to cite.
    """
    identity = args["activity_id"]
    parsed = timesink.parse_reference(identity)
    if parsed is not None and parsed[0] == "c":
        row = timesink.read_capture(timesink_path, identity)
        text = str(row["text"] or "")
        return {
            **_paged_text(identity, text, args, identity),
            "source_refs": [row["sourceRef"]],
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
            "content_format": "text",
        }
    if parsed is not None:
        row = timesink.read_span(timesink_path, identity)
        source_ref = str(row.pop("sourceRef"))
        return {
            **_paged_text(identity, encoded(row), args, identity),
            "source_refs": [source_ref],
            "kind": "app.usage_span",
            "observed_at": None,
            "stored_timestamp_timezone": "UTC",
            "content_format": "json",
        }
    if identity.startswith(("git:", "codex-session:")):
        # ADR 0025: a daily report cites commits and Codex sessions; the original behind
        # either is read from where it lives, paged like every other saved observation.
        text, kind = _report_source(identity)
        return {
            **_paged_text(identity, text, args, identity),
            "source_refs": [identity],
            "kind": kind,
            "observed_at": None,
            "content_format": "text",
        }
    git_match = _GIT_ROW_REF.fullmatch(identity)
    if git_match is not None:
        uid = git_match[1]
    elif identity.startswith("activity:"):
        uid = identity.removeprefix("activity:")
    else:
        msg = "Use a row reference returned by query_activity"
        raise DailyError(msg)
    row = conn.execute(
        "SELECT type,ts_epoch_ms,payload_json FROM events WHERE event_uid=? AND type IN (?,?)",
        (uid, *_ACTIVITY_TYPES),
    ).fetchone()
    if row is None:
        msg = "Saved activity does not exist"
        raise DailyError(msg, "not_found")
    return {
        **_paged_text(identity, row[2], args, fingerprint([identity, row])),
        "source_refs": [f"event:{uid}"],
        "kind": row[0],
        "observed_at": _iso(row[1]),
        "content_format": "json",
    }
