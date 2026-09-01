"""Summarize diagnostic realtime JSONL without claiming production truth.

Usage after a controlled run::

    uv run python tools/realtime_trace_report.py trace.jsonl \
      --scenario live_voice --turn-id T123

Pass ``--baseline`` to compute like-for-like duration deltas. The report sorts
by monotonic timestamp because concurrent exporters may enqueue adjacent rows
in a different file order.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    from jarvis.shared.realtime_trace import TraceValue

type Scenario = Literal["routine", "deep_tool", "live_voice"]

_DURATIONS: tuple[tuple[str, str, str], ...] = (
    ("endpoint_to_commit_ms", "endpoint_candidate", "utterance_committed"),
    ("commit_to_asr_final_ms", "utterance_committed", "asr_final"),
    (
        "llm_sdk_call_start_to_batch_complete_ms",
        "llm_sdk_request_call_started_upper_bound",
        "llm_batch_response_completed",
    ),
    (
        "permit_to_tts_start_ms",
        "response_candidate_permitted",
        "tts_batch_synthesis_started",
    ),
    (
        "tts_start_to_provider_first_pcm_ms",
        "tts_batch_synthesis_started",
        "tts_provider_first_pcm_received",
    ),
    (
        "provider_first_pcm_to_ring_accept_ms",
        "tts_provider_first_pcm_received",
        "tts_first_pcm_accepted_to_ring",
    ),
    (
        "ring_accept_to_portaudio_callback_ms",
        "tts_first_pcm_accepted_to_ring",
        "audio_output_first_nonzero_callback",
    ),
    (
        "portaudio_callback_to_armed_acoustic_proxy_ms",
        "audio_output_first_nonzero_callback",
        "acoustic_monitor_first_output_detected_armed",
    ),
    (
        "portaudio_callback_to_software_ring_empty_ms",
        "audio_output_first_nonzero_callback",
        "audio_ring_empty_observed",
    ),
    ("action_wait_ms", "action_wait_started", "action_wait_completed"),
)


@dataclass(frozen=True)
class TraceRow:
    """Validated subset of one diagnostic exporter row."""

    name: str
    monotonic_ns: int
    attributes: dict[str, TraceValue]


def load_trace(path: Path) -> tuple[TraceRow, ...]:
    """Load only explicitly non-production realtime trace rows."""
    rows: list[TraceRow] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            raw = json.loads(line)
            if not isinstance(raw, dict):
                msg = f"{path}:{line_number}: expected JSON object"
                raise TypeError(msg)
            if (
                raw.get("record_kind") != "realtime_trace_point"
                or raw.get("telemetry_only") is not True
                or raw.get("production_fact") is not False
            ):
                msg = f"{path}:{line_number}: row is not diagnostic realtime telemetry"
                raise ValueError(msg)
            name = raw.get("name")
            monotonic_ns = raw.get("monotonic_ns")
            attributes = raw.get("attributes")
            if (
                not isinstance(name, str)
                or not isinstance(monotonic_ns, int)
                or not isinstance(attributes, dict)
            ):
                msg = f"{path}:{line_number}: malformed realtime trace row"
                raise TypeError(msg)
            rows.append(
                TraceRow(
                    name=name,
                    monotonic_ns=monotonic_ns,
                    attributes=cast("dict[str, TraceValue]", attributes),
                ),
            )
    return tuple(sorted(rows, key=lambda row: row.monotonic_ns))


def _select_turn(rows: tuple[TraceRow, ...], requested: str | None) -> str | None:
    if requested is not None:
        return requested
    correlated = [
        (row.monotonic_ns, value)
        for row in rows
        if isinstance((value := row.attributes.get("turn_id")), str) and value
    ]
    return max(correlated, default=(0, None))[1]


def _first_times(rows: tuple[TraceRow, ...]) -> dict[str, int]:
    first: dict[str, int] = {}
    for row in rows:
        first.setdefault(row.name, row.monotonic_ns)
    return first


def summarize_trace(
    rows: tuple[TraceRow, ...],
    *,
    scenario: Scenario,
    turn_id: str | None = None,
) -> dict[str, Any]:
    """Build one bounded report whose limits are machine-readable."""
    selected_turn = _select_turn(rows, turn_id)
    selected = tuple(
        row
        for row in rows
        if selected_turn is None or row.attributes.get("turn_id") == selected_turn
    )
    times = _first_times(selected)
    durations_ms = {
        label: round((times[end] - times[start]) / 1_000_000, 3)
        for label, start, end in _DURATIONS
        if start in times and end in times and times[end] >= times[start]
    }
    endpoint_row = next(
        (row for row in selected if row.name == "endpoint_candidate"),
        None,
    )
    if endpoint_row is not None:
        endpoint_audio_ms = endpoint_row.attributes.get("consecutive_silence_audio_ms")
        if isinstance(endpoint_audio_ms, (int, float)):
            durations_ms["speech_end_to_endpoint_candidate_audio_ms"] = float(
                endpoint_audio_ms,
            )
    origin_ns = selected[0].monotonic_ns if selected else None
    relative_ms = (
        {
            name: round((timestamp - origin_ns) / 1_000_000, 3)
            for name, timestamp in times.items()
        }
        if origin_ns is not None
        else {}
    )
    bounded_failures = [
        row.name
        for row in selected
        if row.name in {
            "action_wait_completed",
            "tts_pipeline_close_bounded",
            "system_output_lease_refused",
        }
        and (
            row.attributes.get("outcome") == "bounded_timeout"
            or row.attributes.get("success") is False
            or row.name == "system_output_lease_refused"
        )
    ]
    return {
        "record_kind": "realtime_trace_report",
        "production_fact": False,
        "scenario": scenario,
        "turn_id": selected_turn,
        "status": "bounded_failure" if bounded_failures else "observed",
        "bounded_failures": bounded_failures,
        "point_count": len(selected),
        "milestones_relative_ms": relative_ms,
        "durations_ms": durations_ms,
        "observability": {
            "sdk_request_call_start_upper_bound_observed": (
                "llm_sdk_request_call_started_upper_bound" in times
            ),
            "network_request_send_observed": False,
            "first_text_delta_observed": "llm_provider_first_text_delta" in times,
            "completed_candidate_permit_observed": (
                "response_candidate_permitted" in times
            ),
            "stream_first_model_candidate_permit_observed": False,
            "truthful_feedback_commit_observed": "surface_segment_committed" in times,
            "truthful_feedback_delivery_observed": False,
            "provider_first_pcm_observed": "tts_provider_first_pcm_received" in times,
            "portaudio_callback_observed": (
                "audio_output_first_nonzero_callback" in times
            ),
            "dac_audible_start_observed": False,
            "dac_audible_completion_observed": False,
            "armed_physical_acoustic_proxy_observed": (
                "acoustic_monitor_first_output_detected_armed" in times
            ),
        },
        "measurement_limits": [
            "batch LLM mode has no first text delta unless chat_stream is used",
            "SDK call start is an upper bound; actual network send is not observed",
            "PortAudio callback is buffer submission, not measured DAC audibility",
            "audio_ring_empty is software-ring state, not complete audible playback",
            "armed microphone energy is an acoustic proxy, not a direct DAC timestamp",
        ],
    }


def compare_reports(
    current: dict[str, Any],
    baseline: dict[str, Any],
) -> dict[str, float]:
    """Return current-minus-baseline deltas for shared duration metrics."""
    current_durations = cast("dict[str, float]", current["durations_ms"])
    baseline_durations = cast("dict[str, float]", baseline["durations_ms"])
    return {
        key: round(current_durations[key] - baseline_durations[key], 3)
        for key in current_durations.keys() & baseline_durations.keys()
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument(
        "--scenario",
        choices=("routine", "deep_tool", "live_voice"),
        required=True,
    )
    parser.add_argument("--turn-id")
    return parser.parse_args()


def main() -> int:
    """Render one diagnostic report to stdout."""
    args = _parse_args()
    scenario = cast("Scenario", args.scenario)
    current = summarize_trace(
        load_trace(cast("Path", args.trace)),
        scenario=scenario,
        turn_id=cast("str | None", args.turn_id),
    )
    if args.baseline is not None:
        baseline = summarize_trace(
            load_trace(cast("Path", args.baseline)),
            scenario=scenario,
            turn_id=cast("str | None", args.turn_id),
        )
        current["comparison_current_minus_baseline_ms"] = compare_reports(
            current,
            baseline,
        )
    sys.stdout.write(json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
