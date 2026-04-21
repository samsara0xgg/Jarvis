"""Unit tests for the system test assertion engine."""
from __future__ import annotations

from unittest.mock import MagicMock

from system_tests.assertions import evaluate
from system_tests.models import (
    DeviceChange,
    MemoryChange,
    MemoryDiff,
    StepExpect,
    StepResult,
)


def _make_step(
    *,
    response: str = "ok",
    route_intent: str = "chat",
    route_tier: str = "cloud",
    route_confidence: float = 0.95,
    path: str = "cloud",
    device_changes: list | None = None,
    memory_diff: MemoryDiff | None = None,
    latency_ms: int = 200,
) -> StepResult:
    route = MagicMock()
    route.intent = route_intent
    route.tier = route_tier
    route.confidence = route_confidence
    return StepResult(
        input_text="test",
        response=response,
        sentences=[response],
        route=route,
        path=path,
        device_changes=device_changes or [],
        memory_diff=memory_diff or MemoryDiff([], []),
        latency_ms=latency_ms,
        api_calls={"groq": 1},
        assertions={},
        error=None,
    )


class TestEvaluateRoute:
    def test_pass(self):
        step = _make_step(route_intent="smart_home")
        expect = StepExpect(route="smart_home")
        results = evaluate(step, expect)
        assert results["route"].status == "pass"

    def test_fail(self):
        step = _make_step(route_intent="chat")
        expect = StepExpect(route="smart_home")
        results = evaluate(step, expect)
        assert results["route"].status == "fail"
        assert results["route"].actual == "chat"


class TestEvaluateTier:
    def test_pass(self):
        step = _make_step(route_tier="local")
        expect = StepExpect(tier="local")
        results = evaluate(step, expect)
        assert results["tier"].status == "pass"

    def test_fail_with_debug(self):
        step = _make_step(route_tier="cloud", route_confidence=0.72)
        expect = StepExpect(tier="local")
        results = evaluate(step, expect)
        assert results["tier"].status == "fail"
        assert "0.72" in results["tier"].debug_context


class TestEvaluatePath:
    def test_pass(self):
        step = _make_step(path="cloud")
        expect = StepExpect(path="cloud")
        results = evaluate(step, expect)
        assert results["path"].status == "pass"


class TestEvaluateDeviceState:
    def test_pass(self):
        step = _make_step(device_changes=[
            DeviceChange("bedroom_light", "is_on", False, True),
        ])
        expect = StepExpect(device_state={"bedroom_light": {"is_on": True}})
        # Need current_device_state for assertion
        results = evaluate(
            step, expect,
            current_device_state={"bedroom_light": {"is_on": True, "brightness": 100}},
        )
        assert results["device_state.bedroom_light.is_on"].status == "pass"

    def test_fail(self):
        step = _make_step()
        expect = StepExpect(device_state={"bedroom_light": {"is_on": True}})
        results = evaluate(
            step, expect,
            current_device_state={"bedroom_light": {"is_on": False, "brightness": 100}},
        )
        assert results["device_state.bedroom_light.is_on"].status == "fail"


class TestEvaluateResponse:
    def test_contains_pass(self):
        step = _make_step(response="好的，卧室灯已打开。")
        expect = StepExpect(response_contains=["灯"])
        results = evaluate(step, expect)
        assert results["response_contains.灯"].status == "pass"

    def test_contains_fail(self):
        step = _make_step(response="好的。")
        expect = StepExpect(response_contains=["灯"])
        results = evaluate(step, expect)
        assert results["response_contains.灯"].status == "fail"

    def test_not_contains(self):
        step = _make_step(response="好的，灯开了")
        expect = StepExpect(response_not_contains=["错误"])
        results = evaluate(step, expect)
        assert results["response_not_contains.错误"].status == "pass"


class TestEvaluateMemory:
    def test_contains_pass(self):
        step = _make_step(
            memory_diff=MemoryDiff(
                added=[MemoryChange("added", "用户喜欢咖啡", "preference", "drink")],
                removed=[],
            ),
        )
        expect = StepExpect(memory_contains={"content_matches": "咖啡"})
        results = evaluate(step, expect)
        assert results["memory_contains"].status == "pass"

    def test_contains_fail(self):
        step = _make_step(memory_diff=MemoryDiff(added=[], removed=[]))
        expect = StepExpect(memory_contains={"content_matches": "咖啡"})
        results = evaluate(step, expect)
        assert results["memory_contains"].status == "fail"


class TestEvaluateLatency:
    def test_pass(self):
        step = _make_step(latency_ms=500)
        expect = StepExpect(latency_max_ms=2000)
        results = evaluate(step, expect)
        assert results["latency"].status == "pass"

    def test_fail(self):
        step = _make_step(latency_ms=3000)
        expect = StepExpect(latency_max_ms=2000)
        results = evaluate(step, expect)
        assert results["latency"].status == "fail"


class TestNoCrash:
    def test_always_present(self):
        step = _make_step()
        results = evaluate(step, StepExpect())
        assert "no_crash" in results
        assert results["no_crash"].status == "pass"

    def test_crash(self):
        step = _make_step()
        step.error = "RuntimeError: something broke"
        results = evaluate(step, StepExpect())
        assert results["no_crash"].status == "fail"
