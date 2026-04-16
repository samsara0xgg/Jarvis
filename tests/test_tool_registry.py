"""Tests for core/tool_registry.py — ToolRegistry unified dispatch."""

from __future__ import annotations

import os
import textwrap

import pytest

from tools import _TOOL_REGISTRY, _EXECUTION_CONTEXT, jarvis_tool


# ---------------------------------------------------------------------------
# Setup / teardown
# ---------------------------------------------------------------------------

_SAVED_REGISTRY: dict = {}


def setup_function():
    """Snapshot and clear the global registry so each test is isolated."""
    _SAVED_REGISTRY.update(_TOOL_REGISTRY)
    _TOOL_REGISTRY.clear()
    _EXECUTION_CONTEXT.clear()


def teardown_function():
    """Restore original registry entries."""
    _TOOL_REGISTRY.clear()
    _TOOL_REGISTRY.update(_SAVED_REGISTRY)
    _SAVED_REGISTRY.clear()


# ---------------------------------------------------------------------------
# Test tools (registered fresh per test via helpers)
# ---------------------------------------------------------------------------

def _register_test_tools():
    """Register two test tools and return their names."""
    @jarvis_tool
    def test_func(x: str) -> str:
        """Test function"""
        return f"result: {x}"

    @jarvis_tool(required_role="owner")
    def admin_func() -> str:
        """Admin only"""
        return "admin"

    return test_func, admin_func


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_registry_finds_python_tools():
    """Python tools registered via @jarvis_tool are discovered."""
    _register_test_tools()

    from core.tool_registry import ToolRegistry
    reg = ToolRegistry(config={})

    assert "test_func" in [d["name"] for d in reg.get_tool_definitions(user_role="owner")]
    assert "admin_func" in [d["name"] for d in reg.get_tool_definitions(user_role="owner")]


def test_registry_rbac_filters():
    """Guest sees test_func but not admin_func; owner sees both."""
    _register_test_tools()

    from core.tool_registry import ToolRegistry
    reg = ToolRegistry(config={})

    guest_names = [d["name"] for d in reg.get_tool_definitions(user_role="guest")]
    assert "test_func" in guest_names
    assert "admin_func" not in guest_names

    owner_names = [d["name"] for d in reg.get_tool_definitions(user_role="owner")]
    assert "test_func" in owner_names
    assert "admin_func" in owner_names


def test_registry_execute():
    """Execute a Python tool and verify the result."""
    _register_test_tools()

    from core.tool_registry import ToolRegistry
    reg = ToolRegistry(config={})

    result = reg.execute("test_func", {"x": "hello"}, user_id="u1", user_role="owner")
    assert result == "result: hello"


def test_registry_execute_unknown():
    """Executing a nonexistent tool returns an error string."""
    from core.tool_registry import ToolRegistry
    reg = ToolRegistry(config={})

    result = reg.execute("no_such_tool", {})
    assert "Error" in result
    assert "no_such_tool" in result


def test_registry_count():
    """Count includes both registered test tools."""
    _register_test_tools()

    from core.tool_registry import ToolRegistry
    reg = ToolRegistry(config={})

    assert reg.count() >= 2


def test_registry_sets_execution_context():
    """Execute sets _EXECUTION_CONTEXT before calling the tool."""
    _register_test_tools()

    from core.tool_registry import ToolRegistry
    reg = ToolRegistry(config={})

    reg.execute("test_func", {"x": "ctx"}, user_id="user42", user_role="family")
    assert _EXECUTION_CONTEXT["user_id"] == "user42"
    assert _EXECUTION_CONTEXT["user_role"] == "family"


def test_registry_loads_yaml_skills(tmp_path):
    """Registry picks up YAML skill files from scanned directories."""
    yaml_content = textwrap.dedent("""\
        name: test_yaml_skill
        description: "A test YAML skill"
        version: 1
        status: live
        parameters:
          - name: city
            type: string
            description: "City"
            required: false
            default: Victoria
        action:
          type: http_get
          url: "https://example.com/{{ city }}"
          headers: {}
          timeout_ms: 5000
        response:
          template: "ok"
        security:
          allowed_domains:
            - example.com
    """)
    (tmp_path / "test_skill.yaml").write_text(yaml_content)

    from core.tool_registry import ToolRegistry
    reg = ToolRegistry(config={}, yaml_dirs=[str(tmp_path)])

    names = [d["name"] for d in reg.get_tool_definitions()]
    assert "test_yaml_skill" in names
    assert reg.count() >= 1


def test_registry_skips_deprecated_yaml(tmp_path):
    """YAML skills with status=deprecated are not loaded."""
    yaml_content = textwrap.dedent("""\
        name: old_skill
        description: "Deprecated"
        version: 1
        status: deprecated
        parameters: []
        action:
          type: http_get
          url: "https://example.com/"
          headers: {}
          timeout_ms: 5000
        response:
          template: "nope"
    """)
    (tmp_path / "old.yaml").write_text(yaml_content)

    from core.tool_registry import ToolRegistry
    reg = ToolRegistry(config={}, yaml_dirs=[str(tmp_path)])

    names = [d["name"] for d in reg.get_tool_definitions()]
    assert "old_skill" not in names


def test_registry_rbac_execute_denied():
    """Executing a tool without sufficient role returns permission denied."""
    _register_test_tools()

    from core.tool_registry import ToolRegistry
    reg = ToolRegistry(config={})

    result = reg.execute("admin_func", {}, user_id="u1", user_role="guest")
    assert "Permission denied" in result
