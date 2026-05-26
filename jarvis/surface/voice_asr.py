"""ADR-0005 §3 + spec §3.6.2 — ASR adapter-internal canonicalization.

Three-layer cascade for misheard speech. Fixes systematic ASR errors
(homophones, near-homophones) for device / scene names without retraining
the model. Applied in order; first layer that *changes* the text returns:

  Layer 1: manual override entries with required-context guard.
  Layer 2: structured alias -> canonical replacement.
  Layer 3: Levenshtein fuzzy fallback, off by default.

Performance budget per call: < 10 ms. Layer 1/2 are O(N*M) string scans;
Layer 3 is O(N*M*W^2) sliding window which is why it ships disabled.

Note: normalizer half — recognizer (engine wiring) added in next commit.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping

LOGGER = logging.getLogger(__name__)

# WP2 T2.1: tightened — bare "灯" was too broad (路灯/灯笼/灯泡 all trigger).
# "暗"/"亮" removed (暗恋/暗号/漂亮 false positives). "打开"/"关闭" added so real
# verbs survive after removing the single-char variants.
_ACTION_WORDS: tuple[str, ...] = (
    "开",
    "关",
    "打开",
    "关闭",
    "调",
    "模式",
    "切换",
    "启动",
    "场景",
)


class AsrNormalizer:
    """Three-layer normalizer for ASR transcripts."""

    def __init__(
        self,
        *,
        corrections: list[dict[str, object]],
        aliases: Mapping[str, Iterable[str]],
        fuzzy_enabled: bool,
        fuzzy_max_distance: int = 2,
    ) -> None:
        """Build a normalizer from already-validated kwargs."""
        self._corrections: list[dict[str, object]] = [
            entry for entry in corrections if isinstance(entry, dict)
        ]
        self._aliases: list[tuple[str, str]] = self._flatten_aliases(aliases)
        # Preserve canonical names even when they only appear as their own
        # alias — Layer 2 skips identity rewrites but Layer 3 still needs to
        # treat the canonical itself as a fuzzy target.
        self._canonicals: tuple[str, ...] = tuple(
            c for c in aliases if isinstance(c, str) and c
        )
        self._fuzzy_enabled = bool(fuzzy_enabled)
        self._fuzzy_max_distance = int(fuzzy_max_distance)

        # Targets for Layer 3: canonical names plus all aliases (each
        # alias maps to its canonical, canonicals map to themselves).
        self._fuzzy_targets: dict[str, str] = self._build_fuzzy_targets()

    @classmethod
    def from_config(cls, config: Mapping[str, object]) -> AsrNormalizer:
        """Build from a legacy ``config.yaml``-style mapping.

        Recognised sections:
          - ``asr_corrections``: list of {pattern, replace, require_context}
          - ``asr_aliases``: dict of canonical_name -> list of aliases
          - ``asr_normalizer_fuzzy``: {enabled, max_distance}
        """
        raw_corrections = config.get("asr_corrections") or []
        corrections: list[dict[str, object]] = []
        if isinstance(raw_corrections, list):
            corrections = [
                entry for entry in raw_corrections if isinstance(entry, dict)
            ]
        raw_aliases = config.get("asr_aliases") or {}
        aliases: Mapping[str, Iterable[str]] = (
            raw_aliases if isinstance(raw_aliases, Mapping) else {}
        )
        fuzzy_cfg_raw = config.get("asr_normalizer_fuzzy") or {}
        fuzzy_cfg: Mapping[str, object] = (
            fuzzy_cfg_raw if isinstance(fuzzy_cfg_raw, Mapping) else {}
        )
        max_distance_raw = fuzzy_cfg.get("max_distance", 2)
        max_distance = (
            int(max_distance_raw) if isinstance(max_distance_raw, (int, str)) else 2
        )
        return cls(
            corrections=corrections,
            aliases=aliases,
            fuzzy_enabled=bool(fuzzy_cfg.get("enabled", False)),
            fuzzy_max_distance=max_distance,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def normalize(self, text: str) -> str:
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
            ctx_raw = entry.get("require_context") or []
            ctx: list[str] = (
                [c for c in ctx_raw if isinstance(c, str)]
                if isinstance(ctx_raw, list)
                else []
            )
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
        for canonical in self._canonicals:
            targets.setdefault(canonical, canonical)
        return targets

    @staticmethod
    def _action_word_positions(text: str) -> set[int]:
        """Return all character positions covered by an action word in *text*.

        This lets the fuzzy loop skip windows that overlap with action verb
        spans, preventing e.g. the suffix "大" in "打开大蛋灯" from being
        included in the fuzzy candidate window "开大".
        """
        covered: set[int] = set()
        for w in _ACTION_WORDS:
            start = 0
            while True:
                idx = text.find(w, start)
                if idx == -1:
                    break
                for k in range(idx, idx + len(w)):
                    covered.add(k)
                start = idx + 1
        return covered

    def _apply_fuzzy(self, text: str) -> str:
        # Only fire when an action word is present — without one, a 2-char
        # window matching "客厅" by accident would corrupt unrelated text.
        if not any(w in text for w in _ACTION_WORDS):
            return text
        if not self._fuzzy_targets:
            return text

        # T2.1 guard: precompute positions covered by action words so we can
        # skip windows that overlap with them (action verbs must not be
        # fuzzy-replaced with device names).
        action_positions = self._action_word_positions(text)

        n = len(text)
        for window_size in range(2, min(6, n + 1)):
            for i in range(n - window_size + 1):
                # Skip if window overlaps any action-word position.
                if action_positions.intersection(range(i, i + window_size)):
                    continue
                window = text[i : i + window_size]
                # Skip windows that already match exactly — Layer 2 should
                # have caught those already; redoing here just wastes work.
                if window in self._fuzzy_targets:
                    continue
                for cand, canonical in self._fuzzy_targets.items():
                    # T2.2: strict length match — window must equal alias length.
                    # This avoids 2-char windows matching 4-char aliases and
                    # corrupting text by expanding position-wise. Canonical CAN
                    # be longer than alias (that's the point: users say short,
                    # system fills in the full name).
                    if len(cand) != window_size:
                        continue
                    d = _levenshtein(window, cand)
                    if d <= self._fuzzy_max_distance:
                        return text[:i] + canonical + text[i + window_size :]
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


__all__ = ["AsrNormalizer"]
