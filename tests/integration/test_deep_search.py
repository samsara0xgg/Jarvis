"""The deep backend of `web_search` over Anthropic's messages (ADR 0289, 0359).

The seam is built the way the runtime builds it and driven over the shared scripted model, which
streams Anthropic's raw events; the clock is the test's, so the minute is a thing the test moves.
Nothing reaches the network and nothing sleeps.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.runtime.deep_search import DeepSearchError, build_deep_search
from jarvis.runtime.search_location import HomePlace
from jarvis.shared.here import Coarse
from jarvis.state.event_log import open_event_log
from tests.canary._helpers import repo_root
from tests.support.clock import FakeClock
from tests.support.model import Reply, ScriptedModel, ScriptedSessions

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from jarvis.execution.web_search_tool import DeepFindings, DeepSearch
    from tests.support.model import Request, Step

VICTORIA = HomePlace(48.4284, -123.3656, "Victoria", "CA", "America/Vancouver")
SAANICH = Coarse(48.46, -123.31)  # about 5 km from VICTORIA
TOKYO = Coarse(35.68, 139.69)

_ANTHROPIC = {
    "provider": "anthropic", "model": "claude-sonnet-5-5", "api_key_env": "ANTHROPIC_API_KEY",
    "max_tokens": 4096,
}
_LLM = {"provider": "anthropic", "default_preset": "sonnet", "presets": {"sonnet": _ANTHROPIC}}


def _search(search_id: str, query: str) -> dict[str, Any]:
    return {"type": "server_tool_use", "id": search_id, "name": "web_search",
            "input": {"query": query}}


def _page(url: str, title: str, age: str | None = None) -> dict[str, Any]:
    page = {"type": "web_search_result", "url": url, "title": title}
    return {**page, "page_age": age} if age else page


def _results(search_id: str, *pages: dict[str, Any]) -> dict[str, Any]:
    return {"type": "web_search_tool_result", "tool_use_id": search_id, "content": list(pages)}


def _text(text: str, *cited: tuple[str, str]) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "text", "text": text}
    if cited:
        block["citations"] = [
            {"type": "web_search_result_location", "url": url, "title": title}
            for url, title in cited
        ]
    return block


def _message(blocks: list[dict[str, Any]], stop: str = "end_turn", **extra: Any) -> Reply:  # noqa: ANN401
    return Reply(raw={"content": blocks, "stop_reason": stop}, **extra)


ONE = _page("https://www.one.example/a", "One", "October 20, 2026")
TWO = _page("https://two.example/b", "Two", "3 days ago")
THREE = _page("https://three.example/c", "Three")


def _brief_message() -> Reply:
    """Notes, two searches, then a brief citing a page."""
    return _message([
        {"type": "thinking", "thinking": "plan"},
        _text("r is a string; fetch directly."),
        _search("s1", "x"),
        _results("s1", ONE, TWO),
        _text("Checking prices next."),
        _search("s2", "y"),
        _results("s2", _page("https://two.example/b", "Two again"), THREE),
        {"type": "thinking", "thinking": "write it"},
        _text("A costs 10 yuan. ", ("https://three.example/c", "Three")),
        _text("B costs 12."),
    ])


def _deep(
    model: ScriptedModel, *, home: HomePlace | None = None, max_uses: int | None = None,
    **accounting: Any,  # noqa: ANN401
) -> DeepSearch:
    config: dict[str, Any] = {
        "llm": {"presets": {"sonnet": dict(_ANTHROPIC)}},
        "tools": {"web": {"deep_preset": "sonnet"}},
    }
    if max_uses:
        config["tools"]["web"]["deep_max_uses"] = max_uses
    built = build_deep_search(
        config, sessions=ScriptedSessions(model, _LLM), home=home, **accounting,
    )
    assert built is not None
    return built


def _model(*steps: Step, otherwise: Step | None = None) -> ScriptedModel:
    return ScriptedModel(*steps, otherwise=otherwise, provider="anthropic")


def _user_location(request: Request) -> dict[str, Any] | None:
    (tool,) = request.tools or []
    location = tool.get("user_location")
    return location if isinstance(location, dict) else None


# --- where he is and what day it is -----------------------------------------------------------


def test_the_request_is_the_question_with_the_search_tool_his_place_and_the_weekday() -> None:
    """The question is the user turn; the tool carries where he is; the date has its weekday."""
    model = _model(_brief_message())
    clock = FakeClock()  # Wednesday 2026-10-14, 09:00 in Vancouver
    deep = _deep(model, home=VICTORIA, max_uses=6)
    findings = deep.research("A or B?", where=SAANICH, clock=clock)
    (request,) = model.requests
    assert request.messages == [{"role": "user", "content": "A or B?"}]
    assert request.tools == [{
        "type": "web_search_20260209", "name": "web_search", "max_uses": 6,
        "user_location": {
            "type": "approximate", "city": "Victoria", "country": "CA",
            "timezone": "America/Vancouver",
        },
    }]
    assert request.system.startswith("Today is Wednesday, 2026-10-14. ")
    assert "language of the question" in request.system
    assert findings.brief == "A costs 10 yuan. B costs 12."


def test_far_from_home_the_place_is_the_country_and_zone_of_where_he_is_without_a_city() -> None:
    """Home's city is his city only within 30 km of home."""
    model = _model(_brief_message())
    _deep(model, home=VICTORIA).research("q", where=TOKYO, clock=FakeClock())
    assert _user_location(model.requests[0]) == {
        "type": "approximate", "country": "JP", "timezone": "Asia/Tokyo",
    }


def test_with_no_report_of_where_he_is_the_place_is_home() -> None:
    """No coarse reading: home's own place; with no home either, no location is sent."""
    model = _model(_brief_message(), _brief_message())
    clock = FakeClock()
    _deep(model, home=VICTORIA).research("q", where=None, clock=clock)
    _deep(model).research("q", where=None, clock=clock)
    assert _user_location(model.requests[0]) == {
        "type": "approximate", "city": "Victoria", "country": "CA", "timezone": "America/Vancouver",
    }
    assert _user_location(model.requests[1]) is None


def test_a_home_with_no_name_gives_a_country_and_zone_but_no_city() -> None:
    """``home.weather`` need not name the city; the config gives only what it has."""
    home = HomePlace.from_weather(
        {"latitude": 48.4284, "longitude": -123.3656, "country_code": "ca"}, "America/Vancouver",
    )
    model = _model(_brief_message())
    _deep(model, home=home).research("q", where=SAANICH, clock=FakeClock())
    assert _user_location(model.requests[0]) == {
        "type": "approximate", "country": "CA", "timezone": "America/Vancouver",
    }


def test_a_home_with_no_country_takes_it_from_its_zone() -> None:
    """The country is the zone's when the config names none."""
    place = {"latitude": 48.4284, "longitude": -123.3656}
    home = HomePlace.from_weather(place, "America/Vancouver")
    assert home.country == "CA"


# --- what the brief and its sources are -------------------------------------------------------


def test_only_the_brief_after_the_last_search_is_the_brief() -> None:
    """Notes before and between searches are scratch work; only the closing text is the answer."""
    found = _deep(_model(_brief_message())).research("q", where=None, clock=FakeClock())
    assert found.brief == "A costs 10 yuan. B costs 12."
    assert found.stopped is None
    assert found.searches == 2


def test_sources_are_cited_pages_first_then_the_briefs_own_list_without_repeats() -> None:
    """Cited pages lead, then the pages the brief lists; each is dated by its own search result."""
    message = _message([
        _search("s1", "x"),
        _results("s1", ONE, TWO, THREE),
        _text("B costs 12.", ("https://two.example/b", "Two")),
        _text(
            "\n\nSources:\nhttps://four.example/a。\n- https://three.example/c\n"
            "- https://www.one.example/a\n",
        ),
    ])
    found = _deep(_model(message)).research("q", where=None, clock=FakeClock())
    assert [(s.title, s.site, s.date, s.url) for s in found.sources] == [
        ("Two", "two.example", "3 days ago", "https://two.example/b"),
        ("", "four.example", None, "https://four.example/a"),
        ("Three", "three.example", None, "https://three.example/c"),
        ("One", "one.example", "2026-10-20", "https://www.one.example/a"),
    ]
    assert found.brief == "B costs 12."


def test_a_brief_with_no_citations_and_no_list_has_no_sources_though_pages_were_found() -> None:
    """Search results alone are not sources: in the live check they were mostly unused pages."""
    message = _message([_search("s1", "x"), _results("s1", ONE, TWO), _text("B costs 12.")])
    found = _deep(_model(message)).research("q", where=None, clock=FakeClock())
    assert found.sources == ()
    assert [p.site for p in found.pages] == ["one.example", "two.example"]


def test_sources_are_capped_at_ten() -> None:
    """Ten sources at most."""
    listed = "B costs 12.\n来源\uff1a\n" + "\n".join(f"https://p{i}.example/x" for i in range(30))
    found = _deep(_model(_message([_search("s1", "x"), _text(listed)]))).research(
        "q", where=None, clock=FakeClock(),
    )
    assert len(found.sources) == 10


def test_a_code_execution_result_also_ends_the_notes() -> None:
    """A code execution result is a tool block too: the text before it is not the brief."""
    message = _message([
        _search("s1", "x"), _text("scratch"),
        {"type": "code_execution_tool_result", "content": {}}, _text("B costs 12."),
    ])
    found = _deep(_model(message)).research("q", where=None, clock=FakeClock())
    assert found.brief == "B costs 12."


def test_searches_come_from_the_providers_count_else_from_the_search_blocks() -> None:
    """The search count is the provider's own, else the search blocks counted."""
    counted = Reply(raw={
        "content": [_search("s1", "x"), _text("B")], "stop_reason": "end_turn",
        "usage": {"server_tool_use": {"web_search_requests": 5}},
    })
    clock = FakeClock()
    assert _deep(_model(counted)).research("q", where=None, clock=clock).searches == 5
    assert _deep(_model(_brief_message())).research("q", where=None, clock=clock).searches == 2


def test_a_brief_cut_by_the_token_limit_is_marked() -> None:
    """The model ran out of room: the brief is given, marked as one that may be unfinished."""
    message = _message([_search("s1", "x"), _text("B costs")], stop="max_tokens")
    found = _deep(_model(message)).research("q", where=None, clock=FakeClock())
    assert found.brief == "B costs"
    assert found.stopped == "length"


def test_a_search_that_ended_on_a_search_hands_over_its_pages_as_a_failure() -> None:
    """No text after the last search is no brief: the pages are still worth giving."""
    message = _message([_search("s1", "x"), _results("s1", ONE, TWO)])
    found = _deep(_model(message)).research("q", where=None, clock=FakeClock())
    assert found.brief == ""
    assert found.stopped == "failed"
    assert [p.site for p in found.pages] == ["one.example", "two.example"]


def test_a_search_that_ended_with_nothing_is_an_error() -> None:
    """No brief and no page, and not out of time: the search failed."""
    with pytest.raises(DeepSearchError, match="without a written brief"):
        _deep(_model(_message([]))).research("q", where=None, clock=FakeClock())


def test_a_stream_that_broke_after_a_search_keeps_the_pages_it_had() -> None:
    """A break after pages is a failure with pages, not a lost search."""
    message = _message([_search("s1", "x"), _results("s1", ONE)], fails=ConnectionResetError("x"))
    found = _deep(_model(message)).research("q", where=None, clock=FakeClock())
    assert found.stopped == "failed"
    assert [p.site for p in found.pages] == ["one.example"]


def test_a_stream_that_broke_before_anything_is_an_error_that_leaks_no_key() -> None:
    """The error names its type and a bounded excerpt, with anything key-shaped scrubbed."""
    error = RuntimeError("401 for key sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    with pytest.raises(DeepSearchError) as caught:
        _deep(_model(_message([], fails=error))).research("q", where=None, clock=FakeClock())
    assert "RuntimeError" in str(caught.value)
    assert "sk-ant" not in str(caught.value)


# --- going on after a pause, and the minute ---------------------------------------------------


def test_a_paused_turn_is_sent_again_with_its_own_blocks_and_no_user_message_between() -> None:
    """``pause_turn`` is the API asking to be called again with what it wrote so far."""
    first = [{"type": "thinking", "thinking": "plan", "signature": "sig"},
             _search("s1", "x"), _results("s1", ONE)]
    model = _model(
        _message(first, stop="pause_turn"),
        _message([_text("Done. ", ("https://www.one.example/a", "One"))]),
    )
    found = _deep(model).research("A or B?", where=None, clock=FakeClock())
    assert len(model.requests) == 2
    again = model.requests[1]
    assert again.messages == [
        {"role": "user", "content": "A or B?"}, {"role": "assistant", "content": first},
    ]
    assert again.tools == model.requests[0].tools
    assert again.system == model.requests[0].system
    assert found.brief == "Done."
    assert found.stopped is None
    assert [s.date for s in found.sources] == ["2026-10-20"]  # the first request's page, dated
    assert found.searches == 1


def test_a_second_pause_goes_back_with_both_requests_blocks() -> None:
    """The transcript grows by each request's blocks, in order."""
    a, b = [_search("s1", "x"), _results("s1", ONE)], [_search("s2", "y"), _results("s2", TWO)]
    model = _model(
        _message(a, stop="pause_turn"), _message(b, stop="pause_turn"), _message([_text("Done.")]),
    )
    _deep(model).research("q", where=None, clock=FakeClock())
    assert model.requests[2].messages[1:] == [{"role": "assistant", "content": [*a, *b]}]


def _quiet_search(pages: list[dict[str, Any]], quiet: threading.Event) -> Reply:
    return _message([_search("sq", "z"), _results("sq", *pages)], quiet=quiet)


def _research_until_quiet(
    deep: DeepSearch, clock: FakeClock, quiet: threading.Event, *, where: Coarse | None = None,
) -> DeepFindings:
    """Run the search on a thread; once its stream is silent and the call waits, end the minute."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(deep.research, "q", where=where, clock=clock)
        assert quiet.wait(5.0), "the stream never reached its silence"
        clock.wait_until_parked()
        clock.advance(60)
        return running.result(timeout=5.0)


def test_a_stream_gone_silent_ends_with_the_pages_found_when_the_minute_is_up() -> None:
    """The minute is the clock's: the call returns at the deadline though nothing ever arrives."""
    quiet = threading.Event()
    model = _model(_quiet_search([ONE, TWO], quiet))
    found = _research_until_quiet(_deep(model), FakeClock(), quiet)
    assert found.brief == ""
    assert found.stopped == "minute"
    assert [p.site for p in found.pages] == ["one.example", "two.example"]
    assert found.searches == 1


def test_one_deadline_covers_every_request_of_the_search() -> None:
    """A paused turn goes again with the time left; the second request is cut at the same minute.

    The pages of both requests are given.
    """
    quiet = threading.Event()
    model = _model(
        _message([_search("s1", "x"), _results("s1", ONE)], stop="pause_turn"),
        _quiet_search([THREE], quiet),
    )
    found = _research_until_quiet(_deep(model), FakeClock(), quiet)
    assert len(model.requests) == 2
    assert found.stopped == "minute"
    assert [p.site for p in found.pages] == ["one.example", "three.example"]
    assert found.searches == 2


def test_a_silent_stream_before_any_page_leaves_nothing_to_hand_over() -> None:
    """The minute passes with nothing received: no pages, marked as the minute's."""
    quiet = threading.Event()
    found = _research_until_quiet(
        _deep(_model(_message([], quiet=quiet))), FakeClock(), quiet,
    )
    assert found.pages == ()
    assert found.brief == ""
    assert found.stopped == "minute"


class _MinuteUpAfterTheFirstRequest(FakeClock):
    """A clock that shows the minute as passed once the model has been asked.

    Its wait ends only when its event is set (a bounded real guard, never the time that passed).
    """

    def __init__(self, model: ScriptedModel) -> None:
        super().__init__()
        self._model = model

    def monotonic(self) -> float:
        return super().monotonic() + (61.0 if self._model.requests else 0.0)

    def wait(self, event: threading.Event, timeout: float | None) -> bool:  # noqa: ARG002
        return event.wait(5.0)


def test_a_pause_with_the_minute_up_is_not_sent_again() -> None:
    """Time left is the only reason to send a paused turn again."""
    model = _model(
        _message([_search("s1", "x"), _results("s1", ONE)], stop="pause_turn"),
        lambda _request: pytest.fail("a request was sent after the minute"),
    )
    found = _deep(model).research("q", where=None, clock=_MinuteUpAfterTheFirstRequest(model))
    assert len(model.requests) == 1
    assert found.stopped == "minute"
    assert [p.site for p in found.pages] == ["one.example"]


# --- what it cost -----------------------------------------------------------------------------


def _cost_rows(path: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(path) as db:
        rows = db.execute(
            "SELECT payload_json FROM events WHERE type = 'cost.recorded' ORDER BY id",
        ).fetchall()
    return [json.loads(payload) for (payload,) in rows]


def test_every_request_has_its_own_cost_row_and_the_one_cut_at_the_minute_says_so(
    tmp_path: Path,
) -> None:
    """Two requests, two rows; the last one, cancelled at the deadline, is not a free call."""
    log = tmp_path / "events.db"
    open_event_log(log).close()
    quiet = threading.Event()
    model = _model(
        _message([_search("s1", "x"), _results("s1", ONE)], stop="pause_turn"),
        _quiet_search([THREE], quiet),
    )
    deep = _deep(
        model, event_log_path=log, pricing_path=repo_root() / "data" / "pricing.json",
        account_cost=True,
    )
    _research_until_quiet(deep, FakeClock(), quiet)
    first, second = _cost_rows(log)
    assert (first["kind"], first["disposition"]) == ("deep_search", "completed")
    assert (second["kind"], second["disposition"]) == ("deep_search", "cancelled")
    assert second["usage_status"] != "provider_final"
    assert first["model"] == "claude-sonnet-5-5"
    assert first["tokens_in"] == 12


def test_a_request_that_finished_is_one_completed_row(tmp_path: Path) -> None:
    """A search that ends in its brief has one completed row with its usage."""
    log = tmp_path / "events.db"
    open_event_log(log).close()
    deep = _deep(
        _model(_brief_message()), event_log_path=log,
        pricing_path=repo_root() / "data" / "pricing.json", account_cost=True,
    )
    deep.research("q", where=None, clock=FakeClock())
    (row,) = _cost_rows(log)
    assert row["disposition"] == "completed"
    assert row["usage_status"] == "provider_final"
    assert row["cost_usd"] is not None


# --- building it ------------------------------------------------------------------------------


def _config(preset: Mapping[str, Any] | None, deep_preset: str = "sonnet") -> dict[str, Any]:
    return {
        "llm": {"presets": {} if preset is None else {"sonnet": dict(preset)}},
        "tools": {"web": {"deep_preset": deep_preset}},
    }


def test_the_builder_wants_an_anthropic_preset_on_anthropics_own_host() -> None:
    """Only an Anthropic preset on Anthropic's host has server web search."""
    assert build_deep_search(_config(_ANTHROPIC)) is not None
    own = {**_ANTHROPIC, "base_url": "https://api.anthropic.com"}
    foreign = {**_ANTHROPIC, "base_url": "https://proxy.example/v1"}
    assert build_deep_search(_config(own)) is not None
    assert build_deep_search(_config(foreign)) is None
    assert build_deep_search(_config({**_ANTHROPIC, "provider": "openai"})) is None
    assert build_deep_search(_config(None)) is None
    assert build_deep_search(_config(_ANTHROPIC, "missing")) is None
    assert build_deep_search({}) is None
