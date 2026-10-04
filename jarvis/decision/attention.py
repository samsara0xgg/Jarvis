"""L3 attention (ADR 0155): event -> context pack -> judge -> delivery -> feedback -> log.

An event source (job mail is the first) builds a :class:`ContextPack`: its typed facts and the
situation at decision time. One swappable seam, :data:`Judge`, turns a pack into a
:class:`Judgement`: how loudly this event should reach Allen. Delivery acts on the level, his
five-level feedback is logged next to the pack, and :func:`replay` runs any other judge over the
logged packs. The first judge, :func:`rule_judge_v1`, is his category rule table.

Layer rules: stdlib only; no wiring.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

# ledger: keep it, tell no one; then a card; a card with a sound; speak a line.
LEVELS: Final[tuple[str, ...]] = ("ledger", "card", "card_sound", "speak")
# A letter read later than this after it arrived is kept, not announced.
STALE_AFTER_H: Final[float] = 48.0


@dataclass(frozen=True)
class ContextPack:
    """Everything a judge sees about one event, serializable exactly as it was seen."""

    source: str
    event_id: str
    facts: dict[str, Any]
    situation: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        """The pack as the log keeps it."""
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> ContextPack:
        """Read a logged pack back."""
        raw = json.loads(text)
        return cls(raw["source"], raw["event_id"], raw["facts"], raw.get("situation", {}))


@dataclass(frozen=True)
class Judgement:
    """A judge's answer: a level, why, and which judge said it."""

    level: str
    reason: str
    judge_id: str
    judge_version: str


Judge = Callable[[ContextPack], Judgement]

# Mail kind -> level (job mail, ADR 0155).
_RULES: Final[dict[str, str]] = {
    "receipt": "ledger",
    "rejection": "card",
    "job_other": "card_sound",
    "interview": "speak",
    "offer": "speak",
}


def rule_judge_v1(pack: ContextPack) -> Judgement:
    """Allen's job-mail rule table, with stale mail kept to the ledger."""

    def said(level: str, reason: str) -> Judgement:
        return Judgement(level, reason, "rule_judge", "1")

    kind = pack.facts.get("kind")
    if kind not in _RULES:
        return said("ledger", f"no rule for kind {kind!r}")
    age_h = pack.facts.get("age_h")
    if isinstance(age_h, int | float) and age_h > STALE_AFTER_H:
        return said("ledger", "older than 48 hours")
    return said(_RULES[kind], f"rule: {kind} -> {_RULES[kind]}")


def replay(judge: Judge, rows: Iterable[Mapping[str, Any]]) -> list[Judgement]:
    """What ``judge`` would have said about each logged decision (rows with ``pack_json``)."""
    return [judge(ContextPack.from_json(row["pack_json"])) for row in rows]
