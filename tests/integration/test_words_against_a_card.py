"""ADR 0321: words the yes/no grammar does not match are read against the waiting card.

The real ``decide()`` loop, dispatcher, confirmation outbox and event log run against a real stdio
MCP server that stands in for a smart-home bridge. The answering model is scripted, and so is the
fast preset that reads his words. Asserts on the rows a turn leaves, the request body the fast
preset was sent, and what she says.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from jarvis.execution.mcp_tools import McpServers
from tests.support import consent
from tests.support.model import Failure, ScriptedModel, tool_call
from tests.support.plugin_approval import logged_dicts, press, say

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from jarvis.decision import DecideContext

ANSWER = "客厅、卧室和厨房的灯都关了。"
TALK = "好的，你想怎么改？"  # noqa: RUF001


@pytest.fixture
def servers() -> Iterator[McpServers]:
    """One loop thread with the stand-in's tools on the menu; stopped after the test."""
    mcp = McpServers(timeout_s=20, always_loaded=consent.LIGHTS_TOOLS)
    yield mcp
    mcp.stop()


def _verdicts(ctx: DecideContext) -> list[str]:
    return [r["verdict"] for r in logged_dicts(ctx.conn, "confirmation.words_classified")]



def test_a_yes_the_grammar_matches_runs_the_card_with_no_classifier_request(
    tmp_path: Path, servers: McpServers,
) -> None:
    """ADR 0012 D6: the exact-sentence grammar answers first and the fast preset is not asked."""
    words = ScriptedModel()
    llm = ScriptedModel(consent.all_off(), ANSWER)
    ctx, _ = consent.asked(tmp_path, servers, llm, words)
    try:
        assert say(ctx, "是", "T2") == ANSWER
        assert words.requests == []
        (accepted,) = logged_dicts(ctx.conn, "confirmation.accepted")
        assert accepted["grammar_rule_id"] not in {"words_classifier", "card_button"}
        assert logged_dicts(ctx.conn, "confirmation.words_classified") == []
        assert len(logged_dicts(ctx.conn, "action.result_observed")) == 3
    finally:
        ctx.conn.close()


def test_words_the_grammar_does_not_match_are_read_against_the_card_and_accept_runs_it(
    tmp_path: Path, servers: McpServers,
) -> None:
    """The fast preset sees his words and the card's lines only; its accept mints the leases."""
    words = ScriptedModel(tool_call("answer", {"verdict": "accept"}))
    words.active_preset = "haiku"
    llm = ScriptedModel(consent.all_off(), ANSWER)
    ctx, _ = consent.asked(tmp_path, servers, llm, words)
    try:
        (card,) = logged_dicts(ctx.conn, "confirmation.requested")
        assert say(ctx, "都关掉快点", "T2") == ANSWER
        # It saw his words and the card's lines, and nothing of the conversation.
        (request,) = words.requests
        (message,) = request.messages
        assert message["role"] == "user"
        assert "都关掉快点" in message["content"]
        assert all(line in message["content"] for line in card["template_line"].split("\n"))
        assert "stub system prompt" not in request.system
        assert (request.tool_choice, [t["name"] for t in request.tools or []]) == (
            "required", ["answer"],
        )
        (read,) = logged_dicts(ctx.conn, "confirmation.words_classified")
        assert (read["confirmation_id"], read["verdict"], read["preset"]) == (
            card["confirmation_id"], "accept", "haiku",
        )
        assert read["latency_ms"] >= 0
        assert "error" not in read
        (accepted,) = logged_dicts(ctx.conn, "confirmation.accepted")
        assert accepted["grammar_rule_id"] == "words_classifier"
        assert len(logged_dicts(ctx.conn, "action.result_observed")) == 3
        # The arguments that ran are the frozen card's, not his words'.
        observed = logged_dicts(ctx.conn, "action.result_observed")
        ran = [json.loads(r["tool_output"]) for r in observed]
        assert [r["light"] for r in ran] == list(consent.ROOMS)
    finally:
        ctx.conn.close()


def test_a_reject_reading_dismisses_the_card(tmp_path: Path, servers: McpServers) -> None:
    """A reject reading answers the card like the close button."""
    words = ScriptedModel(tool_call("answer", {"verdict": "reject"}))
    ctx, _ = consent.asked(tmp_path, servers, ScriptedModel(consent.all_off()), words)
    try:
        assert say(ctx, "算了别关了", "T2") == "好，这几件都不做了。"  # noqa: RUF001
        assert _verdicts(ctx) == ["reject"]
        assert len(logged_dicts(ctx.conn, "confirmation.rejected")) == 1
        assert logged_dicts(ctx.conn, "action.result_observed") == []
    finally:
        ctx.conn.close()


@pytest.mark.parametrize("verdict", ["change", "unrelated", "not_sure"])
def test_a_reading_that_is_not_an_answer_runs_his_words_as_an_ordinary_turn_and_the_card_stays(
    tmp_path: Path, servers: McpServers, verdict: str,
) -> None:
    """Change, unrelated and not sure answer nothing: his words run as an ordinary turn."""
    words = ScriptedModel(tool_call("answer", {"verdict": verdict}))
    llm = ScriptedModel(consent.all_off(), TALK)
    ctx, _ = consent.asked(tmp_path, servers, llm, words)
    try:
        assert say(ctx, "只关客厅的", "T2") == TALK
        assert _verdicts(ctx) == [verdict]
        assert len(llm.requests) == 2  # his words reached the conversation model
        assert "A card is waiting for the user's button" in json.dumps(llm.requests[1].messages)
        assert logged_dicts(ctx.conn, "confirmation.accepted") == []
        assert logged_dicts(ctx.conn, "confirmation.rejected") == []
        assert logged_dicts(ctx.conn, "action.result_observed") == []
        slot = consent.waiting_card(ctx)
        assert slot is not None
        assert slot.state == "pending"
    finally:
        ctx.conn.close()


def test_a_classifier_that_fails_or_times_out_leaves_the_card_waiting(
    tmp_path: Path, servers: McpServers,
) -> None:
    """A failed reading is an event with its error, and the card keeps waiting for its button."""
    words = ScriptedModel(Failure(TimeoutError("4 s")))
    llm = ScriptedModel(consent.all_off(), TALK)
    ctx, _ = consent.asked(tmp_path, servers, llm, words)
    try:
        assert say(ctx, "都关掉快点", "T2") == TALK
        (read,) = logged_dicts(ctx.conn, "confirmation.words_classified")
        assert (read["verdict"], read["error"]) == ("not_sure", "TimeoutError")
        assert logged_dicts(ctx.conn, "confirmation.accepted") == []
        slot = consent.waiting_card(ctx)
        assert slot is not None
        assert slot.state == "pending"
        card = slot.confirmation_id
        # His button still runs it.
        llm.script(ANSWER)
        assert press(ctx, {"confirmation_id": card, "decision": "accept"}, "T3") == ANSWER
    finally:
        ctx.conn.close()


def test_a_late_yes_after_another_utterance_does_not_answer_the_card(
    tmp_path: Path, servers: McpServers,
) -> None:
    """ADR 0039: only the first utterance after the card is read against it, by grammar or model."""
    words = ScriptedModel(tool_call("answer", {"verdict": "unrelated"}))
    llm = ScriptedModel(consent.all_off(), "现在是下午三点。", "好的。")
    ctx, _ = consent.asked(tmp_path, servers, llm, words)
    try:
        say(ctx, "现在几点", "T2")
        assert say(ctx, "好", "T3") == "好的。"
        assert len(words.requests) == 1  # the second utterance was never read against the card
        assert logged_dicts(ctx.conn, "confirmation.accepted") == []
        assert logged_dicts(ctx.conn, "action.result_observed") == []
        slot = consent.waiting_card(ctx)
        assert slot is not None
        assert slot.state == "pending"
    finally:
        ctx.conn.close()
