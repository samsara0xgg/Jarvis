"""Unit tests for system test data models."""
from __future__ import annotations

from system_tests.models import (
    AssertionResult,
    DeviceChange,
    MemoryChange,
    MemoryDiff,
    RunResult,
    ScenarioResult,
    StepExpect,
    StepResult,
    SuiteResult,
)


class TestDeviceChange:
    def test_display_bool(self):
        c = DeviceChange("bedroom_light", "is_on", False, True)
        assert "OFF" in c.display() and "ON" in c.display()

    def test_display_int(self):
        c = DeviceChange("bedroom_light", "brightness", 100, 50)
        assert "100" in c.display() and "50" in c.display()


class TestMemoryDiff:
    def test_empty(self):
        d = MemoryDiff(added=[], removed=[])
        assert d.is_empty

    def test_not_empty(self):
        d = MemoryDiff(
            added=[MemoryChange("added", "likes coffee", "preference", "drink")],
            removed=[],
        )
        assert not d.is_empty


class TestStepResult:
    def test_passed_when_all_pass(self):
        r = StepResult(
            input_text="test",
            response="ok",
            sentences=["ok"],
            route=None,
            path="local",
            device_changes=[],
            memory_diff=MemoryDiff([], []),
            latency_ms=100,
            api_calls={},
            assertions={"no_crash": AssertionResult("pass", None, None, None)},
            error=None,
        )
        assert r.passed

    def test_failed_when_any_fail(self):
        r = StepResult(
            input_text="test",
            response="ok",
            sentences=["ok"],
            route=None,
            path="local",
            device_changes=[],
            memory_diff=MemoryDiff([], []),
            latency_ms=100,
            api_calls={},
            assertions={"route": AssertionResult("fail", "smart_home", "chat", "wrong intent")},
            error=None,
        )
        assert not r.passed


class TestScenarioResult:
    def test_status_pass(self):
        step = StepResult(
            "x", "y", ["y"], None, "local", [], MemoryDiff([], []),
            100, {}, {"ok": AssertionResult("pass", None, None, None)}, None,
        )
        r = ScenarioResult("test", [step], review=False, review_hint=None)
        assert r.status == "pass"

    def test_status_review(self):
        step = StepResult(
            "x", "y", ["y"], None, "local", [], MemoryDiff([], []),
            100, {}, {"ok": AssertionResult("pass", None, None, None)}, None,
        )
        r = ScenarioResult("test", [step], review=True, review_hint="check quality")
        assert r.status == "review"


class TestRunResult:
    def test_summary(self):
        step_pass = StepResult(
            "x", "y", ["y"], None, "local", [], MemoryDiff([], []),
            100, {"groq": 1}, {"ok": AssertionResult("pass", None, None, None)}, None,
        )
        step_fail = StepResult(
            "x", "y", ["y"], None, "local", [], MemoryDiff([], []),
            100, {"xai": 1}, {"r": AssertionResult("fail", "a", "b", None)}, None,
        )
        s1 = ScenarioResult("a", [step_pass], False, None)
        s2 = ScenarioResult("b", [step_fail], False, None)
        suite = SuiteResult("test", [s1, s2])
        run = RunResult(
            timestamp="2026-04-13T14:30:00",
            duration_s=10.0,
            suites=[suite],
        )
        s = run.summary
        assert s["pass"] == 1
        assert s["fail"] == 1
        assert run.total_api_calls["groq"] == 1
        assert run.total_api_calls["xai"] == 1
