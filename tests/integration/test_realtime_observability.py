"""Wave-0C integration checks for monotonic traces and read-only shadowing."""

from __future__ import annotations

import sqlite3
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from jarvis.shared.realtime_trace import (
    realtime_trace_snapshot,
    record_realtime_trace,
    reset_realtime_trace,
)
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.surface import voice_tts
from tools.realtime_shadow import analyze_event_log, open_shadow_event_log

if TYPE_CHECKING:
    from pathlib import Path


def test_tts_trace_points_share_one_monotonic_clock() -> None:
    """TTS provider timing uses telemetry, not wall-clock/Event Log facts."""
    reset_realtime_trace()
    before_ns = time.monotonic_ns()
    provider = MagicMock(spec=voice_tts.MiniMaxWSClient)
    provider.synthesize = AsyncMock(return_value=b"\x00" * 960)
    player = MagicMock(spec=voice_tts.AudioStreamPlayer)
    player.bytes_pending.return_value = 0
    pipeline = voice_tts.TTSPipeline(
        provider=provider,
        player=player,
        fallback=lambda _text: None,
    )

    pipeline.begin_turn("T-trace", gate_mode="sentence")
    pipeline.handle_chunk("T-trace", "<voice>你好</voice>")

    points = [
        point for point in realtime_trace_snapshot()
        if point.attributes.get("turn_id") == "T-trace"
    ]
    assert [point.name for point in points] == ["tts_text_pushed", "tts_first_pcm"]
    assert [point.monotonic_ns for point in points] == sorted(
        point.monotonic_ns for point in points
    )
    assert all(point.monotonic_ns >= before_ns for point in points)
    assert all(point.telemetry_only for point in points)


def test_realtime_trace_is_bounded_and_contains_no_implicit_production_facts() -> None:
    """The diagnostic sink drops oldest points instead of growing unbounded."""
    reset_realtime_trace()
    for sequence in range(5_000):
        record_realtime_trace("canary", turn_id="T-bounded", sequence=sequence)

    points = realtime_trace_snapshot()
    assert len(points) == 4_096
    assert points[0].attributes["sequence"] == 904
    assert points[-1].attributes["sequence"] == 4_999
    assert [point.monotonic_ns for point in points] == sorted(
        point.monotonic_ns for point in points
    )


def _emit_shadow_turn(
    conn: sqlite3.Connection,
    *,
    turn_id: str,
    gate_mode: str,
    action: bool = False,
    exact_stream_permit: bool = False,
) -> None:
    correlation = {"turn_id": turn_id}
    emit_event(
        conn,
        type="surface.user_intent",
        payload={"transcript": "shadow fixture", "turn_id": turn_id},
        correlation=correlation,
    )
    emit_event(
        conn,
        type="turn.started",
        payload={"turn_id": turn_id},
        correlation=correlation,
    )
    if action:
        emit_event(
            conn,
            type="action.proposed",
            payload={
                "action_id": f"A-{turn_id}",
                "tool_name": "get_current_time",
                "caller_principal": "jarvis_llm",
                "risk_level": "L1",
            },
            correlation=correlation,
        )
        emit_event(
            conn,
            type="action.dispatched",
            payload={"action_id": f"A-{turn_id}"},
            correlation=correlation,
        )
    emit_event(
        conn,
        type="gate.evaluated",
        payload={
            "gate": "pre_emit",
            "outcome": "allow_completion_language",
            "reasons": [],
            "response_hash": f"hash-{turn_id}",
            "claim_levels": [],
            "attempt": 0,
        },
        correlation=correlation,
    )
    if exact_stream_permit:
        emit_event(
            conn,
            type="gate.evaluated",
            payload={
                "gate": "stream_emit",
                "outcome": "pass",
                "reasons": [],
                "check_results": {
                    "candidate_sequence": 0,
                    "response_risk_context_hash": f"risk-{turn_id}",
                },
            },
            correlation=correlation,
        )
    emit_event(
        conn,
        type="surface.response_open",
        payload={
            "turn_id": turn_id,
            "query": "shadow fixture",
            "kind": "text",
            "required_gate_mode": gate_mode,
        },
        correlation=correlation,
    )
    emit_event(
        conn,
        type="surface.response_chunk",
        payload={"turn_id": turn_id, "text": "这是一个普通回答。"},
        correlation=correlation,
    )
    emit_event(
        conn,
        type="surface.response_emitted",
        payload={"turn_id": turn_id, "text": "这是一个普通回答。"},
        correlation=correlation,
    )
    emit_event(
        conn,
        type="turn.ended",
        payload={"turn_id": turn_id},
        correlation=correlation,
    )


def test_shadow_report_is_fail_closed_read_only_and_not_production_truth(
    tmp_path: Path,
) -> None:
    """Shadow separates three metrics without changing the source database."""
    db_path = tmp_path / "events.db"
    writer = open_event_log(db_path)
    _emit_shadow_turn(writer, turn_id="T-safe", gate_mode="sentence")
    _emit_shadow_turn(writer, turn_id="T-action", gate_mode="full_text", action=True)
    _emit_shadow_turn(
        writer,
        turn_id="T-exact",
        gate_mode="sentence",
        exact_stream_permit=True,
    )
    writer.close()
    db_before = db_path.read_bytes()

    read_only = open_shadow_event_log(db_path)
    try:
        assert read_only.execute("PRAGMA query_only").fetchone() == (1,)
        with pytest.raises(sqlite3.OperationalError):
            read_only.execute("CREATE TABLE forbidden_shadow_write (id INTEGER)")
    finally:
        read_only.close()

    report = analyze_event_log(db_path)

    assert db_path.read_bytes() == db_before
    assert len(report.turn_records) == 3
    metrics = report.summary["metrics"]
    assert metrics["first_model_candidate_permitted"] == {
        "numerator": 1,
        "denominator": 3,
        "rate": pytest.approx(1 / 3),
        "basis": "exact_stream_gate_fail_closed",
    }
    assert metrics["any_truthful_early_feedback"]["numerator"] == 3
    assert metrics["any_truthful_early_feedback"]["basis"] == (
        "opportunity_upper_bound_not_observed_delivery"
    )
    histogram = metrics["buffer_full_text"]["reason_histogram"]
    assert histogram["missing_response_risk_context"] == 2
    assert histogram["missing_first_model_candidate"] == 2
    assert histogram["required_gate_mode:full_text"] == 1
    assert histogram["action_or_tool_turn"] == 1

    for record in (*report.turn_records, report.summary):
        assert record["production_fact"] is False
        assert record["record_kind"].startswith("realtime_shadow_")
        assert not {"played", "confirmed", "executed"}.intersection(record)
