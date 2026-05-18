"""Unit tests for ``jarvis.decision.intent`` (Tier 0 + Tier 2 LLM bridge).

Covers:

- ``tier_0_match`` returns None for every Day-1 trigger type.
- ``build_llm_messages`` shape across trigger types.
- ``tool_definitions_for_llm`` round-trips Anthropic-style input_schema.
"""

from __future__ import annotations

from jarvis.decision.intent import (
    build_llm_messages,
    tier_0_match,
    tool_definitions_for_llm,
)
from jarvis.decision.packet import SituationPacket
from jarvis.shared import Event
from jarvis.state.projections import TaskLedger


def _packet_for(trigger: Event) -> SituationPacket:
    """Build a SituationPacket for an isolated trigger (no ledger needed)."""
    snapshot = TaskLedger.from_events([]).snapshot()
    return SituationPacket(
        trigger_event=trigger,
        recent_trace=(),
        task_ledger_snapshot=snapshot,
        open_tasks=(),
        current_turn_id=None,
        current_run_id=None,
    )


def test_tier_0_match_is_callable_no_op():
    """Day-1 Tier 0 has no deterministic shortcuts; the call is a no-op."""
    trigger = Event(
        event_uid="utt-1",
        type="surface.user_intent",
        schema_version=1,
        ts_epoch_ms=1,
        payload={"transcript": "hi", "turn_id": "T1"},
        source_event_id=None,
        correlation=None,
    )
    # The function returns None implicitly; calling it MUST NOT raise.
    tier_0_match(_packet_for(trigger))


def test_build_llm_messages_passes_transcript_for_utterance():
    """surface.user_intent → single user message with transcript content."""
    trigger = Event(
        event_uid="utt-2",
        type="surface.user_intent",
        schema_version=1,
        ts_epoch_ms=1,
        payload={"transcript": "review yesterday's task", "turn_id": "T1"},
        source_event_id=None,
        correlation=None,
    )
    messages = build_llm_messages(_packet_for(trigger))

    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "review yesterday's task"


def test_build_llm_messages_for_worker_reported_describes_trigger():
    """worker.reported → user message that names the trigger + run_id."""
    trigger = Event(
        event_uid="worker-1",
        type="worker.reported",
        schema_version=1,
        ts_epoch_ms=2,
        payload={
            "run_id": "R1",
            "action_id": "A1",
            "status": "reported_complete",
            "summary": "codex wrote diff",
        },
        source_event_id=None,
        correlation={"run_id": "R1"},
    )
    messages = build_llm_messages(_packet_for(trigger))

    assert len(messages) == 1
    content = messages[0]["content"]
    assert "worker.reported" in content
    assert "R1" in content
    assert "codex wrote diff" in content


def test_build_llm_messages_explicit_utterance_overrides_trigger():
    """An explicit utterance kwarg trumps the trigger payload."""
    trigger = Event(
        event_uid="any-1",
        type="surface.user_intent",
        schema_version=1,
        ts_epoch_ms=1,
        payload={"transcript": "ignored", "turn_id": "T1"},
        source_event_id=None,
        correlation=None,
    )
    messages = build_llm_messages(_packet_for(trigger), utterance="explicit")

    assert messages[0]["content"] == "explicit"


def test_tool_definitions_for_llm_passes_through_input_schema():
    """Tool list keeps name / description / input_schema verbatim."""
    raw = [
        {
            "name": "verify_diff",
            "description": "verify the diff artifact",
            "input_schema": {
                "type": "object",
                "properties": {"run_id": {"type": "string"}},
                "required": ["run_id"],
            },
        }
    ]
    out = tool_definitions_for_llm(raw)
    assert out == raw
