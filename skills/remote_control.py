"""Remote control skill — LLM-callable tools for controlling a remote computer."""

from __future__ import annotations

import logging
from typing import Any

from skills import Skill

LOGGER = logging.getLogger(__name__)


class RemoteControlSkill(Skill):
    """Control a remote computer via WebSocket agent."""

    def __init__(self, config: dict) -> None:
        self._config = config
        self._client: Any = None
        self.logger = LOGGER

    @property
    def skill_name(self) -> str:
        return "remote_control"

    def get_required_role(self) -> str:
        return "owner"

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "remote_open_app",
                "description": "Open an application on the remote computer.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "app_name": {
                            "type": "string",
                            "description": "Application name, e.g. 'Chrome', 'Terminal', 'Spotify'.",
                        },
                    },
                    "required": ["app_name"],
                },
            },
            {
                "name": "remote_close_app",
                "description": "Close an application on the remote computer.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "app_name": {
                            "type": "string",
                            "description": "Application name to close.",
                        },
                    },
                    "required": ["app_name"],
                },
            },
            {
                "name": "remote_screenshot",
                "description": "Take a screenshot of the remote computer.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "remote_volume",
                "description": "Set the volume on the remote computer (0-100).",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "level": {
                            "type": "integer",
                            "description": "Volume level 0-100.",
                        },
                    },
                    "required": ["level"],
                },
            },
            {
                "name": "remote_lock",
                "description": "Lock the remote computer screen.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "remote_media",
                "description": "Control media playback on the remote computer.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["play", "pause", "playpause", "next", "previous"],
                            "description": "Media action.",
                        },
                    },
                    "required": ["action"],
                },
            },
            {
                "name": "remote_open_url",
                "description": "Open a URL in the browser on the remote computer.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "URL to open.",
                        },
                    },
                    "required": ["url"],
                },
            },
            {
                "name": "remote_system_info",
                "description": "Get system information from the remote computer.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "remote_list_apps",
                "description": "List running applications on the remote computer.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
        ]

    def execute(self, tool_name: str, tool_input: dict[str, Any], **context: Any) -> str:
        client = self._get_client()
        if client is None:
            return "Remote control is not configured or unavailable."

        dispatch = {
            "remote_open_app": ("open_app", {"app_name": tool_input.get("app_name")}),
            "remote_close_app": ("close_app", {"app_name": tool_input.get("app_name")}),
            "remote_screenshot": ("screenshot", {}),
            "remote_volume": ("set_volume", {"level": tool_input.get("level")}),
            "remote_lock": ("lock_screen", {}),
            "remote_media": ("media_control", {"action": tool_input.get("action")}),
            "remote_open_url": ("open_url", {"url": tool_input.get("url")}),
            "remote_system_info": ("system_info", {}),
            "remote_list_apps": ("list_running_apps", {}),
        }

        entry = dispatch.get(tool_name)
        if entry is None:
            return f"Unknown remote tool: {tool_name}"

        action, params = entry
        try:
            result = client.send_command_sync(action, params)
            if result.get("success"):
                data = result.get("data", "Done.")
                if isinstance(data, dict):
                    return "\n".join(f"{k}: {v}" for k, v in data.items())
                if isinstance(data, list):
                    return ", ".join(str(x) for x in data)
                # Truncate screenshot base64 for LLM
                if action == "screenshot" and isinstance(data, str) and len(data) > 200:
                    return "Screenshot captured successfully (base64 data available)."
                return str(data)
            return f"Remote command failed: {result.get('error', 'unknown error')}"
        except Exception as exc:
            self.logger.exception("Remote control error")
            return f"Remote control error: {exc}"

    def _get_client(self) -> Any:
        """Lazy-initialize the remote client."""
        if self._client is not None:
            return self._client
        try:
            from remote.client import RemoteClient
            self._client = RemoteClient(self._config)
            return self._client
        except ImportError:
            self.logger.warning("websockets not installed. Remote control unavailable.")
            return None
