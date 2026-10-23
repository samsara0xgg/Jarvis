"""ADR 0360, 0011 D5: ``web_fetch``, her one tool for reading a page.

The brain reads the page itself first, through the egress guard with every redirect checked. The
extract service (Tavily) reads it only when the site refused the brain (401, 403, 429), the page has
no readable text without scripts, or it is a PDF; the result says which read it is. A page that did
not load, or is walled, is an error that says what to do next. A read page names its title, the
address it ends at (its canonical address when it names one) and the date it was published, and says
first when that address is not the one asked for. A page longer than a result is read in parts, or
where his question is: its opening and the paragraphs that match ``looking_for``.
"""

from __future__ import annotations

import codecs
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any, Final, Literal
from urllib.parse import parse_qsl, urljoin, urlsplit

import httpx

from jarvis.execution import paragraphs, speakable
from jarvis.execution.tools import (
    Receipted,
    Tool,
    ToolContext,
    ToolError,
    normalize_egress_url,
    resolve_hostname_ips,
    validate_egress_url,
)
from jarvis.shared import CallerPrincipal
from jarvis.shared.receipts import Covered, Receipt
from jarvis.shared.text import truncate_utf8

if TYPE_CHECKING:
    import ipaddress
    from collections.abc import Callable, Mapping, Sequence

    type IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

LOGGER = logging.getLogger(__name__)

_PART_CHARS: Final[int] = 2_600
"""One part of a page. The result carries its title, address, date and first line besides, so the
whole stays under about 4,000 characters and the dispatcher's own window never fires."""
_TITLE_CHARS: Final[int] = 160
_ADDRESS_CHARS: Final[int] = 300
_SHOWN_ADDRESS_CHARS: Final[int] = 80
"""How much of an address the first line says back."""
_ASKED_CHARS: Final[int] = 60
"""How much of what he is looking for the first line and the receipt say back."""
_NO_TEXT_CHARS: Final[int] = 40
"""A page with less text than this has none worth reading without its scripts. A Chinese character
counts as two, for it says as much as two letters."""
_CJK: Final = re.compile(r"[\u3400-\u9fff]")
_TEASER_CHARS: Final[int] = 2_500
"""A page shorter than this that asks him to subscribe is a teaser. A longer page that mentions
subscribing is an article."""
_CHALLENGE_CHARS: Final[int] = 1_500
"""A bot check or a consent page is shorter than this; a page that is not never is one."""
_MAIN_CHARS: Final[int] = 200
"""An article or main region with at least this much text is the page's text, else the body."""

_DESCRIPTION: Final = (
    "Use to read one web page he names or you found: an article, a post, a manual, a PDF. "
    "Not for finding pages or what the web says on a subject (web_search), nor a place's "
    "details (map_search). Returns the page's title, address and published date with its text: "
    "the whole page if short, else a part or the paragraphs that match looking_for; a page that "
    "did not load or is walled is an error."
)

_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "description": "The page's http(s) address, as he said it or as web_search gave it.",
        },
        "looking_for": {
            "type": "string",
            "description": "What he wants from a long page, in his words. Returns the opening "
            "and the paragraphs that match best; leave out to read from the start.",
        },
        "page": {
            "type": "integer",
            "minimum": 1,
            "description": "Which part of a long page, from 1; the result says how many there "
            "are. Not used with looking_for.",
        },
    },
    "required": ["url"],
}

# --- reading the page directly -----------------------------------------------------------------

_WEB_FETCH_HEADERS: Final[Mapping[str, str]] = {
    # httpx's default UA (`python-httpx/x.y.z`) is refused outright by a meaningful slice of the
    # web. Wikipedia answers it with a bare 403 whose body reads "Please set a user-agent and
    # respect our robot policy".
    #
    # Self-identifying (a real name plus a contact URL) is what the sites asking for a UA actually
    # want, and it keeps this honest: `web_fetch` does not pretend to be a browser, and pairing a
    # browser UA with a client that runs no JS earns bot-detection blocks rather than avoiding
    # them.
    "User-Agent": "Jarvis/1.0 (+https://github.com/alllllenshi/jarvis)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.5",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
"""Request headers for every `web_fetch` hop (see the UA note above)."""

_MAX_REDIRECTS: Final[int] = 3
"""Hard ceiling on redirects FOLLOWED (ADR-0011 D5: "at most 3 redirects"): a 4th redirect
response is refused rather than followed."""

_REDIRECT_STATUS_CODES: Final[frozenset[int]] = frozenset({301, 302, 303, 307, 308})


def _parse_content_length(raw: str | None) -> int | None:
    """Parse a `Content-Length` header value, or `None` if absent/junk."""
    if raw is None:
        return None
    try:
        value = int(raw.strip())
    except ValueError:
        return None
    return value if value >= 0 else None


@dataclass(frozen=True)
class _HopResponse:
    """One non-redirect-following HTTP response."""

    status_code: int
    location: str | None
    content_type: str | None
    body: bytes
    total_bytes: int
    total_bytes_exact: bool = True
    """False iff `total_bytes` is a lower bound: the drain stopped at `max_bytes` on a response
    with no usable `Content-Length`. `body` is always the true prefix actually read."""


class _FetchDeadlineExceededError(Exception):
    """The overall wall-clock fetch budget elapsed mid-drain; never escapes this module."""


def _http_get_one_hop(
    url: str, *, timeout_s: float, max_bytes: int, deadline: float,
) -> _HopResponse:
    """Issue ONE GET with redirects disabled, streamed, body capped at `max_bytes`.

    The caller owns redirect-following, so every hop's `Location` can be re-validated by the SSRF
    guard before it is followed (ADR-0011 D5 point 5). The drain STOPS as soon as `max_bytes` is
    collected, and `deadline` (an absolute `time.monotonic()` value shared by every hop) is checked
    every chunk, so a slow-loris server that never trips httpx's per-read timeout is still cut off.
    """
    with (
        httpx.Client(timeout=timeout_s, follow_redirects=False, trust_env=False) as client,
        client.stream("GET", url, headers=_WEB_FETCH_HEADERS) as response,
    ):
        content_type = response.headers.get("content-type")
        location = response.headers.get("location")
        declared_length = _parse_content_length(response.headers.get("content-length"))
        chunks: list[bytes] = []
        collected = 0
        stopped_early = False
        for chunk in response.iter_bytes():
            if collected < max_bytes:
                chunks.append(chunk[: max_bytes - collected])
            collected += len(chunk)
            if collected >= max_bytes:
                stopped_early = True
                break
            if time.monotonic() >= deadline:
                msg = f"exceeded the fetch deadline after {collected} bytes"
                raise _FetchDeadlineExceededError(msg)
        body = b"".join(chunks)
        if declared_length is not None:
            total_bytes, total_bytes_exact = declared_length, True
        elif stopped_early:
            total_bytes, total_bytes_exact = collected, False
        else:
            total_bytes, total_bytes_exact = collected, True
        return _HopResponse(
            status_code=response.status_code,
            location=location,
            content_type=content_type,
            body=body,
            total_bytes=total_bytes,
            total_bytes_exact=total_bytes_exact,
        )


@dataclass(frozen=True)
class _FetchOutcome:
    """Result of :func:`_fetch_url_backend` — success fields XOR error fields."""

    ok: bool
    status_code: int = 0
    content_type: str | None = None
    body: bytes = b""
    total_bytes: int = 0
    final_url: str = ""
    error_code: str | None = None
    error_message: str | None = None


def _fetch_url_backend(  # noqa: PLR0911 — one linear guard/canonicalize/fetch/redirect loop; each return is a distinct named terminal outcome.
    url: str,
    *,
    timeout_s: float,
    max_bytes: int,
    max_redirects: int = _MAX_REDIRECTS,
    resolver: Callable[[str], Sequence[IPAddress]] | None = None,
) -> _FetchOutcome:
    """Guarded GET with manual redirect walking (ADR-0011 D5).

    Validates EVERY hop — the initial `url` and each `Location` header — through
    `validate_egress_url` before issuing that hop's request, and always fetches
    `normalize_egress_url`'s canonical form of that hop, never the raw string (the validated string
    must be the used string). A guard refusal at any hop, and exceeding `max_redirects`, both return
    `ok=False` with a named `error_code`; neither ever raises past this function.

    A single wall-clock `deadline` is set ONCE from `timeout_s` and shared across every hop, so no
    redirect chain can make one `web_fetch` call run longer than `timeout_s` in total.

    `resolver` defaults to `None`, resolved to `resolve_hostname_ips` INSIDE the body, so a test
    that substitutes it reaches every caller that does not pass one.
    """
    resolve = resolver if resolver is not None else resolve_hostname_ips
    deadline = time.monotonic() + timeout_s
    current = url
    hops_followed = 0
    while True:
        allowed, reason = validate_egress_url(current, resolver=resolve)
        if not allowed:
            return _FetchOutcome(
                ok=False,
                error_code="ssrf_guard_refused",
                error_message=(
                    f"web_fetch: {current!r} was refused ({reason}); only public http(s) "
                    "pages can be fetched"
                ),
            )

        normalized = normalize_egress_url(current)
        if normalized is None:
            # Should be unreachable — `validate_egress_url` just allowed `current` via the
            # identical parser — but refuse rather than ever fall back to the raw string.
            return _FetchOutcome(
                ok=False,
                error_code="ssrf_guard_refused",
                error_message=f"web_fetch: {current!r} is not a usable address; check the URL",
            )

        remaining_s = deadline - time.monotonic()
        if remaining_s <= 0:
            return _slow(timeout_s)

        try:
            hop = _http_get_one_hop(
                normalized, timeout_s=remaining_s, max_bytes=max_bytes, deadline=deadline,
            )
        except _FetchDeadlineExceededError:
            return _slow(timeout_s)
        except httpx.HTTPError as exc:
            return _FetchOutcome(
                ok=False,
                error_code="site_failed",
                error_message=(
                    f"web_fetch: the site did not answer ({exc}); tell him it did not load, "
                    "or try web_search for the same information"
                ),
            )

        if hop.status_code in _REDIRECT_STATUS_CODES:
            if hop.location is None:
                return _FetchOutcome(
                    ok=False,
                    error_code="site_failed",
                    error_message=(
                        f"web_fetch: {normalized!r} redirected to nowhere; tell him the page is "
                        "broken, or try web_search"
                    ),
                )
            if hops_followed >= max_redirects:
                return _FetchOutcome(
                    ok=False,
                    error_code="too_many_redirects",
                    error_message=(
                        f"web_fetch: {url!r} redirected more than {max_redirects} times; "
                        "try the address it ends at, or web_search"
                    ),
                )
            hops_followed += 1
            current = urljoin(normalized, hop.location)
            continue

        return _FetchOutcome(
            ok=True,
            status_code=hop.status_code,
            content_type=hop.content_type,
            body=hop.body,
            total_bytes=hop.total_bytes,
            final_url=normalized,
        )


def _slow(timeout_s: float) -> _FetchOutcome:
    """The outcome of a site that took longer than the fetch budget."""
    return _FetchOutcome(
        ok=False,
        error_code="site_failed",
        error_message=(
            f"web_fetch: the page took longer than {timeout_s:g}s to load; tell him it did "
            "not load, or try web_search"
        ),
    )


def _charset(content_type: str | None) -> str | None:
    """The `charset=` parameter of a `Content-Type` header value."""
    for part in (content_type or "").split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.strip().lower() == "charset":
            return value.strip().strip('"').strip("'") or None
    return None


def _decode(body: bytes, total_bytes: int, content_type: str | None) -> tuple[str, int]:
    """``(text, undelivered_bytes)`` honouring a declared `charset=`.

    UTF-8 (declared, or no usable charset named) goes through `truncate_utf8`, whose
    codepoint-safe boundary handling is not duplicated here; a charset Python knows is decoded with
    ``errors="replace"``, so a body cut mid-character still reads.
    """
    charset = _charset(content_type)
    if charset is None or charset.lower().replace("_", "-") in ("utf-8", "utf8"):
        text, undelivered, _lossy = truncate_utf8(body, total_bytes)
        return text, undelivered
    try:
        name = codecs.lookup(charset).name
        return body.decode(name, errors="replace"), max(0, total_bytes - len(body))
    except LookupError:  # an unknown charset, or a registered one that is not text
        text, undelivered, _lossy = truncate_utf8(body, total_bytes)
        return text, undelivered


# --- the page's own marks ----------------------------------------------------------------------

_DROP: Final = frozenset({"script", "style", "noscript", "template", "svg", "canvas", "iframe"})
_CHROME: Final = frozenset({"nav", "footer", "aside", "form", "button", "select", "dialog", "menu"})
_BLOCK: Final = frozenset({
    "p", "div", "section", "article", "main", "h1", "h2", "h3", "h4", "h5", "h6", "li", "ul", "ol",
    "dl", "dt", "dd", "blockquote", "pre", "table", "tr", "figure", "figcaption", "details",
    "summary", "address", "hr", "header", "body",
})
_SPACE: Final = frozenset({"br", "td", "th", "option"})
"""Tags that end a word but not a paragraph."""
_ISO_DAY: Final = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass
class _Doc:
    """What a page says about itself and its text as paragraphs."""

    paragraphs: list[str] = field(default_factory=list)
    title: str | None = None
    og_title: str | None = None
    canonical: str | None = None
    og_url: str | None = None
    published_meta: str | None = None
    published_ld: str | None = None
    free: bool | None = None
    """schema.org ``isAccessibleForFree``: ``False`` when any part of the page says it is not."""


class _PageParser(HTMLParser):
    """An HTML document as its marks and its paragraphs (stdlib only, ADR-0011 D5).

    Text is cut into paragraphs at block tags; `<script>`, `<style>` and the like are dropped
    (a JSON-LD script is read for ``datePublished`` and ``isAccessibleForFree``), and so is a
    navigation, footer, aside or form. An `<article>` or `<main>` with text in it is the page's
    text; without one, the whole body is.
    """

    def __init__(self) -> None:
        """Start empty."""
        super().__init__(convert_charrefs=True)
        self.doc = _Doc()
        self._main_paragraphs: list[str] = []
        self._buffer: list[str] = []
        self._title: list[str] = []
        self._ld: list[str] | None = None
        self._in_title = False
        self._item = False
        self._after_item = False
        self._drop = 0
        self._chrome = 0
        self._main = 0
        self._open: dict[str, int] = {}

    def _flush(self) -> None:
        text = " ".join("".join(self._buffer).split())
        self._buffer = []
        if not text:
            return
        item, self._item = self._item, False
        # A run of list items is one paragraph, a line each.
        join = item and self._after_item
        self._after_item = item
        text = f"- {text}" if item else text
        self._add(self.doc.paragraphs, text, join=join)
        if self._main:
            self._add(self._main_paragraphs, text, join=join)

    @staticmethod
    def _add(into: list[str], text: str, *, join: bool) -> None:
        if join and into:
            into[-1] = f"{into[-1]}\n{text}"
        else:
            into.append(text)

    def _meta(self, attrs: Mapping[str, str]) -> None:
        content = attrs.get("content", "").strip()
        if not content:
            return
        for key in (attrs.get("property", ""), attrs.get("name", ""), attrs.get("itemprop", "")):
            match key.lower():
                case "og:title":
                    self.doc.og_title = self.doc.og_title or content
                case "og:url":
                    self.doc.og_url = self.doc.og_url or content
                case "article:published_time" | "og:article:published_time":
                    self.doc.published_meta = self.doc.published_meta or content

    def _read_ld(self, raw: str) -> None:
        """Take ``datePublished`` and ``isAccessibleForFree`` from a JSON-LD script."""
        try:
            node = json.loads(raw)
        except ValueError:
            return
        stack: list[Any] = [node]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                for key, value in item.items():
                    if key == "isAccessibleForFree" and str(value).strip().lower() in {
                        "false", "0", "no",
                    }:
                        self.doc.free = False
                    elif key == "datePublished" and isinstance(value, str):
                        self.doc.published_ld = self.doc.published_ld or value.strip()
                    elif isinstance(value, (dict, list)):
                        stack.append(value)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Count the regions being entered and take the marks."""
        found = {name.lower(): value or "" for name, value in attrs}
        if tag == "meta":
            self._meta(found)
        elif tag == "link":
            if "canonical" in found.get("rel", "").lower().split() and found.get("href"):
                self.doc.canonical = self.doc.canonical or found["href"].strip()
        elif tag in _DROP:
            self._drop += 1
            if tag == "script" and "ld+json" in found.get("type", "").lower():
                self._ld = []
        elif not self._drop:
            self._begin(tag)

    def _begin(self, tag: str) -> None:
        """A tag opens outside anything dropped: a title, a space, or a block or region."""
        if tag == "title":
            self._in_title = True
        elif tag in _SPACE:
            self._buffer.append(" ")
        elif tag in _BLOCK or tag in _CHROME:
            self._flush()
            if tag in _CHROME or (tag == "header" and not self._main):
                self._chrome += 1
                self._open[tag] = self._open.get(tag, 0) + 1
            elif tag in {"article", "main"}:
                self._main += 1
                self._open[tag] = self._open.get(tag, 0) + 1
            if tag == "li":
                self._item = True

    def handle_endtag(self, tag: str) -> None:
        """Leave the regions entered; end a paragraph at a block tag."""
        if tag in _DROP:
            if tag == "script" and self._ld is not None:
                self._read_ld("".join(self._ld))
                self._ld = None
            self._drop = max(0, self._drop - 1)
        elif self._drop:
            return
        elif tag == "title":
            self._in_title = False
        elif tag in _SPACE:
            self._buffer.append(" ")
        elif tag in _BLOCK or tag in _CHROME:
            self._flush()
            if self._open.get(tag, 0) > 0:
                self._open[tag] -= 1
                if tag in {"article", "main"}:
                    self._main = max(0, self._main - 1)
                else:
                    self._chrome = max(0, self._chrome - 1)

    def handle_data(self, data: str) -> None:
        """Keep the text outside what is dropped; the title and a JSON-LD script apart."""
        if self._ld is not None:
            self._ld.append(data)
        elif self._drop:
            return
        elif self._in_title:
            self._title.append(data)
        elif not self._chrome:
            self._buffer.append(data)

    def finish(self) -> _Doc:
        """The page: its title, marks and paragraphs."""
        self._flush()
        self.doc.title = " ".join("".join(self._title).split()) or None
        main = self._main_paragraphs
        if sum(len(one) for one in main) >= _MAIN_CHARS:
            self.doc.paragraphs = main
        return self.doc


def _parse_html(text: str) -> _Doc:
    """``text`` as a page."""
    parser = _PageParser()
    parser.feed(text)
    parser.close()
    return parser.finish()


def _day(raw: str) -> str:
    """A published time as its day when it starts with one, else as written, short."""
    found = _ISO_DAY.match(raw.strip())
    if found is not None:
        try:
            date.fromisoformat(found.group(0))
        except ValueError:
            pass
        else:
            return found.group(0)
    return speakable.cut(raw, 40)


def _no_text(text: str) -> bool:
    """Whether a text is too little to be what a page says."""
    return len(text) + len(_CJK.findall(text)) < _NO_TEXT_CHARS


# --- walls -------------------------------------------------------------------------------------

_CHALLENGE: Final = re.compile(
    r"just a moment|attention required|checking your browser|verify(?:ing)? (?:that )?you are "
    r"(?:a )?human|are you a (?:robot|human)|unusual traffic|enable javascript and cookies|"
    r"captcha|access denied|request blocked|security check|ddos protection|请完成安全验证|"
    r"人机验证|正在验证",
    re.IGNORECASE,
)
_CONSENT: Final = re.compile(
    r"before you continue|we value your privacy|accept all cookies|reject all|"
    r"manage (?:your )?(?:cookie|consent|privacy)|cookie (?:preferences|settings)|"
    r"使用 ?cookie|同意.{0,8}cookie",
    re.IGNORECASE,
)
_SUBSCRIBE: Final = re.compile(
    r"subscribe (?:now )?to (?:continue|keep|read|unlock|get unlimited)|subscribers?[ -]only|"
    r"(?:only )?for (?:paid )?subscribers|(?:sign|log) ?in to (?:continue|keep|read)|"
    r"(?:continue|keep) reading (?:this|the|with|by)|already a subscriber|"
    r"(?:become|join as) a (?:paid )?member to (?:read|continue)|"
    r"create a free account to (?:continue|read)|unlock this (?:article|story)|"
    r"订阅后(?:阅读|查看|可见)|登录后(?:阅读|查看)|付费(?:阅读|内容)|仅限(?:订阅|会员)|"
    r"开通会员|订阅(?:以|后)?继续阅读",
    re.IGNORECASE,
)


def _wall(
    text: str, *, title: str | None, free: bool | None, whole: bool = True,
) -> str | None:
    """What walls this page, in words, or ``None`` when it reads.

    A page marked not free by schema.org is a paywall. A bot check or a consent page is short and
    says so in its title or its first lines; a teaser is short and asks him to subscribe or sign
    in. A page longer than that is an article, whatever it says about subscribing: so is a page
    whose download was cut (``whole`` false), which was longer than what is left of it.
    """
    if free is False:
        return "a paywall: the page marks its text as not free to read"
    if len(text) < _CHALLENGE_CHARS:
        head = f"{title or ''}\n{text[:600]}"
        if _CHALLENGE.search(head):
            return "a bot check"
        if _CONSENT.search(head):
            return "a cookie consent page"
    if whole and len(text) < _TEASER_CHARS and _SUBSCRIBE.search(text):
        return "a paywall: only a teaser is shown"
    return None


# --- the extract service -----------------------------------------------------------------------

_EXTRACT_URL: Final[str] = "https://api.tavily.com/extract"
_HEADING: Final = re.compile(r"^#{1,3}\s+(.+?)\s*#*\s*$", re.MULTILINE)


@dataclass(frozen=True)
class _Extracted:
    """A page as the extract service read it: where it ended and its text."""

    address: str
    text: str


def _extract(url: str, *, api_key: str, timeout_s: float) -> _Extracted | None:
    """Tavily `/extract` (basic depth) for one URL: markdown text, PDFs included.

    ``None`` when the service failed, answered no page or no text; the reason is logged by type
    only, never with the request.
    """
    try:
        response = httpx.post(
            _EXTRACT_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"urls": [url], "extract_depth": "basic"},
            timeout=timeout_s,
        )
        response.raise_for_status()
        data = response.json()
        first = (data.get("results") or [None])[0]
        text = str(first.get("raw_content") or "").strip() if isinstance(first, dict) else ""
    except (httpx.HTTPError, ValueError, AttributeError, TypeError) as exc:
        LOGGER.warning("web_fetch: the extract service failed (%s)", type(exc).__name__)
        return None
    if _no_text(text) or not isinstance(first, dict):
        return None
    return _Extracted(str(first.get("url") or url), text)


# --- the read ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Read:
    """A page as read: its text and what it says about itself."""

    text: str
    title: str | None
    address: str
    published: str | None
    read_by: Literal["direct", "extract"]
    why_extract: str | None = None
    download_cut: bool = False


def _short(address: str) -> str:
    """An address as host and path, short, for a sentence."""
    parts = urlsplit(address)
    shown = f"{parts.hostname or ''}{parts.path}"
    if parts.query:
        shown += f"?{parts.query}"
    return speakable.cut(shown.removeprefix("www."), _SHOWN_ADDRESS_CHARS)


def _identity(address: str) -> tuple[str, str, tuple[tuple[str, str], ...]]:
    """What makes an address a page: host, path and query.

    The scheme, ``www.``, a trailing slash and any ``utm_`` parameter do not.
    """
    parts = urlsplit(address)
    host = (parts.hostname or "").lower().removeprefix("www.")
    query = tuple(sorted(
        (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
    ))
    return host, parts.path.rstrip("/"), query


def _page_address(doc: _Doc, final_url: str) -> str:
    """The page's canonical address, else its Open Graph one, else where the read ended.

    One that is not a web address, or is on no host, is not taken.
    """
    for named in (doc.canonical, doc.og_url):
        if named:
            joined = urljoin(final_url, named)
            parts = urlsplit(joined)
            if parts.scheme in {"http", "https"} and parts.hostname:
                return joined
    return final_url


def _heading(markdown: str) -> str | None:
    """The first heading of a markdown text."""
    found = _HEADING.search(markdown)
    return found.group(1) if found else None


def _not_found(url: str, status: int) -> ToolError:
    """404 and 410: the address is wrong or the page is gone."""
    msg = (
        f"web_fetch: the page was not found ({status}) at {_short(url)}; check the address, "
        "or find the right page with web_search"
    )
    return ToolError(msg, code="page_not_found", kind="retry_other_args")


def _refused(url: str, status: int, *, extract_tried: bool) -> ToolError:
    """401, 403, 429, 451 and any other 4xx, when the extract service could not read it."""
    also = " and the extract service could not read it either" if extract_tried else ""
    msg = (
        f"web_fetch: the site refused to give the page ({status}) at {_short(url)}{also}; "
        "tell him, or look for the same story on another site with web_search"
    )
    return ToolError(msg, code="page_refused", kind="impossible")


def _site_failed(url: str, status: int) -> ToolError:
    """5xx, and any status that is no page."""
    msg = (
        f"web_fetch: the site failed ({status}) at {_short(url)}; tell him it is not "
        "working now, or look for another source with web_search"
    )
    return ToolError(msg, code="site_failed", kind="dont_retry")


def _walled(url: str, why: str) -> ToolError:
    """A paywall, a bot check or a consent page."""
    msg = (
        f"web_fetch: the page at {_short(url)} is behind {why}; tell him, or look for the "
        "same story on another site with web_search"
    )
    return ToolError(msg, code="page_walled", kind="impossible")


def _unreadable(url: str, why: str) -> ToolError:
    """A page the brain cannot read as text and the extract service did not."""
    msg = (
        f"web_fetch: {why} at {_short(url)}; tell him, or look for another source with web_search"
    )
    return ToolError(msg, code="page_unreadable", kind="impossible")


def _from_extract(
    url: str, why: str, *, api_key: str | None, timeout_s: float, title: str | None = None,
) -> _Read | None:
    """The page as the extract service read it, or ``None`` with no service or when it failed."""
    if not api_key:
        return None
    got = _extract(url, api_key=api_key, timeout_s=timeout_s)
    if got is None:
        return None
    if (why_walled := _wall(got.text, title=title, free=None)) is not None:
        raise _walled(url, why_walled)
    return _Read(got.text, _heading(got.text), got.address, None, "extract", why)


def _kind(outcome: _FetchOutcome) -> Literal["html", "text", "pdf", "other"]:
    """What the 2xx response is, by its content type, else by its first bytes."""
    mime = (outcome.content_type or "").split(";", 1)[0].strip().lower()
    head = outcome.body.lstrip(b"\xef\xbb\xbf \t\r\n")[:8].lower()
    if mime == "application/pdf" or head.startswith(b"%pdf"):
        return "pdf"
    if mime in {"text/html", "application/xhtml+xml"} or (not mime and head.startswith(b"<")):
        return "html"
    if mime.startswith("text/") or mime in {"application/json", "application/xml"}:
        return "text"
    return "other"


_NOT_FOUND: Final = frozenset({404, 410})
_EXTRACTABLE: Final = frozenset({401, 403, 429})
"""The refusals a read by the extract service may get past."""
_FIRST_CLIENT_ERROR: Final = 400
_FIRST_SERVER_ERROR: Final = 500


def _read(url: str, *, timeout_s: float, max_bytes: int, extract_key: str | None) -> _Read:
    """Read ``url`` directly; the extract service only where the direct read cannot (ADR 0360)."""
    outcome = _fetch_url_backend(url, timeout_s=timeout_s, max_bytes=max_bytes)
    if not outcome.ok:
        raise ToolError(
            outcome.error_message or "web_fetch: the page could not be fetched; tell him",
            code=outcome.error_code or "site_failed",
        )
    status = outcome.status_code
    if status in _NOT_FOUND:
        raise _not_found(outcome.final_url, status)
    if status in _EXTRACTABLE:
        return _read_refused(url, outcome, timeout_s=timeout_s, extract_key=extract_key)
    if status >= _FIRST_CLIENT_ERROR and status < _FIRST_SERVER_ERROR:
        raise _refused(outcome.final_url, status, extract_tried=False)
    if not 200 <= status < 300:  # noqa: PLR2004 — the successful statuses
        raise _site_failed(outcome.final_url, status)
    return _read_page(url, outcome, timeout_s=timeout_s, extract_key=extract_key)


def _read_refused(
    url: str, outcome: _FetchOutcome, *, timeout_s: float, extract_key: str | None,
) -> _Read:
    """A site that refused the direct read: the extract service may read it, else it is an error."""
    status = outcome.status_code
    got = _from_extract(
        url, f"the site refused it ({status})", api_key=extract_key, timeout_s=timeout_s,
    )
    if got is not None:
        return got
    text, _ = _decode(outcome.body, outcome.total_bytes, outcome.content_type)
    doc = _parse_html(text) if _kind(outcome) == "html" else _Doc()
    why = _wall("\n\n".join(doc.paragraphs), title=doc.title, free=None)
    if why is not None:
        raise _walled(outcome.final_url, why)
    raise _refused(outcome.final_url, status, extract_tried=extract_key is not None)


def _read_page(
    url: str, outcome: _FetchOutcome, *, timeout_s: float, extract_key: str | None,
) -> _Read:
    """A page the site gave: a PDF or a page with no text goes to the extract service."""
    final, kind = outcome.final_url, _kind(outcome)
    if kind == "pdf":
        got = _from_extract(url, "it is a PDF", api_key=extract_key, timeout_s=timeout_s)
        if got is None:
            raise _unreadable(
                final,
                "it is a PDF, which the extract service could not read" if extract_key
                else "it is a PDF, and no extract service is set up to read one",
            )
        return got
    if kind == "other":
        mime = (outcome.content_type or "unknown type").split(";", 1)[0].strip()
        raise _unreadable(final, f"it is {mime}, not a page of text")
    text, undelivered = _decode(outcome.body, outcome.total_bytes, outcome.content_type)
    if kind == "html":
        doc = _parse_html(text)
    else:
        doc = _Doc(paragraphs=paragraphs.split(text, _PART_CHARS))
    body = "\n\n".join(doc.paragraphs)
    why = _wall(body, title=doc.title, free=doc.free, whole=undelivered == 0)
    if why is not None:
        raise _walled(final, why)
    if _no_text(body):
        got = _from_extract(
            url, "the page has no text without its scripts", api_key=extract_key,
            timeout_s=timeout_s, title=doc.title,
        )
        if got is None:
            msg = "the page has no text without running its scripts, which this tool does not do"
            raise _unreadable(final, msg)
        return got
    title = doc.og_title or doc.title
    published = doc.published_meta or doc.published_ld
    return _Read(
        body, speakable.cut(title, _TITLE_CHARS) if title else None,
        _page_address(doc, final), _day(published) if published else None, "direct",
        download_cut=undelivered > 0,
    )


# --- the result --------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Shown:
    """The part of a page this result carries, and how it was chosen."""

    text: str
    how: str
    shown: int
    more: int
    how_more: str
    page: int | None = None
    parts: int | None = None


def _choose(text: str, looking_for: str | None, page: int | None) -> _Shown:
    """The part of ``text`` to show: all of it, a part, or the paragraphs that match."""
    pieces = paragraphs.parts(text, _PART_CHARS)
    if len(pieces) <= 1:
        return _Shown(text, "the whole page", 1, 0, "")
    if looking_for:
        total = len(paragraphs.split(text, _PART_CHARS))
        picked = paragraphs.pick(text, looking_for, _PART_CHARS)
        chosen = total - picked.left_out
        asked = speakable.cut(looking_for, _ASKED_CHARS)
        if chosen <= 1:
            how = (
                f"only the opening paragraph; no other of its {total} paragraphs matches "
                f"'{asked}', so try the words the page itself uses"
            )
        else:
            how = (
                f"the opening and {chosen - 1} paragraphs that best match '{asked}', "
                f"{picked.left_out} of {total} paragraphs left out"
            )
        if page is not None:
            how += "; page was not used"
        return _Shown(picked.text, how, chosen, picked.left_out, "page=1.. reads it in parts")
    number = page or 1
    if number > len(pieces):
        msg = (
            f"web_fetch: the page has {len(pieces)} parts, so page {number} does not exist; "
            f"ask for page 1 to {len(pieces)}"
        )
        raise ToolError(msg, code="invalid_arguments", kind="retry_other_args")
    more = len(pieces) - 1
    how = f"part {number} of {len(pieces)}"
    if number < len(pieces):
        how += f", page={number + 1} reads the next"
    return _Shown(
        pieces[number - 1], how, 1, more, f"page={number + 1}" if number < len(pieces) else "",
        page=number, parts=len(pieces),
    )


def _whole_address(address: str) -> str:
    """The address as it is; one longer than a result has room for loses its query, then its end."""
    if len(address) <= _ADDRESS_CHARS:
        return address
    bare = address.split("?", 1)[0]
    return bare if len(bare) <= _ADDRESS_CHARS else f"{bare[: _ADDRESS_CHARS - 1]}\u2026"


def _published(published: str | None, today: date) -> str:
    """``, published yesterday (Tue)`` for a day, else the date as the page wrote it."""
    if published is None:
        return ""
    try:
        return f", published {speakable.day_name(date.fromisoformat(published), today)}"
    except ValueError:
        return f", published {published}"


def _result(read: _Read, asked: str, shown: _Shown, *, today: date) -> Receipted:
    """A read page as the tool's result, led by one line she can say."""
    site = (urlsplit(read.address).hostname or "").removeprefix("www.")
    lead = ""
    if _identity(read.address) != _identity(asked):
        lead = (
            f"The address of this page is {_short(read.address)}, not the one you gave "
            f"({_short(asked)}). "
        )
    what = f"'{read.title}' on {site}" if read.title else f"the page on {site}"
    how_read = "read directly" if read.read_by == "direct" else (
        f"read by the extract service because {read.why_extract}"
    )
    summary = f"{lead}Read {what}{_published(read.published, today)}: {shown.how}; {how_read}."
    if read.download_cut:
        summary += " The download stopped at its size limit, so the end of the page was not read."
    payload: dict[str, Any] = {"summary": summary}
    if read.title:
        payload["title"] = read.title
    payload["address"] = _whole_address(read.address)
    if read.published:
        payload["published"] = read.published
    payload["read_by"] = read.read_by
    if shown.page is not None:
        payload["page"], payload["parts"] = shown.page, shown.parts
    elif shown.more:
        payload["left_out"] = shown.more
    payload["content"] = shown.text
    window = f"'{read.title}': {shown.how}" if read.title else shown.how
    return Receipted(
        payload,
        Receipt(
            "done", reads_only=True,
            line=f"read {site}: {shown.how}",
            covered=Covered((site,), window, shown.shown, shown.more, shown.how_more),
        ),
    )


def _arguments(args: Mapping[str, Any]) -> tuple[str, str | None, int | None]:
    """The url, what he is looking for and the part wanted; the error where they are wrong."""
    url = args.get("url")
    if not isinstance(url, str) or not url.strip():
        msg = f"web_fetch: url must be the page address, http or https (got {url!r})"
        raise ToolError(msg, code="invalid_arguments")
    looking_for = args.get("looking_for")
    wanted = looking_for.strip() if isinstance(looking_for, str) and looking_for.strip() else None
    page = args.get("page")
    if page is not None:
        whole = isinstance(page, int) and not isinstance(page, bool)
        if not whole and isinstance(page, str) and page.strip().isdigit():
            page, whole = int(page), True
        if not whole or page < 1:
            msg = f"web_fetch: page must be a whole number from 1 (got {page!r})"
            raise ToolError(msg, code="invalid_arguments")
    return url.strip(), wanted, page


def make_web_fetch(
    *, max_bytes: int, max_text_chars: int, timeout_s: float, extract_api_key: str | None,
) -> Tool:
    """Bind the `tools.web.*` fetch knobs and the extract service's key into `web_fetch`."""

    def web_fetch(args: Mapping[str, Any], ctx: ToolContext) -> Receipted:
        url, looking_for, page = _arguments(args)
        allowed, reason = validate_egress_url(url)
        if not allowed:
            msg = (
                f"web_fetch: {url!r} was refused ({reason}); only public http(s) pages "
                "can be fetched"
            )
            raise ToolError(msg, code="ssrf_guard_refused")
        read = _read(url, timeout_s=timeout_s, max_bytes=max_bytes, extract_key=extract_api_key)
        shown = _choose(read.text, looking_for, page)
        return _result(read, url, shown, today=ctx.clock.now().date())

    return Tool(
        name="web_fetch",
        description=_DESCRIPTION,
        input_schema=_SCHEMA,
        handler=web_fetch,
        allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
        risk_level="L1",
        read_only=True,
        max_result_chars=max_text_chars,
    )


__all__ = ["make_web_fetch"]
