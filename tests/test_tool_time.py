"""Tests for tools/time_utils.py — @jarvis_tool time functions."""

from __future__ import annotations

import importlib
import re

import pytest

from tools import _TOOL_REGISTRY


@pytest.fixture(autouse=True)
def _ensure_registered():
    """Re-register time tools if another test cleared the registry."""
    import tools.time_utils as tm
    if "get_current_time" not in _TOOL_REGISTRY:
        importlib.reload(tm)
    yield
    # Cancel any timers started during tests
    tm.cancel_all()


import tools.time_utils as tm  # noqa: E402


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_get_current_time_registered():
    assert "get_current_time" in _TOOL_REGISTRY


def test_set_timer_registered():
    assert "set_timer" in _TOOL_REGISTRY


def test_get_current_time_read_only():
    assert _TOOL_REGISTRY["get_current_time"]["read_only"] is True


def test_set_timer_not_read_only():
    assert _TOOL_REGISTRY["set_timer"]["read_only"] is False


# ---------------------------------------------------------------------------
# get_current_time
# ---------------------------------------------------------------------------


def test_get_current_time_returns_string():
    entry = _TOOL_REGISTRY["get_current_time"]
    result = entry["execute"]("get_current_time", {})
    assert result.startswith("Current time:")
    # Should contain date pattern like 2026-04-15
    assert re.search(r"\d{4}-\d{2}-\d{2}", result)


def test_get_current_time_direct():
    result = tm.get_current_time()
    assert "Current time:" in result


# ---------------------------------------------------------------------------
# set_timer
# ---------------------------------------------------------------------------


def test_set_timer_positive():
    entry = _TOOL_REGISTRY["set_timer"]
    result = entry["execute"]("set_timer", {"seconds": 5, "label": "test"})
    assert "Timer set:" in result
    assert "'test'" in result
    assert "5 seconds" in result


def test_set_timer_negative():
    entry = _TOOL_REGISTRY["set_timer"]
    result = entry["execute"]("set_timer", {"seconds": -1})
    assert "must be positive" in result.lower()


def test_set_timer_zero():
    entry = _TOOL_REGISTRY["set_timer"]
    result = entry["execute"]("set_timer", {"seconds": 0})
    assert "must be positive" in result.lower()


def test_set_timer_minutes_display():
    entry = _TOOL_REGISTRY["set_timer"]
    result = entry["execute"]("set_timer", {"seconds": 90, "label": "pasta"})
    assert "1 minutes 30 seconds" in result


def test_set_timer_default_label():
    entry = _TOOL_REGISTRY["set_timer"]
    result = entry["execute"]("set_timer", {"seconds": 10})
    assert "'timer'" in result


def test_set_timer_replaces_existing():
    """Setting a timer with the same label+seconds replaces the old one."""
    entry = _TOOL_REGISTRY["set_timer"]
    entry["execute"]("set_timer", {"seconds": 300, "label": "dup"})
    result = entry["execute"]("set_timer", {"seconds": 300, "label": "dup"})
    assert "Timer set:" in result
    # Only one timer should remain for this id
    assert "dup_300" in tm._active_timers


def test_cancel_all():
    entry = _TOOL_REGISTRY["set_timer"]
    entry["execute"]("set_timer", {"seconds": 600, "label": "a"})
    entry["execute"]("set_timer", {"seconds": 601, "label": "b"})
    assert len(tm._active_timers) >= 2
    tm.cancel_all()
    assert len(tm._active_timers) == 0
