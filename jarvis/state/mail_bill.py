"""ADR 0365: the bill or payment facts a mail states, and whether a receipt pays a bill.

Facts are read by code and no model: the amount and currency, the invoice or order number, the due
date, and whether the mail ``asks`` for a payment, says it was ``paid``, or is a ``refund``. The
mail's schema.org markup (JSON-LD or microdata, ``Invoice`` and ``Order``) comes first, then its
subject and text, then its PDFs. A layer fills only what the ones before it left empty, and a
fact no layer states is absent, never guessed.

A bill is paid by a receipt from the same registrable domain that carries the same number or the
same amount and currency, dated from the bill to its due date, or within a week after the bill
when it names none (:func:`paid`). The mail tool and the commitment intake both call it.

The words below are the ones bills and receipts print (``amount due``, ``paid``, ``合计``), matched
as input.

Layer rules: stdlib and ``jarvis.state.mail_message``, ``jarvis.state.mail_text``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any, Final, Literal

from jarvis.state.mail_message import attachment_text
from jarvis.state.mail_text import clean, is_forward

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from datetime import tzinfo

    from jarvis.state.mail_message import Mail

type Kind = Literal["asks", "paid", "refund"]

GRACE: Final = timedelta(days=7)
"""How long after a bill a receipt may come when the bill names no due date."""
_MARKUP_CHARS: Final = 400_000
_TEXT_CHARS: Final = 60_000
_PDFS: Final = 4
_PDF_CHARS: Final = 30_000
_REACH: Final = 60
"""How far before an amount its keyword may stand, in characters of the same line."""
_ADJACENT: Final = 14
"""How far before an amount a bare ``total`` may stand: only separators between."""
_TRAILING: Final = 24
_DUE_REACH: Final = 40
_MONTHS_IN_YEAR: Final = 12
_SENTENCE: Final = re.compile(r"[.!?]\s")
_NUMBERS: Final = 4
"""How many numbers a mail's facts keep: the first is shown, the others match."""
_MIN_NUMBER: Final = 3
"""A shorter number is too common to say two mails are about the same invoice."""

# --- the facts ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Facts:
    """What a mail states about a bill or a payment; each field is absent when it does not."""

    kind: Kind
    amount: Decimal | None = None
    currency: str = ""
    """An ISO code, or the bare symbol (``$``, ``¥``) when the mail gives no more."""
    number: str = ""
    due: date | None = None
    also: tuple[str, ...] = ()
    """The other numbers it names (the invoice a receipt is for): they match, and are not shown."""

    @property
    def line(self) -> str:
        """The facts as one short line for the model: ``asks for payment: 5.00 USD, no. IN-1``."""
        what = {"asks": "asks for payment", "paid": "payment confirmed", "refund": "refund"}
        parts: list[str] = []
        if self.amount is not None:
            parts.append(_shown(self.amount, self.currency))
        if self.number:
            parts.append(f"no. {self.number}")
        if self.due is not None:
            parts.append(f"due {self.due.isoformat()}")
        return what[self.kind] + (f": {', '.join(parts)}" if parts else "")


def _shown(amount: Decimal, currency: str) -> str:
    text = f"{amount:,.2f}"
    if not currency:
        return text
    return f"{currency}{text}" if currency in _FAMILIES else f"{text} {currency}"


@dataclass
class _Found:
    """Facts as layers give them: every field optional until the layers are merged."""

    kind: Kind | None = None
    amount: Decimal | None = None
    currency: str = ""
    number: str = ""
    due: date | None = None
    also: tuple[str, ...] = ()

    def fill(self, other: _Found) -> None:
        """Take from ``other`` what this has not got; an amount comes with its currency."""
        self.kind = self.kind or other.kind
        if self.amount is None and other.amount is not None:
            self.amount, self.currency = other.amount, other.currency
        if not self.number:
            self.number, self.also = other.number, other.also
        else:
            self.also = _distinct(self.number, (*self.also, other.number, *other.also))
        self.due = self.due or other.due

    def complete(self) -> bool:
        """Whether every fact is there, so no further layer need be read."""
        return bool(self.kind and self.amount is not None and self.number and self.due)

    def facts(self) -> Facts | None:
        """The facts, when the mail says what kind of mail it is and states something of it."""
        if self.kind is None or (self.amount is None and not self.number and self.due is None):
            return None
        return Facts(self.kind, self.amount, self.currency, self.number, self.due, self.also)


# --- money -------------------------------------------------------------------------------------

_CODES: Final = (
    "USD", "CAD", "AUD", "NZD", "EUR", "GBP", "CNY", "JPY", "CHF", "HKD", "SGD", "INR", "KRW",
    "MXN", "SEK", "NOK", "DKK",
)
_SYMBOLS: Final = {
    "US$": "USD", "CA$": "CAD", "CAD$": "CAD", "C$": "CAD", "AU$": "AUD", "A$": "AUD",
    "NZ$": "NZD", "HK$": "HKD", "S$": "SGD", "$": "$", "€": "EUR", "£": "GBP", "¥": "¥",
    "￥": "¥", "RMB": "CNY", "元": "CNY", "人民币": "CNY", "円": "JPY",
}
_FAMILIES: Final = {
    "$": frozenset({"USD", "CAD", "AUD", "NZD", "HKD", "SGD", "MXN"}),
    "¥": frozenset({"CNY", "JPY"}),
}
"""A bare symbol is compatible with any code of its family: ``$`` is dollars, not which."""
_NUMBER: Final = r"\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?"
_BEFORE: Final = (
    "US\\$|CA\\$|CAD\\$|C\\$|AU\\$|A\\$|NZ\\$|HK\\$|S\\$|\\$|€|£|¥|￥|RMB|" + "|".join(_CODES)
)
_AFTER: Final = "|".join(_CODES) + "|RMB|元|円|人民币|€|£"
_MONEY: Final = re.compile(
    rf"(?:(?P<pre>{_BEFORE})\s?(?P<a>{_NUMBER})(?!\d)(?:\s?\b(?P<tail>{'|'.join(_CODES)})\b)?"
    rf"|(?P<b>{_NUMBER})\s?(?P<post>{_AFTER}))(?!\d)",
    re.IGNORECASE,
)


def _currency(symbol: str) -> str:
    key = symbol.upper()
    return _SYMBOLS.get(key, key)


def _currency_of(found: re.Match[str]) -> str:
    """The currency a money match states: a code after a symbol (``$5 USD``) is the finer one."""
    return _currency(found["tail"] or found["pre"] or found["post"])


def _decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


def same_currency(one: str, other: str) -> bool:
    """Whether two currencies are one money: equal, or a bare symbol and a code of its family."""
    if one == other:
        return True
    return other in _FAMILIES.get(one, ()) or one in _FAMILIES.get(other, ())


def same_amount(one: Facts, other: Facts) -> bool:
    """Whether both state an amount, the same, in the same currency."""
    return (
        one.amount is not None and one.amount == other.amount
        and same_currency(one.currency, other.currency)
    )


# --- words -------------------------------------------------------------------------------------


def _words(ascii_words: str, cjk_words: str = "") -> re.Pattern[str]:
    pattern = rf"\b(?:{ascii_words})\b"
    return re.compile(f"{pattern}|{cjk_words}" if cjk_words else pattern, re.IGNORECASE)


_ASKS: Final = _words(
    r"(?:total\s+|amount\s+|balance\s+|payment\s+)?(?:amount|balance|payment)\s+due|total\s+due"
    r"|due\s+amount|amount\s+to\s+pay|new\s+balance|statement\s+balance|please\s+pay|pay\s+now",
    "应付(?:金额|款)?|应还(?:款|金额)?|待付|待支付|本期应还",
)
_WEAK_ASKS: Final = _words(r"minimum\s+(?:payment|due|amount)", "最低还款")
_PAID: Final = _words(
    r"amount\s+paid|total\s+paid|payments?\s+(?:received|of)|you\s+paid|paid|(?:was|were|been)"
    r"\s+charged|charged|we\s+charged",
    "已付款|已支付|付款金额|实付(?:款|金额)?|扣款(?:金额)?|支付成功",
)
_REFUND: Final = _words(r"refund(?:ed)?(?:\s+amount)?", "退款(?:金额)?|已退")
_NEUTRAL: Final = _words(r"(?<!sub)total(?:\s+amount)?|amount", "合计|总计|总额|金额")
_KEYWORDS: Final[tuple[tuple[Kind, bool, re.Pattern[str]], ...]] = (
    ("asks", False, _ASKS), ("asks", True, _WEAK_ASKS), ("paid", False, _PAID),
    ("refund", False, _REFUND),
)
"""Each keyword pattern with the kind it says and whether it says it weakly."""
_SUBJECT_CUES: Final[tuple[tuple[Kind, re.Pattern[str]], ...]] = (
    ("refund", _words("refund(?:ed)?", "退款|退还")),
    ("paid", _words(
        r"receipt|payment\s+(?:received|confirm\w*|success\w*|processed)"
        r"|thank\s+you\s+for\s+your\s+payment|you\s+paid|was\s+charged|has\s+been\s+paid"
        r"|paid\s+in\s+full",
        "收据|已付款|已支付|付款成功|支付成功|扣款",
    )),
    ("asks", _words(
        r"invoice|statement|bill|amount\s+due|balance\s+due|payment\s+due|due\s+(?:on|by|date)"
        r"|please\s+pay|past\s+due|overdue|payment\s+(?:failed|declined|unsuccessful)",
        "账单|发票|应付|待付|催缴|缴费",
    )),
)
_BODY_CUES: Final[tuple[tuple[Kind, re.Pattern[str]], ...]] = (
    ("refund", _words(
        r"(?:has\s+been|we(?:'ve|\s+have)?|was)\s+refunded|refund\s+(?:of|amount|issued|processed)",
        "退款",
    )),
    ("paid", _words(
        r"payment\s+(?:received|confirmation|successful)|thank\s+you\s+for\s+your\s+payment"
        r"|you\s+paid|has\s+been\s+paid|paid\s+in\s+full|receipt",
        "已付款|付款成功|支付成功|收据",
    )),
    ("asks", _words(
        r"amount\s+due|balance\s+due|payment\s+due|due\s+date|please\s+pay|invoice|statement",
        "应付|待付|账单|发票",
    )),
)


def _cue(text: str, cues: Sequence[tuple[Kind, re.Pattern[str]]]) -> Kind | None:
    for kind, pattern in cues:
        if pattern.search(text):
            return kind
    return None


@dataclass(frozen=True)
class _Hit:
    kind: Kind | None
    """The kind the keyword beside the amount says; ``None`` for a bare ``total``."""
    weak: bool
    amount: Decimal
    currency: str


def _keyword(before: str, after: str) -> tuple[Kind | None, bool] | None:
    """The kind the words around an amount say: the nearest before it, else one right after it."""
    best: tuple[int, Kind, bool] | None = None
    for kind, weak, pattern in _KEYWORDS:
        for word in pattern.finditer(before):
            if best is None or word.end() >= best[0]:
                best = (word.end(), kind, weak)
    if best is not None:
        return best[1], best[2]
    for kind, weak, pattern in _KEYWORDS:
        if pattern.match(after.lstrip(" :|-")) is not None:
            return kind, weak
    tail = _NEUTRAL.search(before[-_ADJACENT:])
    return (None, False) if tail is not None else None


def _hits(text: str) -> Iterator[_Hit]:
    """Each amount a line states beside a keyword; a zero amount asks nothing."""
    for line in text.split("\n"):
        for found in _MONEY.finditer(line):
            number = found["a"] or found["b"]
            amount = _decimal(number)
            if amount is None or amount <= 0:
                continue
            before = _SENTENCE.split(line[max(0, found.start() - _REACH) : found.start()])[-1]
            after = line[found.end() : found.end() + _TRAILING]
            said = _keyword(before, after)
            if said is None:
                continue
            kind, weak = said
            yield _Hit(kind, weak, amount, _currency_of(found))


def _chosen(hits: Sequence[_Hit], cue: Kind | None) -> tuple[Kind | None, _Hit | None]:
    """The kind and the amount among ``hits``: the kind ``cue`` gives, else the strongest hit's."""
    strong = [one for one in hits if one.kind is not None]
    kinds = {one.kind for one in strong}
    kind = cue
    if kind is None:
        order: tuple[Kind, ...] = ("asks", "refund", "paid")
        kind = next((one for one in order if one in kinds), None)
    if kind is None:
        return None, None
    own = [one for one in strong if one.kind == kind]
    pool = [one for one in own if not one.weak] or own or [one for one in hits if one.kind is None]
    return kind, (pool[0] if pool else None)


# --- numbers and dates -------------------------------------------------------------------------

_NUMBER_WORDS: Final = re.compile(
    r"(?:invoice|order|receipt|confirmation|发票|订单|收据|账单)\s*"
    r"(?:number|num\.?|no\.?|id|编号|号码|号)?[\s:\uff1a#|]*(?P<n>[A-Z0-9][A-Z0-9\-_/]{2,29})",
    re.IGNORECASE,
)
_HASHED: Final = re.compile(r"#\s?(?P<n>[A-Z0-9][A-Z0-9\-_/]{2,29})", re.IGNORECASE)
_MONTHS: Final = {
    name: n for n, names in enumerate(
        (
            ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
            ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
            ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
            ("dec", "december"),
        ), 1,
    ) for name in names
}
_MONTH: Final = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DATES: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})"),
    re.compile(r"(?P<y>\d{4})\s*年\s*(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*[日号]"),
    re.compile(
        rf"\b(?P<mon>{_MONTH})\.?\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(?P<y>\d{{4}}))?\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<mon>{_MONTH})\.?(?:,?\s+(?P<y>\d{{4}}))?\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*[日号]"),
    re.compile(r"\b(?P<a>\d{1,2})/(?P<b>\d{1,2})/(?P<y>\d{4})\b"),
)
_DUE_WORDS: Final = re.compile(
    r"\bdue\b|\bpay(?:able)?\s+by\b|\bpayment\s+by\b|到期|截止|应于|付款日", re.IGNORECASE,
)


def _day(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _year_for(month: int, day: int, sent: date | None) -> date | None:
    """A date that names no year: the mail's year, or the next when that is long before the mail."""
    if sent is None:
        return None
    one = _day(sent.year, month, day)
    if one is not None and one < sent - timedelta(days=60):
        one = _day(sent.year + 1, month, day)
    return one


def _date_in(text: str, sent: date | None) -> date | None:
    """The first date ``text`` states, as the mail wrote it; an ambiguous ``3/4/2026`` is none."""
    best: tuple[int, date] | None = None
    for pattern in _DATES:
        for found in pattern.finditer(text):
            one = _date_of(found, sent)
            if one is not None and (best is None or found.start() < best[0]):
                best = (found.start(), one)
                break
    return None if best is None else best[1]


def _date_of(found: re.Match[str], sent: date | None) -> date | None:
    parts = found.groupdict()
    if parts.get("a"):
        first, second = int(parts["a"]), int(parts["b"])
        if first > _MONTHS_IN_YEAR >= second:
            return _day(int(parts["y"]), second, first)
        if second > _MONTHS_IN_YEAR >= first or first == second:
            return _day(int(parts["y"]), first, second)
        return None
    month = _MONTHS[parts["mon"].lower()] if parts.get("mon") else int(parts["m"])
    if parts.get("y"):
        return _day(int(parts["y"]), month, int(parts["d"]))
    return _year_for(month, int(parts["d"]), sent)


def _due(text: str, sent: date | None) -> date | None:
    for line in text.split("\n"):
        for word in _DUE_WORDS.finditer(line):
            one = _date_in(line[word.end() : word.end() + _DUE_REACH], sent)
            if one is not None:
                return one
    return None


def _key(number: str) -> str:
    return re.sub(r"[^0-9A-Z]", "", number.upper())


def _distinct(first: str, others: Sequence[str]) -> tuple[str, ...]:
    """``others`` without ``first`` and without repeats, spelled as they were first written."""
    seen = {_key(first)}
    kept: list[str] = []
    for one in others:
        if one and _key(one) not in seen and len(kept) < _NUMBERS - 1:
            seen.add(_key(one))
            kept.append(one)
    return tuple(kept)


def _numbers(subject: str, text: str, kind: Kind | None) -> list[str]:
    """The numbers a subject and a text name, the kind's own first (a receipt's, an invoice's)."""
    found: list[tuple[int, int, str]] = []
    for source, where in ((subject, 0), (text, 1)):
        for pattern in (_NUMBER_WORDS, _HASHED):
            for one in pattern.finditer(source):
                token = one["n"].strip("-_/")
                if any(ch.isdigit() for ch in token):
                    word = one.group(0).lower()
                    found.append((_rank(word, kind), where, token))
    found.sort(key=lambda row: row[:2])
    return list(dict.fromkeys(token for _, _, token in found))


def _rank(word: str, kind: Kind | None) -> int:
    """0 for a number the kind is about (``receipt`` for a payment, ``invoice`` for a bill)."""
    own = {"paid": ("receipt", "收据"), "asks": ("invoice", "发票", "账单")}.get(kind or "", ())
    return 0 if word.startswith(own) else 1


# --- text --------------------------------------------------------------------------------------


def _scan(subject: str, text: str, sent: date | None, kind: Kind | None = None) -> _Found:
    """What a subject and a text state: the kind from the subject's words, else the amount's."""
    hits = list(_hits(text))
    cue = kind or _cue(subject, _SUBJECT_CUES)
    chosen, hit = _chosen(hits, cue)
    if chosen is None:
        chosen = _cue(text, _BODY_CUES)
        if chosen is not None:
            chosen, hit = _chosen(hits, chosen)
    found = _Found(kind=chosen)
    if chosen is not None:
        if hit is not None:
            found.amount, found.currency = hit.amount, hit.currency
        named = _numbers(subject, text, chosen)
        found.number = named[0] if named else ""
        found.also = _distinct(found.number, named[1:])
        found.due = _due(text, sent) if chosen == "asks" else None
    return found


# --- schema.org --------------------------------------------------------------------------------

_LD_JSON: Final = re.compile(
    r"<script[^>]*type\s*=\s*[\"']?application/ld\+json[\"']?[^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
_VOID: Final = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source",
    "track", "wbr",
})
_VALUE_ATTRS: Final = {
    "a": "href", "link": "href", "area": "href", "img": "src", "source": "src",
    "time": "datetime", "data": "value", "meter": "value",
}


@dataclass
class _Frame:
    tag: str
    scope: dict[str, Any] | None
    prop: str
    value: str | None
    text: list[str] = field(default_factory=list)


class _Microdata(HTMLParser):
    """The ``itemscope`` elements of a page as objects: ``{"@type": ..., property: value}``."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[dict[str, Any]] = []
        self._stack: list[_Frame] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        given = {name.lower(): value or "" for name, value in attrs}
        scope: dict[str, Any] | None = None
        if "itemscope" in given:
            kind = given.get("itemtype", "").strip().rstrip("/").rpartition("/")[2]
            scope = {"@type": kind}
            self.found.append(scope)
        names = given.get("itemprop", "").split()
        value = given.get("content") or given.get(_VALUE_ATTRS.get(tag, ""), "")
        self._stack.append(_Frame(tag, scope, names[0] if names else "", value or None))
        if tag in _VOID:
            self._close(len(self._stack) - 1)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for at in range(len(self._stack) - 1, -1, -1):
            if self._stack[at].tag == tag:
                self._close(at)
                return

    def handle_data(self, data: str) -> None:
        for frame in self._stack:
            if frame.prop and frame.value is None and frame.scope is None:
                frame.text.append(data)

    def close(self) -> None:
        super().close()
        self._close(0)

    def _close(self, at: int) -> None:
        """Close the frame at ``at`` and every one opened inside it."""
        while len(self._stack) > at:
            frame = self._stack.pop()
            if not frame.prop:
                continue
            owner = next(
                (one.scope for one in reversed(self._stack) if one.scope is not None), None,
            )
            if owner is None:
                continue
            value: Any = frame.scope if frame.scope is not None else (
                frame.value if frame.value is not None else " ".join("".join(frame.text).split())
            )
            owner.setdefault(frame.prop, value)


def _schema_objects(markup: str) -> list[dict[str, Any]]:
    """Every object the markup describes, JSON-LD first and then microdata."""
    markup = markup[:_MARKUP_CHARS]
    found: list[dict[str, Any]] = []
    for block in _LD_JSON.findall(markup):
        try:
            found.extend(_walk(json.loads(block.strip().removeprefix("<!--").removesuffix("-->"))))
        except ValueError:
            continue
    if "itemscope" in markup.lower():
        reader = _Microdata()
        try:
            reader.feed(markup)
            reader.close()
        except (AssertionError, ValueError):  # a page the parser cannot read gives what it had
            pass
        found.extend(reader.found)
    return found


def _walk(node: object) -> Iterator[dict[str, Any]]:
    if isinstance(node, list):
        for one in node:
            yield from _walk(one)
    elif isinstance(node, dict):
        if "@type" in node:
            yield node
        for value in node.values():
            if isinstance(value, dict | list):
                yield from _walk(value)


def _types(obj: dict[str, Any]) -> set[str]:
    raw = obj.get("@type")
    names = raw if isinstance(raw, list) else [raw]
    return {str(one).rstrip("/").rpartition("/")[2].rpartition(":")[2] for one in names}


def _plain(value: object) -> str:
    """A schema.org value as text: a string or number, or a ``PropertyValue``'s ``value``."""
    if isinstance(value, dict):
        return _plain(value.get("value") or value.get("name") or "")
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    return ""


def _money(value: object, parent: dict[str, Any]) -> tuple[Decimal | None, str]:
    """An amount and currency from a price, a ``PriceSpecification`` or a ``MonetaryAmount``."""
    given = parent.get("priceCurrency") or parent.get("currency")
    amount: object = value
    if isinstance(value, dict):
        amount = value.get("price") or value.get("value") or value.get("amount")
        given = value.get("priceCurrency") or value.get("currency") or given
    text = _plain(amount)
    found = _MONEY.search(text)
    number = _decimal(found["a"] or found["b"]) if found else _decimal(text)
    currency = _currency(str(given)) if given else (
        _currency_of(found) if found else ""
    )
    return (number, currency) if number is not None and number > 0 else (None, "")


def _iso(value: object, sent: date | None) -> date | None:
    text = _plain(value)
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return _date_in(text, sent)


def _invoice(obj: dict[str, Any], sent: date | None) -> _Found:
    status = _plain(obj.get("paymentStatus")).lower()
    amount, currency = _money(obj.get("totalPaymentDue"), obj)
    kind: Kind | None = None
    if any(word in status for word in ("complete", "paid", "automaticallyapplied")) and (
        "unpaid" not in status
    ):
        kind = "paid"
    elif any(word in status for word in ("due", "unpaid", "declined")) or (
        not status and amount is not None
    ):
        kind = "asks"
    return _Found(
        kind, amount, currency,
        _plain(obj.get("confirmationNumber")) or _plain(obj.get("identifier")),
        _iso(obj.get("paymentDueDate") or obj.get("paymentDue"), sent) if kind == "asks" else None,
    )


def _order(obj: dict[str, Any]) -> _Found:
    status = _plain(obj.get("orderStatus")).lower()
    if "cancel" in status or "return" in status:
        return _Found()
    amount, currency = _money(obj.get("price") or obj.get("totalPrice"), obj)
    return _Found("asks" if "paymentdue" in status else "paid", amount, currency,
                  _plain(obj.get("orderNumber")))


def _markup(markup: str, sent: date | None) -> _Found:
    found = _Found()
    for obj in _schema_objects(markup):
        names = _types(obj)
        if "Invoice" in names:
            found.fill(_invoice(obj, sent))
        elif "Order" in names:
            found.fill(_order(obj))
        if found.complete():
            break
    return found


# --- a mail ------------------------------------------------------------------------------------


def facts(
    subject: str, text: str, *, markup: str = "", pdfs: Sequence[str] = (),
    sent: datetime | date | None = None,
) -> Facts | None:
    """The facts a mail states, from its markup, then its subject and text, then its PDFs."""
    day = sent.date() if isinstance(sent, datetime) else sent
    found = _Found()
    if markup:
        found.fill(_markup(markup, day))
    found.fill(_scan(subject, text[:_TEXT_CHARS], day))
    for pdf in pdfs:
        if found.complete():
            break
        found.fill(_scan("", pdf[:_PDF_CHARS], day, found.kind))
    return found.facts()


def mail_facts(mail: Mail) -> Facts | None:
    """The facts of one parsed mail; its PDFs are read only when the rest left a fact out."""
    sent = mail.at.date() if mail.at is not None else None
    markup = mail.html or (mail.plain if "<" in mail.plain else "")
    first = facts(mail.subject, mail.text, markup=markup, sent=sent)
    if first is not None and first.amount is not None and first.number and (
        first.kind != "asks" or first.due is not None
    ):
        return first
    pdfs: list[str] = []
    for file in mail.attachments[:_PDFS]:
        is_pdf = file.mime.lower() == "application/pdf" or file.name.lower().endswith(".pdf")
        if file.data and is_pdf:
            read = attachment_text(file)
            if read.kind == "text":
                pdfs.append(read.text)
    if not pdfs:
        return first
    return facts(mail.subject, mail.text, markup=markup, pdfs=pdfs, sent=sent)


def text_facts(subject: str, body: str, sent: datetime | None) -> Facts | None:
    """The facts of a message known only by its subject and the body the server gave.

    The body is the server's: its plain part, else its HTML, whose markup is read too.
    """
    markup = body if "ld+json" in body or "itemscope" in body.lower() else ""
    return facts(subject, clean(body, forwarded=is_forward(subject)).text, markup=markup, sent=sent)


# --- the match ---------------------------------------------------------------------------------

_SECOND_LEVEL: Final = frozenset({"co", "com", "org", "net", "gov", "edu", "ac", "ne", "or", "go"})


def registrable(address: str) -> str:
    """The registrable domain of an address or host: ``billing.example.com`` is ``example.com``.

    Without a public-suffix list: the last two labels, or three under a country code with a
    second level such as ``co.uk`` and ``com.au``.
    """
    labels = [one for one in address.rpartition("@")[2].lower().strip(". >").split(".") if one]
    if len(labels) > 2 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL:  # noqa: PLR2004
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


@dataclass(frozen=True)
class Seen:
    """A mail's facts with who sent it and when: what :func:`paid` compares."""

    facts: Facts
    sender: str
    """The sender's address."""
    at: datetime


def _shared_number(one: Facts, other: Facts) -> bool:
    """Whether the two name a number in common (long enough to mean one invoice)."""
    left = {_key(n) for n in (one.number, *one.also) if n}
    right = {_key(n) for n in (other.number, *other.also) if n}
    return any(len(n) >= _MIN_NUMBER for n in left & right)


def window(bill: Seen, zone: tzinfo = UTC) -> tuple[date, date]:
    """The days a receipt may be dated on: from the bill's day to its due day, else a week on."""
    first = bill.at.astimezone(zone).date()
    last = bill.facts.due if bill.facts.due is not None else first + GRACE
    return first, max(first, last)


def paid(bill: Seen, receipt: Seen, zone: tzinfo = UTC) -> bool:
    """Whether ``receipt`` pays ``bill`` (ADR 0365).

    The bill asks and the receipt confirms; both are from the same registrable domain; the
    receipt carries the bill's number, or its amount in its currency; and its day lies in
    :func:`window`. Another purchase of the same amount from the same sender can match, which is
    why the caller names the receipt.
    """
    one, other = bill.facts, receipt.facts
    if one.kind != "asks" or other.kind != "paid":
        return False
    domain = registrable(bill.sender)
    if not domain or domain != registrable(receipt.sender):
        return False
    if not (_shared_number(one, other) or same_amount(one, other)):
        return False
    first, last = window(bill, zone)
    return first <= receipt.at.astimezone(zone).date() <= last


__all__ = [
    "GRACE",
    "Facts",
    "Kind",
    "Seen",
    "facts",
    "mail_facts",
    "paid",
    "registrable",
    "same_amount",
    "same_currency",
    "text_facts",
    "window",
]
