"""ADR 0033/0034/0062: a plugin tool is searched onto the menu, asks Allen, and runs on his yes.

Drives the real ``decide()`` loop, dispatcher, confirmation grammar and event log
(with production's atomic confirmation-dispatch outbox on) against the real echo
MCP server as a subprocess and a scripted model. Asserts on the tool lists the
model is offered, the consent line, and the ``confirmation.*`` /
``action.result_observed`` rows. A card's button is the ``surface.user_intent``
the desktop's ``POST /inherent/confirmation`` appends.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from jarvis.decision import (
    DecideContext,
    LifecycleLike,
    RuntimePathsLike,
    ToolRegistryLike,
    request_confirmation,
)
from jarvis.decision.confirm_grammar import load_confirm_grammar
from jarvis.execution.mcp_tools import McpServers
from jarvis.execution.tool_search import build_tool_search
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.shared import lang
from jarvis.shared.realtime import Wave1FeatureFlags
from jarvis.state.event_log import emit_event, open_event_log
from tests.support.model import Reply, ScriptedModel, tool_call
from tests.support.plugin_approval import Paths, logged_dicts, press, say

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from jarvis.decision.llm import LLMClient

HERE = Path(__file__).parent
REPO = HERE.parent.parent
ECHO = {"echo": {"command": sys.executable, "args": [str(HERE / "mcp_echo_server.py")]}}
ASK = "要执行 echo add（a：17，b：25）吗？"  # noqa: RUF001 — the Chinese ask under the card names its arguments (ADR 0321).
ANSWER = "算好了，和是 42。"  # noqa: RUF001 — her words after the card ran, from its real result.
LETTER = {"to": "allen@example.com", "subject": "周六见", "body": "周六早上八点停车场见。"}


TALK = "现在是下午三点。要我顺便把明天的日程也念一下吗？"  # noqa: RUF001
WITHDRAW = tool_call("dismiss_card", call_id="w")


def _model(
    query: str = "add two integers",
    call: tuple[str, Mapping[str, Any]] = ("add", {"a": 17, "b": 25}),
    *,
    withdraws: bool = False,
    then: tuple[str, ...] = (),
) -> ScriptedModel:
    """Searches for a tool, calls it, then answers each later request with the ``then`` texts.

    With ``withdraws``, the first call of each later turn (T2 and T4) takes the card down.
    """
    name, arguments = call
    steps: list[Reply | str] = [
        tool_call("tool_search", {"query": query}, call_id="c1"),
        tool_call(f"mcp__echo__{name}", arguments, call_id="c2"),
    ]
    if withdraws:
        steps += [WITHDRAW, TALK, WITHDRAW]
    return ScriptedModel(*steps, *then, otherwise=TALK)


def _offered(model: ScriptedModel) -> list[list[str]]:
    """The tool names each request offered the model."""
    return [[t["name"] for t in request.tools or []] for request in model.requests]


def _seen(model: ScriptedModel) -> list[str]:
    """What each request showed the model, as one JSON text."""
    return [json.dumps(request.messages, ensure_ascii=False) for request in model.requests]


@pytest.fixture
def servers() -> Iterator[McpServers]:
    """One loop thread; stopped after the test so the subprocess exits."""
    mcp = McpServers(timeout_s=20)
    yield mcp
    mcp.stop()


def _context(tmp_path: Path, servers: McpServers, llm: ScriptedModel) -> DecideContext:
    registry = build_default_registry(confirmation_dispatch_outbox=True)
    mcp_tools = servers.connect(ECHO)
    for one in (*mcp_tools, *build_tool_search(mcp_tools, {"echo": "Echo test server"})):
        registry.register(one)
    paths = Paths(event_log=tmp_path / "events.db", artifacts_root=tmp_path / "artifacts")
    paths.artifacts_root.mkdir(parents=True, exist_ok=True)
    return DecideContext(
        conn=open_event_log(paths.event_log),
        runtime_paths=cast("RuntimePathsLike", paths),
        tool_registry=cast("ToolRegistryLike", registry),
        lifecycle=cast("LifecycleLike", ActionLifecycle()),
        llm_client=cast("LLMClient", llm),
        system_prompt="stub system prompt",
        confirm_grammar_table=load_confirm_grammar(REPO / "config" / "confirm_grammar.yaml"),
        wave1_features=Wave1FeatureFlags(
            confirmation_dispatch_outbox=True,
        ),
    )


def _ask(
    tmp_path: Path,
    servers: McpServers,
    then: tuple[str, ...] = (),
    **context: Any,  # noqa: ANN401 — DecideContext fields
) -> tuple[DecideContext, ScriptedModel]:
    """Turn 1: the adder is not offered until searched, and calling it asks instead of running."""
    llm = _model(then=then)
    ctx = dataclasses.replace(_context(tmp_path, servers, llm), **context)
    assert say(ctx, "用 add 工具算 17 加 25", "T1") == ASK
    first, second = _offered(llm)
    assert "tool_search" in first
    assert not [name for name in first if name.startswith("mcp__")]
    assert "mcp__echo__add" in second
    # ADR 0056: the hit's server brings its read-only echo along; boom has no hint, so it stays off.
    assert "mcp__echo__echo" in second
    assert "mcp__echo__boom" not in second
    assert logged_dicts(ctx.conn, "confirmation.requested")[0]["template_line"] == ASK
    assert len(logged_dicts(ctx.conn, "action.result_observed")) == 1  # only the search ran
    return ctx, llm


def test_searched_plugin_tool_asks_then_runs_on_yes(tmp_path: Path, servers: McpServers) -> None:
    """Allen's 可以 re-proposes the frozen arguments through the outbox and the adder runs once.

    The yes is matched by grammar; the one request after it carries the real result, and her
    reply to it is the answer (ADR 0321).
    """
    ctx, llm = _ask(tmp_path, servers, then=(ANSWER,))
    try:
        assert say(ctx, "可以", "T2") == ANSWER
        assert len(llm.requests) == 3  # search, the call, and the answer after the result
        # The searched adder is on that request's menu, and she may call nothing more.
        assert llm.requests[2].tool_choice == "none"
        assert "mcp__echo__add" in [d["name"] for d in llm.requests[2].tools or ()]
        assert '\\"sum\\": 42' in _seen(llm)[2]
        assert len(logged_dicts(ctx.conn, "confirmation.accepted")) == 1
        added = [
            r
            for r in logged_dicts(ctx.conn, "action.proposed")
            if r["tool_name"] == "mcp__echo__add"
        ]
        assert [r["arguments"] for r in added] == [{"a": 17, "b": 25}, {"a": 17, "b": 25}]
        assert json.loads(logged_dicts(ctx.conn, "action.result_observed")[-1]["tool_output"]) == {
            "sum": 42
        }
    finally:
        ctx.conn.close()


@pytest.mark.parametrize(
    ("words", "ask", "answer"),
    [
        (("用 add 工具算 17 加 25", "可以"), ASK, ANSWER),
        (
            ("use the add tool to add 17 and 25", "yes"),
            "Shall I run echo add (a: 17, b: 25)?",
            "The sum is 42.",
        ),
    ],
)
def test_the_ask_follows_the_words_not_the_system_language(
    tmp_path: Path, servers: McpServers, words: tuple[str, str], ask: str, answer: str
) -> None:
    """System language en: the consent question names the arguments in the language of his words."""
    lang.set_language("en")
    ctx = _context(tmp_path, servers, _model(then=(answer,)))
    try:
        assert say(ctx, words[0], "T1") == ask
        assert logged_dicts(ctx.conn, "confirmation.requested")[0]["template_line"] == ask
        assert say(ctx, words[1], "T2") == answer
    finally:
        ctx.conn.close()


def test_searched_plugin_tool_does_nothing_on_no(tmp_path: Path, servers: McpServers) -> None:
    """Allen's 不要 closes the ask; the adder never runs."""
    ctx, _ = _ask(tmp_path, servers)
    try:
        assert say(ctx, "不要", "T2") == "好，不执行 echo add了。"  # noqa: RUF001 — the fixed Chinese rejection.
        assert len(logged_dicts(ctx.conn, "confirmation.rejected")) == 1
        assert len(logged_dicts(ctx.conn, "action.result_observed")) == 1  # still only the search
    finally:
        ctx.conn.close()


def test_an_unrelated_turn_leaves_the_card_to_its_button(
    tmp_path: Path, servers: McpServers
) -> None:
    """ADR 0062: a 好 meant for the model's own question never fires the card; its button does."""
    ctx, llm = _ask(tmp_path, servers, then=(TALK, TALK, ANSWER))
    try:
        say(ctx, "现在几点", "T2")
        assert "A card is waiting for the user's button: mcp__echo__add" in _seen(llm)[-1]
        assert logged_dicts(ctx.conn, "confirmation.rejected") == []  # the card still waits
        say(ctx, "好", "T3")
        assert len(llm.requests) == 4  # the 好 reached the model, not the grammar
        assert logged_dicts(ctx.conn, "confirmation.accepted") == []
        assert len(logged_dicts(ctx.conn, "action.result_observed")) == 1  # still only the search
        card = logged_dicts(ctx.conn, "confirmation.requested")[0]["confirmation_id"]
        pressed = press(ctx, {"confirmation_id": card, "decision": "accept"}, "T4")
        assert pressed == ANSWER
        assert len(llm.requests) == 5  # the button's only request is the answer after the result
        assert [r["grammar_rule_id"] for r in logged_dicts(ctx.conn, "confirmation.accepted")] == [
            "card_button"
        ]
        assert json.loads(logged_dicts(ctx.conn, "action.result_observed")[-1]["tool_output"]) == {
            "sum": 42
        }
        # Pressed again, the card is gone: nothing runs twice.
        assert press(ctx, {"confirmation_id": card, "decision": "accept"}, "T5") == (
            "这张卡已经处理过或被换掉了，没有执行。"  # noqa: RUF001 — the fixed Chinese stale line.
        )
        assert len(logged_dicts(ctx.conn, "action.result_observed")) == 2
    finally:
        ctx.conn.close()


def test_a_card_pressed_after_its_words_window_still_sends(
    tmp_path: Path, servers: McpServers
) -> None:
    """ADR 0062: ``confirmation.ttl_ms`` bounds only a spoken yes; a card's button never expires."""
    ctx, llm = _ask(  # the words window is shut at the ask
        tmp_path, servers, then=(TALK, ANSWER), confirmation_ttl_ms=0,
    )
    try:
        card = logged_dicts(ctx.conn, "confirmation.requested")[0]["confirmation_id"]
        assert say(ctx, "可以", "T2") == TALK  # the yes is not a button
        assert logged_dicts(ctx.conn, "confirmation.accepted") == []
        pressed = press(ctx, {"confirmation_id": card, "decision": "accept"}, "T3")
        assert pressed == ANSWER
        assert [r["grammar_rule_id"] for r in logged_dicts(ctx.conn, "confirmation.accepted")] == [
            "card_button"
        ]
        assert json.loads(logged_dicts(ctx.conn, "action.result_observed")[-1]["tool_output"]) == {
            "sum": 42
        }
        assert len(llm.requests) == 4  # the yes reached the model; the button's is the answer
    finally:
        ctx.conn.close()


def test_a_letter_card_sends_what_allen_edited(tmp_path: Path, servers: McpServers) -> None:
    """ADR 0062: the ask names the letter; send with an edited body re-freezes it, then runs it."""
    llm = _model(query="send a letter", call=("send", LETTER), then=(ANSWER,))
    ctx = _context(tmp_path, servers, llm)
    try:
        ask = say(ctx, "给我自己发一封信", "T1")
        assert ask == "信写好了，发给 allen@example.com，主题「周六见」。要发吗？"  # noqa: RUF001
        card = logged_dicts(ctx.conn, "confirmation.requested")[0]["confirmation_id"]
        edits = {"body": "周六早上九点停车场见。", "to": 42, "cc": "someone@else"}
        pressed = press(ctx, {"confirmation_id": card, "decision": "accept", "edits": edits}, "T2")
        assert pressed == ANSWER
        edited = {**LETTER, "body": "周六早上九点停车场见。"}
        # Only the existing string field changed; the new snapshot is exactly what ran.
        asked = logged_dicts(ctx.conn, "confirmation.requested")
        assert [r["calls"][0]["args_meta"] for r in asked] == [LETTER, edited]
        assert (
            logged_dicts(ctx.conn, "confirmation.accepted")[0]["confirmation_id"]
            == asked[1]["confirmation_id"]
        )
        proposed = logged_dicts(ctx.conn, "action.proposed")
        sent = [r for r in proposed if r["tool_name"] == "mcp__echo__send"]
        assert sent[-1]["arguments"] == edited
        assert (
            json.loads(logged_dicts(ctx.conn, "action.result_observed")[-1]["tool_output"])
            == edited
        )
    finally:
        ctx.conn.close()


def test_the_cards_x_dismisses_it(tmp_path: Path, servers: McpServers) -> None:
    """ADR 0062: the card's close button is a rejection bound to its id; nothing runs."""
    ctx, _ = _ask(tmp_path, servers)
    try:
        card = logged_dicts(ctx.conn, "confirmation.requested")[0]["confirmation_id"]
        assert press(ctx, {"confirmation_id": card, "decision": "reject"}, "T2") == (
            "好，不执行 echo add了。"  # noqa: RUF001 — the fixed Chinese rejection.
        )
        assert [r["grammar_rule_id"] for r in logged_dicts(ctx.conn, "confirmation.rejected")] == [
            "card_dismiss"
        ]
        assert len(logged_dicts(ctx.conn, "action.result_observed")) == 1  # still only the search
    finally:
        ctx.conn.close()


def test_the_model_takes_the_card_down_when_allen_says_not_to_send(
    tmp_path: Path, servers: McpServers
) -> None:
    """ADR 0075: 撤销吧 rejects the card as ``card_withdrawn``; its button then runs nothing.

    Asked again with no card waiting, the result says nothing was taken down and is no error.
    """
    llm = _model(withdraws=True)
    ctx = _context(tmp_path, servers, llm)
    try:
        assert say(ctx, "用 add 工具算 17 加 25", "T1") == ASK
        card = logged_dicts(ctx.conn, "confirmation.requested")[0]["confirmation_id"]
        say(ctx, "OK撤销吧，不发了。", "T2")  # noqa: RUF001 — Allen's words, 2026-09-26 23:33
        assert "To take it down without running it, call dismiss_card." in _seen(llm)[2]
        rejected = logged_dicts(ctx.conn, "confirmation.rejected")
        assert [(r["confirmation_id"], r["grammar_rule_id"]) for r in rejected] == [
            (card, "card_withdrawn"),
        ]
        assert json.loads(logged_dicts(ctx.conn, "action.result_observed")[-1]["tool_output"]) == {
            "summary": "Took down the mcp__echo__add confirmation card (not run).",
            "dismissed": ["the mcp__echo__add confirmation card (not run)"],
        }
        assert press(ctx, {"confirmation_id": card, "decision": "accept"}, "T3") == (
            "这张卡已经处理过或被换掉了，没有执行。"  # noqa: RUF001 — the fixed Chinese stale line.
        )
        say(ctx, "撤销吧", "T4")
        last = logged_dicts(ctx.conn, "action.result_observed")[-1]
        assert "error" not in last  # nothing waiting is not a failure to retry
        assert json.loads(last["tool_output"]) == {
            "summary": "No card is waiting; nothing to do.",
            "dismissed": [],
        }
        assert "A card is waiting" not in _seen(llm)[-1]
        proposed = logged_dicts(ctx.conn, "action.proposed")
        assert [r["tool_name"] for r in proposed].count("mcp__echo__add") == 1  # never run
    finally:
        ctx.conn.close()


def test_dismiss_card_takes_down_a_confirmation_and_a_question_card_together(
    tmp_path: Path, servers: McpServers
) -> None:
    """ADR 0247: with both cards up one call takes both down, and neither action runs."""
    ctx = _context(tmp_path, servers, _model(withdraws=True))
    try:
        assert say(ctx, "用 add 工具算 17 加 25", "T1") == ASK
        emit_event(
            ctx.conn,
            type="clarification.requested",
            payload={
                "clarification_id": "Q1",
                "question": "Which one?",
                "turn_id": "T1",
                "fields": [{"label": "which", "choices": ["a", "b"]}],
            },
        )
        say(ctx, "撤销吧", "T2")
        out = json.loads(logged_dicts(ctx.conn, "action.result_observed")[-1]["tool_output"])
        assert out["dismissed"] == [
            "the mcp__echo__add confirmation card (not run)",
            'the question card "Which one?"',
        ]
        assert [r["grammar_rule_id"] for r in logged_dicts(ctx.conn, "confirmation.rejected")] == [
            "card_withdrawn",
        ]
        assert len(logged_dicts(ctx.conn, "clarification.withdrawn")) == 1
    finally:
        ctx.conn.close()


def test_a_card_raised_without_a_turn_sends_on_his_button(
    tmp_path: Path, servers: McpServers
) -> None:
    """ADR 0147: the Dashboard's Send raises the letter card itself; only his button runs it."""
    llm = ScriptedModel(ANSWER)
    ctx = _context(tmp_path, servers, llm)
    try:
        for no_card in ("mcp__echo__echo", "mcp__echo__nothing"):  # read-only, unknown
            assert request_confirmation(ctx.conn, ctx.tool_registry, no_card, {}, ttl_ms=1) is None
        asked = request_confirmation(
            ctx.conn,
            ctx.tool_registry,
            "mcp__echo__send",
            LETTER,
            ttl_ms=60_000,
            lang="zh",
        )
        assert asked is not None
        assert logged_dicts(ctx.conn, "confirmation.requested")[0]["template_line"] == (
            "信写好了，发给 allen@example.com，主题「周六见」。要发吗？"  # noqa: RUF001
        )
        assert logged_dicts(ctx.conn, "action.proposed") == []  # nothing ran, no model asked
        assert press(ctx, {"confirmation_id": asked, "decision": "accept"}, "T1") == ANSWER
        assert len(llm.requests) == 1  # only the answer after the result
        assert len(logged_dicts(ctx.conn, "action.result_observed")) == 1
    finally:
        ctx.conn.close()
