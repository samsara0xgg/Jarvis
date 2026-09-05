"""Bounded incremental semantic candidates for ADR-0008 D5.

Candidates are untrusted text, never permits. Unsupported formatting or a
missing safe boundary stops assembly; the caller retains the complete draft
for full-text handling. Already returned candidates are never rewritten.
"""

# ruff: noqa: RUF001 — CJK punctuation is intentionally distinct from ASCII.
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

_SENTENCE_ENDS = frozenset(".!?。！？\n")
_CLAUSE_ENDS = frozenset(",;，；")
_CLOSERS = frozenset('"”’」』）)')
_ABBREVIATIONS = (
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
    "Ph.D.",
    "M.Sc.",
    "B.Sc.",
    "D.Phil.",
    "etc.",
    "Fig.",
    "Eq.",
)
_ABBREVIATION = re.compile(
    r"(?<!\w)(?:(?:[a-z]\.){2,}|" + "|".join(re.escape(word) for word in _ABBREVIATIONS) + r")",
    re.IGNORECASE,
)
_FORMATTING = re.compile(
    r"[`*_~#\[\]{}<>|\\]"  # code/Markdown/JSON/XML are not speech candidates
    r"|(?:^|\n)\s*(?:#{1,6}\s|[-+]\s|\d+[.)]\s)"
    r"|(?:https?:|ftp:|www\.|mailto:|@)",
    re.IGNORECASE,
)
_BARE_DOMAIN = re.compile(
    r"(?<!\w)(?:[\w-]+\.)+(?:[^\W\d_]){2,63}(?=\W|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SemanticCandidate:
    """A stable sentence/subclause, still awaiting the L3 emission gate."""

    text: str
    boundary: Literal["sentence", "subclause", "final"]


def _protected_dots(text: str) -> frozenset[int]:
    positions: set[int] = set()
    for match in _ABBREVIATION.finditer(text):
        positions.update(index for index in range(*match.span()) if text[index] == ".")
    return frozenset(positions)


def _unsupported_syntax(text: str, *, final: bool) -> bool:
    if _FORMATTING.search(text):
        return True
    abbreviations = tuple(_ABBREVIATION.finditer(text))
    for match in _BARE_DOMAIN.finditer(text):
        if any(
            word.start() <= match.start() and word.end() >= match.end() for word in abbreviations
        ):
            continue
        # M.Sc can still become M.Sc.; do not let an incomplete SDK token
        # decide whether a known abbreviation is a domain name.
        if (
            not final
            and match.end() == len(text)
            and any(word.lower().startswith(match.group().lower()) for word in _ABBREVIATIONS)
        ):
            continue
        return True
    return False


def _balanced_prose(text: str) -> bool:
    """Avoid a boundary inside parentheses or an unfinished quotation."""
    stack: list[str] = []
    pairs = {"(": ")", "（": "）", "“": "”", "「": "」", "『": "』"}
    quoted = False
    for char in text:
        if char == '"':
            quoted = not quoted
        elif char in pairs:
            stack.append(pairs[char])
        elif char in pairs.values() and (not stack or stack.pop() != char):
            return False
    return not stack and not quoted


def _boundary_end(text: str, index: int, *, final: bool) -> int | None:
    """Include a punctuation/closing-quote run, waiting for one lookahead."""
    end = index + 1
    while end < len(text) and (text[end] in _CLOSERS or text[end] in _SENTENCE_ENDS):
        end += 1
    if end == len(text) and not final:
        return None
    if text[index] in ".!?" and end < len(text) and not text[end].isspace():
        # Inspect the entire punctuation run: !? can occur together inside
        # an email local-part, so checking only the next character leaks it.
        return None
    return end if _balanced_prose(text[:end]) else None


def _sentence_boundary(
    text: str,
    index: int,
    protected: frozenset[int],
    *,
    final: bool,
) -> int | None:
    if text[index] not in _SENTENCE_ENDS or not text[: index + 1].strip():
        return None
    if text[index] == "." and (
        index in protected
        or text[:index].strip().isdecimal()
        or (index + 1 < len(text) and text[index + 1].isalnum())
    ):
        # The right-hand token can extend a decimal, acronym or URL.
        return None
    return _boundary_end(text, index, final=final)


class SemanticAssembler:
    """Preserve text exactly and stop when no bounded safe candidate exists.

    Speech uses the ADR's initial 60-code-point limit; documents may request
    a larger explicit limit. A forced split chooses punctuation, never an
    arbitrary character offset. The engine must stop feeding this assembler
    once ``blocked_reason`` is set, and buffer/finalize the remaining draft.
    """

    def __init__(self, *, max_candidate_chars: int = 60, max_buffer_chars: int = 2048) -> None:
        """Set positive candidate and pending-text bounds."""
        if not 1 <= max_candidate_chars <= max_buffer_chars:
            message = "candidate bound must be positive and no larger than buffer bound"
            raise ValueError(message)
        self._max_candidate = max_candidate_chars
        self._max_buffer = max_buffer_chars
        self._buffer = ""
        self._closed = False
        self._blocked_reason: str | None = None

    @property
    def pending_text(self) -> str:
        """Return the bounded, uncommitted tail (not the caller's complete draft)."""
        return self._buffer

    @property
    def blocked_reason(self) -> str | None:
        """Return why the rest of this response needs full-text handling."""
        return self._blocked_reason

    def feed(self, delta: str) -> tuple[SemanticCandidate, ...]:
        """Append a model delta and return only complete stable candidates."""
        if self._closed or self._blocked_reason is not None:
            message = "cannot feed a closed or blocked semantic assembler"
            raise RuntimeError(message)
        # Advance the same text prefix regardless of SDK coalescing. In
        # particular syntax blocking and forced-subclause decisions must not
        # change order just because a chunk happens to cross the length cap.
        candidates: list[SemanticCandidate] = []
        for char in delta:
            self._buffer += char
            candidates.extend(self._drain(final=False))
            if len(self._buffer) > self._max_buffer:
                self._buffer = self._buffer[: self._max_buffer]
                self._blocked_reason = "buffer_limit"
                break
            if self.blocked_reason is not None:
                break
        return tuple(candidates)

    def finish(self) -> tuple[SemanticCandidate, ...]:
        """Flush a safe final fragment once; never release an unsupported tail."""
        if self._closed:
            message = "semantic assembler already finished"
            raise RuntimeError(message)
        self._closed = True
        if self._blocked_reason is not None:
            return ()
        return tuple(self._drain(final=True))

    def _next(self, *, final: bool) -> tuple[int, Literal["sentence", "subclause", "final"]] | None:
        protected = _protected_dots(self._buffer)
        clauses: list[int] = []
        for index, char in enumerate(self._buffer):
            if index >= self._max_candidate:
                break
            numeric_comma = (
                char == ","
                and 0 < index < len(self._buffer) - 1
                and self._buffer[index - 1].isdigit()
                and self._buffer[index + 1].isdigit()
            )
            if (
                char in _CLAUSE_ENDS
                and not numeric_comma
                and _balanced_prose(self._buffer[: index + 1])
            ):
                clauses.append(index + 1)
            end = _sentence_boundary(self._buffer, index, protected, final=final)
            if end is not None and end <= self._max_candidate:
                return end, "sentence"
        if len(self._buffer) > self._max_candidate and clauses:
            return clauses[-1], "subclause"
        if final and len(self._buffer) <= self._max_candidate and _balanced_prose(self._buffer):
            return len(self._buffer), "final"
        return None

    def _drain(self, *, final: bool) -> list[SemanticCandidate]:
        candidates: list[SemanticCandidate] = []
        while self._buffer:
            boundary = self._next(final=final)
            if boundary is None:
                if _unsupported_syntax(self._buffer, final=final):
                    self._blocked_reason = "unsupported_speech_syntax"
                elif len(self._buffer) > self._max_candidate:
                    self._blocked_reason = "no_safe_bounded_boundary"
                elif final:
                    self._blocked_reason = "no_safe_final_boundary"
                break
            end, kind = boundary
            text = self._buffer[:end]
            if _unsupported_syntax(text, final=final):
                self._blocked_reason = "unsupported_speech_syntax"
                break
            self._buffer = self._buffer[end:]
            if text.strip():
                candidates.append(SemanticCandidate(text, kind))
            else:
                # Whitespace cannot be a standalone candidate. Retain it until
                # another stable unit can carry it without changing the prefix.
                self._buffer = text + self._buffer
                break
        return candidates
