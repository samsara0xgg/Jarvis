"""Conservative outcome signal detector for trace feedback.

Scans user utterances for clear approval/disapproval patterns. Designed
to minimize false positives — ambiguous cases return None (NULL outcome).

Phase 3 Step 1 treats NULL as 'unknown' and does not filter on it, so
erring conservative keeps the training pipeline honest.
"""
from __future__ import annotations

import re

# Positive: short approving utterances, no negation
_POSITIVE_PATTERNS = [
    r"^好的?[。!！.]?$",
    r"^(好嘞|行|对|对的|没错|可以|棒|厉害)[。!！.]?$",
    r"^(谢谢|多谢|感谢)(你|啦|了)?[。!！.]?$",
    r"^就是(这样|这个|它)[。!！.]?$",
]

# Negative: clear correction / disagreement
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


def detect_outcome(user_text: str) -> int | None:
    """Return +1 / -1 / None based on conservative pattern match.

    Only fires on short, unambiguous utterances. Longer user utterances
    that *contain* the trigger word (e.g., "谢谢你刚才说的那件事其实...")
    are NOT matched because the patterns are anchored to start/end with
    optional punctuation only.

    Args:
        user_text: Raw utterance from the user.

    Returns:
        +1 if a positive pattern matches, -1 if a negative pattern matches,
        None if the utterance is empty, too long, or ambiguous.
    """
    text = user_text.strip()
    if not text or len(text) > 30:  # long utterances: skip
        return None
    for r in _POS_RE:
        if r.match(text):
            return 1
    for r in _NEG_RE:
        if r.match(text):
            return -1
    return None
