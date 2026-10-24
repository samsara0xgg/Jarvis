"""L3 monthly core memory rebuild: the model's input and the gates on its answer (ADR 0389).

Once a month the note is written anew from the current version, his pinned items and the
month's day summaries. The answer is a whole document: every current item comes back kept
(as it was or reworded) or dropped with its reason, and new items cite the summaries' records.
The gates are mechanical and reject the whole answer on any failure; what passes becomes
nightly-shaped changes (rewrite, add, stale) that L2 applies as one version.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, Final

from jarvis.state import core_memory

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

_FENCE: Final[str] = "```"
_MARK: Final[re.Pattern[str]] = re.compile(r"\[record_id=([^\]]*)\]")


def summary_labels(
    summaries: Sequence[tuple[str, str]],
) -> tuple[list[tuple[str, str]], dict[str, str], dict[str, str]]:
    """``(summaries with labels, {label: record id}, {label: day})``.

    Each ``[record_id=...]`` marker becomes ``[s1]`` (``[s1 s2]`` for several ids), numbered in
    order across the month; a short label is hard to mistype and is mapped back before storing.
    """
    labels: dict[str, str] = {}
    days: dict[str, str] = {}
    out: list[tuple[str, str]] = []
    for day, summary in summaries:

        def relabel(match: re.Match[str], day: str = day) -> str:
            names = []
            for record_id in re.split(r"[\s,]+", match[1].strip()):
                if record_id:
                    name = f"s{len(labels) + 1}"
                    labels[name], days[name] = record_id, day
                    names.append(name)
            return f"[{' '.join(names)}]"

        out.append((day, _MARK.sub(relabel, summary)))
    return out, labels, days


def build_rebuild_messages(  # noqa: PLR0913 — the month's inputs, all keyword-only.
    *,
    month: str,
    doc: core_memory.Doc,
    kinds: Mapping[str, str],
    summaries: Sequence[tuple[str, str]],
    places: str = "",
    section_caps: Mapping[str, int] | None = None,
    max_chars: int,
) -> list[dict[str, Any]]:
    """The one user message: the numbered note with each item's kind, then the month's summaries.

    ``summaries`` are already relabelled by :func:`summary_labels`; ``kinds`` maps an item id to
    its kind (said, set, inferred, ...).
    """
    count = core_memory.item_count(doc)
    lines = [
        f"[Current core memory: {count} items, at most {max_chars} characters in all; every"
        " item comes back in items or in dropped]",
        core_memory.numbered(doc, section_caps, notes=kinds),
        "",
        *([places, ""] if places else []),
        f"[Day summaries of {month}, oldest first; sources cite the [sN] labels]",
    ]
    lines.extend(summary for _day, summary in summaries)
    if not summaries:
        lines.append("(none)")
    return [{"role": "user", "content": "\n".join(lines)}]


def _parse(text: str) -> tuple[list[Any], list[Any]] | str:
    body = text.strip()
    if body.startswith(_FENCE):  # a fenced answer is still the one JSON object
        body = body.removeprefix(_FENCE).removeprefix("json").removesuffix(_FENCE).strip()
    try:
        data = json.loads(body)
    except ValueError:
        return "not valid JSON"
    if not isinstance(data, dict):
        return "not a JSON object"
    items, dropped = data.get("items"), data.get("dropped", [])
    if not isinstance(items, list) or not isinstance(dropped, list):
        return 'no "items" and "dropped" lists'
    return items, dropped


def _number(raw: dict[str, Any], count: int) -> int | None | str:
    number = raw.get("item")
    if number is None:
        return None
    if not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= count:
        return f"item {number!r} does not exist"
    return number


def _kept(  # noqa: PLR0911 — one early return per failed check.
    raw: object, *, count: int, labels: Mapping[str, str],
) -> tuple[int | None, str, str, list[str]] | str:
    """``(item number or None for a new one, section, text, cited labels)``, or why not."""
    if not isinstance(raw, dict):
        return "an item is not an object"
    number = _number(raw, count)
    if isinstance(number, str):
        return number
    text, section, cited = raw.get("text"), raw.get("section"), raw.get("sources", [])
    if not (isinstance(text, str) and text.split()):
        return f"item {number} has no text" if number else "a new item has no text"
    if section not in core_memory.SECTIONS:
        return f"section {section!r} is not one of the ten"
    if not (isinstance(cited, list) and all(isinstance(label, str) for label in cited)):
        return "sources is not a list of labels"
    if foreign := sorted(set(cited) - set(labels)):
        return f"cites labels that were not given: {', '.join(foreign[:5])}"
    if number is None and not cited:
        return "a new item cites no sources"
    line = " ".join(text.split())
    try:
        core_memory.check_item(section, line)
    except ValueError as exc:
        return str(exc)
    return number, section, line, list(cited)


def _dropped(raw: object, count: int) -> tuple[int, str] | str:
    if not isinstance(raw, dict):
        return "a dropped entry is not an object"
    number = _number(raw, count)
    if isinstance(number, str):
        return number
    reason = raw.get("reason")
    if number is None:
        return "a dropped entry names no item"
    if not (isinstance(reason, str) and reason.split()):
        return f"dropped item {number} gives no reason"
    return number, " ".join(reason.split())


def _caps(
    new: core_memory.Doc, *, max_chars: int, section_caps: Mapping[str, int],
) -> str | None:
    for section, cap in section_caps.items():
        if (size := core_memory.section_chars(new, section)) > cap:
            return f"{section} is {size} chars over its {cap} cap"
    size = len(core_memory.render(new))
    return f"{size} chars over the {max_chars} cap" if size > max_chars else None


def check_rebuild(  # noqa: C901, PLR0911, PLR0912, PLR0913 — the answer and every gate's input.
    text: str | None,
    finish_reason: str | None,
    *,
    doc: core_memory.Doc,
    labels: Mapping[str, str],
    label_days: Mapping[str, str],
    his_labels: Collection[str],
    kinds: Mapping[str, str],
    max_chars: int,
    section_caps: Mapping[str, int] = core_memory.SECTION_CAPS,
    day: str,
) -> tuple[list[dict[str, Any]], None] | tuple[None, str]:
    """``(changes, None)`` when the rebuild may land, else ``(None, why)``.

    The gates: not cut off; every current item kept or dropped exactly once, a dropped one with
    its reason; every pinned item kept word for word in its section; an item of kind
    ``inferred`` kept only when it cites a record of his words (``his_labels``); new items cite
    the summaries; caps held. The changes are nightly-shaped with real record ids: a reworded,
    moved or newly cited item is a rewrite that keeps its records and adds the new ones, a new
    one an add, a dropped one a stale whose ``reason`` is the model's. A thread's evidence is the
    latest day it cites, else unchanged.
    """
    if finish_reason in ("length", "max_tokens"):
        return None, "cut off by the output limit"
    parsed = _parse(text or "")
    if isinstance(parsed, str):
        return None, parsed
    items = core_memory.numbered_items(doc)
    seen: dict[int, str] = {}
    changes: list[dict[str, Any]] = []
    for raw in parsed[0]:
        kept = _kept(raw, count=len(items), labels=labels)
        if isinstance(kept, str):
            return None, kept
        number, section, line, cited = kept
        evidence = max((label_days[label] for label in cited), default=None)
        sources = list(dict.fromkeys(labels[label] for label in cited))
        if number is None:
            changes.append(
                {"op": "add", "section": section, "text": line, "sources": sources,
                 "evidence": evidence},
            )
            continue
        if number in seen:
            return None, f"item {number} comes back twice"
        seen[number] = "kept"
        old_section, item, item_id = items[number - 1]
        if item.get("pinned") and (line != item["text"] or section != old_section):
            return None, f"pinned item {number} is not kept word for word"
        if kinds.get(item_id) == "inferred" and not set(cited) & set(his_labels):
            return None, f"inferred item {number} is kept without a record of his words"
        if line != item["text"] or section != old_section or sources:
            # The item's records stay; new citations add to them (a pinned item's too).
            kept_sources = list(dict.fromkeys([*(item.get("sources") or []), *sources]))
            changes.append(
                {"op": "rewrite", "item": number, "text": line, "section": section,
                 "sources": kept_sources, "evidence": evidence},
            )
    for raw in parsed[1]:
        dropped = _dropped(raw, len(items))
        if isinstance(dropped, str):
            return None, dropped
        number, reason = dropped
        if number in seen:
            return None, f"item {number} comes back twice"
        seen[number] = "dropped"
        if items[number - 1][1].get("pinned"):
            return None, f"pinned item {number} is dropped"
        changes.append({"op": "stale", "item": number, "sources": [], "reason": reason})
    if missing := [n for n in range(1, len(items) + 1) if n not in seen]:
        return None, f"items neither kept nor dropped: {', '.join(map(str, missing[:5]))}"
    over = _caps(
        core_memory.apply_changes(doc, changes, day),
        max_chars=max_chars,
        section_caps=section_caps,
    )
    return (None, over) if over else (changes, None)


__all__ = ["build_rebuild_messages", "check_rebuild", "summary_labels"]
