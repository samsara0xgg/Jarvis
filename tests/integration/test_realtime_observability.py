"""Wave-0C integration checks for monotonic traces and read-only shadowing."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from jarvis import runtime as jarvis_runtime
from jarvis.deployment import RuntimePaths
from jarvis.shared.realtime_trace import (
    configure_realtime_trace_jsonl,
    realtime_trace_context,
    realtime_trace_snapshot,
    record_realtime_trace,
    reset_realtime_trace,
)
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.surface import voice_tts
from tools.realtime_shadow import analyze_event_log, open_shadow_event_log
from tools.realtime_trace_report import load_trace, summarize_trace

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
        fallback=None,
    )

    pipeline.begin_turn("T-trace", gate_mode="sentence")
    pipeline.handle_chunk("T-trace", "<voice>你好</voice>")
    assert pipeline.wait_until_idle(timeout_s=1.0)

    points = [
        point for point in realtime_trace_snapshot()
        if point.attributes.get("turn_id") == "T-trace"
    ]
    assert [point.name for point in points] == [
        "tts_batch_synthesis_started",
        "tts_batch_synthesis_completed",
    ]
    assert [point.monotonic_ns for point in points] == sorted(
        point.monotonic_ns for point in points
    )
    assert all(point.monotonic_ns >= before_ns for point in points)
    assert all(point.telemetry_only for point in points)
    assert pipeline.close(wait_timeout_s=1.0)


def test_jsonl_export_is_correlated_diagnostic_only_and_reportable(
    tmp_path: Path,
) -> None:
    """The controlled exporter persists no Event Log-shaped production facts."""
    trace_path = tmp_path / "routine-trace.jsonl"
    configure_realtime_trace_jsonl(trace_path)
    try:
        with realtime_trace_context(turn_id="T-jsonl", channel="integration"):
            record_realtime_trace(
                "llm_sdk_request_call_started_upper_bound",
                provider="fixture",
            )
            record_realtime_trace(
                "response_candidate_permitted",
                measurement_semantics="completed_batch_candidate_not_stream_delta",
            )
    finally:
        # Closing drains the background queue before the test reads the file.
        configure_realtime_trace_jsonl(None)

    exported = [json.loads(line) for line in trace_path.read_text().splitlines()]
    assert len(exported) == 2
    assert all(row["record_kind"] == "realtime_trace_point" for row in exported)
    assert all(row["telemetry_only"] is True for row in exported)
    assert all(row["production_fact"] is False for row in exported)
    assert all(row["attributes"]["turn_id"] == "T-jsonl" for row in exported)
    assert not {"played", "confirmed", "executed"}.intersection(exported[0])

    report = summarize_trace(
        load_trace(trace_path),
        scenario="routine",
        turn_id="T-jsonl",
    )
    assert report["production_fact"] is False
    assert (
        report["observability"]["sdk_request_call_start_upper_bound_observed"]
        is True
    )
    assert report["observability"]["network_request_send_observed"] is False
    assert report["observability"]["dac_audible_completion_observed"] is False
    assert any(
        "not complete audible playback" in limit
        for limit in report["measurement_limits"]
    )


def test_runtime_refuses_event_log_as_trace_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live-burn exporter cannot be pointed at canonical SQLite."""
    event_log = tmp_path / "mac_events.db"
    paths = RuntimePaths(
        root=tmp_path,
        event_log=event_log,
        artifacts_root=tmp_path / "artifacts",
        registry=tmp_path / "registry.json",
        inherent_v2_token=tmp_path / "inherent-v2.token",
    )
    monkeypatch.setenv("JARVIS_REALTIME_TRACE_JSONL", str(event_log))
    with pytest.raises(
        jarvis_runtime.RuntimeBootstrapError,
        match="must not target the production Event Log",
    ):
        jarvis_runtime._configure_realtime_trace_export(paths)  # noqa: SLF001
    assert not event_log.exists()


def test_player_distinguishes_ring_accept_from_portaudio_callback() -> None:
    """Software queue acceptance and callback submission remain separate points."""
    reset_realtime_trace()
    player = voice_tts.AudioStreamPlayer(
        sample_rate_hz=100,
        ring_seconds=1.0,
        lazy_open=True,
    )
    player.begin_trace_turn({"turn_id": "T-player-trace"})
    player.write(np.ones(8, dtype=np.float32).tobytes())
    callback_buffer = np.zeros((8, 1), dtype=np.float32)
    player._callback(  # noqa: SLF001 — drive the registered PortAudio callback seam.
        callback_buffer,
        8,
        None,
        None,
    )
    # Callback only publishes into its bounded SPSC report lane.  User
    # callbacks, trace I/O, and allocation stay on the media owner side.
    player.poll_presentation()

    points = [
        point
        for point in realtime_trace_snapshot()
        if point.attributes.get("turn_id") == "T-player-trace"
    ]
    assert [point.name for point in points] == [
        "tts_first_pcm_accepted_to_ring",
        "audio_output_first_nonzero_callback",
    ]
    assert points[0].attributes["measurement_semantics"] == (
        "first_float32_samples_committed_to_software_ring"
    )
    assert points[1].attributes["measurement_semantics"] == (
        "portaudio_callback_buffer_submission_not_dac_audible"
    )


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
