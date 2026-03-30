"""WebSocket protocol definitions for Jarvis remote control."""

from __future__ import annotations

import uuid
from typing import Any

# Message types
MSG_COMMAND = "command"
MSG_RESULT = "result"
MSG_AUTH = "auth"
MSG_AUTH_OK = "auth_ok"
MSG_AUTH_FAIL = "auth_fail"
MSG_PING = "ping"
MSG_PONG = "pong"

# Supported actions
ACTIONS = {
    "open_app",
    "close_app",
    "set_volume",
    "get_volume",
    "screenshot",
    "lock_screen",
    "system_info",
    "run_command",
    "media_control",
    "open_url",
    "type_text",
    "get_active_window",
    "list_running_apps",
}


def make_command(action: str, params: dict | None = None, request_id: str | None = None) -> dict:
    """Build a command message to send to the remote agent.

    Args:
        action: One of the supported ACTIONS.
        params: Optional parameters for the action.
        request_id: Optional unique ID; auto-generated if omitted.

    Returns:
        A dict ready to be JSON-serialised and sent over the WebSocket.
    """
    if action not in ACTIONS:
        raise ValueError(f"Unknown action '{action}'. Must be one of: {sorted(ACTIONS)}")
    return {
        "type": MSG_COMMAND,
        "request_id": request_id or uuid.uuid4().hex[:12],
        "action": action,
        "params": params or {},
    }


def make_result(request_id: str, success: bool, data: Any = None, error: str | None = None) -> dict:
    """Build a result message returned by the remote agent.

    Args:
        request_id: The request_id from the original command.
        success: Whether the command succeeded.
        data: Arbitrary result payload on success.
        error: Error description on failure.

    Returns:
        A dict ready to be JSON-serialised and sent over the WebSocket.
    """
    msg: dict[str, Any] = {
        "type": MSG_RESULT,
        "request_id": request_id,
        "success": success,
    }
    if data is not None:
        msg["data"] = data
    if error is not None:
        msg["error"] = error
    return msg


def make_auth(token: str) -> dict:
    """Build an authentication message.

    Args:
        token: The one-time token printed by the remote agent on startup.

    Returns:
        A dict ready to be JSON-serialised and sent over the WebSocket.
    """
    return {
        "type": MSG_AUTH,
        "token": token,
    }
