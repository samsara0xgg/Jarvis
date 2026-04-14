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
    verbose: bool = False,
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
            TerminalReporter.print_scenario_header(sc_name, sc_idx + 1, len(scenarios))

        # Reset state
        harness.reset_devices(setup.get("devices"))
        harness.reset_memory(user_id)
        harness.reset_conversation(session_id)

        if mode == "human":
            TerminalReporter.print_reset()

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
                TerminalReporter.print_step(result, step_idx + 1, verbose=verbose)

        sc_result = ScenarioResult(sc_name, step_results, sc_review, sc_review_hint)
        scenario_results.append(sc_result)

        if mode == "human":
            TerminalReporter.print_scenario_result(sc_result)

    return SuiteResult(suite_name, scenario_results)


def run_free_chat(harness: TestHarness, verbose: bool = False) -> None:
    """Interactive free-form chat with enhanced logging."""
    print()
    print("jarvis-system-test / free chat  (q to quit)")
    print()
    session_id = f"free_{int(time.time())}"
    step_num = 0
    while True:
        try:
            text = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text or text.lower() in ("q", "quit", "exit"):
            break
        step_num += 1
        result = harness.run_step(text, session_id=session_id)
        TerminalReporter.print_step(result, step_num, verbose=verbose)


def run_human_review(run: RunResult) -> list[dict]:
    """Interactive human review for scenarios marked review=True."""
    review_items = []
    for suite in run.suites:
        for sc in suite.scenarios:
            if sc.status == "review":
                for i, step in enumerate(sc.steps):
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

    print()
    print("---")
    print()
    print(f"review ({len(review_items)})")

    reviews = []
    for idx, item in enumerate(review_items):
        TerminalReporter.print_review_item(item, idx + 1, len(review_items))
        try:
            feedback = input("         feedback > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if feedback.lower() == "q":
            break
        if feedback:
            item["feedback"] = feedback
            reviews.append(item)
    return reviews


def build_adhoc_suite(prompts: list[str], name: str = "adhoc") -> dict[str, Any]:
    """Build an ephemeral suite from a list of prompts.

    No assertions — just captures full trace for CC to inspect.
    Each prompt becomes a step in a single scenario (multi-turn within one session).
    """
    steps = [{"user": p.strip()} for p in prompts if p.strip()]
    return {
        "_file": name,
        "name": f"ad-hoc ({name})",
        "setup": {"user": {"id": "allen", "name": "Allen", "role": "owner"}},
        "scenarios": [
            {
                "name": name,
                "review": True,
                "review_hint": "ad-hoc test — inspect trace for behavior correctness",
                "steps": steps,
            }
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Jarvis 系统测试")
    parser.add_argument("--mode", choices=["human", "cc"], default="human")
    parser.add_argument("--suite", help="Suite name or comma-separated list (e.g. smart_home,memory)")
    parser.add_argument("--prompts",
                        help="Ad-hoc prompts separated by | (multi-turn session). "
                             "Takes precedence over --suite. Example: --prompts '开灯|关掉'")
    parser.add_argument("--adhoc-name", default="adhoc",
                        help="Scenario name label when using --prompts")
    parser.add_argument("--free", action="store_true", help="Free chat mode")
    parser.add_argument("--no-interactive", action="store_true")
    parser.add_argument("--live", action="store_true", help="Use real devices (Hue bridge) instead of sim")
    parser.add_argument("--tts", action="store_true", help="Play TTS audio for each response")
    parser.add_argument("--verbose", action="store_true", help="Show tier-3 debug details per step")
    parser.add_argument("--color", action="store_true", help="Enable ANSI color output")
    args = parser.parse_args()

    if args.color:
        from system_tests.reporter import enable_color
        enable_color()

    # Build scenario list
    if args.prompts:
        prompts = [p for p in args.prompts.split("|") if p.strip()]
        if not prompts:
            print("--prompts is empty", file=sys.stderr)
            return 1
        all_suites = [build_adhoc_suite(prompts, args.adhoc_name)]
    else:
        all_suites = load_suites(SCENARIOS_DIR)
        if not all_suites and not args.free:
            print("No scenario files found in", SCENARIOS_DIR)
            return 1

    # Initialize harness
    mode_label = "live" if args.live else "sim"
    extras = []
    if args.tts:
        extras.append("tts")
    if args.prompts:
        extras.append("adhoc")
    extra_str = f"+{','.join(extras)}" if extras else ""
    print(f"init  jarvis-app  mode={mode_label}  {extra_str}")
    t0 = time.monotonic()
    harness = TestHarness(live=args.live, tts=args.tts)
    print(f"      ready ({time.monotonic() - t0:.1f}s)")

    if args.free:
        run_free_chat(harness, verbose=args.verbose)
        harness.shutdown()
        return 0

    # Select suites
    selected: list[dict]
    if args.prompts:
        selected = all_suites  # ad-hoc suite, just run it
    elif args.suite:
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
                f"{previous.get('timestamp', '?')}   "
                f"{s.get('pass', 0)} pass, {s.get('fail', 0)} fail, "
                f"{s.get('review', 0)} review"
            )
        suite_info = [(s.get("_file", s["name"]), len(s.get("scenarios", []))) for s in all_suites]
        TerminalReporter.print_menu(last_summary, suite_info)

        try:
            choice = input().strip()
        except (EOFError, KeyboardInterrupt):
            harness.shutdown()
            return 0

        if choice.upper() == "F":
            run_free_chat(harness, verbose=args.verbose)
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
    timestamp_str = datetime.now().isoformat(timespec="seconds")
    if args.mode == "human":
        mode_label = "live" if args.live else "sim"
        TerminalReporter.print_run_header(mode_label, timestamp_str)

    for suite_data in selected:
        if args.mode == "human":
            TerminalReporter.print_suite_header(suite_data.get("_file", suite_data["name"]))
        result = run_suite(harness, suite_data, mode=args.mode, verbose=args.verbose)
        suite_results.append(result)

    run_result = RunResult(
        timestamp=timestamp_str,
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
        for line in TerminalReporter.format_summary(run_result, regressions):
            print(line)

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
        TerminalReporter.print_report_path(str(md_path))

    harness.shutdown()
    return 1 if run_result.summary["fail"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
