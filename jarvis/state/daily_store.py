"""Local todos, knowledge and briefings derived from immutable revision events."""

from __future__ import annotations

import json
import subprocess
from contextlib import closing
from datetime import date, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jarvis.state import timesink
from jarvis.state.daily_contract import (
    PAGE_BUDGET,
    SCHEMAS,
    DailyError,
    cursor_position,
    encoded,
    fingerprint,
    make_cursor,
    page_rows,
    text_chunk,
    timestamp,
    validate,
)
from jarvis.state.daily_records import read_connection
from jarvis.state.event_log import append_event_in_transaction, read_log_epoch

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

EVENT_TYPES = ("todo.revised", "knowledge.revised", "briefing.revised")
_OPERATIONS = {
    "create_todo": "todo",
    "update_todo": "todo",
    "save_knowledge": "knowledge",
    "save_briefing": "briefing",
}


def high_water(conn: sqlite3.Connection) -> int:
    """Read the immutable log's append watermark."""
    return int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0])


def current_items(conn: sqlite3.Connection, kind: str, snapshot: int) -> list[dict[str, Any]]:
    """Fold at a fixed watermark before applying any mutable-field filter."""
    items: dict[str, dict[str, Any]] = {}
    for uid, raw in conn.execute(
        "SELECT event_uid,payload_json FROM events WHERE type=? AND id<=? ORDER BY id",
        (f"{kind}.revised", snapshot),
    ):
        item = dict(json.loads(raw)["item"])
        item["source_ref"] = f"event:{uid}"
        items[item["id"]] = item
    return sorted(items.values(), key=lambda item: item["id"])


def commit_exists(repo: str, sha: str) -> bool:
    """True when ``sha`` is a commit object in the repository at ``repo``."""
    if len(sha) != 40 or not all(c in "0123456789abcdef" for c in sha):  # noqa: PLR2004 — a full SHA-1.
        return False
    try:
        done = subprocess.run(  # noqa: S603 — fixed argv; `repo` came from a saved reference.
            ["git", "-C", repo, "cat-file", "-e", f"{sha}^{{commit}}"],  # noqa: S607
            capture_output=True,
            timeout=5.0,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def check_refs(
    conn: sqlite3.Connection,
    memory_path: Path | None,
    refs: list[str],
    timesink_path: Path | None = None,
) -> None:
    """Require real sources and matching external revisions, without claiming verification."""
    for ref in refs:
        prefix, separator, identity = ref.partition(":")
        if not separator or not identity:
            msg = f"Malformed source reference: {ref}"
            raise DailyError(msg, "invalid_source")
        exists = False
        if prefix == "event":
            exists = (
                conn.execute("SELECT 1 FROM events WHERE event_uid=?", (identity,)).fetchone()
                is not None
            )
        elif prefix == "record":
            with closing(read_connection(memory_path)) as memory:
                exists = (
                    memory.execute("SELECT 1 FROM records WHERE id=?", (identity,)).fetchone()
                    is not None
                )
        elif prefix == "timesink":
            timesink.read_span(timesink_path, ref)
            exists = True
        elif prefix == "timesink-capture":
            timesink.read_capture(timesink_path, ref)
            exists = True
        elif prefix == "git":
            repo, _, sha = identity.rpartition(":")
            exists = bool(repo) and commit_exists(repo, sha)
        if not exists:
            msg = f"Source does not exist: {ref}"
            raise DailyError(msg, "invalid_source")


def _brief_key(args: dict[str, Any]) -> str:
    try:
        day = date.fromisoformat(args["local_date"])
        if day.isoformat() != args["local_date"]:
            raise ValueError  # noqa: TRY301 — noncanonical dates share the parsing error below.
        ZoneInfo(args["timezone"])
    except (ValueError, ZoneInfoNotFoundError) as exc:
        msg = "Use YYYY-MM-DD and an IANA timezone"
        raise DailyError(msg) from exc
    return "briefing_" + fingerprint([args["local_date"], args["timezone"]])[:32]


def _version_check(previous: dict[str, Any] | None, args: dict[str, Any]) -> None:
    version = previous["version"] if previous else 0
    if args.get("expected_version", 0) != version:
        msg = f"Version conflict; current_version={version}. Re-read before editing."
        raise DailyError(
            msg,
            "version_conflict",
        )


def _new_item(
    kind: str, args: dict[str, Any], previous: dict[str, Any] | None, identity: str
) -> dict[str, Any]:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    item: dict[str, Any] = {
        "id": identity,
        "version": 1 if previous is None else previous["version"] + 1,
        "created_at": previous["created_at"] if previous else now,
        "updated_at": now,
    }
    if kind == "todo":
        item.update(
            {
                "title": args["title"],
                "project": args.get("project"),
                "due_at": args.get("due_at"),
                "priority": args.get("priority", "normal"),
                "status": "open",
                "source_refs": args.get("source_refs", []),
            }
        )
    elif kind == "knowledge":
        item.update({k: args[k] for k in ("statement", "kind", "source_refs", "basis")})
        item.update(
            project=args.get("project", previous.get("project") if previous else None),
            status=args.get("status", previous["status"] if previous else "active"),
            supersedes=previous["source_ref"] if previous else None,
        )
    else:
        item.update({k: args[k] for k in ("local_date", "timezone", "content", "source_refs")})
        coverage = dict.fromkeys(
            ("records", "git", "app", "screen", "agent", "todos", "knowledge"), "unknown"
        )
        coverage.update(args["coverage"])
        item.update(coverage=coverage, coverage_basis="reported_by_generator")
    return item


def write_revision(  # noqa: PLR0913 — two configured source stores and one write request.
    conn: sqlite3.Connection,
    memory_path: Path | None,
    operation: str,
    args: dict[str, Any],
    action_id: str,
    *,
    timesink_path: Path | None = None,
) -> dict[str, Any]:
    """Atomically deduplicate, check revision/references and append a new version."""
    if conn.in_transaction:
        msg = "Write requires an idle connection"
        raise DailyError(msg, "transaction_busy")
    validate(args, SCHEMAS[operation])
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = _write_locked(
            conn, memory_path, operation, args, action_id, timesink_path=timesink_path
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return result


def _write_locked(  # noqa: PLR0913 — transaction-local half of write_revision.
    conn: sqlite3.Connection,
    memory_path: Path | None,
    operation: str,
    args: dict[str, Any],
    action_id: str,
    *,
    timesink_path: Path | None = None,
) -> dict[str, Any]:
    kind = _OPERATIONS[operation]
    digest = fingerprint([operation, args])
    receipt = conn.execute(
        "SELECT event_uid,payload_json FROM events WHERE type IN (?,?,?) "
        "AND json_extract(payload_json,'$.request_id')=? ORDER BY id LIMIT 1",
        (*EVENT_TYPES, args["request_id"]),
    ).fetchone()
    if receipt:
        body = json.loads(receipt[1])
        if body["request_hash"] != digest:
            msg = "request_id was used with different arguments"
            raise DailyError(msg, "request_conflict")
        return _receipt(kind, body["item"], receipt[0])
    identity = (
        args["todo_id"]
        if operation == "update_todo"
        else args.get("knowledge_id")
        if kind == "knowledge"
        else _brief_key(args)
        if kind == "briefing"
        else None
    )
    previous = next(
        (x for x in current_items(conn, kind, high_water(conn)) if x["id"] == identity), None
    )
    if (operation == "update_todo" or "knowledge_id" in args) and previous is None:
        msg = "Entry does not exist"
        raise DailyError(msg, "not_found")
    _version_check(previous, args)
    identity = identity or f"{kind}_{uuid4().hex}"
    if operation == "update_todo":
        if previous is None:
            msg = "Todo does not exist"
            raise DailyError(msg, "not_found")
        item = {k: v for k, v in previous.items() if k != "source_ref"}
        item.update(args["patch"])
        item.update(
            version=previous["version"] + 1,
            updated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )
    else:
        item = _new_item(kind, args, previous, identity)
    if kind == "todo" and item["due_at"] is not None:
        timestamp(item["due_at"])
    # Every saved item must remain retrievable below the Tool output cap.
    metadata = {key: value for key, value in item.items() if key != "content"}
    if len(encoded(metadata)) > PAGE_BUDGET - 1000:
        message = "Entry metadata/text is too large; shorten it or use fewer sources"
        raise DailyError(message, "result_too_large")
    if operation != "update_todo":
        # Existing commitments cannot change sources; prior evidence may have disappeared.
        check_refs(conn, memory_path, item["source_refs"], timesink_path)
    running = conn.execute(
        "SELECT event_uid FROM events WHERE type='action.running' "
        "AND json_extract(payload_json,'$.action_id')=? ORDER BY id DESC LIMIT 1",
        (action_id,),
    ).fetchone()
    event = append_event_in_transaction(
        conn,
        type=f"{kind}.revised",
        payload={
            "request_id": args["request_id"],
            "request_hash": digest,
            "operation": operation,
            "item": item,
            "action_id": action_id,
        },
        source_event_id=running[0] if running else None,
        correlation={"action_id": action_id},
        actor="jarvis_llm",
    )
    return _receipt(kind, item, event.event_uid)


def _receipt(kind: str, item: dict[str, Any], uid: str) -> dict[str, Any]:
    return {f"{kind}_id": item["id"], "version": item["version"], "source_ref": f"event:{uid}"}


def search_items(conn: sqlite3.Connection, operation: str, args: dict[str, Any]) -> dict[str, Any]:
    """Page todos or knowledge from a reproducible historical revision set."""
    kind = "todo" if operation == "list_todos" else "knowledge"
    binding = fingerprint(
        [operation, _log_identity(conn), {k: v for k, v in args.items() if k != "cursor"}]
    )
    snapshot, offset = cursor_position(args.get("cursor"), binding, [high_water(conn), 0])
    items = current_items(conn, kind, snapshot)
    status = args.get("status", "open" if kind == "todo" else "active")
    before = timestamp(args["due_before"]) if "due_before" in args else None
    found = []
    for item in items:
        if status != "all" and item["status"] != status:
            continue
        if "project" in args and item.get("project") != args["project"]:
            continue
        if "kind" in args and item["kind"] != args["kind"]:
            continue
        if (
            kind == "knowledge"
            and args.get("query", "").casefold() not in item["statement"].casefold()
        ):
            continue
        if before and (item.get("due_at") is None or timestamp(item["due_at"]) >= before):
            continue
        found.append(item)
    return page_rows(found, args, binding, snapshot, offset)


def _log_identity(conn: sqlite3.Connection) -> str:
    return read_log_epoch(conn)


def get_briefing(conn: sqlite3.Connection, args: dict[str, Any]) -> dict[str, Any]:
    """Read a saved version in chunks; saving does not imply delivery."""
    identity = _brief_key(args)
    binding = fingerprint(["briefing", _log_identity(conn), identity])
    snapshot, offset = cursor_position(args.get("cursor"), binding, [high_water(conn), 0])
    item = next((x for x in current_items(conn, "briefing", snapshot) if x["id"] == identity), None)
    if item is None:
        msg = "No briefing saved for this date and timezone"
        raise DailyError(msg, "not_found")
    content = item.pop("content")
    chunk, end = text_chunk(content, offset)
    return {
        **item,
        "content": chunk,
        "offset": offset,
        "total_chars": len(content),
        "complete": end == len(content),
        "delivery_status": "not_tracked",
        "next_cursor": make_cursor(binding, [snapshot, end]) if end < len(content) else None,
    }
