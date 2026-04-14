"""Historical run comparison and regression detection."""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from system_tests.models import RunResult


def _serialize_run(run: RunResult) -> str:
    """Serialize RunResult to JSON string.

    Falls back to dataclasses.asdict when JsonReporter is unavailable
    (e.g. reporter module not yet implemented in parallel task).
    """
    try:
        from system_tests.reporter import JsonReporter  # noqa: PLC0415

        return JsonReporter.format(run)
    except (ImportError, AttributeError):
        data = asdict(run)
        data["summary"] = run.summary
        return json.dumps(data, indent=2)


def save_run(run: RunResult, runs_dir: Path) -> Path:
    """Save run results as JSON for future baseline comparison.

    Args:
        run: The completed test run to persist.
        runs_dir: Directory where JSON baselines are stored.

    Returns:
        Path to the written JSON file.
    """
    runs_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.fromisoformat(run.timestamp).strftime("%Y-%m-%d_%H%M")
    path = runs_dir / f"{ts}.json"
    path.write_text(_serialize_run(run), encoding="utf-8")
    return path


def load_latest(runs_dir: Path) -> dict[str, Any] | None:
    """Load the most recent run result JSON.

    Args:
        runs_dir: Directory containing JSON baseline files.

    Returns:
        Parsed JSON dict of the most recent run, or None if no files exist.
    """
    if not runs_dir.exists():
        return None
    files = sorted(runs_dir.glob("*.json"), reverse=True)
    if not files:
        return None
    return json.loads(files[0].read_text(encoding="utf-8"))


def compare_runs(
    current: RunResult,
    previous: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Compare current run against previous baseline, return list of changes.

    Detects regressions (pass→fail), improvements (fail→pass), and latency
    spikes (>150 % of previous average).

    Args:
        current: The freshly completed RunResult.
        previous: Parsed JSON dict of the previous baseline, or None.

    Returns:
        List of change dicts.  Each has at minimum ``scenario``, ``severity``,
        ``field``, and ``change`` keys.  Severity values: ``regression``,
        ``improvement``, ``warning``, ``info``.
    """
    if previous is None:
        return []

    # Build lookup: "suite/scenario" → {status, latency_ms}
    prev_lookup: dict[str, dict[str, Any]] = {}
    for suite in previous.get("suites", []):
        for sc in suite.get("scenarios", []):
            key = f"{suite['name']}/{sc['name']}"
            steps = sc.get("steps", [])
            avg_latency = (
                sum(s.get("latency_ms", 0) for s in steps) // len(steps)
                if steps
                else 0
            )
            prev_lookup[key] = {"status": sc["status"], "latency_ms": avg_latency}

    changes: list[dict[str, Any]] = []

    for suite in current.suites:
        for sc in suite.scenarios:
            key = f"{suite.name}/{sc.name}"
            prev = prev_lookup.get(key)

            if prev is None:
                changes.append(
                    {
                        "scenario": key,
                        "severity": "info",
                        "field": "status",
                        "change": "new scenario",
                    }
                )
                continue

            curr_status = sc.status
            prev_status = prev["status"]

            if prev_status == "pass" and curr_status == "fail":
                changes.append(
                    {
                        "scenario": key,
                        "severity": "regression",
                        "field": "status",
                        "change": "pass→fail",
                        "previous_run": previous.get("timestamp", ""),
                    }
                )
            elif prev_status == "fail" and curr_status == "pass":
                changes.append(
                    {
                        "scenario": key,
                        "severity": "improvement",
                        "field": "status",
                        "change": "fail→pass",
                    }
                )

            # Latency comparison: flag if current average exceeds 150 % of previous.
            curr_latency = sum(s.latency_ms for s in sc.steps) // max(len(sc.steps), 1)
            prev_latency = prev["latency_ms"]
            if prev_latency > 0 and curr_latency > prev_latency * 1.5:
                pct = int((curr_latency / prev_latency - 1) * 100)
                changes.append(
                    {
                        "scenario": key,
                        "severity": "warning",
                        "field": "latency",
                        "change": f"{prev_latency}ms→{curr_latency}ms (+{pct}%)",
                    }
                )

    return changes
