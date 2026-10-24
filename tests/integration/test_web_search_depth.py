"""`web_search` depth: quick is the keyed backend, deep is one nested Anthropic web search.

The handler is driven for real over a fake backend and a fake `DeepSearch` (ADR 0359). What the
deep backend does with Anthropic's messages is in `test_deep_search.py`. Nothing reaches the
network.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

import pytest

from jarvis.execution import web_search_tool
from jarvis.execution.tools import Receipted, Tool, ToolError, build_default_registry
from jarvis.execution.web_search_tool import DeepFindings, Hit, Source
from jarvis.shared.here import Coarse
from tests.support.clock import FakeClock
from tests.support.here import FixedHere
from tests.support.tool_context import tool_context

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from jarvis.shared.clock import Clock

NOW = datetime(2026, 10, 21, 9, 30, tzinfo=FakeClock().zone)  # a Wednesday


def _web_search(tmp_path: Path, deep: object | None = None) -> Tool:
    registry = build_default_registry(
        obsidian_vault_root=tmp_path, web_search_provider="ddgs", web_search_max_results=3,
        web_deep_search=cast("Any", deep),
    )
    found = next(t for t in registry.get_definitions() if t.name == "web_search")
    assert isinstance(found, Tool)
    return found


def _call(
    tool: Tool, args: Mapping[str, Any], *, here: FixedHere | None = None,
) -> Receipted:
    clock = FakeClock(NOW)
    result = tool.handler(
        args, tool_context(clock=clock, here=None if here is None else here.bind(None)),
    )
    assert isinstance(result, Receipted)
    return result


class _Deep:
    """A deep backend that answers the findings it was given, or breaks, and keeps the asks."""

    def __init__(self, findings: DeepFindings | None = None) -> None:
        self.findings = findings
        self.asked: list[tuple[str, Coarse | None, Clock]] = []

    def research(self, query: str, *, where: Coarse | None, clock: Clock) -> DeepFindings:
        self.asked.append((query, where, clock))
        if self.findings is None:
            msg = "boom sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
            raise RuntimeError(msg)
        return self.findings


ONE = Source("Review of A and B", "review.example", "2026-10-20", "https://www.review.example/ab")
TWO = Source("Shop", "shop.example", None, "https://shop.example/b")


@pytest.fixture
def quick_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The `max_results` each quick backend call was made with."""
    seen: list[int] = []

    def backend(ask: Any, *, timeout_s: float) -> list[Hit]:  # noqa: ANN401, ARG001
        seen.append(ask.count)
        return [Hit("T", "https://a.example", f"about {ask.query}")]

    monkeypatch.setattr(web_search_tool, "_ddgs_search_backend", backend)
    return seen


def test_the_menu_makes_depth_required_and_says_when_to_use_which(tmp_path: Path) -> None:
    """Depth is a required enum, and the description says what each depth costs and returns."""
    tool = _web_search(tmp_path)
    schema = tool.input_schema
    assert schema["required"] == ["query", "depth"]
    assert schema["properties"]["depth"]["enum"] == ["quick", "deep"]
    assert "about half a minute" in schema["properties"]["depth"]["description"]
    assert "recommendation" in schema["properties"]["depth"]["description"]


def test_deep_returns_the_brief_its_dated_sources_and_how_it_was_made(tmp_path: Path) -> None:
    """The brief, a row per source with site and date, and a first line she can say."""
    deep = _Deep(DeepFindings("A is cheaper than B.", (ONE, TWO), (ONE, TWO), 7))
    result = _call(_web_search(tmp_path, deep), {"query": "A vs B", "depth": "deep"})
    assert next(iter(result)) == "summary"
    assert result["brief"] == "A is cheaper than B."
    assert result["sources"] == [
        {"title": "Review of A and B", "site": "review.example", "date": "2026-10-20",
         "url": "https://www.review.example/ab"},
        {"title": "Shop", "site": "shop.example", "url": "https://shop.example/b"},
    ]
    assert result["depth"] == "deep"
    assert result["searches"] == 7
    assert "unfinished" not in result
    line = result["summary"]
    assert "2 sources" in line
    assert "newest yesterday (Tue)" in line
    assert "7 searches" in line
    assert "under your answer" in line
    assert deep.asked[0][0] == "A vs B"
    assert result.receipt.status == "done"
    assert result.receipt.reads_only is True
    assert result.receipt.covered is not None
    assert result.receipt.covered.sources == ("anthropic",)
    assert result.receipt.covered.shown == 2


def test_the_deep_search_is_told_roughly_where_he_is_from_the_calls_here(tmp_path: Path) -> None:
    """The search gets the call's coarse place and the call's clock; no Here, no place."""
    deep = _Deep(DeepFindings("brief", (), (), 1))
    tool = _web_search(tmp_path, deep)
    _call(tool, {"query": "q", "depth": "deep"}, here=FixedHere(coarse=Coarse(49.28, -123.12)))
    _call(tool, {"query": "q", "depth": "deep"})
    assert deep.asked[0][1] == Coarse(49.28, -123.12)
    assert deep.asked[1][1] is None
    assert deep.asked[0][2].now() == NOW


def test_a_brief_with_no_source_says_so(tmp_path: Path) -> None:
    """A brief that named no page is not given a list it does not have."""
    deep = _Deep(DeepFindings("brief", (), (ONE,), 3))
    result = _call(_web_search(tmp_path, deep), {"query": "q", "depth": "deep"})
    assert result["sources"] == []
    assert "no source named" in result["summary"]
    assert "under your answer" not in result["summary"]


def test_a_brief_longer_than_a_result_is_cut_at_a_paragraph_and_says_how_much(
    tmp_path: Path,
) -> None:
    """The brief gives way to what its sources take: whole paragraphs, and the rest counted."""
    paragraphs = [f"Finding {i}: " + " ".join(["word"] * 120) for i in range(12)]
    sources = tuple(
        Source(f"Page {i} with a title", f"site{i}.example", "2026-10-1" + str(i % 10),
               f"https://site{i}.example/article/{i}")
        for i in range(10)
    )
    deep = _Deep(DeepFindings("\n\n".join(paragraphs), sources, sources, 12))
    result = _call(_web_search(tmp_path, deep), {"query": "q", "depth": "deep"})
    assert len(json.dumps(dict(result), ensure_ascii=False)) <= 4_000
    shown = result["brief"].split("\n\n")
    assert 0 < len(shown) < len(paragraphs)
    assert shown == paragraphs[: len(shown)]
    assert re.search(r"cut after \d[\d,]* characters; \d+ more parts left out", result["summary"])


def test_a_brief_that_hit_the_length_limit_is_unfinished(tmp_path: Path) -> None:
    """The model's own length limit is said, and the result is marked unfinished."""
    deep = _Deep(DeepFindings("brief", (ONE,), (ONE,), 2, "length"))
    result = _call(_web_search(tmp_path, deep), {"query": "q", "depth": "deep"})
    assert result["unfinished"] is True
    assert "length limit" in result["summary"]


def test_a_search_that_ran_out_of_its_minute_hands_over_the_pages_it_found(
    tmp_path: Path,
) -> None:
    """No brief but pages: they come back as unfinished, with site and date, not as an error."""
    deep = _Deep(DeepFindings("", (), (ONE, TWO), 4, "minute"))
    result = _call(_web_search(tmp_path, deep), {"query": "A vs B", "depth": "deep"})
    assert result["unfinished"] is True
    assert "brief" not in result
    assert result["pages"] == [one.row() for one in (ONE, TWO)]
    assert "stopped at a minute before writing its brief" in result["summary"]
    assert "2 pages found" in result["summary"]
    assert "under your answer" in result["summary"]
    assert result.receipt.status == "done"


def test_a_failed_search_with_pages_says_it_failed_not_that_a_minute_passed(
    tmp_path: Path,
) -> None:
    """The reason is the true one."""
    deep = _Deep(DeepFindings("", (), (ONE,), 1, "failed"))
    result = _call(_web_search(tmp_path, deep), {"query": "q", "depth": "deep"})
    assert "failed before writing its brief" in result["summary"]
    assert "minute" not in result["summary"]


def test_unfinished_pages_are_cut_whole_to_fit_and_the_rest_counted(tmp_path: Path) -> None:
    """A long list of pages keeps whole rows and says how many it left out."""
    pages = tuple(
        Source("T" * 60, f"site{i}.example", "2026-10-19", f"https://site{i}.example/" + "p" * 80)
        for i in range(40)
    )
    deep = _Deep(DeepFindings("", (), pages, 9, "minute"))
    result = _call(_web_search(tmp_path, deep), {"query": "q", "depth": "deep"})
    assert len(json.dumps(dict(result), ensure_ascii=False)) <= 4_000
    assert 0 < len(result["pages"]) < 40
    assert f"{40 - len(result['pages'])} more left out" in result["summary"]
    assert result.receipt.covered is not None
    assert result.receipt.covered.more == 40 - len(result["pages"])


def test_a_search_that_found_no_page_in_its_minute_is_an_error_that_points_at_quick(
    tmp_path: Path,
) -> None:
    """Nothing to hand over: an error of kind dont_retry that names the quick search."""
    deep = _Deep(DeepFindings("", (), (), 0, "minute"))
    with pytest.raises(ToolError) as caught:
        _call(_web_search(tmp_path, deep), {"query": "q", "depth": "deep"})
    assert caught.value.kind == "dont_retry"
    assert "quick" in str(caught.value)


def test_a_failed_deep_search_names_the_type_not_the_error_text(tmp_path: Path) -> None:
    """The failure names the exception type only, never text that could carry a key."""
    with pytest.raises(ToolError) as caught:
        _call(_web_search(tmp_path, _Deep()), {"query": "q", "depth": "deep"})
    assert caught.value.code == "web_search_backend_error"
    text = str(caught.value)
    assert "deep search failed (RuntimeError)" in text
    assert "quick" in text
    assert "sk-ant" not in text


def test_deep_without_a_backend_runs_the_widest_quick_search(
    tmp_path: Path, quick_calls: list[int],
) -> None:
    """No deep backend: the quick backend answers at the 8-result ceiling and says quick."""
    result = _call(_web_search(tmp_path), {"query": "q", "depth": "deep", "max_results": 2})
    assert quick_calls == [8]
    assert result["depth"] == "quick"
    assert result["provider"] == "ddgs"
    assert result["results"] == [{
        "title": "T", "site": "a.example", "date": "date unknown", "excerpt": "about q",
        "url": "https://a.example",
    }]


@pytest.mark.parametrize("args", [{"query": "q"}, {"query": "q", "depth": "thorough"}])
def test_a_missing_or_unknown_depth_is_quick(
    tmp_path: Path, quick_calls: list[int], args: dict[str, Any],
) -> None:
    """A call that names no valid depth is quick; the deep backend is not asked."""
    deep = _Deep()
    result = _call(_web_search(tmp_path, deep), args)
    assert deep.asked == []
    assert quick_calls == [3]
    assert result["depth"] == "quick"


def test_quick_stays_quick_even_with_a_deep_backend(
    tmp_path: Path, quick_calls: list[int],
) -> None:
    """Quick never reaches the deep backend."""
    deep = _Deep()
    result = _call(_web_search(tmp_path, deep), {"query": "q", "depth": "quick"})
    assert deep.asked == []
    assert quick_calls == [3]
    assert result["depth"] == "quick"
