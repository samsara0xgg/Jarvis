"""Stateless sentence splitter for ADR-0003 Step 2 chunked emission.

Pure one-shot splitter consumed by ``jarvis.surface.cli_render.render_response``
(Build Step 3) to break a finished LLM response into sentence-sized chunks for
the inherent daemon's WebSocket append stream.

Ports the boundary-detection heuristic from jarvis-legacy/core/llm.py
(``_find_split_point`` + ``_protected_dot_positions``) — same delimiters,
decimal guard, and abbreviation guard — but drops the legacy streaming-buffer
plumbing (``force=True``, ``MAX_SENTENCE_CHARS``, faster-first-response). A1
operates on the full LLM text in one shot, so there is no live stream to
bound and no latency to optimize.

References:
- ADR-0003 § D14 (decision rationale for surface-layer placement)
- jarvis-legacy/core/llm.py:1025-1095 (_find_split_point heuristic)
- jarvis-legacy/core/llm.py:880-898 (_ABBREVIATIONS list)
"""

from __future__ import annotations

from typing import Final

# Boundaries: ASCII + CJK terminators + newline. Legacy adds ``；`` (Chinese
# semicolon) — intentionally excluded here per ADR-0003 D14.
_DELIMITERS: Final[frozenset[str]] = frozenset({".", "!", "?", "。", "！", "？", "\n"})

# Copied verbatim from jarvis-legacy/core/llm.py:880-898. Order matches legacy
# (length-desc-ish) so multi-dot abbreviations are scanned with the same shape.
_ABBREVIATIONS: Final[tuple[str, ...]] = (
    "Mrs.",
    "Prof.",
    "e.g.",
    "i.e.",
    "Mr.",
    "Ms.",
    "Dr.",
    "Jr.",
    "Sr.",
    "St.",
    "Rd.",
    "Inc.",
    "Ltd.",
    "vs.",
)


def split_into_sentences(text: str) -> list[str]:
    """Split *text* into stripped sentence strings.

    Boundaries are ASCII ``.!?``, CJK ``。！？``, and newline. The decimal
    guard keeps ``3.14`` intact; the abbreviation guard keeps ``Dr.``,
    ``e.g.``, etc. intact (case-sensitive, per legacy).

    Returns ``[]`` for empty / whitespace-only input. Returns
    ``[text.strip()]`` when the (stripped) input contains no boundary
    after the guards. Otherwise returns the list of stripped sentences
    with empty intermediates dropped.
    """
    if not text.strip():
        return []

    protected = _protected_dot_positions(text)
    sentences: list[str] = []
    start = 0
    n = len(text)

    for i in range(n):
        ch = text[i]
        if ch not in _DELIMITERS:
            continue
        if ch == "." and _is_protected_dot(text, i, protected):
            continue
        chunk = text[start : i + 1].strip()
        if chunk:
            sentences.append(chunk)
        start = i + 1

    remainder = text[start:].strip()
    if remainder:
        sentences.append(remainder)

    return sentences


def _is_protected_dot(text: str, i: int, protected: frozenset[int]) -> bool:
    """Return True if the ``.`` at index *i* in *text* should not split.

    Two guards:
      - Decimal: digit on both sides (``3.14``). Last-char digit is *not*
        protected — one-shot input means no further chars will arrive.
      - Abbreviation: index appears in the precomputed protected set.
    """
    n = len(text)
    if 0 < i < n - 1 and text[i - 1].isdigit() and text[i + 1].isdigit():
        return True
    return i in protected


def _protected_dot_positions(text: str) -> frozenset[int]:
    """Indices of every ``.`` inside an abbreviation match within *text*.

    Multi-dot abbreviations like ``e.g.`` need every internal dot
    protected — checking only the trailing dot leaves the inner ``.``
    free to split.
    """
    bad: set[int] = set()
    for abbr in _ABBREVIATIONS:
        start = 0
        while True:
            idx = text.find(abbr, start)
            if idx == -1:
                break
            for k, ch in enumerate(abbr):
                if ch == ".":
                    bad.add(idx + k)
            start = idx + 1
    return frozenset(bad)


__all__ = ["split_into_sentences"]
