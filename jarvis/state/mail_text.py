"""ADR 0363: a mail's body as the text he would read.

An HTML body is read from its tree and a plain body from its lines, and both come out the same
way:

- what the mail hides is dropped (``display:none`` and its kin, ``hidden``, comments, MSO-only
  blocks, pictures of no size) and so is padding (U+034F, zero-width characters, runs of no-break
  spaces), before anything else is read;
- table cells stay apart (`` | `` within a row, a line per row), blocks on their own lines, and a
  link reads as its text and its site;
- an older message is cut only where the mail marks it as quoted (Gmail's, Outlook's, Apple's and
  Yahoo's quote blocks, ``>`` lines with their attribution line); a signature only at the line
  ``-- ``; a forwarded message stays;
- :class:`Body` says what was cut.

The words below are the ones mail programs write into a mail (``Forwarded message``, ``wrote:``),
matched as input.

Layer rules: stdlib only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Final, Literal
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import Iterator

# --- padding and spaces ------------------------------------------------------------------------

_PADDING: Final = re.compile(r"[\u034f\u200b-\u200d\u2060\ufeff\u00ad]")
"""Preheader padding: the combining grapheme joiner, zero-width characters, the word joiner, the
byte order mark and the soft hyphen."""
_ODD_SPACES: Final = re.compile(r"[\u00a0\u2002-\u200a\u202f\u3000]")
"""No-break, figure, thin and ideographic spaces: plain spaces, so a run of them is one."""
_SPACES: Final = re.compile(r"[ \t\r\n\f\v]+")
SIGNATURE: Final = "-- "
"""The line that opens a signature (RFC 3676), trailing space and all."""


def tidy(text: str) -> str:
    """``text`` without padding characters and with its odd spaces plain."""
    return _ODD_SPACES.sub(" ", _PADDING.sub("", text))


# --- links -------------------------------------------------------------------------------------

_BARE_URL: Final = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_URL_TAIL: Final = ".,;:!?)]}>"
_PATH_CHARS: Final = 60


def _site(url: str) -> tuple[str, str]:
    """The host (without ``www.``) and the path (without query or fragment) of an address."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().removeprefix("www.")
    except ValueError:  # a bracket in the host, a port that is no number
        return "", ""
    path = parts.path.rstrip("/")
    if len(path) > _PATH_CHARS:
        path = f"{path[:_PATH_CHARS]}…"
    return host, path


def _bare(found: re.Match[str]) -> str:
    """A bare address as its site and path, without the query; the punctuation after it stays."""
    url = found.group(0)
    tail = ""
    while url and url[-1] in _URL_TAIL:
        url, tail = url[:-1], url[-1] + tail
    host, path = _site(url)
    return f"{host}{path}{tail}" if host else found.group(0)


def sites(text: str) -> str:
    """``text`` with each bare address written as its site and path, without the query."""
    return _BARE_URL.sub(_bare, text)


# --- the HTML tree -----------------------------------------------------------------------------

_VOID: Final = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
     "source", "track", "wbr"},
)
_SKIPPED: Final = frozenset(
    {"head", "script", "style", "title", "template", "svg", "iframe", "object"},
)
_LINES: Final = frozenset(
    {"div", "section", "article", "header", "footer", "main", "nav", "aside", "center", "form",
     "address", "figure", "figcaption", "dl", "dt", "dd", "summary", "details", "fieldset",
     "thead", "tbody", "tfoot", "tr", "td", "th", "caption"},
)
_PARAGRAPHS: Final = frozenset({"p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "hr"})
_LISTS: Final = frozenset({"ul", "ol"})
_TABLE_PARTS: Final = frozenset({"thead", "tbody", "tfoot", "tr", "td", "th"})
_IMPLIED: Final = {
    "li": (frozenset({"li"}), frozenset({"ul", "ol"})),
    "dt": (frozenset({"dt", "dd"}), frozenset({"dl"})),
    "dd": (frozenset({"dt", "dd"}), frozenset({"dl"})),
    "tr": (frozenset({"tr", "td", "th"}), frozenset({"table"})),
    "td": (frozenset({"td", "th"}), frozenset({"tr", "table"})),
    "th": (frozenset({"td", "th"}), frozenset({"tr", "table"})),
    "thead": (_TABLE_PARTS, frozenset({"table"})),
    "tbody": (_TABLE_PARTS, frozenset({"table"})),
    "tfoot": (_TABLE_PARTS, frozenset({"table"})),
}
"""A start tag that ends open elements, until one of its stops: ``<td>`` ends the open cell of
its row, ``<tr>`` the open row, ``<li>`` the open item of its list."""
_DEEPEST: Final = 120
"""Elements nested deeper than this are read as part of the one above: a mail built to blow the
stack gets a flat tail, not a crash."""


class _Node:
    """One element of the tree; its children are text and elements in document order."""

    __slots__ = ("attrs", "children", "depth", "parent", "tag")

    def __init__(self, tag: str, attrs: dict[str, str], parent: _Node | None) -> None:
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.depth: int = parent.depth + 1 if parent is not None else 0
        self.children: list[_Node | str] = []

    def classes(self) -> frozenset[str]:
        """The class tokens, lower-cased."""
        return frozenset(self.attrs.get("class", "").lower().split())

    def elements(self) -> Iterator[_Node]:
        """Every element below this one, in document order."""
        for child in self.children:
            if isinstance(child, _Node):
                yield child
                yield from child.elements()

    def text(self) -> str:
        """The text below this element, whitespace gathered, unrendered."""
        parts = [c if isinstance(c, str) else c.text() for c in self.children]
        return _SPACES.sub(" ", tidy("".join(parts))).strip()


class _Tree(HTMLParser):
    """A tolerant tree of a mail's HTML.

    An unclosed ``p``, ``li``, ``td`` or ``tr`` ends where a browser would end it, a stray end tag
    is ignored, and comments (MSO blocks included) go.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {}, None)
        self._open = self.root

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        self._end_implied(tag)
        node = _Node(tag, {k.lower(): v or "" for k, v in attrs}, self._open)
        self._open.children.append(node)
        if tag not in _VOID and node.depth < _DEEPEST:
            self._open = node

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        node = self._open
        while node.parent is not None and node.tag != tag.lower():
            node = node.parent
        if node.parent is not None:
            self._open = node.parent

    def handle_data(self, data: str) -> None:
        self._open.children.append(data)

    def _end_implied(self, tag: str) -> None:
        if (tag in _PARAGRAPHS or tag in _LISTS or tag == "table") and (
            self._open.tag == "p" and self._open.parent is not None
        ):
            self._open = self._open.parent
        rule = _IMPLIED.get(tag)
        if rule is None:
            return
        closes, stops = rule
        node, outermost = self._open, None
        while node.parent is not None and node.tag not in stops:
            if node.tag in closes:
                outermost = node
            node = node.parent
        if outermost is not None and outermost.parent is not None:
            self._open = outermost.parent


def _parse(html: str) -> _Node:
    tree = _Tree()
    tree.feed(html)
    tree.close()
    return tree.root


# --- what the mail hides -----------------------------------------------------------------------

_HIDING: Final = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|mso-hide\s*:\s*all"
    r"|max-height\s*:\s*0(?:\.0+)?(?:px|pt|em|rem|%)?\s*(?:;|!|$)"
    r"|opacity\s*:\s*0(?:\.0+)?\s*(?:;|!|$)",
    re.IGNORECASE,
)
_NOTHING: Final = r"0(?:\.0+)?(?:px|pt|em|rem|%)?\s*(?:;|!|$)"
_ZERO_FONT: Final = re.compile(rf"font-size\s*:\s*{_NOTHING}", re.IGNORECASE)
_ZERO_SIZE: Final = re.compile(rf"(?:width|height)\s*:\s*{_NOTHING}", re.IGNORECASE)
_TINY: Final = frozenset({"0", "1", "0px", "1px"})


def _hidden(node: _Node) -> bool:
    """Whether the mail hides this element.

    It has ``hidden``, an inline style that hides it, no size (a picture), or ``font-size:0`` with
    no element inside (a spacing trick wraps elements, a hidden preheader only its text).
    """
    if "hidden" in node.attrs:
        return True
    style = node.attrs.get("style", "")
    if _HIDING.search(style):
        return True
    if node.tag == "img":
        width, height = node.attrs.get("width", "").lower(), node.attrs.get("height", "").lower()
        return bool(
            _ZERO_SIZE.search(style) or (width in _TINY and height in _TINY)
            or width == "0" or height == "0",
        )
    return bool(_ZERO_FONT.search(style)) and not any(isinstance(c, _Node) for c in node.children)


# --- quotes, by the mail's own marks -----------------------------------------------------------

_FORWARD_WORDS: Final = (
    r"forwarded message|original message|begin forwarded message"
    r"|转发的邮件|开始转发的邮件|已转发邮件|转发邮件|原始邮件"
)
_FORWARD_LINE: Final = re.compile(rf"^\s*[-_ ]*(?:{_FORWARD_WORDS})", re.IGNORECASE)
_DASHES: Final = re.compile(r"^\s*-{3,}")
_FORWARD_SUBJECT: Final = re.compile(r"^\s*(?:fwd?|转发|轉發)\s*[:\uff1a]", re.IGNORECASE)


def is_forward(subject: str) -> bool:
    """Whether a subject says the mail forwards another (``Fwd:``, ``FW:``, a Chinese one)."""
    return bool(_FORWARD_SUBJECT.match(subject))


def _forwards(node: _Node) -> bool:
    """Whether a quote-like block opens with a forward header: Gmail's dashes, Apple's words."""
    head = node.text()[:200]
    return bool(_DASHES.match(head) or _FORWARD_LINE.match(head))


def _marks(node: _Node) -> Literal["quote", "rest", "forward", ""]:
    """What an element marks.

    ``quote``: an older message; ``rest``: it and all that follows are older messages;
    ``forward``: marked like a quote, but a forwarded message.
    """
    if node.attrs.get("id") in {"divRplyFwdMsg", "appendonsend"}:
        return "rest"
    classes = node.classes()
    if (
        "yahoo_quoted" in classes
        or "gmail_quote" in classes
        or (node.tag == "blockquote" and node.attrs.get("type", "").lower() == "cite")
    ):
        return "forward" if _forwards(node) else "quote"
    return ""


def _drop_attribution(kept: list[_Node | str]) -> None:
    """Thunderbird's attribution line sits just above its quote: it goes with the quote."""
    while kept and isinstance(kept[-1], str) and not kept[-1].strip():
        kept.pop()
    if kept and isinstance(kept[-1], _Node) and "moz-cite-prefix" in kept[-1].classes():
        kept.pop()


def _cut_quotes(parent: _Node, *, forwarded: bool) -> tuple[bool, bool]:
    """Remove the older messages below ``parent``; ``(any removed, what follows is gone too)``.

    A mail whose subject says it forwards keeps its first marked block and what Outlook marks:
    that is the forwarded message. What is quoted inside a forward is cut as anywhere else.
    """
    kept: list[_Node | str] = []
    cut = False
    for child in parent.children:
        if isinstance(child, str):
            kept.append(child)
            continue
        kind = _marks(child)
        if forwarded and kind in {"quote", "rest"}:
            forwarded, kind = forwarded and kind == "rest", "forward"
        if kind == "rest":
            parent.children = kept
            return True, True
        if kind == "quote":
            cut = True
            if child.tag == "blockquote":
                _drop_attribution(kept)
            continue
        inner, rest = _cut_quotes(child, forwarded=forwarded and not kind)
        cut = cut or inner
        kept.append(child)
        if rest and kind != "forward":
            parent.children = kept
            return cut, True
    parent.children = kept
    return cut, False


# --- the HTML as text --------------------------------------------------------------------------

_LINE_END: Final = re.compile(r" +$")


class _Out:
    """Text being written: inline text on the current line, and the breaks blocks ask for."""

    def __init__(self) -> None:
        self._lines: list[str] = []
        self._line = ""
        self._breaks = 0
        self._gap = False
        self._after_link = False

    def text(self, raw: str) -> None:
        """Inline text: its padding gone, its whitespace one space."""
        text = _SPACES.sub(" ", tidy(raw))
        if not text:
            return
        if self._breaks:
            self._end_line()
            self._gap = self._gap or self._breaks >= 2  # noqa: PLR2004 — a blank line, not a count.
            self._breaks = 0
        if not self._line or self._line.endswith(" "):
            text = text.lstrip(" ")
            if not text:
                return
        self._line += text
        self._after_link = False

    def link(self, shown: str) -> None:
        """A link's text: set apart from a link just before it, which has no space between."""
        if self._after_link and not self._breaks and self._line and not self._line.endswith(" "):
            self._line += " "
        self.text(shown)
        self._after_link = True

    def block(self, breaks: int) -> None:
        """A block starts or ends: one line break, or a blank line."""
        self._breaks = max(self._breaks, breaks)

    def line_break(self) -> None:
        """``<br>``: one more line break, two make a blank line."""
        self._breaks = min(self._breaks + 1, 2)

    def _end_line(self) -> None:
        shown = SIGNATURE if self._line == SIGNATURE else _LINE_END.sub("", self._line.lstrip(" "))
        if shown:
            if self._gap and self._lines:
                self._lines.append("")
            self._gap = False
            self._lines.append(shown)
        self._line = ""
        self._after_link = False

    def lines(self) -> list[str]:
        """Every line written, a blank line between paragraphs."""
        self._end_line()
        return self._lines


def _flat(node: _Node) -> str:
    """What is below ``node`` as one line of text."""
    out = _Out()
    _walk(node, out)
    return _SPACES.sub(" ", " ".join(out.lines())).strip()


def _link(node: _Node, out: _Out) -> None:
    """``Pay now (example.com)``: the text and the site it goes to; a bare address, its site."""
    label = _flat(node)
    href = node.attrs.get("href", "").strip()
    if not label:  # a picture that is the link: its alt text is the label
        label = " ".join(
            alt for pic in node.elements() if pic.tag == "img" and not _hidden(pic)
            for alt in [pic.attrs.get("alt", "").strip()] if alt
        )
    scheme = urlsplit(href).scheme.lower() if href else ""
    if scheme == "mailto":
        address = href.split(":", 1)[1].split("?", 1)[0]
        shown = address if not label or label == address else f"{label} ({address})"
    elif scheme in {"http", "https"}:
        host, path = _site(href)
        if not label:
            shown = f"{host}{path}"
        elif not host or label.lower().removeprefix("www.").startswith(host):
            shown = label  # the text already says the site
        else:
            shown = f"{label} ({host})"
    else:
        shown = label
    if shown:
        out.link(shown)


def _cells(row: _Node) -> list[_Node]:
    return [c for c in row.children if isinstance(c, _Node) and c.tag in {"td", "th"}]


def _row(row: _Node, out: _Out) -> None:
    """One row: its cells on a line apart by `` | ``.

    One cell, or cells that run over several lines, write their lines as they are.
    """
    cells: list[list[str]] = []
    for cell in _cells(row):
        if _hidden(cell):
            continue
        part = _Out()
        _walk(cell, part)
        if lines := part.lines():
            cells.append(lines)
    if len(cells) > 1 and all(len(lines) == 1 for lines in cells):
        out.block(1)
        out.text(" | ".join(lines[0] for lines in cells))
        out.block(1)
        return
    for lines in cells:
        out.block(1)
        for line in lines:
            out.block(2 if not line else 1)
            out.text(line)
        out.block(1)


def _table_part(node: _Node, out: _Out) -> None:
    """The rows of a table or of its head, body or foot in order; any other child as it comes."""
    for child in node.children:
        if isinstance(child, _Node) and child.tag == "tr":
            _row(child, out)
        elif isinstance(child, _Node) and child.tag in {"thead", "tbody", "tfoot"}:
            _table_part(child, out)
        else:
            _visit(child, out)


def _table(node: _Node, out: _Out) -> None:
    """A table: each row a line, its cells apart by `` | ``."""
    out.block(2)
    _table_part(node, out)
    out.block(2)


def _raw_text(node: _Node) -> str:
    """The text below ``node`` with its line breaks (``<pre>``)."""
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(child)
        elif child.tag == "br":
            parts.append("\n")
        elif child.tag not in _SKIPPED and not _hidden(child):
            parts.append(_raw_text(child))
    return "".join(parts)


def _walk(node: _Node, out: _Out) -> None:
    for child in node.children:
        _visit(child, out)


def _visit(child: _Node | str, out: _Out) -> None:  # noqa: C901, PLR0912 — one branch per kind.
    if isinstance(child, str):
        out.text(sites(child))
        return
    tag = child.tag
    if tag in _SKIPPED or (tag in _VOID and tag not in {"br", "hr"}) or _hidden(child):
        return
    if tag == "br":
        out.line_break()
    elif tag == "a":
        _link(child, out)
    elif tag == "table":
        _table(child, out)
    elif tag == "pre":
        out.block(2)
        for line in tidy(_raw_text(child)).replace("\r", "").split("\n"):
            out.block(2 if not line.strip() else 1)
            out.text(sites(line))
        out.block(2)
    elif tag == "li":
        out.block(1)
        out.text("- ")
        _walk(child, out)
        out.block(1)
    elif tag in _LISTS:
        nested = child.parent is not None and child.parent.tag == "li"
        out.block(1 if nested else 2)
        _walk(child, out)
        out.block(1 if nested else 2)
    elif tag in _PARAGRAPHS:
        out.block(2)
        if tag != "hr":
            _walk(child, out)
            out.block(2)
    elif tag in _LINES:
        out.block(1)
        _walk(child, out)
        out.block(1)
    else:
        _walk(child, out)


# --- the plain text ----------------------------------------------------------------------------

_QUOTED: Final = re.compile(r"^\s*>")
_COLON: Final = r"\s*[:\uff1a]\s*$"
_ATTRIBUTION: Final = re.compile(
    rf"(?:\bwrote|\bwrites|\bschrieb|\bécrit|\bescribió|\bscritto|写道|寫道){_COLON}|>{_COLON}",
    re.IGNORECASE,
)
_WRAPPED_ON: Final = re.compile(r"^\s*On\b", re.IGNORECASE)
_WROTE_ALONE: Final = re.compile(rf"^\s*wrote{_COLON}", re.IGNORECASE)


def _unflow(text: str, *, delsp: bool) -> str:
    """RFC 3676 ``format=flowed``.

    A line ending in a space continues on the next line of the same quote depth, which loses its
    quote marks and the space put in front of it.
    """
    rows: list[tuple[int, str, bool]] = []  # quote depth, text, whether the line continues
    for line in text.split("\n"):
        depth = len(line) - len(line.lstrip(">"))
        body = line[depth:].removeprefix(" ")
        flowed = body.endswith(" ") and line != SIGNATURE
        rows.append((depth, body[:-1] if flowed and delsp else body, flowed))
    out: list[str] = []
    index = 0
    while index < len(rows):
        depth, body, flowed = rows[index]
        joined = body
        while flowed and index + 1 < len(rows) and rows[index + 1][0] == depth:
            index += 1
            _depth, more, flowed = rows[index]
            joined += more
        out.append(f"{'>' * depth}{' ' if depth else ''}{joined}")
        index += 1
    return "\n".join(out)


def _cut_quoted_lines(lines: list[str]) -> tuple[list[str], bool]:
    """Remove each block of ``>`` lines with the attribution line just above it."""
    kept: list[str] = []
    cut = False
    index = 0
    while index < len(lines):
        if not _QUOTED.match(lines[index]):
            kept.append(lines[index])
            index += 1
            continue
        end = index
        while end < len(lines) and (
            _QUOTED.match(lines[end])
            or (not lines[end].strip() and end + 1 < len(lines) and _QUOTED.match(lines[end + 1]))
        ):
            end += 1
        cut = True
        index = end
        above = len(kept) - 1
        if above >= 0 and not kept[above].strip():  # one blank line may stand between
            above -= 1
        if above >= 0 and _ATTRIBUTION.search(kept[above]):
            if _WROTE_ALONE.match(kept[above]) and above > 0 and _WRAPPED_ON.match(kept[above - 1]):
                above -= 1  # Gmail wraps "On …, Sam <…>" and "wrote:" on two lines
            del kept[above:]
    return kept, cut


def _cut_signature(lines: list[str]) -> tuple[list[str], bool]:
    """Remove a signature: from the line ``-- `` to the end, or to the forward that follows."""
    if SIGNATURE not in lines:
        return lines, False
    start = lines.index(SIGNATURE)
    stop = next(
        (i for i in range(start + 1, len(lines)) if _FORWARD_LINE.match(lines[i])), len(lines),
    )
    return lines[:start] + lines[stop:], True


# --- a body ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Body:
    """A mail's text, and what was left out of it."""

    text: str
    quote_cut: bool = False
    signature_cut: bool = False

    @property
    def cut(self) -> tuple[str, ...]:
        """What was left out: ``quote``, ``signature``."""
        return tuple(
            name for name, gone in (("quote", self.quote_cut), ("signature", self.signature_cut))
            if gone
        )


def tidy_lines(lines: list[str]) -> str:
    """Lines as text: no trailing spaces (but the signature's), no run of blank lines."""
    out: list[str] = []
    for line in lines:
        shown = line if line == SIGNATURE else line.rstrip()
        if shown or (out and out[-1]):
            out.append(shown)
    return "\n".join(out).strip()


def _finish(lines: list[str], *, quote_cut: bool) -> Body:
    """The signature goes; a text that is nothing but a signature is not cut."""
    kept, signed = _cut_signature(lines)
    text = tidy_lines(kept)
    return Body(text, quote_cut, signed) if text else Body(tidy_lines(lines), quote_cut)


def read_html(html: str, *, forwarded: bool = False) -> Body:
    """An HTML body as text (see the module's docstring)."""
    root = _parse(html)
    quoted, _ = _cut_quotes(root, forwarded=forwarded)
    out = _Out()
    _walk(root, out)
    lines = out.lines()
    if quoted and not any(line.strip() for line in lines):  # all of it was quoted: read it all
        fresh = _Out()
        _walk(_parse(html), fresh)
        return _finish(fresh.lines(), quote_cut=False)
    return _finish(lines, quote_cut=quoted)


def read_plain(text: str, *, flowed: bool = False, delsp: bool = False) -> Body:
    """A plain body as text: padding gone, quoted lines and a signature cut, addresses short."""
    text = tidy(text).replace("\r\n", "\n").replace("\r", "\n")
    if flowed:
        text = _unflow(text, delsp=delsp)
    lines = [line if _QUOTED.match(line) else sites(line) for line in text.split("\n")]
    kept, quoted = _cut_quoted_lines(lines)
    if quoted and not any(line.strip() for line in kept):  # all of it was quoted: read it all
        return _finish([sites(line) for line in text.split("\n")], quote_cut=False)
    return _finish(kept, quote_cut=quoted)


_MARKUP: Final = re.compile(
    r"<(?:html|body|div|p|br|table|span|a|img|font|td|tr|ul|li|h[1-6])\b", re.IGNORECASE,
)


def clean(body: str, *, forwarded: bool = False) -> Body:
    """A body the Gmail server chose (the plain part, else the HTML) as text."""
    if _MARKUP.search(body[:5000]):
        return read_html(body, forwarded=forwarded)
    return read_plain(body)


__all__ = [
    "SIGNATURE",
    "Body",
    "clean",
    "is_forward",
    "read_html",
    "read_plain",
    "sites",
    "tidy",
    "tidy_lines",
]
