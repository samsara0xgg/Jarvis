"""ADR 0320: the claims block an answer opens with, and the check of it against receipts.

The answer declares what it says was done, set up or looked up, with the receipts that back it,
and what it offers. This module reads that block and judges it; the loops decide what to do with
the verdict (send the answer back once, or speak it). Pure: no clock, no log, no model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

from jarvis.shared.receipts import Receipt, said

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

Kind = Literal["did", "looked", "offer"]

_ENTRY: Final = re.compile(r"^\s*(did|looked|offer)\s+(.*?)\s*:\s*(.*?)\s*$", re.IGNORECASE)
_ID: Final = re.compile(r"\br[0-9a-f]{6}\b")

NOTE_HEAD: Final[str] = (
    "[From the program, not the user's words] The <claims> block of your answer does not match "
    "the receipts:"
)
NOTE_TAIL: Final[str] = (
    "Rewrite the answer to match the receipts. Call a tool only if his words this turn ask "
    "for it."
)
NOTE_OFFER: Final[str] = "Do it instead of offering."


@dataclass(frozen=True)
class Entry:
    """One line of the block: what kind, what it cites (receipt ids, or a tool), and the line."""

    kind: Kind
    cites: tuple[str, ...]
    line: str


@dataclass(frozen=True)
class Verdict:
    """One entry's outcome: ``why`` is empty when it passes."""

    entry: Entry
    why: str = ""

    @property
    def ok(self) -> bool:
        """Whether the entry passes."""
        return not self.why

    def to_json(self) -> dict[str, Any]:
        """The row form."""
        return {"line": self.entry.line, "kind": self.entry.kind, "ok": self.ok, "why": self.why}


def parse(block: str | None) -> list[Entry]:
    """The entries of a block; a line that is no entry is left out, an empty block has none."""
    entries: list[Entry] = []
    for raw in (block or "").splitlines():
        found = _ENTRY.match(raw)
        if found is None:
            continue
        kind = found.group(1).lower()
        cites = tuple(one.strip().lower() for one in found.group(2).split(",") if one.strip())
        entries.append(Entry(kind, cites, raw.strip()))  # type: ignore[arg-type]
    return entries


def _flat(value: object) -> Iterable[str]:
    """Every string inside a message's content, whatever the provider's shape."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for one in value.values():
            yield from _flat(one)
    elif isinstance(value, list):
        for one in value:
            yield from _flat(one)


def shown_ids(messages: Sequence[Mapping[str, Any]]) -> set[str]:
    """The receipt ids a request's messages carry: tool results, history lines, in-flight note."""
    return {found for message in messages for text in _flat(message) for found in _ID.findall(text)}


def check(
    block: str | None,
    *,
    shown: Mapping[str, Receipt],
    offerable: Callable[[str], bool],
) -> list[Verdict]:
    """Judge each entry of ``block``.

    ``shown`` maps the receipt ids she was shown in this request to their receipts; a cited id
    outside it has no such receipt. ``did`` needs a change that is done, ``looked`` a read that is
    done. ``offerable(tool)`` is true when the tool is on this request's menu and would only
    read: offering it is refused, she does the lookup instead.
    """
    verdicts = []
    for entry in parse(block):
        why = _offer(entry, offerable) if entry.kind == "offer" else _cited(entry, shown)
        verdicts.append(Verdict(entry, why))
    return verdicts


def _offer(entry: Entry, offerable: Callable[[str], bool]) -> str:
    tool = entry.cites[0] if entry.cites else ""
    return f"{tool} only reads and is on the menu" if offerable(tool) else ""


def _cited(entry: Entry, shown: Mapping[str, Receipt]) -> str:
    if not entry.cites:
        return "it cites no receipt"
    for cite in entry.cites:
        receipt = shown.get(cite)
        if receipt is None:
            return f"no such receipt {cite}"
        if not receipt.ok:
            return f"receipt {cite} is {receipt.status}, not done"
        if entry.kind == "did" and receipt.reads_only:
            return f"receipt {cite} is a read, not a change"
        if entry.kind == "looked" and not receipt.reads_only:
            return f"receipt {cite} is a change, not a read"
    return ""


def note(failed: Sequence[Verdict], this_turn: Sequence[tuple[str, Receipt]]) -> str:
    """The one note that sends an answer back: each failing entry, this turn's receipts, the ask."""
    lines = [f"- {v.entry.line} ({v.why})" for v in failed]
    receipts = (
        "This turn's receipts: " + "; ".join(said(r, rid) for rid, r in this_turn) + "."
        if this_turn
        else "This turn has no receipts."
    )
    offered = any(v.entry.kind == "offer" for v in failed)
    tail = f"{NOTE_TAIL} {NOTE_OFFER}" if offered else NOTE_TAIL
    return "\n".join([NOTE_HEAD, *lines, receipts, tail])
