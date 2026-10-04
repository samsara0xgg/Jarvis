"""L2 core memory: the append-only versioned note about the user (ADR 0146).

One ``core_memory`` row is one version of a document with six fixed sections; the
latest row is current and every older row stays as history. This module owns the
document (render, number, apply typed changes) and the row reads and writes on an
open connection. ``memory_db`` wraps them with a path and the clock.

The first version is migrated from the legacy ``profile`` table the first time anything
reads core memory; ``profile`` is never read for prompts after that.
"""

from __future__ import annotations

import copy
import hashlib
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

# One item: {"text", "topic", "sources", "since"}, plus "id" once any write has stamped it and
# "pinned" once the user edited or moved it (ADR 0154). A document maps each section to its items.
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
    """The consolidation input: items numbered ``[N]`` 1..N in section order.

    A pinned item (ADR 0154) reads ``[N] (pinned) text``: the model is told it is fixed.
    """
    lines: list[str] = []
    number = 0
    for section in SECTIONS:
        if doc[section]:
            lines.append(f"### {section}")
            for item in doc[section]:
                number += 1
                mark = "(pinned) " if item.get("pinned") else ""
                lines.append(f"[{number}] {mark}{item['text']}")
    return "\n".join(lines) or "(empty)"


def item_count(doc: Doc) -> int:
    """How many items the document has (the highest valid ``[N]``)."""
    return sum(len(doc[section]) for section in SECTIONS)


def _new_item(text: str, topic: str | None, sources: Sequence[str], since: str) -> Item:
    return {
        "id": uuid.uuid4().hex[:12],
        "text": text,
        "topic": topic,
        "sources": list(sources),
        "since": since,
    }


def listing(doc: Doc) -> list[tuple[str, Item, str]]:
    """``(section, item, id)`` of every item in ``numbered`` order.

    An item written before ids existed has none stored: its id is a hash of its text, made
    unique within the document. :func:`stamp_ids` stores them, so a rewrite keeps the id.
    """
    seen: set[str] = set()
    out: list[tuple[str, Item, str]] = []
    for section in SECTIONS:
        for item in doc[section]:
            item_id = str(
                item.get("id") or "t" + hashlib.sha256(item["text"].encode()).hexdigest()[:10]
            )
            while item_id in seen:
                item_id += "+"
            seen.add(item_id)
            out.append((section, item, item_id))
    return out


def stamp_ids(doc: Doc) -> None:
    """Store every item's id in place (every write does this to its copy of the document)."""
    for _section, item, item_id in listing(doc):
        item["id"] = item_id


def find(doc: Doc, item_id: str) -> tuple[str, int, Item] | None:
    """``(section, index in that section, item)`` of the item with ``item_id``."""
    for section, item, found in listing(doc):
        if found == item_id:
            return section, next(i for i, other in enumerate(doc[section]) if other is item), item
    return None


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
    stamp_ids(new)
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
    stamp_ids(new)
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


def _where(doc: Doc, item_id: str) -> tuple[str, int, Item]:
    found = find(doc, item_id)
    if found is None:
        msg = f"no item {item_id}"
        raise LookupError(msg)
    return found


def _snapshot(section: str, index: int, item: Item) -> dict[str, Any]:
    return {"section": section, "index": index, "item": copy.deepcopy(item)}


def edit(
    doc: Doc, item_id: str, *, text: str, section: str, day: str
) -> tuple[Doc, dict[str, Any]]:
    """``doc`` with the user's ``text`` and ``section`` on an item, pinned (ADR 0154).

    The item stays where it is when its section does not change, else it goes to the end of
    the new one. Its sources stay: they are what the item was first drawn from.
    """
    new = copy.deepcopy(doc)
    stamp_ids(new)
    old_section, index, item = _where(new, item_id)
    before = _snapshot(old_section, index, item)
    item.update(text=text, since=day, pinned=True)
    if section != old_section:
        _drop(new, item)
        new[section].append(item)
    return new, {"op": "edit", "id": item_id, "before": before, "text": text, "section": section}


def delete(doc: Doc, item_id: str) -> tuple[Doc, dict[str, Any]]:
    """``doc`` without the item, and the change that holds what was removed."""
    new = copy.deepcopy(doc)
    stamp_ids(new)
    section, index, item = _where(new, item_id)
    before = _snapshot(section, index, item)
    _drop(new, item)
    return new, {"op": "delete", "id": item_id, "before": before}


def confirm(doc: Doc, item_id: str) -> tuple[Doc, dict[str, Any]]:
    """The same document and a ``confirm`` change: the user read this item and it stays as is."""
    new = copy.deepcopy(doc)
    stamp_ids(new)
    _where(new, item_id)
    return new, {"op": "confirm", "id": item_id}


def restore(doc: Doc, item: Item, section: str) -> tuple[Doc, dict[str, Any]]:
    """``doc`` with ``item`` put back at the end of ``section``, pinned: the user kept it.

    Used for an item a nightly pass marked stale and the user wants to keep.
    """
    new = copy.deepcopy(doc)
    stamp_ids(new)
    kept = copy.deepcopy(item)
    kept["pinned"] = True
    if find(new, str(kept["id"])) is not None:
        msg = "the item is already in core memory"
        raise ValueError(msg)
    new[section].append(kept)
    return new, {"op": "restore", "id": kept["id"], "section": section, "text": kept["text"]}


def states(doc: Doc | None) -> dict[str, tuple[str, str, bool]]:
    """``{id: (section, text, pinned)}``: what a version says about each item."""
    return {
        item_id: (section, str(item["text"]), bool(item.get("pinned")))
        for section, item, item_id in listing(doc if doc is not None else empty_doc())
    }


class Delta(NamedTuple):
    """What a version changed, by item id."""

    added: set[str]
    removed: set[str]
    changed: set[str]

    @property
    def touched(self) -> set[str]:
        """Every id the version added, removed or changed."""
        return self.added | self.removed | self.changed


def delta(base: Doc | None, doc: Doc) -> Delta:
    """The items ``doc`` adds, removes or changes (text, section or pin) against ``base``."""
    before, after = states(base), states(doc)
    return Delta(
        set(after) - set(before),
        set(before) - set(after),
        {item_id for item_id in before.keys() & after.keys() if before[item_id] != after[item_id]},
    )


def undo(current: Doc, base: Doc, version: Doc) -> Doc:
    """``current`` with what ``version`` changed against ``base`` taken back.

    The caller has checked that no later version touched those items. Added items go,
    removed ones come back where they were (clamped), changed ones return to their old text,
    section and pin.
    """
    changed = delta(base, version)
    new = copy.deepcopy(current)
    stamp_ids(new)
    old = {item_id: (section, item) for section, item, item_id in listing(base)}
    for item_id in changed.added:
        if (found := find(new, item_id)) is not None:
            _drop(new, found[2])
    for item_id in changed.changed | changed.removed:
        if (found := find(new, item_id)) is not None:
            _drop(new, found[2])
        section, item = old[item_id]
        index = next(i for i, other in enumerate(base[section]) if other is item)
        restored = copy.deepcopy(item)
        restored["id"] = item_id
        new[section].insert(min(index, len(new[section])), restored)
    return new


def load(raw: str) -> Doc:
    """A stored ``doc`` column as a document."""
    sections = json.loads(raw)["sections"]
    return {section: list(sections.get(section, [])) for section in SECTIONS}


def latest(conn: sqlite3.Connection) -> Version | None:
    """The current version, or None before the first one exists."""
    row = conn.execute(
        "SELECT id, upto_day, doc FROM core_memory ORDER BY rowid DESC LIMIT 1",
    ).fetchone()
    return None if row is None else Version(str(row[0]), row[1], load(row[2]))


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
    "Delta",
    "Doc",
    "Item",
    "Version",
    "append_version",
    "apply_changes",
    "confirm",
    "current",
    "delete",
    "delta",
    "edit",
    "empty_doc",
    "find",
    "item_count",
    "latest",
    "listing",
    "load",
    "numbered",
    "remember",
    "render",
    "restore",
    "stamp_ids",
    "states",
    "undo",
    "write_transaction",
]
