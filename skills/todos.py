"""Todo skill — per-user todo lists with JSON persistence."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from skills import Skill

LOGGER = logging.getLogger(__name__)


class TodoSkill(Skill):
    """Per-user todo list management."""

    def __init__(self, config: dict) -> None:
        self.persist_dir = Path(
            config.get("skills", {}).get("todos", {}).get("dir", "data/todos")
        )
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self.logger = LOGGER

    @property
    def skill_name(self) -> str:
        return "todos"

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "add_todo",
                "description": "Add a todo item for the current user.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "The todo item."},
                        "priority": {
                            "type": "string",
                            "enum": ["low", "medium", "high"],
                            "description": "Priority level. Defaults to medium.",
                        },
                    },
                    "required": ["content"],
                },
            },
            {
                "name": "list_todos",
                "description": "List all incomplete todos for the current user.",
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "complete_todo",
                "description": "Mark a todo as completed by its ID.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "todo_id": {"type": "string", "description": "The todo ID."},
                    },
                    "required": ["todo_id"],
                },
            },
            {
                "name": "delete_todo",
                "description": "Delete a todo by its ID.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "todo_id": {"type": "string", "description": "The todo ID."},
                    },
                    "required": ["todo_id"],
                },
            },
        ]

    def execute(self, tool_name: str, tool_input: dict[str, Any], **context: Any) -> str:
        user_id = context.get("user_id") or "_anonymous"

        if tool_name == "add_todo":
            return self._add(user_id, tool_input)
        if tool_name == "list_todos":
            return self._list(user_id)
        if tool_name == "complete_todo":
            return self._complete(user_id, tool_input)
        if tool_name == "delete_todo":
            return self._delete(user_id, tool_input)
        return f"Unknown todo tool: {tool_name}"

    def _add(self, user_id: str, tool_input: dict[str, Any]) -> str:
        content = str(tool_input.get("content", "")).strip()
        if not content:
            return "Todo content cannot be empty."
        priority = str(tool_input.get("priority", "medium")).strip().lower()

        todo = {
            "id": str(uuid.uuid4())[:8],
            "content": content,
            "priority": priority,
            "done": False,
            "created_at": datetime.now().isoformat(),
        }
        data = self._load(user_id)
        data.append(todo)
        self._save(user_id, data)
        return f"Todo added (ID: {todo['id']}): '{content}' [{priority}]."

    def _list(self, user_id: str) -> str:
        data = self._load(user_id)
        active = [t for t in data if not t.get("done", False)]
        if not active:
            return "No active todos."
        lines = []
        for t in active:
            lines.append(f"- [{t['id']}] ({t.get('priority', 'medium')}) {t['content']}")
        return "Todos:\n" + "\n".join(lines)

    def _complete(self, user_id: str, tool_input: dict[str, Any]) -> str:
        todo_id = str(tool_input.get("todo_id", "")).strip()
        data = self._load(user_id)
        for t in data:
            if t.get("id") == todo_id:
                t["done"] = True
                self._save(user_id, data)
                return f"Todo '{t['content']}' completed."
        return f"Todo {todo_id} not found."

    def _delete(self, user_id: str, tool_input: dict[str, Any]) -> str:
        todo_id = str(tool_input.get("todo_id", "")).strip()
        data = self._load(user_id)
        for i, t in enumerate(data):
            if t.get("id") == todo_id:
                removed = data.pop(i)
                self._save(user_id, data)
                return f"Todo '{removed['content']}' deleted."
        return f"Todo {todo_id} not found."

    def _load(self, user_id: str) -> list[dict[str, Any]]:
        filepath = self._filepath(user_id)
        if not filepath.exists():
            return []
        try:
            with filepath.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def _save(self, user_id: str, data: list[dict[str, Any]]) -> None:
        filepath = self._filepath(user_id)
        with filepath.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _filepath(self, user_id: str) -> Path:
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in user_id)
        return self.persist_dir / f"{safe_id}.json"
