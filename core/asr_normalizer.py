"""ASR text normalizer — three-layer cascade for misheard speech.

Used to fix systematic ASR errors (homophones, near-homophones) for
device / scene names without retraining the ASR model. Three layers,
applied in order; the first one that *changes* the text returns:

  Layer 1: manual override entries with required-context guard.
  Layer 2: structured alias → canonical replacement (no guard needed).
  Layer 3: Levenshtein fuzzy fallback, off by default.

Config sections (all top-level in ``config.yaml``):
  - ``asr_corrections``: list of {pattern, replace, require_context}
  - ``asr_aliases``: dict of canonical_name → list of aliases
  - ``asr_normalizer_fuzzy``: {enabled, max_distance}

Performance budget: < 10ms per call. Layer 1/2 are O(N*M) string scans;
Layer 3 is O(N*M*W²) sliding window which is why it ships disabled.
"""

from __future__ import annotations

import logging
from typing import Iterable, Mapping

LOGGER = logging.getLogger(__name__)

_ACTION_WORDS: tuple[str, ...] = (
    "开", "关", "调", "亮", "暗", "模式", "灯", "切换", "启动",
)


class ASRNormalizer:
    """Three-layer normalizer for ASR transcripts."""

    def __init__(self, config: Mapping[str, object]) -> None:
        raw_corrections = config.get("asr_corrections") or []
        self._corrections: list[dict] = [
            entry for entry in raw_corrections if isinstance(entry, dict)
        ]

        raw_aliases = config.get("asr_aliases") or {}
        self._aliases: list[tuple[str, str]] = self._flatten_aliases(raw_aliases)

        fuzzy_cfg = config.get("asr_normalizer_fuzzy") or {}
        if not isinstance(fuzzy_cfg, Mapping):
            fuzzy_cfg = {}
        self._fuzzy_enabled = bool(fuzzy_cfg.get("enabled", False))
        self._fuzzy_max_distance = int(fuzzy_cfg.get("max_distance", 2))

        # Targets for Layer 3: canonical names plus all aliases (each
        # alias maps to its canonical, canonicals map to themselves).
        self._fuzzy_targets: dict[str, str] = self._build_fuzzy_targets()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def normalize(self, text: str, intent_hint: str | None = None) -> str:
        """Apply the three layers in order; return on the first change."""
        if not text:
            return text

        layered = self._apply_corrections(text)
        if layered != text:
            return layered

        layered = self._apply_aliases(text)
        if layered != text:
            return layered

        if self._fuzzy_enabled:
            layered = self._apply_fuzzy(text)
            if layered != text:
                return layered

        return text

    # ------------------------------------------------------------------
    # Layer 1: manual corrections with require_context guard
    # ------------------------------------------------------------------

    def _apply_corrections(self, text: str) -> str:
        changed = text
        for entry in self._corrections:
            pattern = str(entry.get("pattern", ""))
            replace = str(entry.get("replace", ""))
            ctx = entry.get("require_context") or []
            if not pattern or not replace or not ctx:
                # require_context is mandatory — silently skip malformed entries
                # (logging here would spam every turn).
                continue
            if pattern not in changed:
                continue
            if not any(c in changed for c in ctx):
                continue
            changed = changed.replace(pattern, replace)
        return changed

    # ------------------------------------------------------------------
    # Layer 2: structured aliases
    # ------------------------------------------------------------------

    @staticmethod
    def _flatten_aliases(
        raw: Mapping[str, Iterable[str]],
    ) -> list[tuple[str, str]]:
        """Flatten {canonical: [alias, ...]} to a length-desc sorted list.

        Sorting longest-first prevents short aliases ("灯") from rewriting
        substrings of longer aliases ("床头灯") before the longer one
        gets its chance.
        """
        flat: list[tuple[str, str]] = []
        for canonical, aliases in raw.items():
            if not isinstance(canonical, str) or not aliases:
                continue
            for alias in aliases:
                if not isinstance(alias, str) or not alias:
                    continue
                if alias == canonical:
                    continue
                flat.append((alias, canonical))
        flat.sort(key=lambda pair: len(pair[0]), reverse=True)
        return flat

    def _apply_aliases(self, text: str) -> str:
        # Return on first hit (longest alias wins via the length-desc sort).
        # Iterating after a hit risks chained replacements where the canonical
        # we just inserted gets eaten by a shorter later alias rule.
        for alias, canonical in self._aliases:
            if alias in text:
                return text.replace(alias, canonical)
        return text

    # ------------------------------------------------------------------
    # Layer 3: Levenshtein fuzzy fallback
    # ------------------------------------------------------------------

    def _build_fuzzy_targets(self) -> dict[str, str]:
        """Map every canonical+alias string to its canonical form."""
        targets: dict[str, str] = {}
        for alias, canonical in self._aliases:
            targets[alias] = canonical
            targets[canonical] = canonical
        return targets

    def _apply_fuzzy(self, text: str) -> str:
        # Only fire when an action word is present — without one, a 2-char
        # window matching "客厅" by accident would corrupt unrelated text.
        if not any(w in text for w in _ACTION_WORDS):
            return text
        if not self._fuzzy_targets:
            return text

        n = len(text)
        for window_size in range(2, min(6, n + 1)):
            for i in range(n - window_size + 1):
                window = text[i: i + window_size]
                # Skip windows that already match exactly — Layer 2 should
                # have caught those already; redoing here just wastes work.
                if window in self._fuzzy_targets:
                    continue
                for cand, canonical in self._fuzzy_targets.items():
                    if abs(len(cand) - len(window)) > self._fuzzy_max_distance:
                        continue
                    d = _levenshtein(window, cand)
                    if d <= self._fuzzy_max_distance:
                        return text[:i] + canonical + text[i + window_size:]
        return text


def _levenshtein(a: str, b: str) -> int:
    """Standard DP Levenshtein distance. Hand-rolled to avoid a new dep."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            curr[j] = min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[-1]
