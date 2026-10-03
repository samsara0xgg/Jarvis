"""Lossless history retrieval without changing the conversation store's schema."""

from __future__ import annotations

import math
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING, Any

from jarvis.state.daily_contract import (
    RECALL_PAGE_BUDGET,
    DailyError,
    cursor_position,
    day_window,
    fingerprint,
    make_cursor,
    page_rows,
    text_chunk,
    timestamp,
    window,
)

if TYPE_CHECKING:
    from pathlib import Path


# The source of Allen's own lines; Jarvis writes jarvis and jarvis_live.
_USER_SOURCE = "allen"


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
        # Newest first unless asked for the earliest: the first thing said in a month
        # took 22 pages from the newest end (live 2026-10-02).
        direction = "ASC" if args.get("order") == "oldest" else "DESC"
        rows = conn.execute(
            f"SELECT id,ts,source,text FROM records WHERE rowid <= ? ORDER BY rowid {direction}",  # noqa: S608
            (snapshot,),
        ).fetchall()
    # Any of the words: the model lists alternatives (「工作 岗位 求职」) that a single
    # substring never matched.
    words = list(dict.fromkeys(args.get("keyword", "").casefold().split()))
    speaker = args.get("speaker")
    passing = []
    for row in rows:
        moment = timestamp(row[1])
        if (start and moment < start) or (end and moment >= end):
            continue
        if speaker and (row[2] == _USER_SOURCE) != (speaker == "user"):
            continue
        passing.append(row)
    result: dict[str, Any] = {}
    if words:
        # Relevance: rare words weigh more, so a date digit shared by hundreds of rows
        # cannot bury the record that holds the rare words (live 2026-10-03).
        hits = [[word in row[3].casefold() for word in words] for row in passing]
        doc_freq = [sum(h[i] for h in hits) for i in range(len(words))]
        weight = [math.log(1 + len(passing) / df) if df else 0.0 for df in doc_freq]
        scored = [
            (sum(w for w, hit in zip(weight, h, strict=True) if hit), row)
            for h, row in zip(hits, passing, strict=True)
            if any(h)
        ]
        # Stable sort keeps the rowid order (newest or oldest first) among equal scores.
        scored.sort(key=lambda pair: -pair[0])
        passing = [row for _, row in scored]
        result["unmatched"] = [w for w, df in zip(words, doc_freq, strict=True) if not df]
    found = [
        {
            "id": record_id,
            "ts": ts,
            "source": source,
            "excerpt": text[:300],
            "text_chars": len(text),
            "source_ref": f"record:{record_id}",
        }
        for record_id, ts, source, text in passing
    ]
    return {
        **page_rows(found, args, binding, snapshot, offset, key="records"),
        "total": len(found),
        **result,
    }


# A cut reply names its id; a user line is shown in full unless one alone would not fit a page.
_ASSISTANT_CUT = 150
_USER_CUT = 3000


def recall(path: Path | None, args: dict[str, Any]) -> dict[str, Any]:
    """Compact time-ordered lines for [from, to), filling each page's budget."""
    start, end = day_window(args)
    binding = fingerprint(
        ["recall", _identity(path), {k: v for k, v in args.items() if k != "cursor"}]
    )
    with closing(read_connection(path)) as conn:
        high = int(conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM records").fetchone()[0])
        snapshot, offset = cursor_position(args.get("cursor"), binding, [high, 0])
        rows = conn.execute(
            "SELECT id,ts,source,text FROM records WHERE rowid <= ? ORDER BY rowid", (snapshot,)
        ).fetchall()
    lines = []
    for record_id, ts, source, text in rows:
        if not start <= timestamp(ts) < end:
            continue
        user = source == _USER_SOURCE
        cap = _USER_CUT if user else _ASSISTANT_CUT
        line = f"{ts[5:10]} {ts[11:16]} {'user' if user else 'assistant'}: {text[:cap]}"
        if len(text) > cap:
            line += f" …[+{len(text) - cap} chars, read_records {record_id}]"
        lines.append(line)
    page = page_rows(
        lines,
        {"limit": len(lines)},
        binding,
        snapshot,
        offset,
        key="lines",
        budget=RECALL_PAGE_BUDGET,
    )
    return {
        "from": start.isoformat(timespec="seconds"),
        "to": end.isoformat(timespec="seconds"),
        **page,
        "total": len(lines),
    }


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
