"""L3 confirmation-answer grammar — deterministic yes/no whitelist (ADR-0012 D6).

Exact-sentence match against ``config/confirm_grammar.yaml``. Mirrors
:mod:`jarvis.decision.tier0`'s data-driven shape and normalization
tolerance (whitespace / ASCII case / trailing punctuation / ASR
spelling) — zero generalization: every rule matches ONE literal spoken
sentence (the ADR's frozen yes-set / no-set v1), full-sentence anchored
(``^...$``). A paraphrase ("行吧那就写进去吧") is never a hit by
construction — it is ordinary conversation the LLM sees via
``format_pending_confirmation_note``, never something this table can be
generalized to accept.

This table's ONLY consumer is the grammar hook in
:func:`jarvis.decision._handle_utterance`, which runs BEFORE
``tier_0_match`` and, on a hit, returns without ever invoking the LLM
this turn — see that call site's docstring for the load-bearing
invariant (ADR D6): no LLM output can mint an ``AuthorizationLease``,
only a hit against this table can.

Layer rules: stdlib + ``yaml`` (same L3 precedent as
:mod:`jarvis.decision.tier0`). No jarvis imports at all — pure
data/logic, trivially unit-testable in isolation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import yaml

if TYPE_CHECKING:
    from pathlib import Path


class ConfirmGrammarConfigError(ValueError):
    """Raised when ``config/confirm_grammar.yaml`` is malformed.

    The composition root converts this into ``RuntimeBootstrapError`` —
    same posture as :class:`jarvis.decision.tier0.Tier0ConfigError`.
    """


ConfirmDecision = Literal["yes", "no"]
"""The only two outcomes a grammar rule can carry (ADR-0012 D6)."""


@dataclass(frozen=True)
class ConfirmGrammarRule:
    """One compiled grammar entry (see module docstring for the file schema)."""

    rule_id: str
    regex: re.Pattern[str]
    decision: ConfirmDecision


ConfirmGrammarTable = tuple[ConfirmGrammarRule, ...]


@dataclass(frozen=True)
class ConfirmGrammarHit:
    """Which rule fired and what it decided."""

    rule_id: str
    decision: ConfirmDecision


_REQUIRED_KEYS = ("id", "pattern", "decision")
_VALID_DECISIONS = ("yes", "no")


def load_confirm_grammar(path: Path) -> ConfirmGrammarTable:  # noqa: C901 — one linear schema-validation pass, mirrors `jarvis.decision.tier0.load_tier0_table`'s own noqa'd shape; splitting would scatter the fail-fast messages away from the checks that produce them.
    """Parse + structurally validate the YAML grammar table at ``path``.

    Missing file -> empty table (the answer-path grammar hook never
    fires; every utterance falls through to ordinary Tier 0 / Tier 2
    handling — same "off means inert, not broken" posture as
    :func:`jarvis.decision.tier0.load_tier0_table`). A YAML syntax
    error, or any malformed entry, raises
    :class:`ConfirmGrammarConfigError` naming the problem — never a
    silent skip.
    """
    if not path.is_file():
        return ()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        msg = f"confirm grammar: {path} is not valid YAML: {exc}"
        raise ConfirmGrammarConfigError(msg) from exc
    if raw is None:
        return ()
    if not isinstance(raw, list):
        msg = f"confirm grammar: top-level YAML must be a list, got {type(raw).__name__}"
        raise ConfirmGrammarConfigError(msg)

    rules: list[ConfirmGrammarRule] = []
    seen_ids: set[str] = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            msg = f"confirm grammar: entry #{index} is not a mapping"
            raise ConfirmGrammarConfigError(msg)
        missing = [k for k in _REQUIRED_KEYS if not isinstance(entry.get(k), str)]
        if missing:
            entry_id = entry.get("id", f"#{index}")
            msg = f"confirm grammar: entry {entry_id!r} missing/non-string keys: {missing}"
            raise ConfirmGrammarConfigError(msg)
        rule_id = entry["id"]
        if rule_id in seen_ids:
            msg = f"confirm grammar: duplicate id {rule_id!r}"
            raise ConfirmGrammarConfigError(msg)
        seen_ids.add(rule_id)
        decision = entry["decision"]
        if decision not in _VALID_DECISIONS:
            msg = (
                f"confirm grammar: {rule_id!r} decision must be one of "
                f"{_VALID_DECISIONS!r}, got {decision!r}"
            )
            raise ConfirmGrammarConfigError(msg)
        pattern_src = entry["pattern"]
        if not (pattern_src.startswith("^") and pattern_src.endswith("$")):
            msg = (
                f"confirm grammar: {rule_id!r} must be full-sentence anchored "
                f"^...$ (zero generalization, ADR-0012 D6)"
            )
            raise ConfirmGrammarConfigError(msg)
        try:
            regex = re.compile(pattern_src)
        except re.error as exc:
            msg = f"confirm grammar: {rule_id!r} regex does not compile: {exc}"
            raise ConfirmGrammarConfigError(msg) from exc
        rules.append(ConfirmGrammarRule(rule_id=rule_id, regex=regex, decision=decision))
    return tuple(rules)


def match_confirm_grammar(
    transcript: str,
    table: ConfirmGrammarTable,
) -> ConfirmGrammarHit | None:
    """Return the first grammar hit for ``transcript``, or None.

    First-match-wins in file order (mirrors
    :func:`jarvis.decision.tier0.match_tier0`). Every pattern is
    ``^...$`` anchored around one exact literal — no substring or
    paraphrase tolerance; ordering only matters for deliberately
    overlapping rows (none in v1: the yes-set / no-set tokens are all
    mutually distinct full strings).
    """
    text = transcript.strip()
    if not text:
        return None
    for rule in table:
        if rule.regex.match(text) is not None:
            return ConfirmGrammarHit(rule_id=rule.rule_id, decision=rule.decision)
    return None


__all__ = [
    "ConfirmDecision",
    "ConfirmGrammarConfigError",
    "ConfirmGrammarHit",
    "ConfirmGrammarRule",
    "ConfirmGrammarTable",
    "load_confirm_grammar",
    "match_confirm_grammar",
]
