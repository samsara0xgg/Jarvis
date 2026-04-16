"""Tests for the @jarvis_tool decorator and tool registry."""

from __future__ import annotations

import pytest

from tools import _TOOL_REGISTRY, _EXECUTION_CONTEXT, jarvis_tool


@pytest.fixture(autouse=True)
def _clean_registry():
    """Clear the global registry before each test to avoid cross-test pollution."""
    _TOOL_REGISTRY.clear()
    yield
    _TOOL_REGISTRY.clear()


def test_basic_decoration():
    """@jarvis_tool on a simple function registers name, description, and execute."""

    @jarvis_tool
    def greet(name: str) -> str:
        """Say hello."""
        return f"hello {name}"

    assert "greet" in _TOOL_REGISTRY
    entry = _TOOL_REGISTRY["greet"]
    assert entry["definition"]["name"] == "greet"
    assert entry["definition"]["description"] == "Say hello."
    assert callable(entry["execute"])
    # defaults
    assert entry["read_only"] is True
    assert entry["destructive"] is False
    assert entry["required_role"] == "guest"


def test_decoration_with_params():
    """@jarvis_tool(...) with typed params builds correct schema and metadata."""

    @jarvis_tool(destructive=True, required_role="owner")
    def set_brightness(room: str, level: int, fade: float = 1.0) -> str:
        """Set light brightness."""
        return f"{room} -> {level} fade={fade}"

    entry = _TOOL_REGISTRY["set_brightness"]
    schema = entry["definition"]["input_schema"]

    # properties reflect type hints
    assert schema["properties"]["room"]["type"] == "string"
    assert schema["properties"]["level"]["type"] == "integer"
    assert schema["properties"]["fade"]["type"] == "number"

    # required excludes params with defaults
    assert "room" in schema["required"]
    assert "level" in schema["required"]
    assert "fade" not in schema["required"]

    # decorator kwargs
    assert entry["destructive"] is True
    assert entry["required_role"] == "owner"
    assert entry["read_only"] is True  # default, not overridden


def test_execute_passes_kwargs():
    """_execute extracts from tool_input, coerces types, and calls function."""

    @jarvis_tool
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    execute = _TOOL_REGISTRY["add"]["execute"]

    # ints passed directly
    assert execute("add", {"a": 2, "b": 3}) == 5

    # str→int coercion
    assert execute("add", {"a": "10", "b": "20"}) == 30


def test_function_remains_callable():
    """Decorated function is still directly callable as a normal function."""

    @jarvis_tool
    def multiply(x: int, y: int) -> int:
        """Multiply two numbers."""
        return x * y

    # direct call still works
    assert multiply(3, 4) == 12
    assert multiply(x=5, y=6) == 30


def test_execution_context_accessible():
    """_EXECUTION_CONTEXT is importable and usable as a mutable dict."""

    _EXECUTION_CONTEXT["user_id"] = "allen"
    _EXECUTION_CONTEXT["user_role"] = "owner"

    assert _EXECUTION_CONTEXT["user_id"] == "allen"
    assert _EXECUTION_CONTEXT["user_role"] == "owner"

    _EXECUTION_CONTEXT.clear()
    assert len(_EXECUTION_CONTEXT) == 0
