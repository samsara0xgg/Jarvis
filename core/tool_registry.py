"""ToolRegistry — unified dispatch for Python @jarvis_tool functions + YAML skills.

Replaces the old SkillRegistry with a single registry that combines both
Python-decorated tools and YAML skill definitions, with RBAC filtering
and execution context propagation.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from core.yaml_interpreter import YAMLInterpreter
from tools import _EXECUTION_CONTEXT, _TOOL_REGISTRY

LOGGER = logging.getLogger(__name__)

_ROLE_HIERARCHY = {
    "guest": 0,
    "member": 1,
    "resident": 1,
    "family": 2,
    "admin": 2,
    "owner": 3,
}

# Default directories to scan for YAML skills (relative to project root).
_DEFAULT_YAML_DIRS = [
    os.path.join(os.path.dirname(__file__), "..", "skills"),
    os.path.join(os.path.dirname(__file__), "..", "skills", "learned"),
]


class ToolRegistry:
    """Unified tool registry combining Python and YAML tools."""

    def __init__(
        self,
        config: dict,
        *,
        yaml_dirs: list[str] | None = None,
    ) -> None:
        """Initialize: scan YAML dirs, log tool count, warn if >15.

        Args:
            config: Application configuration dict.
            yaml_dirs: Override directories to scan for YAML skills.
                       Defaults to ``skills/`` and ``skills/learned/``.
        """
        self._config = config
        self._interpreter = YAMLInterpreter()
        self._yaml_tools: dict[str, dict[str, Any]] = {}

        dirs = yaml_dirs if yaml_dirs is not None else _DEFAULT_YAML_DIRS
        for d in dirs:
            self._scan_yaml_dir(d)

        total = self.count()
        LOGGER.info("ToolRegistry: %d tools registered", total)
        if total > 15:
            LOGGER.warning("ToolRegistry: %d tools exceed recommended limit of 15", total)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_tool_definitions(self, user_role: str = "guest") -> list[dict]:
        """Return all tool definitions accessible to the given role.

        Args:
            user_role: The authenticated user's role string.

        Returns:
            A list of OpenAI-compatible tool definition dicts.
        """
        user_level = _ROLE_HIERARCHY.get(user_role.strip().lower(), 0)
        tools: list[dict] = []

        # Python tools
        for entry in _TOOL_REGISTRY.values():
            required_level = _ROLE_HIERARCHY.get(
                entry.get("required_role", "guest").strip().lower(), 0,
            )
            if user_level >= required_level:
                tools.append(entry["definition"])

        # YAML tools
        for entry in self._yaml_tools.values():
            required_level = _ROLE_HIERARCHY.get(
                entry.get("required_role", "guest").strip().lower(), 0,
            )
            if user_level >= required_level:
                tools.append(entry["definition"])

        return tools

    def execute(
        self,
        name: str,
        args: dict,
        *,
        user_id: str | None = None,
        user_role: str = "guest",
    ) -> str:
        """Execute a tool by name.

        Sets ``_EXECUTION_CONTEXT`` before calling. Dispatches to Python
        function or YAML interpreter. Returns result string; returns error
        string for unknown tools.

        Args:
            name: The tool name to execute.
            args: Input arguments dict.
            user_id: Authenticated user identifier.
            user_role: Authenticated user role.

        Returns:
            A text result string.
        """
        # Set execution context BEFORE calling the tool
        _EXECUTION_CONTEXT["user_id"] = user_id
        _EXECUTION_CONTEXT["user_role"] = user_role

        # Check Python tools first
        py_entry = _TOOL_REGISTRY.get(name)
        if py_entry is not None:
            # RBAC check
            required_level = _ROLE_HIERARCHY.get(
                py_entry.get("required_role", "guest").strip().lower(), 0,
            )
            user_level = _ROLE_HIERARCHY.get(user_role.strip().lower(), 0)
            if user_level < required_level:
                return f"Permission denied: {name} requires role '{py_entry['required_role']}'."
            try:
                result = py_entry["execute"](name, args, user_id=user_id, user_role=user_role)
                return str(result) if result is not None else ""
            except Exception as exc:
                LOGGER.exception("Python tool %s failed", name)
                return f"Tool execution error: {exc}"

        # Check YAML tools
        yaml_entry = self._yaml_tools.get(name)
        if yaml_entry is not None:
            try:
                return self._interpreter.execute(yaml_entry["skill"], dict(args))
            except Exception as exc:
                LOGGER.exception("YAML tool %s failed", name)
                return f"Tool execution error: {exc}"

        return f"Error: unknown tool '{name}'"

    def count(self) -> int:
        """Total registered tools (Python + YAML)."""
        return len(_TOOL_REGISTRY) + len(self._yaml_tools)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _scan_yaml_dir(self, directory: str) -> None:
        """Scan a directory for *.yaml files and load them."""
        directory = os.path.abspath(directory)
        if not os.path.isdir(directory):
            LOGGER.debug("YAML dir does not exist, skipping: %s", directory)
            return

        for fname in sorted(os.listdir(directory)):
            if not fname.endswith(".yaml"):
                continue
            path = os.path.join(directory, fname)
            try:
                skill = self._interpreter.load_skill(path)
            except Exception:
                LOGGER.exception("Failed to load YAML skill: %s", path)
                continue

            if skill.get("status") == "deprecated":
                LOGGER.debug("Skipping deprecated YAML skill: %s", path)
                continue

            name = skill.get("name", "")
            if not name:
                LOGGER.warning("YAML skill has no name, skipping: %s", path)
                continue

            definition = self._interpreter.to_tool_definition(skill)
            required_role = skill.get("required_role", "guest")

            self._yaml_tools[name] = {
                "definition": definition,
                "skill": skill,
                "required_role": required_role,
            }
            LOGGER.debug("Loaded YAML tool: %s from %s", name, path)
