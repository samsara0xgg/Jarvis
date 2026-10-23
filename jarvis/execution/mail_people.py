"""ADR 0366: who a letter is to. A person he names is found in his own mail; his own are known.

A recipient is an address (used as given), a word that means himself (``me``, 自己: his own
address, read from his sent mail) or a name. A name is looked up among the senders and recipients
of the mail he has had in the last year: the (display name, address) pairs whose display name has
every word of the name, case and accents folded (a Chinese name, whole), each with how often it
appears and when it last did. One address is used and the result names the last mail with it;
several are put to him, up to three; none is an error that asks him for the address. A name's
pairs are kept for a day.

Layer rules: execution; ``jarvis.execution.mail_gmail`` and ``jarvis.state.mail_message``.
"""

from __future__ import annotations

import re
import threading
import unicodedata
from dataclasses import dataclass
from datetime import timedelta
from email.utils import parseaddr
from typing import TYPE_CHECKING, Final

from jarvis.execution import speakable
from jarvis.execution.tools import ToolError

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Sequence
    from datetime import datetime, tzinfo

    from jarvis.execution.mail_gmail import Head, Mailbox
    from jarvis.state.mail_message import Person

LOOKED_BACK: Final = "1y"
"""How far back a name is looked for (Gmail's ``newer_than``)."""
LOOKUP_CAP: Final = 30
"""How many of the newest mails with the name are read."""
NAMED_MAX: Final = 3
"""How many candidates a question to him names."""
_KEPT: Final = timedelta(days=1)
_ME: Final = frozenset({"me", "myself", "自己", "我", "我自己"})
_SEPARATORS: Final = ",;\uff0c\uff1b\u3001\n"
_CJK: Final = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff\uac00-\ud7af]")
_WORDS: Final = re.compile(r"[^\W_]+")
_ADDRESS: Final = re.compile(r"[^\s@<>,;\"]+@[^\s@<>,;\"]+\.[^\s@<>,;\"]+")
_SUBJECT_CHARS: Final = 50


@dataclass(frozen=True)
class Who:
    """One recipient: a name (possibly none) and an address, and whether it is one of his own."""

    name: str
    address: str
    mine: bool = False
    note: str = ""
    """How a name was found: the last mail with that person; empty for an address as given."""

    @property
    def shown(self) -> str:
        """``Name <address>``, or the address alone when there is no name."""
        return f"{self.name} <{self.address}>" if self.name else self.address

    @property
    def marked(self) -> str:
        """:attr:`shown`, with ``(you)`` after his own."""
        return f"{self.shown} (you)" if self.mine else self.shown


@dataclass(frozen=True)
class Candidate:
    """One (display name, address) pair of his mail, with how often it appears and when last."""

    name: str
    address: str
    count: int
    last: datetime | None
    subject: str
    """The subject of the last mail it is in."""


def fold(text: str) -> str:
    """Lower case, accents dropped, one space between words."""
    plain = unicodedata.normalize("NFKD", text)
    plain = "".join(one for one in plain if not unicodedata.combining(one))
    return " ".join(plain.casefold().split())


def name_matches(wanted: str, shown: str) -> bool:
    """Whether the display name ``shown`` is the person ``wanted``: all its words, or all of it."""
    want, have = fold(wanted), fold(shown)
    if not want or not have:
        return False
    if _CJK.search(want):
        return want.replace(" ", "") in have.replace(" ", "")
    return set(_WORDS.findall(want)) <= set(_WORDS.findall(have))


def split(value: object) -> list[str]:
    """The recipients in ``value``: a list of them, or one string of them in a row.

    Commas, semicolons and their CJK forms separate them; one inside quotes or angle brackets
    does not.
    """
    if value is None:
        return []
    if isinstance(value, list | tuple):
        # one item is one recipient (a name may hold a comma: "Lee, Sam"), unless it is a row of
        # addresses
        return [
            piece for one in value
            for piece in (split(one) if isinstance(one, str) and "@" in one else _item(one))
        ]
    if not isinstance(value, str):
        msg = "mail: a recipient must be text"
        raise ToolError(msg, code="invalid_arguments")
    pieces: list[str] = []
    now, quoted, bracketed = "", False, False
    for char in value:
        if char == '"':
            quoted = not quoted
        elif char == "<" and not quoted:
            bracketed = True
        elif char == ">" and not quoted:
            bracketed = False
        if char in _SEPARATORS and not quoted and not bracketed:
            pieces.append(now)
            now = ""
        else:
            now += char
    pieces.append(now)
    return [" ".join(one.split()) for one in pieces if one.strip()]


def _item(value: object) -> list[str]:
    if not isinstance(value, str):
        msg = "mail: a recipient must be text"
        raise ToolError(msg, code="invalid_arguments")
    return [" ".join(value.split())] if value.strip() else []


def _own(address: str, own: Collection[str]) -> bool:
    return address.lower() in own


def _pairs(heads: Iterable[Head | None], wanted: str) -> list[Candidate]:
    """The matching (display name, address) pairs of the heads, most recent first."""
    found: dict[str, Candidate] = {}
    for head in heads:
        if head is None:
            continue
        people: Sequence[Person] = (head.sender, *head.to, *head.cc)
        seen: set[str] = set()
        for one in people:
            key = one.address.lower()
            if not key or key in seen or not name_matches(wanted, one.name):
                continue
            seen.add(key)
            before = found.get(key)
            if before is None:
                found[key] = Candidate(one.name, one.address, 1, head.at, head.subject)
                continue
            newer = head.at is not None and (before.last is None or head.at > before.last)
            found[key] = Candidate(
                one.name if newer else before.name, before.address, before.count + 1,
                head.at if newer else before.last, head.subject if newer else before.subject,
            )
    return sorted(
        found.values(), key=lambda one: (one.last is not None, one.last, one.count), reverse=True,
    )


def _plain(one: Candidate, now: datetime, zone: tzinfo) -> str:
    when = f", last mail {speakable.when(one.last, now, zone)}" if one.last else ""
    return f"{one.name} <{one.address}>{when}"


class People:
    """Names looked up in his mail, and his own addresses: over one :class:`Mailbox`."""

    def __init__(self, box: Mailbox) -> None:
        """Hold the mailbox the lookups search and the pairs found for a day."""
        self._box = box
        self._lock = threading.Lock()
        self._kept: dict[str, tuple[datetime, tuple[Candidate, ...]]] = {}

    def _candidates(self, name: str, now: datetime) -> tuple[Candidate, ...]:
        key = fold(name)
        with self._lock:
            held = self._kept.get(key)
        if held is not None and now - held[0] < _KEPT:
            return held[1]
        quoted = name.replace('"', "")
        query = (
            f'(from:"{quoted}" OR to:"{quoted}") newer_than:{LOOKED_BACK} -in:drafts'
        )
        found = self._box.search(query, cap=LOOKUP_CAP)
        pairs = tuple(_pairs(self._box.heads([one.id for one in found.hits]), name))
        with self._lock:
            self._kept[key] = (now, pairs)
        return pairs

    def _pick(self, name: str, now: datetime, zone: tzinfo, own: frozenset[str]) -> Who:
        pairs = self._candidates(name, now)
        if not pairs:
            msg = (
                f'mail: no one named "{name}" wrote to him or had a letter from him in the last'
                " year; ask him for the address"
            )
            raise ToolError(msg, code="not_found", kind="impossible")
        if len(pairs) > 1:
            named = "; ".join(_plain(one, now, zone) for one in pairs[:NAMED_MAX])
            more = f" ({len(pairs) - NAMED_MAX} more)" if len(pairs) > NAMED_MAX else ""
            msg = (
                f'mail: "{name}" is more than one person in his mail: {named}{more}; ask him'
                " which, or for the address"
            )
            raise ToolError(msg, code="ambiguous_recipient", kind="needs_him")
        one = pairs[0]
        last = ""
        if one.last is not None:
            subject = speakable.cut(one.subject, _SUBJECT_CHARS) or "(no subject)"
            last = f'last mail with them: "{subject}", {speakable.when(one.last, now, zone)}'
        return Who(one.name, one.address, _own(one.address, own), last)

    def resolve(self, wanted: object, now: datetime, zone: tzinfo) -> list[Who]:
        """The recipients ``wanted`` names, each once, in the order given."""
        out: list[Who] = []
        own = self._box.own(now)
        for piece in split(wanted):
            who = self._one(piece, now, zone, own)
            if all(who.address.lower() != one.address.lower() for one in out):
                out.append(who)
        return out

    def _one(self, piece: str, now: datetime, zone: tzinfo, own: frozenset[str]) -> Who:
        if fold(piece) in _ME:
            address = self._box.me(now)
            if not address:
                msg = (
                    "mail: his own address is not known: he has sent no mail from this account;"
                    " ask him for it"
                )
                raise ToolError(msg, code="not_found", kind="impossible")
            return Who("", address, mine=True)
        if "@" in piece or "<" in piece:
            name, address = parseaddr(piece)
            if not valid(address):
                msg = f"mail: {piece!r} is not an address; give name <address> or the address"
                raise ToolError(msg, code="invalid_arguments")
            return Who(" ".join(name.split()), address, _own(address, own))
        return self._pick(piece, now, zone, own)


def valid(address: str) -> bool:
    """Whether ``address`` looks like one: something, an at sign, a domain with a dot."""
    return bool(_ADDRESS.fullmatch(address))


def own_marks(who: Iterable[Who]) -> list[str]:
    """The addresses among ``who`` that are his own, lower case, each once."""
    return list(dict.fromkeys(one.address.lower() for one in who if one.mine))


__all__ = [
    "LOOKED_BACK",
    "Candidate",
    "People",
    "Who",
    "fold",
    "name_matches",
    "own_marks",
    "split",
    "valid",
]
