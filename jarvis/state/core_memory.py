"""L2 core memory: the append-only versioned note about the user (ADR 0145).

One ``core_memory`` row is one version of a document with six fixed sections; the
latest row is current and every older row stays as history. This module owns the
document (render, number, apply typed changes) and the row reads and writes on an
open connection. ``memory_db`` wraps them with a path and the clock.

The first version is migrated from the legacy ``profile`` table the first time anything
reads core memory; ``profile`` is never read for prompts after that.
"""

from __future__ import annotations

import copy
import json
import uuid
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Final, NamedTuple

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator, Mapping, Sequence

SECTIONS: Final[tuple[str, ...]] = (
    "关于你",
    "偏好",
    "常提到的人",
    "正在做的事",
    "定下来的规矩",
    "承诺和待办",
)
DEFAULT_SECTION: Final[str] = SECTIONS[0]
NAME_TOPIC: Final[str] = "name"
_NAME_ROW: Final[str] = "profile-name"  # the legacy profile row first-run setup wrote
_FACT_PREFIX: Final[str] = "fact:"

# One item: {"text", "topic", "sources", "since"}. A document maps each section to its items.
Item = dict[str, Any]
Doc = dict[str, list[Item]]


class Version(NamedTuple):
    """One ``core_memory`` row."""

    id: str
    upto_day: str | None  # the last local day a nightly run consolidated
    doc: Doc


def empty_doc() -> Doc:
    """A document with the six sections and no items."""
    return {section: [] for section in SECTIONS}


def render(doc: Doc) -> str:
    """The prompt text: ``### <section>`` then ``- <text>`` lines; empty sections omitted."""
    lines: list[str] = []
    for section in SECTIONS:
        if doc[section]:
            lines.append(f"### {section}")
            lines.extend(f"- {item['text']}" for item in doc[section])
    return "\n".join(lines)


def numbered(doc: Doc) -> str:
    """The consolidation input: items numbered ``[N]`` 1..N in section order."""
    lines: list[str] = []
    number = 0
    for section in SECTIONS:
        if doc[section]:
            lines.append(f"### {section}")
            for item in doc[section]:
                number += 1
                lines.append(f"[{number}] {item['text']}")
    return "\n".join(lines) or "(empty)"


def item_count(doc: Doc) -> int:
    """How many items the document has (the highest valid ``[N]``)."""
    return sum(len(doc[section]) for section in SECTIONS)


def _new_item(text: str, topic: str | None, sources: Sequence[str], since: str) -> Item:
    return {"text": text, "topic": topic, "sources": list(sources), "since": since}


def _drop(doc: Doc, item: Item) -> None:
    for section in SECTIONS:
        doc[section][:] = [other for other in doc[section] if other is not item]


def apply_changes(doc: Doc, changes: Sequence[Mapping[str, Any]], day: str) -> Doc:
    """A copy of ``doc`` with the gated nightly ``changes`` applied in order.

    Item numbers refer to ``numbered(doc)`` before any change: an add appends to its
    section, a rewrite replaces text, sources and ``since`` (the topic stays) and may move
    the item, a stale removes it.
    """
    new = copy.deepcopy(doc)
    flat = [item for section in SECTIONS for item in new[section]]
    for change in changes:
        if change["op"] == "add":
            new[change["section"]].append(_new_item(change["text"], None, change["sources"], day))
            continue
        item = flat[change["item"] - 1]
        if change["op"] == "stale":
            _drop(new, item)
            continue
        item.update(text=change["text"], sources=list(change["sources"]), since=day)
        target = change.get("section")
        if target is not None and not any(other is item for other in new[target]):
            _drop(new, item)
            new[target].append(item)
    return new


def remember(
    doc: Doc,
    *,
    topic: str,
    text: str,
    section: str | None,
    day: str,
) -> tuple[Doc, dict[str, Any]]:
    """``doc`` with ``text`` kept under ``topic``, and the change that records it.

    An item with the same topic (case-folded) in any section is replaced where it is (text
    and ``since``; its sources stay); an explicit ``section`` that differs moves it. Else the
    item is added at the end of ``section`` (default: the first section).
    """
    new = copy.deepcopy(doc)
    for current in SECTIONS:
        for item in new[current]:
            if str(item["topic"] or "").casefold() == topic.casefold():
                item.update(text=text, since=day)
                if section is not None and section != current:
                    _drop(new, item)
                    new[section].append(item)
                change = {"op": "rewrite", "topic": topic, "text": text, "sources": item["sources"]}
                return new, change
    target = section or DEFAULT_SECTION
    new[target].append(_new_item(text, topic, [], day))
    return new, {"op": "add", "section": target, "topic": topic, "text": text, "sources": []}


def _load(raw: str) -> Doc:
    sections = json.loads(raw)["sections"]
    return {section: list(sections.get(section, [])) for section in SECTIONS}


def latest(conn: sqlite3.Connection) -> Version | None:
    """The current version, or None before the first one exists."""
    row = conn.execute(
        "SELECT id, upto_day, doc FROM core_memory ORDER BY rowid DESC LIMIT 1",
    ).fetchone()
    return None if row is None else Version(str(row[0]), row[1], _load(row[2]))


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """``BEGIN IMMEDIATE`` .. ``COMMIT`` on ``conn`` so a read-modify-write cannot interleave.

    Inside a transaction already open on ``conn`` it joins that one.
    """
    if conn.in_transaction:
        yield
        return
    previous = conn.isolation_level
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
    finally:
        conn.isolation_level = previous


def append_version(  # noqa: PLR0913 — the row's columns, all required.
    conn: sqlite3.Connection,
    *,
    base: Version | None,
    doc: Doc,
    origin: str,
    upto_day: str | None,
    changes: Sequence[Mapping[str, Any]],
    now: str,
) -> str:
    """Insert one version row (inside the caller's transaction) and return its id."""
    version_id = f"core:{uuid.uuid4().hex}"
    conn.execute(
        "INSERT INTO core_memory (id, ts, base_id, origin, upto_day, doc, changes, chars) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            version_id,
            now,
            base.id if base else None,
            origin,
            upto_day,
            json.dumps({"sections": doc}, ensure_ascii=False),
            json.dumps(list(changes), ensure_ascii=False),
            len(render(doc)),
        ),
    )
    return version_id


def _migrated(conn: sqlite3.Connection) -> Doc:
    doc = empty_doc()
    for row_id, ts, text in conn.execute("SELECT id, ts, text FROM profile ORDER BY rowid"):
        if row_id == _NAME_ROW:
            topic: str | None = NAME_TOPIC
        elif row_id.startswith(_FACT_PREFIX):
            topic = row_id.removeprefix(_FACT_PREFIX)
        else:
            topic = None
        doc[DEFAULT_SECTION].append(_new_item(text, topic, [], str(ts)[:10]))
    return doc


def current(conn: sqlite3.Connection, now: str) -> Version:
    """The current version; the first read of a store migrates its ``profile`` rows into one."""
    found = latest(conn)
    if found is not None:
        return found
    with write_transaction(conn):
        found = latest(conn)
        if found is None:
            doc = _migrated(conn)
            append_version(
                conn,
                base=None,
                doc=doc,
                origin="migration",
                upto_day=None,
                changes=[],
                now=now,
            )
            found = latest(conn)
    assert found is not None  # noqa: S101 — just written
    return found


__all__ = [
    "DEFAULT_SECTION",
    "NAME_TOPIC",
    "SECTIONS",
    "Doc",
    "Item",
    "Version",
    "append_version",
    "apply_changes",
    "current",
    "empty_doc",
    "item_count",
    "latest",
    "numbered",
    "remember",
    "render",
    "write_transaction",
]
