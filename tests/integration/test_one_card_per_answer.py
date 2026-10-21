"""ADR 0321: one card covers an answer.

The real ``decide()`` loop, dispatcher, confirmation outbox and event log run against a real stdio
MCP server that stands in for a smart-home bridge, with a scripted model. Asserts on the rows a
turn leaves, the request bodies the model was sent, the card as the phone and the Mac read it, and
what she says.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from jarvis.decision.packet import format_pending_confirmation_note
from jarvis.execution.mcp_tools import McpServers
from jarvis.runtime.inherent_loop import confirmation_card
from jarvis.state.event_log import emit_event
from jarvis.state.projections import PendingConfirmations
from tests.support import consent
from tests.support.model import ScriptedModel
from tests.support.plugin_approval import logged_dicts, press, say

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

ANSWER = "客厅、卧室和厨房的灯都关了。"


@pytest.fixture
def servers() -> Iterator[McpServers]:
    """One loop thread with the stand-in's tools on the menu; stopped after the test."""
    mcp = McpServers(timeout_s=20, always_loaded=consent.LIGHTS_TOOLS)
    yield mcp
    mcp.stop()


def test_three_calls_in_one_response_make_one_card_and_accepting_runs_all_three(
    tmp_path: Path, servers: McpServers,
) -> None:
    """One card lists three calls; accepting mints three leases, runs them, asks once more."""
    llm = ScriptedModel(consent.all_off(), ANSWER)
    ctx, ask = consent.asked(tmp_path, servers, llm)
    try:
        (card,) = logged_dicts(ctx.conn, "confirmation.requested")
        assert [c["args_meta"]["light"] for c in card["calls"]] == list(consent.ROOMS)
        lines = card["template_line"].split("\n")
        assert len(lines) == 3
        assert all(room in line for room, line in zip(consent.ROOMS, lines, strict=True))
        assert [c["question"] for c in card["calls"]] == lines
        assert ask.endswith(card["template_line"])
        assert logged_dicts(ctx.conn, "action.result_observed") == []  # nothing ran yet
        assert len(llm.requests) == 1  # the card ends the turn

        slot = consent.waiting_card(ctx)
        assert slot is not None
        view = confirmation_card(slot, None)
        assert (view["action"], view["args"], view["letter"]) == (card["template_line"], {}, False)

        said = press(ctx, {"confirmation_id": card["confirmation_id"], "decision": "accept"}, "T2")
        assert said == ANSWER
        assert len(logged_dicts(ctx.conn, "confirmation.accepted")) == 1
        gates = logged_dicts(ctx.conn, "gate.evaluated")
        leased = [g["lease_id"] for g in gates if g.get("lease_id")]
        assert len(set(leased)) == 3  # one lease per call
        done = logged_dicts(ctx.conn, "action.result_observed")
        assert [r["receipt"]["status"] for r in done] == ["done"] * 3
        # One more request, whose body carries the three real results; her reply is the answer.
        assert len(llm.requests) == 2
        # The turn's menu with the card's tool on it, and she may call nothing more.
        assert llm.requests[1].tool_choice == "none"
        assert "mcp__lights__set_light" in [d["name"] for d in llm.requests[1].tools or ()]
        results = consent.tool_results(llm, 1)
        assert [(r["light"], r["on"], r["is_error"]) for r in results] == [
            (room, False, False) for room in consent.ROOMS
        ]
        spent = consent.waiting_card(ctx)
        assert spent is not None
        assert spent.state == "consumed"
    finally:
        ctx.conn.close()


def test_dismissing_a_card_of_three_rejects_all_three(tmp_path: Path, servers: McpServers) -> None:
    """The card's close button rejects every call on it."""
    llm = ScriptedModel(consent.all_off())
    ctx, _ = consent.asked(tmp_path, servers, llm)
    try:
        card = logged_dicts(ctx.conn, "confirmation.requested")[0]["confirmation_id"]
        said = press(ctx, {"confirmation_id": card, "decision": "reject"}, "T2")
        assert said == "好，这几件都不做了。"  # noqa: RUF001 — the fixed Chinese rejection of a card of several.
        assert len(logged_dicts(ctx.conn, "confirmation.rejected")) == 1
        assert logged_dicts(ctx.conn, "action.result_observed") == []
        assert len(llm.requests) == 1
    finally:
        ctx.conn.close()


def test_proposing_the_same_calls_again_leaves_the_card_and_a_different_set_replaces_it(
    tmp_path: Path, servers: McpServers,
) -> None:
    """Only a different set of calls replaces the waiting card; the same set leaves it as it is."""
    llm = ScriptedModel(
        consent.all_off(),
        consent.all_off(tuple(reversed(consent.ROOMS))),  # the same set, in another order
        consent.all_off(consent.ROOMS[:2]),  # a different set
    )
    ctx, first = consent.asked(tmp_path, servers, llm)
    try:
        (card,) = logged_dicts(ctx.conn, "confirmation.requested")
        again = say(ctx, "把灯都关了啊", "T2")
        assert logged_dicts(ctx.conn, "confirmation.requested") == [card]  # no second ask
        assert again.endswith(card["template_line"])
        assert first.endswith(card["template_line"])
        slot = consent.waiting_card(ctx)
        assert slot is not None
        assert (slot.confirmation_id, slot.expires_at_ms) == (
            card["confirmation_id"], card["expires_at_ms"],
        )
        say(ctx, "只关前两个", "T3")
        asked = logged_dicts(ctx.conn, "confirmation.requested")
        assert len(asked) == 2
        assert [c["args_meta"]["light"] for c in asked[1]["calls"]] == list(consent.ROOMS[:2])
    finally:
        ctx.conn.close()


def test_a_card_stored_before_cards_held_several_calls_still_folds_shows_and_runs(
    tmp_path: Path, servers: McpServers,
) -> None:
    """An old `confirmation.requested` carries the one `action_snapshot`; it reads as one call."""
    llm = ScriptedModel(ANSWER)
    ctx = consent.context(tmp_path, servers, llm, consent.lights(consent.prompting()))
    try:
        emit_event(
            ctx.conn,
            type="confirmation.requested",
            payload={
                "confirmation_id": "Cold",
                "action_snapshot": {
                    "tool_name": "mcp__lights__set_light",
                    "caller": "jarvis_llm",
                    "canonical_target": "",
                    "target_entity_ref": None,
                    "risk_level": "L3",
                    "args_meta": {"light": "living room", "on": False},
                },
                "template_line": "要执行 lights set_light吗？",  # noqa: RUF001
                "expires_at_ms": 4_000_000_000_000,
            },
        )
        slot = consent.waiting_card(ctx)
        assert slot is not None
        assert len(slot.calls) == 1
        view = confirmation_card(slot, None)
        assert (view["tool"], view["args"]) == (
            "mcp__lights__set_light", {"light": "living room", "on": False},
        )
        packet_note = format_pending_confirmation_note(  # the note names the one call
            SimpleNamespace(pending_confirmation=PendingConfirmations(slot=slot)),  # type: ignore[arg-type]
        )
        assert packet_note is not None
        assert "mcp__lights__set_light" in packet_note
        assert press(ctx, {"confirmation_id": "Cold", "decision": "accept"}, "T1") == ANSWER
        (done,) = logged_dicts(ctx.conn, "action.result_observed")
        assert json.loads(done["tool_output"])["light"] == "living room"
    finally:
        ctx.conn.close()

