"""Tests for the skill registry and base skill framework."""

from __future__ import annotations

import pytest
from typing import Any

from skills import Skill, SkillRegistry


class _DummySkill(Skill):
    """Minimal skill for testing."""

    def __init__(self, name: str = "dummy", role: str = "guest"):
        self._name = name
        self._role = role

    @property
    def skill_name(self) -> str:
        return self._name

    def get_required_role(self) -> str:
        return self._role

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": f"{self._name}_action",
                "description": f"Test tool for {self._name}",
                "input_schema": {
                    "type": "object",
                    "properties": {"arg": {"type": "string"}},
                },
            },
        ]

    def execute(self, tool_name: str, tool_input: dict[str, Any], **context: Any) -> str:
        return f"executed {tool_name} with {tool_input} context={context}"


class _FailingSkill(_DummySkill):
    def execute(self, tool_name: str, tool_input: dict[str, Any], **context: Any) -> str:
        raise RuntimeError("skill exploded")


def test_registry_register_and_dispatch():
    registry = SkillRegistry()
    skill = _DummySkill("test")
    registry.register(skill)

    assert "test" in registry.skill_names
    result = registry.execute("test_action", {"arg": "hello"}, user_id="u1", user_role="guest")
    assert "executed" in result
    assert "hello" in result


def test_registry_returns_tools_filtered_by_role():
    registry = SkillRegistry()
    registry.register(_DummySkill("public", role="guest"))
    registry.register(_DummySkill("admin_only", role="owner"))

    guest_tools = registry.get_tool_definitions("guest")
    assert any(t["name"] == "public_action" for t in guest_tools)
    assert not any(t["name"] == "admin_only_action" for t in guest_tools)

    owner_tools = registry.get_tool_definitions("owner")
    assert any(t["name"] == "public_action" for t in owner_tools)
    assert any(t["name"] == "admin_only_action" for t in owner_tools)


def test_registry_blocks_underprivileged_execution():
    registry = SkillRegistry()
    registry.register(_DummySkill("admin_tool", role="owner"))

    result = registry.execute("admin_tool_action", {}, user_id="u1", user_role="guest")
    assert "Permission denied" in result


def test_registry_handles_unknown_tool():
    registry = SkillRegistry()
    result = registry.execute("nonexistent", {})
    assert "unknown tool" in result.lower()


def test_registry_catches_skill_exceptions():
    registry = SkillRegistry()
    registry.register(_FailingSkill("broken"))

    result = registry.execute("broken_action", {}, user_id="u1", user_role="owner")
    assert "error" in result.lower()
