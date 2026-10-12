"""What counts as one emoji (ADR 0256): a reaction is exactly one grapheme cluster of one.

The standard library has no Unicode emoji property, so the ranges below are the
``Extended_Pictographic`` set of Unicode 15 (its ``emoji-data.txt``), with the sequences that
build one cluster out of several code points: a variation selector or skin tone, a joiner
(family, couple, profession), a keycap, a flag and a tag flag.

ponytail: the ranges are Unicode 15; an emoji added later is refused until they are extended.
A regional-indicator pair is accepted whether or not it is a country.
"""

from __future__ import annotations

import re
from typing import Final

_PICTOGRAPHIC: Final = (
    "\u00a9\u00ae\u203c\u2049\u2122\u2139\u2194-\u2199\u21a9-\u21aa\u231a-\u231b\u2328\u2388"
    "\u23cf\u23e9-\u23f3\u23f8-\u23fa\u24c2\u25aa-\u25ab\u25b6\u25c0\u25fb-\u25fe\u2600-\u2605"
    "\u2607-\u2612\u2614-\u2685\u2690-\u2705\u2708-\u2712\u2714\u2716\u271d\u2721\u2728"
    "\u2733-\u2734\u2744\u2747\u274c\u274e\u2753-\u2755\u2757\u2763-\u2767\u2795-\u2797\u27a1"
    "\u27b0\u27bf\u2934-\u2935\u2b05-\u2b07\u2b1b-\u2b1c\u2b50\u2b55"
    # the wavy dash, part alternation mark and the circled secret and congratulations: in the
    # CJK block, so written as numbers
    f"{chr(0x3030)}{chr(0x303D)}{chr(0x3297)}{chr(0x3299)}"
    "\U0001f000-\U0001f0ff\U0001f10d-\U0001f10f\U0001f12f\U0001f16c-\U0001f171"
    "\U0001f17e-\U0001f17f\U0001f18e\U0001f191-\U0001f19a\U0001f1ad-\U0001f1e5"
    "\U0001f201-\U0001f20f\U0001f21a\U0001f22f\U0001f232-\U0001f23a\U0001f23c-\U0001f23f"
    "\U0001f249-\U0001f3fa\U0001f400-\U0001f53d\U0001f546-\U0001f64f\U0001f680-\U0001f6ff"
    "\U0001f774-\U0001f77f\U0001f7d5-\U0001f7ff\U0001f80c-\U0001f80f\U0001f848-\U0001f84f"
    "\U0001f85a-\U0001f85f\U0001f888-\U0001f88f\U0001f8ae-\U0001f8ff\U0001f90c-\U0001f93a"
    "\U0001f93c-\U0001f945\U0001f947-\U0001faff\U0001fc00-\U0001fffd"
)
_PART: Final = rf"[{_PICTOGRAPHIC}][\ufe0f\U0001f3fb-\U0001f3ff]?"
_ONE_EMOJI: Final = re.compile(
    rf"{_PART}(?:\u200d{_PART})*"
    r"|[0-9#*]\ufe0f?\u20e3"
    r"|[\U0001f1e6-\U0001f1ff]{2}"
    r"|\U0001f3f4[\U000e0020-\U000e007e]+\U000e007f",
)


def is_single_emoji(text: str) -> bool:
    """Whether ``text`` is exactly one emoji and nothing else: no space, no second emoji."""
    return _ONE_EMOJI.fullmatch(text) is not None
