"""Smart home skill — wraps existing DeviceManager as a Jarvis tool."""

from __future__ import annotations

import json
import logging
from typing import Any

from devices.device_manager import DeviceManager
from auth.permission_manager import PermissionManager
from skills import Skill

LOGGER = logging.getLogger(__name__)


class SmartHomeSkill(Skill):
    """Bridge between the Jarvis skill system and the existing device layer.

    Exposes ``smart_home_control`` and ``smart_home_status`` as Claude tools,
    delegating to the already-working DeviceManager and PermissionManager.
    """

    def __init__(
        self,
        device_manager: DeviceManager,
        permission_manager: PermissionManager,
    ) -> None:
        self.device_manager = device_manager
        self.permission_manager = permission_manager
        self.logger = LOGGER

    @property
    def skill_name(self) -> str:
        return "smart_home"

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        available_devices = list(self.device_manager.get_all_status().keys())
        device_list = ", ".join(available_devices) if available_devices else "none"
        return [
            {
                "name": "smart_home_control",
                "description": (
                    "Control smart home devices: lights, thermostat, door locks, Hue scenes. "
                    "Use for any request to turn on/off, adjust brightness, change color/color_temp, "
                    "set temperature, lock/unlock, or activate a scene. "
                    f"Available devices: {device_list}"
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "device_id": {
                            "type": "string",
                            "description": f"Device identifier. Available: {device_list}",
                        },
                        "action": {
                            "type": "string",
                            "enum": [
                                "turn_on", "turn_off",
                                "set_brightness", "set_color", "set_color_temp",
                                "set_effect", "set_temperature",
                                "lock", "unlock",
                                "activate",
                            ],
                            "description": "The action to perform on the device.",
                        },
                        "value": {
                            "description": (
                                "Action parameter: brightness 0-100, color name (red/blue/warm/cool), "
                                "temperature 16-30, scene name, etc. Omit for on/off/lock/unlock."
                            ),
                        },
                    },
                    "required": ["device_id", "action"],
                },
            },
            {
                "name": "smart_home_status",
                "description": (
                    "Get the current status of smart home devices. "
                    "Omit device_id to get all devices."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "device_id": {
                            "type": "string",
                            "description": "Optional device identifier. Omit for all devices.",
                        },
                    },
                },
            },
        ]

    def execute(self, tool_name: str, tool_input: dict[str, Any], **context: Any) -> str:
        user_role = str(context.get("user_role", "guest"))

        if tool_name == "smart_home_status":
            return self._handle_status(tool_input)

        if tool_name == "smart_home_control":
            return self._handle_control(tool_input, user_role)

        return f"Unknown smart home tool: {tool_name}"

    def _handle_status(self, tool_input: dict[str, Any]) -> str:
        device_id = tool_input.get("device_id")
        if device_id:
            try:
                device = self.device_manager.get_device(device_id)
                return json.dumps(device.get_status(), ensure_ascii=False)
            except KeyError:
                return f"Device not found: {device_id}"
        return json.dumps(self.device_manager.get_all_status(), ensure_ascii=False, indent=2)

    def _handle_control(self, tool_input: dict[str, Any], user_role: str) -> str:
        device_id = str(tool_input.get("device_id", ""))
        action = str(tool_input.get("action", ""))
        value = tool_input.get("value")

        try:
            device = self.device_manager.get_device(device_id)
        except KeyError:
            return f"Device not found: {device_id}"

        if not self.permission_manager.check_permission(user_role, device, action):
            return f"Permission denied: your role '{user_role}' cannot control {device.name}."

        try:
            return self.device_manager.execute_command(device_id, action, value)
        except Exception as exc:
            return f"Failed to execute {action} on {device.name}: {exc}"
