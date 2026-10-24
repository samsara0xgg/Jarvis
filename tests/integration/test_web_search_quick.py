"""ADR 0358: a quick web search says how fresh and how deep its rows are, and what it searched.

The handler runs for real over a fake web (httpx requests answered by a function) and a fake
``ddgs``; the clock is the test's, in his zone. Nothing reaches the network.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any, Self
from zoneinfo import ZoneInfo

import pytest

from jarvis.execution.tools import Receipted, ToolError
from jarvis.execution.web_search_tool import make_web_search
from tests.support.clock import FakeClock
from tests.support.http import body_of, fake_web, json_reply
from tests.support.tool_context import tool_context

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    import httpx

    from jarvis.execution.tools import Tool

VANCOUVER = ZoneInfo("America/Vancouver")
NOW = datetime(2026, 10, 21, 9, 30, tzinfo=VANCOUVER)  # a Wednesday, 16:30 UTC
TAVILY = "https://api.tavily.com/search"
EXA = "https://api.exa.ai/search"

PORTS = {
    "title": "Port workers vote on the employer's offer",
    "url": "https://www.example-times.test/news/ports-vote",
    "content": "Members of the longshore union vote today on a revised offer.",
    "published_date": "Wed, 21 Oct 2026 14:00:00 GMT",  # 07:00 in Vancouver, today
}
FERRY = {
    "title": "Ferry sailings resume after the storm",
    "url": "https://example-ferries.test/notices/resume",
    "content": "Sailings on the main routes resumed this morning.",
    "published_date": "Tue, 20 Oct 2026 03:00:00 GMT",  # Mon 20:00 in Vancouver
}
RAIL = {
    "title": "Commuter rail adds weekend trains",
    "url": "https://example-rail.test/service/weekend",
    "content": "Two extra weekend trains start in November.",
}


def _tool(provider: str = "tavily", api_key: str | None = "test-key") -> Tool:
    return make_web_search(
        default_max_results=5, timeout_s=5.0, provider=provider, api_key=api_key,
    )


def _call(tool: Tool, args: Mapping[str, Any]) -> Receipted:
    result = tool.handler(args, tool_context(clock=FakeClock(NOW)))
    assert isinstance(result, Receipted)
    return result


def _tavily_says(rows: list[dict[str, Any]]) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: json_reply({"results": rows})


def test_the_provider_is_asked_for_the_topic_and_range_his_words_named() -> None:
    """``news`` is Tavily's news topic and ``within`` its time range; neither, neither key."""
    with fake_web(_tavily_says([PORTS])) as seen:
        _call(_tool(), {"query": "BC news", "depth": "quick", "within": "day", "news": True})
        _call(_tool(), {"query": "oat milk", "depth": "quick"})
    first, second = (body_of(r) for r in seen)
    assert [str(r.url) for r in seen] == [TAVILY, TAVILY]
    assert first == {"query": "BC news", "max_results": 5, "topic": "news", "time_range": "day"}
    assert second == {"query": "oat milk", "max_results": 5}


def test_exa_is_asked_for_a_start_date_and_the_news_category() -> None:
    """Exa takes the range as the published-date start, counted from the clock's now."""
    with fake_web(lambda _r: json_reply({"results": []})) as seen:
        _call(
            _tool("exa"), {"query": "rust", "depth": "quick", "within": "week", "news": True},
        )
        _call(_tool("exa"), {"query": "rust", "depth": "quick"})
    news, plain = (body_of(r) for r in seen)
    assert [str(r.url) for r in seen] == [EXA, EXA]
    assert news["startPublishedDate"] == "2026-10-14T16:30:00.000Z"
    assert news["category"] == "news"
    assert "startPublishedDate" not in plain
    assert "category" not in plain


class _FakeDDGS:
    """``ddgs.DDGS`` as the backend uses it: a context manager with ``text`` and ``news``."""

    calls: list[tuple[str, str, dict[str, Any]]] = []  # noqa: RUF012 - one fake, one test at a time

    def __init__(self, **_options: object) -> None:
        pass

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def text(self, query: str, **kw: Any) -> list[dict[str, Any]]:  # noqa: ANN401
        self.calls.append(("text", query, kw))
        return [{"title": "Oat milk guide", "href": "https://example-food.test/oat", "body": "b"}]

    def news(self, query: str, **kw: Any) -> list[dict[str, Any]]:  # noqa: ANN401
        self.calls.append(("news", query, kw))
        return [{
            "title": "Dairy prices rise", "url": "https://example-news.test/dairy",
            "body": "Prices rose.", "date": "2026-10-20T18:00:00+00:00", "source": "Example News",
        }]


def test_ddgs_searches_news_or_text_with_its_time_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A news search uses ddgs's news endpoint (its rows carry a date); ``within`` is its limit."""
    monkeypatch.setattr("ddgs.DDGS", _FakeDDGS)
    _FakeDDGS.calls = []
    tool = _tool("ddgs", None)
    news = _call(tool, {"query": "milk", "depth": "quick", "within": "month", "news": True})
    text = _call(tool, {"query": "milk", "depth": "quick"})
    assert _FakeDDGS.calls == [
        ("news", "milk", {"max_results": 5, "timelimit": "m"}),
        ("text", "milk", {"max_results": 5}),
    ]
    assert news["results"][0]["date"] == "yesterday (Tue)"
    assert text["results"][0]["date"] == "date unknown"


def test_rows_carry_title_site_date_excerpt_and_url_with_the_first_line_leading() -> None:
    """A dated row names its local day; an undated row says so; the first line sums them up."""
    with fake_web(_tavily_says([PORTS, FERRY, RAIL])):
        result = _call(
            _tool(), {"query": "BC news", "depth": "quick", "within": "day", "news": True},
        )
    assert next(iter(result)) == "summary"
    assert result["summary"] == (
        "3 news excerpts for 'BC news', none opened; newest today (Wed); past day."
    )
    assert result["results"] == [
        {
            "title": PORTS["title"], "site": "example-times.test", "date": "today (Wed)",
            "excerpt": PORTS["content"], "url": PORTS["url"],
        },
        {
            "title": FERRY["title"], "site": "example-ferries.test", "date": "Mon Oct 19",
            "excerpt": FERRY["content"], "url": FERRY["url"],
        },
        {
            "title": RAIL["title"], "site": "example-rail.test", "date": "date unknown",
            "excerpt": RAIL["content"], "url": RAIL["url"],
        },
    ]
    assert result["provider"] == "tavily"
    assert result["depth"] == "quick"


@pytest.mark.parametrize(
    ("args", "rows", "line"),
    [
        pytest.param(
            {"query": "rust version"}, [RAIL],
            "1 search excerpt for 'rust version', none opened; dates unknown.",
            id="no row says when, and no range was asked",
        ),
        pytest.param(
            {"query": "ferry", "within": "week"}, [FERRY, RAIL],
            "2 search excerpts for 'ferry', none opened; newest Mon Oct 19; past week.",
            id="the newest dated row, the range asked",
        ),
    ],
)
def test_the_first_line_says_what_the_excerpts_rest_on(
    args: dict[str, Any], rows: list[dict[str, Any]], line: str,
) -> None:
    """How many excerpts, none opened, the newest date among them (or that none is dated)."""
    with fake_web(_tavily_says(rows)):
        result = _call(_tool(), {**args, "depth": "quick"})
    assert result["summary"] == line


def test_an_empty_search_names_the_query_topic_and_range_and_what_to_try() -> None:
    """No rows: what was searched, with which topic and range, and how to change the search."""
    with fake_web(_tavily_says([])):
        narrow = _call(
            _tool(), {"query": "BC news", "depth": "quick", "within": "day", "news": True},
        )
        open_ = _call(_tool(), {"query": "oat milk", "depth": "quick"})
    assert narrow["summary"] == (
        "No results for 'BC news': news search, past day. "
        "Try other words, a wider range or depth deep."
    )
    assert open_["summary"] == (
        "No results for 'oat milk': general search, any date. Try other words or depth deep."
    )
    assert narrow["results"] == []
    assert narrow.receipt.status == "done"
    assert narrow.receipt.covered is not None
    assert narrow.receipt.covered.shown == 0


def test_the_receipt_names_the_provider_query_and_range() -> None:
    """A read's receipt says which provider it asked, what for and over which range."""
    with fake_web(_tavily_says([PORTS, FERRY])):
        result = _call(
            _tool(), {"query": "BC news", "depth": "quick", "within": "day", "news": True},
        )
    receipt = result.receipt
    assert (receipt.status, receipt.reads_only) == ("done", True)
    assert receipt.covered is not None
    assert receipt.covered.sources == ("tavily",)
    assert receipt.covered.window == "'BC news', news, past day"
    assert (receipt.covered.shown, receipt.covered.more) == (2, 0)


def test_rows_go_whole_and_the_result_stays_under_four_thousand_characters() -> None:
    """Too many long excerpts: the last rows are left out whole, and the line says how many."""
    rows = [
        {
            "title": f"Report {i} on the same story",
            "url": f"https://example-wire.test/story/{i}",
            "content": f"Paragraph {i}. " + "The council met and discussed the plan. " * 40,
            "published_date": "Wed, 21 Oct 2026 14:00:00 GMT",
        }
        for i in range(8)
    ]
    with fake_web(_tavily_says(rows)):
        result = _call(_tool(), {"query": "council plan", "depth": "quick", "max_results": 8})
    shown = len(result["results"])
    assert 1 <= shown < 8
    assert len(json.dumps(dict(result), ensure_ascii=False)) <= 3_800
    assert f"Showing {shown}; {8 - shown} more left out" in result["summary"]
    assert [row["url"] for row in result["results"]] == [r["url"] for r in rows[:shown]]
    assert result.receipt.covered is not None
    assert (result.receipt.covered.shown, result.receipt.covered.more) == (shown, 8 - shown)
    excerpts = [row["excerpt"] for row in result["results"]]
    assert all(text.startswith(f"Paragraph {i}.") for i, text in enumerate(excerpts))


def test_markup_in_a_row_is_dropped_and_a_row_with_no_page_is_not_a_row() -> None:
    """Titles and excerpts are plain text; a row with no usable address is left out."""
    rows = [
        {
            "title": "<b>Oat</b> milk &amp; cream", "url": "https://shop.example.test/oat",
            "content": "<p>Plain   text</p>\nover lines",
        },
        {"title": "No page", "url": "", "content": "nothing to open"},
    ]
    with fake_web(_tavily_says(rows)):
        result = _call(_tool(), {"query": "oat {milk}", "depth": "quick"})
    assert [(r["title"], r["excerpt"]) for r in result["results"]] == [
        ("Oat milk & cream", "Plain text over lines"),
    ]
    assert result["summary"].startswith("1 search excerpt for 'oat milk', none opened")


@pytest.mark.parametrize(
    ("args", "words"),
    [
        pytest.param({"within": "hour"}, ("within must be one of day, week, month, year",)),
        pytest.param({"news": "yes"}, ("news must be true or false",)),
        pytest.param({"query": "  "}, ("query must be the words to search for",)),
    ],
)
def test_a_bad_argument_says_what_is_allowed(args: dict[str, Any], words: tuple[str, ...]) -> None:
    """A bad ``within``, ``news`` or query is an error the model can fix; no provider is asked."""
    with fake_web(_tavily_says([PORTS])) as seen, pytest.raises(ToolError) as caught:
        _call(_tool(), {"query": "x", "depth": "quick", **args})
    assert caught.value.code == "invalid_arguments"
    assert caught.value.kind == "retry_other_args"
    assert all(word in str(caught.value) for word in words)
    assert seen == []


def test_the_menu_entry_sends_a_place_the_weather_and_a_page_elsewhere() -> None:
    """The description says what not to search for and where each goes; every parameter is told."""
    tool = _tool()
    assert len(tool.description) <= 600
    assert "he" in tool.description.split()
    for elsewhere in ("map_search", "weather", "web_fetch"):
        assert elsewhere in tool.description
    schema = tool.input_schema
    assert schema["required"] == ["query", "depth"]
    assert set(schema["properties"]) == {"query", "depth", "within", "news", "max_results"}
    assert all(p["description"] for p in schema["properties"].values())
    assert schema["properties"]["within"]["enum"] == ["day", "week", "month", "year"]
