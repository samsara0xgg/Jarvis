"""Unit tests for the `get_current_time` L4 read-only tool (Tier 0 batch 1)."""

from __future__ import annotations

import json
from contextlib import closing
from typing import TYPE_CHECKING

import pytest

from jarvis.deployment import bootstrap_runtime
from jarvis.execution.tools import (
    ActionLifecycle,
    _spoken_clock,
    _spoken_day_period,
    build_default_registry,
)
from jarvis.shared import ActionRequest, CallerPrincipal
from jarvis.state.event_log import open_event_log

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("hour", "expected"),
    [
        (0, "凌晨"),
        (5, "凌晨"),
        (6, "早上"),
        (8, "早上"),
        (9, "上午"),
        (11, "上午"),
        (12, "中午"),
        (13, "下午"),
        (17, "下午"),
        (18, "晚上"),
        (23, "晚上"),
    ],
)
def test_spoken_day_period_boundaries(hour: int, expected: str) -> None:
    """Every day-period boundary hour lands in the intended zh-CN bucket."""
    assert _spoken_day_period(hour) == expected


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        # Minute 0 says 整, never "0分".
        (10, 0, "上午10点整"),
        # Minutes 1-9 need the 零 filler or the utterance is wrong.
        (10, 2, "上午10点零2分"),
        (10, 9, "上午10点零9分"),
        (10, 10, "上午10点10分"),
        (10, 30, "上午10点30分"),
        # Hour 0 is 零点, not 12点 (which would read as noon under 凌晨).
        (0, 0, "凌晨零点整"),
        (0, 5, "凌晨零点零5分"),
        (0, 30, "凌晨零点30分"),
        # 12h wrap for the afternoon/evening buckets; noon stays 12.
        (12, 0, "中午12点整"),
        (13, 45, "下午1点45分"),
        (23, 59, "晚上11点59分"),
    ],
)
def test_spoken_clock_formatting(hour: int, minute: int, expected: str) -> None:
    """`_spoken_clock` renders idiomatic zh-CN for every minute/hour shape."""
    assert _spoken_clock(hour, minute) == expected


def _request(caller: CallerPrincipal) -> ActionRequest:
    """Build the zero-argument `get_current_time` ActionRequest."""
    return ActionRequest(
        action_id="A_gct1",
        tool_name="get_current_time",
        target_entity_ref=None,
        caller_principal=caller,
        risk_level="L0",
        arguments={},
        authorization_lease=None,
        run_id=None,
        turn_id="T_gct1",
    )


def test_get_current_time_observation_flow(tmp_path: Path) -> None:
    """Dispatch returns one observation slot with the full clock payload."""
    paths = bootstrap_runtime(root=tmp_path)
    registry = build_default_registry()
    lifecycle = ActionLifecycle()
    lifecycle.register("A_gct1")
    lifecycle.transition("A_gct1", "authorized")
    with closing(open_event_log(paths.event_log)) as conn:
        bundle = registry.dispatch(
            _request(CallerPrincipal.REGEX_ROUTER), conn, paths, lifecycle,
        )
        slot = bundle.slots[0]
        assert slot.semantics == "observation"
        assert slot.error is None
        for key in ("iso", "date", "time", "weekday", "spoken_time", "spoken_date"):
            assert isinstance(slot.payload[key], str)
            assert slot.payload[key]
        assert slot.payload["weekday"].startswith("周")
        # spoken_time is the helper's output for the payload's own clock
        # reading — clock-independent, but pins the handler to the helper.
        hour_str, minute_str = slot.payload["time"].split(":")
        assert slot.payload["spoken_time"] == _spoken_clock(
            int(hour_str), int(minute_str),
        )
        # handler emitted its own action.result_observed + finished lifecycle
        row = conn.execute(
            "SELECT COUNT(*) FROM events WHERE type='action.result_observed'",
        ).fetchone()
        assert int(row[0]) == 1
        assert lifecycle.state_of("A_gct1") == "result_observed"
        # tool_output is the house JSON envelope
        assert slot.tool_output is not None
        assert json.loads(slot.tool_output)["date"] == slot.payload["date"]


def test_get_current_time_allows_regex_router_and_llm_only(tmp_path: Path) -> None:
    """The tool is visible to regex_router + jarvis_llm, and to nobody else."""
    del tmp_path
    registry = build_default_registry()
    names_for = {
        p: {t.name for t in registry.for_caller(p)} for p in CallerPrincipal
    }
    assert "get_current_time" in names_for[CallerPrincipal.REGEX_ROUTER]
    assert "get_current_time" in names_for[CallerPrincipal.JARVIS_LLM]
    assert "get_current_time" not in names_for[CallerPrincipal.WORKER_AGENT]
