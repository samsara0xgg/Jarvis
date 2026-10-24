"""ADR 0359: the pages a deep search rests on go under the written part of her answer.

The real ``decide()`` loop, dispatcher, `web_search` handler and event log run on a scripted
model, over the batch loop and over the spoken stream (plain and structured). Only the deep
search itself is a fake that hands back what it was given. What is asserted is the answer as it
ends: its voice, its written part and whether that part is the whole answer, and what was spoken.
"""

# ruff: noqa: RUF001 — a Chinese answer is written with fullwidth punctuation.
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision import DecideResult, decide
from jarvis.decision.stream_envelope import split_envelope
from jarvis.execution.tools import Tool, build_default_registry, tool
from jarvis.execution.web_search_tool import DeepFindings, Source
from jarvis.shared import CallerPrincipal
from jarvis.state.event_log import emit_event, open_event_log
from tests.support.model import Call, Reply, ScriptedModel, tool_call
from tests.support.spoken_structured import structured_reply
from tests.support.turn_guards import guard_context, segments, spoken_turn

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from jarvis.shared.clock import Clock
    from jarvis.shared.here import Coarse

ONE = Source("Review of A and B", "review.example", "2026-10-20", "https://www.review.example/ab")
TWO = Source("Shop", "shop.example", None, "https://shop.example/b(1)")
THREE = Source("A | B <tests>", "tests.example", "2026-10-18", "https://tests.example/a-vs-b")
FOUND = DeepFindings("A is cheaper than B.", (ONE, TWO), (ONE, TWO), 7)
SEARCH = {"query": "A vs B", "depth": "deep"}
ANSWER = "A 比 B 便宜一些。"
FIRST_ROW = "- [Review of A and B](https://www.review.example/ab) · review.example · 2026-10-20"
SECOND_ROW = "- [Shop](https://shop.example/b%281%29) · shop.example"


class _Deep:
    """A deep backend that answers each search with the findings it was given, in turn."""

    def __init__(self, *findings: DeepFindings) -> None:
        self.findings = list(findings)

    def research(self, query: str, *, where: Coarse | None, clock: Clock) -> DeepFindings:  # noqa: ARG002
        return self.findings.pop(0) if len(self.findings) > 1 else self.findings[0]


@tool(
    description="Book a table.",
    input_schema={"type": "object", "properties": {}},
    allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
    risk_level="L3",
    read_only=False,
    requires_confirmation=True,
)
def book_table(args: Any, ctx: Any) -> dict[str, Any]:  # noqa: ANN401, ARG001
    """A call that needs a card, so a turn can end on one."""
    return {"booked": True}


@dataclass
class Route:
    """One way a turn runs: the batch loop, or the spoken stream plain or structured."""

    name: str
    tmp_path: Path

    def run(
        self, model: ScriptedModel, *findings: DeepFindings, words: str = "A 和 B 哪个便宜",
        tools: tuple[Tool, ...] = (),
    ) -> tuple[DecideResult, sqlite3.Connection]:
        """The turn's result and its log (the caller closes it)."""
        registry = build_default_registry(
            obsidian_vault_root=self.tmp_path, web_search_provider="ddgs",
            web_deep_search=_Deep(*(findings or (FOUND,))),
        )
        for extra in tools:
            registry.register(extra)
        if self.name == "text":
            conn = open_event_log(self.tmp_path / "events.db")
            trigger = emit_event(
                conn, type="surface.user_intent",
                payload={"transcript": words, "turn_id": "T_text"},
                correlation={"turn_id": "T_text"},
            )
            return decide(
                trigger, guard_context(conn, self.tmp_path, model, 8, registry=registry),
            ), conn
        return spoken_turn(
            self.tmp_path, model, structured=self.name == "structured", registry=registry,
            words=words,
        )

    def say(self, voice: str, document: str = "") -> str:
        """A reply as this route's model writes it: the voice and, if any, the written part."""
        if self.name == "structured":
            return structured_reply(voice, document, ascii_only=False)
        if document:
            return f"<claims></claims><voice>{voice}</voice><document>{document}</document>"
        return f"<claims></claims>{voice}"


@pytest.fixture(params=["text", "spoken", "structured"])
def route(request: pytest.FixtureRequest, tmp_path: Path) -> Route:
    """Each of the three routes in turn."""
    return Route(request.param, tmp_path)


def _text(result: DecideResult) -> str:
    assert result.response_plan is not None
    return result.response_plan.text


def test_an_answer_with_no_written_part_gets_the_pages_as_its_written_part(route: Route) -> None:
    """Her words stay the voice; the pages are the written part, which the voice leaves out."""
    model = ScriptedModel(tool_call("web_search", SEARCH), Reply(route.say(ANSWER), chunk=4))
    result, conn = route.run(model)
    try:
        voice, document, enveloped = split_envelope(_text(result))
        assert enveloped
        assert voice == ANSWER
        assert document == f"## 来源\n{FIRST_ROW}\n{SECOND_ROW}"
        assert result.written_apart is True
        if route.name != "text":
            assert segments(conn) == ANSWER  # what was said never had the pages in it
    finally:
        conn.close()


def test_the_heading_is_in_the_language_of_his_words(route: Route) -> None:
    """English words, an English heading."""
    model = ScriptedModel(
        tool_call("web_search", SEARCH), Reply(route.say("A is cheaper."), chunk=4),
    )
    result, conn = route.run(model, words="Which is cheaper, A or B?")
    try:
        _, document, _ = split_envelope(_text(result))
        assert document.splitlines()[0] == "## Sources"
    finally:
        conn.close()


def test_an_answer_with_its_own_written_part_gets_the_pages_at_its_end(route: Route) -> None:
    """The pages follow what she wrote on screen, after a blank line; the voice is as it was."""
    written = "## 对比\n- A：便宜\n- B：贵一些"
    model = ScriptedModel(
        tool_call("web_search", SEARCH), Reply(route.say(ANSWER, written), chunk=4),
    )
    result, conn = route.run(model)
    try:
        voice, document, _ = split_envelope(_text(result))
        assert voice == ANSWER
        assert document == f"{written}\n\n## 来源\n{FIRST_ROW}\n{SECOND_ROW}"
        assert result.written_apart is (route.name == "structured")
        if route.name != "text":
            assert segments(conn) == ANSWER
    finally:
        conn.close()


def test_a_search_that_rests_on_no_page_adds_nothing(route: Route) -> None:
    """A brief that named no source leaves her answer exactly as she wrote it."""
    model = ScriptedModel(tool_call("web_search", SEARCH), Reply(route.say(ANSWER), chunk=4))
    result, conn = route.run(model, DeepFindings("A is cheaper.", (), (ONE,), 3))
    try:
        assert _text(result) == ANSWER
        assert result.written_apart is False
    finally:
        conn.close()


def test_a_search_that_stopped_before_its_brief_lists_the_pages_it_found(route: Route) -> None:
    """The pages she could only name by title are the links he can open."""
    model = ScriptedModel(tool_call("web_search", SEARCH), Reply(route.say(ANSWER), chunk=4))
    result, conn = route.run(model, DeepFindings("", (), (ONE, TWO), 4, "minute"))
    try:
        _, document, _ = split_envelope(_text(result))
        assert document == f"## 来源\n{FIRST_ROW}\n{SECOND_ROW}"
    finally:
        conn.close()


def test_two_searches_in_a_turn_list_each_page_once_in_the_order_found(route: Route) -> None:
    """A page both searches rest on is listed once, where it was first found."""
    model = ScriptedModel(
        tool_call("web_search", SEARCH),
        tool_call("web_search", {"query": "A vs B reviews", "depth": "deep"}),
        Reply(route.say(ANSWER), chunk=4),
    )
    second = DeepFindings("B is slower.", (THREE, ONE), (THREE, ONE), 5)
    result, conn = route.run(model, FOUND, second)
    try:
        _, document, _ = split_envelope(_text(result))
        rows = document.splitlines()[1:]
        assert [row.split("](")[0] for row in rows] == [
            "- [Review of A and B", "- [Shop", "- [A | B tests",
        ]
        assert "<" not in document
    finally:
        conn.close()


def test_an_answer_the_turn_ends_on_a_card_for_has_no_pages(route: Route) -> None:
    """A card ends the turn: the ask is heard as it is and is not an answer the pages rest on."""
    model = ScriptedModel(
        Reply(calls=(Call("web_search", SEARCH), Call("book_table", {}))), "unused",
    )
    result, conn = route.run(model, tools=(book_table,))
    try:
        text = _text(result)
        assert conn.execute(
            "SELECT COUNT(*) FROM events WHERE type='confirmation.requested'",
        ).fetchone()[0] == 1
        assert "来源" not in text
        assert "review.example" not in text
        assert result.written_apart is False
    finally:
        conn.close()


def test_a_turn_with_no_deep_search_is_unchanged(route: Route) -> None:
    """No pages named, no change: her answer ends as the model wrote it."""
    model = ScriptedModel(Reply(route.say(ANSWER), chunk=4))
    result, conn = route.run(model)
    try:
        assert _text(result) == ANSWER
        assert result.written_apart is False
    finally:
        conn.close()


def test_an_answer_that_is_all_written_part_gets_the_pages_after_it(tmp_path: Path) -> None:
    """Nothing is spoken, so the unspoken answer is judged whole; the pages still end it."""
    model = ScriptedModel(
        tool_call("web_search", SEARCH),
        Reply("<claims></claims><document>A 比 B 便宜一些。</document>", chunk=4),
    )
    result, conn = Route("spoken", tmp_path).run(model)
    try:
        voice, document, _ = split_envelope(_text(result))
        assert voice == ""
        assert document == f"A 比 B 便宜一些。\n\n## 来源\n{FIRST_ROW}\n{SECOND_ROW}"
        assert segments(conn) == ""
        assert result.written_apart is False
    finally:
        conn.close()
