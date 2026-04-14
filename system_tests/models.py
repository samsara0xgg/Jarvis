"""Shared data models for system testing framework."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DeviceChange:
    """One field change on one device."""
    device_id: str
    field: str
    before: Any
    after: Any

    def display(self) -> str:
        def _fmt(v: Any) -> str:
            if isinstance(v, bool):
                return "ON" if v else "OFF"
            return str(v)
        return f"{self.device_id}.{self.field}: {_fmt(self.before)}→{_fmt(self.after)}"


@dataclass
class MemoryChange:
    """One memory added or removed."""
    action: str
    content: str
    category: str | None
    key: str | None


@dataclass
class MemoryDiff:
    """Diff between two memory snapshots."""
    added: list[MemoryChange]
    removed: list[MemoryChange]

    @property
    def is_empty(self) -> bool:
        return not self.added and not self.removed


@dataclass
class AssertionResult:
    """Result of one assertion check."""
    status: str
    expected: Any
    actual: Any
    debug_context: str | None


@dataclass
class StepExpect:
    """Parsed expect block from YAML."""
    route: str | None = None
    tier: str | None = None
    path: str | None = None
    device_state: dict[str, dict[str, Any]] | None = None
    response_contains: list[str] | None = None
    response_not_contains: list[str] | None = None
    memory_contains: dict | None = None
    memory_not_contains: dict | None = None
    latency_max_ms: int | None = None


@dataclass
class TtsInfo:
    """TTS synthesis details for a step."""
    engine: str              # minimax / openai_tts / edge-tts / pyttsx3
    emotion: str             # neutral / happy / sad / ...
    synth_ms: int            # synthesis time (0 if not measured)
    played: bool             # whether audio was actually played

@dataclass
class StepResult:
    """Full result of one test step."""
    input_text: str
    response: str
    sentences: list[str]
    route: Any
    path: str | None
    device_changes: list[DeviceChange]
    memory_diff: MemoryDiff
    latency_ms: int
    api_calls: dict[str, int]
    assertions: dict[str, AssertionResult]
    error: str | None
    tts_info: TtsInfo | None = None
    timings: dict[str, int] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(a.status == "pass" for a in self.assertions.values())


@dataclass
class ScenarioResult:
    """Result of one multi-step scenario."""
    name: str
    steps: list[StepResult]
    review: bool
    review_hint: str | None

    @property
    def status(self) -> str:
        has_failures = any(not s.passed for s in self.steps)
        # review: true 的场景始终进评审，即使有断言失败
        if self.review:
            return "review"
        if has_failures:
            return "fail"
        return "pass"


@dataclass
class SuiteResult:
    """Result of one YAML file (suite of scenarios)."""
    name: str
    scenarios: list[ScenarioResult]


@dataclass
class RunResult:
    """Result of an entire test run."""
    timestamp: str
    duration_s: float
    suites: list[SuiteResult]

    @property
    def summary(self) -> dict[str, int]:
        counts = {"pass": 0, "fail": 0, "review": 0}
        for suite in self.suites:
            for scenario in suite.scenarios:
                counts[scenario.status] += 1
        counts["total"] = sum(counts.values())
        return counts

    @property
    def total_api_calls(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for suite in self.suites:
            for scenario in suite.scenarios:
                for step in scenario.steps:
                    for k, v in step.api_calls.items():
                        totals[k] = totals.get(k, 0) + v
        return totals

    def estimate_cost(self) -> float:
        rates = {"groq": 0.0008, "xai": 0.005, "gpt4o_mini": 0.001}
        return sum(self.total_api_calls.get(k, 0) * v for k, v in rates.items())
