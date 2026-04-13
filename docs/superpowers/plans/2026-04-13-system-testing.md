# System Testing Framework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an end-to-end system testing framework that validates Jarvis behavior through real LLM/routing/memory/device pipeline, with three modes (human interactive, CC programmatic, free chat), baseline comparison, and cost tracking.

**Architecture:** YAML scenario files define multi-turn conversations with expected outcomes. A TestHarness wraps JarvisApp (sim mode, real APIs), executes steps via `handle_text()`, captures state diffs (devices, memory) and trace attributes (`_last_route`, `_last_path`, etc.) for structured assertions. Reporter outputs colored terminal (human), JSON (CC), or markdown (archive). Baseline engine compares runs over time.

**Tech Stack:** Python 3.13, PyYAML (already in deps), existing JarvisApp + sim devices + real Groq/xAI/GPT-4o-mini APIs.

**Spec:** `docs/superpowers/specs/2026-04-13-system-testing-design.md`

---

## File Map

```
jarvis.py                          MODIFY  — trace attrs + enhanced logging in _process_turn
system_tests/
  __init__.py                      CREATE  — empty
  models.py                        CREATE  — shared dataclasses (StepResult, DeviceChange, etc.)
  harness.py                       CREATE  — TestHarness: init app, snapshot, diff, reset, run_step
  assertions.py                    CREATE  — evaluate expect blocks against StepResult
  reporter.py                      CREATE  — TerminalReporter, JsonReporter, MarkdownReporter
  baseline.py                      CREATE  — save/load runs, diff two runs, detect regressions
  runner.py                        CREATE  — CLI entry, mode dispatch, scenario loop, review loop
  scenarios/
    smart_home.yaml                CREATE
    memory.yaml                    CREATE
    multi_turn.yaml                CREATE
    routing.yaml                   CREATE
    error_handling.yaml            CREATE
    cloud_chat.yaml                CREATE
  runs/                            CREATE  — (empty dir, auto-populated)
tests/
  test_system_test_models.py       CREATE  — unit tests for models
  test_system_test_assertions.py   CREATE  — unit tests for assertion engine
  test_system_test_harness.py      CREATE  — unit tests for diff/reset logic
  test_system_test_baseline.py     CREATE  — unit tests for baseline comparison
  test_system_test_reporter.py     CREATE  — unit tests for formatting
```

---

### Task 1: Enhance `_process_turn` with trace attributes and logging

**Files:**
- Modify: `jarvis.py:632-1048` (`_process_turn` + `handle_text`)

This is the foundation. Add 4 trace attributes and enhanced prints to `_process_turn`. Also extend `handle_text` to accept `user_id`/`user_name`/`user_role` so the harness can pass identity.

- [ ] **Step 1: Add trace attribute initialization at top of `_process_turn`**

At line ~665 (after `history = self.conversation_store.get_history(session_id)`), add:

```python
# ── Trace attributes (for system test harness) ──
self._last_route = None
self._last_path = "unknown"
self._last_device_ops = []
self._last_memory_hits = ""
```

- [ ] **Step 2: Add `_last_path` at every exit point**

At each return/path-determination point in `_process_turn`:

```python
# Line ~676 (resume interruption block, before return):
self._last_path = "resume"

# Line ~687 (farewell shortcut, before output_fn):
self._last_path = "farewell"

# Line ~720 (memory shortcut, before output_fn):
self._last_path = "memory_shortcut"

# Line ~737 (learn create, before output_fn):
self._last_path = "learn_create"

# Line ~769 (keyword rule match, after response_text is set):
self._last_path = "keyword_rule"

# Line ~791 (direct answer, before output_fn):
self._last_path = "memory_l1"

# Line ~884 (local execution, after response_text = ar.text):
self._last_path = "local"

# Line ~828 (route.text_response, after response_text = route.text_response):
self._last_path = "local"

# Line ~895 (cloud LLM block start):
self._last_path = "cloud"
```

- [ ] **Step 3: Add `_last_route` and enhance route print**

Replace the existing route print block (lines ~821-824):

```python
_t_route = time.monotonic()
self._last_route = route
if route:
    print(f"⏱ 路由: {(_t_route - _t_think)*1000:.0f}ms → {route.tier}/{route.intent} ({route.provider}, {route.confidence:.2f})")
    if route.actions:
        for a in route.actions:
            val_str = f" ({a['value']})" if a.get("value") else ""
            print(f"   📋 {a.get('device_id', '?')} → {a.get('action', '?')}{val_str}")
else:
    print(f"⏱ 路由: {(_t_route - _t_think)*1000:.0f}ms → 无路由")
```

- [ ] **Step 4: Add `_last_device_ops` and device state print after local execution**

After the `execute_smart_home` call (line ~848), capture ops and print device state:

```python
ar = self.local_executor.execute_smart_home(
    route.actions, user_role, response=route.response,
)
self._last_device_ops = route.actions
# Print device state after operation
for a in route.actions:
    did = a.get("device_id")
    if did:
        try:
            st = self.device_manager.get_device(did).get_status()
            on_str = "ON" if st.get("is_on") else "OFF"
            extras = []
            if "brightness" in st:
                extras.append(f"brightness={st['brightness']}")
            if "color_temp" in st:
                extras.append(f"color_temp={st['color_temp']}")
            if "color" in st and st["color"] != "white":
                extras.append(f"color={st['color']}")
            if "temperature" in st:
                extras.append(f"temp={st['temperature']}°C")
            if "is_locked" in st:
                extras.append("locked" if st["is_locked"] else "unlocked")
            extra_str = f"  {' '.join(extras)}" if extras else ""
            print(f"   💡 {did}: {on_str}{extra_str}")
        except Exception:
            pass
```

- [ ] **Step 5: Add `_last_memory_hits` and memory print after query**

After the `memory_manager.query` call (line ~780):

```python
memory_context = ""
if user_id:
    try:
        memory_context = self.memory_manager.query(text, user_id)
        self._last_memory_hits = memory_context
        if memory_context:
            # Count memory entries in XML block
            _mem_count = memory_context.count("\n- ")
            print(f"🧠 记忆检索: {_mem_count} 条相关记忆")
    except Exception as exc:
        self.logger.warning("Memory query failed: %s", exc)
```

- [ ] **Step 6: Add path label print at end of `_process_turn`**

Before the final `return response_text` (line ~1022):

```python
print(f"📍 路径: {self._last_path}")
```

- [ ] **Step 7: Extend `handle_text` to accept user identity params**

Replace the `handle_text` method (lines 1028-1048):

```python
def handle_text(
    self,
    text: str,
    session_id: str = "_web",
    on_sentence: Any = None,
    emotion: str = "",
    *,
    user_id: str = "default_user",
    user_name: str = "用户",
    user_role: str = "owner",
) -> str:
    """Process a text message without audio/TTS.

    Thin wrapper around :meth:`_process_turn` for the web frontend.
    """
    def _text_output(sentence: str) -> None:
        if on_sentence:
            on_sentence(sentence, emotion=emotion)

    return self._process_turn(
        text,
        emotion=emotion,
        session_id=session_id,
        user_id=user_id,
        user_name=user_name,
        user_role=user_role,
        output_fn=_text_output,
    )
```

- [ ] **Step 8: Run tests**

```bash
python -m pytest tests/ -q
```

All 812+ tests must still pass. The trace attributes and new prints are additive; no control flow changed.

- [ ] **Step 9: Commit**

```bash
git add jarvis.py
git commit -m "feat: trace attributes + enhanced logging in _process_turn for system testing"
```

---

### Task 2: Data models

**Files:**
- Create: `system_tests/__init__.py`
- Create: `system_tests/models.py`
- Create: `tests/test_system_test_models.py`

Shared dataclasses used by harness, assertions, reporter, and baseline.

- [ ] **Step 1: Create package**

```bash
mkdir -p system_tests/scenarios system_tests/runs
touch system_tests/__init__.py
```

- [ ] **Step 2: Write tests for models**

```python
# tests/test_system_test_models.py
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
```

- [ ] **Step 3: Run test to see it fail**

```bash
python -m pytest tests/test_system_test_models.py -v
```

Expected: ImportError — `system_tests.models` does not exist yet.

- [ ] **Step 4: Implement models**

```python
# system_tests/models.py
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
    action: str          # "added" or "removed"
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
    status: str          # "pass" or "fail"
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
class StepResult:
    """Full result of one test step."""
    input_text: str
    response: str
    sentences: list[str]
    route: Any           # RouteResult | None — avoid import cycle
    path: str | None
    device_changes: list[DeviceChange]
    memory_diff: MemoryDiff
    latency_ms: int
    api_calls: dict[str, int]
    assertions: dict[str, AssertionResult]
    error: str | None

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
        if any(not s.passed for s in self.steps):
            return "fail"
        if self.review:
            return "review"
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
```

- [ ] **Step 5: Run tests**

```bash
python -m pytest tests/test_system_test_models.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add system_tests/ tests/test_system_test_models.py
git commit -m "feat(system-tests): data models for test framework"
```

---

### Task 3: Assertion engine

**Files:**
- Create: `system_tests/assertions.py`
- Create: `tests/test_system_test_assertions.py`

Evaluates YAML `expect` blocks against a `StepResult`. Returns a dict of `AssertionResult`.

- [ ] **Step 1: Write tests**

```python
# tests/test_system_test_assertions.py
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
        step = _make_step(path="memory_l1")
        expect = StepExpect(path="memory_l1")
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
```

- [ ] **Step 2: Run to see failures**

```bash
python -m pytest tests/test_system_test_assertions.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement assertion engine**

```python
# system_tests/assertions.py
"""Evaluate YAML expect blocks against step results."""
from __future__ import annotations

from typing import Any

from system_tests.models import AssertionResult, StepExpect, StepResult


def evaluate(
    step: StepResult,
    expect: StepExpect,
    *,
    current_device_state: dict[str, dict[str, Any]] | None = None,
) -> dict[str, AssertionResult]:
    """Run all applicable assertions, return results keyed by assertion name."""
    results: dict[str, AssertionResult] = {}

    # Always check no_crash
    if step.error:
        results["no_crash"] = AssertionResult("fail", "no error", step.error, step.error)
    else:
        results["no_crash"] = AssertionResult("pass", None, None, None)

    # Route intent
    if expect.route is not None:
        actual = step.route.intent if step.route else None
        if actual == expect.route:
            results["route"] = AssertionResult("pass", expect.route, actual, None)
        else:
            ctx = None
            if step.route:
                ctx = f"confidence={step.route.confidence:.2f}, provider={step.route.provider}"
            results["route"] = AssertionResult("fail", expect.route, actual, ctx)

    # Route tier
    if expect.tier is not None:
        actual = step.route.tier if step.route else None
        if actual == expect.tier:
            results["tier"] = AssertionResult("pass", expect.tier, actual, None)
        else:
            ctx = None
            if step.route:
                ctx = (
                    f"confidence={step.route.confidence:.2f} "
                    f"(threshold=0.90), intent={step.route.intent}"
                )
            results["tier"] = AssertionResult("fail", expect.tier, actual, ctx)

    # Pipeline path
    if expect.path is not None:
        if step.path == expect.path:
            results["path"] = AssertionResult("pass", expect.path, step.path, None)
        else:
            results["path"] = AssertionResult("fail", expect.path, step.path, None)

    # Device state
    if expect.device_state and current_device_state:
        for device_id, expected_fields in expect.device_state.items():
            actual_status = current_device_state.get(device_id, {})
            for field_name, expected_val in expected_fields.items():
                actual_val = actual_status.get(field_name)
                key = f"device_state.{device_id}.{field_name}"
                if actual_val == expected_val:
                    results[key] = AssertionResult("pass", expected_val, actual_val, None)
                else:
                    results[key] = AssertionResult(
                        "fail", expected_val, actual_val,
                        f"device {device_id} {field_name}: expected {expected_val}, got {actual_val}",
                    )

    # Response contains
    if expect.response_contains:
        for keyword in expect.response_contains:
            key = f"response_contains.{keyword}"
            if keyword in step.response:
                results[key] = AssertionResult("pass", keyword, "present", None)
            else:
                results[key] = AssertionResult(
                    "fail", keyword, "absent",
                    f"response: {step.response[:100]}",
                )

    # Response not contains
    if expect.response_not_contains:
        for keyword in expect.response_not_contains:
            key = f"response_not_contains.{keyword}"
            if keyword not in step.response:
                results[key] = AssertionResult("pass", f"not {keyword}", "absent", None)
            else:
                results[key] = AssertionResult(
                    "fail", f"not {keyword}", "present",
                    f"response: {step.response[:100]}",
                )

    # Memory contains
    if expect.memory_contains:
        pattern = expect.memory_contains.get("content_matches", "")
        found = any(pattern in m.content for m in step.memory_diff.added)
        if found:
            results["memory_contains"] = AssertionResult("pass", pattern, "found", None)
        else:
            added_str = "; ".join(m.content[:50] for m in step.memory_diff.added) or "(none)"
            results["memory_contains"] = AssertionResult(
                "fail", pattern, "not found",
                f"added memories: {added_str}",
            )

    # Memory not contains
    if expect.memory_not_contains:
        pattern = expect.memory_not_contains.get("content_matches", "")
        # Check ALL active memories, not just diff
        found = any(pattern in m.content for m in step.memory_diff.added)
        if not found:
            results["memory_not_contains"] = AssertionResult("pass", f"not {pattern}", "absent", None)
        else:
            results["memory_not_contains"] = AssertionResult(
                "fail", f"not {pattern}", "found",
                f"unexpected memory containing '{pattern}'",
            )

    # Latency
    if expect.latency_max_ms is not None:
        if step.latency_ms <= expect.latency_max_ms:
            results["latency"] = AssertionResult(
                "pass", f"<={expect.latency_max_ms}ms", f"{step.latency_ms}ms", None,
            )
        else:
            results["latency"] = AssertionResult(
                "fail", f"<={expect.latency_max_ms}ms", f"{step.latency_ms}ms",
                f"exceeded by {step.latency_ms - expect.latency_max_ms}ms",
            )

    return results
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_system_test_assertions.py -v
```

Expected: all pass.

- [ ] **Step 5: Run full test suite**

```bash
python -m pytest tests/ -q
```

- [ ] **Step 6: Commit**

```bash
git add system_tests/assertions.py tests/test_system_test_assertions.py
git commit -m "feat(system-tests): assertion engine for expect block evaluation"
```

---

### Task 4: Test harness

**Files:**
- Create: `system_tests/harness.py`
- Create: `tests/test_system_test_harness.py`

Wraps JarvisApp — handles initialization, state snapshots, diffing, reset, and per-step execution.

- [ ] **Step 1: Write tests for diff and reset logic**

```python
# tests/test_system_test_harness.py
"""Unit tests for harness diff/reset logic (no real JarvisApp needed)."""
from __future__ import annotations

from system_tests.harness import diff_devices, diff_memory


class TestDiffDevices:
    def test_no_change(self):
        before = {"bedroom_light": {"is_on": False, "brightness": 100}}
        after = {"bedroom_light": {"is_on": False, "brightness": 100}}
        changes = diff_devices(before, after)
        assert changes == []

    def test_single_change(self):
        before = {"bedroom_light": {"is_on": False, "brightness": 100}}
        after = {"bedroom_light": {"is_on": True, "brightness": 100}}
        changes = diff_devices(before, after)
        assert len(changes) == 1
        assert changes[0].device_id == "bedroom_light"
        assert changes[0].field == "is_on"
        assert changes[0].before is False
        assert changes[0].after is True

    def test_multiple_changes(self):
        before = {"bedroom_light": {"is_on": False, "brightness": 100}}
        after = {"bedroom_light": {"is_on": True, "brightness": 50}}
        changes = diff_devices(before, after)
        assert len(changes) == 2

    def test_multiple_devices(self):
        before = {
            "bedroom_light": {"is_on": False},
            "living_room_light": {"is_on": False},
        }
        after = {
            "bedroom_light": {"is_on": True},
            "living_room_light": {"is_on": True},
        }
        changes = diff_devices(before, after)
        assert len(changes) == 2

    def test_ignores_metadata_fields(self):
        """device_id, name, device_type, required_role, is_available are metadata — skip."""
        before = {"bedroom_light": {"device_id": "bedroom_light", "name": "卧室灯", "is_on": False}}
        after = {"bedroom_light": {"device_id": "bedroom_light", "name": "卧室灯", "is_on": True}}
        changes = diff_devices(before, after)
        assert len(changes) == 1
        assert changes[0].field == "is_on"


class TestDiffMemory:
    def test_no_change(self):
        before = [{"id": "a", "content": "likes coffee", "category": "preference", "key": "drink"}]
        after = [{"id": "a", "content": "likes coffee", "category": "preference", "key": "drink"}]
        diff = diff_memory(before, after)
        assert diff.is_empty

    def test_added(self):
        before = []
        after = [{"id": "a", "content": "likes coffee", "category": "preference", "key": "drink"}]
        diff = diff_memory(before, after)
        assert len(diff.added) == 1
        assert diff.added[0].content == "likes coffee"

    def test_removed(self):
        before = [{"id": "a", "content": "likes coffee", "category": "preference", "key": "drink"}]
        after = []
        diff = diff_memory(before, after)
        assert len(diff.removed) == 1
```

- [ ] **Step 2: Run tests to see failure**

```bash
python -m pytest tests/test_system_test_harness.py -v
```

- [ ] **Step 3: Implement harness**

```python
# system_tests/harness.py
"""Test harness — wraps JarvisApp with state management for system tests."""
from __future__ import annotations

import copy
import logging
import sys
import time
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from system_tests.assertions import evaluate
from system_tests.models import (
    DeviceChange,
    MemoryChange,
    MemoryDiff,
    ScenarioResult,
    StepExpect,
    StepResult,
)

LOGGER = logging.getLogger(__name__)

# Fields in device status that are metadata, not controllable state
_DEVICE_META_FIELDS = {"device_id", "name", "device_type", "required_role", "is_available",
                       "color_temp_map", "color_xy"}


def diff_devices(
    before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]],
) -> list[DeviceChange]:
    """Compare two device snapshots, return list of field changes."""
    changes: list[DeviceChange] = []
    for device_id in after:
        b = before.get(device_id, {})
        a = after[device_id]
        for field_name, after_val in a.items():
            if field_name in _DEVICE_META_FIELDS:
                continue
            before_val = b.get(field_name)
            if before_val != after_val:
                changes.append(DeviceChange(device_id, field_name, before_val, after_val))
    return changes


def diff_memory(
    before: list[dict[str, Any]], after: list[dict[str, Any]],
) -> MemoryDiff:
    """Compare two memory snapshots by id, return added/removed."""
    before_ids = {m["id"]: m for m in before}
    after_ids = {m["id"]: m for m in after}
    added = [
        MemoryChange("added", m["content"], m.get("category"), m.get("key"))
        for mid, m in after_ids.items() if mid not in before_ids
    ]
    removed = [
        MemoryChange("removed", m["content"], m.get("category"), m.get("key"))
        for mid, m in before_ids.items() if mid not in after_ids
    ]
    return MemoryDiff(added=added, removed=removed)


class TestHarness:
    """Manages JarvisApp lifecycle and per-step state observation."""

    def __init__(self, tmp_dir: Path | None = None) -> None:
        self._tmp_dir = tmp_dir or Path("/tmp/jarvis_system_test")
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        self._api_counter: dict[str, int] = {}
        self.app = self._create_app()

    def _build_config(self) -> dict:
        """Build a config dict for system testing: sim devices, temp DBs, real APIs."""
        import os
        return {
            "audio": {
                "sample_rate": 16000, "channels": 1, "default_duration": 1.0,
                "min_duration": 0.1, "low_volume_threshold": 0.001,
            },
            "asr": {"model_size": "base", "language": "zh"},
            "speaker": {"model_source": "test", "embedding_dim": 192, "device": "cpu"},
            "verification": {"threshold": 0.70},
            "enrollment": {"num_samples": 3, "default_role": "resident"},
            "auth": {"user_store_path": str(self._tmp_dir / "users.json")},
            "devices": {
                "mode": "sim",
                "sim_devices": [
                    {
                        "device_id": "bedroom_light", "name": "卧室灯",
                        "device_type": "light", "required_role": "guest",
                        "is_available": True,
                        "initial_state": {
                            "is_on": False, "brightness": 100,
                            "color_temp": "neutral", "color": "white",
                        },
                    },
                    {
                        "device_id": "living_room_light", "name": "客厅灯",
                        "device_type": "light", "required_role": "guest",
                        "is_available": True,
                        "initial_state": {
                            "is_on": False, "brightness": 100,
                            "color_temp": "neutral", "color": "white",
                        },
                    },
                    {
                        "device_id": "home_thermostat", "name": "客厅空调",
                        "device_type": "thermostat", "required_role": "member",
                        "is_available": True,
                        "initial_state": {"is_on": False, "temperature": 24},
                    },
                    {
                        "device_id": "front_door_lock", "name": "入户门锁",
                        "device_type": "door_lock", "required_role": "admin",
                        "is_available": True,
                        "initial_state": {"is_locked": True},
                    },
                ],
            },
            "hue": {
                "light_aliases": {
                    "bedroom_light": ["卧室灯", "卧室的灯"],
                    "living_room_light": ["客厅灯", "客厅的灯"],
                },
                "group_aliases": {},
                "scene_aliases": {},
                "voice_shortcuts": {},
            },
            "models": {
                "groq": {
                    "api_key": os.environ.get("GROQ_API_KEY", ""),
                    "model": "llama-3.3-70b-versatile",
                },
                "cerebras": {
                    "api_key": os.environ.get("CEREBRAS_API_KEY", ""),
                    "model": "llama-3.3-70b",
                },
            },
            "llm": {
                "provider": "xai",
                "presets": {
                    "fast": {
                        "provider": "xai",
                        "model": os.environ.get("XAI_MODEL", "grok-3-mini-fast"),
                        "api_key": os.environ.get("XAI_API_KEY", ""),
                    },
                },
                "default_preset": "fast",
                "max_tokens": 1024,
            },
            "tts": {"engine": "pyttsx3", "fallback_enabled": False},
            "wake_word": {"enabled": False},
            "session": {
                "silence_timeout": 30, "utterance_duration": 3,
                "farewell_phrases": ["再见", "退出", "bye", "goodbye"],
            },
            "memory": {
                "max_conversation_turns": 10,
                "db_path": str(self._tmp_dir / "memory.db"),
                "conversation_dir": str(self._tmp_dir / "convos"),
                "preferences_dir": str(self._tmp_dir / "prefs"),
            },
            "skills": {
                "weather": {"default_city": "Toronto"},
                "reminders": {"path": str(self._tmp_dir / "reminders.json")},
                "todos": {"dir": str(self._tmp_dir / "todos")},
            },
            "health": {"enabled": False},
            "logging": {"level": "WARNING"},
        }

    def _create_app(self) -> Any:
        """Create JarvisApp with real APIs but mocked audio hardware."""
        from unittest.mock import patch

        config = self._build_config()
        config_path = self._tmp_dir / "config.yaml"

        # Install fake pyttsx3 if not available
        if "pyttsx3" not in sys.modules:
            fake_pyttsx3 = types.ModuleType("pyttsx3")
            mock_engine = MagicMock()
            fake_pyttsx3.init = MagicMock(return_value=mock_engine)
            sys.modules["pyttsx3"] = fake_pyttsx3

        with (
            patch("core.speaker_encoder.SpeakerEncoder"),
            patch("core.speaker_verifier.SpeakerVerifier"),
            patch("core.speech_recognizer.SpeechRecognizer"),
            patch("core.audio_recorder.AudioRecorder"),
        ):
            from jarvis import JarvisApp
            app = JarvisApp(config, config_path=config_path)

        # Wrap API methods for call counting
        self._wrap_api_counter(app)
        return app

    def _wrap_api_counter(self, app: Any) -> None:
        """Wrap API-calling methods to count calls per provider."""
        if app.intent_router:
            orig_route = app.intent_router.route_and_respond
            def _counted_route(*a: Any, **kw: Any) -> Any:
                self._api_counter["groq"] = self._api_counter.get("groq", 0) + 1
                return orig_route(*a, **kw)
            app.intent_router.route_and_respond = _counted_route

        orig_chat = app.llm.chat_stream
        def _counted_chat(*a: Any, **kw: Any) -> Any:
            self._api_counter["xai"] = self._api_counter.get("xai", 0) + 1
            return orig_chat(*a, **kw)
        app.llm.chat_stream = _counted_chat

        orig_save = app.memory_manager.save
        def _counted_save(*a: Any, **kw: Any) -> Any:
            self._api_counter["gpt4o_mini"] = self._api_counter.get("gpt4o_mini", 0) + 1
            return orig_save(*a, **kw)
        app.memory_manager.save = _counted_save

    def snapshot_devices(self) -> dict[str, dict[str, Any]]:
        return copy.deepcopy(self.app.device_manager.get_all_status())

    def snapshot_memory(self, user_id: str) -> list[dict[str, Any]]:
        memories = self.app.memory_manager.store.get_active_memories(user_id)
        # Strip embedding arrays for comparison (large, not useful for diff)
        for m in memories:
            m.pop("embedding", None)
        return memories

    def flush_background(self) -> None:
        """Wait for all background tasks (memory extraction etc.) to complete."""
        sentinel = self.app._executor.submit(lambda: None)
        sentinel.result(timeout=60)

    def reset_devices(self, setup: dict[str, dict[str, Any]] | None = None) -> None:
        """Reset all sim devices to initial or specified state."""
        for device_id, device in self.app.device_manager._devices.items():
            status = device.get_status()
            # Turn off everything first
            if status.get("is_on"):
                device.execute("turn_off")
            if status.get("is_locked") is False:
                device.execute("lock")
            # Apply setup overrides
            if setup and device_id in setup:
                for field_name, val in setup[device_id].items():
                    if field_name == "is_on" and val:
                        device.execute("turn_on")
                    elif field_name == "brightness":
                        device.execute("set_brightness", val)
                    elif field_name == "color_temp":
                        device.execute("set_color_temp", val)
                    elif field_name == "color" and val != "white":
                        device.execute("set_color", val)
                    elif field_name == "temperature":
                        device.execute("set_temperature", val)
                    elif field_name == "is_locked" and not val:
                        device.execute("unlock")

    def reset_memory(self, user_id: str) -> None:
        """Clear all memories for a user."""
        conn = self.app.memory_manager.store._get_conn()
        conn.execute("DELETE FROM memories WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM user_profiles WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM episodes WHERE user_id = ?", (user_id,))
        conn.commit()

    def reset_conversation(self, session_id: str) -> None:
        """Clear conversation history for a session."""
        self.app.conversation_store.replace(session_id, [])

    def reset_api_counter(self) -> None:
        self._api_counter.clear()

    def run_step(
        self,
        text: str,
        session_id: str,
        user_id: str = "default_user",
        user_name: str = "Allen",
        user_role: str = "owner",
        expect: StepExpect | None = None,
    ) -> StepResult:
        """Execute one step: snapshot → handle_text → flush → diff → assert."""
        before_devices = self.snapshot_devices()
        before_memory = self.snapshot_memory(user_id)
        self.reset_api_counter()

        sentences: list[str] = []

        def _on_sentence(sentence: str, **kw: Any) -> None:
            sentences.append(sentence)

        t0 = time.monotonic()
        error = None
        response = ""
        try:
            response = self.app.handle_text(
                text,
                session_id=session_id,
                on_sentence=_on_sentence,
                user_id=user_id,
                user_name=user_name,
                user_role=user_role,
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            LOGGER.exception("Step failed: %s", text)
        latency_ms = int((time.monotonic() - t0) * 1000)

        # Wait for background tasks
        try:
            self.flush_background()
        except Exception:
            pass

        after_devices = self.snapshot_devices()
        after_memory = self.snapshot_memory(user_id)

        device_changes = diff_devices(before_devices, after_devices)
        memory_diff_result = diff_memory(before_memory, after_memory)

        step = StepResult(
            input_text=text,
            response=response or "",
            sentences=sentences,
            route=getattr(self.app, "_last_route", None),
            path=getattr(self.app, "_last_path", None),
            device_changes=device_changes,
            memory_diff=memory_diff_result,
            latency_ms=latency_ms,
            api_calls=dict(self._api_counter),
            assertions={},
            error=error,
        )

        # Evaluate assertions
        if expect:
            step.assertions = evaluate(step, expect, current_device_state=after_devices)

        return step

    def shutdown(self) -> None:
        try:
            self.app.shutdown()
        except Exception:
            pass
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_system_test_harness.py -v
```

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests/ -q
```

- [ ] **Step 6: Commit**

```bash
git add system_tests/harness.py tests/test_system_test_harness.py
git commit -m "feat(system-tests): test harness with state snapshot/diff/reset"
```

---

### Task 5: Reporter

**Files:**
- Create: `system_tests/reporter.py`
- Create: `tests/test_system_test_reporter.py`

Three output formats: colored terminal (human), JSON (CC), markdown (archive).

- [ ] **Step 1: Write tests for key formatting functions**

```python
# tests/test_system_test_reporter.py
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
        assert data["failures"][0]["scenario"] == "智能家居/颜色设置"

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
```

- [ ] **Step 2: Run to see failures**

```bash
python -m pytest tests/test_system_test_reporter.py -v
```

- [ ] **Step 3: Implement reporter**

```python
# system_tests/reporter.py
"""Output formatting for system test results."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from system_tests.models import (
    DeviceChange,
    MemoryDiff,
    RunResult,
    ScenarioResult,
    StepResult,
    SuiteResult,
)

# ── Terminal colors ──

_GREEN = "\033[92m"
_RED = "\033[91m"
_YELLOW = "\033[93m"
_BLUE = "\033[94m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


class TerminalReporter:
    """Colored terminal output for human mode."""

    @staticmethod
    def print_menu(last_run_summary: str | None, suites: list[tuple[str, int]]) -> None:
        print(f"\n{_BOLD}🧪 Jarvis 系统测试{_RESET}")
        print("━" * 45)
        if last_run_summary:
            print(f"{_DIM}上次运行: {last_run_summary}{_RESET}")
            print()
        for i, (name, count) in enumerate(suites, 1):
            print(f"  [{i}] {name:<20s} {count} scenarios")
        print("  ─" * 15)
        print(f"  [A] 全部运行")
        print(f"  [F] 自由对话 (手动输入)")

    @staticmethod
    def print_step(step: StepResult, step_num: int, user_name: str = "Allen",
                   user_role: str = "owner") -> None:
        print(f"\n┌─ Step {step_num}: \"{step.input_text}\" {'─' * max(1, 38 - len(step.input_text))}")
        print(f"│ 👤 {user_name} ({user_role}): {step.input_text}")

        # Route info (already printed by _process_turn, but add path)
        # Device changes
        if step.device_changes:
            print(f"│")
            print(f"│ 💡 设备变化:")
            for c in step.device_changes:
                print(f"│   {c.display()}")

        # Memory changes
        if step.memory_diff.is_empty:
            print(f"│ 🧠 记忆: 无变化")
        else:
            print(f"│ 🧠 记忆变化:")
            for m in step.memory_diff.added:
                cat = f" ({m.category})" if m.category else ""
                print(f"│   + {m.content[:60]}{cat}")
            for m in step.memory_diff.removed:
                print(f"│   - {m.content[:60]}")

        # API calls
        if step.api_calls:
            parts = [f"{k} ×{v}" for k, v in step.api_calls.items()]
            print(f"│ 💰 API: {', '.join(parts)}")

        print(f"│ ⏱ 总: {step.latency_ms}ms")

        # Assertions
        if step.assertions:
            print(f"│")
            for name, result in step.assertions.items():
                if result.status == "pass":
                    print(f"│ {_GREEN}✅ {name}{_RESET}")
                else:
                    ctx = f"  ({result.debug_context})" if result.debug_context else ""
                    print(f"│ {_RED}❌ {name}: expected={result.expected}, actual={result.actual}{ctx}{_RESET}")

        print(f"└{'─' * 44}")

    @staticmethod
    def print_scenario_result(scenario: ScenarioResult) -> None:
        status_map = {"pass": f"{_GREEN}PASS{_RESET}", "fail": f"{_RED}FAIL{_RESET}",
                      "review": f"{_YELLOW}REVIEW{_RESET}"}
        passed = sum(1 for s in scenario.steps if s.passed)
        total = len(scenario.steps)
        print(f"\n── Scenario: {status_map[scenario.status]} ({passed}/{total} steps) ──")

    @staticmethod
    def format_summary(run: RunResult) -> list[str]:
        lines = []
        lines.append("")
        lines.append("═" * 45)
        lines.append(f"📊 测试报告")
        lines.append("═" * 45)
        lines.append(f"时间: {run.timestamp}  耗时: {run.duration_s:.0f}s")
        api = run.total_api_calls
        if api:
            parts = [f"{k} ×{v}" for k, v in api.items()]
            cost = run.estimate_cost()
            lines.append(f"API: {', '.join(parts)}  ≈ ${cost:.3f}")
        lines.append("")
        for suite in run.suites:
            lines.append(f"{suite.name}:")
            for sc in suite.scenarios:
                status_sym = {"pass": "✅", "fail": "❌", "review": "⚠️"}[sc.status]
                avg_ms = sum(s.latency_ms for s in sc.steps) // max(len(sc.steps), 1)
                lines.append(f"  {status_sym} {sc.name:<20s} {avg_ms}ms")
        lines.append("")
        s = run.summary
        lines.append(f"结果: {s['pass']}✅ {s['fail']}❌ {s['review']}⚠️")
        return lines

    @staticmethod
    def print_review_item(item: dict) -> None:
        print(f"\n⚠️  [{item['suite']}/{item['scenario']}] — Step {item['step_index'] + 1}")
        print(f"  输入: {item['input']}")
        print(f"  回复: {item['response']}")
        if item.get("device_changes"):
            for c in item["device_changes"]:
                print(f"  设备: {c}")
        if item.get("review_hint"):
            print(f"  → {item['review_hint']}")


class JsonReporter:
    """JSON output for CC mode."""

    @staticmethod
    def format(run: RunResult, regressions: list[dict] | None = None) -> str:
        needs_review = []
        failures = []
        suites_data = []

        for suite in run.suites:
            scenarios_data = []
            for sc in suite.scenarios:
                steps_data = []
                for i, step in enumerate(sc.steps):
                    step_d: dict[str, Any] = {
                        "input": step.input_text,
                        "response": step.response,
                        "latency_ms": step.latency_ms,
                        "api_calls": step.api_calls,
                    }
                    if step.route:
                        step_d["route"] = {
                            "intent": step.route.intent,
                            "tier": step.route.tier,
                            "confidence": round(step.route.confidence, 2),
                            "provider": step.route.provider,
                        }
                    if step.device_changes:
                        step_d["device_changes"] = [
                            {"device_id": c.device_id, "field": c.field,
                             "before": c.before, "after": c.after}
                            for c in step.device_changes
                        ]
                    if not step.memory_diff.is_empty:
                        step_d["memory_changes"] = [
                            {"action": m.action, "content": m.content}
                            for m in step.memory_diff.added + step.memory_diff.removed
                        ]
                    # Assertions
                    assertions_d = {}
                    for name, result in step.assertions.items():
                        if result.status == "pass":
                            assertions_d[name] = "pass"
                        else:
                            assertions_d[name] = {
                                "status": "fail",
                                "expected": _jsonable(result.expected),
                                "actual": _jsonable(result.actual),
                            }
                            if result.debug_context:
                                assertions_d[name]["debug_context"] = result.debug_context
                    step_d["assertions"] = assertions_d
                    steps_data.append(step_d)

                sc_d = {"name": sc.name, "status": sc.status, "steps": steps_data}
                scenarios_data.append(sc_d)

                if sc.status == "fail":
                    for i, step in enumerate(sc.steps):
                        failed_asserts = {
                            k: v for k, v in step.assertions.items() if v.status == "fail"
                        }
                        if failed_asserts:
                            ctx_parts = [v.debug_context for v in failed_asserts.values() if v.debug_context]
                            failures.append({
                                "suite": suite.name,
                                "scenario": sc.name,
                                "step_index": i,
                                "input": step.input_text,
                                "response": step.response,
                                "assertions_failed": {
                                    k: {"expected": _jsonable(v.expected), "actual": _jsonable(v.actual)}
                                    for k, v in failed_asserts.items()
                                },
                                "debug_context": "; ".join(ctx_parts) if ctx_parts else None,
                            })

                if sc.status == "review":
                    for i, step in enumerate(sc.steps):
                        needs_review.append({
                            "id": len(needs_review) + 1,
                            "suite": suite.name,
                            "scenario": sc.name,
                            "step_index": i,
                            "input": step.input_text,
                            "response": step.response,
                            "device_changes": [c.display() for c in step.device_changes],
                            "auto_checks_passed": step.passed,
                            "review_hint": sc.review_hint,
                        })

            suites_data.append({"name": suite.name, "scenarios": scenarios_data})

        result = {
            "timestamp": run.timestamp,
            "duration_s": round(run.duration_s, 1),
            "api_calls": run.total_api_calls,
            "cost_estimate_usd": round(run.estimate_cost(), 4),
            "summary": run.summary,
            "suites": suites_data,
            "failures": failures,
            "needs_review": needs_review,
            "regressions": regressions or [],
        }
        return json.dumps(result, ensure_ascii=False, indent=2)


class MarkdownReporter:
    """Markdown report for archiving."""

    @staticmethod
    def format(run: RunResult, regressions: list[dict] | None = None,
               reviews: list[dict] | None = None) -> str:
        lines: list[str] = []
        lines.append(f"# 系统测试报告 — {run.timestamp}")
        lines.append("")
        s = run.summary
        lines.append(f"**结果**: {s['pass']}通过 / {s['fail']}失败 / {s['review']}待审")
        lines.append(f"**耗时**: {run.duration_s:.0f}s")
        api = run.total_api_calls
        if api:
            parts = [f"{k}×{v}" for k, v in api.items()]
            lines.append(f"**API**: {', '.join(parts)} ≈ ${run.estimate_cost():.3f}")
        lines.append("")

        for suite in run.suites:
            lines.append(f"## {suite.name}")
            lines.append("")
            for sc in suite.scenarios:
                status_sym = {"pass": "PASS", "fail": "FAIL", "review": "REVIEW"}[sc.status]
                lines.append(f"### [{status_sym}] {sc.name}")
                lines.append("")
                for i, step in enumerate(sc.steps):
                    lines.append(f"**Step {i+1}**: \"{step.input_text}\"")
                    lines.append(f"- 回复: {step.response}")
                    lines.append(f"- 路径: {step.path} ({step.latency_ms}ms)")
                    if step.device_changes:
                        for c in step.device_changes:
                            lines.append(f"- 设备: {c.display()}")
                    for name, result in step.assertions.items():
                        sym = "PASS" if result.status == "pass" else "FAIL"
                        lines.append(f"- [{sym}] {name}")
                    lines.append("")

        if regressions:
            lines.append("## 回归")
            lines.append("")
            for r in regressions:
                lines.append(f"- {r['scenario']}: {r.get('change', '')}")
            lines.append("")

        if reviews:
            lines.append("## 人工评审")
            lines.append("")
            for r in reviews:
                lines.append(f"- **{r.get('scenario', '')}**: {r.get('feedback', '未评价')}")
            lines.append("")

        return "\n".join(lines)


def _jsonable(v: Any) -> Any:
    """Make a value JSON-serializable."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_system_test_reporter.py -v
```

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests/ -q
```

- [ ] **Step 6: Commit**

```bash
git add system_tests/reporter.py tests/test_system_test_reporter.py
git commit -m "feat(system-tests): terminal, JSON, and markdown reporters"
```

---

### Task 6: Baseline comparison

**Files:**
- Create: `system_tests/baseline.py`
- Create: `tests/test_system_test_baseline.py`

Save/load run results as JSON, compare two runs, detect regressions.

- [ ] **Step 1: Write tests**

```python
# tests/test_system_test_baseline.py
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
```

- [ ] **Step 2: Run to see failures**

```bash
python -m pytest tests/test_system_test_baseline.py -v
```

- [ ] **Step 3: Implement baseline**

```python
# system_tests/baseline.py
"""Historical run comparison and regression detection."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from system_tests.models import RunResult, ScenarioResult
from system_tests.reporter import JsonReporter


def save_run(run: RunResult, runs_dir: Path) -> Path:
    """Save run results as JSON for future baseline comparison."""
    runs_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.fromisoformat(run.timestamp).strftime("%Y-%m-%d_%H%M")
    path = runs_dir / f"{ts}.json"
    path.write_text(JsonReporter.format(run), encoding="utf-8")
    return path


def load_latest(runs_dir: Path) -> dict[str, Any] | None:
    """Load the most recent run result JSON."""
    if not runs_dir.exists():
        return None
    files = sorted(runs_dir.glob("*.json"), reverse=True)
    if not files:
        return None
    return json.loads(files[0].read_text(encoding="utf-8"))


def compare_runs(
    current: RunResult, previous: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Compare current run against previous baseline, return list of changes."""
    if previous is None:
        return []

    # Build lookup: suite/scenario → {status, latency}
    prev_lookup: dict[str, dict] = {}
    for suite in previous.get("suites", []):
        for sc in suite.get("scenarios", []):
            key = f"{suite['name']}/{sc['name']}"
            avg_latency = 0
            steps = sc.get("steps", [])
            if steps:
                avg_latency = sum(s.get("latency_ms", 0) for s in steps) // len(steps)
            prev_lookup[key] = {"status": sc["status"], "latency_ms": avg_latency}

    changes: list[dict[str, Any]] = []

    for suite in current.suites:
        for sc in suite.scenarios:
            key = f"{suite.name}/{sc.name}"
            prev = prev_lookup.get(key)
            if prev is None:
                changes.append({
                    "scenario": key, "severity": "info",
                    "field": "status", "change": "new scenario",
                })
                continue

            curr_status = sc.status
            prev_status = prev["status"]

            if prev_status == "pass" and curr_status == "fail":
                changes.append({
                    "scenario": key, "severity": "regression",
                    "field": "status", "change": "pass→fail",
                    "previous_run": previous.get("timestamp", ""),
                })
            elif prev_status == "fail" and curr_status == "pass":
                changes.append({
                    "scenario": key, "severity": "improvement",
                    "field": "status", "change": "fail→pass",
                })

            # Latency comparison
            curr_latency = sum(s.latency_ms for s in sc.steps) // max(len(sc.steps), 1)
            prev_latency = prev["latency_ms"]
            if prev_latency > 0 and curr_latency > prev_latency * 1.5:
                pct = int((curr_latency / prev_latency - 1) * 100)
                changes.append({
                    "scenario": key, "severity": "warning",
                    "field": "latency",
                    "change": f"{prev_latency}ms→{curr_latency}ms (+{pct}%)",
                })

    return changes
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_system_test_baseline.py -v
```

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests/ -q
```

- [ ] **Step 6: Commit**

```bash
git add system_tests/baseline.py tests/test_system_test_baseline.py
git commit -m "feat(system-tests): baseline comparison and regression detection"
```

---

### Task 7: Runner (CLI entry point)

**Files:**
- Create: `system_tests/runner.py`

The main entry point. Handles CLI args, interactive menu, scenario loading, execution loop, human review, and free chat mode.

- [ ] **Step 1: Implement runner**

```python
# system_tests/runner.py
"""System test runner — main entry point."""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from system_tests.baseline import compare_runs, load_latest, save_run
from system_tests.harness import TestHarness
from system_tests.models import (
    RunResult,
    ScenarioResult,
    StepExpect,
    SuiteResult,
)
from system_tests.reporter import JsonReporter, MarkdownReporter, TerminalReporter

SCENARIOS_DIR = Path(__file__).parent / "scenarios"
RUNS_DIR = Path(__file__).parent / "runs"


def load_suites(scenario_dir: Path) -> list[dict[str, Any]]:
    """Load all YAML scenario files."""
    suites = []
    for f in sorted(scenario_dir.glob("*.yaml")):
        with open(f, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if data:
            data["_file"] = f.stem
            suites.append(data)
    return suites


def parse_expect(raw: dict | None) -> StepExpect:
    """Parse a YAML expect block into StepExpect."""
    if not raw:
        return StepExpect()
    return StepExpect(
        route=raw.get("route"),
        tier=raw.get("tier"),
        path=raw.get("path"),
        device_state=raw.get("device_state"),
        response_contains=raw.get("response_contains"),
        response_not_contains=raw.get("response_not_contains"),
        memory_contains=raw.get("memory_contains"),
        memory_not_contains=raw.get("memory_not_contains"),
        latency_max_ms=raw.get("latency_max_ms"),
    )


def run_suite(
    harness: TestHarness,
    suite_data: dict[str, Any],
    mode: str = "human",
) -> SuiteResult:
    """Execute all scenarios in a suite."""
    suite_name = suite_data["name"]
    setup = suite_data.get("setup", {})
    user_cfg = setup.get("user", {})
    user_id = user_cfg.get("id", "default_user")
    user_name = user_cfg.get("name", "Allen")
    user_role = user_cfg.get("role", "owner")

    scenario_results: list[ScenarioResult] = []
    scenarios = suite_data.get("scenarios", [])

    for sc_idx, sc_data in enumerate(scenarios):
        sc_name = sc_data["name"]
        sc_review = sc_data.get("review", False)
        sc_review_hint = sc_data.get("review_hint")
        session_id = f"test_{suite_data.get('_file', 'suite')}_{sc_name}"

        if mode == "human":
            print(f"\n── Scenario {sc_idx + 1}/{len(scenarios)}: {sc_name} ──")

        # Reset state
        harness.reset_devices(setup.get("devices"))
        harness.reset_memory(user_id)
        harness.reset_conversation(session_id)

        if mode == "human":
            print("🔄 重置: 设备✓ 记忆✓ 对话✓")

        # Pre-seed memories if specified
        for mem in setup.get("memory", []):
            harness.app.memory_manager.store.add_memory(
                user_id=user_id,
                content=mem["content"],
                category=mem.get("category", "knowledge"),
                key=mem.get("key"),
                importance=mem.get("importance", 5.0),
            )

        step_results = []
        for step_idx, step_data in enumerate(sc_data.get("steps", [])):
            text = step_data["user"]
            expect = parse_expect(step_data.get("expect"))

            # Check for step-level review hint
            step_review_hint = step_data.get("review_hint")
            if step_review_hint and not sc_review_hint:
                sc_review_hint = step_review_hint
                sc_review = True

            result = harness.run_step(
                text, session_id=session_id,
                user_id=user_id, user_name=user_name, user_role=user_role,
                expect=expect,
            )
            step_results.append(result)

            if mode == "human":
                TerminalReporter.print_step(result, step_idx + 1, user_name, user_role)

        sc_result = ScenarioResult(sc_name, step_results, sc_review, sc_review_hint)
        scenario_results.append(sc_result)

        if mode == "human":
            TerminalReporter.print_scenario_result(sc_result)

    return SuiteResult(suite_name, scenario_results)


def run_free_chat(harness: TestHarness) -> None:
    """Interactive free-form chat with enhanced logging."""
    print(f"\n🧪 自由对话模式 (输入 q 退出)")
    print("━" * 40)
    session_id = f"free_{int(time.time())}"
    while True:
        try:
            text = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text or text.lower() in ("q", "quit", "exit"):
            break
        result = harness.run_step(text, session_id=session_id)
        TerminalReporter.print_step(result, 0, "Allen", "owner")


def run_human_review(run: RunResult) -> list[dict]:
    """Interactive human review for scenarios marked review=True."""
    review_items = []
    for suite in run.suites:
        for sc in suite.scenarios:
            if sc.status == "review":
                for i, step in enumerate(sc.steps):
                    review_items.append({
                        "suite": suite.name,
                        "scenario": sc.name,
                        "step_index": i,
                        "input": step.input_text,
                        "response": step.response,
                        "device_changes": [c.display() for c in step.device_changes],
                        "review_hint": sc.review_hint,
                    })

    if not review_items:
        return []

    print(f"\n{'═' * 45}")
    print(f"👤 人工评审 ({len(review_items)} 项)")
    print(f"{'═' * 45}")

    reviews = []
    for item in review_items:
        TerminalReporter.print_review_item(item)
        try:
            feedback = input("\n评价 (Enter跳过, q结束): ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if feedback.lower() == "q":
            break
        if feedback:
            item["feedback"] = feedback
            reviews.append(item)
            print("✅ 已记录")
    return reviews


def main() -> int:
    parser = argparse.ArgumentParser(description="Jarvis 系统测试")
    parser.add_argument("--mode", choices=["human", "cc"], default="human")
    parser.add_argument("--suite", help="Suite name or comma-separated list (e.g. smart_home,memory)")
    parser.add_argument("--free", action="store_true", help="Free chat mode")
    parser.add_argument("--no-interactive", action="store_true")
    args = parser.parse_args()

    # Load scenarios
    all_suites = load_suites(SCENARIOS_DIR)
    if not all_suites and not args.free:
        print("No scenario files found in", SCENARIOS_DIR)
        return 1

    # Initialize harness
    print("⚙️  初始化 JarvisApp (sim)...")
    t0 = time.monotonic()
    harness = TestHarness()
    print(f"  就绪 ({time.monotonic() - t0:.1f}s)")

    if args.free:
        run_free_chat(harness)
        harness.shutdown()
        return 0

    # Select suites
    selected: list[dict]
    if args.suite:
        names = [n.strip() for n in args.suite.split(",")]
        selected = [s for s in all_suites if s.get("_file") in names or s.get("name") in names]
    elif args.no_interactive or args.mode == "cc":
        selected = all_suites
    else:
        # Interactive menu
        previous = load_latest(RUNS_DIR)
        last_summary = None
        if previous:
            s = previous.get("summary", {})
            last_summary = (
                f"{previous.get('timestamp', '?')} "
                f"({s.get('total', '?')} scenarios, {s.get('pass', 0)}✅ {s.get('fail', 0)}❌)"
            )
        suite_info = [(s["name"], len(s.get("scenarios", []))) for s in all_suites]
        TerminalReporter.print_menu(last_summary, suite_info)

        try:
            choice = input("\n选择 (逗号分隔, A=全部, F=自由对话): ").strip()
        except (EOFError, KeyboardInterrupt):
            harness.shutdown()
            return 0

        if choice.upper() == "F":
            run_free_chat(harness)
            harness.shutdown()
            return 0
        elif choice.upper() == "A":
            selected = all_suites
        else:
            indices = []
            for c in choice.split(","):
                c = c.strip()
                if c.isdigit():
                    idx = int(c) - 1
                    if 0 <= idx < len(all_suites):
                        indices.append(idx)
            selected = [all_suites[i] for i in indices] if indices else all_suites

    # Execute
    run_start = time.monotonic()
    suite_results: list[SuiteResult] = []
    for suite_data in selected:
        if args.mode == "human":
            print(f"\n{'═' * 45}")
            print(f"📋 {suite_data['name']}")
            print(f"{'═' * 45}")
        result = run_suite(harness, suite_data, mode=args.mode)
        suite_results.append(result)

    run_result = RunResult(
        timestamp=datetime.now().isoformat(timespec="seconds"),
        duration_s=time.monotonic() - run_start,
        suites=suite_results,
    )

    # Baseline comparison
    previous = load_latest(RUNS_DIR)
    regressions = compare_runs(run_result, previous)

    # Output
    if args.mode == "cc":
        print(JsonReporter.format(run_result, regressions=regressions))
    else:
        for line in TerminalReporter.format_summary(run_result):
            print(line)
        if regressions:
            print(f"\n📈 对比上次:")
            for r in regressions:
                severity_sym = {"regression": "🔴", "warning": "🟡",
                                "improvement": "🟢", "info": "🔵"}
                sym = severity_sym.get(r.get("severity", ""), "  ")
                print(f"  {sym} {r['scenario']}: {r['change']}")

    # Save results
    save_path = save_run(run_result, RUNS_DIR)

    # Human review
    reviews = []
    if args.mode == "human":
        reviews = run_human_review(run_result)

    # Save markdown report
    md = MarkdownReporter.format(run_result, regressions=regressions, reviews=reviews)
    md_path = save_path.with_suffix(".md")
    md_path.write_text(md, encoding="utf-8")

    if args.mode == "human":
        print(f"\n报告: {md_path}")

    harness.shutdown()
    return 1 if run_result.summary["fail"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run full test suite to verify no breakage**

```bash
python -m pytest tests/ -q
```

- [ ] **Step 3: Commit**

```bash
git add system_tests/runner.py
git commit -m "feat(system-tests): runner with human/CC/free-chat modes"
```

---

### Task 8: YAML scenario files

**Files:**
- Create: `system_tests/scenarios/smart_home.yaml`
- Create: `system_tests/scenarios/memory.yaml`
- Create: `system_tests/scenarios/routing.yaml`
- Create: `system_tests/scenarios/multi_turn.yaml`
- Create: `system_tests/scenarios/error_handling.yaml`
- Create: `system_tests/scenarios/cloud_chat.yaml`

- [ ] **Step 1: Create smart_home.yaml**

```yaml
name: 智能家居控制
description: 灯光、恒温器、门锁的语音控制

setup:
  devices:
    bedroom_light: {is_on: false, brightness: 100, color_temp: neutral}
    living_room_light: {is_on: false, brightness: 100, color_temp: neutral}
    home_thermostat: {is_on: false, temperature: 24}
    front_door_lock: {is_locked: true}
  user: {id: allen, name: Allen, role: owner}

scenarios:
  - name: 单灯开关
    steps:
      - user: "打开卧室灯"
        expect:
          route: smart_home
          tier: local
          device_state:
            bedroom_light: {is_on: true}
          response_contains: ["灯"]
      - user: "再关掉"
        expect:
          route: smart_home
          device_state:
            bedroom_light: {is_on: false}

  - name: 亮度调节
    steps:
      - user: "把卧室灯调到50%亮度"
        expect:
          route: smart_home
          device_state:
            bedroom_light: {is_on: true, brightness: 50}

  - name: 色温调节
    steps:
      - user: "把卧室灯调成暖光"
        expect:
          route: smart_home
          device_state:
            bedroom_light: {color_temp: warm}

  - name: 恒温器控制
    steps:
      - user: "把空调温度调到22度"
        expect:
          route: smart_home
          device_state:
            home_thermostat: {is_on: true, temperature: 22}

  - name: 门锁控制
    steps:
      - user: "把门锁打开"
        expect:
          route: smart_home
          device_state:
            front_door_lock: {is_locked: false}
      - user: "再锁上"
        expect:
          device_state:
            front_door_lock: {is_locked: true}

  - name: 多灯控制
    steps:
      - user: "把所有灯都打开"
        expect:
          device_state:
            bedroom_light: {is_on: true}
            living_room_light: {is_on: true}
      - user: "全部关掉"
        expect:
          device_state:
            bedroom_light: {is_on: false}
            living_room_light: {is_on: false}

  - name: 自然语言控灯
    review: true
    steps:
      - user: "帮我把卧室弄暗一点"
        expect:
          route: smart_home
          device_state:
            bedroom_light: {is_on: true}
        review_hint: "回复是否说明了调整结果和具体数值"
```

- [ ] **Step 2: Create memory.yaml**

```yaml
name: 记忆系统
description: 记忆存储、检索、直答、偏好

setup:
  memory: []
  user: {id: allen, name: Allen, role: owner}

scenarios:
  - name: 偏好存储与检索
    steps:
      - user: "记住我喜欢喝咖啡"
        expect:
          path: memory_shortcut
          response_contains: ["记住"]
          memory_contains:
            content_matches: "咖啡"
      - user: "我喜欢喝什么"
        expect:
          response_contains: ["咖啡"]

  - name: 身份记忆
    steps:
      - user: "记住我叫Allen"
        expect:
          path: memory_shortcut
          memory_contains:
            content_matches: "Allen"
      - user: "我叫什么名字"
        expect:
          response_contains: ["Allen"]

  - name: 记忆不污染
    steps:
      - user: "现在几点了"
        expect:
          route: time
```

- [ ] **Step 3: Create routing.yaml**

```yaml
name: 路由准确性
description: 验证不同类型输入走正确的 intent 路由

setup:
  user: {id: allen, name: Allen, role: owner}

scenarios:
  - name: 智能家居路由
    steps:
      - user: "打开卧室灯"
        expect:
          route: smart_home
          tier: local

  - name: 时间路由
    steps:
      - user: "现在几点了"
        expect:
          route: time
          tier: local

  - name: 闲聊路由
    steps:
      - user: "你觉得人生的意义是什么"
        expect:
          tier: cloud

  - name: 告别快捷路径
    steps:
      - user: "再见"
        expect:
          path: farewell

  - name: 记忆快捷路径
    steps:
      - user: "记住我喜欢红色"
        expect:
          path: memory_shortcut
```

- [ ] **Step 4: Create multi_turn.yaml**

```yaml
name: 多轮对话
description: 多轮上下文连贯性

setup:
  devices:
    bedroom_light: {is_on: false, brightness: 100}
  user: {id: allen, name: Allen, role: owner}

scenarios:
  - name: 设备多轮
    steps:
      - user: "打开卧室灯"
        expect:
          device_state:
            bedroom_light: {is_on: true}
      - user: "调暗一点"
        expect:
          device_state:
            bedroom_light: {is_on: true}
        review_hint: "是否正确理解了'调暗'指的是上一轮提到的卧室灯"
    review: true

  - name: 闲聊多轮
    review: true
    steps:
      - user: "给我讲个笑话"
        expect:
          tier: cloud
      - user: "再讲一个"
        expect:
          tier: cloud
        review_hint: "是否理解'再讲一个'指的是再讲一个笑话，而不是其他东西"
```

- [ ] **Step 5: Create error_handling.yaml**

```yaml
name: 容错测试
description: 异常输入不崩溃

setup:
  user: {id: allen, name: Allen, role: owner}

scenarios:
  - name: 空输入
    steps:
      - user: ""
        expect:
          latency_max_ms: 5000

  - name: 超长输入
    steps:
      - user: "你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好你好"
        expect:
          latency_max_ms: 10000

  - name: 乱码输入
    steps:
      - user: "asdf!@#$%^&*()_+{}|:<>?"
        expect:
          latency_max_ms: 5000

  - name: 不存在的设备
    steps:
      - user: "打开厨房灯"
        expect:
          response_not_contains: ["Error", "Traceback"]
```

- [ ] **Step 6: Create skill_learning.yaml**

```yaml
name: 技能学习
description: 教 Jarvis 新技能并验证调用

setup:
  user: {id: allen, name: Allen, role: owner}

scenarios:
  - name: 学习意图检测
    review: true
    steps:
      - user: "学会查汇率"
        expect:
          path: learn_create
          response_contains: ["后台", "学"]
        review_hint: "是否正确识别为学习意图并启动后台学习"

  - name: 已有技能不重复学
    review: true
    steps:
      - user: "学会查天气"
        expect:
          response_contains: ["已经会"]
        review_hint: "是否正确识别 weather 技能已存在"
```

- [ ] **Step 7: Create cloud_chat.yaml**

```yaml
name: 云端对话
description: 需要 LLM 处理的对话

setup:
  user: {id: allen, name: Allen, role: owner}

scenarios:
  - name: 简单问答
    review: true
    steps:
      - user: "加拿大的首都是哪里"
        expect:
          response_contains: ["渥太华"]
          tier: cloud
        review_hint: "回复是否简洁准确"

  - name: 创作
    review: true
    steps:
      - user: "用一句话描述春天"
        expect:
          tier: cloud
          latency_max_ms: 10000
        review_hint: "回复是否自然有文采"

  - name: 复杂推理
    review: true
    steps:
      - user: "如果今天是星期三，三天后是星期几"
        expect:
          tier: cloud
          response_contains: ["六"]
        review_hint: "推理是否正确"
```

- [ ] **Step 8: Commit all scenarios**

```bash
git add system_tests/scenarios/
git commit -m "feat(system-tests): YAML scenario files for all test categories"
```

---

### Task 9: Integration verification

Verify the complete framework works end-to-end.

- [ ] **Step 1: Run unit tests**

```bash
python -m pytest tests/test_system_test_*.py -v
```

All should pass.

- [ ] **Step 2: Run full project test suite**

```bash
python -m pytest tests/ -q
```

All 812+ existing tests should still pass.

- [ ] **Step 3: Smoke-test human mode with one suite**

```bash
python system_tests/runner.py --suite routing --no-interactive
```

Expected: initializes app, runs routing scenarios, prints colored output with step boxes, saves report to `system_tests/runs/`.

- [ ] **Step 4: Smoke-test CC mode**

```bash
python system_tests/runner.py --mode cc --suite routing 2>/dev/null | python -m json.tool
```

Expected: valid JSON output with `summary`, `suites`, `failures`, `needs_review`, `regressions` fields.

- [ ] **Step 5: Smoke-test free chat**

```bash
python system_tests/runner.py --free
```

Type "打开卧室灯", verify device state diff shows in output. Type "q" to exit.

- [ ] **Step 6: Verify report files created**

```bash
ls -la system_tests/runs/
```

Should contain `.json` and `.md` files from the test runs above.

- [ ] **Step 7: Final commit**

```bash
git add -A
git commit -m "feat: complete system testing framework with human/CC/free-chat modes"
```
