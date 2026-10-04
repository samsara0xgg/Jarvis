"""L2 memory page: what the Dashboard's memory page reads and the writes it makes (ADR 0154).

Reads fold ``core_memory`` versions, ``day_summaries`` and ``records`` into the small JSON
the page draws, with sources resolved to the words that were said. Every write the user
makes is one new ``core_memory`` version of origin ``user`` through the same transaction
the nightly pass and ``remember`` use; a day-summary edit appends a ``day_summaries`` row.
Nothing here edits a row in place.

Errors: ``LookupError`` is a missing item, version or day, ``ValueError`` a refused input,
:class:`Conflict` an undo that a later version blocks.
"""

from __future__ import annotations

import json
import re
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Final

from jarvis.state import core_memory, memory_db
from jarvis.state.memory_db import append_day_summary, iso_seconds, local_now, open_memory_db

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from pathlib import Path

ITEM_CHARS: Final[int] = 400
CAP_RANGE: Final[tuple[int, int]] = (1000, 20000)
_LINE_CHARS: Final[int] = 80
_VERSION_LIMIT: Final[int] = 60
_VERSION_LINES: Final[int] = 6
_QUOTE_CHARS: Final[int] = 90
_SOURCE_CHARS: Final[int] = 400
_HIT_LIMIT: Final[int] = 200
_HIT_CHARS: Final[int] = 160
_QUERY_WORDS: Final[int] = 6
_DAY_PAGE: Final[int] = 300
_DAY_TEXT: Final[int] = 600
_CONVERSATION: Final[tuple[str, ...]] = ("allen", "jarvis", "jarvis_live")
_MARKER = re.compile(r"\s*\[record_id=[^\]]*\]")
# The headings a day summary carries, in the order they are written (decision.day_summary).
_HEADINGS: Final[tuple[tuple[str, str], ...]] = (
    ("topics", "### Topics"),
    ("decisions", "### Decisions and facts the user stated"),
    ("unfinished", "### Unfinished"),
)
_EMPTY: Final[str] = "none"
_DAY_LINES: Final[int] = 40
_CARD_LINES: Final[int] = 4
_DAY_EDIT_CHARS: Final[int] = 6000


class Conflict(Exception):  # noqa: N818 — reads as the fact: the request conflicts.
    """An undo that a later version blocks; the message says which items."""


@dataclass(frozen=True)
class Row:
    """One ``core_memory`` row."""

    id: str
    ts: str
    origin: str
    upto_day: str | None
    doc: core_memory.Doc
    changes: list[dict[str, Any]]
    chars: int


def _rows(conn: sqlite3.Connection) -> list[Row]:
    """Every version, oldest first; the first read of a store migrates ``profile``."""
    core_memory.current(conn, iso_seconds(local_now()))
    return [
        Row(
            str(rid),
            str(ts),
            str(origin),
            upto,
            core_memory.load(doc),
            json.loads(changes),
            int(chars),
        )
        for rid, ts, origin, upto, doc, changes, chars in conn.execute(
            "SELECT id, ts, origin, upto_day, doc, changes, chars FROM core_memory ORDER BY rowid",
        )
    ]


def _base(rows: Sequence[Row], index: int) -> core_memory.Doc | None:
    return rows[index - 1].doc if index else None


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _who(source: str) -> str:
    return "user" if source == "allen" else "jarvis" if source in _CONVERSATION else "other"


def _local_day(ts: str) -> str:
    return datetime.fromisoformat(ts).astimezone().date().isoformat()


def _resolve(conn: sqlite3.Connection, record_ids: Iterable[str]) -> list[dict[str, Any]]:
    """The records behind ``record_ids``, in that order; a missing record is left out."""
    ids = list(dict.fromkeys(record_ids))
    if not ids:
        return []
    marks = ", ".join("?" * len(ids))
    found = {
        str(rid): (str(ts), str(source), str(text))
        for rid, ts, source, text in conn.execute(
            f"SELECT id, ts, source, text FROM records WHERE id IN ({marks})",  # noqa: S608 — marks only.
            ids,
        )
    }
    return [
        {
            "id": rid,
            "who": _who(found[rid][1]),
            "ts": found[rid][0],
            "day": _local_day(found[rid][0]),
            "text": _cut(found[rid][2], _SOURCE_CHARS),
        }
        for rid in ids
        if rid in found
    ]


def _lines(rows: Sequence[Row], index: int) -> list[dict[str, str]]:
    """The version's items as 新/改/过时 lines: ``add`` | ``chg`` | ``old`` with the text."""
    base = _base(rows, index)
    delta = core_memory.delta(base, rows[index].doc)
    new = {item_id: item for _s, item, item_id in core_memory.listing(rows[index].doc)}
    old = {
        item_id: item for _s, item, item_id in core_memory.listing(base or core_memory.empty_doc())
    }
    return [
        *(
            {"tag": "add", "text": _cut(str(new[i]["text"]), _LINE_CHARS)}
            for i in sorted(delta.added)
        ),
        *(
            {"tag": "chg", "text": _cut(str(new[i]["text"]), _LINE_CHARS)}
            for i in sorted(delta.changed)
        ),
        *(
            {"tag": "old", "text": _cut(str(old[i]["text"]), _LINE_CHARS)}
            for i in sorted(delta.removed)
        ),
    ]


def _confirmed_after(rows: Sequence[Row], index: int) -> set[str]:
    """Ids the user confirmed or changed in any version after ``rows[index]``."""
    seen: set[str] = set()
    for later in range(index + 1, len(rows)):
        seen |= core_memory.delta(rows[later - 1].doc, rows[later].doc).touched
        for change in rows[later].changes:
            if change.get("op") == "confirm":
                seen.add(str(change["id"]))
            elif change.get("op") == "unconfirm":
                seen.discard(str(change["id"]))
    return seen


_NIGHTLY_OPS: Final[frozenset[str]] = frozenset({"add", "rewrite", "stale", "suggest_stale"})


def _new_entries(conn: sqlite3.Connection, rows: Sequence[Row]) -> dict[str, Any]:
    """What the latest nightly version that changed anything added, rewrote or marked stale.

    An entry the user has since confirmed, edited, deleted or kept is not listed.
    """
    index = next(
        (
            i
            for i in range(len(rows) - 1, -1, -1)
            if rows[i].origin == "nightly"
            and any(c.get("op") in _NIGHTLY_OPS for c in rows[i].changes)
        ),
        None,
    )
    if index is None:
        return {"day": None, "ts": None, "version": None, "entries": []}
    row, base = rows[index], rows[index - 1].doc if index else core_memory.empty_doc()
    done = _confirmed_after(rows, index)
    before = core_memory.listing(base)
    after = {item_id: (section, item) for section, item, item_id in core_memory.listing(row.doc)}
    entries: list[dict[str, Any]] = []
    for change in row.changes:
        op = change.get("op")
        if op not in _NIGHTLY_OPS:
            continue
        entry: dict[str, Any] | None = None
        if op == "add":
            hit = next(
                (
                    (item_id, section)
                    for item_id, (section, item) in after.items()
                    if item["text"] == change["text"]
                ),
                None,
            )
            entry = (
                None
                if hit is None
                else {"kind": "add", "id": hit[0], "section": hit[1], "text": change["text"]}
            )
        elif op == "rewrite":
            section, old, item_id = before[change["item"] - 1]
            if item_id in after:
                entry = {
                    "kind": "rewrite",
                    "id": item_id,
                    "section": after[item_id][0],
                    "text": change["text"],
                    "before": old["text"],
                }
        elif op == "stale":
            section, old, item_id = before[change["item"] - 1]
            entry = {"kind": "stale", "id": item_id, "section": section, "text": old["text"]}
        else:
            entry = {
                "kind": "suggest_stale",
                "id": change["id"],
                "section": change["section"],
                "text": change["text"],
            }
        if entry is None or entry["id"] in done:
            continue
        entry["sources"] = list(change.get("sources", []))
        entries.append(entry)
    quotes = {
        record["id"]: record
        for record in _resolve(conn, (rid for entry in entries for rid in entry["sources"][:1]))
    }
    for entry in entries:
        first = quotes.get(entry["sources"][0]) if entry["sources"] else None
        entry["quote"] = (
            None
            if first is None
            else {"who": first["who"], "text": _cut(first["text"], _QUOTE_CHARS)}
        )
        del entry["sources"]
    return {"day": row.upto_day, "ts": row.ts, "version": row.id, "entries": entries}


def overview(path: Path, *, max_chars: int, booted_max_chars: int) -> dict[str, Any]:
    """The 记着的 screen: the six sections, last night's changes, and the counts the tabs show."""
    with closing(open_memory_db(path)) as conn:
        rows = _rows(conn)
        days = int(conn.execute("SELECT COUNT(DISTINCT day) FROM day_summaries").fetchone()[0])
        new = _new_entries(conn, rows)
    current = rows[-1]
    pins = {
        item_id: bool(item.get("pinned")) for _s, item, item_id in core_memory.listing(current.doc)
    }
    sections = [
        {
            "name": section,
            "items": [
                {"id": item_id, "text": item["text"], "pinned": pins[item_id]}
                for sec, item, item_id in core_memory.listing(current.doc)
                if sec == section
            ],
        }
        for section in core_memory.SECTIONS
    ]
    return {
        "version": {"id": current.id, "ts": current.ts, "origin": current.origin},
        "items": core_memory.item_count(current.doc),
        "days": days,
        "chars": current.chars,
        "max_chars": max_chars,
        "booted_max_chars": booted_max_chars,
        "sections": sections,
        "new": new,
    }


def item(path: Path, item_id: str) -> dict[str, Any]:
    """One item whole: its sources as words, who wrote it and which versions rewrote it."""
    with closing(open_memory_db(path)) as conn:
        rows = _rows(conn)
        current = rows[-1]
        found = core_memory.find(current.doc, item_id)
        if found is None:
            msg = f"no item {item_id}"
            raise LookupError(msg)
        section, _index, entry = found
        sources = _resolve(conn, entry.get("sources", []))
        reminder = any(
            e["id"] == item_id and e["kind"] == "suggest_stale"
            for e in _new_entries(conn, rows)["entries"]
        )
    born: dict[str, Any] | None = None
    edits: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        delta = core_memory.delta(_base(rows, index), row.doc)
        when = {"ts": row.ts, "origin": row.origin, "day": row.upto_day or _local_day(row.ts)}
        if item_id in delta.added:
            born, edits = when, []
        elif item_id in delta.changed:
            edits.append(when)
    flat = [i for _s, _i, i in core_memory.listing(current.doc)].index(item_id) + 1
    return {
        "id": item_id,
        "section": section,
        "text": entry["text"],
        "pinned": bool(entry.get("pinned")),
        "since": entry.get("since"),
        "number": flat,
        "chars": len(str(entry["text"])),
        "sources": sources,
        "born": born,
        "edits": edits[-5:],
        "edit_count": len(edits),
        "reminder": reminder,
    }


def _write(
    path: Path,
    act: Callable[
        [sqlite3.Connection, Row, core_memory.Doc], tuple[core_memory.Doc, list[dict[str, Any]]]
    ],
    *,
    max_chars: int,
) -> dict[str, Any]:
    """One user version: ``act(conn, current row, current doc)`` gives the new document and changes.

    A write that would leave the note over ``max_chars`` is refused unless it makes it no longer.
    """
    now = local_now()
    with closing(open_memory_db(path)) as conn, core_memory.write_transaction(conn):
        rows = _rows(conn)
        current = rows[-1]
        doc, changes = act(conn, current, current.doc)
        size = len(core_memory.render(doc))
        if size > max_chars and size > current.chars:
            msg = f"core memory would be {size} characters, over the {max_chars} cap"
            raise ValueError(msg)
        base = core_memory.Version(current.id, current.upto_day, current.doc)
        version = core_memory.append_version(
            conn,
            base=base,
            doc=doc,
            origin="user",
            upto_day=current.upto_day,
            changes=changes,
            now=iso_seconds(now),
        )
    return {"ok": True, "version": version}


def edit_item(
    path: Path, item_id: str, text: str, section: str | None, *, max_chars: int
) -> dict[str, Any]:
    """Set an item's words and section; it is pinned, so the night leaves it alone."""
    line = " ".join(text.split())
    if not line:
        msg = "the item has no text"
        raise ValueError(msg)
    if len(line) > ITEM_CHARS:
        msg = f"an item is at most {ITEM_CHARS} characters"
        raise ValueError(msg)
    if section is not None and section not in core_memory.SECTIONS:
        msg = f"section {section!r} is not one of the six"
        raise ValueError(msg)
    day = local_now().date().isoformat()

    def act(
        _conn: sqlite3.Connection, _row: Row, doc: core_memory.Doc
    ) -> tuple[core_memory.Doc, list[dict[str, Any]]]:
        found = core_memory.find(doc, item_id)
        if found is None:
            msg = f"no item {item_id}"
            raise LookupError(msg)
        new, change = core_memory.edit(
            doc, item_id, text=line, section=section or found[0], day=day
        )
        return new, [change]

    return _write(path, act, max_chars=max_chars)


def delete_item(path: Path, item_id: str, *, max_chars: int) -> dict[str, Any]:
    """Remove an item; the version keeps it, so undoing that version brings it back."""

    def act(
        _conn: sqlite3.Connection, _row: Row, doc: core_memory.Doc
    ) -> tuple[core_memory.Doc, list[dict[str, Any]]]:
        new, change = core_memory.delete(doc, item_id)
        return new, [change]

    return _write(path, act, max_chars=max_chars)


def confirm_item(path: Path, item_id: str, *, max_chars: int) -> dict[str, Any]:
    """Say an item is right as it is: a version with no document change that hides it from new."""

    def act(
        _conn: sqlite3.Connection, _row: Row, doc: core_memory.Doc
    ) -> tuple[core_memory.Doc, list[dict[str, Any]]]:
        new, change = core_memory.confirm(doc, item_id)
        return new, [change]

    return _write(path, act, max_chars=max_chars)


def keep_stale(path: Path, version_id: str, item_id: str, *, max_chars: int) -> dict[str, Any]:
    """Put back an item the nightly ``version_id`` marked stale, pinned (留着)."""

    def act(
        conn: sqlite3.Connection, _row: Row, doc: core_memory.Doc
    ) -> tuple[core_memory.Doc, list[dict[str, Any]]]:
        rows = _rows(conn)
        index = next((i for i, row in enumerate(rows) if row.id == version_id), None)
        if index is None or index == 0:
            msg = f"no version {version_id}"
            raise LookupError(msg)
        old = next(
            (
                (s, i)
                for s, i, found in core_memory.listing(rows[index - 1].doc)
                if found == item_id
            ),
            None,
        )
        if old is None:
            msg = f"version {version_id} did not hold item {item_id}"
            raise LookupError(msg)
        new, change = core_memory.restore(doc, {**old[1], "id": item_id}, old[0])
        return new, [change]

    return _write(path, act, max_chars=max_chars)


def _kind(row: Row) -> str:
    ops = {str(c.get("op")) for c in row.changes}
    if row.origin != "user":
        return row.origin
    if ops & {"undo", "unconfirm"}:
        return "undo"
    if ops == {"confirm"}:
        return "confirm"
    return "user"


def versions(path: Path) -> dict[str, Any]:
    """The 改动 screen: one entry per version, newest first, with the lines it changed."""
    with closing(open_memory_db(path)) as conn:
        rows = _rows(conn)
    out: list[dict[str, Any]] = []
    for index in range(len(rows) - 1, max(len(rows) - 1 - _VERSION_LIMIT, -1), -1):
        row = rows[index]
        lines = _lines(rows, index)
        kind = _kind(row)
        edited = sum(
            1
            for c in row.changes
            if c.get("op") == "edit" and c["text"] != c["before"]["item"]["text"]
        )
        moved = sum(
            1
            for c in row.changes
            if c.get("op") == "edit" and c["section"] != c["before"]["section"]
        )
        out.append(
            {
                "id": row.id,
                "ts": row.ts,
                "origin": row.origin,
                "kind": kind,
                "day": row.upto_day if row.origin == "nightly" else None,
                "current": index == len(rows) - 1,
                "undoable": (bool(lines) or kind == "confirm") and row.origin != "migration",
                "chars": row.chars,
                "edited": edited,
                "moved": moved,
                "counts": {
                    tag: sum(1 for line in lines if line["tag"] == tag)
                    for tag in ("add", "chg", "old")
                },
                "lines": lines[:_VERSION_LINES],
                "more": max(0, len(lines) - _VERSION_LINES),
            },
        )
    return {"versions": out, "total": len(rows)}


def undo(path: Path, version_id: str, *, max_chars: int) -> dict[str, Any]:
    """Take back what a version changed, as a new version; refuse if a later one touched it.

    The nightly ``upto_day`` stays: undoing a night does not make the night run that day again.
    """

    def act(
        conn: sqlite3.Connection, _current: Row, doc: core_memory.Doc
    ) -> tuple[core_memory.Doc, list[dict[str, Any]]]:
        rows = _rows(conn)
        index = next((i for i, row in enumerate(rows) if row.id == version_id), None)
        if index is None:
            msg = f"no version {version_id}"
            raise LookupError(msg)
        if index == 0:
            msg = "the first version is where memory started; there is nothing before it"
            raise ValueError(msg)
        confirmed = [c for c in rows[index].changes if c.get("op") == "confirm"]
        if confirmed and len(confirmed) == len(rows[index].changes):
            # A confirm changes no text: taking it back puts its items on the new list again.
            return doc, [{"op": "unconfirm", "id": c["id"]} for c in confirmed]
        mine = core_memory.delta(rows[index - 1].doc, rows[index].doc).touched
        if not mine:
            msg = "this version changed no item, so there is nothing to take back"
            raise ValueError(msg)
        # A later version blocks the undo only if the item is not as this version left it now.
        left, now = core_memory.states(rows[index].doc), core_memory.states(doc)
        clash = {item_id for item_id in mine if left.get(item_id) != now.get(item_id)}
        if clash:
            names = (
                ", ".join(
                    f"“{_cut(str(item['text']), 24)}”"
                    for _s, item, item_id in core_memory.listing(doc)
                    if item_id in clash
                )
                or "an item that is gone now"
            )
            msg = f"a later change touched the same item ({names}); take that back first"
            raise Conflict(msg)
        return core_memory.undo(doc, rows[index - 1].doc, rows[index].doc), [
            {"op": "undo", "of": version_id}
        ]

    return _write(path, act, max_chars=max_chars)


def _by_ts(hit: dict[str, Any]) -> str:
    return str(hit["ts"])


def search(
    path: Path,
    *,
    query: str,
    who: str,
    since: str,
    limit: int = _HIT_LIMIT,
) -> dict[str, Any]:
    """Every conversation record with any of the words, grouped by local day, newest first."""
    words = list(dict.fromkeys(query.casefold().split()))[:_QUERY_WORDS]
    if not words:
        return {"words": [], "days": [], "total": 0, "truncated": False, "since": since}
    sources = {"user": ("allen",), "jarvis": ("jarvis", "jarvis_live")}.get(who, _CONVERSATION)
    like = " OR ".join("text LIKE ? ESCAPE '\\'" for _ in words)
    params: list[Any] = [*sources]
    params += [
        "%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for w in words
    ]
    clause = f"source IN ({', '.join('?' * len(sources))}) AND ({like})"
    if since:
        clause += " AND datetime(ts) >= datetime(?)"
        params.append(since)
    with closing(open_memory_db(path)) as conn:
        # ponytail: LIKE full scan, as memory_db.search_records; FTS5 past ~100k rows.
        found = conn.execute(
            f"SELECT id, ts, source, text FROM records WHERE {clause} ORDER BY rowid DESC LIMIT ?",  # noqa: S608 — marks and fixed literals; every value is bound.
            [*params, limit + 1],
        ).fetchall()
    truncated = len(found) > limit
    by_day: dict[str, list[dict[str, Any]]] = {}
    for rid, ts, source, text in found[:limit]:
        lowered = str(text).casefold()
        at = min((lowered.find(w) for w in words if w in lowered), default=0)
        start = max(0, at - _HIT_CHARS // 3)
        excerpt = (
            ("…" if start else "")
            + str(text)[start : start + _HIT_CHARS]
            + ("…" if start + _HIT_CHARS < len(text) else "")
        )
        by_day.setdefault(_local_day(ts), []).append(
            {"id": rid, "ts": ts, "who": _who(source), "text": excerpt},
        )
    days: list[dict[str, Any]] = [
        {"day": day, "hits": sorted(by_day[day], key=_by_ts)}
        for day in sorted(by_day, reverse=True)
    ]
    return {
        "words": words,
        "days": days,
        "total": len(found[:limit]),
        "truncated": truncated,
        "since": since,
    }


def day_records(
    path: Path,
    day: str,
    *,
    around: str | None = None,
    offset: int = 0,
    limit: int = 120,
) -> dict[str, Any]:
    """One local day of the conversation, oldest first, a page at a time.

    ``around`` (a record id) starts the page a little before that record.
    """
    date.fromisoformat(day)  # ValueError: not a day
    limit = max(1, min(limit, _DAY_PAGE))
    rows = [row for row in memory_db.day_records(path, day) if row[2] in _CONVERSATION]
    if around is not None:
        offset = max(0, next((i for i, row in enumerate(rows) if row[0] == around), 0) - 8)
    offset = max(0, min(offset, len(rows)))
    page = rows[offset : offset + limit]
    return {
        "day": day,
        "total": len(rows),
        "offset": offset,
        "records": [
            {"id": rid, "ts": ts, "who": _who(source), "text": _cut(text, _DAY_TEXT)}
            for rid, ts, source, text in page
        ],
    }


def _latest_days(conn: sqlite3.Connection) -> dict[str, tuple[str, str, str | None, int, int]]:
    """``{day: (ts, summary, model, record_count, input_chars)}`` of each day's current row."""
    rows = conn.execute(
        "SELECT day, ts, summary, model, record_count, input_chars "
        "FROM day_summaries ORDER BY day, rowid",
    )
    return {
        str(day): (str(ts), str(summary), model, int(count), int(chars))
        for day, ts, summary, model, count, chars in rows
    }


def parse_summary(summary: str) -> dict[str, list[str]]:
    """The bullets under each of a day summary's headings, record markers removed.

    A lone ``none`` is no bullet. Text before the first heading is ignored.
    """
    out: dict[str, list[str]] = {key: [] for key, _ in _HEADINGS}
    heading = {title: key for key, title in _HEADINGS}
    key: str | None = None
    for raw in summary.splitlines():
        line = raw.strip()
        if line in heading:
            key = heading[line]
        elif line.startswith("#"):
            key = None if line.startswith("## ") else key
        elif key is not None and line:
            bullet = _MARKER.sub("", line.removeprefix("- ")).strip()
            if bullet and bullet.casefold() != _EMPTY:
                out[key].append(bullet)
    return out


def day_summaries(path: Path) -> dict[str, Any]:
    """The 日摘要 screen: one small card per day, newest first."""
    with closing(open_memory_db(path)) as conn:
        latest = _latest_days(conn)
    cards = []
    for day in sorted(latest, reverse=True):
        ts, summary, model, count, _chars = latest[day]
        kind = "verbatim" if model == "verbatim" else "user" if model == "user" else "model"
        if kind == "verbatim":
            spoken = [_cut(line.split(": ", 1)[-1], 24) for line in summary.splitlines()[1:]]
            lines = spoken[:_CARD_LINES]
        else:
            lines = [_cut(b, 120) for b in parse_summary(summary)["topics"][:_CARD_LINES]]
        cards.append({"day": day, "ts": ts, "kind": kind, "records": count, "lines": lines})
    return {"days": cards}


def day_summary(path: Path, day: str) -> dict[str, Any]:
    """One day's summary as its headings; a verbatim day carries its lines instead."""
    date.fromisoformat(day)
    with closing(open_memory_db(path)) as conn:
        found = _latest_days(conn).get(day)
    if found is None:
        msg = f"no summary for {day}"
        raise LookupError(msg)
    ts, summary, model, count, _chars = found
    kind = "verbatim" if model == "verbatim" else "user" if model == "user" else "model"
    base: dict[str, Any] = {"day": day, "ts": ts, "kind": kind, "records": count}
    if kind == "verbatim":
        return {**base, "lines": summary.splitlines()[1:]}
    return {**base, "sections": parse_summary(summary)}


def edit_day_summary(path: Path, day: str, sections: Mapping[str, Sequence[str]]) -> dict[str, Any]:
    """Store the user's version of a day's summary as its newest row.

    The day then has a summary row, so the nightly job never writes it again, and readers
    (``recall``, the history) take the newest row. A line the user left as it was keeps its
    original ``[record_id=..]`` marker.
    """
    date.fromisoformat(day)
    with closing(open_memory_db(path)) as conn:
        found = _latest_days(conn).get(day)
    if found is None:
        msg = f"no summary for {day}"
        raise LookupError(msg)
    _ts, old, model, count, input_chars = found
    if model == "verbatim":
        msg = "a day kept word for word has no headings to edit"
        raise ValueError(msg)
    markers = {
        _MARKER.sub("", line.removeprefix("- ")).strip(): line.removeprefix("- ")
        for line in old.splitlines()
    }
    text = [f"## {day}"]
    for key, title in _HEADINGS:
        lines = [" ".join(str(line).split()) for line in sections.get(key, ())]
        lines = [line for line in lines if line]
        if len(lines) > _DAY_LINES:
            msg = f"a heading holds at most {_DAY_LINES} lines"
            raise ValueError(msg)
        text.extend(
            [title, *(f"- {markers.get(line, line)}" for line in lines)]
            if lines
            else [title, _EMPTY]
        )
    summary = "\n".join(text)
    if len(summary) > _DAY_EDIT_CHARS:
        msg = f"a day summary is at most {_DAY_EDIT_CHARS} characters"
        raise ValueError(msg)
    append_day_summary(
        path,
        day=day,
        summary=summary,
        model="user",
        record_count=count,
        input_chars=input_chars,
        output_chars=len(summary),
    )
    return {"ok": True}


__all__ = [
    "CAP_RANGE",
    "ITEM_CHARS",
    "Conflict",
    "confirm_item",
    "day_records",
    "day_summaries",
    "day_summary",
    "delete_item",
    "edit_day_summary",
    "edit_item",
    "item",
    "keep_stale",
    "overview",
    "parse_summary",
    "search",
    "undo",
    "versions",
]
