"""Automation skill — trigger multi-step scenes."""

from __future__ import annotations

import logging
from typing import Any

from skills import Skill

LOGGER = logging.getLogger(__name__)


class AutomationSkill(Skill):
    """LLM-callable skill for triggering automation scenes."""

    def __init__(self, automation_engine: Any) -> None:
        self._engine = automation_engine
        self.logger = LOGGER

    @property
    def skill_name(self) -> str:
        return "automation"

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "run_automation",
                "description": "Execute a pre-configured automation scene.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "scene_name": {
                            "type": "string",
                            "description": "Name of the scene to execute.",
                        },
                    },
                    "required": ["scene_name"],
                },
            },
            {
                "name": "list_automations",
                "description": "List all available automation scenes.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
        ]

    def execute(self, tool_name: str, tool_input: dict[str, Any], **context: Any) -> str:
        if tool_name == "run_automation":
            return self._run(tool_input)
        if tool_name == "list_automations":
            return self._list()
        return f"Unknown automation tool: {tool_name}"

    def _run(self, tool_input: dict[str, Any]) -> str:
        scene_name = str(tool_input.get("scene_name", "")).strip()
        if not scene_name:
            return "scene_name is required."
        results = self._engine.execute_scene(scene_name)
        return f"Executed scene '{scene_name}':\n" + "\n".join(results)

    def _list(self) -> str:
        scenes = self._engine.list_scenes()
        if not scenes:
            return "No automation scenes configured."
        return "Available scenes: " + ", ".join(scenes)
