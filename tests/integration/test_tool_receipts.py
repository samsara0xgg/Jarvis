"""ADR 0319 — the receipt every tool call ends in.

Observed through the real dispatcher and the Event Log: a done write with its object, a failed
call's kind, a list cut at whole items, the projections by turn and by object, a stored log
without receipts folding to derived ones, the pending phrase in the step line, and the
``Receipt`` row form.
"""

# ruff: noqa: D103 - the test names state the behaviour.
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

import pytest

from jarvis.decision.llm_anthropic import to_messages
from jarvis.execution.bus_tool import build_bus_tool
from jarvis.execution.schedule_tool import build_schedule_tool
from jarvis.execution.tools import Receipted, ToolContext, ToolError, tool
from jarvis.shared import CallerPrincipal, lang
from jarvis.shared.receipts import Covered, Receipt, only_reads, said
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.turn_steps import object_receipts, turn_receipts
from tests.support.phone import StepsHost, logged_tool_call
from tests.support.tool_world import ToolWorld, tool_chain, tool_request

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

_ALLOWED = frozenset({CallerPrincipal.JARVIS_LLM})
_SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}}}


@tool(
    description="Write a thing and say so.",
    input_schema=_SCHEMA,
    allowed_callers=_ALLOWED,
    risk_level="L1",
    read_only=False,
)
def writes_a_thing(args: Mapping[str, Any], _ctx: ToolContext) -> Receipted:
    """A change that names its object."""
    return Receipted(
        {"ok": True},
        Receipt("done", reads_only=False, line=f"Kept thing {args['n']}", object="thing:9684d5"),
    )


@tool(
    description="Ask which of two.",
    input_schema=_SCHEMA,
    allowed_callers=_ALLOWED,
    risk_level="L1",
    read_only=False,
)
def asks_which(_args: Mapping[str, Any], _ctx: ToolContext) -> Receipted:
    """A write that stopped to ask."""
    return Receipted(
        {"candidates": ["a", "b"]},
        Receipt("needs_choice", reads_only=False, line="Two stops match"),
    )


@tool(
    description="List many rows.",
    input_schema=_SCHEMA,
    allowed_callers=_ALLOWED,
    risk_level="L0",
    read_only=True,
    max_result_chars=400,
)
def lists_rows(args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
    """A list over its cap."""
    return {"rows": [{"id": i, "text": "x" * 40} for i in range(args["n"])]}


@tool(
    description="Fail with a named code.",
    input_schema=_SCHEMA,
    allowed_callers=_ALLOWED,
    risk_level="L0",
    read_only=True,
)
def fails_closed(_args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
    """The way a tool refuses."""
    msg = "the store is closed"
    raise ToolError(msg, code="closed")


def _world(tmp_path: Path) -> ToolWorld:
    return ToolWorld(tmp_path, tools=(writes_a_thing, asks_which, lists_rows, fails_closed))


def test_a_done_write_leaves_its_own_receipt_with_the_object(tmp_path: Path) -> None:
    fx = _world(tmp_path)
    try:
        fx.dispatch(tool_request("writes_a_thing", "A1", arguments={"n": 3}))
        receipt = tool_chain(fx, "A1")["receipt"]
        assert receipt == {
            "status": "done", "reads_only": False, "line": "Kept thing 3",
            "object": "thing:9684d5",
        }
        assert said(Receipt.from_json(receipt)) == "Kept thing 3 (thing:9684d5, done)"
    finally:
        fx.close()


def test_a_write_that_asks_is_needs_choice_and_not_a_success(tmp_path: Path) -> None:
    fx = _world(tmp_path)
    try:
        fx.dispatch(tool_request("asks_which", "A1"))
        receipt = Receipt.from_json(tool_chain(fx, "A1")["receipt"])
        assert (receipt.status, receipt.ok, receipt.derived) == ("needs_choice", False, False)
    finally:
        fx.close()


def test_a_refusal_is_failed_with_the_kind_its_code_stands_for(tmp_path: Path) -> None:
    fx = _world(tmp_path)
    try:
        fx.dispatch(tool_request("fails_closed", "A1"))
        observed = tool_chain(fx, "A1")
        assert json.loads(observed["tool_output"]) == {
            "error": "the store is closed", "code": "closed", "kind": "impossible",
        }
        assert observed["receipt"]["failure"] == "impossible"
        assert observed["receipt"]["status"] == "failed"
    finally:
        fx.close()


def test_a_list_over_its_cap_is_cut_at_whole_items_and_says_how_many(tmp_path: Path) -> None:
    fx = _world(tmp_path)
    try:
        fx.dispatch(tool_request("lists_rows", "A1", arguments={"n": 30}))
        out = json.loads(tool_chain(fx, "A1")["tool_output"])
        shown = len(out["rows"])
        assert 0 < shown < 30
        assert out["cut"] == {"rows": {"shown": shown, "more": 30 - shown}}
        assert all(row["text"] == "x" * 40 for row in out["rows"])
        assert "truncated" not in out
    finally:
        fx.close()


def test_bus_and_schedule_say_per_call_whether_they_only_read() -> None:
    bus = build_bus_tool("key", dict)[0]
    schedule = build_schedule_tool(None, 30)[0]
    assert only_reads(bus, {"to": "campus"})
    assert not only_reads(bus, {"get_off": "Main St"})
    assert only_reads(schedule, {"action": "read"})
    assert not only_reads(schedule, {"action": "add"})


def test_a_receipt_refuses_to_contradict_itself() -> None:
    with pytest.raises(ValueError, match="failed if and only if"):
        Receipt("failed", reads_only=False, line="x")
    with pytest.raises(ValueError, match="failed if and only if"):
        Receipt("done", reads_only=False, line="x", failure="dont_retry")
    with pytest.raises(ValueError, match="only a read says"):
        Receipt("done", reads_only=False, line="x", covered=Covered(("a",), "today", 1, 0))
    with pytest.raises(ValueError, match="only a change has"):
        Receipt("done", reads_only=True, line="x", object="thing:1")
    with pytest.raises(ValueError, match="object must be"):
        Receipt("done", reads_only=False, line="x", object="no kind")


def test_a_receipt_round_trips_its_row_form_and_keeps_its_line_one_short_line() -> None:
    receipt = Receipt("done", reads_only=True, line="read [calendar]\n" + "x" * 300,
        covered=Covered(("calendar",), "2026-10-21", 2, 5, "ask for the rest"),
    )
    assert len(receipt.line) <= 200
    assert "[" not in receipt.line
    assert Receipt.from_json(receipt.to_json()) == receipt
    assert said(receipt).endswith("; 5 more not shown, ask for the rest)")
    with pytest.raises(ValueError, match="status"):
        Receipt.from_json({"status": 3, "reads_only": True, "line": "x"})


def _result_row(host: StepsHost, turn: str, tool_name: str, receipt: dict[str, Any] | None) -> None:
    """One finished call as the dispatcher logs it, with or without a receipt."""
    conn = open_event_log(host.log)
    action = f"A-{tool_name}-{turn}-{receipt and receipt['line']}"
    try:
        for kind, payload in (
            ("action.proposed", {
                "action_id": action, "tool_name": tool_name, "caller_principal": "jarvis_llm",
                "risk_level": "L1", "arguments": {}, "turn_id": turn,
            }),
            ("action.running", {"action_id": action}),
            ("action.result_observed", {
                "action_id": action, "semantics": "observation", "tool_output": "{}",
                **({"receipt": receipt} if receipt else {}),
            }),
        ):
            emit_event(conn, type=kind, payload=payload, correlation={
                "action_id": action, "turn_id": turn,
            })
    finally:
        conn.close()


def test_projections_by_turn_and_by_object_and_old_rows_fold_to_derived(tmp_path: Path) -> None:
    host = StepsHost(tmp_path)
    first = Receipt("done", reads_only=False, line="Kept A", object="thing:aaaaaa").to_json()
    later = Receipt("done", reads_only=False, line="Moved A", object="thing:aaaaaa").to_json()
    _result_row(host, "T1", "schedule", first)
    _result_row(host, "T2", "schedule", later)
    logged_tool_call(host, "T2", "keep", {"text": "old row"}, take=0)  # no receipt in the row
    conn = open_event_log(host.log)
    try:
        by_turn = turn_receipts(conn, ["T1", "T2", "T9"])
        assert [r.line for r in by_turn["T2"]] == [
            "Moved A", "ran keep(text=old row)",
        ]
        assert [r.derived for r in by_turn["T2"]] == [False, True]
        assert "T9" not in by_turn
        latest = object_receipts(conn)["thing:aaaaaa"]
        assert (latest.turn_id, latest.tool, latest.receipt.line) == ("T2", "schedule", "Moved A")
    finally:
        conn.close()


@pytest.mark.parametrize("language", ["zh", "en"])
def test_a_pending_call_says_so_in_its_step_line(
    tmp_path: Path, language: Literal["zh", "en"],
) -> None:
    lang.set_language(language)
    host = StepsHost(tmp_path)
    _result_row(host, "T1", "keep", Receipt("pending", reads_only=False, line="Sent").to_json())
    conn = open_event_log(host.log)
    try:
        from jarvis.state.turn_steps import answer_steps  # noqa: PLC0415 - needs the log first
        from tests.support.phone import answer_with_record  # noqa: PLC0415

        conn.close()
        uid = answer_with_record(host, "T1", "Sent.")
        conn = open_event_log(host.log)
        summary = answer_steps(conn, [uid])[uid]
        assert lang.t("step.pending", lang=language) in json.dumps(summary, ensure_ascii=False)
    finally:
        conn.close()
        lang.set_language("zh")


def test_only_a_failed_call_is_an_error_on_the_messages_api() -> None:
    """ADR 0319: ``is_error`` rides a failed call's ``tool_result`` and no other."""
    history: list[dict[str, Any]] = [
        {"role": "user", "content": "go"},
        {"role": "tool", "tool_call_id": "c1", "content": '{"error": "x"}', "is_error": True},
        {"role": "tool", "tool_call_id": "c2", "content": "{}"},
    ]
    blocks = [b for b in to_messages(history)[0]["content"] if b["type"] == "tool_result"]
    assert [b.get("is_error") for b in blocks] == [True, None]
