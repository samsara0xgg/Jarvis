"""Lossless history retrieval without changing the conversation store's schema."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING, Any

from jarvis.state.daily_contract import (
    DailyError,
    cursor_position,
    fingerprint,
    make_cursor,
    page_rows,
    text_chunk,
    timestamp,
    window,
)

if TYPE_CHECKING:
    from pathlib import Path


def read_connection(path: Path | None) -> sqlite3.Connection:
    """Open only an existing memory store; a lookup must not create one."""
    if path is None or not path.is_file():
        msg = "Conversation store is unavailable"
        raise DailyError(msg, "source_unavailable")
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def search_records(path: Path | None, args: dict[str, Any]) -> dict[str, Any]:
    """Page timestamped, identified excerpts at one append watermark."""
    start, end = window(args)
    binding = fingerprint(
        ["records", _identity(path), {k: v for k, v in args.items() if k != "cursor"}]
    )
    with closing(read_connection(path)) as conn:
        high = int(conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM records").fetchone()[0])
        snapshot, offset = cursor_position(args.get("cursor"), binding, [high, 0])
        rows = conn.execute(
            "SELECT id,ts,source,text FROM records WHERE rowid <= ? ORDER BY rowid DESC",
            (snapshot,),
        ).fetchall()
    found = []
    keyword = args.get("keyword", "").casefold()
    for record_id, ts, source, text in rows:
        moment = timestamp(ts)
        if (start and moment < start) or (end and moment >= end) or keyword not in text.casefold():
            continue
        found.append(
            {
                "id": record_id,
                "ts": ts,
                "source": source,
                "excerpt": text[:300],
                "text_chars": len(text),
                "source_ref": f"record:{record_id}",
            }
        )
    return page_rows(found, args, binding, snapshot, offset, key="records")


def read_records(path: Path | None, args: dict[str, Any]) -> dict[str, Any]:
    """Read exact text in chunks, preserving order, missing IDs and resumability."""
    ids = args["record_ids"]
    binding = fingerprint(["record-detail", _identity(path), ids])
    with closing(read_connection(path)) as conn:
        high = int(conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM records").fetchone()[0])
        snapshot, index, offset = cursor_position(args.get("cursor"), binding, [high, 0, 0])
        if index >= len(ids):
            message = "Record cursor out of range"
            raise DailyError(message, "invalid_cursor")
        rows = [
            conn.execute(
                "SELECT id,ts,source,text FROM records WHERE id=? AND rowid<=?", (rid, snapshot)
            ).fetchone()
            for rid in ids
        ]
    missing = [rid for rid, row in zip(ids, rows, strict=True) if row is None]
    while index < len(rows) and rows[index] is None:
        index += 1
        offset = 0
    if index == len(rows):
        return {"records": [], "missing_ids": missing, "next_cursor": None}
    record_id, ts, source, text = rows[index]
    if offset > len(text):
        msg = "Text cursor out of range"
        raise DailyError(msg, "invalid_cursor")
    chunk, end = text_chunk(text, offset)
    position = [index, end] if end < len(text) else [index + 1, 0]
    while position[0] < len(rows) and rows[position[0]] is None:
        position[0] += 1
    return {
        "records": [
            {
                "id": record_id,
                "ts": ts,
                "source": source,
                "text": chunk,
                "offset": offset,
                "total_chars": len(text),
                "complete": end == len(text),
            }
        ],
        "missing_ids": missing,
        "next_cursor": make_cursor(binding, [snapshot, *position])
        if position[0] < len(rows)
        else None,
    }


def _identity(path: Path | None) -> str:
    if path is None or not path.is_file():
        message = "Conversation store is unavailable"
        raise DailyError(message, "source_unavailable")
    stat = path.stat()
    return f"{path.resolve()}:{stat.st_dev}:{stat.st_ino}"
