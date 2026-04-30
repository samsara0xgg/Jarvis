"""Smart home tools — wraps DeviceManager + PermissionManager."""

from __future__ import annotations

import json
import logging
from typing import Any

from tools import jarvis_tool, _EXECUTION_CONTEXT

LOGGER = logging.getLogger(__name__)

_device_manager: Any = None
_permission_manager: Any = None

# Virtual groups: client-side fan-out to multiple physical device_ids.
# Use when Hue Bridge has no native group for the bundle (e.g. two whitelamps
# wired separately on the bridge). Members must be real device_ids.
VIRTUAL_GROUPS: dict[str, list[str]] = {
    "bedroom_group": ["bedroom_lamp_1", "bedroom_lamp_2"],
}


def init(device_manager: Any, permission_manager: Any) -> None:
    """Inject dependencies at startup. Called by jarvis.py."""
    global _device_manager, _permission_manager
    _device_manager = device_manager
    _permission_manager = permission_manager


@jarvis_tool(destructive=True, read_only=False, required_role="guest")
def smart_home_control(device_id: str, action: str, value: str = "") -> str:
    """Control smart home devices: lights, thermostat, door locks, Hue scenes. Use for turn on/off, adjust brightness, change color/color_temp, set temperature, lock/unlock, or activate a scene."""
    user_role = _EXECUTION_CONTEXT.get("user_role", "owner")

    if device_id in VIRTUAL_GROUPS:
        return _execute_virtual_group(device_id, action, value, user_role)

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


def _execute_virtual_group(group_id: str, action: str, value: str, user_role: str) -> str:
    """Fan out one logical command to each member device, aggregate results."""
    successes: list[str] = []
    failures: list[str] = []
    for member_id in VIRTUAL_GROUPS[group_id]:
        try:
            device = _device_manager.get_device(member_id)
        except KeyError:
            failures.append(f"{member_id}: not found")
            continue
        if not _permission_manager.check_permission(user_role, device, action):
            failures.append(f"{device.name}: permission denied")
            continue
        try:
            _device_manager.execute_command(member_id, action, value if value else None)
            successes.append(device.name)
        except Exception as exc:
            failures.append(f"{device.name}: {exc}")
    if not failures:
        return f"{group_id}: {len(successes)}/{len(successes)} OK."
    if not successes:
        return f"{group_id}: all failed — {'; '.join(failures)}"
    return (
        f"{group_id}: {len(successes)}/{len(successes) + len(failures)} OK "
        f"(failed: {'; '.join(failures)})"
    )


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
