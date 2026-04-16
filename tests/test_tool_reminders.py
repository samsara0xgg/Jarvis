"""Tests for tools/reminders.py — @jarvis_tool reminder functions."""

from __future__ import annotations

import importlib
import json

import pytest

from tools import _TOOL_REGISTRY, _EXECUTION_CONTEXT


@pytest.fixture(autouse=True)
def _ensure_registered():
    """Re-register reminder tools if another test cleared the registry."""
    import tools.reminders as rm
    if "create_reminder" not in _TOOL_REGISTRY:
        importlib.reload(rm)
    yield


import tools.reminders as rm  # noqa: E402


@pytest.fixture(autouse=True)
def _setup(tmp_path):
    """Init reminders with tmp_path and set user context."""
    fp = tmp_path / "reminders.json"
    rm.init(filepath=str(fp))
    _EXECUTION_CONTEXT["user_id"] = "test_user"
    yield
    _EXECUTION_CONTEXT.pop("user_id", None)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_create_reminder_registered():
    assert "create_reminder" in _TOOL_REGISTRY


def test_list_reminders_registered():
    assert "list_reminders" in _TOOL_REGISTRY


def test_complete_reminder_registered():
    assert "complete_reminder" in _TOOL_REGISTRY


def test_list_reminders_read_only():
    assert _TOOL_REGISTRY["list_reminders"]["read_only"] is True


def test_create_reminder_not_read_only():
    assert _TOOL_REGISTRY["create_reminder"]["read_only"] is False


# ---------------------------------------------------------------------------
# create / list / complete cycle
# ---------------------------------------------------------------------------


def test_create_reminder():
    entry = _TOOL_REGISTRY["create_reminder"]
    result = entry["execute"]("create_reminder", {"content": "Buy milk"})
    assert "Reminder created" in result
    assert "Buy milk" in result


def test_create_reminder_with_time():
    entry = _TOOL_REGISTRY["create_reminder"]
    result = entry["execute"](
        "create_reminder",
        {"content": "Meeting", "remind_at": "2030-01-01 10:00"},
    )
    assert "for 2030-01-01 10:00" in result


def test_create_reminder_empty_content():
    entry = _TOOL_REGISTRY["create_reminder"]
    result = entry["execute"]("create_reminder", {"content": ""})
    assert "cannot be empty" in result.lower()


def test_list_reminders_empty():
    entry = _TOOL_REGISTRY["list_reminders"]
    result = entry["execute"]("list_reminders", {})
    assert "No active reminders" in result


def test_list_reminders_after_create():
    create = _TOOL_REGISTRY["create_reminder"]
    create["execute"]("create_reminder", {"content": "Task A"})
    create["execute"]("create_reminder", {"content": "Task B"})

    lst = _TOOL_REGISTRY["list_reminders"]
    result = lst["execute"]("list_reminders", {})
    assert "Task A" in result
    assert "Task B" in result


def test_complete_reminder():
    create = _TOOL_REGISTRY["create_reminder"]
    result = create["execute"]("create_reminder", {"content": "Done soon"})
    # Extract ID from "Reminder created (ID: xxxxxxxx)"
    rid = result.split("ID: ")[1].split(")")[0]

    comp = _TOOL_REGISTRY["complete_reminder"]
    result = comp["execute"]("complete_reminder", {"reminder_id": rid})
    assert "marked as done" in result.lower()

    # Should not appear in listing
    lst = _TOOL_REGISTRY["list_reminders"]
    result = lst["execute"]("list_reminders", {})
    assert "No active reminders" in result


def test_complete_reminder_not_found():
    entry = _TOOL_REGISTRY["complete_reminder"]
    result = entry["execute"]("complete_reminder", {"reminder_id": "nonexist"})
    assert "not found" in result.lower()


def test_user_isolation():
    """Reminders from one user are not visible to another."""
    create = _TOOL_REGISTRY["create_reminder"]
    create["execute"]("create_reminder", {"content": "User1 item"})

    _EXECUTION_CONTEXT["user_id"] = "other_user"
    lst = _TOOL_REGISTRY["list_reminders"]
    result = lst["execute"]("list_reminders", {})
    assert "No active reminders" in result


def test_persistence(tmp_path):
    """Reminders survive a reload from the same file."""
    fp = tmp_path / "persist_test.json"
    rm.init(filepath=str(fp))

    create = _TOOL_REGISTRY["create_reminder"]
    create["execute"]("create_reminder", {"content": "Persist me"})

    # Re-init from same file
    rm.init(filepath=str(fp))
    lst = _TOOL_REGISTRY["list_reminders"]
    result = lst["execute"]("list_reminders", {})
    assert "Persist me" in result
