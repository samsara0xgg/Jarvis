"""Todo tools — per-user todo lists with JSON persistence."""

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from tools import jarvis_tool, _EXECUTION_CONTEXT

LOGGER = logging.getLogger(__name__)

_persist_dir: Path = Path("data/todos")


def init(persist_dir: str = "data/todos") -> None:
    """Initialize todo module with persistence directory."""
    global _persist_dir
    _persist_dir = Path(persist_dir)
    _persist_dir.mkdir(parents=True, exist_ok=True)


@jarvis_tool(read_only=False)
def add_todo(content: str, priority: str = "medium") -> str:
    """Add a todo item for the current user."""
    content = content.strip()
    if not content:
        return "Todo content cannot be empty."
    priority = priority.strip().lower()

    user_id = _EXECUTION_CONTEXT.get("user_id") or "_anonymous"
    todo = {
        "id": str(uuid.uuid4())[:8],
        "content": content,
        "priority": priority,
        "done": False,
        "created_at": datetime.now().isoformat(),
    }
    data = _load(user_id)
    data.append(todo)
    _save(user_id, data)
    return f"Todo added (ID: {todo['id']}): '{content}' [{priority}]."


@jarvis_tool(read_only=True)
def list_todos() -> str:
    """List all incomplete todos for the current user."""
    user_id = _EXECUTION_CONTEXT.get("user_id") or "_anonymous"
    data = _load(user_id)
    active = [t for t in data if not t.get("done", False)]
    if not active:
        return "No active todos."
    lines = []
    for t in active:
        lines.append(f"- [{t['id']}] ({t.get('priority', 'medium')}) {t['content']}")
    return "Todos:\n" + "\n".join(lines)


@jarvis_tool(read_only=False)
def complete_todo(todo_id: str) -> str:
    """Mark a todo as completed by its ID."""
    user_id = _EXECUTION_CONTEXT.get("user_id") or "_anonymous"
    data = _load(user_id)
    for t in data:
        if t.get("id") == todo_id:
            t["done"] = True
            _save(user_id, data)
            return f"Todo '{t['content']}' completed."
    return f"Todo {todo_id} not found."


@jarvis_tool(read_only=False)
def delete_todo(todo_id: str) -> str:
    """Delete a todo by its ID."""
    user_id = _EXECUTION_CONTEXT.get("user_id") or "_anonymous"
    data = _load(user_id)
    for i, t in enumerate(data):
        if t.get("id") == todo_id:
            removed = data.pop(i)
            _save(user_id, data)
            return f"Todo '{removed['content']}' deleted."
    return f"Todo {todo_id} not found."


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _filepath(user_id: str) -> Path:
    """Return the JSON file path for a given user."""
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in user_id)
    return _persist_dir / f"{safe_id}.json"


def _load(user_id: str) -> list[dict[str, Any]]:
    """Load todos for a user from JSON file."""
    fp = _filepath(user_id)
    if not fp.exists():
        return []
    try:
        with fp.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(user_id: str, data: list[dict[str, Any]]) -> None:
    """Save todos for a user to JSON file."""
    fp = _filepath(user_id)
    with fp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
