"""Unit tests for reporter formatting."""
from __future__ import annotations

import json

from system_tests.models import (
    AssertionResult,
    DeviceChange,
    MemoryChange,
    MemoryDiff,
    RunResult,
    ScenarioResult,
    StepResult,
    SuiteResult,
)
from system_tests.reporter import JsonReporter, TerminalReporter


def _make_run() -> RunResult:
    step_pass = StepResult(
        "打开卧室灯", "好的，灯开了", ["好的，灯开了"], None, "local",
        [DeviceChange("bedroom_light", "is_on", False, True)],
        MemoryDiff([], []), 200, {"groq": 1},
        {"no_crash": AssertionResult("pass", None, None, None),
         "route": AssertionResult("pass", "smart_home", "smart_home", None)},
        None,
    )
    step_fail = StepResult(
        "调成蓝色", "ok", ["ok"], None, "cloud", [], MemoryDiff([], []),
        2800, {"groq": 1, "xai": 1},
        {"tier": AssertionResult("fail", "local", "cloud", "confidence=0.72")},
        None,
    )
    s1 = ScenarioResult("单灯开关", [step_pass], False, None)
    s2 = ScenarioResult("颜色设置", [step_fail], False, None)
    suite = SuiteResult("智能家居", [s1, s2])
    return RunResult("2026-04-13T14:30:00", 10.0, [suite])


class TestJsonReporter:
    def test_produces_valid_json(self):
        run = _make_run()
        output = JsonReporter.format(run)
        data = json.loads(output)
        assert data["summary"]["pass"] == 1
        assert data["summary"]["fail"] == 1

    def test_includes_failures(self):
        run = _make_run()
        data = json.loads(JsonReporter.format(run))
        assert len(data["failures"]) == 1
        assert data["failures"][0]["scenario"] == "颜色设置"

    def test_cost_estimate(self):
        run = _make_run()
        data = json.loads(JsonReporter.format(run))
        assert data["cost_estimate_usd"] > 0


class TestTerminalReporter:
    def test_summary_line(self):
        run = _make_run()
        lines = TerminalReporter.format_summary(run)
        text = "\n".join(lines)
        assert "1" in text  # at least shows counts
