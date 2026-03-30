"""Memory skill — lets Claude remember and recall user preferences."""

from __future__ import annotations

import logging
from typing import Any

from memory.user_preferences import UserPreferenceStore
from skills import Skill

LOGGER = logging.getLogger(__name__)


class MemorySkill(Skill):
    """Allows Claude to store and retrieve per-user facts and preferences.

    When a user says "remember that I like coffee" or "what's my daughter's name",
    Claude can use these tools to persist and recall information.
    """

    def __init__(self, preference_store: UserPreferenceStore) -> None:
        self.store = preference_store
        self.logger = LOGGER

    @property
    def skill_name(self) -> str:
        return "memory"

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "remember",
                "description": (
                    "Store a fact or preference about the current user. "
                    "Use when the user says 'remember that...' or shares personal info worth keeping."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "key": {
                            "type": "string",
                            "description": "A short descriptive key, e.g. 'favorite_drink', 'daughter_name', 'work_schedule'.",
                        },
                        "value": {
                            "type": "string",
                            "description": "The value to remember.",
                        },
                    },
                    "required": ["key", "value"],
                },
            },
            {
                "name": "recall",
                "description": (
                    "Retrieve stored facts about the current user. "
                    "Omit key to get all stored preferences."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "key": {
                            "type": "string",
                            "description": "Specific key to recall. Omit for all.",
                        },
                    },
                },
            },
            {
                "name": "forget",
                "description": "Delete a stored fact about the current user.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "key": {
                            "type": "string",
                            "description": "The key to forget.",
                        },
                    },
                    "required": ["key"],
                },
            },
        ]

    def execute(self, tool_name: str, tool_input: dict[str, Any], **context: Any) -> str:
        user_id = context.get("user_id")
        if not user_id:
            return "Cannot use memory for unidentified users."

        if tool_name == "remember":
            key = str(tool_input.get("key", "")).strip()
            value = str(tool_input.get("value", "")).strip()
            if not key or not value:
                return "Both key and value are required."
            self.store.set(user_id, key, value)
            return f"Remembered: {key} = {value}"

        if tool_name == "recall":
            key = tool_input.get("key")
            if key:
                value = self.store.get(user_id, str(key).strip())
                if value is None:
                    return f"No stored value for '{key}'."
                return f"{key}: {value}"
            all_prefs = self.store.get_all(user_id)
            if not all_prefs:
                return "No stored preferences for this user."
            lines = [f"- {k}: {v}" for k, v in all_prefs.items()]
            return "Stored preferences:\n" + "\n".join(lines)

        if tool_name == "forget":
            key = str(tool_input.get("key", "")).strip()
            if self.store.delete(user_id, key):
                return f"Forgotten: {key}"
            return f"No stored value for '{key}'."

        return f"Unknown memory tool: {tool_name}"
