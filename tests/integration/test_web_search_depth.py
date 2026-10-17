"""`web_search` depth: quick is the keyed backend, deep is one nested Anthropic web search.

The handler is driven for real over a fake backend and a fake `DeepSearch`; the deep
backend's parsing runs over a hand-built Anthropic response. Nothing reaches the network.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from jarvis.decision.llm_session import LLMSessionFactory
from jarvis.execution import tools
from jarvis.execution.tools import (
    DeepFindings,
    Tool,
    ToolContext,
    ToolError,
    build_default_registry,
)
from jarvis.runtime.deep_search import (
    DeepSearchError,
    build_deep_search,
    findings_from,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


def _web_search(tmp_path: Path, deep: object | None = None) -> Tool:
    registry = build_default_registry(
        obsidian_vault_root=tmp_path, web_search_provider="ddgs", web_search_max_results=3,
        web_deep_search=cast("Any", deep),
    )
    found = next(t for t in registry.get_definitions() if t.name == "web_search")
    assert isinstance(found, Tool)
    return found


def _call(tool: Tool, args: Mapping[str, Any]) -> dict[str, Any]:
    ctx = cast("ToolContext", SimpleNamespace(conn=None, action_id="A1"))
    return dict(tool.handler(args, ctx))


class _Deep:
    def __init__(self, findings: DeepFindings | None = None) -> None:
        self.findings = findings
        self.asked: list[str] = []

    def research(self, query: str) -> DeepFindings:
        self.asked.append(query)
        if self.findings is None:
            msg = "boom sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
            raise RuntimeError(msg)
        return self.findings


@pytest.fixture
def quick_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The `max_results` each quick backend call was made with."""
    seen: list[int] = []

    def backend(
        query: str, max_results: int, *, timeout_s: float,  # noqa: ARG001
    ) -> list[tuple[str, str, str]]:
        seen.append(max_results)
        return [("T", "https://a.example", f"about {query}")]

    monkeypatch.setattr(tools, "_ddgs_search_backend", backend)
    return seen


def test_the_menu_makes_depth_required_and_says_when_to_use_which(tmp_path: Path) -> None:
    """Depth is a required enum, and the description says what each depth costs and returns."""
    tool = _web_search(tmp_path)
    schema = tool.input_schema
    assert schema["required"] == ["query", "depth"]
    assert schema["properties"]["depth"]["enum"] == ["quick", "deep"]
    assert "about 20 seconds" in tool.description
    assert "recommendation" in tool.description


def test_deep_returns_the_summary_with_numbered_sources(tmp_path: Path) -> None:
    """A deep search returns the researched summary with its numbered sources."""
    deep = _Deep(
        DeepFindings(
            summary="A is cheaper than B.",
            sources=(("Review", "https://r.example"), ("Shop", "https://s.example")),
            searches=7,
            complete=True,
        ),
    )
    result = _call(_web_search(tmp_path, deep), {"query": "A vs B", "depth": "deep"})
    assert deep.asked == ["A vs B"]
    assert result == {
        "summary": "A is cheaper than B.",
        "sources": ["1. Review — https://r.example", "2. Shop — https://s.example"],
        "provider": "anthropic",
        "depth": "deep",
        "searches": 7,
    }


def test_an_unfinished_deep_search_says_so_and_a_long_summary_is_capped(tmp_path: Path) -> None:
    """An unfinished brief is flagged, and a summary past 8 KiB is cut with a marker."""
    deep = _Deep(DeepFindings(summary="字" * 20_000, sources=(), searches=2, complete=False))
    result = _call(_web_search(tmp_path, deep), {"query": "q", "depth": "deep"})
    assert result["incomplete"] is True
    assert len(result["summary"].encode()) <= 8192
    assert "truncated" in result["summary"]


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
    assert result["results"] == ["1. T — https://a.example — about q"]


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


# --- the deep backend's reading of Anthropic's response ----------------------------


def _result(raw: dict[str, Any], finish_reason: str | None = "end_turn") -> Any:  # noqa: ANN401
    return SimpleNamespace(raw=raw, finish_reason=finish_reason)


def _page(url: str, title: str) -> dict[str, Any]:
    return {"type": "web_search_result", "url": url, "title": title}


def _cite(url: str, title: str) -> dict[str, Any]:
    return {"type": "web_search_result_location", "url": url, "title": title}


def _transcript() -> dict[str, Any]:
    return {
        "content": [
            {"type": "thinking", "thinking": "plan"},
            {"type": "text", "text": "r is a string; fetch directly."},
            {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {"query": "x"}},
            {
                "type": "web_search_tool_result", "tool_use_id": "s1",
                "content": [
                    _page("https://one.example", "One"), _page("https://two.example", "Two"),
                ],
            },
            {"type": "text", "text": "Checking prices next."},
            {"type": "server_tool_use", "id": "s2", "name": "web_search", "input": {"query": "y"}},
            {
                "type": "web_search_tool_result", "tool_use_id": "s2",
                "content": [
                    _page("https://two.example", "Two again"),
                    _page("https://three.example", "Three"),
                ],
            },
            {"type": "thinking", "thinking": "write it"},
            {
                "type": "text", "text": "A costs 10 yuan. ",
                "citations": [
                    *[_cite("https://three.example", "Three")] * 2,
                ],
            },
            {"type": "text", "text": "B costs 12."},
        ],
        "usage": {"server_tool_use": {"web_search_requests": 2}},
        "stop_reason": "end_turn",
    }


def test_only_the_brief_after_the_last_search_is_the_summary() -> None:
    """Notes before and between searches are scratch work; only the closing text is the answer."""
    findings = findings_from(_result(_transcript()))
    assert findings.summary == "A costs 10 yuan. B costs 12."
    assert "fetch directly" not in findings.summary
    assert "Checking prices" not in findings.summary
    assert findings.complete is True


def test_sources_are_cited_pages_first_then_searched_pages_without_repeats() -> None:
    """Cited pages lead, then every page the searches returned, one row per url."""
    findings = findings_from(_result(_transcript()))
    assert findings.sources == (
        ("Three", "https://three.example"),
        ("One", "https://one.example"),
        ("Two", "https://two.example"),
    )


def test_sources_are_capped_at_ten() -> None:
    """Ten sources at most."""
    raw = _transcript()
    raw["content"].insert(
        4,
        {
            "type": "web_search_tool_result", "tool_use_id": "s1",
            "content": [_page(f"https://p{i}.example", f"P{i}") for i in range(30)],
        },
    )
    assert len(findings_from(_result(raw)).sources) == 10


def test_a_code_execution_result_also_ends_the_notes() -> None:
    """A code execution result is a tool block too: the text before it is not the brief."""
    raw = _transcript()
    raw["content"].insert(-1, {"type": "code_execution_tool_result", "content": {}})
    assert findings_from(_result(raw)).summary == "B costs 12."


def test_searches_come_from_usage_else_from_the_search_blocks() -> None:
    """The search count is the provider's own, else the search blocks counted."""
    raw = _transcript()
    raw["usage"]["server_tool_use"]["web_search_requests"] = 5
    assert findings_from(_result(raw)).searches == 5
    del raw["usage"]
    assert findings_from(_result(raw)).searches == 2


@pytest.mark.parametrize("stop", ["pause_turn", "max_tokens"])
def test_a_provider_that_stopped_early_is_incomplete(stop: str) -> None:
    """A pause or a token limit means the brief may be unfinished."""
    assert findings_from(_result(_transcript(), stop)).complete is False


def test_no_text_after_the_last_search_is_an_error() -> None:
    """A transcript that ends on a search has no brief: that is an error, not an empty answer."""
    raw = _transcript()
    raw["content"] = raw["content"][:7]
    with pytest.raises(DeepSearchError):
        findings_from(_result(raw))


# --- building it --------------------------------------------------------------------


def _config(preset: Mapping[str, Any] | None, deep_preset: str = "sonnet") -> dict[str, Any]:
    return {
        "llm": {"presets": {} if preset is None else {"sonnet": dict(preset)}},
        "tools": {"web": {"deep_preset": deep_preset}},
    }


_ANTHROPIC = {
    "provider": "anthropic", "model": "claude-sonnet-5-5", "api_key_env": "ANTHROPIC_API_KEY",
    "max_tokens": 4096,
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


class _FakeClient:
    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw
        self.requests: list[dict[str, Any]] = []

    def chat(self, **request: Any) -> Any:  # noqa: ANN401
        self.requests.append(request)
        return _result(self.raw)


def test_research_sends_the_question_with_the_server_search_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One request: the question as the user turn, Anthropic's web search tool at max_uses."""
    client = _FakeClient(_transcript())
    config = _config(_ANTHROPIC)
    config["tools"]["web"]["deep_max_uses"] = 6
    deep = build_deep_search(config)
    assert deep is not None
    monkeypatch.setattr(LLMSessionFactory, "create", lambda *_a, **_k: client)
    findings = deep.research("A or B?")
    (request,) = client.requests
    assert request["messages"] == [{"role": "user", "content": "A or B?"}]
    assert request["tools"] == [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": 6},
    ]
    assert "language of the question" in request["system"]
    assert findings.summary == "A costs 10 yuan. B costs 12."
