"""Stray HTML in her answer text: ``<br>`` becomes a newline, other tags are dropped.

The model sometimes writes ``<br>``, ``<p>`` or ``<b>`` in a reply, and a tag shown or kept as
written reads wrong on every surface. Only a short list of HTML tag names counts: ``<voice>`` and
``<document>`` (the program's envelope), ``a < b``, ``<3`` and code in backticks are left as they
are. The text between a dropped tag's two halves stays.

Layer rules: stdlib only.
"""

from __future__ import annotations

import re
from typing import Final

_NAMES: Final = r"(?:p|b|i|u|em|strong|span|div|ul|ol|li|h[1-6]|a|hr|font)"
_BREAK: Final = r"<br(?:\s[^<>\n]*)?/?>"
_OTHER: Final = rf"</?{_NAMES}(?:\s[^<>\n]*)?/?>"
_CODE: Final = r"```.*?```|`[^`\n]*`"
_TAG_RE: Final = re.compile(
    rf"({_CODE})|({_BREAK})|({_OTHER})", re.IGNORECASE | re.DOTALL,
)
# The end of a piece that may be the start of a tag: ``<``, ``</`` or ``<`` and a letter.
_PARTIAL_RE: Final = re.compile(r"<(?:/?(?:[A-Za-z][^<>\n]{0,80})?)?\Z")


def strip_html(text: str) -> str:
    """``text`` with ``<br>`` as a newline and the other HTML tags dropped, code left alone."""
    if "<" not in text:
        return text
    return _TAG_RE.sub(lambda m: m.group(1) or ("\n" if m.group(2) else ""), text)


class HtmlTagFilter:
    """Feed raw deltas; receive the text with its HTML tags dealt with, a cut tag held back."""

    def __init__(self) -> None:
        """Start with nothing held."""
        self._held = ""

    def feed(self, delta: str) -> str:
        """Append a delta; return the text now safe to use."""
        text = self._held + delta
        self._held = ""
        partial = _PARTIAL_RE.search(text)
        if partial is not None:
            text, self._held = text[: partial.start()], text[partial.start() :]
        # ponytail: a code fence cut across two deltas is not seen as code; spoken text has none.
        return strip_html(text)

    def finish(self) -> str:
        """End of the text: what is still held was no tag, so it is given back as written."""
        held, self._held = self._held, ""
        return held
