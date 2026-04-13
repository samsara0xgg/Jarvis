"""Output formatting for system test results."""
from __future__ import annotations

import json
from typing import Any

from system_tests.models import (
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
        if item.get("failures"):
            for name, detail in item["failures"].items():
                print(f"  {_RED}❌ {name}: {detail}{_RESET}")
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
                    if step.route is not None:
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
                            ctx_parts = [v.debug_context for v in failed_asserts.values()
                                         if v.debug_context]
                            failures.append({
                                "suite": suite.name,
                                "scenario": sc.name,
                                "step_index": i,
                                "input": step.input_text,
                                "response": step.response,
                                "assertions_failed": {
                                    k: {"expected": _jsonable(v.expected),
                                        "actual": _jsonable(v.actual)}
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
                    lines.append(f"**Step {i + 1}**: \"{step.input_text}\"")
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
