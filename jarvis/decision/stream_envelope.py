"""L3 streaming ``<voice>``/``<document>`` envelope: voice flows, document waits.

The prompt makes the model wrap its answer; the assembler must never see a tag
(it blocks on ``<``), and a spoken chunk must never carry one. Tag-less output
is voice and document at once, the same rule L5 applies to a complete text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

_VOICE_OPEN = "<voice>"
_VOICE_CLOSE = "</voice>"
_DOCUMENT_OPEN = "<document>"
_DOCUMENT_CLOSE = "</document>"
_ENVELOPE_RE = re.compile(
    r"<(?P<tag>voice|document)>\s*(?P<body>.*?)\s*</(?P=tag)>",
    re.IGNORECASE | re.DOTALL,
)

type _State = Literal["undecided", "plain", "voice", "after_voice", "document", "done"]


@dataclass(frozen=True)
class EnvelopeTail:
    """What remained once the stream ended: held voice, the document, the shape."""

    voice_tail: str
    document: str
    enveloped: bool


def split_envelope(text: str) -> tuple[str, str, bool]:
    """Return ``(voice, document, enveloped)`` for a complete text, L5's rule."""
    values: dict[str, str] = {}
    for match in _ENVELOPE_RE.finditer(text):
        values.setdefault(match.group("tag").lower(), match.group("body").strip())
    if not values:
        stripped = text.strip()
        return stripped, stripped, False
    return values.get("voice", ""), values.get("document", ""), True


def compose_envelope(voice: str, document: str) -> str:
    """Wrap voice and document the way the prompt asks the model to."""
    return (
        f"{_VOICE_OPEN}\n{voice}\n{_VOICE_CLOSE}\n{_DOCUMENT_OPEN}\n{document}\n{_DOCUMENT_CLOSE}"
    )


def envelope_only(text: str) -> str:
    """Return the envelope alone; a tag-less text, or one with nothing outside, as is.

    A model that thinks aloud before ``<voice>`` puts that reasoning in
    ``ResponsePlan.text`` (memory.db, the next brief) while the channels stay
    clean; text outside both bodies has no channel and is dropped here.
    """
    voice, document, enveloped = split_envelope(text)
    if not enveloped or not _ENVELOPE_RE.sub("", text).strip():
        return text
    return compose_envelope(voice, document)


def _could_open_tag(lower: str) -> bool:
    return any(
        len(lower) < len(tag) and tag.startswith(lower) for tag in (_VOICE_OPEN, _DOCUMENT_OPEN)
    )


def _hold_index(text: str, closing_tag: str | None) -> int:
    """Index from which ``text`` is not yet safe: a possible tag start or trailing space."""
    cut = len(text)
    if closing_tag is not None:
        for index in range(max(0, len(text) - len(closing_tag) + 1), len(text)):
            if text[index] == "<" and closing_tag.startswith(text[index:].lower()):
                cut = index
                break
    while cut > 0 and text[cut - 1].isspace():
        cut -= 1
    return cut


class StreamEnvelopeSplitter:
    """Feed raw deltas; receive only voice text that can no longer be a tag."""

    def __init__(self) -> None:
        """Start before the first non-whitespace character decides the shape."""
        self._state: _State = "undecided"
        self._held = ""
        self._document: list[str] = []
        self._finished = False

    @property
    def enveloped(self) -> bool | None:
        """Return the decided shape, ``None`` until the first real character."""
        if self._state == "undecided":
            return None
        return self._state != "plain"

    def feed(self, delta: str) -> str:  # noqa: C901, PLR0912 - one explicit tag state machine
        """Append a delta and return the voice text now safe to assemble."""
        if self._finished:
            message = "cannot feed a finished envelope splitter"
            raise RuntimeError(message)
        out: list[str] = []
        text = self._held + delta
        self._held = ""
        while text:
            if self._state == "undecided":
                stripped = text.lstrip()
                lower = stripped.lower()
                if not stripped or _could_open_tag(lower):
                    self._held = stripped
                    break
                if lower.startswith(_VOICE_OPEN):
                    self._state, text = "voice", stripped[len(_VOICE_OPEN) :].lstrip()
                elif lower.startswith(_DOCUMENT_OPEN):
                    self._state, text = "document", stripped[len(_DOCUMENT_OPEN) :]
                else:
                    self._state, text = "plain", stripped
            elif self._state in {"plain", "voice"}:
                closing = _VOICE_CLOSE if self._state == "voice" else None
                index = text.lower().find(closing) if closing is not None else -1
                if index >= 0:
                    out.append(text[:index].rstrip())
                    self._state, text = "after_voice", text[index + len(_VOICE_CLOSE) :]
                    continue
                cut = _hold_index(text, closing)
                out.append(text[:cut])
                self._held, text = text[cut:], ""
            elif self._state == "after_voice":
                stripped = text.lstrip()
                lower = stripped.lower()
                if lower.startswith(_DOCUMENT_OPEN):
                    self._state, text = "document", stripped[len(_DOCUMENT_OPEN) :]
                elif not stripped or _could_open_tag(lower):
                    self._held, text = stripped, ""
                else:
                    # Text outside both bodies has no channel; scan on for the tag.
                    text = stripped[1:]
            elif self._state == "document":
                index = text.lower().find(_DOCUMENT_CLOSE)
                if index >= 0:
                    self._document.append(text[:index])
                    self._state, text = "done", ""
                    continue
                cut = _hold_index(text, _DOCUMENT_CLOSE)
                self._document.append(text[:cut])
                self._held, text = text[cut:], ""
            else:
                text = ""
        return "".join(out)

    def finish(self) -> EnvelopeTail:
        """Close the stream; a held partial tag at the end is plain voice text."""
        if self._finished:
            message = "envelope splitter already finished"
            raise RuntimeError(message)
        self._finished = True
        held, self._held = self._held.strip(), ""
        voice_tail = held if self._state in {"undecided", "plain", "voice"} else ""
        if self._state == "document":
            self._document.append(held)
        return EnvelopeTail(
            voice_tail=voice_tail,
            document="".join(self._document).strip(),
            enveloped=self._state not in {"undecided", "plain"},
        )
