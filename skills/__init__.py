"""Skill framework: base class and registry for Jarvis tool-calling skills."""

from __future__ import annotations

from abc import ABC, abstractmethod
import logging
from typing import Any

LOGGER = logging.getLogger(__name__)

# Reuse the existing role hierarchy from the auth module.
_ROLE_HIERARCHY = {
    "guest": 0,
    "member": 1,
    "resident": 1,
    "family": 2,
    "admin": 2,
    "owner": 3,
}


class Skill(ABC):
    """Base class for all Jarvis skills.

    Each skill exposes one or more Claude tool definitions and handles
    execution when Claude invokes them.  Mirrors the SmartDevice pattern
    used by the existing device layer.
    """

    @property
    @abstractmethod
    def skill_name(self) -> str:
        """Unique identifier for this skill."""

    @abstractmethod
    def get_tool_definitions(self) -> list[dict[str, Any]]:
        """Return Claude API tool schemas this skill handles."""

    @abstractmethod
    def execute(self, tool_name: str, tool_input: dict[str, Any], **context: Any) -> str:
        """Execute a tool call and return a text result for Claude.

        Args:
            tool_name: The name of the tool being invoked.
            tool_input: The input parameters from Claude.
            **context: Extra context such as ``user_id`` and ``user_role``.

        Returns:
            A human-readable result string that Claude will incorporate
            into its response.
        """

    def get_required_role(self) -> str:
        """Minimum role needed to use this skill.  Override to restrict."""
        return "guest"


class SkillRegistry:
    """Discover, register, and dispatch tool calls to skills.

    Follows the DeviceManager pattern: a central registry that maps
    Claude tool names to skill instances and handles dispatch.
    """

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}
        self._tool_map: dict[str, Skill] = {}
        self.logger = LOGGER

    def register(self, skill: Skill) -> None:
        """Register a skill and index its tool definitions.

        Args:
            skill: The skill instance to register.
        """
        self._skills[skill.skill_name] = skill
        for tool_def in skill.get_tool_definitions():
            tool_name = tool_def["name"]
            self._tool_map[tool_name] = skill
            self.logger.info(
                "Registered tool %s from skill %s", tool_name, skill.skill_name,
            )

    def get_tool_definitions(self, user_role: str = "guest") -> list[dict[str, Any]]:
        """Return all tool definitions accessible to the given role.

        Args:
            user_role: The authenticated user's role string.

        Returns:
            A list of Claude-compatible tool definition dicts.
        """
        user_level = _ROLE_HIERARCHY.get(user_role.strip().lower(), 0)
        tools: list[dict[str, Any]] = []
        for skill in self._skills.values():
            required_level = _ROLE_HIERARCHY.get(
                skill.get_required_role().strip().lower(), 0,
            )
            if user_level >= required_level:
                tools.extend(skill.get_tool_definitions())
        return tools

    def execute(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        *,
        user_id: str | None = None,
        user_role: str = "guest",
    ) -> str:
        """Dispatch a tool call to the appropriate skill.

        Args:
            tool_name: The tool name from Claude's tool_use block.
            tool_input: The input dict from Claude.
            user_id: Authenticated user identifier.
            user_role: Authenticated user role.

        Returns:
            A text result to feed back to Claude.
        """
        skill = self._tool_map.get(tool_name)
        if skill is None:
            return f"Error: unknown tool '{tool_name}'"

        required_level = _ROLE_HIERARCHY.get(
            skill.get_required_role().strip().lower(), 0,
        )
        user_level = _ROLE_HIERARCHY.get(user_role.strip().lower(), 0)
        if user_level < required_level:
            return f"Permission denied: {tool_name} requires role '{skill.get_required_role()}'."

        try:
            return skill.execute(
                tool_name, tool_input, user_id=user_id, user_role=user_role,
            )
        except Exception as exc:
            self.logger.exception("Skill %s failed on tool %s", skill.skill_name, tool_name)
            return f"Tool execution error: {exc}"

    @property
    def skill_names(self) -> list[str]:
        """Return all registered skill names."""
        return list(self._skills.keys())
