"""Output formatting for system test results.

ASCII professional style. No emoji, no unicode box drawing.
Tier 1: always shown. Tier 2: conditional. Tier 3: --verbose only.
CC/JSON mode always outputs full trace regardless of tier.
"""
from __future__ import annotations

import json
from typing import Any

from system_tests.models import (
    RunResult,
    ScenarioResult,
    StepResult,
    SuiteResult,
)

# Minimal ANSI (only when --color, off by default)
_GREEN = ""
_RED = ""
_YELLOW = ""
_DIM = ""
_BOLD = ""
_RESET = ""


def enable_color() -> None:
    """Turn on ANSI color output (call from runner if --color flag set)."""
    global _GREEN, _RED, _YELLOW, _DIM, _BOLD, _RESET
    _GREEN = "\033[92m"
    _RED = "\033[91m"
    _YELLOW = "\033[93m"
    _DIM = "\033[2m"
    _BOLD = "\033[1m"
    _RESET = "\033[0m"


def _fmt_bool(v: Any) -> str:
    if isinstance(v, bool):
        return "ON" if v else "OFF"
    return str(v)


def _label(name: str, width: int = 9) -> str:
    """Left-aligned label, uniform width."""
    return f"{name:<{width}s}"


class TerminalReporter:
    """ASCII professional-style terminal output."""

    @staticmethod
    def print_menu(last_run_summary: str | None, suites: list[tuple[str, int]]) -> None:
        print()
        print("jarvis-system-test")
        print()
        if last_run_summary:
            print(f"last run  {last_run_summary}")
            print()
        for i, (name, count) in enumerate(suites, 1):
            suffix = "scenario" if count == 1 else "scenarios"
            print(f"  [{i}]  {name:<18s}  {count} {suffix}")
        print("  [A]  all")
        print("  [F]  free chat")
        print()
        print("select >", end=" ", flush=True)

    @staticmethod
    def print_run_header(mode_label: str, timestamp: str) -> None:
        print()
        print(f"jarvis-system-test  {mode_label}  {timestamp}")

    @staticmethod
    def print_suite_header(name: str) -> None:
        print()
        print(f"=== suite: {name}")

    @staticmethod
    def print_scenario_header(name: str, idx: int, total: int) -> None:
        print()
        print(f"[{idx}/{total}] {name}")

    @staticmethod
    def print_reset() -> None:
        print("       reset  devices=ok  memory=ok  conversation=ok")

    @staticmethod
    def print_step(step: StepResult, step_num: int, verbose: bool = False) -> None:
        """Print one step. Tier 1 always, Tier 2 when applicable, Tier 3 if verbose."""
        print()
        print(f"  step {step_num}  in:   \"{step.input_text}\"")
        # out
        out_text = step.response if step.response else ""
        if step.response == "farewell":
            out_text = "(farewell — 再见。)"
        print(f"          out:  \"{out_text}\"")
        print()

        # ── Tier 1: always shown ──
        if step.path:
            print(f"          {_label('path')} {step.path}")
        if step.route is not None:
            r = step.route
            print(f"          {_label('route')} {r.tier}/{r.intent}   conf={r.confidence:.2f}   "
                  f"via={r.provider}")
            if r.actions:
                for a in r.actions:
                    val = f"({a['value']})" if a.get("value") is not None else ""
                    print(f"          {_label('action')} {a.get('device_id', '?')}.{a.get('action', '?')} {val}")

        if step.device_changes:
            first = True
            for c in step.device_changes:
                lbl = _label('device') if first else _label('')
                print(f"          {lbl} {c.display()}")
                first = False

        if step.memory_diff.is_empty:
            if step.memory_hits_count > 0 or step.memory_keyword or step.direct_answer:
                print(f"          {_label('memory')} hits={step.memory_hits_count}")
        else:
            first = True
            for m in step.memory_diff.added:
                cat = f" ({m.category})" if m.category else ""
                lbl = _label('memory') if first else _label('')
                print(f"          {lbl} + {m.content[:70]}{cat}")
                first = False
            for m in step.memory_diff.removed:
                print(f"          {_label('')} - {m.content[:70]}")

        # TTS
        if step.tts_info:
            t = step.tts_info
            if t.played:
                print(f"          {_label('tts')} {t.engine}   synth={t.synth_ms}ms   played=yes")
            else:
                print(f"          {_label('tts')} {t.engine}")

        # Timing
        if step.timings:
            t = step.timings
            parts = []
            if "route_ms" in t:
                parts.append(f"route={t['route_ms']}")
            if "memory_query_ms" in t:
                parts.append(f"mem={t['memory_query_ms']}")
            if "direct_answer_ms" in t:
                parts.append(f"da={t['direct_answer_ms']}")
            if "local_exec_ms" in t:
                parts.append(f"local={t['local_exec_ms']}")
            if "llm_first_ms" in t:
                parts.append(f"llm_first={t['llm_first_ms']}")
            parts.append(f"total={step.latency_ms}")
            print(f"          {_label('timing')} {'  '.join(parts)}   (ms)")
        else:
            print(f"          {_label('timing')} total={step.latency_ms}ms")

        # API + cost
        if step.api_calls:
            parts = [f"{k}={v}" for k, v in step.api_calls.items()]
            rates = {"groq": 0.0008, "xai": 0.005, "gpt4o_mini": 0.001}
            cost = sum(step.api_calls.get(k, 0) * v for k, v in rates.items())
            print(f"          {_label('api')} {'  '.join(parts)}   cost=${cost:.4f}")
        else:
            print(f"          {_label('api')} none")

        # ── Tier 2: conditional ──
        if step.escalation:
            e = step.escalation
            print(f"          {_label('escalate')} keyword=\"{e['keyword']}\"  "
                  f"{e['from']} -> {e['to']}")
        if step.farewell_match:
            print(f"          {_label('farewell')} matched=\"{step.farewell_match}\"")
        if step.memory_keyword:
            print(f"          {_label('mem_kw')} matched=\"{step.memory_keyword}\"")
        if step.learning_intent:
            li = step.learning_intent
            print(f"          {_label('learn')} mode={li['mode']}   "
                  f"desc=\"{li['description'][:60]}\"")
        if step.keyword_rule:
            kr = step.keyword_rule
            print(f"          {_label('kw_rule')} rule=\"{kr['rule_name']}\"   "
                  f"actions={len(kr.get('actions', []))}")
        if step.direct_answer:
            da = step.direct_answer
            print(f"          {_label('direct')} \"{da['answer'][:60]}\"   {da['latency_ms']}ms")
        if step.reqllm:
            print(f"          {_label('reqllm')} true  (local -> cloud for rephrasing)")
        if step.history_turns > 0:
            print(f"          {_label('history')} {step.history_turns} turns loaded")

        # ── Tier 3: verbose only ──
        if verbose:
            # Could dump full memory context, full prompt etc. — for now just note
            if step.memory_hits_count > 0:
                print(f"          {_label('memhits')} {step.memory_hits_count} entries retrieved")

        # Assertions
        if step.assertions:
            print()
            for name, result in step.assertions.items():
                if result.status == "pass":
                    print(f"          {_GREEN}PASS{_RESET}    {name}")
                else:
                    ctx = f"   [{result.debug_context}]" if result.debug_context else ""
                    print(f"          {_RED}FAIL{_RESET}    {name}")
                    print(f"                  expected  {result.expected}")
                    print(f"                  actual    {result.actual}{ctx}")

        # Error
        if step.error:
            print()
            print(f"          {_RED}ERROR{_RESET}   {step.error}")

    @staticmethod
    def print_scenario_result(scenario: ScenarioResult) -> None:
        passed = sum(1 for s in scenario.steps if s.passed)
        total = len(scenario.steps)
        status = scenario.status.upper()
        color = {"pass": _GREEN, "fail": _RED, "review": _YELLOW}.get(scenario.status, "")
        total_ms = sum(s.latency_ms for s in scenario.steps)
        print()
        print(f"  result  {color}{status}{_RESET}  {passed}/{total} steps   {total_ms}ms total")

    @staticmethod
    def format_summary(run: RunResult, regressions: list[dict] | None = None) -> list[str]:
        out: list[str] = []
        out.append("")
        out.append("---")
        out.append("")
        out.append("summary")
        out.append("")

        # Table header
        out.append(f"  {'scenario':<38s}  {'result':<7s}  {'pass/total':<11s}  time")
        out.append(f"  {'-'*38}  {'-'*7}  {'-'*11}  {'-'*6}")

        for suite in run.suites:
            for sc in suite.scenarios:
                passed = sum(1 for s in sc.steps if s.passed)
                total = len(sc.steps)
                avg_ms = sum(s.latency_ms for s in sc.steps) // max(total, 1)
                status = sc.status.upper()
                color = {"pass": _GREEN, "fail": _RED, "review": _YELLOW}.get(sc.status, "")
                label = f"{suite.name}/{sc.name}"
                if len(label) > 38:
                    label = label[:35] + "..."
                out.append(f"  {label:<38s}  {color}{status:<7s}{_RESET}  "
                           f"{passed}/{total:<9d}  {avg_ms}ms")

        s = run.summary
        out.append("")
        out.append(f"  totals     pass={s['pass']}   fail={s['fail']}   "
                   f"review={s['review']}   ({s['total']} scenarios)")
        out.append(f"  duration   {run.duration_s:.0f}s")

        api = run.total_api_calls
        if api:
            parts = [f"{k}={v}" for k, v in api.items()]
            cost = run.estimate_cost()
            out.append(f"  api        {'  '.join(parts)}   ~${cost:.4f}")

        if regressions:
            out.append("")
            out.append("  baseline")
            for r in regressions:
                sev = r.get("severity", "info")
                sym = {"regression": "regr", "warning": "warn",
                       "improvement": "impr", "info": "info"}[sev]
                out.append(f"    {sym}   {r['scenario']:<36s}  {r['change']}")

        return out

    @staticmethod
    def print_review_item(item: dict, idx: int, total: int) -> None:
        print()
        print(f"  [{idx}/{total}]  {item['suite']}/{item['scenario']}   "
              f"step {item['step_index'] + 1}")
        print()
        print(f"         in       \"{item['input']}\"")
        print(f"         out      \"{item['response']}\"")
        if item.get("device_changes"):
            first = True
            for c in item["device_changes"]:
                lbl = "device" if first else "      "
                print(f"         {lbl:<9s}{c}")
                first = False
        if item.get("failures"):
            for name, detail in item["failures"].items():
                print(f"         {_RED}FAIL{_RESET}     {name}  ({detail})")
        if item.get("review_hint"):
            print(f"         hint     {item['review_hint']}")

    @staticmethod
    def print_report_path(md_path: str) -> None:
        print()
        print(f"  report     {md_path}")
        print()


class JsonReporter:
    """Full-trace JSON output for CC mode.

    Includes ALL trace data (Tier 1+2+3) for CC to debug with.
    """

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
                        "path": step.path,
                        "latency_ms": step.latency_ms,
                        "timings": step.timings,
                        "api_calls": step.api_calls,
                        "history_turns": step.history_turns,
                        "user": {"id": step.user_id, "name": step.user_name,
                                 "role": step.user_role},
                    }
                    if step.route is not None:
                        step_d["route"] = {
                            "intent": step.route.intent,
                            "tier": step.route.tier,
                            "confidence": round(step.route.confidence, 2),
                            "provider": step.route.provider,
                            "duration_ms": step.route.duration_ms,
                            "actions": step.route.actions,
                            "sub_type": step.route.sub_type,
                        }
                    if step.device_changes:
                        step_d["device_changes"] = [
                            {"device_id": c.device_id, "field": c.field,
                             "before": c.before, "after": c.after}
                            for c in step.device_changes
                        ]
                    if step.device_ops:
                        step_d["device_ops"] = step.device_ops
                    if not step.memory_diff.is_empty:
                        step_d["memory_changes"] = [
                            {"action": m.action, "content": m.content,
                             "category": m.category, "key": m.key}
                            for m in step.memory_diff.added + step.memory_diff.removed
                        ]
                    if step.memory_hits_count:
                        step_d["memory_hits_count"] = step.memory_hits_count
                    if step.tts_info:
                        step_d["tts"] = {
                            "engine": step.tts_info.engine,
                            "played": step.tts_info.played,
                            "synth_ms": step.tts_info.synth_ms,
                        }
                    # Conditional trace data
                    if step.escalation:
                        step_d["escalation"] = step.escalation
                    if step.farewell_match:
                        step_d["farewell_match"] = step.farewell_match
                    if step.memory_keyword:
                        step_d["memory_keyword"] = step.memory_keyword
                    if step.learning_intent:
                        step_d["learning_intent"] = step.learning_intent
                    if step.keyword_rule:
                        step_d["keyword_rule"] = step.keyword_rule
                    if step.direct_answer:
                        step_d["direct_answer"] = step.direct_answer
                    if step.reqllm:
                        step_d["reqllm"] = True
                    if step.error:
                        step_d["error"] = step.error

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

                sc_d = {"name": sc.name, "status": sc.status,
                        "review": sc.review, "review_hint": sc.review_hint,
                        "steps": steps_data}
                scenarios_data.append(sc_d)

                if sc.status == "fail":
                    for i, step in enumerate(sc.steps):
                        failed_asserts = {k: v for k, v in step.assertions.items()
                                          if v.status == "fail"}
                        if failed_asserts:
                            ctx_parts = [v.debug_context for v in failed_asserts.values()
                                         if v.debug_context]
                            failures.append({
                                "suite": suite.name,
                                "scenario": sc.name,
                                "step_index": i,
                                "input": step.input_text,
                                "response": step.response,
                                "path": step.path,
                                "route": {
                                    "intent": step.route.intent,
                                    "tier": step.route.tier,
                                    "confidence": round(step.route.confidence, 2),
                                } if step.route else None,
                                "device_changes": [
                                    {"device_id": c.device_id, "field": c.field,
                                     "before": c.before, "after": c.after}
                                    for c in step.device_changes
                                ],
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
        lines.append(f"# system test report — {run.timestamp}")
        lines.append("")
        s = run.summary
        lines.append(f"**result**: {s['pass']} pass / {s['fail']} fail / {s['review']} review")
        lines.append(f"**duration**: {run.duration_s:.0f}s")
        api = run.total_api_calls
        if api:
            parts = [f"{k}={v}" for k, v in api.items()]
            lines.append(f"**api**: {', '.join(parts)} ~${run.estimate_cost():.4f}")
        lines.append("")

        for suite in run.suites:
            lines.append(f"## {suite.name}")
            lines.append("")
            for sc in suite.scenarios:
                status = sc.status.upper()
                lines.append(f"### [{status}] {sc.name}")
                lines.append("")
                for i, step in enumerate(sc.steps):
                    lines.append(f"**step {i + 1}**: \"{step.input_text}\"")
                    lines.append(f"- out: `{step.response}`")
                    if step.path:
                        lines.append(f"- path: `{step.path}`")
                    if step.route:
                        lines.append(f"- route: `{step.route.tier}/{step.route.intent}` "
                                     f"conf={step.route.confidence:.2f} via={step.route.provider}")
                    if step.device_changes:
                        for c in step.device_changes:
                            lines.append(f"- device: `{c.display()}`")
                    if not step.memory_diff.is_empty:
                        for m in step.memory_diff.added:
                            lines.append(f"- memory: +{m.content[:70]}")
                    if step.timings:
                        t_parts = [f"{k}={v}" for k, v in step.timings.items()]
                        lines.append(f"- timing: {', '.join(t_parts)} ms, total={step.latency_ms}ms")
                    for name, result in step.assertions.items():
                        sym = "PASS" if result.status == "pass" else "FAIL"
                        if result.status == "pass":
                            lines.append(f"- [{sym}] {name}")
                        else:
                            lines.append(f"- [{sym}] {name}: expected={result.expected}, "
                                         f"actual={result.actual}")
                    lines.append("")

        if regressions:
            lines.append("## regressions")
            lines.append("")
            for r in regressions:
                lines.append(f"- {r['scenario']}: {r.get('change', '')}")
            lines.append("")

        if reviews:
            lines.append("## human review")
            lines.append("")
            for r in reviews:
                lines.append(f"- **{r.get('scenario', '')}**: {r.get('feedback', '(skipped)')}")
            lines.append("")

        return "\n".join(lines)


def _jsonable(v: Any) -> Any:
    """Make a value JSON-serializable."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)
