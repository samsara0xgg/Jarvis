"""L3 Tier 0 deterministic pattern table (spec §17).

The closed regex whitelist lives in ``config/tier0_patterns.yaml`` so
Allen can add patterns without touching code. The file is routing data,
NOT a capability grant — three code-side layers still enforce what the
regex_router principal may dispatch (registry ``allowed_callers``
filter, Pre-action Gate ``caller_allowed`` check, L4 dispatch
re-check). Spec anchors: §17 "Tier 0 命中 → 直接 tool_registry.execute",
§3.5.2 "低延迟路径，但仍要过 entity / policy / risk gate", §14.5
"regex_router tiny L0-L1 不升级".

Layer rules: stdlib + ``yaml`` (L3 precedent: ``jarvis.decision.llm``).
No jarvis imports at all — this module is pure data/logic so the
loader/matcher stay trivially unit-testable.
"""  # noqa: RUF002 — fullwidth punctuation is verbatim spec §3.5.2 Chinese quotation.

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

LOGGER = logging.getLogger(__name__)


class Tier0ConfigError(ValueError):
    """Raised when ``config/tier0_patterns.yaml`` is malformed.

    The composition root converts this into ``RuntimeBootstrapError``
    so a broken pattern file fails the daemon start loudly instead of
    silently dropping entries.
    """


@dataclass(frozen=True)
class Tier0Pattern:
    """One compiled whitelist entry (see module docstring for the file schema)."""

    pattern_id: str
    regex: re.Pattern[str]
    tool_name: str
    arg_template: Mapping[str, str] = field(default_factory=dict)
    response_template: str = ""


Tier0Table = tuple[Tier0Pattern, ...]


@dataclass(frozen=True)
class Tier0Hit:
    """A pattern hit with capture-group args already extracted."""

    pattern_id: str
    tool_name: str
    tool_args: Mapping[str, str]
    response_template: str


_GROUP_REF_RE = re.compile(r"^\$(\d+)$")

_REQUIRED_KEYS = ("id", "pattern", "tool", "template")


def load_tier0_table(path: Path) -> Tier0Table:  # noqa: C901, PLR0912 — one linear schema-validation pass; splitting it would scatter the fail-fast messages away from the checks that produce them.
    """Parse + structurally validate the YAML whitelist at ``path``.

    Missing file → empty table (Tier 0 disabled, spec §17 Day-1 state).
    A YAML *syntax* error → :class:`Tier0ConfigError` naming the file
    (PyYAML's own error names no path). Any malformed *entry* →
    :class:`Tier0ConfigError` naming the entry —
    never a silent skip. That includes an ``args`` ``$N`` reference
    outside ``$1..$<group count>``, which would otherwise raise only
    once a live utterance hit the pattern.
    """
    if not path.is_file():
        return ()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        # PyYAML's own marks read ``<unicode string>`` because we hand it
        # text, not a stream — so the path has to come from us or the
        # operator is told the line/column of an unnamed file. The file
        # invites hand edits, making this the likeliest config error.
        msg = f"tier0 patterns: {path} is not valid YAML: {exc}"
        raise Tier0ConfigError(msg) from exc
    if raw is None:
        return ()
    if not isinstance(raw, list):
        msg = f"tier0 patterns: top-level YAML must be a list, got {type(raw).__name__}"
        raise Tier0ConfigError(msg)

    patterns: list[Tier0Pattern] = []
    seen_ids: set[str] = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            msg = f"tier0 patterns: entry #{index} is not a mapping"
            raise Tier0ConfigError(msg)
        missing = [k for k in _REQUIRED_KEYS if not isinstance(entry.get(k), str)]
        if missing:
            entry_id = entry.get("id", f"#{index}")
            msg = f"tier0 patterns: entry {entry_id!r} missing/non-string keys: {missing}"
            raise Tier0ConfigError(msg)
        pattern_id = entry["id"]
        if pattern_id in seen_ids:
            msg = f"tier0 patterns: duplicate id {pattern_id!r}"
            raise Tier0ConfigError(msg)
        seen_ids.add(pattern_id)
        pattern_src = entry["pattern"]
        if not (pattern_src.startswith("^") and pattern_src.endswith("$")):
            msg = (
                f"tier0 patterns: {pattern_id!r} must be full-sentence "
                f"anchored ^...$ (spec §17)"
            )
            raise Tier0ConfigError(msg)
        try:
            regex = re.compile(pattern_src)
        except re.error as exc:
            msg = f"tier0 patterns: {pattern_id!r} regex does not compile: {exc}"
            raise Tier0ConfigError(msg) from exc
        args_raw = entry.get("args", {})
        if not isinstance(args_raw, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in args_raw.items()
        ):
            msg = f"tier0 patterns: {pattern_id!r} args must be a str->str mapping"
            raise Tier0ConfigError(msg)
        for arg_key, arg_value in args_raw.items():
            group_ref = _GROUP_REF_RE.match(arg_value)
            if group_ref is None:
                continue
            group_num = int(group_ref.group(1))
            if not 1 <= group_num <= regex.groups:
                msg = (
                    f"tier0 patterns: {pattern_id!r} arg {arg_key!r} references "
                    f"capture group ${group_num}, but the regex defines "
                    f"{regex.groups} (valid: $1..${regex.groups})"
                )
                raise Tier0ConfigError(msg)
        patterns.append(
            Tier0Pattern(
                pattern_id=pattern_id,
                regex=regex,
                tool_name=entry["tool"],
                arg_template=dict(args_raw),
                response_template=entry["template"],
            )
        )
    return tuple(patterns)


def validate_tier0_table(
    table: Tier0Table,
    *,
    allowed_tool_names: frozenset[str],
    async_tool_names: frozenset[str],
    entity_required_tool_names: frozenset[str] = frozenset(),
    requires_confirmation_tool_names: frozenset[str] = frozenset(),
) -> None:
    """Cross-check the table against the registry's regex_router surface.

    ``allowed_tool_names`` comes from
    ``registry.for_caller(CallerPrincipal.REGEX_ROUTER)`` at the
    composition root; a pattern naming any other tool fails fast here
    (defense layer 0 — the Pre-action Gate would refuse it per-call
    anyway).

    ``entity_required_tool_names`` (ADR-0011 §12.1 item M) is the
    subset of ``allowed_tool_names`` whose ``requires_entity`` is
    True. The Tier 0 dispatch path hardcodes
    ``target_entity_ref=None`` (`jarvis.decision.__init__` — Tier 0
    never runs resolve-on-propose), so a row naming such a tool would
    boot clean and then be refused by the Pre-action Gate's D3 arm at
    EVERY dispatch. Defaulted to an empty frozenset so pre-existing
    callers (and hand-built test fixtures) keep compiling; an empty
    set simply means no row can ever trip this check.

    ``requires_confirmation_tool_names`` (ADR-0012 §3 D5) is the
    subset of ``allowed_tool_names`` whose ``requires_confirmation`` is
    True. Tier 0 has no LLM on this path — nobody to receive Allen's
    「可以」/「不要」 answer — so a row naming such a tool would boot
    clean and then hit ``confirm_required`` at dispatch with no way to
    ever resolve it. This check makes that unreachable at boot time,
    same defense-layer-0 posture as the two checks above; the Tier 0
    dispatch path's own ``gate.outcome != "pass"`` refusal (
    `jarvis.decision.__init__._run_tier0_path`) stays as
    defense-in-depth for a row that somehow slips past this check —
    that branch is the RUNTIME enforcement of this boot rule, not dead
    code. Defaulted to an empty frozenset for the same backward-compat
    reason as ``entity_required_tool_names``.
    """
    for pattern in table:
        if pattern.tool_name not in allowed_tool_names:
            msg = (
                f"tier0 patterns: {pattern.pattern_id!r} targets tool "
                f"{pattern.tool_name!r} which regex_router may not call "
                f"(allowed: {sorted(allowed_tool_names)})"
            )
            raise Tier0ConfigError(msg)
        if pattern.tool_name in async_tool_names:
            msg = (
                f"tier0 patterns: {pattern.pattern_id!r} targets async tool "
                f"{pattern.tool_name!r}; Tier 0 dispatches sync tools only"
            )
            raise Tier0ConfigError(msg)
        if pattern.tool_name in entity_required_tool_names:
            msg = (
                f"tier0 patterns: {pattern.pattern_id!r} targets tool "
                f"{pattern.tool_name!r}, which declares requires_entity=True; "
                "Tier 0 always dispatches with target_entity_ref=None and "
                "would be refused by the Pre-action Gate at every call"
            )
            raise Tier0ConfigError(msg)
        if pattern.tool_name in requires_confirmation_tool_names:
            msg = (
                f"tier0 patterns: {pattern.pattern_id!r} targets tool "
                f"{pattern.tool_name!r}, which declares "
                "requires_confirmation=True; Tier 0 has no LLM to receive "
                "Allen's confirmation answer (ADR-0012 §3 D5) — "
                "confirm_required must never be reachable on this path"
            )
            raise Tier0ConfigError(msg)


def match_tier0(transcript: str, table: Tier0Table) -> Tier0Hit | None:
    """Return the first whitelist hit for ``transcript``, or None.

    First-match-wins in file order (legacy ``regex_router.py``
    semantics); patterns are ``^...$`` anchored so ordering only
    matters for deliberately overlapping entries.
    """
    text = transcript.strip()
    if not text:
        return None
    for pattern in table:
        m = pattern.regex.match(text)
        if m is None:
            continue
        args: dict[str, str] = {}
        for key, value in pattern.arg_template.items():
            group_ref = _GROUP_REF_RE.match(value)
            if group_ref is not None:
                args[key] = (m.group(int(group_ref.group(1))) or "").strip()
            else:
                args[key] = value
        return Tier0Hit(
            pattern_id=pattern.pattern_id,
            tool_name=pattern.tool_name,
            tool_args=args,
            response_template=pattern.response_template,
        )
    return None


def render_tier0_response(hit: Tier0Hit, payload: Mapping[str, Any]) -> str:
    """Fill the hit's template from tool payload scalars + captured args.

    An unusable template must not crash the turn. Every value is
    stringified first, so the reachable ``str.format`` failure family is
    exactly ``KeyError`` (missing variable), ``IndexError`` (positional
    or out-of-range index), ``ValueError`` (malformed spec, unmatched
    brace, bad conversion), ``AttributeError`` (attribute access on a
    stringified value) and ``TypeError`` (non-integer string index). All
    five fall back to a fixed limitation-phrased line (no
    completion-class keywords, so the Pre-emit Gate passes it
    unchanged).
    """
    variables: dict[str, str] = {
        key: str(value) for key, value in payload.items() if isinstance(value, (str, int, float))
    }
    variables.update(hit.tool_args)
    try:
        return hit.response_template.format(**variables)
    except (KeyError, IndexError, ValueError, AttributeError, TypeError) as exc:
        LOGGER.warning(
            "tier0 render: template for %r is unusable: %r",
            hit.pattern_id,
            exc,
        )
        return f"指令 {hit.pattern_id} 已执行，但响应模板变量缺失。"  # noqa: RUF001 — fullwidth comma/period are intentional Chinese punctuation.


__all__ = [
    "Tier0ConfigError",
    "Tier0Hit",
    "Tier0Pattern",
    "Tier0Table",
    "load_tier0_table",
    "match_tier0",
    "render_tier0_response",
    "validate_tier0_table",
]
