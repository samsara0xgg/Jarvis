"""Tests for tools/todos.py — @jarvis_tool todo functions."""

from __future__ import annotations

import importlib

import pytest

from tools import _TOOL_REGISTRY, _EXECUTION_CONTEXT


@pytest.fixture(autouse=True)
def _ensure_registered():
    """Re-register todo tools if another test cleared the registry."""
    import tools.todos as td
    if "add_todo" not in _TOOL_REGISTRY:
        importlib.reload(td)
    yield


import tools.todos as td  # noqa: E402


@pytest.fixture(autouse=True)
def _setup(tmp_path):
    """Init todos with tmp_path and set user context."""
    td.init(persist_dir=str(tmp_path / "todos"))
    _EXECUTION_CONTEXT["user_id"] = "test_user"
    yield
    _EXECUTION_CONTEXT.pop("user_id", None)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_add_todo_registered():
    assert "add_todo" in _TOOL_REGISTRY


def test_list_todos_registered():
    assert "list_todos" in _TOOL_REGISTRY


def test_complete_todo_registered():
    assert "complete_todo" in _TOOL_REGISTRY


def test_delete_todo_registered():
    assert "delete_todo" in _TOOL_REGISTRY


def test_list_todos_read_only():
    assert _TOOL_REGISTRY["list_todos"]["read_only"] is True


def test_add_todo_not_read_only():
    assert _TOOL_REGISTRY["add_todo"]["read_only"] is False


# ---------------------------------------------------------------------------
# add / list / complete / delete cycle
# ---------------------------------------------------------------------------


def test_add_todo():
    entry = _TOOL_REGISTRY["add_todo"]
    result = entry["execute"]("add_todo", {"content": "Write tests"})
    assert "Todo added" in result
    assert "Write tests" in result
    assert "[medium]" in result


def test_add_todo_with_priority():
    entry = _TOOL_REGISTRY["add_todo"]
    result = entry["execute"](
        "add_todo", {"content": "Fix bug", "priority": "high"},
    )
    assert "[high]" in result


def test_add_todo_empty_content():
    entry = _TOOL_REGISTRY["add_todo"]
    result = entry["execute"]("add_todo", {"content": ""})
    assert "cannot be empty" in result.lower()


def test_list_todos_empty():
    entry = _TOOL_REGISTRY["list_todos"]
    result = entry["execute"]("list_todos", {})
    assert "No active todos" in result


def test_list_todos_after_add():
    add = _TOOL_REGISTRY["add_todo"]
    add["execute"]("add_todo", {"content": "Item A"})
    add["execute"]("add_todo", {"content": "Item B", "priority": "high"})

    lst = _TOOL_REGISTRY["list_todos"]
    result = lst["execute"]("list_todos", {})
    assert "Item A" in result
    assert "Item B" in result
    assert "(high)" in result


def test_complete_todo():
    add = _TOOL_REGISTRY["add_todo"]
    result = add["execute"]("add_todo", {"content": "Finish this"})
    tid = result.split("ID: ")[1].split(")")[0]

    comp = _TOOL_REGISTRY["complete_todo"]
    result = comp["execute"]("complete_todo", {"todo_id": tid})
    assert "completed" in result.lower()

    # Should not appear in listing
    lst = _TOOL_REGISTRY["list_todos"]
    result = lst["execute"]("list_todos", {})
    assert "No active todos" in result


def test_complete_todo_not_found():
    entry = _TOOL_REGISTRY["complete_todo"]
    result = entry["execute"]("complete_todo", {"todo_id": "nonexist"})
    assert "not found" in result.lower()


def test_delete_todo():
    add = _TOOL_REGISTRY["add_todo"]
    result = add["execute"]("add_todo", {"content": "Delete me"})
    tid = result.split("ID: ")[1].split(")")[0]

    dele = _TOOL_REGISTRY["delete_todo"]
    result = dele["execute"]("delete_todo", {"todo_id": tid})
    assert "deleted" in result.lower()

    lst = _TOOL_REGISTRY["list_todos"]
    result = lst["execute"]("list_todos", {})
    assert "No active todos" in result


def test_delete_todo_not_found():
    entry = _TOOL_REGISTRY["delete_todo"]
    result = entry["execute"]("delete_todo", {"todo_id": "nonexist"})
    assert "not found" in result.lower()


def test_user_isolation():
    """Todos from one user are not visible to another."""
    add = _TOOL_REGISTRY["add_todo"]
    add["execute"]("add_todo", {"content": "User1 todo"})

    _EXECUTION_CONTEXT["user_id"] = "other_user"
    lst = _TOOL_REGISTRY["list_todos"]
    result = lst["execute"]("list_todos", {})
    assert "No active todos" in result


def test_persistence(tmp_path):
    """Todos survive a reload from the same directory."""
    todo_dir = tmp_path / "persist_todos"
    td.init(persist_dir=str(todo_dir))

    add = _TOOL_REGISTRY["add_todo"]
    add["execute"]("add_todo", {"content": "Persist me"})

    # Re-init from same dir
    td.init(persist_dir=str(todo_dir))
    lst = _TOOL_REGISTRY["list_todos"]
    result = lst["execute"]("list_todos", {})
    assert "Persist me" in result
