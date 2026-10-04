"""L3 core memory consolidation: the model's input and the gate on its answer (ADR 0146).

One local day goes in with the current core memory; typed changes come out, or the
reason to store nothing. The gates are mechanical and reject the whole list on any
failure. The runtime owns when it runs and the client; L2 owns the document and the write.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Final

from jarvis.state import core_memory

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

LOGGER = logging.getLogger(__name__)

_OPS: Final[frozenset[str]] = frozenset({"add", "rewrite", "stale"})
_FENCE: Final[str] = "```"


def day_labels(records: Sequence[tuple[str, str, str, str]]) -> dict[str, str]:
    """``{label: record id}`` with labels r1, r2, ... in time order.

    A 32-hex record id is easy for a model to mistype; a short label is not, and the
    runtime maps it back before anything is stored.
    """
    return {f"r{number}": record[0] for number, record in enumerate(records, start=1)}


def build_core_memory_messages(
    *,
    day: str,
    doc: core_memory.Doc,
    day_summary: str,
    records: Sequence[tuple[str, str, str, str]],
) -> list[dict[str, Any]]:
    """The one user message the consolidator sees: core memory, the day's summary, its records."""
    lines = [
        "[Current core memory]",
        core_memory.numbered(doc),
        "",
        f"[Summary of {day}]",
        day_summary or "(none)",
        "",
        f"[Records of {day}, oldest first; each line: label | time | speaker | words;"
        " sources cite these labels]",
    ]
    lines.extend(
        f"{label} | {record[1]} | {record[2]} | {record[3]}"
        for label, record in zip(day_labels(records), records, strict=True)
    )
    return [{"role": "user", "content": "\n".join(lines)}]


def _parse(text: str) -> list[Any] | str:
    body = text.strip()
    if body.startswith(_FENCE):  # a fenced answer is still the one JSON object
        body = body.removeprefix(_FENCE).removeprefix("json").removesuffix(_FENCE).strip()
    try:
        data = json.loads(body)
    except ValueError:
        return "not valid JSON"
    changes = data.get("changes") if isinstance(data, dict) else None
    return changes if isinstance(changes, list) else 'no "changes" list'


def _sources(raw: dict[str, Any], labels: Mapping[str, str]) -> list[str] | str:
    """The real record ids of the cited labels, or why the citation fails."""
    cited = raw.get("sources")
    if not (isinstance(cited, list) and cited and all(isinstance(label, str) for label in cited)):
        return f"{raw['op']} cites no sources"
    if foreign := sorted(set(cited) - set(labels)):
        return f"cites labels that are not that day's: {', '.join(foreign[:5])}"
    return [labels[label] for label in cited]


def _target(raw: dict[str, Any], items: int) -> int | str:
    number = raw.get("item")
    if not (isinstance(number, int) and not isinstance(number, bool)):
        return f"{raw['op']} has no item number"
    return number if 1 <= number <= items else f"item {number} does not exist"


def _wording(raw: dict[str, Any]) -> dict[str, str] | str:
    """The text collapsed to one line, and the section (required for an add, optional to move)."""
    text = raw.get("text")
    if not (isinstance(text, str) and text.split()):
        return f"{raw['op']} has no text"
    wording = {"text": " ".join(text.split())}
    section = raw.get("section")
    if section is not None or raw["op"] == "add":
        if section not in core_memory.SECTIONS:
            return f"section {section!r} is not one of the six"
        wording["section"] = section
    return wording


def _clean(raw: object, *, items: int, labels: Mapping[str, str]) -> dict[str, Any] | str:
    """One well-formed change, or why it is not."""
    if not isinstance(raw, dict) or raw.get("op") not in _OPS:
        return "an op is not add, rewrite or stale"
    cited = _sources(raw, labels)
    if isinstance(cited, str):
        return cited
    change: dict[str, Any] = {"op": raw["op"], "sources": cited}
    if raw["op"] != "add":
        number = _target(raw, items)
        if isinstance(number, str):
            return number
        change["item"] = number
    if raw["op"] != "stale":
        wording = _wording(raw)
        if isinstance(wording, str):
            return wording
        change.update(wording)
    return change


def _list_gates(
    changes: Sequence[dict[str, Any]],
    *,
    doc: core_memory.Doc,
    max_stale: int,
    max_chars: int,
    day: str,
) -> str | None:
    targets = [change["item"] for change in changes if "item" in change]
    if len(targets) != len(set(targets)):
        return "an item is targeted twice"
    if (stale := sum(change["op"] == "stale" for change in changes)) > max_stale:
        return f"{stale} stale ops over the {max_stale} cap"
    size = len(core_memory.render(core_memory.apply_changes(doc, changes, day)))
    return f"{size} chars over the {max_chars} cap" if size > max_chars else None


def pin_gate(
    changes: Sequence[dict[str, Any]],
    doc: core_memory.Doc,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(changes that may apply, notes to store beside them)`` (ADR 0154).

    An item the user edited or moved is pinned and the night does not touch it: a rewrite of
    it is dropped (note ``skipped``), a stale of it is not applied but kept as a
    ``suggest_stale`` note the page shows as a reminder. Every other change passes.
    """
    items = core_memory.listing(doc)
    kept: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    for change in changes:
        number = change.get("item")
        if number is None or not items[number - 1][1].get("pinned"):
            kept.append(change)
            continue
        section, item, item_id = items[number - 1]
        if change["op"] == "stale":
            notes.append(
                {
                    "op": "suggest_stale",
                    "item": number,
                    "id": item_id,
                    "section": section,
                    "text": item["text"],
                    "sources": change["sources"],
                },
            )
        else:
            notes.append({"op": "skipped", "reason": "pinned", "id": item_id, "change": change})
        LOGGER.info(
            "core_memory: %s of pinned item %s not applied (%s)",
            change["op"],
            item_id,
            item["text"][:40],
        )
    return kept, notes


def check_core_memory(  # noqa: PLR0913 — the answer and every gate's input.
    text: str | None,
    finish_reason: str | None,
    *,
    doc: core_memory.Doc,
    labels: Mapping[str, str],
    max_stale: int,
    max_chars: int,
    day: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], None] | tuple[None, None, str]:
    """``(changes, notes, None)`` when the answer may land, else ``(None, None, why)``.

    ``labels`` is :func:`day_labels` of that day; the changes come back with real record ids.
    ``notes`` are what :func:`pin_gate` kept out of ``changes``; the caller stores them.
    """
    if finish_reason in ("length", "max_tokens"):
        return None, None, "cut off by the output limit"
    parsed = _parse(text or "")
    if isinstance(parsed, str):
        return None, None, parsed
    cleaned_all: list[dict[str, Any]] = []
    for raw in parsed:
        cleaned = _clean(raw, items=core_memory.item_count(doc), labels=labels)
        if isinstance(cleaned, str):
            return None, None, cleaned
        cleaned_all.append(cleaned)
    changes, notes = pin_gate(cleaned_all, doc)
    reason = _list_gates(changes, doc=doc, max_stale=max_stale, max_chars=max_chars, day=day)
    return (changes, notes, None) if reason is None else (None, None, reason)


__all__ = ["build_core_memory_messages", "check_core_memory", "day_labels", "pin_gate"]
