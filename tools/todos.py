"""Todo tools — per-user todo lists with JSON persistence."""

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from core.tool_result import FAILURE, NOOP, SUCCESS, make_tool_result
from tools import jarvis_tool, _EXECUTION_CONTEXT

LOGGER = logging.getLogger(__name__)

_persist_dir: Path = Path("data/todos")


def init(persist_dir: str = "data/todos") -> None:
    """Initialize todo module with persistence directory."""
    global _persist_dir
    _persist_dir = Path(persist_dir)
    _persist_dir.mkdir(parents=True, exist_ok=True)


@jarvis_tool(
    read_only=False,
    lifecycle={
        "status": "active",
        "reason": "Low-risk personal capture capability.",
        "reviewed_at": "2026-05-10",
        "phase3_action": "Enhance structured output only; no rewrite required.",
        "replacement": None,
    },
    exposure={
        "expose_to_llm": True,
        "allow_regex": True,
        "allow_frontend_direct": False,
    },
    classification={
        "layer": "primitive",
        "primary": "state_changing",
        "risk_level": "low",
        "has_side_effects": True,
    },
)
def add_todo(content: str, priority: str = "medium") -> str:
    """Add a todo item for the current user."""
    content = content.strip()
    if not content:
        return make_tool_result(
            FAILURE,
            "Todo content cannot be empty.",
            error_code="empty_content",
        )
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
    return make_tool_result(
        SUCCESS,
        f"Todo added (ID: {todo['id']}): '{content}' [{priority}].",
        data={"todo": todo},
    )


@jarvis_tool(
    read_only=True,
    lifecycle={
        "status": "active",
        "reason": "Low-risk read-only todo query.",
        "reviewed_at": "2026-05-10",
        "phase3_action": "Improve voice/document rendering for list output.",
        "replacement": None,
    },
    exposure={
        "expose_to_llm": True,
        "allow_regex": True,
        "allow_frontend_direct": False,
    },
    classification={
        "layer": "primitive",
        "primary": "read_only",
        "risk_level": "low",
        "has_side_effects": False,
    },
)
def list_todos() -> str:
    """List all incomplete todos for the current user."""
    user_id = _EXECUTION_CONTEXT.get("user_id") or "_anonymous"
    data = _load(user_id)
    active = [t for t in data if not t.get("done", False) and not t.get("archived", False)]
    if not active:
        return make_tool_result(SUCCESS, "No active todos.", data={"todos": []})
    lines = []
    for t in active:
        lines.append(f"- [{t['id']}] ({t.get('priority', 'medium')}) {t['content']}")
    return make_tool_result(
        SUCCESS,
        "Todos:\n" + "\n".join(lines),
        data={"todos": active},
    )


@jarvis_tool(
    read_only=False,
    lifecycle={
        "status": "active",
        "reason": "Low-risk completion action when todo_id is explicit.",
        "reviewed_at": "2026-05-10",
        "phase3_action": "Add ambiguity handling for natural-language todo references.",
        "replacement": None,
    },
    exposure={
        "expose_to_llm": True,
        "allow_regex": True,
        "allow_frontend_direct": False,
    },
    classification={
        "layer": "primitive",
        "primary": "state_changing",
        "risk_level": "low",
        "has_side_effects": True,
    },
)
def complete_todo(todo_id: str) -> str:
    """Mark a todo as completed by its ID."""
    user_id = _EXECUTION_CONTEXT.get("user_id") or "_anonymous"
    data = _load(user_id)
    for t in data:
        if t.get("id") == todo_id:
            if t.get("archived", False):
                return make_tool_result(
                    FAILURE,
                    f"Todo {todo_id} is archived and cannot be completed.",
                    data={"todo": t},
                    error_code="todo_archived",
                    outcome_type="failed",
                    verified=False,
                    verification_source="todo_store_ack",
                )
            t["done"] = True
            t["completed_at"] = datetime.now().isoformat()
            _save(user_id, data)
            return make_tool_result(
                SUCCESS,
                f"Todo '{t['content']}' completed.",
                data={"todo": t},
                outcome_type="updated",
                verified=True,
                verification_source="todo_store_ack",
            )
    return make_tool_result(
        FAILURE,
        f"Todo {todo_id} not found.",
        data={"todo_id": todo_id},
        error_code="todo_not_found",
    )


@jarvis_tool(
    read_only=False,
    lifecycle={
        "status": "active",
        "reason": "Delete is implemented as archive/soft-delete with undo token.",
        "reviewed_at": "2026-05-10",
        "phase3_action": "Monitor ambiguity cases and add natural-language candidate resolution later.",
        "replacement": None,
    },
    exposure={
        "expose_to_llm": True,
        "allow_regex": False,
        "allow_frontend_direct": False,
    },
    classification={
        "layer": "primitive",
        "primary": "state_changing",
        "risk_level": "low",
        "has_side_effects": True,
    },
)
def delete_todo(todo_id: str) -> str:
    """Archive a todo by its ID and return an undo token."""
    user_id = _EXECUTION_CONTEXT.get("user_id") or "_anonymous"
    data = _load(user_id)
    for t in data:
        if t.get("id") == todo_id:
            if t.get("archived", False):
                return make_tool_result(
                    NOOP,
                    f"Todo '{t['content']}' was already archived.",
                    data={"todo": t, "undo_token": t.get("undo_token")},
                    outcome_type="no_change",
                    verified=True,
                    verification_source="todo_store_ack",
                    claim_policy={
                        "allowed_claims": ["todo_already_archived"],
                        "forbidden_claims": ["todo_permanently_deleted"],
                    },
                )
            undo_token = str(uuid.uuid4())[:12]
            t["archived"] = True
            t["archived_at"] = datetime.now().isoformat()
            t["undo_token"] = undo_token
            _save(user_id, data)
            return make_tool_result(
                SUCCESS,
                f"Todo '{t['content']}' archived. Undo token: {undo_token}.",
                data={"todo": t, "undo_token": undo_token},
                outcome_type="archived",
                verified=True,
                verification_source="todo_store_ack",
                claim_policy={
                    "allowed_claims": ["todo_archived"],
                    "forbidden_claims": ["todo_permanently_deleted"],
                },
            )
    return make_tool_result(
        FAILURE,
        f"Todo {todo_id} not found.",
        data={"todo_id": todo_id},
        error_code="todo_not_found",
        outcome_type="failed",
        verified=False,
        verification_source="todo_store_ack",
    )


@jarvis_tool(
    read_only=False,
    lifecycle={
        "status": "active",
        "reason": "Undo path for soft-deleted todos.",
        "reviewed_at": "2026-05-10",
        "phase3_action": "Enhance with expiry if trace shows stale undo-token misuse.",
        "replacement": None,
    },
    exposure={
        "expose_to_llm": True,
        "allow_regex": False,
        "allow_frontend_direct": False,
    },
    classification={
        "layer": "primitive",
        "primary": "state_changing",
        "risk_level": "low",
        "has_side_effects": True,
    },
)
def undo_delete_todo(undo_token: str) -> str:
    """Restore an archived todo by undo token."""
    token = undo_token.strip()
    if not token:
        return make_tool_result(
            FAILURE,
            "Undo token cannot be empty.",
            error_code="empty_undo_token",
            outcome_type="failed",
            verified=False,
            verification_source="todo_store_ack",
        )
    user_id = _EXECUTION_CONTEXT.get("user_id") or "_anonymous"
    data = _load(user_id)
    for t in data:
        if t.get("undo_token") == token:
            t["archived"] = False
            t["restored_at"] = datetime.now().isoformat()
            t.pop("archived_at", None)
            t.pop("undo_token", None)
            _save(user_id, data)
            return make_tool_result(
                SUCCESS,
                f"Todo '{t['content']}' restored.",
                data={"todo": t},
                outcome_type="updated",
                verified=True,
                verification_source="todo_store_ack",
                claim_policy={
                    "allowed_claims": ["todo_restored"],
                    "forbidden_claims": ["todo_permanently_deleted"],
                },
            )
    return make_tool_result(
        FAILURE,
        "Undo token not found.",
        data={"undo_token": token},
        error_code="undo_token_not_found",
        outcome_type="failed",
        verified=False,
        verification_source="todo_store_ack",
    )


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
