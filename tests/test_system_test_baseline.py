"""Unit tests for baseline comparison."""
from __future__ import annotations

import json
from pathlib import Path

from system_tests.baseline import compare_runs, load_latest, save_run
from system_tests.models import (
    AssertionResult,
    MemoryDiff,
    RunResult,
    ScenarioResult,
    StepResult,
    SuiteResult,
)


def _make_step(latency: int = 200, passed: bool = True) -> StepResult:
    a = {"ok": AssertionResult("pass" if passed else "fail", None, None, None)}
    return StepResult("test", "ok", ["ok"], None, "local", [], MemoryDiff([], []),
                      latency, {"groq": 1}, a, None)


def _make_run(timestamp: str, statuses: list[bool]) -> RunResult:
    scenarios = [
        ScenarioResult(f"scenario_{i}", [_make_step(passed=s)], False, None)
        for i, s in enumerate(statuses)
    ]
    return RunResult(timestamp, 10.0, [SuiteResult("suite", scenarios)])


class TestSaveAndLoad:
    def test_round_trip(self, tmp_path: Path):
        run = _make_run("2026-04-13T14:30:00", [True, True])
        save_run(run, tmp_path)
        loaded = load_latest(tmp_path)
        assert loaded is not None
        assert loaded["summary"]["pass"] == 2

    def test_load_empty_dir(self, tmp_path: Path):
        assert load_latest(tmp_path) is None


class TestCompareRuns:
    def test_regression_detected(self):
        current = _make_run("2026-04-13", [True, False])
        previous = {
            "suites": [
                {
                    "name": "suite",
                    "scenarios": [
                        {"name": "scenario_0", "status": "pass", "steps": [{"latency_ms": 200}]},
                        {"name": "scenario_1", "status": "pass", "steps": [{"latency_ms": 200}]},
                    ],
                }
            ]
        }
        regressions = compare_runs(current, previous)
        assert any(r["change"] == "pass→fail" for r in regressions)

    def test_improvement_detected(self):
        current = _make_run("2026-04-13", [True, True])
        previous = {
            "suites": [
                {
                    "name": "suite",
                    "scenarios": [
                        {"name": "scenario_0", "status": "pass", "steps": [{"latency_ms": 200}]},
                        {"name": "scenario_1", "status": "fail", "steps": [{"latency_ms": 200}]},
                    ],
                }
            ]
        }
        regressions = compare_runs(current, previous)
        assert any(r.get("severity") == "improvement" for r in regressions)

    def test_latency_spike(self):
        current_step = _make_step(latency=1000)
        current = RunResult("2026-04-13", 10.0, [
            SuiteResult("suite", [ScenarioResult("sc", [current_step], False, None)])
        ])
        previous = {
            "suites": [
                {
                    "name": "suite",
                    "scenarios": [
                        {"name": "sc", "status": "pass", "steps": [{"latency_ms": 200}]},
                    ],
                }
            ]
        }
        regressions = compare_runs(current, previous)
        assert any(r.get("severity") == "warning" and "latency" in r.get("field", "") for r in regressions)

    def test_no_previous(self):
        current = _make_run("2026-04-13", [True])
        regressions = compare_runs(current, None)
        assert regressions == []
