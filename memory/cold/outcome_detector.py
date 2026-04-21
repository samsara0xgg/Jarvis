"""Outcome signal detector for trace feedback — NLI-based.

DEPRECATED regex layer — kept for historical reference only.

Superseded by NLI-based detection (see memory/cold/nli_classifier.py).
The regex patterns below are no longer called by detect_outcome() as of
2026-04-21. Kept in source for:
  1. rollback path (if NLI model unavailable, can be re-enabled)
  2. test fixture reference
  3. comparison benchmarks
Do NOT extend or modify these patterns for production use.
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memory.cold.nli_classifier import NLIClassifier

LOGGER = logging.getLogger(__name__)

# DEPRECATED — superseded by NLI. Kept for reference only.
_POSITIVE_PATTERNS = [
    r"^好的?[。!！.]?$",
    r"^(好嘞|行|对|对的|没错|可以|棒|厉害)[。!！.]?$",
    r"^(谢谢|多谢|感谢)(你|啦|了)?[。!！.]?$",
    r"^就是(这样|这个|它)[。!！.]?$",
]

# DEPRECATED — superseded by NLI. Kept for reference only.
_NEGATIVE_PATTERNS = [
    r"^不对[。!！.]?$",
    r"^错了?[。!！.]?$",
    r"^(再来|重新|重试)(一?遍|一?次)?[。!！.]?$",
    r"^不是(这样|这个|它|啊)?[。!！.]?$",
    r"^(你)?理解错了?[。!！.]?$",
    r"^(不|别)[。!！.]?$",
]

_POS_RE = [re.compile(p) for p in _POSITIVE_PATTERNS]
_NEG_RE = [re.compile(p) for p in _NEGATIVE_PATTERNS]


def detect_outcome(
    user_text: str,
    nli: "NLIClassifier | None" = None,
) -> int | None:
    """Detect outcome signal via NLI layer.

    Args:
        user_text: user utterance (will be stripped).
        nli: NLIClassifier instance. If None → return None (regex layer is
             deprecated and will not be invoked as fallback).

    Returns:
        +1 (entailment) / -1 (contradiction) / None (ambiguous or no NLI).
    """
    text = user_text.strip()
    if not text or len(text) < 2 or len(text) > 500:
        return None
    if nli is None:
        return None
    try:
        return nli.detect_outcome(text)
    except Exception:
        LOGGER.exception("NLI outcome detection failed, returning None")
        return None


def _detect_regex_only(user_text: str) -> int | None:
    """DEPRECATED: regex-layer detection kept for test fixtures only.

    Production code must use detect_outcome() which goes through NLI.
    """
    text = user_text.strip()
    if not text or len(text) > 30:
        return None
    for r in _POS_RE:
        if r.match(text):
            return 1
    for r in _NEG_RE:
        if r.match(text):
            return -1
    return None
