"""Smart home tools — wraps DeviceManager + PermissionManager."""

from __future__ import annotations

import json
import logging
from typing import Any

from core.tool_result import FAILURE, PARTIAL_SUCCESS, SUCCESS, make_tool_result
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


@jarvis_tool(
    destructive=True,
    read_only=False,
    required_role="guest",
    lifecycle={
        "status": "active",
        "reason": "High-value Jarvis capability with proven regex fast-path usage.",
        "reviewed_at": "2026-05-10",
        "phase3_action": "Add entity registry, alias provenance, and postcondition verification.",
        "replacement": None,
    },
    exposure={
        "expose_to_llm": True,
        "allow_regex": True,
        "allow_frontend_direct": False,
    },
    classification={
        "layer": "primitive",
        "primary": "physical_control",
        "risk_level": "medium",
        "has_side_effects": True,
    },
)
def smart_home_control(device_id: str, action: str, value: str = "") -> str:
    """Change smart-home state for a verified device_id.

    Use only after the target entity is resolved to a real, stable device_id
    from smart_home_status, runtime registry, stable profile mapping, explicit
    user-provided ID, or a recent successful tool result. Do not pass casual
    natural-language names as device_id. If the user names a device ambiguously,
    call smart_home_status first or ask one clarification question. Success or
    failure is limited to the returned tool result.
    """
    user_role = _EXECUTION_CONTEXT.get("user_role", "owner")

    if device_id in VIRTUAL_GROUPS:
        return _execute_virtual_group(device_id, action, value, user_role)

    try:
        device = _device_manager.get_device(device_id)
    except KeyError:
        return make_tool_result(
            FAILURE,
            f"Device not found: {device_id}",
            data={"device_id": device_id, "action": action},
            error_code="device_not_found",
        )

    if not _permission_manager.check_permission(user_role, device, action):
        return make_tool_result(
            FAILURE,
            f"Permission denied: your role '{user_role}' cannot control {device.name}.",
            data={"device_id": device_id, "action": action, "role": user_role},
            error_code="permission_denied",
        )

    try:
        message = _device_manager.execute_command(
            device_id,
            action,
            value if value else None,
        )
        return make_tool_result(
            SUCCESS,
            str(message),
            data={"device_id": device_id, "action": action, "value": value or None},
        )
    except Exception as exc:
        return make_tool_result(
            FAILURE,
            f"Failed to execute {action} on {device.name}: {exc}",
            data={"device_id": device_id, "action": action, "value": value or None},
            error_code="execution_failed",
        )


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
    data = {
        "group_id": group_id,
        "action": action,
        "value": value or None,
        "successes": successes,
        "failures": failures,
    }
    if not failures:
        return make_tool_result(
            SUCCESS,
            f"{group_id}: {len(successes)}/{len(successes)} OK.",
            data=data,
        )
    if not successes:
        return make_tool_result(
            FAILURE,
            f"{group_id}: all failed — {'; '.join(failures)}",
            data=data,
            error_code="group_all_failed",
        )
    return make_tool_result(
        PARTIAL_SUCCESS,
        (
            f"{group_id}: {len(successes)}/{len(successes) + len(failures)} OK "
            f"(failed: {'; '.join(failures)})"
        ),
        data=data,
        error_code="group_partial_failure",
    )


@jarvis_tool(
    read_only=True,
    lifecycle={
        "status": "active",
        "reason": "Read-only inventory/status primitive needed for entity resolution.",
        "reviewed_at": "2026-05-10",
        "phase3_action": "Return aliases, area, capabilities, current_state, and last_updated.",
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
def smart_home_status(device_id: str = "") -> str:
    """Read smart-home inventory/status without changing state.

    Use this before control when a user gives a natural-language device name,
    room, group, or ambiguous entity reference. Omit device_id to list all
    controllable entities and their current states. Passing device_id returns
    status for that verified entity only.
    """
    if device_id:
        try:
            device = _device_manager.get_device(device_id)
            status = device.get_status()
            return make_tool_result(
                SUCCESS,
                json.dumps(status, ensure_ascii=False),
                data={"device_id": device_id, "status": status},
            )
        except KeyError:
            return make_tool_result(
                FAILURE,
                f"Device not found: {device_id}",
                data={"device_id": device_id},
                error_code="device_not_found",
            )
    status = _device_manager.get_all_status()
    return make_tool_result(
        SUCCESS,
        json.dumps(status, ensure_ascii=False, indent=2),
        data={"entities": status},
    )
