"""Attention Policy default channel — a direct answer to the user is spoken.

docs/goals/speak-ordinary-answers.md: an ordinary user utterance routes
``voice_notify``, and the reconciliation terminals the ADR-0009 D4
supervisor sweep drives stay ``queue_review`` so a 3am system turn never
speaks (ADR-0002 Limitation-routing amendment, B-0005).

The first test drives :func:`jarvis.decision.gates.attention_policy` on a
packet folded from an empty event log; the second re-checks both verdicts
one layer up, through the real :func:`jarvis.decision.decide`, so a future
override inside ``_finalize_response`` cannot silently undo the routing.
No daemon, no real LLM.
"""

from __future__ import annotations

import contextlib
import dataclasses
from typing import TYPE_CHECKING

from jarvis.decision import decide
from jarvis.decision.gates import attention_policy
from jarvis.decision.packet import assemble_packet
from jarvis.runtime.inherent_loop import _system_trigger_event
from jarvis.shared import Event
from jarvis.state.event_log import emit_event, open_event_log

# The decide()-level test needs a DecideContext with a scripted LLM; the
# conversational-turn regression already builds exactly that one.
from tests.integration.test_conversational_turn_no_gate_downgrade import _build_ctx

if TYPE_CHECKING:
    from pathlib import Path


def _trigger(event_type: str) -> Event:
    return Event(
        event_uid=f"uid-{event_type}",
        type=event_type,
        schema_version=1,
        ts_epoch_ms=0,
        payload={},
        source_event_id=None,
        correlation=None,
    )


def test_attention_policy_speaks_ordinary_answers_only(tmp_path: Path) -> None:
    """Plain utterance -> voice_notify; system triggers keep their channels."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        packet = assemble_packet(_trigger("surface.user_intent"), conn)

    assert attention_policy(packet) == "voice_notify"
    utterance = dataclasses.replace(packet, trigger_event=_trigger("utterance.received"))
    assert attention_policy(utterance) == "voice_notify"
    assert attention_policy(utterance, document_form=True) == "badge_card"

    for terminal in ("action.timeout_assumed", "action.failed", "action.cancelled"):
        system = dataclasses.replace(packet, trigger_event=_trigger(terminal))
        assert attention_policy(system) == "queue_review"
        assert attention_policy(system, document_form=True) == "queue_review"


def test_decide_keeps_reconciliation_turns_in_queue_review(tmp_path: Path) -> None:
    """Same two verdicts through the real decide(): system turn stays silent."""
    ctx, conn, _llm = _build_ctx(tmp_path, draft_text="今天下午三点有一个会。")
    try:
        utterance = emit_event(
            conn,
            type="surface.user_intent",
            payload={"transcript": "我今天有什么安排", "turn_id": "T_speak_001"},
            correlation={"turn_id": "T_speak_001"},
        )
        assert decide(utterance, ctx).attention_channel == "voice_notify"

        # Shaped exactly like the ADR-0009 D4 supervisor sweep's system
        # turn: the orphan terminal row, re-stamped with a fresh turn_id.
        terminal = emit_event(
            conn,
            type="action.timeout_assumed",
            payload={"action_id": "A_orphan_001", "reason": "budget_exceeded"},
            correlation={"turn_id": "T_orphan_000"},
        )
        result = decide(_system_trigger_event(terminal), ctx)
        assert result.attention_channel == "queue_review", (
            "A sweep-driven reconciliation turn routed to "
            f"{result.attention_channel!r}: it would speak into an empty room."
        )
    finally:
        conn.close()
