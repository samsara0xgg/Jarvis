"""ADR 0360: ``web_fetch`` reads the page itself first, and says what it read.

The handler runs for real, over the real egress guard, redirect walk and parser, with the web
scripted by address: pages recorded as invented files (`tests/fixtures/web_pages`, all
`example.com`) and the extract service's answers. Nothing reaches the network. What is asserted is
the result she reads, the error she is told, and which requests were made.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from jarvis.execution.tools import Receipted, Tool, ToolError, build_default_registry
from tests.support.clock import FakeClock
from tests.support.http import fake_dns, fake_web, json_reply, page_reply
from tests.support.tool_context import tool_context

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

PAGES = Path(__file__).resolve().parents[1] / "fixtures" / "web_pages"
NOW = datetime(2026, 10, 21, 9, 30, tzinfo=FakeClock().zone)  # a Wednesday
ASKED = "https://news.example.com/s/ferries"
LONG = "https://news.example.com/s/long"
EXTRACT = "https://api.tavily.com/extract"
KEY = "tvly-test-key-not-real"


def _text(name: str) -> str:
    return (PAGES / name).read_text(encoding="utf-8")


def _tool(*, extract_key: str | None = None, max_bytes: int = 2_000_000) -> Tool:
    registry = build_default_registry(
        web_search_provider="tavily" if extract_key else "ddgs", web_search_api_key=extract_key,
        web_fetch_max_bytes=max_bytes,
    )
    found = next(t for t in registry.get_definitions() if t.name == "web_fetch")
    assert isinstance(found, Tool)
    return found


type Reply = Callable[[], httpx.Response]


def _html(name: str, status: int = 200) -> Reply:
    return lambda: page_reply(_text(name), status)


def _status(status: int) -> Reply:
    return lambda: page_reply("", status)


def _extracted(text: str, url: str = ASKED) -> httpx.Response:
    return json_reply({"results": [{"url": url, "raw_content": text}], "failed_results": []})


class Site:
    """The web as a test sets it: pages by address, the extract service, every request seen."""

    def __init__(self, pages: Mapping[str, Reply], extract: httpx.Response | None = None) -> None:
        """The pages by address and, when the extract service is set up, what it answers."""
        self.pages = dict(pages)
        self.extract = extract
        self.seen: list[httpx.Request] = []

    def answer(self, request: httpx.Request) -> httpx.Response:
        """The page at the request's address, or the extract service's answer."""
        if str(request.url) == EXTRACT:
            assert self.extract is not None, "the extract service was not expected to be asked"
            return self.extract
        return self.pages[str(request.url)]()

    def extract_requests(self) -> list[httpx.Request]:
        """The requests that reached the extract service."""
        return [r for r in self.seen if str(r.url) == EXTRACT]

    def page_requests(self) -> list[httpx.Request]:
        """The requests that reached a page."""
        return [r for r in self.seen if str(r.url) != EXTRACT]


def _call(
    tool: Tool, site: Site, args: Mapping[str, Any] | None = None,
) -> Receipted:
    with fake_dns(), fake_web(site.answer) as seen:
        try:
            context = tool_context(clock=FakeClock(NOW))
            result = tool.handler({"url": ASKED, **(args or {})}, context)
        finally:
            site.seen.extend(seen)
    assert isinstance(result, Receipted)
    return result


def _error(
    tool: Tool, site: Site, args: Mapping[str, Any] | None = None,
) -> ToolError:
    with pytest.raises(ToolError) as caught:
        _call(tool, site, args)
    return caught.value


def _size(result: Mapping[str, Any]) -> int:
    return len(json.dumps(dict(result), ensure_ascii=False))


# --- a page that reads -------------------------------------------------------------------------


def test_a_page_is_read_directly_and_names_its_title_address_and_day() -> None:
    """The title is the page's own, the address its canonical one, the day its published time."""
    site = Site({ASKED: _html("short_note.html")})
    result = _call(_tool(), site)
    assert next(iter(result)) == "summary"
    assert result["read_by"] == "direct"
    assert result["title"] == "Terminal notice"
    assert result["content"].startswith("Terminal notice\n\nThe Quay terminal washrooms")
    assert result["address"] == ASKED
    assert "published" not in result
    assert result["summary"] == (
        "Read 'Terminal notice' on news.example.com: the whole page; read directly."
    )
    assert [str(r.url) for r in site.seen] == [ASKED]


def test_the_title_address_and_day_are_the_pages_own_marks_in_their_order() -> None:
    """og:title beats <title>, a canonical link beats where the read ended, the time is a day."""
    site = Site({ASKED: _html("long_article.html")})
    result = _call(_tool(), site)
    assert result["title"] == "Port Alder ferries to run later on Fridays"
    assert result["address"] == "https://www.news.example.com/2026/10/ferries-run-later/"
    assert result["published"] == "2026-10-19"
    assert "published Mon Oct 19" in result["summary"]


def test_the_page_text_is_its_article_without_the_navigation_the_aside_or_the_footer() -> None:
    """Menus, 'most read' and the footer are not the page; scripts and styles are not text."""
    site = Site({ASKED: _html("long_article.html")})
    text = "\n".join(_call(_tool(), site, {"page": n})["content"] for n in (1, 2, 3))
    assert "Port Alder's harbour ferries will sail an hour later" in text
    for chrome in ("Most read", "Home", "All rights reserved", "dataLayer", "font-family"):
        assert chrome not in text


def test_a_plain_text_page_is_read_as_it_is_without_a_title() -> None:
    """Text has no title mark; its paragraphs are its blank-line blocks."""
    site = Site({ASKED: lambda: page_reply(_text("plain.txt"), content_type="text/plain")})
    result = _call(_tool(), site)
    assert "title" not in result
    assert result["content"].startswith("Alder Harbour Ferries service notice\n\nLate sailings")
    assert result["summary"].startswith("Read the page on news.example.com: the whole page;")


def test_a_page_in_another_charset_is_decoded_as_it_declares() -> None:
    """A gb2312 page reads as Chinese, not as replacement characters."""
    sentence = "末班船凌晨零点十五分开出，周末延后一小时，平日的班次不变，票价也不变。"  # noqa: RUF001
    body = f"<html><head><title>渡轮时刻</title></head><body><p>{sentence}</p></body></html>"
    site = Site({ASKED: lambda: httpx.Response(
        200, content=body.encode("gb2312"), headers={"content-type": "text/html; charset=gb2312"},
    )})
    result = _call(_tool(), site)
    assert result["title"] == "渡轮时刻"
    assert sentence in result["content"]


def test_a_page_larger_than_the_download_cap_says_its_end_was_not_read() -> None:
    """The cap stops the download; the first line says the end of the page was not read."""
    site = Site({ASKED: _html("long_article.html")})
    result = _call(_tool(max_bytes=3_000), site)
    assert "The download stopped at its size limit, so the end of the page was not read." in (
        result["summary"]
    )


# --- which page --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "asked",
    [
        "https://www.news.example.com/2026/10/ferries-run-later",
        "http://news.example.com/2026/10/ferries-run-later/",
        "https://news.example.com/2026/10/ferries-run-later?utm_source=x&utm_medium=y",
    ],
)
def test_an_address_that_differs_only_in_scheme_www_slash_or_tracking_is_the_one_asked(
    asked: str,
) -> None:
    """The first line does not claim a different address for those."""
    site = Site({asked: _html("long_article.html")})
    result = _call(_tool(), site, {"url": asked})
    assert result["summary"].startswith("Read 'Port Alder ferries")


def test_an_address_that_is_not_the_one_asked_is_said_first() -> None:
    """The canonical address is another path: the first line says so before anything else."""
    site = Site({ASKED: _html("long_article.html")})
    result = _call(_tool(), site)
    assert result["summary"].startswith(
        "The address of this page is news.example.com/2026/10/ferries-run-later/, not the one "
        "you gave (news.example.com/s/ferries). Read 'Port Alder ferries to run later on "
        "Fridays' on news.example.com, published Mon Oct 19:"
    )


def test_a_redirect_to_another_host_is_said_and_every_hop_goes_through_the_guard() -> None:
    """The read ended elsewhere than asked; and a hop to a private address is refused."""
    site = Site({
        ASKED: lambda: page_reply("", 301, location="https://mirror.example.org/ferries"),
        "https://mirror.example.org/ferries": _html("short_note.html"),
    })
    result = _call(_tool(), site)
    assert result["address"] == "https://mirror.example.org/ferries"
    assert result["summary"].startswith(
        "The address of this page is mirror.example.org/ferries, not the one you gave "
        "(news.example.com/s/ferries). Read"
    )
    private = Site({ASKED: lambda: page_reply("", 302, location="http://intranet.example.net/x")})
    with (
        fake_dns(intranet_example_net="10.0.0.5"), fake_web(private.answer) as seen,
        pytest.raises(ToolError) as refused,
    ):
        _tool().handler({"url": ASKED}, tool_context(clock=FakeClock(NOW)))
    assert refused.value.code == "ssrf_guard_refused"
    assert [str(r.url) for r in seen] == [ASKED]


# --- a long page -------------------------------------------------------------------------------


def test_a_long_page_comes_in_parts_cut_at_paragraphs_with_the_count() -> None:
    """Every part is under a result's size; together they are the page, none cut mid-paragraph."""
    site = Site({LONG: _html("long_article.html")})
    tool = _tool()
    parts = [_call(tool, site, {"url": LONG, "page": n}) for n in (1, 2, 3)]
    assert [(p["page"], p["parts"]) for p in parts] == [(1, 3), (2, 3), (3, 3)]
    assert all(_size(p) <= 4_000 for p in parts)
    assert parts[0]["summary"].endswith("read directly.")
    assert "part 1 of 3, page=2 reads the next" in parts[0]["summary"]
    assert "part 3 of 3" in parts[2]["summary"]
    assert "page=" not in parts[2]["summary"]
    paragraphs = "\n\n".join(p["content"] for p in parts).split("\n\n")
    assert paragraphs[0] == "Port Alder ferries to run later on Fridays"
    assert paragraphs[-2].startswith("The new timetable takes effect on Friday 6 November.")
    assert paragraphs[-1].startswith("Sign up for the Northwind Press newsletter")
    assert any(one.startswith("Parking at the Quay terminal") for one in paragraphs)
    assert all(one.endswith(".") for one in paragraphs[2:])  # after the heading and the byline


def test_a_part_that_is_not_there_is_an_error_that_says_how_many_there_are() -> None:
    """Page 9 of a page with three parts."""
    site = Site({LONG: _html("long_article.html")})
    error = _error(_tool(), site, {"url": LONG, "page": 9})
    assert error.code == "invalid_arguments"
    assert error.kind == "retry_other_args"
    assert "the page has 3 parts, so page 9 does not exist; ask for page 1 to 3" in str(error)


@pytest.mark.parametrize("page", [0, -1, "two", 1.5, True])
def test_a_page_that_is_not_a_number_from_one_is_an_error(page: object) -> None:
    """The argument is named, with what it was."""
    error = _error(_tool(), Site({}), {"page": page})
    assert error.code == "invalid_arguments"
    assert f"page must be a whole number from 1 (got {page!r})" in str(error)


def test_looking_for_gives_the_opening_and_the_paragraphs_that_match_in_page_order() -> None:
    """The question lands in the middle of the article; that paragraph comes, with the opening."""
    site = Site({LONG: _html("long_article.html")})
    result = _call(_tool(), site, {"url": LONG, "looking_for": "parking fee at the terminal"})
    paragraphs = result["content"].split("\n\n")
    assert paragraphs[0] == "Port Alder ferries to run later on Fridays"
    assert any(one.startswith("Parking at the Quay terminal will stay free until 9 p.m.")
               for one in paragraphs)
    assert _size(result) <= 4_000
    assert "page" not in result
    assert result["left_out"] > 0
    assert "that best match 'parking fee at the terminal'" in result["summary"]
    assert f"{result['left_out']} of 25 paragraphs left out" in result["summary"]
    assert "left_out" in result


def test_looking_for_with_nothing_that_matches_says_only_the_opening_is_shown() -> None:
    """No other paragraph matches: the result says so, and the parts are still there."""
    site = Site({LONG: _html("long_article.html")})
    result = _call(_tool(), site, {"url": LONG, "looking_for": "quantum chromodynamics"})
    assert result["content"].count("\n\n") == 0
    assert "only the opening paragraph; no other of its 25 paragraphs matches" in result["summary"]
    assert "so try the words the page itself uses" in result["summary"]


def test_chinese_words_for_an_english_page_find_nothing_and_the_result_says_what_to_try() -> None:
    """The model is told, in the result, to ask again in the page's words."""
    site = Site({LONG: _html("long_article.html")})
    result = _call(_tool(), site, {"url": LONG, "looking_for": "停车费"})
    assert "no other of its 25 paragraphs matches '停车费', so try the words" in result["summary"]
    again = _call(_tool(), site, {"url": LONG, "looking_for": "parking fee"})
    assert "Parking at the Quay terminal" in again["content"]


def test_a_page_that_fits_in_one_result_is_whole_whatever_is_looked_for() -> None:
    """Nothing is left out of a short page."""
    site = Site({ASKED: _html("short_note.html")})
    result = _call(_tool(), site, {"looking_for": "unrelated words"})
    assert "Fennel Point washrooms are open as usual" in result["content"]
    assert "left_out" not in result
    assert "the whole page" in result["summary"]


def test_a_chinese_page_is_read_where_a_chinese_question_is() -> None:
    """Characters are matched in pairs, so a Chinese question finds its paragraph."""
    site = Site({ASKED: _html("long_article_zh.html")})
    result = _call(_tool(), site, {"looking_for": "自行车 免票"})
    assert "骑自行车的人在两班晚船上可以免票上船" in result["content"]
    assert _size(result) <= 4_000
    assert result["left_out"] > 0


def test_looking_for_wins_over_page_and_the_line_says_so() -> None:
    """Both given: the paragraphs are chosen by the words, and page is not used."""
    site = Site({LONG: _html("long_article.html")})
    result = _call(_tool(), site, {"url": LONG, "looking_for": "parking", "page": 3})
    assert "page" not in result
    assert "page was not used" in result["summary"]


def test_a_result_with_the_longest_title_address_and_summary_still_fits() -> None:
    """The worst case the tool's own fields can make stays under the size a result is held to."""
    long_title = "A very long headline " * 20
    html = (
        f'<html><head><title>{long_title}</title>'
        f'<link rel="canonical" href="https://news.example.com/{"p" * 400}?id=1"></head><body>'
        + "".join(f"<p>{'word ' * 120}</p>" for _ in range(20)) + "</body></html>"
    )
    site = Site({ASKED: lambda: page_reply(html)})
    result = _call(_tool(), site, {"page": 2})
    assert _size(result) <= 4_000
    assert result["address"].endswith("…")
    assert len(result["title"]) <= 160


# --- the receipt -------------------------------------------------------------------------------


def test_the_receipt_is_a_read_that_names_the_site_the_title_and_the_part_shown() -> None:
    """What she may say she read: this part of this page on this site."""
    site = Site({LONG: _html("long_article.html")})
    receipt = _call(_tool(), site, {"url": LONG, "page": 2}).receipt
    assert (receipt.status, receipt.reads_only) == ("done", True)
    assert receipt.line == "read news.example.com: part 2 of 3, page=3 reads the next"
    assert receipt.covered is not None
    assert receipt.covered.sources == ("news.example.com",)
    assert receipt.covered.window == (
        "'Port Alder ferries to run later on Fridays': part 2 of 3, page=3 reads the next"
    )
    covered = receipt.covered
    assert (covered.shown, covered.more, covered.how_more) == (1, 2, "page=3")
    whole = _call(_tool(), Site({ASKED: _html("short_note.html")})).receipt.covered
    assert whole is not None
    assert (whole.shown, whole.more, whole.how_more) == (1, 0, "")


# --- a page that did not load ------------------------------------------------------------------


@pytest.mark.parametrize("status", [404, 410])
def test_a_page_not_found_is_an_error_that_says_to_check_the_address(status: int) -> None:
    """Its body is not the page; the kind is the one another address may fix."""
    site = Site({ASKED: lambda: page_reply("<html><body>Oops, nothing here</body></html>", status)})
    error = _error(_tool(extract_key=KEY), site)
    assert (error.code, error.kind) == ("page_not_found", "retry_other_args")
    assert str(error) == (
        f"web_fetch: the page was not found ({status}) at news.example.com/s/ferries; check the "
        "address, or find the right page with web_search"
    )
    assert site.extract_requests() == []  # a page that is not there is not read by anyone else


@pytest.mark.parametrize("status", [500, 502, 503])
def test_a_site_that_failed_is_an_error_that_says_it_is_not_working_now(status: int) -> None:
    """The site's failure: do not try the same again; no extract."""
    site = Site({ASKED: lambda: page_reply("<html><body>Error</body></html>", status)})
    error = _error(_tool(extract_key=KEY), site)
    assert (error.code, error.kind) == ("site_failed", "dont_retry")
    assert f"the site failed ({status}) at news.example.com/s/ferries" in str(error)
    assert site.extract_requests() == []


def test_a_site_that_does_not_answer_is_site_failed_too() -> None:
    """A connection that breaks is the site not answering."""

    def broken() -> httpx.Response:
        msg = "connection reset"
        raise httpx.ConnectError(msg)

    error = _error(_tool(), Site({ASKED: broken}))
    assert (error.code, error.kind) == ("site_failed", "dont_retry")
    assert "the site did not answer (connection reset)" in str(error)


@pytest.mark.parametrize("status", [401, 403, 429])
def test_a_refusal_without_the_extract_service_is_page_refused(status: int) -> None:
    """The site would not give the page, and there is nobody else to ask."""
    site = Site({ASKED: lambda: page_reply("<html><body>Forbidden</body></html>", status)})
    error = _error(_tool(), site)
    assert (error.code, error.kind) == ("page_refused", "impossible")
    assert str(error) == (
        f"web_fetch: the site refused to give the page ({status}) at news.example.com/s/ferries; "
        "tell him, or look for the same story on another site with web_search"
    )


@pytest.mark.parametrize("status", [401, 403, 429])
def test_a_refusal_the_extract_service_reads_past_is_read_by_extract_and_says_why(
    status: int,
) -> None:
    """The extract service is asked once, for that page, and the result says it read it."""
    site = Site(
        {ASKED: lambda: page_reply("<html><body>Forbidden</body></html>", status)},
        extract=_extracted("# Harbour notice\n\nThe late boats start on 6 November.\n\n"
                           "The last crossing from the Quay leaves at 12:15 a.m."),
    )
    result = _call(_tool(extract_key=KEY), site)
    assert result["read_by"] == "extract"
    assert result["title"] == "Harbour notice"
    assert result["content"].startswith("# Harbour notice")
    assert result["summary"] == (
        f"Read 'Harbour notice' on news.example.com: the whole page; read by the extract service "
        f"because the site refused it ({status})."
    )
    (asked,) = site.extract_requests()
    assert json.loads(asked.content) == {"urls": [ASKED], "extract_depth": "basic"}
    assert asked.headers["authorization"] == f"Bearer {KEY}"


def test_a_refusal_the_extract_service_cannot_read_either_says_so() -> None:
    """Both failed: refused, and the message says the extract service tried."""
    site = Site(
        {ASKED: lambda: page_reply("", 403)},
        extract=json_reply({"results": [], "failed_results": [{"url": ASKED, "error": "blocked"}]}),
    )
    error = _error(_tool(extract_key=KEY), site)
    assert error.code == "page_refused"
    assert "and the extract service could not read it either" in str(error)


def test_a_refusal_that_is_a_451_or_another_4xx_is_not_sent_to_the_extract_service() -> None:
    """Only 401, 403 and 429 may be read past."""
    for status in (451, 400):
        site = Site({ASKED: _status(status)})
        error = _error(_tool(extract_key=KEY), site)
        assert error.code == "page_refused"
        assert site.extract_requests() == []


def test_the_extract_service_failing_is_a_refusal_not_a_crash() -> None:
    """Its own failure (a 500) is logged by type and the page is refused."""
    site = Site({ASKED: lambda: page_reply("", 403)}, extract=httpx.Response(500, json={"x": 1}))
    error = _error(_tool(extract_key=KEY), site)
    assert error.code == "page_refused"
    assert KEY not in str(error)


# --- walls -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("page", "why"),
    [
        ("marked_paywall.html", "a paywall: the page marks its text as not free to read"),
        ("teaser.html", "a paywall: only a teaser is shown"),
        ("teaser_zh.html", "a paywall: only a teaser is shown"),
        ("bot_check.html", "a bot check"),
        ("consent.html", "a cookie consent page"),
    ],
)
def test_a_wall_is_an_error_whatever_text_it_still_shows(page: str, why: str) -> None:
    """Its own marks decide: the schema.org flag, a challenge, a consent page, a teaser."""
    site = Site({ASKED: _html(page)})
    error = _error(_tool(extract_key=KEY), site)
    assert (error.code, error.kind) == ("page_walled", "impossible")
    assert str(error) == (
        f"web_fetch: the page at news.example.com/s/ferries is behind {why}; tell him, or look "
        "for the same story on another site with web_search"
    )
    assert site.extract_requests() == []  # a wall found in a 200 is not read past


def test_a_long_article_that_talks_about_subscribing_is_still_read() -> None:
    """It quotes 'subscribe to continue reading' and has a newsletter line; it is an article."""
    page = _text("long_article.html")
    assert "subscribe to continue reading" in page
    assert "Subscribe to our weekly newsletter" in page
    site = Site({LONG: _html("long_article.html")})
    result = _call(_tool(), site, {"url": LONG})
    assert result["read_by"] == "direct"
    assert result["page"] == 1


def test_a_short_page_with_no_mark_of_a_wall_is_a_note_not_a_wall() -> None:
    """Short is not walled by itself."""
    result = _call(_tool(), Site({ASKED: _html("short_note.html")}))
    assert result["read_by"] == "direct"


def test_a_teaser_the_extract_service_returns_is_a_wall_too() -> None:
    """The site refused the brain, the extract service got a teaser: walled, not read."""
    teaser = (
        "# Inside the harbour board's closed-door budget talks\n\nThe board spent four hours "
        "behind closed doors on Tuesday.\n\nSubscribe to continue reading. Already a subscriber? "
        "Sign in."
    )
    site = Site({ASKED: lambda: page_reply("", 403)}, extract=_extracted(teaser))
    error = _error(_tool(extract_key=KEY), site)
    assert (error.code, error.kind) == ("page_walled", "impossible")
    assert "a paywall: only a teaser is shown" in str(error)


def test_a_refusal_whose_page_is_a_bot_check_is_a_wall_not_a_refusal() -> None:
    """The 403 carries a challenge page; nobody else could read it; it is a wall."""
    site = Site({ASKED: lambda: page_reply(_text("bot_check.html"), 403)})
    error = _error(_tool(), site)
    assert error.code == "page_walled"
    assert "a bot check" in str(error)


# --- a page with no text, a PDF, something that is not a page ---------------------------------


def test_a_page_with_no_text_without_its_scripts_is_read_by_the_extract_service() -> None:
    """The shell has nothing to read; the extract service renders it; the result says why."""
    site = Site(
        {ASKED: _html("js_shell.html")},
        extract=_extracted("# Harbour live board\n\nQuay terminal: next sailing 6:05 p.m."),
    )
    result = _call(_tool(extract_key=KEY), site)
    assert result["read_by"] == "extract"
    assert "because the page has no text without its scripts" in result["summary"]


def test_a_page_with_no_text_and_no_extract_service_is_an_error_that_says_why() -> None:
    """Not a read: the tool does not run scripts."""
    error = _error(_tool(), Site({ASKED: _html("js_shell.html")}))
    assert (error.code, error.kind) == ("page_unreadable", "impossible")
    assert "the page has no text without running its scripts, which this tool does not do" in str(
        error,
    )


def test_a_pdf_goes_to_the_extract_service_and_without_one_is_unreadable() -> None:
    """A PDF is not text for the brain."""
    pdf = lambda: httpx.Response(  # noqa: E731
        200, content=b"%PDF-1.7 not text", headers={"content-type": "application/pdf"},
    )
    read = _call(
        _tool(extract_key=KEY),
        Site({ASKED: pdf}, extract=_extracted("# Annual report\n\nRidership rose to 1.4 million.")),
    )
    assert read["read_by"] == "extract"
    assert "because it is a PDF" in read["summary"]
    error = _error(_tool(), Site({ASKED: pdf}))
    assert error.code == "page_unreadable"
    assert "it is a PDF, and no extract service is set up to read one" in str(error)


def test_an_image_is_not_a_page_of_text() -> None:
    """The content type is named."""
    image = lambda: httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"})  # noqa: E731
    error = _error(_tool(extract_key=KEY), Site({ASKED: image}))
    assert error.code == "page_unreadable"
    assert "it is image/png, not a page of text" in str(error)


def test_the_extract_service_is_never_asked_for_a_page_that_read() -> None:
    """With a key, a readable page costs no extract request."""
    site = Site({ASKED: _html("short_note.html")})
    _call(_tool(extract_key=KEY), site)
    assert site.extract_requests() == []


# --- arguments and the menu --------------------------------------------------------------------


def test_the_address_must_be_a_public_web_address() -> None:
    """The guard's refusals keep their words and their code."""
    error = _error(_tool(), Site({}), {"url": "ftp://example.com/x"})
    assert error.code == "ssrf_guard_refused"
    assert "was refused (scheme 'ftp' is not in the http/https allowlist)" in str(error)
    empty = _error(_tool(), Site({}), {"url": " "})
    assert empty.code == "invalid_arguments"


def test_the_menu_describes_web_fetch_and_every_parameter() -> None:
    """The description is the standard's; every parameter is described; the address is required."""
    tool = _tool()
    assert len(tool.description) <= 600
    assert " he " in f" {tool.description} "
    assert "web_search" in tool.description
    assert "map_search" in tool.description
    properties = tool.input_schema["properties"]
    assert set(properties) == {"url", "looking_for", "page"}
    assert all(one["description"] for one in properties.values())
    assert tool.input_schema["required"] == ["url"]
    assert properties["page"]["type"] == "integer"
