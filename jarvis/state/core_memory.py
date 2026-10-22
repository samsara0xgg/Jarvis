"""L2 core memory: the append-only versioned note about the user (ADR 0146).

One ``core_memory`` row is one version of a document with ten fixed sections; the
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
import re
import uuid
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Final, NamedTuple

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator, Mapping, Sequence

# ADR 0258, 0299: section name -> default cap in characters; the caps sum to the 40,000 ceiling.
SECTION_CAPS: Final[dict[str, int]] = {
    "经历": 4000,
    "性格和价值观": 3000,
    "身体": 2000,
    "钱": 2000,
    "兴趣和品味": 2000,
    "你希望被怎么对待": 3000,
    "定下来的规矩": 3000,
    "证件和硬期限": 1000,
    "身边的人": 10000,
    "生活里的几件事": 10000,
}
SECTIONS: Final[tuple[str, ...]] = tuple(SECTION_CAPS)
DEFAULT_SECTION: Final[str] = "经历"
# Not a section any more (ADR 0299): his to-dos are the ledger's. A stored version may still hold
# the section; :func:`load` leaves it out and the one-time carry-in reads it with
# :func:`retired_items` before :func:`memory_db.drop_due_section` writes a version without it.
DUE_SECTION: Final[str] = "承诺和待办"
ID_FREE_SECTION: Final[str] = "证件和硬期限"
GOALS_SECTION: Final[str] = "生活里的几件事"
# ADR 0389: what is going on. A thread item carries ``state`` (open or closed) and ``evidence``,
# the day of its latest evidence; every other section is standing, retired only on evidence.
THREAD_SECTIONS: Final[frozenset[str]] = frozenset({GOALS_SECTION, ID_FREE_SECTION})
OPEN: Final[str] = "open"
CLOSED: Final[str] = "closed"  # a thread that ended
STALE: Final[str] = "stale"  # a standing item that stopped being true
# The six sections of ADR 0146 and where an older version's items go.
_LEGACY: Final[dict[str, str]] = {
    "关于你": "经历",
    "偏好": "兴趣和品味",
    "常提到的人": "身边的人",
    "正在做的事": "生活里的几件事",
}
_DATES: Final[re.Pattern[str]] = re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}")
_LONG_DIGITS: Final[re.Pattern[str]] = re.compile(r"\d{6,}")
NAME_TOPIC: Final[str] = "name"
_NAME_ROW: Final[str] = "profile-name"  # the legacy profile row first-run setup wrote
_FACT_PREFIX: Final[str] = "fact:"

# One item: {"text", "topic", "sources", "since"}, plus "id" once any write has stamped it and
# "pinned" once the user edited or moved it (ADR 0154). A thread item also carries "state" and
# "evidence"; an item that ended carries "state" closed or stale, "ended_on" and "why", stays in the
# document and leaves the prompt (ADR 0389). A document maps each section to its items.
Item = dict[str, Any]
Doc = dict[str, list[Item]]


class Version(NamedTuple):
    """One ``core_memory`` row."""

    id: str
    upto_day: str | None  # the last local day a nightly run consolidated
    doc: Doc


def empty_doc() -> Doc:
    """A document with every section and no items."""
    return {section: [] for section in SECTIONS}


def ended(item: Item) -> bool:
    """Whether the item ended (a closed thread, a stale standing item): out of the prompt."""
    return item.get("state") in (CLOSED, STALE)


def live(doc: Doc) -> Doc:
    """The document without its ended items: what the prompt and every reader of him use."""
    return {section: [item for item in items if not ended(item)] for section, items in doc.items()}


def evidence_day(item: Item) -> str | None:
    """The day of a thread item's latest evidence; an item from before ADR 0389 uses ``since``."""
    return item.get("evidence") or item.get("since")


def section_chars(doc: Doc, section: str) -> int:
    """The size of a section as the prompt renders its lines (without the heading)."""
    return len("\n".join(f"- {item['text']}" for item in doc[section] if not ended(item)))


def check_item(section: str, text: str) -> None:
    """ValueError when ``text`` may not go in ``section`` (证件和硬期限 holds dates, no ID)."""
    if section == ID_FREE_SECTION and _LONG_DIGITS.search(_DATES.sub("", text)):
        msg = f"{section} holds dates only: no number of 6 or more digits"
        raise ValueError(msg)


def _rank(doc: Doc) -> None:
    """Each section in rank order, in place: pinned first, then the most recent ``since``.

    Ended items go after every live one.
    """
    for items in doc.values():
        items.sort(key=lambda item: str(item.get("since") or ""), reverse=True)
        items.sort(key=lambda item: not item.get("pinned"))
        items.sort(key=ended)


def _revise(item: Item, text: str, since: str) -> None:
    """Set the item's wording; the old one stays in ``history`` with the day it held from."""
    if item["text"] != text:
        item.setdefault("history", []).append(
            {"text": item["text"], "since": item.get("since")},
        )
    item.update(text=text, since=since)


def render(doc: Doc, labels: Mapping[str, str] | None = None) -> str:
    """The prompt text: ``### <section>`` then ``- <text>`` lines; empty sections omitted.

    ``labels`` (item id to a short note) puts ``(note)`` ahead of that item's text.
    """
    lines: list[str] = []
    shown = live(doc)
    for section in SECTIONS:
        if shown[section]:
            lines.append(f"### {section}")
            for item in shown[section]:
                note = (labels or {}).get(item.get("id", ""))
                lines.append(f"- ({note}) {item['text']}" if note else f"- {item['text']}")
    return "\n".join(lines)


def numbered(
    doc: Doc,
    caps: Mapping[str, int] | None = None,
    cites: Mapping[str, str] | None = None,
    notes: Mapping[str, str] | None = None,
) -> str:
    """The consolidation input: live items numbered ``[N]`` 1..N in section order.

    Each heading carries the section's size and cap: ``### 身边的人 (1234/10000)``.

    A pinned item (ADR 0154) reads ``[N] (pinned) text``: the model is told it is fixed. A thread
    item ends with ``(evidence <day>)``; with ``cites`` (record id to label) an item that cites
    records ends with ``[cites e1 e2]``, so a change may carry them over (ADR 0389). ``notes`` (item
    id to a short note, as :func:`render` takes) goes ahead of the text.
    """
    cap = caps or SECTION_CAPS
    lines: list[str] = []
    current = ""
    for number, (section, item, item_id) in enumerate(numbered_items(doc), start=1):
        if section != current:
            current = section
            lines.append(f"### {section} ({section_chars(doc, section)}/{cap[section]})")
        mark = "(pinned) " if item.get("pinned") else ""
        note = f"({notes[item_id]}) " if notes and item_id in notes else ""
        line = f"[{number}] {mark}{note}{item['text']}"
        if section in THREAD_SECTIONS:
            line += f" (evidence {evidence_day(item)})"
        cited = [cites[s] for s in item.get("sources") or [] if cites and s in cites]
        lines.append(f"{line} [cites {' '.join(cited)}]" if cited else line)
    return "\n".join(lines) or "(empty)"


def item_count(doc: Doc) -> int:
    """How many live items the document has (the highest valid ``[N]``)."""
    return sum(len(items) for items in live(doc).values())


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


def numbered_items(doc: Doc) -> list[tuple[str, Item, str]]:
    """``(section, item, id)`` of every live item: ``[N]`` in :func:`numbered` is entry N."""
    return [entry for entry in listing(doc) if not ended(entry[1])]


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


def _thread(item: Item, section: str, evidence: str | None) -> None:
    """Mark ``item`` an open thread with ``evidence`` (kept when None) if ``section`` is one."""
    if section in THREAD_SECTIONS:
        item["state"] = OPEN
        if evidence is not None:
            item["evidence"] = evidence


def apply_changes(doc: Doc, changes: Sequence[Mapping[str, Any]], day: str) -> Doc:
    """A copy of ``doc`` with the gated nightly ``changes`` applied in order.

    Item numbers refer to ``numbered(doc)`` (live items) before any change: an add appends to
    its section, a rewrite replaces text, sources and ``since`` (the topic stays; the old wording
    goes to ``history``; the same text keeps ``since``) and may move the item, ``ongoing`` only
    renews a thread's evidence, and a
    stale ends the item where it is: a thread closes, a standing item goes stale, both with
    ``ended_on`` and the change's ``reason`` as ``why`` (ADR 0389). A thread's evidence is the
    change's ``evidence`` when given (None keeps it), else ``day``. Every section ends ranked.
    """
    new = copy.deepcopy(doc)
    stamp_ids(new)
    flat = [item for _section, item, _id in numbered_items(new)]
    for change in changes:
        evidence = change.get("evidence", day)
        if change["op"] == "add":
            item = _new_item(change["text"], None, change["sources"], day)
            _thread(item, change["section"], evidence)
            new[change["section"]].append(item)
            continue
        item = flat[change["item"] - 1]
        section = next(name for name in SECTIONS if any(other is item for other in new[name]))
        if change["op"] == "ongoing":
            _thread(item, section, evidence)
            continue
        if change["op"] == "stale":
            item.update(
                state=CLOSED if section in THREAD_SECTIONS else STALE,
                ended_on=day,
                why=change.get("reason"),
            )
            continue
        if change["text"] != item["text"]:  # the same words keep the day they were worded
            _revise(item, change["text"], day)
        item["sources"] = list(change["sources"])
        target = change.get("section") or section
        if target != section:
            _drop(new, item)
            new[target].append(item)
        _thread(item, target, evidence)
    _rank(new)
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
    item is added to ``section`` (default: 经历); a section is ranked after. ValueError when the
    section may not hold the text (:func:`check_item`).
    """
    new = copy.deepcopy(doc)
    stamp_ids(new)
    for current in SECTIONS:
        for item in new[current]:
            if not ended(item) and str(item["topic"] or "").casefold() == topic.casefold():
                check_item(section or current, text)
                _revise(item, text, day)
                if section is not None and section != current:
                    _drop(new, item)
                    new[section].append(item)
                _thread(item, section or current, day)
                _rank(new)
                change = {"op": "rewrite", "topic": topic, "text": text, "sources": item["sources"]}
                return new, change
    target = section or DEFAULT_SECTION
    check_item(target, text)
    added = _new_item(text, topic, [], day)
    _thread(added, target, day)
    new[target].append(added)
    _rank(new)
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

    The item moves to the new section when it changes; the section is ranked after. Its sources
    stay: they are what the item was first drawn from. ValueError when the section may not hold
    the text (:func:`check_item`).
    """
    check_item(section, text)
    new = copy.deepcopy(doc)
    stamp_ids(new)
    old_section, index, item = _where(new, item_id)
    before = _snapshot(old_section, index, item)
    _revise(item, text, day)
    item["pinned"] = True
    if section != old_section:
        _drop(new, item)
        new[section].append(item)
    _rank(new)
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


def restore(
    doc: Doc, item: Item, section: str, day: str | None = None,
) -> tuple[Doc, dict[str, Any]]:
    """``doc`` with ``item`` live again in ``section``, pinned: the user kept it.

    Used for an item that ended (a nightly stale, a date that passed, a rebuild) and the user wants
    to keep: an ended item still in the document comes back where it is, a thread open again; an
    item a version before ADR 0389 removed is put back at the end of ``section``. His keeping it
    is a thread's evidence on ``day``.
    """
    new = copy.deepcopy(doc)
    stamp_ids(new)
    found = find(new, str(item["id"]))
    if found is not None and not ended(found[2]):
        msg = "the item is already in core memory"
        raise ValueError(msg)
    if found is None:
        kept = copy.deepcopy(item)
        new[section].append(kept)
    else:
        section, _index, kept = found
    for key in ("state", "ended_on", "why"):
        kept.pop(key, None)
    kept["pinned"] = True
    _thread(kept, section, day)
    _rank(new)
    return new, {"op": "restore", "id": kept["id"], "section": section, "text": kept["text"]}


def states(doc: Doc | None) -> dict[str, tuple[str, str, bool, bool]]:
    """``{id: (section, text, pinned, ended)}``: what a version says about each item."""
    return {
        item_id: (section, str(item["text"]), bool(item.get("pinned")), ended(item))
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
    """The items ``doc`` adds, removes or changes (text, section, pin or ended) against ``base``."""
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
    _rank(new)
    return new


def load(raw: str) -> Doc:
    """A stored ``doc`` column as a document.

    A name that is no section (the retired 承诺和待办, ADR 0299) is left out.
    """
    stored = json.loads(raw)["sections"]
    sections: Doc = {section: [] for section in SECTIONS}
    for name, items in stored.items():  # an older version's names map one to one (ADR 0258)
        if (target := _LEGACY.get(name, name)) in sections:
            sections[target].extend(items)
    return sections


def retired_items(raw: str) -> list[Item]:
    """The items a stored ``doc`` column still holds under the retired 承诺和待办 (ADR 0299)."""
    return list(json.loads(raw)["sections"].get(DUE_SECTION) or [])


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
    "CLOSED",
    "DEFAULT_SECTION",
    "DUE_SECTION",
    "GOALS_SECTION",
    "ID_FREE_SECTION",
    "NAME_TOPIC",
    "OPEN",
    "SECTIONS",
    "SECTION_CAPS",
    "STALE",
    "THREAD_SECTIONS",
    "Delta",
    "Doc",
    "Item",
    "Version",
    "append_version",
    "apply_changes",
    "check_item",
    "confirm",
    "current",
    "delete",
    "delta",
    "edit",
    "empty_doc",
    "ended",
    "evidence_day",
    "find",
    "item_count",
    "latest",
    "listing",
    "live",
    "load",
    "numbered",
    "numbered_items",
    "remember",
    "render",
    "restore",
    "retired_items",
    "section_chars",
    "stamp_ids",
    "states",
    "undo",
    "write_transaction",
]
