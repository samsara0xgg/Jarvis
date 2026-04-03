"""Skill management — list, disable, remove learned skills via voice."""

from __future__ import annotations

import logging
from typing import Any

from skills import Skill

LOGGER = logging.getLogger(__name__)


class SkillManagementSkill(Skill):
    """Allows users to query and manage learned skills.

    Args:
        skill_loader: SkillLoader instance for metadata and removal.
        skill_registry: SkillRegistry instance for listing.
    """

    def __init__(self, skill_loader: Any, skill_registry: Any) -> None:
        self._loader = skill_loader
        self._registry = skill_registry

    @property
    def skill_name(self) -> str:
        return "skill_mgmt"

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "list_skills",
                "description": (
                    "List all skills Jarvis can use (built-in and learned). "
                    "Use when user asks 'what can you do' or 'what skills do you have'."
                ),
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "disable_skill",
                "description": (
                    "Disable or permanently delete a learned skill. "
                    "Use when user says 'forget skill X' or 'remove skill X'. "
                    "Cannot disable built-in skills."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "skill_name": {
                            "type": "string",
                            "description": "Name of the learned skill to disable or delete.",
                        },
                        "delete": {
                            "type": "boolean",
                            "description": "True to permanently delete file, False to just disable. Default False.",
                        },
                    },
                    "required": ["skill_name"],
                },
            },
        ]

    def execute(self, tool_name: str, tool_input: dict[str, Any], **ctx: Any) -> str:
        if tool_name == "list_skills":
            return self._list_skills()
        if tool_name == "disable_skill":
            return self._disable_skill(
                tool_input.get("skill_name", ""),
                tool_input.get("delete", False),
            )
        return f"Unknown tool: {tool_name}"

    def _list_skills(self) -> str:
        names = self._registry.skill_names
        builtin = [n for n in names if not self._is_learned(n)]
        learned = [n for n in names if self._is_learned(n)]
        parts = [f"内置技能（{len(builtin)}个）：{', '.join(builtin)}"]
        if learned:
            parts.append(f"学会的技能（{len(learned)}个）：{', '.join(learned)}")
        else:
            parts.append("还没学会新技能。")
        return "\n".join(parts)

    def _disable_skill(self, name: str, delete: bool) -> str:
        if not self._is_learned(name):
            return f"'{name}' 是内置技能，不能删除。"
        if delete:
            self._loader.remove_skill(name)
            return f"已永久删除技能 '{name}'。"
        self._loader.update_metadata(name, {"enabled": False})
        return f"已禁用技能 '{name}'，重启后生效。"

    def _is_learned(self, name: str) -> bool:
        """Check if a skill is learned (has metadata AND file exists)."""
        meta = self._loader.get_metadata(name)
        if not meta:
            return False
        from pathlib import Path
        return (Path(self._loader._dir) / f"{name}.py").exists() or bool(meta.get("taught_by"))
