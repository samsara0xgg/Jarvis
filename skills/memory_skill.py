"""Memory skill — lets Claude remember and recall user preferences."""

from __future__ import annotations

import logging
from typing import Any

from memory.manager import MemoryManager
from skills import Skill

LOGGER = logging.getLogger(__name__)


class MemorySkill(Skill):
    """Allows Claude to store and retrieve per-user facts and preferences.

    When a user says "remember that I like coffee" or "what's my daughter's name",
    Claude can use these tools to persist and recall information.

    Supports two backends:
      - ``MemoryManager`` (new, embedding-based)
      - ``UserPreferenceStore`` (legacy, key-value)
    """

    def __init__(self, backend: Any) -> None:
        self._backend = backend
        self._use_manager = isinstance(backend, MemoryManager)
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

        if self._use_manager:
            return self._execute_manager(tool_name, tool_input, user_id)
        return self._execute_legacy(tool_name, tool_input, user_id)

    def _execute_manager(self, tool_name: str, tool_input: dict[str, Any], user_id: str) -> str:
        """Execute using MemoryManager backend."""
        store = self._backend.store
        embedder = self._backend.embedder

        if tool_name == "remember":
            mem_key = str(tool_input.get("key", "")).strip()
            value = str(tool_input.get("value", "")).strip()
            if not mem_key or not value:
                return "Both key and value are required."
            content = f"{mem_key}: {value}"
            try:
                embedding = embedder.encode(content)
            except Exception:
                embedding = None
            # Check if same key already exists → supersede
            existing = store.find_by_key(user_id, "preference", mem_key)
            new_id = store.add_memory(
                user_id=user_id,
                content=content,
                category="preference",
                key=mem_key,
                importance=7.0,
                tags=[mem_key],
                source="explicit",
                embedding=embedding,
            )
            if existing:
                store.supersede_memory(existing["id"], new_id)
            return f"Remembered: {mem_key} = {value}"

        if tool_name == "recall":
            key = tool_input.get("key")
            memories = store.get_active_memories(user_id)
            if not memories:
                return "No stored memories for this user."
            if key:
                key_str = str(key).strip().lower()
                matches = [
                    m for m in memories
                    if key_str in m["content"].lower() or key_str in str(m.get("tags", [])).lower()
                ]
                if not matches:
                    return f"No stored value for '{key}'."
                lines = [f"- {m['content']}" for m in matches]
                return "\n".join(lines)
            lines = [f"- {m['content']}" for m in memories[:20]]
            return "Stored memories:\n" + "\n".join(lines)

        if tool_name == "forget":
            key = str(tool_input.get("key", "")).strip()
            if store.deactivate_memory(user_id, key):
                return f"Forgotten: {key}"
            return f"No stored value for '{key}'."

        return f"Unknown memory tool: {tool_name}"

    def _execute_legacy(self, tool_name: str, tool_input: dict[str, Any], user_id: str) -> str:
        """Execute using legacy UserPreferenceStore backend."""
        if tool_name == "remember":
            key = str(tool_input.get("key", "")).strip()
            value = str(tool_input.get("value", "")).strip()
            if not key or not value:
                return "Both key and value are required."
            self._backend.set(user_id, key, value)
            return f"Remembered: {key} = {value}"

        if tool_name == "recall":
            key = tool_input.get("key")
            if key:
                value = self._backend.get(user_id, str(key).strip())
                if value is None:
                    return f"No stored value for '{key}'."
                return f"{key}: {value}"
            all_prefs = self._backend.get_all(user_id)
            if not all_prefs:
                return "No stored preferences for this user."
            lines = [f"- {k}: {v}" for k, v in all_prefs.items()]
            return "Stored preferences:\n" + "\n".join(lines)

        if tool_name == "forget":
            key = str(tool_input.get("key", "")).strip()
            if self._backend.delete(user_id, key):
                return f"Forgotten: {key}"
            return f"No stored value for '{key}'."

        return f"Unknown memory tool: {tool_name}"
