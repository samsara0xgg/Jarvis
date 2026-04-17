"""TTS text preprocessor.

Strips characters / wrapped content that should not be spoken aloud
(emoji, brackets, parentheses, asterisks, angle brackets). Adapted from
Open-LLM-VTuber (`utils/tts_preprocessor.py`) with two changes:
  - both ASCII and full-width Chinese parentheses are stripped together
  - no translator hook (Jarvis does no auto-translate before TTS)

Each filter is independently toggleable via config so they can be
disabled if a use case actually needs to read those characters aloud.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Mapping

LOGGER = logging.getLogger(__name__)


def clean(text: str, config: Mapping[str, bool] | None = None) -> str:
    """Apply enabled TTS filters to ``text``.

    Args:
        text: Raw text from LLM (may contain emoji, markdown, etc.).
        config: Dict with the five toggles. Missing keys default to True
            (preprocess by default — caller can disable individually).

    Returns:
        Filtered text safe to feed to a TTS engine.

    Each filter swallows its own exceptions and logs a warning, so a
    bad regex on one filter never blocks the whole pipeline.
    """
    if not text:
        return text
    cfg = dict(config) if config else {}
    if cfg.get("ignore_asterisks", True):
        text = _safely(filter_asterisks, text, "asterisks")
    if cfg.get("ignore_brackets", True):
        text = _safely(filter_brackets, text, "brackets")
    if cfg.get("ignore_parentheses", True):
        text = _safely(filter_parentheses, text, "parentheses")
    if cfg.get("ignore_angle_brackets", True):
        text = _safely(filter_angle_brackets, text, "angle_brackets")
    if cfg.get("remove_special_char", True):
        text = _safely(remove_special_characters, text, "special_chars")
    return _collapse_whitespace(text)


def _safely(fn, text: str, label: str) -> str:
    try:
        return fn(text)
    except Exception as exc:
        LOGGER.warning("tts_preprocessor.%s failed: %s; passing through", label, exc)
        return text


def _collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def remove_special_characters(text: str) -> str:
    """Drop emoji and other non-letter/number/punctuation glyphs.

    Lets letters (L*), numbers (N*), punctuation (P*), and whitespace
    pass through. Emoji land in S* (Symbol) categories and are dropped.
    """
    normalized = unicodedata.normalize("NFKC", text)

    def keep(char: str) -> bool:
        cat = unicodedata.category(char)
        return cat[0] in ("L", "N", "P") or char.isspace()

    return "".join(c for c in normalized if keep(c))


def _filter_nested(text: str, pairs: list[tuple[str, str]]) -> str:
    """Strip content enclosed by any of the given delimiter pairs.

    Handles arbitrary nesting per pair (depth counter per opener char).
    Mixed-pair nesting (e.g. ``[(]``) is treated by independent depth
    counters per pair, which is how OLV does it too.
    """
    if not isinstance(text, str):
        raise TypeError("Input must be a string")
    if not text:
        return text

    openers = {p[0]: i for i, p in enumerate(pairs)}
    closers = {p[1]: i for i, p in enumerate(pairs)}
    depths = [0] * len(pairs)
    out: list[str] = []
    for ch in text:
        if ch in openers:
            depths[openers[ch]] += 1
        elif ch in closers:
            idx = closers[ch]
            if depths[idx] > 0:
                depths[idx] -= 1
            else:
                # Unmatched closer — keep it (avoid silently eating user text).
                if all(d == 0 for d in depths):
                    out.append(ch)
        else:
            if all(d == 0 for d in depths):
                out.append(ch)
    return "".join(out)


def filter_brackets(text: str) -> str:
    """Strip content within ASCII square brackets ``[ ]``."""
    return _filter_nested(text, [("[", "]")])


def filter_parentheses(text: str) -> str:
    """Strip content within both ASCII ``( )`` and full-width ``（ ）`` parens."""
    return _filter_nested(text, [("(", ")"), ("（", "）")])


def filter_angle_brackets(text: str) -> str:
    """Strip content within angle brackets ``< >`` (e.g. XML/SSML tags)."""
    return _filter_nested(text, [("<", ">")])


def filter_asterisks(text: str) -> str:
    """Strip content wrapped by 1+ asterisks (markdown emphasis)."""
    return re.sub(r"\*{1,}((?!\*).)*?\*{1,}", "", text)
