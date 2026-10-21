"""L3 ``<react>EMOJI</react>``: her reaction to his message, cut out of the reply (ADR 0257).

The tag may stand anywhere in a reply and must never reach speech or the screen. This filter takes
the reply's text in pieces, as it streams, returns it without the tag, and keeps the first valid
emoji. A tag that is cut by a piece boundary is held until it can be told from text; one that
never closes, or holds more than an emoji can, is text after all and is given back as written.

The same filter cuts a ``[Receipts: ...]`` line she copies from the history (ADR 0319): the
program writes that line, so it is no part of what she says, spoken, streamed or kept.

And it cuts the ``<claims>`` block every answer opens with (ADR 0320): the first closed block is
kept as ``claims`` for the check, and a block left unclosed is cut to the end of the reply and
counts as no block.
"""

from __future__ import annotations

import re
from typing import Final

from jarvis.shared.emoji import is_single_emoji

_OPEN: Final = "<react>"
_CLOSE: Final = "</react>"
_OPEN_RE: Final = re.compile(re.escape(_OPEN), re.IGNORECASE)
_CLOSE_RE: Final = re.compile(re.escape(_CLOSE), re.IGNORECASE)
_MAX_BODY: Final = 40
"""The longest tag body held back: a family or a flag-with-tags is under 30 characters."""
_RECEIPTS_OPEN: Final = "[Receipts: "
_RECEIPTS_MAX: Final = 4000
"""The longest ``[Receipts: ...]`` body held back; a longer one is text after all."""


class _ReceiptsFilter:
    """Cut ``[Receipts: ...]`` from text fed in pieces, holding a start the piece ends in."""

    def __init__(self) -> None:
        self._held = ""

    def feed(self, delta: str) -> str:
        text = self._held + delta
        self._held = ""
        out: list[str] = []
        while text:
            start = text.find(_RECEIPTS_OPEN)
            if start < 0:
                cut = _partial(text, _RECEIPTS_OPEN)
                out.append(text[:cut])
                self._held = text[cut:]
                break
            out.append(text[:start])
            body = text[start + len(_RECEIPTS_OPEN) :]
            end, newline = body.find("]"), body.find("\n")
            if 0 <= end <= _RECEIPTS_MAX and (newline < 0 or end < newline):
                text = body[end + 1 :]  # the line is cut out
            elif newline >= 0:
                out.append(_RECEIPTS_OPEN + body[: newline + 1])  # a line break: it was text
                text = body[newline + 1 :]
            elif len(body) > _RECEIPTS_MAX:
                out.append(_RECEIPTS_OPEN)
                text = body
            else:
                self._held = text[start:]
                break
        return "".join(out)

    def finish(self) -> str:
        """End of the reply: a start that never closed is text."""
        held, self._held = self._held, ""
        return held


_CLAIMS_OPEN: Final = "<claims>"
_CLAIMS_OPEN_RE: Final = re.compile(re.escape(_CLAIMS_OPEN), re.IGNORECASE)
_CLAIMS_CLOSE_RE: Final = re.compile(re.escape("</claims>"), re.IGNORECASE)


class _ClaimsFilter:
    """Cut ``<claims>...</claims>`` from text fed in pieces; keep the first closed block's body."""

    def __init__(self) -> None:
        self.closed = False  # a block has closed: ``body`` is final
        self.body = ""
        self._held = ""
        self._inside: str | None = None  # the text since an open tag, while inside a block

    def feed(self, delta: str) -> str:
        text = self._held + delta
        self._held = ""
        out: list[str] = []
        while text:
            if self._inside is None:
                found = _CLAIMS_OPEN_RE.search(text)
                if found is not None:
                    out.append(text[: found.start()])
                    text, self._inside = text[found.end() :], ""
                    continue
                cut = _partial(text.lower(), _CLAIMS_OPEN)
                out.append(text[:cut])
                self._held, text = text[cut:], ""
                continue
            self._inside += text
            text = ""
            closed = _CLAIMS_CLOSE_RE.search(self._inside)
            if closed is not None:
                if not self.closed:
                    self.closed, self.body = True, self._inside[: closed.start()].strip()
                text, self._inside = self._inside[closed.end() :], None
        return "".join(out)

    def finish(self) -> str:
        """End of the reply: a block left open is cut with no body; a held start is text."""
        held, self._held, self._inside = self._held, "", None
        return held


class ReactionFilter:
    """Feed raw deltas; receive the text without ``<react>``, and ``emoji`` once one is seen."""

    def __init__(self) -> None:
        """Start outside a tag, with no reaction."""
        self.emoji: str | None = None
        self._held = ""
        self._body: str | None = None  # the text since an open tag, while inside one
        self._receipts = _ReceiptsFilter()
        self._claims = _ClaimsFilter()

    @property
    def block_closed(self) -> bool:
        """Whether the reply's claims block has closed (ADR 0320)."""
        return self._claims.closed

    @property
    def claims(self) -> str | None:
        """The body of the first closed ``<claims>`` block, else None (no block, or unclosed)."""
        return self._claims.body if self._claims.closed else None

    def feed(self, delta: str) -> str:
        """Append a delta; return the text now safe to use."""
        return self._receipts.feed(self._cut_tag(self._claims.feed(delta)))

    def finish(self) -> str:
        """End of the reply: whatever is still held is text, except a tag left open on an emoji."""
        rest = self._cut_tag(self._claims.finish())
        return self._receipts.feed(rest + self._finish_tag()) + self._receipts.finish()

    def _cut_tag(self, delta: str) -> str:
        text = self._held + delta
        self._held = ""
        out: list[str] = []
        while text:
            if self._body is None:
                found = _OPEN_RE.search(text)
                if found is not None:
                    out.append(text[: found.start()])
                    text, self._body = text[found.end() :], ""
                    continue
                cut = _partial(text.lower(), _OPEN)
                out.append(text[:cut])
                self._held, text = text[cut:], ""
                continue
            self._body += text
            text = ""
            closed = _CLOSE_RE.search(self._body)
            if closed is not None:
                body, rest = self._body[: closed.start()], self._body[closed.end() :]
                self._take(body)
                self._body, text = None, rest
            elif len(self._body) > _MAX_BODY + len(_CLOSE):
                out.append(_OPEN + self._body)  # no emoji is this long: it was text
                self._body = None
        return "".join(out)

    def _finish_tag(self) -> str:
        held, body = self._held, self._body
        self._held, self._body = "", None
        if body is None:
            return held
        if is_single_emoji(body.strip()):
            self._take(body)
            return ""
        return _OPEN + body

    def _take(self, body: str) -> None:
        if self.emoji is None and is_single_emoji(body.strip()):
            self.emoji = body.strip()


def _partial(text: str, opening: str) -> int:
    """Where the end of ``text`` starts to be the beginning of ``opening``, else its length."""
    for size in range(min(len(opening) - 1, len(text)), 0, -1):
        if opening.startswith(text[-size:]):
            return len(text) - size
    return len(text)


def split_reaction(text: str) -> tuple[str, str | None]:
    """A whole reply without its ``<react>`` tags, and the first valid emoji in them."""
    parsed = ReactionFilter()
    clean = parsed.feed(text) + parsed.finish()
    return clean, parsed.emoji


def split_claims(text: str) -> tuple[str, str | None, str | None]:
    """A whole reply without its ``<react>`` tags and ``<claims>`` block, its emoji, its block.

    The block is None when the reply had none or left it unclosed.
    """
    parsed = ReactionFilter()
    clean = parsed.feed(text) + parsed.finish()
    return clean, parsed.emoji, parsed.claims
