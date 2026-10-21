"""A long text read in parts, or where a question is (ADR 0360, 0363).

A text is cut only between paragraphs; a paragraph too long for one part is cut at its lines,
and a line too long at a space (a run of CJK characters, which has none, at the limit).
:func:`pick` keeps the opening paragraph and the paragraphs that match what he is looking for,
weighted so a rare word counts for more than a common one.

Layer rules: stdlib only.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Final

PART_CHARS: Final = 3500
"""One part of a text, so the whole result stays under about 4,000 characters."""
_PARAGRAPH_BREAK: Final = re.compile(r"\n\s*\n")
_TERM: Final = re.compile(r"[\u3400-\u9fff]+|[^\W_\u3400-\u9fff]+")
_CJK_RUN: Final = re.compile(r"[\u3400-\u9fff]+")
_BREAK_CHARS: Final = 2
"""The blank line between two paragraphs."""


def split(text: str, limit: int = PART_CHARS) -> list[str]:
    """``text``'s paragraphs, none longer than ``limit``."""
    paragraphs: list[str] = []
    for block in _PARAGRAPH_BREAK.split(text.strip()):
        paragraph = block.strip()
        if not paragraph:
            continue
        if len(paragraph) <= limit:
            paragraphs.append(paragraph)
            continue
        current = ""
        for line in paragraph.split("\n"):
            for piece in _pieces(line, limit):
                if current and len(current) + 1 + len(piece) > limit:
                    paragraphs.append(current)
                    current = piece
                else:
                    current = f"{current}\n{piece}" if current else piece
        paragraphs.append(current)
    return paragraphs


def _pieces(line: str, limit: int) -> list[str]:
    """``line`` whole, or cut at spaces into pieces of at most ``limit``."""
    out: list[str] = []
    while len(line) > limit:
        at = line.rfind(" ", 0, limit)
        at = limit if at <= 0 else at
        out.append(line[:at].rstrip())
        line = line[at:].lstrip()
    return [*out, line] if line else out


def parts(text: str, limit: int = PART_CHARS) -> list[str]:
    """``text`` in parts of at most ``limit`` characters, each a run of whole paragraphs."""
    out: list[str] = []
    current = ""
    for paragraph in split(text, limit):
        if current and len(current) + _BREAK_CHARS + len(paragraph) > limit:
            out.append(current)
            current = paragraph
        else:
            current = f"{current}\n\n{paragraph}" if current else paragraph
    return [*out, current] if current else out


def _terms(text: str) -> set[str]:
    """Words lower-cased and without accents, and each pair of adjacent CJK characters.

    A CJK character with no neighbour stands for itself.
    """
    folded = "".join(
        c for c in unicodedata.normalize("NFKD", text).casefold() if not unicodedata.combining(c)
    )
    found: set[str] = set()
    for run in _TERM.findall(folded):
        if _CJK_RUN.fullmatch(run):
            found.update(run[i : i + 2] for i in range(len(run) - 1))
            if len(run) == 1:
                found.add(run)
        elif len(run) > 1:
            found.add(run)
    return found


@dataclass(frozen=True)
class Picked:
    """The paragraphs chosen, in text order, and how many were left out."""

    text: str
    left_out: int


def pick(text: str, looking_for: str, limit: int = PART_CHARS) -> Picked:
    """The opening paragraph, then the paragraphs that match ``looking_for`` best, in text order.

    Whole paragraphs only, to at most ``limit`` characters. A paragraph that matches nothing is
    left out, and so is one that does not fit.
    """
    paragraphs = split(text, limit)
    if not paragraphs:
        return Picked("", 0)
    wanted = _terms(looking_for)
    held = [_terms(one) for one in paragraphs]
    weight = {
        term: math.log(1 + len(paragraphs) / (1 + sum(term in terms for terms in held)))
        for term in wanted
    }
    ranked = sorted(
        ((sum(weight[t] for t in wanted & held[i]), i) for i in range(1, len(paragraphs))),
        key=lambda one: (-one[0], one[1]),
    )
    chosen = {0}
    used = len(paragraphs[0])
    for score, i in ranked:
        if score <= 0:
            break
        if used + _BREAK_CHARS + len(paragraphs[i]) <= limit:
            chosen.add(i)
            used += _BREAK_CHARS + len(paragraphs[i])
    shown = "\n\n".join(paragraphs[i] for i in sorted(chosen))
    return Picked(shown, len(paragraphs) - len(chosen))


__all__ = ["PART_CHARS", "Picked", "parts", "pick", "split"]
