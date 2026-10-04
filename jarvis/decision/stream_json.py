"""L3 streaming ``{"spoken", "written"}`` reply: spoken flows, written waits (ADR 0114).

On the structured spoken route the model's text is one JSON object whose
``spoken`` string comes first. The extractor reads it character by character
and hands out each unescaped ``spoken`` character as soon as it is known, so
the assembler downstream sees only the words to be said. ``written`` is
collected for the screen. A fenced code block inside ``spoken`` is never
read aloud: it moves to the screen, after ``written``. It never parses the
object as a whole: a stream cut anywhere keeps what was already unescaped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

_SIMPLE_ESCAPES: Final[dict[str, str]] = {
    '"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t",
}
_HEX: Final[frozenset[str]] = frozenset("0123456789abcdefABCDEF")
_UNICODE_DIGITS: Final[int] = 4
_NEXT: Final[dict[tuple[str, str], str]] = {
    ("start", "{"): "key_or_end", ("colon", ":"): "value", ("after_value", ","): "key_or_end",
}
_LOST: Final[str] = "�"  # a surrogate that never found its partner
_FENCE: Final[str] = "```"


@dataclass(frozen=True)
class JsonReply:
    """What the stream said: the spoken text read so far, the finished written part."""

    spoken: str
    written: str
    complete: bool


class SpokenJsonExtractor:
    """Feed raw deltas of a JSON object; receive the ``spoken`` string's new text.

    Only string values are read (the schema has no others); a key other than
    ``spoken`` and ``written`` is skipped. ``failed`` is set at the first
    character the schema cannot produce. A ``written`` value counts only once
    its closing quote has arrived.
    """

    def __init__(self) -> None:
        """Start before the opening brace."""
        self._state = "start"
        self._key = ""
        self._target: str | None = None  # the key whose string value is being read
        self._hex = ""
        self._high: int | None = None  # a high surrogate waiting for its low half
        self._spoken: list[str] = []
        self._written: list[str] = []
        self._written_done = ""
        self._ticks = 0  # backticks in spoken, held until it is known whether they open a fence
        self._code: list[str] | None = None  # the fenced block being read, while one is open
        self._blocks: list[str] = []  # fenced blocks taken out of spoken
        self.has_spoken = False
        self.failed = False
        self.complete = False

    @property
    def spoken(self) -> str:
        """Every unescaped ``spoken`` character handed out so far."""
        return "".join(self._spoken)

    @property
    def written(self) -> str:
        """The ``written`` value, once its closing quote has arrived, then spoken's code blocks."""
        if not self._blocks:
            return self._written_done
        blocks = [f"{_FENCE}{b.rstrip()}\n{_FENCE}" for b in self._blocks if b.strip()
                  and b.split("\n", 1)[-1].strip() not in self._written_done]
        return "\n\n".join(part for part in [self._written_done.strip(), *blocks] if part)

    def feed(self, delta: str) -> str:
        """Take one delta; return the ``spoken`` text it completes."""
        emitted: list[str] = []
        for char in delta:
            self._step(char, emitted)
        text = "".join(emitted)
        self._spoken.append(text)
        return text

    @property
    def moved_code(self) -> bool:
        """A code block left spoken for the screen."""
        return bool(self._blocks)

    def finish(self) -> JsonReply:
        """Return what the stream said; a cut stream is whatever was already read."""
        return JsonReply(self.spoken, self.written, self.complete)

    def _step(self, char: str, emitted: list[str]) -> None:
        if self._state in {"string", "escape", "unicode"}:
            self._in_string(char, emitted)
        elif not (char.isspace() and self._state != "key"):
            self._in_structure(char)

    def _in_string(self, char: str, emitted: list[str]) -> None:
        if self._state == "string":
            if char == "\\":
                self._state = "escape"
            elif char == '"':
                self._close_string(emitted)
            else:
                self._put(self._lost() + char, emitted)
        elif self._state == "escape":
            if char == "u":
                self._hex, self._state = "", "unicode"
            elif char in _SIMPLE_ESCAPES:
                self._put(self._lost() + _SIMPLE_ESCAPES[char], emitted)
                self._state = "string"
            else:
                self._fail()
        elif char not in _HEX:
            self._fail()
        else:
            self._hex += char
            if len(self._hex) == _UNICODE_DIGITS:
                self._put(self._unit(int(self._hex, 16)), emitted)
                self._state = "string"

    def _close_string(self, emitted: list[str]) -> None:
        self._put(self._lost(), emitted)
        if self._target == "spoken":
            self._end_spoken(emitted)
        if self._target == "written":
            self._written_done = "".join(self._written)
        self._state = "after_value"

    def _in_structure(self, char: str) -> None:
        state = self._state
        if state == "key":
            self._in_key(char)
        elif char == "}" and state in {"key_or_end", "after_value"}:
            self._state, self.complete = "done", True
        elif char == '"' and state == "key_or_end":
            self._key, self._state = "", "key"
        elif char == '"' and state == "value":
            self._open_value()
        elif (state, char) in _NEXT:
            self._state = _NEXT[state, char]
        else:
            self._fail()

    def _in_key(self, char: str) -> None:
        if char == '"':
            self._state = "colon"
        elif char == "\\":
            self._fail()
        else:
            self._key += char

    def _open_value(self) -> None:
        self._target = self._key if self._key in {"spoken", "written"} else None
        if self._target == "spoken":
            self.has_spoken = True
        elif self._target == "written":
            self._written = []
        self._state = "string"

    def _fail(self) -> None:
        self._state, self.failed = "failed", True

    def _lost(self) -> str:
        """A high surrogate that no low half followed becomes one replacement character."""
        if self._high is None:
            return ""
        self._high = None
        return _LOST

    def _unit(self, code: int) -> str:
        """One unicode escape: whole characters out, a high half held for its low half."""
        high, self._high = self._high, None
        if 0xD800 <= code < 0xDC00:  # noqa: PLR2004 - the high-surrogate range
            self._high = code
            return _LOST if high is not None else ""
        if 0xDC00 <= code < 0xE000:  # noqa: PLR2004 - the low-surrogate range
            if high is None:
                return _LOST
            return chr(0x10000 + ((high - 0xD800) << 10) + (code - 0xDC00))
        return (_LOST if high is not None else "") + chr(code)

    def _put(self, text: str, emitted: list[str]) -> None:
        if self._target == "spoken":
            for char in text:
                self._speak(char, emitted)
        elif self._target == "written":
            self._written.append(text)

    def _speak(self, char: str, emitted: list[str]) -> None:
        """One spoken character: three backticks open or close a block that goes to the screen."""
        if char == "`":
            self._ticks += 1
            if self._ticks == len(_FENCE):
                self._ticks = 0
                if self._code is None:
                    self._code = []
                else:
                    self._blocks.append("".join(self._code))
                    self._code = None
            return
        self._flush_ticks(emitted)
        (emitted if self._code is None else self._code).append(char)

    def _flush_ticks(self, emitted: list[str]) -> None:
        if self._ticks:
            (emitted if self._code is None else self._code).append("`" * self._ticks)
            self._ticks = 0

    def _end_spoken(self, emitted: list[str]) -> None:
        """Spoken closed: held backticks are text; a block never closed still goes to the screen."""
        self._flush_ticks(emitted)
        if self._code is not None:
            self._blocks.append("".join(self._code))
            self._code = None
