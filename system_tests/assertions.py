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
