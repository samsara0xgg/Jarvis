"""Read-only Wave-0 shadow analysis for historical realtime opportunity.

This tool never imports the Event Log writer and opens SQLite with both URI
``mode=ro`` and ``PRAGMA query_only``.  Its records are analytical estimates,
never claims that text was played, confirmed, or executed in production.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

_SAFE_PRE_EMIT_OUTCOMES: Final[frozenset[str]] = frozenset(
    {"pass", "allow_completion_language", "force_limitation_language"}
)
_CONSERVATIVE_RISK_MARKERS: Final[tuple[str, ...]] = (
    "已完成",
    "已执行",
    "已删除",
    "已发送",
    "成功",
    "失败",
    "确认",
    "执行",
    "删除",
    "发送",
    "completed",
    "executed",
    "deleted",
    "sent",
    "success",
    "failed",
    "confirmed",
)


@dataclass(frozen=True)
class ShadowReport:
    """JSON-ready analytical turn rows plus one summary row."""

    turn_records: tuple[dict[str, Any], ...]
    summary: dict[str, Any]


@dataclass(frozen=True)
class _ShadowEvent:
    """Minimal row shape needed by the offline fold."""

    event_type: str
    payload: dict[str, Any]
    correlation: dict[str, Any]


def open_shadow_event_log(path: Path) -> sqlite3.Connection:
    """Open an existing Event Log through SQLite's read-only URI mode."""
    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
    except BaseException:
        conn.close()
        raise
    return conn


def _mapping_json(raw: str | None) -> dict[str, Any]:
    if raw is None:
        return {}
    decoded = json.loads(raw)
    return dict(decoded) if isinstance(decoded, dict) else {}


def _read_complete_turns(conn: sqlite3.Connection) -> list[tuple[str, list[_ShadowEvent]]]:
    grouped: dict[str, list[_ShadowEvent]] = defaultdict(list)
    completed: set[str] = set()
    rows = conn.execute(
        "SELECT type, payload_json, correlation_json FROM events ORDER BY id ASC"
    )
    for event_type, payload_raw, correlation_raw in rows:
        payload = _mapping_json(str(payload_raw))
        correlation = _mapping_json(
            None if correlation_raw is None else str(correlation_raw)
        )
        turn_raw = correlation.get("turn_id", payload.get("turn_id"))
        if not isinstance(turn_raw, str) or not turn_raw:
            continue
        event = _ShadowEvent(
            event_type=str(event_type),
            payload=payload,
            correlation=correlation,
        )
        grouped[turn_raw].append(event)
        if event.event_type == "turn.ended":
            completed.add(turn_raw)
    return [(turn_id, events) for turn_id, events in grouped.items() if turn_id in completed]


def _first_payload(events: list[_ShadowEvent], event_type: str) -> dict[str, Any]:
    for event in events:
        if event.event_type == event_type:
            return event.payload
    return {}


def _exact_first_candidate_permit(events: list[_ShadowEvent]) -> bool:
    for event in events:
        payload = event.payload
        checks_raw = payload.get("check_results")
        checks = checks_raw if isinstance(checks_raw, dict) else {}
        sequence = payload.get("candidate_sequence", checks.get("candidate_sequence"))
        risk_hash = payload.get(
            "response_risk_context_hash",
            checks.get("response_risk_context_hash"),
        )
        if (
            event.event_type == "gate.evaluated"
            and payload.get("gate") == "stream_emit"
            and payload.get("outcome") == "pass"
            and isinstance(sequence, int)
            and not isinstance(sequence, bool)
            and sequence == 0
            and isinstance(risk_hash, str)
            and bool(risk_hash)
        ):
            return True
    return False


def _safe_historical_proxy(events: list[_ShadowEvent], gate_mode: str) -> bool:
    """Return a conservative opportunity bound, never observed delivery."""
    if gate_mode != "sentence":
        return False
    pre_emit = next(
        (
            event.payload
            for event in events
            if event.event_type == "gate.evaluated"
            and event.payload.get("gate") == "pre_emit"
            and event.payload.get("attempt", 0) == 0
        ),
        {},
    )
    if pre_emit.get("outcome") not in _SAFE_PRE_EMIT_OUTCOMES:
        return False
    claim_levels = pre_emit.get("claim_levels", [])
    if not isinstance(claim_levels, list) or claim_levels:
        return False
    chunks = [
        str(event.payload.get("text", ""))
        for event in events
        if event.event_type == "surface.response_chunk"
    ]
    candidate = "".join(chunks).strip()
    lowered = candidate.casefold()
    return bool(candidate) and not any(marker in lowered for marker in _CONSERVATIVE_RISK_MARKERS)


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def analyze_event_log(path: Path) -> ShadowReport:
    """Fold complete historical turns without mutating the source database."""
    conn = open_shadow_event_log(path)
    try:
        turns = _read_complete_turns(conn)
    finally:
        conn.close()

    reason_histogram: Counter[str] = Counter()
    records: list[dict[str, Any]] = []
    exact_count = 0
    feedback_count = 0

    for turn_id, events in turns:
        open_payload = _first_payload(events, "surface.response_open")
        gate_mode_raw = open_payload.get("required_gate_mode", "unknown")
        gate_mode = gate_mode_raw if isinstance(gate_mode_raw, str) else "unknown"
        action_or_tool = any(
            event.event_type in {"action.proposed", "action.dispatched"}
            for event in events
        )
        action_dispatched = any(
            event.event_type == "action.dispatched" for event in events
        )
        exact_permit = _exact_first_candidate_permit(events)
        safe_proxy = _safe_historical_proxy(events, gate_mode)
        feedback_opportunity = exact_permit or action_dispatched or safe_proxy

        reasons: list[str] = []
        if not exact_permit:
            reasons.extend(
                ["missing_response_risk_context", "missing_first_model_candidate"]
            )
        if gate_mode != "sentence":
            reasons.append(f"required_gate_mode:{gate_mode}")
        if action_or_tool:
            reasons.append("action_or_tool_turn")
        if not feedback_opportunity:
            reasons.append("no_truthful_early_feedback_opportunity")
        reason_histogram.update(reasons)

        exact_count += int(exact_permit)
        feedback_count += int(feedback_opportunity)
        records.append(
            {
                "record_kind": "realtime_shadow_turn",
                "production_fact": False,
                "turn_id": turn_id,
                "first_model_candidate_permitted": exact_permit,
                "any_truthful_early_feedback_opportunity": feedback_opportunity,
                "feedback_basis": (
                    "exact_stream_gate"
                    if exact_permit
                    else "action_lifecycle_opportunity"
                    if action_dispatched
                    else "conservative_historical_lexical_proxy"
                    if safe_proxy
                    else "none"
                ),
                "buffer_full_text_reasons": reasons,
            }
        )

    denominator = len(records)
    summary: dict[str, Any] = {
        "record_kind": "realtime_shadow_summary",
        "production_fact": False,
        "source_open_mode": "sqlite_mode_ro_query_only",
        "metrics": {
            "first_model_candidate_permitted": {
                "numerator": exact_count,
                "denominator": denominator,
                "rate": _rate(exact_count, denominator),
                "basis": "exact_stream_gate_fail_closed",
            },
            "any_truthful_early_feedback": {
                "numerator": feedback_count,
                "denominator": denominator,
                "rate": _rate(feedback_count, denominator),
                "basis": "opportunity_upper_bound_not_observed_delivery",
            },
            "buffer_full_text": {
                "turns_with_reason": sum(
                    bool(record["buffer_full_text_reasons"])
                    for record in records
                ),
                "denominator": denominator,
                "reason_histogram": dict(sorted(reason_histogram.items())),
            },
        },
    }
    return ShadowReport(turn_records=tuple(records), summary=summary)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("event_log", type=Path, help="existing Event Log SQLite path")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Print JSONL analytical records to stdout only."""
    args = _build_parser().parse_args(argv)
    report = analyze_event_log(args.event_log)
    for record in (*report.turn_records, report.summary):
        sys.stdout.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as an integration CLI.
    raise SystemExit(main())
