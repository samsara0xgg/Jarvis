"""L3 core memory consolidation: the model's input and the gate on its answer (ADR 0146).

One local day goes in with the current core memory; typed changes come out, or the
reason to store nothing. The gates are mechanical and reject the whole list on any
failure. A change may cite the day's records and the records the items already cite, and a
thread item closes in code once every date it looked ahead to has passed (ADR 0389). The
runtime owns when it runs and the client; L2 owns the document and the write.
"""

from __future__ import annotations

import calendar
import json
import logging
import re
from datetime import date
from typing import TYPE_CHECKING, Any, Final

from jarvis.state import core_memory

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

LOGGER = logging.getLogger(__name__)

_OPS: Final[frozenset[str]] = frozenset({"add", "rewrite", "stale", "ongoing"})
_FENCE: Final[str] = "```"
# ISO, 2026年10月3日, 10月3日 / 10月3号, then a month alone: 2026-12, 2026年12月. The groups are
# (y, m, d), (y?, m, d), (y, m), (y, m); a month alone stands for its last day. A bare 10/3 is not
# read: it is also a ratio (读了 1/2), and a misread closes a thread that is still going.
_DATE: Final[re.Pattern[str]] = re.compile(
    r"(?<![\d-])(?:(\d{4})-(\d{1,2})-(\d{1,2})|(?:(\d{4})年)?(\d{1,2})月(\d{1,2})[日号]"
    r"|(\d{4})-(\d{1,2})(?![\d-])|(\d{4})年(\d{1,2})月(?!\d))",
)


def day_labels(records: Sequence[tuple[str, str, str, str]]) -> dict[str, str]:
    """``{label: record id}`` with labels r1, r2, ... in time order.

    A 32-hex record id is easy for a model to mistype; a short label is not, and the
    runtime maps it back before anything is stored.
    """
    return {f"r{number}": record[0] for number, record in enumerate(records, start=1)}


def item_labels(doc: core_memory.Doc) -> dict[str, str]:
    """``{label: record id}`` with labels e1, e2, ... for the records the live items cite.

    Shown beside each item, so a change on any day can carry an item's earlier sources over
    (a merge keeps both items' records) and cite records of days other than the one read.
    """
    cited = dict.fromkeys(
        s for _sec, item, _id in core_memory.numbered_items(doc) for s in item.get("sources") or []
    )
    return {f"e{number}": record_id for number, record_id in enumerate(cited, start=1)}


def build_core_memory_messages(  # noqa: PLR0913 — the day's inputs, all keyword-only.
    *,
    day: str,
    doc: core_memory.Doc,
    day_summary: str,
    records: Sequence[tuple[str, str, str, str]],
    places: str = "",
    section_caps: Mapping[str, int] | None = None,
) -> list[dict[str, Any]]:
    """The one user message the consolidator sees: core memory, the day's summary, its records.

    ``places`` is the ``[Places he keeps]`` block (ADR 0234), so the consolidator can leave out
    items that only restate a kept place; empty when none is kept.
    """
    cites = {record_id: label for label, record_id in item_labels(doc).items()}
    lines = [
        "[Current core memory]",
        core_memory.numbered(doc, section_caps, cites),
        "",
        *([places, ""] if places else []),
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
        return f"cites labels that were not given: {', '.join(foreign[:5])}"
    return list(dict.fromkeys(labels[label] for label in cited))


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
            return f"section {section!r} is not one of the ten"
        wording["section"] = section
    return wording


def _clean(  # noqa: PLR0911 — one early return per failed check.
    raw: object, *, doc: core_memory.Doc, labels: Mapping[str, str], earlier: Mapping[str, str],
) -> dict[str, Any] | str:
    """One well-formed change, or why it is not.

    ``labels`` are the day's records, ``earlier`` the records the items cite; ``ongoing`` renews a
    thread item's evidence only and must cite at least one of the day's records.
    """
    if not isinstance(raw, dict) or raw.get("op") not in _OPS:
        return "an op is not add, rewrite, stale or ongoing"
    cited = _sources(raw, {**earlier, **labels})
    if isinstance(cited, str):
        return cited
    change: dict[str, Any] = {"op": raw["op"], "sources": cited}
    if raw["op"] != "add":
        items = core_memory.numbered_items(doc)
        number = _target(raw, len(items))
        if isinstance(number, str):
            return number
        change["item"] = number
        if raw["op"] == "ongoing":
            if items[number - 1][0] not in core_memory.THREAD_SECTIONS:
                return f"ongoing on item {number}, which is no thread"
            if not set(raw["sources"]) & set(labels):
                return "ongoing cites none of the day's records"
            return change
    if raw["op"] != "stale":
        wording = _wording(raw)
        if isinstance(wording, str):
            return wording
        change.update(wording)
    return change


def _list_gates(  # noqa: PLR0913 — the gates' inputs, all keyword-only.
    changes: Sequence[dict[str, Any]],
    *,
    doc: core_memory.Doc,
    max_stale: int,
    max_chars: int,
    section_caps: Mapping[str, int],
    day: str,
) -> str | None:
    targets = [change["item"] for change in changes if "item" in change]
    if len(targets) != len(set(targets)):
        return "an item is targeted twice"
    if (stale := sum(change["op"] == "stale" for change in changes)) > max_stale:
        return f"{stale} stale ops over the {max_stale} cap"
    numbered = core_memory.numbered_items(doc)
    for change in changes:
        if "text" in change:
            section = change.get("section") or numbered[change["item"] - 1][0]
            try:
                core_memory.check_item(section, change["text"])
            except ValueError as exc:
                return str(exc)
    new = core_memory.apply_changes(doc, changes, day)
    for section, cap in section_caps.items():
        if (size := core_memory.section_chars(new, section)) > cap:
            return f"{section} is {size} chars over its {cap} cap"
    size = len(core_memory.render(new))
    return f"{size} chars over the {max_chars} cap" if size > max_chars else None


def _dates_in(text: str, day: date) -> list[date]:
    """Every valid date in ``text``; a year-less one takes the year that puts it nearest ``day``.

    ``day`` is the day the text was worded. A month without a day (2026-12, 2026年12月) stands
    for its last day.
    """
    found: list[date] = []
    for match in _DATE.finditer(text):
        if match[7] or match[9]:
            year, whole = int(match[7] or match[9]), int(match[8] or match[10])
            if 1 <= whole <= 12:  # noqa: PLR2004 — the calendar's months
                found.append(date(year, whole, calendar.monthrange(year, whole)[1]))
            continue
        y, month, dom = match.group(1, 2, 3) if match[1] else match.group(4, 5, 6)
        years = [int(y)] if y else [day.year - 1, day.year, day.year + 1]
        candidates: list[date] = []
        for candidate in years:
            try:
                candidates.append(date(candidate, int(month), int(dom)))
            except ValueError:
                continue  # not a calendar date (month 13, Feb 29 of a common year)
        if candidates:
            found.append(min(candidates, key=lambda c: abs((c - day).days)))
    return found


def date_close(doc: core_memory.Doc, day: str, since: str | None) -> list[dict[str, Any]]:
    """Stale changes for thread items whose every date ahead has passed by ``day`` (ADR 0389).

    By code, no model. A date counts only if it lay after the day the item was last worded
    (``since``): a date that was already history then ("as of 10-01", "interviewed 10-14") is not
    one the thread waits for. An item with no such date is never touched, nor is a standing
    section. A pinned item only gets one on the night its date first passes (later than
    ``since``, the version's ``upto_day``), so a reminder the user answered with 对 does not come
    back.
    """
    today = date.fromisoformat(day)
    seen = date.fromisoformat(since) if since else None
    out: list[dict[str, Any]] = []
    for number, (section, item, _id) in enumerate(core_memory.numbered_items(doc), start=1):
        if section not in core_memory.THREAD_SECTIONS:
            continue
        worded = str(item.get("since") or "")
        try:
            anchor = date.fromisoformat(worded)
        except ValueError:  # an item with no day of wording: the night it is read
            anchor = today
        ahead = [d for d in _dates_in(item["text"], anchor) if d.isoformat() > worded]
        if not ahead or (due := max(ahead)) > today:
            continue
        if item.get("pinned") and seen and due <= seen:
            continue
        out.append(
            {"op": "stale", "item": number, "sources": [], "reason": f"date passed ({due})"},
        )
    return out


def pin_gate(
    changes: Sequence[dict[str, Any]],
    doc: core_memory.Doc,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(changes that may apply, notes to store beside them)`` (ADR 0154).

    An item the user edited or moved is pinned and the night does not touch it: a rewrite of
    it is dropped (note ``skipped``), a stale of it is not applied but kept as a
    ``suggest_stale`` note the page shows as a reminder. Every other change passes, ``ongoing``
    on a pinned thread too: it changes no wording.
    """
    items = core_memory.numbered_items(doc)
    kept: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    for change in changes:
        number = change.get("item")
        if number is None or change["op"] == "ongoing" or not items[number - 1][1].get("pinned"):
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
    section_caps: Mapping[str, int] = core_memory.SECTION_CAPS,
    day: str,
    since: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], None] | tuple[None, None, str]:
    """``(changes, notes, None)`` when the answer may land, else ``(None, None, why)``.

    ``labels`` is :func:`day_labels` of that day; :func:`item_labels` of ``doc`` may be cited too.
    The changes come back with real record ids. ``notes`` are what :func:`pin_gate` kept out of
    ``changes``; the caller stores them. Threads whose dates passed (:func:`date_close`,
    ``since`` = the base version's ``upto_day``) are added after the gates, outside the
    ``max_stale`` cap, and skip an item the answer targets.
    """
    if finish_reason in ("length", "max_tokens"):
        return None, None, "cut off by the output limit"
    parsed = _parse(text or "")
    if isinstance(parsed, str):
        return None, None, parsed
    cleaned_all: list[dict[str, Any]] = []
    for raw in parsed:
        cleaned = _clean(raw, doc=doc, labels=labels, earlier=item_labels(doc))
        if isinstance(cleaned, str):
            return None, None, cleaned
        cleaned_all.append(cleaned)
    changes, notes = pin_gate(cleaned_all, doc)
    reason = _list_gates(changes, doc=doc, max_stale=max_stale,
        max_chars=max_chars,
        section_caps=section_caps,
        day=day,
    )
    if reason is not None:
        return None, None, reason
    targeted = {change["item"] for change in cleaned_all if "item" in change}
    swept = [c for c in date_close(doc, day, since) if c["item"] not in targeted]
    swept_changes, swept_notes = pin_gate(swept, doc)
    return [*changes, *swept_changes], [*notes, *swept_notes], None


__all__ = [
    "build_core_memory_messages",
    "check_core_memory",
    "date_close",
    "day_labels",
    "item_labels",
    "pin_gate",
]
