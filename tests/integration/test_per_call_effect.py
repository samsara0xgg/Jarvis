"""ADR 0361: a tool decides each call's effect from its arguments and completes a card's call.

The real ``decide()`` loop, dispatcher and event log run over one tool whose look, note and send
differ, with a scripted model. Asserts on the rows a turn leaves, the card, the receipts and the
results the model was sent.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from jarvis.decision import CompletionRefused, request_confirmation
from jarvis.decision.policy import PolicyConsistencyError, validate_requires_confirmation
from jarvis.decision.tier0 import Tier0ConfigError, load_tier0_table, validate_tier0_table
from jarvis.execution.tools import build_default_registry
from jarvis.runtime.bootstrap.catalog import load_tables
from jarvis.runtime.config import RuntimeBootstrapError, parse_runtime_config
from jarvis.shared import CallerPrincipal
from jarvis.shared.receipts import call_asks, call_risk
from tests.support import call_effect
from tests.support.consent import tool_results
from tests.support.model import Call, Reply, ScriptedModel
from tests.support.plugin_approval import logged_dicts, press, say
from tests.support.tool_world import ToolWorld, tool_request

if TYPE_CHECKING:
    from pathlib import Path


def _one_call(**args: str) -> Reply:
    return Reply(calls=(Call("depot", args, "c0"),))


def test_a_read_runs_with_no_card(tmp_path: Path) -> None:
    """A look is L0: it runs, with a read's receipt and no card."""
    llm = ScriptedModel(_one_call(action="look"), "Nothing new.")
    ctx = call_effect.context(tmp_path, llm)
    try:
        assert say(ctx, "look", "T1") == "Nothing new."
        assert logged_dicts(ctx.conn, "confirmation.requested") == []
        (proposed,) = logged_dicts(ctx.conn, "action.proposed")
        assert proposed["risk_level"] == "L0"
        (done,) = logged_dicts(ctx.conn, "action.result_observed")
        assert done["receipt"]["reads_only"] is True
    finally:
        ctx.conn.close()


def test_a_change_inside_his_own_things_runs_with_no_card_and_gives_an_undo(
    tmp_path: Path,
) -> None:
    """A note is L1: it runs with no card, and its receipt names the event an undo reverses."""
    llm = ScriptedModel(_one_call(action="note"), "Noted.")
    ctx = call_effect.context(tmp_path, llm)
    try:
        assert say(ctx, "note", "T1") == "Noted."
        assert logged_dicts(ctx.conn, "confirmation.requested") == []
        (proposed,) = logged_dicts(ctx.conn, "action.proposed")
        assert proposed["risk_level"] == "L1"
        (done,) = logged_dicts(ctx.conn, "action.result_observed")
        assert (done["receipt"]["object"], done["receipt"]["undo"]) == ("note:n1", call_effect.UNDO)
    finally:
        ctx.conn.close()


def test_a_send_makes_a_card_with_the_completed_arguments_and_runs_them_on_his_button(
    tmp_path: Path,
) -> None:
    """A send is L3: the card holds the completed arguments, and his button runs exactly those."""
    llm = ScriptedModel(_one_call(action="send", to="Sam", body="Thursday works"), "Sent.")
    ctx = call_effect.context(tmp_path, llm)
    try:
        say(ctx, "send", "T1")
        (card,) = logged_dicts(ctx.conn, "confirmation.requested")
        (frozen,) = card["calls"]
        assert frozen["args_meta"] == {
            "action": "send", "to": "Sam Lee <sam@example.com>", "body": "Thursday works",
        }
        assert frozen["risk_level"] == "L3"
        assert logged_dicts(ctx.conn, "action.result_observed") == []
        assert press(ctx, {"confirmation_id": card["confirmation_id"], "decision": "accept"}, "T2")
        proposals = logged_dicts(ctx.conn, "action.proposed")  # the ask, then the leased run
        assert [p["risk_level"] for p in proposals] == ["L3", "L3"]
        assert {p["arguments"]["to"] for p in proposals} == {"Sam Lee <sam@example.com>"}
        (done,) = logged_dicts(ctx.conn, "action.result_observed")
        assert json.loads(done["tool_output"])["to"] == "Sam Lee <sam@example.com>"
    finally:
        ctx.conn.close()


@pytest.mark.parametrize(
    ("to", "kind", "says"),
    [("Pat", "needs_him", "Pat Kim"), ("Zed", "impossible", "ask him for the address")],
)
def test_a_completion_that_cannot_resolve_makes_no_card_and_fails_the_receipt(
    tmp_path: Path, to: str, kind: str, says: str,
) -> None:
    """A completion that raises ends the call with its kind and a receipt, and no card."""
    llm = ScriptedModel(_one_call(action="send", to=to, body="hi"), "Which one?")
    ctx = call_effect.context(tmp_path, llm)
    try:
        assert say(ctx, "send", "T1") == "Which one?"
        assert logged_dicts(ctx.conn, "confirmation.requested") == []
        assert logged_dicts(ctx.conn, "action.proposed") == []
        (result,) = tool_results(llm, 1)
        assert (result["kind"], result["is_error"]) == (kind, True)
        assert says in result["error"]
        assert result["receipt"].startswith("r")
    finally:
        ctx.conn.close()


def test_a_send_with_no_lease_is_refused_at_l4_and_a_leased_one_runs(tmp_path: Path) -> None:
    """L4 refuses a call at the card level that carries no lease; reads and leased sends run."""
    world = ToolWorld(tmp_path, tools=(call_effect.depot(),))
    try:
        loose = world.dispatch(
            tool_request("depot", "A1", arguments={"action": "send", "to": "a@example.com"}),
        )
        assert loose.error == "no_lease"
        read = world.dispatch(tool_request("depot", "A2", arguments={"action": "look"}))
        assert read.error is None
        sent = world.dispatch(
            tool_request(
                "depot", "A3", arguments={"action": "send", "to": "a@example.com"}, lease=True,
            ),
        )
        assert sent.error is None
    finally:
        world.close()


def test_the_dashboards_send_completes_the_call_before_its_card(tmp_path: Path) -> None:
    """The Dashboard's ask completes the call, skips a read, and refuses what cannot be resolved."""
    ctx = call_effect.context(tmp_path, ScriptedModel())
    try:
        registry, paths = ctx.tool_registry, ctx.runtime_paths
        assert request_confirmation(
            ctx.conn, registry, "depot", {"action": "look"}, ttl_ms=1, runtime_paths=paths,
        ) is None  # a read asks for nothing
        asked = request_confirmation(
            ctx.conn, registry, "depot", {"action": "send", "to": "Sam"}, ttl_ms=60_000,
            runtime_paths=paths,
        )
        assert asked is not None
        (card,) = logged_dicts(ctx.conn, "confirmation.requested")
        assert card["calls"][0]["args_meta"]["to"] == "Sam Lee <sam@example.com>"
        with pytest.raises(CompletionRefused) as refused:
            request_confirmation(
                ctx.conn, registry, "depot", {"action": "send", "to": "Pat"}, ttl_ms=1,
                runtime_paths=paths,
            )
        assert refused.value.kind == "needs_him"
    finally:
        ctx.conn.close()


def test_tier0_and_the_boot_check_judge_a_varying_tool_per_action(tmp_path: Path) -> None:
    """A varying tool is checked per action: Tier 0 may target only a no-card action."""
    tool = call_effect.depot()
    assert [call_risk(tool, {"action": a}) for a in ("look", "note", "send")] == [
        "L0", "L1", "L3",
    ]
    assert [call_asks(tool, {"action": a}) for a in ("look", "note", "send")] == [
        False, False, True,
    ]
    validate_requires_confirmation([tool], "L3")  # a varying tool is not held to one flag
    with pytest.raises(PolicyConsistencyError, match="requires_confirmation"):
        validate_requires_confirmation([replace(tool, requires_confirmation=True)], "L3")
    with pytest.raises(PolicyConsistencyError, match=r"action 'send'.*above"):  # the highest is L3
        validate_requires_confirmation([replace(tool, risk_level="L1")], "L3")
    with pytest.raises(PolicyConsistencyError, match="action 'note'"):  # a lower floor wants a card
        validate_requires_confirmation([tool], "L1")

    def table(action: str) -> object:
        path = tmp_path / f"{action}.yaml"
        path.write_text(
            "- id: row\n  pattern: '^go$'\n  tool: depot\n"
            f"  args: {{action: {action}}}\n  template: tier0.time_now\n",
            encoding="utf-8",
        )
        return load_tier0_table(path)

    asks = {"depot": lambda args: call_asks(tool, args)}
    for action in ("look", "note"):
        validate_tier0_table(
            table(action), allowed_tool_names=frozenset({"depot"}), asks_for_args=asks,  # type: ignore[arg-type]
        )
    with pytest.raises(Tier0ConfigError):
        validate_tier0_table(
            table("send"), allowed_tool_names=frozenset({"depot"}), asks_for_args=asks,  # type: ignore[arg-type]
        )


def test_a_completion_that_fails_unexpectedly_makes_no_card_and_says_not_to_repeat(
    tmp_path: Path,
) -> None:
    """A completion's own bug ends the call like a refusal does: no card, a failed receipt."""
    llm = ScriptedModel(_one_call(action="send", to="Boom", body="hi"), "It failed.")
    ctx = call_effect.context(tmp_path, llm)
    try:
        assert say(ctx, "send", "T1") == "It failed."
        assert logged_dicts(ctx.conn, "confirmation.requested") == []
        assert logged_dicts(ctx.conn, "action.proposed") == []
        (result,) = tool_results(llm, 1)
        assert (result["code"], result["kind"], result["is_error"]) == (
            "unexpected_error", "dont_retry", True,
        )
        assert "do not repeat the same call" in result["error"]
    finally:
        ctx.conn.close()


def test_the_model_is_told_which_of_a_tools_calls_wait_for_his_approval(tmp_path: Path) -> None:
    """A tool whose calls differ says so in its menu entry; a tool that always runs says nothing."""
    llm = ScriptedModel("Nothing to do.")
    ctx = call_effect.context(tmp_path, llm)
    try:
        say(ctx, "hello", "T1")
        menu = {
            (one.get("function") or one)["name"]: (one.get("function") or one)["description"]
            for one in (llm.requests[0].tools or [])
        }
        assert menu["depot"].startswith("Look, note or send.")
        assert "A call that needs his approval does not run" in menu["depot"]
        assert "needs his approval" not in menu["keep"]
    finally:
        ctx.conn.close()


def test_an_effect_that_cannot_answer_counts_as_the_tools_highest_level() -> None:
    """A call the declaration cannot place is never lowered: it needs a card."""
    tool = call_effect.depot()
    assert call_risk(tool, {"action": "teleport"}) == "L3"  # the effect raises on it
    assert call_asks(tool, {"action": "teleport"}) is True
    lost = replace(tool, effect=lambda _args: "L9")  # type: ignore[arg-type, return-value]
    assert call_risk(lost, {"action": "look"}) == "L3"


def test_the_boot_refuses_a_tier0_row_that_fixes_a_call_that_needs_a_card(tmp_path: Path) -> None:
    """The runtime's boot checks each Tier 0 row's fixed arguments against the tool's effect."""
    config = parse_runtime_config({}, config_path=tmp_path / "jarvis.yaml", runtime_root=tmp_path)
    tool = replace(
        call_effect.depot(),
        allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM, CallerPrincipal.REGEX_ROUTER}),
    )
    registry = build_default_registry()
    registry.register(tool)
    rows = tmp_path / "tier0_patterns.yaml"

    def row(action: str) -> str:
        return (
            "- id: row\n  pattern: '^go$'\n  tool: depot\n"
            f"  args: {{action: {action}}}\n  template: tier0.time_now\n"
        )

    rows.write_text(row("note"), encoding="utf-8")
    load_tables(config, registry)  # a change he can undo needs no card
    rows.write_text(row("send"), encoding="utf-8")
    with pytest.raises(RuntimeBootstrapError, match="depot"):
        load_tables(config, registry)
