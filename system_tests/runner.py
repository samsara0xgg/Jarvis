"""System test runner — main entry point."""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# Allow running as `python system_tests/runner.py` from project root
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
                    # Collect assertion failures for context
                    failures = {
                        k: f"expected={v.expected}, actual={v.actual}"
                        for k, v in step.assertions.items() if v.status == "fail"
                    }
                    review_items.append({
                        "suite": suite.name,
                        "scenario": sc.name,
                        "step_index": i,
                        "input": step.input_text,
                        "response": step.response,
                        "device_changes": [c.display() for c in step.device_changes],
                        "review_hint": sc.review_hint,
                        "failures": failures,
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
    parser.add_argument("--live", action="store_true", help="Use real devices (Hue bridge) instead of sim")
    parser.add_argument("--tts", action="store_true", help="Play TTS audio for each response")
    args = parser.parse_args()

    # Load scenarios
    all_suites = load_suites(SCENARIOS_DIR)
    if not all_suites and not args.free:
        print("No scenario files found in", SCENARIOS_DIR)
        return 1

    # Initialize harness
    mode_label = "live" if args.live else "sim"
    extras = []
    if args.tts:
        extras.append("TTS")
    extra_str = f" + {', '.join(extras)}" if extras else ""
    print(f"⚙️  初始化 JarvisApp ({mode_label}{extra_str})...")
    t0 = time.monotonic()
    harness = TestHarness(live=args.live, tts=args.tts)
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
