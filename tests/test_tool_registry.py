"""Tests for core/tool_registry.py — ToolRegistry unified dispatch."""

from __future__ import annotations

import os
import textwrap

import pytest

from core.tool_result import (
    LEGACY_UNVERIFIED,
    SCHEMA_VERSION,
    parse_tool_result,
)
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
    """Execute a Python tool and verify the normalized result."""
    _register_test_tools()

    from core.tool_registry import ToolRegistry
    reg = ToolRegistry(config={})

    result = reg.execute("test_func", {"x": "hello"}, user_id="u1", user_role="owner")
    parsed = parse_tool_result(result)
    assert parsed["schema_version"] == SCHEMA_VERSION
    assert parsed["skill_name"] == "test_func"
    assert parsed["caller"] == "llm"
    assert parsed["message"] == "result: hello"
    assert parsed["outcome"]["type"] == "observed"


def test_registry_execute_unknown():
    """Executing a nonexistent tool returns an error string."""
    from core.tool_registry import ToolRegistry
    reg = ToolRegistry(config={})

    result = reg.execute("no_such_tool", {})
    parsed = parse_tool_result(result)
    assert parsed["status"] == "failure"
    assert parsed["error_code"] == "unknown_tool"
    assert "no_such_tool" in parsed["message"]


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


def test_default_registry_hides_deprecated_yaml_skills():
    """Low-value/high-risk YAML skills are not exposed by default."""
    from core.tool_registry import ToolRegistry

    reg = ToolRegistry(config={})
    names = {d["name"] for d in reg.get_tool_definitions(user_role="owner")}

    assert "get_exchange_rate" not in names
    assert "cc_approve" not in names
    assert "cc_deny" not in names
    assert "cc_slash" in names


def test_default_registry_exposes_phase2_yaml_metadata():
    """Lifecycle metadata is auditable without changing the tool surface."""
    from core.tool_registry import ToolRegistry

    reg = ToolRegistry(config={})
    metadata = {m["name"]: m for m in reg.get_skill_metadata()}

    assert metadata["get_weather"]["lifecycle"]["status"] == "active"
    assert metadata["get_weather"]["exposure"]["expose_to_llm"] is True
    assert metadata["mac_gui"]["lifecycle"]["status"] == "active"
    assert metadata["mac_gui"]["exposure"]["expose_to_llm"] is True
    assert metadata["type_to_focused"]["lifecycle"]["status"] == "active"
    assert metadata["type_to_focused"]["exposure"]["expose_to_llm"] is True
    assert metadata["type_to_focused"]["classification"]["risk_level"] == "high"
    assert metadata["cc_tell"]["lifecycle"]["status"] == "active"
    assert metadata["cc_tell"]["exposure"]["allow_frontend_direct"] is True
    assert metadata["cc_approve"]["lifecycle"]["status"] == "deprecated"
    assert metadata["cc_approve"]["loaded"] is False
    assert metadata["get_exchange_rate"]["lifecycle"]["status"] == "deprecated"


def test_registry_exposes_python_lifecycle_metadata():
    """Python @jarvis_tool metadata participates in the same audit surface."""
    @jarvis_tool(
        read_only=False,
        lifecycle={
            "status": "rewrite_required",
            "reason": "test reason",
            "reviewed_at": "2026-05-10",
            "phase3_action": "test action",
            "replacement": None,
        },
        exposure={
            "expose_to_llm": "limited",
            "allow_regex": False,
            "allow_frontend_direct": False,
        },
        classification={
            "layer": "primitive",
            "primary": "state_changing",
            "risk_level": "medium",
            "has_side_effects": True,
        },
    )
    def needs_rewrite() -> str:
        return "ok"

    from core.tool_registry import ToolRegistry

    reg = ToolRegistry(config={})
    metadata = {m["name"]: m for m in reg.get_skill_metadata()}

    assert metadata["needs_rewrite"]["source"] == "python"
    assert metadata["needs_rewrite"]["lifecycle"]["status"] == "rewrite_required"
    assert metadata["needs_rewrite"]["exposure"]["expose_to_llm"] == "limited"
    assert metadata["needs_rewrite"]["classification"]["risk_level"] == "medium"


def test_registry_rbac_execute_denied():
    """Executing a tool without sufficient role returns permission denied."""
    _register_test_tools()

    from core.tool_registry import ToolRegistry
    reg = ToolRegistry(config={})

    result = reg.execute("admin_func", {}, user_id="u1", user_role="guest")
    parsed = parse_tool_result(result)
    assert parsed["status"] == "failure"
    assert parsed["error_code"] == "permission_denied"
    assert "Permission denied" in parsed["message"]


def test_registry_normalizes_yaml_legacy_side_effect(tmp_path):
    """YAML side-effect plaintext results become V1 evidence envelopes."""
    yaml_content = textwrap.dedent("""\
        name: test_file_write
        description: "A test file-write YAML skill"
        version: 1
        status: live
        parameters:
          - name: body
            type: string
            required: true
        annotations:
          read_only: false
          destructive: false
          idempotent: false
        action:
          type: file_write
          allowed_root: "{{ root }}"
          path: "{{ root }}/note.txt"
          content: "{{ body }}"
        response:
          template: "Saved."
    """)
    root = tmp_path / "vault"
    root.mkdir()
    (tmp_path / "write.yaml").write_text(
        yaml_content.replace("{{ root }}", str(root)),
    )

    from core.tool_registry import ToolRegistry
    reg = ToolRegistry(config={}, yaml_dirs=[str(tmp_path)])

    result = reg.execute(
        "test_file_write",
        {"body": "hello"},
        caller="regex_router",
    )
    parsed = parse_tool_result(result)
    assert parsed["schema_version"] == SCHEMA_VERSION
    assert parsed["skill_name"] == "test_file_write"
    assert parsed["caller"] == "regex_router"
    assert parsed["message"] == "Saved."
    assert parsed["outcome"] == {
        "type": "created",
        "verified": True,
        "verification_source": "file_write_ack",
    }


def test_registry_normalizes_untyped_side_effect_as_unverified():
    """Plain medium-risk side-effect output cannot support completion claims."""
    @jarvis_tool(read_only=False, destructive=True)
    def legacy_side_effect() -> str:
        return "已输入"

    from core.tool_registry import ToolRegistry
    reg = ToolRegistry(config={})

    parsed = parse_tool_result(
        reg.execute("legacy_side_effect", {}, caller="llm")
    )
    assert parsed["status"] == "success"
    assert parsed["message"] == "工具返回了旧格式结果，但没有可验证完成证据。"
    assert parsed["data"]["legacy_raw_result"] == "已输入"
    assert parsed["outcome"]["type"] == LEGACY_UNVERIFIED
