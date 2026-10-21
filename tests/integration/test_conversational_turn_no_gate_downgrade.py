"""E2E regression: conversational turn must not be force-downgraded.

Per the 2026-05-27 Pre-emit Gate design draft (removed 2026-09-29), §7
acceptance criteria:

  1. Conversational pass-through — a turn with no active task subject
     surfaces the LLM's own draft text (not the canonical "找不到对应的
     task" hard-refusal template).
  2. Gate event audit — exactly one ``gate.evaluated`` event with
     ``attempt=0``, ``permission=allow_completion_language``,
     ``downgrade_required=False``, ``active_claim_levels=[]``.
  6. No new LLM calls — ``cost.recorded`` count for the turn drops to
     one (the original draft generation); the attempt-1 retry and
     attempt-2 template branch must not fire.

Spec basis: §3.4.12 v0 (only gate consequential claims), §3.4.4
(LLMSituationPacket ``active_task?`` is optional, so empty-task turns
are first-class).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from jarvis.decision import (
    decide,
)
from jarvis.state.event_log import emit_event
from tests.support.turn_context import build_decide_context

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


def _gate_evaluated_payloads(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT payload_json FROM events WHERE type = 'gate.evaluated' "
        "ORDER BY ts_epoch_ms"
    ).fetchall()
    return [json.loads(row[0]) for row in rows]


def _count_rows(conn: sqlite3.Connection, event_type: str) -> int:
    cursor = conn.execute(
        "SELECT COUNT(*) FROM events WHERE type = ?", (event_type,),
    )
    return int(cursor.fetchone()[0])


def test_conversational_turn_passes_through_without_downgrade(
    tmp_path: Path,
) -> None:
    """`介绍一下你自己` against empty ledger → LLM draft surfaces unmodified."""
    # The draft contains the "完成" keyword. Pre-change this triggers
    # force_limitation → retry → template → hard_refusal. Post-change
    # the None-subject pass-through ships it verbatim.
    draft = "我是 Jarvis，我可以帮你完成各种任务。"  # noqa: RUF001 — intentional CJK fullwidth punctuation; the draft mirrors a real LLM Chinese reply.
    ctx, conn, llm = build_decide_context(tmp_path, draft_text=draft)
    try:
        trigger = emit_event(
            conn,
            type="surface.user_intent",
            payload={
                "transcript": "介绍一下你自己",
                "turn_id": "T_conv_001",
            },
            correlation={"turn_id": "T_conv_001"},
        )

        result = decide(trigger, ctx)

        # Acceptance criterion 1: the LLM draft surfaces verbatim.
        assert result.response_plan is not None
        assert result.response_plan.text == draft, (
            "Conversational turn was downgraded — pre-emit gate fired despite no "
            f"subject. Got: {result.response_plan.text!r}"
        )
        assert "找不到对应的 task" not in result.response_plan.text, (
            "Conversational turn surfaced the F1 / hard-refusal canonical text — "
            "indicates the gate retry chain ran to exhaustion."
        )
        assert result.response_plan.permission == "allow_completion_language"
        assert result.response_plan.downgrade_required is False

        # Acceptance criterion 2: exactly one gate.evaluated, attempt 0,
        # outcome=allow_completion_language, claim_levels=[]. The gate
        # event payload key is "outcome" (not "permission") and
        # downgrade_required is a ResponsePlan field — its absence from
        # the gate payload is by design; outcome=allow_completion_language
        # already conveys non-downgrading.
        gates = _gate_evaluated_payloads(conn)
        assert len(gates) == 1, (
            f"Expected exactly 1 gate.evaluated event, got {len(gates)}: "
            f"the retry chain (attempts 1 and 2) must not fire when no "
            f"subject is in scope. Payloads: {gates!r}"
        )
        verdict = gates[0]
        assert verdict["gate"] == "pre_emit"
        assert verdict["attempt"] == 0
        assert verdict["outcome"] == "allow_completion_language"
        assert verdict["claim_levels"] == []

        # Acceptance criterion 6: exactly one cost.recorded — the original
        # draft generation. No retry round-trip.
        cost_count = _count_rows(conn, "cost.recorded")
        assert cost_count == 1, (
            f"Expected exactly 1 cost.recorded event, got {cost_count}: "
            f"the attempt-1 LLM retry must not fire on the no-subject path."
        )
        assert len(llm.requests) == 1, (
            f"LLM stub recorded {len(llm.requests)} chat calls; expected 1."
        )
    finally:
        conn.close()
