"""Smart home tools — wraps DeviceManager + PermissionManager."""

from __future__ import annotations

import json
import logging
from typing import Any

from tools import jarvis_tool, _EXECUTION_CONTEXT

LOGGER = logging.getLogger(__name__)

_device_manager: Any = None
_permission_manager: Any = None


def init(device_manager: Any, permission_manager: Any) -> None:
    """Inject dependencies at startup. Called by jarvis.py."""
    global _device_manager, _permission_manager
    _device_manager = device_manager
    _permission_manager = permission_manager


@jarvis_tool(destructive=True, read_only=False, required_role="guest")
def smart_home_control(device_id: str, action: str, value: str = "") -> str:
    """Control smart home devices: lights, thermostat, door locks, Hue scenes. Use for turn on/off, adjust brightness, change color/color_temp, set temperature, lock/unlock, or activate a scene."""
    user_role = _EXECUTION_CONTEXT.get("user_role", "owner")

    try:
        device = _device_manager.get_device(device_id)
    except KeyError:
        return f"Device not found: {device_id}"

    if not _permission_manager.check_permission(user_role, device, action):
        return f"Permission denied: your role '{user_role}' cannot control {device.name}."

    try:
        return _device_manager.execute_command(device_id, action, value if value else None)
    except Exception as exc:
        return f"Failed to execute {action} on {device.name}: {exc}"


@jarvis_tool(read_only=True)
def smart_home_status(device_id: str = "") -> str:
    """Get the current status of smart home devices. Omit device_id to get all devices."""
    if device_id:
        try:
            device = _device_manager.get_device(device_id)
            return json.dumps(device.get_status(), ensure_ascii=False)
        except KeyError:
            return f"Device not found: {device_id}"
    return json.dumps(_device_manager.get_all_status(), ensure_ascii=False, indent=2)
