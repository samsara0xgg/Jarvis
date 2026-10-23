"""ADR 0358, 0289: ``web_search``, her one tool for what the web says.

A quick search asks one provider (Tavily, Exa or ddgs) for excerpts of pages. It gives each row
its site and the date the provider knows (or says it is unknown), opens no page and says so in the
first line, and keeps to the range and topic his words asked for (``within``, ``news``). An empty
search says what was searched. A deep search is one nested request to Anthropic's own web search,
behind the :class:`DeepSearch` seam the runtime fills.
"""

from __future__ import annotations

import html
import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol
from urllib.parse import urlsplit

import httpx

from jarvis.execution import paragraphs, speakable
from jarvis.execution.tools import (
    DEFAULT_WEB_SEARCH_PROVIDER,
    Receipted,
    Tool,
    ToolContext,
    ToolError,
)
from jarvis.shared import CallerPrincipal
from jarvis.shared.receipts import Covered, Receipt

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import tzinfo

    from jarvis.shared.clock import Clock
    from jarvis.shared.here import Coarse

LOGGER = logging.getLogger(__name__)

_MAX_RESULTS_CAP: Final[int] = 8
"""Hard ceiling (ADR-0011 D5) on rows asked of a provider: not configurable, unlike the default
(`tools.web.search_max_results`), and applied whatever the request or the config asks for."""

_EXCERPT_CHARS: Final[int] = 500
"""The most of one excerpt a row keeps. Requested from a text-bearing provider too, and enforced
here whatever it returns, so a handful of rows fit the result's budget."""

_TITLE_CHARS: Final[int] = 160
_URL_CHARS: Final[int] = 300
_QUERY_SHOWN: Final[int] = 60
"""How much of the query the first line, the receipt and an empty result say back."""

_RESULT_CHARS: Final[int] = 3_800
"""The payload's JSON, in characters. The spec keeps a result to about 4,000 (docs/spec.html#tools);
the call's receipt id and its own fields come on top. Rows go whole, last first, to fit it."""

_RANGE_DAYS: Final = {"day": 1, "week": 7, "month": 30, "year": 365}
"""The ``within`` values and how far back each reaches, for a provider that takes a start date."""
_DDGS_LIMIT: Final = {"day": "d", "week": "w", "month": "m", "year": "y"}

_TAGS: Final = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*(?:\s[^<>]*)?/?>")

_DESCRIPTION: Final = (
    "Use to look something up on the web: a fact, a price, the news, what is current, or what "
    "the sources say. Not for a place or its hours (map_search), the weather (weather) or a page "
    "he names (web_fetch). Returns search excerpts with site and date, none opened, under one "
    "line saying how fresh they are; depth deep takes about half a minute and returns a "
    "cross-checked brief with dated sources."
)

_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "What to search for: his question in his words, with its conditions "
            "(place, date, how many).",
        },
        "depth": {
            "type": "string",
            "enum": ["quick", "deep"],
            "description": (
                "quick: one fact or a quick check, a few seconds. deep: about half a minute, "
                "searches many sources and cross-checks them; for a recommendation or a "
                "comparison, anything that costs money or is booked, sources that may disagree, "
                "or when he asks you to look carefully."
            ),
        },
        "within": {
            "type": "string",
            "enum": list(_RANGE_DAYS),
            "description": "Quick only. Only pages from the past day, week, month or year, for "
            "'today', 'latest' or 'this week'; leave out for any date.",
        },
        "news": {
            "type": "boolean",
            "description": "Quick only. True when he asks for the news or for what happened; "
            "searches news sources.",
        },
        "max_results": {
            "type": "integer",
            "minimum": 1,
            "description": "Quick only. How many excerpts, default 5, at most 8.",
        },
    },
    "required": ["query", "depth"],
}

_SOURCE_TITLE_CHARS: Final[int] = 70
_PAGES_CHARS: Final[int] = 3_000
"""The pages an unfinished deep search lists, in JSON characters; rows go whole to fit it."""
_SUMMARY_ROOM: Final[int] = 420
"""What a deep result keeps free of :data:`_RESULT_CHARS` for its first line, written last."""
_BRIEF_MIN_CHARS: Final[int] = 600
"""The brief gets at least this much, whatever the sources take."""


@dataclass(frozen=True)
class Source:
    """A page a deep search found or rests on: title, site, the page's date if the search gave one.

    ``site`` is the host without ``www.``. ``date`` is the day as ``YYYY-MM-DD`` when the search's
    page age names one, else the age as the search wrote it, ``None`` when it gave none. ``url`` is
    whole: it is the link a device shows.
    """

    title: str
    site: str
    date: str | None
    url: str

    def row(self) -> dict[str, str]:
        """The row the model reads and the answer's list of sources is written from."""
        row = {"title": speakable.cut(self.title, _SOURCE_TITLE_CHARS) or self.site}
        row["site"] = self.site
        if self.date:
            row["date"] = self.date
        row["url"] = self.url
        return row


@dataclass(frozen=True)
class DeepFindings:
    """What one deep web search found: its brief, the pages it rests on, the pages it saw.

    ``brief`` is what the nested model wrote after its last search, empty when it stopped before
    writing one. ``sources`` are the pages the brief rests on; ``pages`` is every page its
    searches returned, in order. ``stopped``: ``minute`` it ran out of its minute, ``failed`` the
    request broke, ``length`` the brief hit the model's length limit; ``None`` it finished.
    """

    brief: str
    sources: tuple[Source, ...]
    pages: tuple[Source, ...]
    searches: int
    stopped: Literal["minute", "failed", "length"] | None = None


@dataclass(frozen=True)
class Hit:
    """One row a provider gave: a page, the provider's excerpt of it, and when it says it came out.

    ``published`` is the provider's own text for the date (ISO or RFC 2822), ``None`` when it
    gives none; a general search mostly gives none.
    """

    title: str
    url: str
    excerpt: str
    published: str | None = None


@dataclass(frozen=True)
class _Ask:
    """What one quick search asks a provider."""

    query: str
    count: int
    within: str | None = None
    news: bool = False
    since: datetime | None = None
    """The start of ``within``, for a provider that takes a date rather than a range."""


def _ddgs_search_backend(ask: _Ask, *, timeout_s: float) -> list[Hit]:
    """Real DuckDuckGo backend via the `ddgs` package (ADR-0011 D5).

    One function, as D5 explicitly asks for, so a keyed API is a config-sized swap later:
    nothing above this function knows or cares that DuckDuckGo is behind it. A news search
    uses its news endpoint, whose rows carry a date; ``within`` is its time limit. The
    acceptance script monkeypatches this name directly rather than mocking `ddgs` internals, so
    it never makes a real network call.
    """
    # Imported here, not at module top, so a `ddgs` import error surfaces as this tool's own
    # error observation rather than failing `import jarvis.execution.tools` for every caller.
    from ddgs import DDGS  # noqa: PLC0415

    limit = {"timelimit": _DDGS_LIMIT[ask.within]} if ask.within else {}
    # NIT-FIX 8, ADR-0011 §12: `int(timeout_s)` floors any sub-1s config value to 0, so the
    # `max(1, ...)` keeps a deliberately-short timeout at a sane floor.
    with DDGS(timeout=max(1, int(timeout_s))) as client:
        rows = (
            client.news(ask.query, max_results=ask.count, **limit)
            if ask.news
            else client.text(ask.query, max_results=ask.count, **limit)
        )
    return [
        Hit(
            str(row.get("title") or ""),
            str(row.get("url") or row.get("href") or ""),
            str(row.get("body") or ""),
            str(row["date"]) if row.get("date") else None,
        )
        for row in rows
    ]


class _KeyedSearchBackend(Protocol):
    """A `web_search` backend that needs a credential (Exa, Tavily)."""

    def __call__(self, ask: _Ask, *, timeout_s: float, api_key: str) -> list[Hit]: ...


def _exa_search_backend(ask: _Ask, *, timeout_s: float, api_key: str) -> list[Hit]:
    """Exa `/search` with page text inlined (`contents.text`).

    The point of a text-bearing provider is that ONE tool call answers "what does the web say
    about X" — the LLM does not have to spend a second tool-loop iteration on `web_fetch` to see
    any actual content. That matters directly: the loop bound is 5, and the search-then-fetch
    dance is what exhausted it in turn Te3815a16.

    `highlights` (Exa's query-relevant passages) is preferred over `text` (the page from its top)
    for the same reason Tavily's `content` beats its `raw_content`: against a per-result budget,
    a page's opening bytes are nav chrome, not the answer. That reasoning is EVIDENCED for Tavily
    and only INFERRED here — this backend has never run against a real Exa key, so treat the
    field preference as unverified.

    ``within`` is Exa's published-date start, a news search its ``news`` category; each row
    carries ``publishedDate``.
    """
    body: dict[str, Any] = {
        "query": ask.query,
        "numResults": ask.count,
        "contents": {"text": {"maxCharacters": _EXCERPT_CHARS}, "highlights": True},
    }
    if ask.since is not None:
        body["startPublishedDate"] = ask.since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    if ask.news:
        body["category"] = "news"
    response = httpx.post(
        "https://api.exa.ai/search", headers={"x-api-key": api_key}, json=body, timeout=timeout_s,
    )
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    return [
        Hit(
            str(row.get("title") or ""),
            str(row.get("url") or ""),
            _exa_row_text(row),
            str(row["publishedDate"]) if row.get("publishedDate") else None,
        )
        for row in rows
        if isinstance(row, dict)
    ]


def _exa_row_text(row: Mapping[str, Any]) -> str:
    """Prefer an Exa row's `highlights` over its `text` (see caller)."""
    highlights = row.get("highlights")
    if isinstance(highlights, list) and highlights:
        return " … ".join(str(h) for h in highlights if h)
    return str(row.get("text") or "")


def _tavily_search_backend(ask: _Ask, *, timeout_s: float, api_key: str) -> list[Hit]:
    """Tavily `/search`, taking its relevance-selected `content`.

    `content` is Tavily's query-relevant extract; `raw_content` is the whole parsed page.
    `raw_content` was tried first and was WRONG: against a per-result budget, a page's opening
    bytes are nav chrome, so the budget bought "[Skip to content](#main) ![logo]…" while
    `content` for the same result read "operated by Air Canada and China Eastern Airlines and the
    flight time is 13 hours and 50 minutes" — the actual answer, at ~1.3 KB. So `content` is
    requested and preferred, `raw_content` is not asked for at all (it is billed payload we would
    discard), and the fallback stays only for a row that somehow carries one and not the other. A
    result whose full page IS needed can be handed to `web_fetch`.

    ``within`` is its ``time_range`` and a news search its ``news`` topic, whose rows carry
    ``published_date``; a general search's rows do not.
    """
    body: dict[str, Any] = {"query": ask.query, "max_results": ask.count}
    if ask.news:
        body["topic"] = "news"
    if ask.within:
        body["time_range"] = ask.within
    response = httpx.post(
        "https://api.tavily.com/search",
        headers={"Authorization": f"Bearer {api_key}"},
        json=body,
        timeout=timeout_s,
    )
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    return [
        Hit(
            str(row.get("title") or ""),
            str(row.get("url") or ""),
            str(row.get("content") or row.get("raw_content") or ""),
            str(row["published_date"]) if row.get("published_date") else None,
        )
        for row in rows
        if isinstance(row, dict)
    ]


class _SearchBackend(Protocol):
    """Uniform call shape every `web_search` backend is adapted to."""

    def __call__(self, ask: _Ask, *, timeout_s: float) -> list[Hit]: ...


def _resolve_search_backend(provider: str, api_key: str | None) -> tuple[_SearchBackend, str]:
    """Pick the `web_search` backend, returning `(backend, name_actually_used)`.

    A keyed provider named without its key does NOT fail boot — it warns and degrades to `ddgs`,
    because losing web search entirely is worse than losing result quality. The degrade is never
    silent: it is logged here AND the provider actually used is reported in every `web_search`
    result payload, so the event log shows which backend answered rather than leaving it to be
    inferred.
    """
    normalized = provider.strip().lower()
    keyed: Mapping[str, _KeyedSearchBackend] = {
        "exa": _exa_search_backend,
        "tavily": _tavily_search_backend,
    }
    if normalized in keyed:
        if not api_key:
            LOGGER.warning(
                "tools.web.search_provider=%r but its search_api_key_env names an "
                "unset/empty variable — degrading to the ddgs backend",
                normalized,
            )
            return _call_ddgs, "ddgs"
        return _with_api_key(keyed[normalized], api_key), normalized
    if normalized != "ddgs":
        LOGGER.warning("unknown tools.web.search_provider=%r — using the ddgs backend", provider)
    return _call_ddgs, "ddgs"


def _call_ddgs(ask: _Ask, *, timeout_s: float) -> list[Hit]:
    """Adapt `_ddgs_search_backend` to `_SearchBackend`.

    Deliberately a late-bound module-global lookup rather than a captured function object:
    `_ddgs_search_backend`'s own docstring promises the acceptance script can monkeypatch that
    NAME, and capturing it at registry-build time would quietly break that seam.
    """
    return _ddgs_search_backend(ask, timeout_s=timeout_s)


def _with_api_key(backend: _KeyedSearchBackend, api_key: str) -> _SearchBackend:
    """Close a keyed backend over its credential, yielding a `_SearchBackend`."""

    def _call(ask: _Ask, *, timeout_s: float) -> list[Hit]:
        return backend(ask, timeout_s=timeout_s, api_key=api_key)

    return _call


# --- what a quick search says about what it found ------------------------------------------


def _plain(text: str, limit: int) -> str:
    """One line of plain text: markup out, entities read, space squeezed, cut at ``limit``."""
    return speakable.cut(_TAGS.sub(" ", html.unescape(text)), limit)


def _site(url: str) -> str:
    """The page's host without ``www.``; empty when ``url`` has none."""
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def _published_day(text: str | None, zone: tzinfo, today: date) -> date | None:
    """The local day a provider's date text names, or ``None`` when it is none or in the future.

    Providers write ISO 8601 (Exa, DuckDuckGo news) or RFC 2822 (Tavily); a time with no offset is
    UTC. A day after today is a provider's slip, not a date.
    """
    if not text or not text.strip():
        return None
    value = text.strip()
    try:
        if len(value) == len("2026-10-16"):
            day = date.fromisoformat(value)
        else:
            day = _local_day(datetime.fromisoformat(value), zone)
    except ValueError:
        try:
            day = _local_day(parsedate_to_datetime(value), zone)
        except (TypeError, ValueError, IndexError):
            return None
    return day if day <= today else None


def _local_day(moment: datetime, zone: tzinfo) -> date:
    """The day ``moment`` falls on in ``zone``; a time with no offset is UTC."""
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).astimezone(zone).date()


def _said(query: str) -> str:
    """The query as the first line, the receipt and an empty result say it back: one short line."""
    return speakable.cut(query.replace("{", " ").replace("}", " "), _QUERY_SHOWN)


def _range_words(within: str | None) -> str:
    return f"past {within}" if within else "any date"


def _first_line(
    ask: _Ask, *, found: int, shown: int, newest: date | None, today: date,
) -> str:
    """One line for her to say: how many excerpts, none opened, how new, which range."""
    what = "news excerpts" if ask.news else "search excerpts"
    parts = [
        f"{found} {what if found != 1 else what.removesuffix('s')} for '{_said(ask.query)}', "
        "none opened",
        f"newest {speakable.day_name(newest, today)}" if newest else "dates unknown",
    ]
    if ask.within:
        parts.append(_range_words(ask.within))
    line = "; ".join(parts) + "."
    if shown < found:
        line += f" Showing {shown}; {found - shown} more left out to keep this short."
    return line


def _nothing_found(ask: _Ask) -> str:
    """What an empty search says: what was searched, with which topic and range, what to try."""
    topic = "news search" if ask.news else "general search"
    again = "other words, a wider range or depth deep"
    if not ask.within:
        again = "other words or depth deep"
    return (
        f"No results for '{_said(ask.query)}': {topic}, {_range_words(ask.within)}. "
        f"Try {again}."
    )


def _quick_result(
    ask: _Ask, hits: list[Hit], provider: str, *, zone: tzinfo, today: date,
) -> Receipted:
    """The rows, the first line and the receipt of one quick search.

    Rows go whole, last first, until the payload fits :data:`_RESULT_CHARS`; the first line and
    the receipt say how many were left out.
    """
    rows: list[tuple[dict[str, str], date | None]] = []
    for hit in hits:
        url = hit.url.strip()
        if not _site(url):
            continue  # a row with no page to open cannot be followed up
        day = _published_day(hit.published, zone, today)
        rows.append((
            {
                "title": _plain(hit.title, _TITLE_CHARS) or _site(url),
                "site": _site(url),
                "date": speakable.day_name(day, today) if day else "date unknown",
                "excerpt": _plain(hit.excerpt, _EXCERPT_CHARS),
                "url": speakable.cut(url, _URL_CHARS),
            },
            day,
        ))
    window = ", ".join(
        [f"'{_said(ask.query)}'", *(["news"] if ask.news else []), _range_words(ask.within)],
    )
    if not rows:
        summary = _nothing_found(ask)
        return Receipted(
            {"summary": summary, "results": [], "provider": provider, "depth": "quick"},
            Receipt(
                "done", reads_only=True, line=f"searched {provider}: nothing found. {window}",
                covered=Covered((provider,), window, 0, 0),
            ),
        )
    shown = len(rows)
    while True:
        kept = rows[:shown]
        dated = [day for _, day in kept if day is not None]
        payload: dict[str, Any] = {
            "summary": _first_line(
                ask, found=len(rows), shown=shown, newest=max(dated, default=None), today=today,
            ),
            "results": [row for row, _ in kept],
            "provider": provider,
            "depth": "quick",
        }
        if shown == 1 or len(json.dumps(payload, ensure_ascii=False)) <= _RESULT_CHARS:
            break
        shown -= 1
    left_out = len(rows) - shown
    return Receipted(
        payload,
        Receipt(
            "done", reads_only=True,
            line=f"searched {provider}: {shown} excerpts shown. {window}",
            covered=Covered((provider,), window, shown, left_out),
        ),
    )


# --- the deep search ----------------------------------------------------------------------


class DeepSearch(Protocol):
    """Injected deep-research seam for `web_search` depth deep.

    L4 cannot import `jarvis.decision.llm.LLMClient` — sibling layers under `.importlinter` — the
    same seam `VisionClient` is. `jarvis.runtime` builds the real implementation around
    Anthropic's own web search server tool and binds it into `build_default_registry`. One
    question in, findings out, inside one minute of ``clock`` (ADR 0359): ``where`` is roughly
    where he is, taken from the call's Here. A search that fails with no page found is raised, and
    the handler turns it into an error observation.
    """

    def research(self, query: str, *, where: Coarse | None, clock: Clock) -> DeepFindings:
        """Search widely, cross-check, and return the brief with its sources."""
        ...


def _newest(sources: tuple[Source, ...]) -> date | None:
    """The latest day any source's date names; a date that is not a day is no date."""
    days: list[date] = []
    for source in sources:
        try:
            days.append(date.fromisoformat(source.date or ""))
        except ValueError:
            continue
    return max(days, default=None)


def _plural(count: int, noun: str) -> str:
    """``1 page``, ``2 pages``, ``2 searches``."""
    ending = "es" if noun.endswith("ch") else "s"
    return f"{count} {noun}" if count == 1 else f"{count} {noun}{ending}"


def _brief_line(findings: DeepFindings, said: str, today: date, *, cut: tuple[int, int]) -> str:
    """The first line of a deep result with its brief: how it was made, how fresh, what is cut."""
    line = f"Deep search for '{said}': brief below"
    if findings.sources:
        newest = _newest(findings.sources)
        when = f"newest {speakable.day_name(newest, today)}" if newest else "dates unknown"
        line += f", from {_plural(len(findings.sources), 'source')}, {when}"
    else:
        line += ", no source named"
    line += f"; {_plural(findings.searches, 'search')}."
    if findings.sources:
        line += " The sources are listed under your answer."
    if findings.stopped == "length":
        line += " The brief hit the model's length limit and may be unfinished."
    if cut[1]:
        line += f" The brief is cut after {cut[0]:,} characters; {cut[1]} more parts left out."
    return line


def _pages_line(findings: DeepFindings, said: str, *, shown: int) -> str:
    """The first line of a deep result that stopped before its brief: why, and what there is."""
    why = "failed" if findings.stopped == "failed" else "stopped at a minute"
    line = (
        f"Deep search for '{said}' {why} before writing its brief; "
        f"{_plural(len(findings.pages), 'page')} found, listed below with site and date. "
        "Read one with web_fetch, or search again with depth quick."
    )
    if shown < len(findings.pages):
        line += f" Showing {shown}; {len(findings.pages) - shown} more left out to keep this short."
    return line


def _deep_result(findings: DeepFindings, query: str, *, today: date) -> Receipted:
    """A deep search as the tool's result: its brief and sources, else the pages it found.

    The brief is cut at a paragraph to what the sources leave of :data:`_RESULT_CHARS`, and the
    line says how much. A search that stopped before its brief hands over the pages it found,
    marked unfinished (not an error); one that found none is an error that points at a quick
    search.
    """
    said = _said(query)
    window = f"'{said}', deep"
    if findings.brief:
        rows = [one.row() for one in findings.sources]
        fixed = {
            "summary": "", "brief": "", "sources": rows, "provider": "anthropic", "depth": "deep",
            "searches": findings.searches, "unfinished": True,
        }
        room = _RESULT_CHARS - len(json.dumps(fixed, ensure_ascii=False)) - _SUMMARY_ROOM
        parts = paragraphs.parts(findings.brief, max(room, _BRIEF_MIN_CHARS))
        shown = parts[0] if parts else ""
        payload: dict[str, Any] = {
            "summary": _brief_line(
                findings, said, today, cut=(len(shown), max(0, len(parts) - 1)),
            ),
            "brief": shown,
            "sources": rows,
            "provider": "anthropic",
            "depth": "deep",
            "searches": findings.searches,
        }
        if findings.stopped:
            payload["unfinished"] = True
        return Receipted(
            payload,
            Receipt(
                "done", reads_only=True,
                line=f"searched the web, deep: {_plural(len(findings.sources), 'source')}",
                covered=Covered(("anthropic",), window, len(findings.sources), 0),
            ),
        )
    if not findings.pages:
        msg = (
            "web_search: the deep search ran out of its minute before it found any page; "
            "try a quick search for it, or tell him the search did not finish"
        )
        raise ToolError(msg, code="web_search_timeout", kind="dont_retry")
    shown_pages = len(findings.pages)
    while True:
        payload = {
            "summary": _pages_line(findings, said, shown=shown_pages),
            "unfinished": True,
            "pages": [one.row() for one in findings.pages[:shown_pages]],
            "provider": "anthropic",
            "depth": "deep",
            "searches": findings.searches,
        }
        if shown_pages == 1 or len(json.dumps(payload, ensure_ascii=False)) <= _PAGES_CHARS:
            break
        shown_pages -= 1
    left_out = len(findings.pages) - shown_pages
    return Receipted(
        payload,
        Receipt(
            "done", reads_only=True,
            line="searched the web, deep: stopped before its brief; "
            f"{_plural(shown_pages, 'page')}",
            covered=Covered(("anthropic",), window, shown_pages, left_out),
        ),
    )


def _deep_web_search(
    deep_search: DeepSearch, query: str, *, where: Coarse | None, ctx: ToolContext,
) -> Receipted:
    """One deep search as the tool's result."""
    try:
        findings = deep_search.research(query, where=where, clock=ctx.clock)
    except Exception as exc:
        # The type only in the message: a provider error can carry request headers or a key.
        msg = (
            f"web_search: the deep search failed ({type(exc).__name__}); try a quick "
            "search, or tell him the search failed"
        )
        raise ToolError(msg, code="web_search_backend_error") from exc
    return _deep_result(findings, query, today=ctx.clock.now().date())


def _within(value: object) -> str | None:
    """``within`` as asked: one of the ranges, or ``None`` when left out."""
    if value is None:
        return None
    if isinstance(value, str) and value in _RANGE_DAYS:
        return value
    msg = (
        f"web_search: within must be one of {', '.join(_RANGE_DAYS)} (got {value!r}); "
        "use one of those, or leave it out for any date"
    )
    raise ToolError(msg, code="invalid_arguments")


def _news(value: object) -> bool:
    """``news`` as asked: true or false, false when left out."""
    if value is None or isinstance(value, bool):
        return bool(value)
    msg = (
        f"web_search: news must be true or false (got {value!r}); "
        "leave it out for a general search"
    )
    raise ToolError(msg, code="invalid_arguments")


def _count(value: object, default: int) -> int:
    """``max_results`` as asked, kept within 1 and the ceiling; ``default`` when it is no number.

    `json.loads` accepts Infinity/NaN on the tool-arg path and `int(inf)` raises, so a malformed
    value means "use the default" (ADR-0011 §12).
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        default = int(value)
    return max(1, min(default, _MAX_RESULTS_CAP))


def make_web_search(
    *,
    default_max_results: int,
    timeout_s: float,
    provider: str = DEFAULT_WEB_SEARCH_PROVIDER,
    api_key: str | None = None,
    deep_search: DeepSearch | None = None,
) -> Tool:
    """Bind `tools.web.{search_max_results,timeout_s,search_provider}` into `web_search`."""
    backend, provider_used = _resolve_search_backend(provider, api_key)

    def web_search(args: Mapping[str, Any], ctx: ToolContext) -> Mapping[str, Any]:
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            msg = f"web_search: query must be the words to search for (got {query!r})"
            raise ToolError(msg, code="invalid_arguments")
        within, news = _within(args.get("within")), _news(args.get("news"))
        deep = args.get("depth") == "deep"
        if deep and deep_search is not None:
            where = None if ctx.here is None else ctx.here.coarse()
            return _deep_web_search(deep_search, query, where=where, ctx=ctx)
        # No deep backend on this build: the widest quick search is the nearest thing.
        count = _MAX_RESULTS_CAP if deep else _count(args.get("max_results"), default_max_results)
        now = ctx.clock.now()
        ask = _Ask(
            query.strip(), count, within, news,
            since=None if within is None else now - timedelta(days=_RANGE_DAYS[within]),
        )
        try:
            hits = backend(ask, timeout_s=timeout_s)
        except Exception as exc:
            # ddgs anti-bot churn and keyed 401/429 are routine (ADR-0011 §5): name the backend,
            # no in-handler retry.
            msg = (
                f"web_search: the {provider_used} search did not answer ({exc}); tell him the "
                "search failed, or try web_fetch on a page he names"
            )
            raise ToolError(msg, code="web_search_backend_error") from exc
        # Naming the backend keeps the `_resolve_search_backend` degrade visible in the event log
        # instead of inferable; `depth` does the same for a deep request that ran quick.
        return _quick_result(ask, hits, provider_used, zone=ctx.clock.zone, today=now.date())

    return Tool(
        name="web_search",
        description=_DESCRIPTION,
        input_schema=_SCHEMA,
        handler=web_search,
        allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
        risk_level="L1",
        read_only=True,
    )


__all__ = ["DeepFindings", "DeepSearch", "Hit", "Source", "make_web_search"]
