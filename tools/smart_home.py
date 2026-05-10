"""Smart home tools — wraps DeviceManager + PermissionManager."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from core.tool_result import (
    CHANGED,
    FAILURE,
    OBSERVED,
    OUTCOME_FAILED,
    PARTIAL_SUCCESS,
    SUCCESS,
    make_tool_result,
)
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
def smart_home_control(
    device_id: str,
    action: str,
    value: str = "",
    matched_alias: str = "",
    resolution_source: str = "tool_input",
) -> str:
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
        return _execute_virtual_group(
            device_id,
            action,
            value,
            user_role,
            matched_alias=matched_alias,
            resolution_source=resolution_source,
        )

    try:
        device = _device_manager.get_device(device_id)
    except KeyError:
        return make_tool_result(
            FAILURE,
            f"Device not found: {device_id}",
            data={
                "entity": _unknown_entity(
                    device_id,
                    matched_alias=matched_alias,
                    resolution_source=resolution_source,
                ),
                "device_id": device_id,
                "action": action,
            },
            error_code="device_not_found",
            outcome_type=OUTCOME_FAILED,
            verified=False,
            verification_source="none",
            claim_policy=_failed_claim_policy(),
        )

    entity = _entity_payload(
        device,
        matched_alias=matched_alias,
        resolution_source=resolution_source,
    )
    if not _permission_manager.check_permission(user_role, device, action):
        return make_tool_result(
            FAILURE,
            f"Permission denied: your role '{user_role}' cannot control {device.name}.",
            data={
                "entity": entity,
                "device_id": device_id,
                "action": action,
                "role": user_role,
            },
            error_code="permission_denied",
            outcome_type=OUTCOME_FAILED,
            verified=False,
            verification_source="none",
            claim_policy=_failed_claim_policy(),
        )

    try:
        message = _device_manager.execute_command(
            device_id,
            action,
            value if value else None,
        )
        verification = _verification_payload(device, action, value)
        verification_source = str(verification["verification_source"])
        return make_tool_result(
            SUCCESS,
            str(message),
            data={
                "entity": entity,
                "device_id": device_id,
                "action": action,
                "value": value or None,
                "verification": verification,
                "post_state": verification.get("post_state"),
            },
            outcome_type=CHANGED,
            verified=True,
            verification_source=verification_source,
            claim_policy=_smart_home_claim_policy(verification_source),
        )
    except Exception as exc:
        return make_tool_result(
            FAILURE,
            f"Failed to execute {action} on {device.name}: {exc}",
            data={
                "entity": entity,
                "device_id": device_id,
                "action": action,
                "value": value or None,
            },
            error_code="execution_failed",
            outcome_type=OUTCOME_FAILED,
            verified=False,
            verification_source="none",
            claim_policy=_failed_claim_policy(),
        )


def _execute_virtual_group(
    group_id: str,
    action: str,
    value: str,
    user_role: str,
    *,
    matched_alias: str = "",
    resolution_source: str = "tool_input",
) -> str:
    """Fan out one logical command to each member device, aggregate results."""
    successes: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for member_id in VIRTUAL_GROUPS[group_id]:
        try:
            device = _device_manager.get_device(member_id)
        except KeyError:
            failures.append(
                {
                    "entity": _unknown_entity(member_id),
                    "message": f"{member_id}: not found",
                    "error_code": "device_not_found",
                }
            )
            continue
        entity = _entity_payload(device)
        if not _permission_manager.check_permission(user_role, device, action):
            failures.append(
                {
                    "entity": entity,
                    "message": f"{device.name}: permission denied",
                    "error_code": "permission_denied",
                }
            )
            continue
        try:
            _device_manager.execute_command(member_id, action, value if value else None)
            successes.append(
                {
                    "entity": entity,
                    "message": f"{device.name}: OK",
                    "verification": _verification_payload(device, action, value),
                }
            )
        except Exception as exc:
            failures.append(
                {
                    "entity": entity,
                    "message": f"{device.name}: {exc}",
                    "error_code": "execution_failed",
                }
            )
    failure_messages = [str(item["message"]) for item in failures]
    group_entity = {
        "entity_id": group_id,
        "display_name": matched_alias or group_id,
        "entity_type": "virtual_group",
        "matched_alias": matched_alias or group_id,
        "alias_source": resolution_source,
        "resolution_source": resolution_source,
        "members": list(VIRTUAL_GROUPS[group_id]),
    }
    all_postconditions_confirmed = bool(successes) and all(
        item.get("verification", {}).get("postcondition_confirmed") is True
        for item in successes
    )
    verification_source = (
        "postcondition" if all_postconditions_confirmed and not failures else "controller_ack"
    )
    data = {
        "entity": group_entity,
        "group_id": group_id,
        "action": action,
        "value": value or None,
        "successes": successes,
        "failures": failures,
        "verification": {
            "controller_ack": bool(successes),
            "postcondition_confirmed": all_postconditions_confirmed,
            "verification_source": verification_source,
        },
    }
    if not failures:
        return make_tool_result(
            SUCCESS,
            f"{group_id}: {len(successes)}/{len(successes)} OK.",
            data=data,
            outcome_type=CHANGED,
            verified=True,
            verification_source=verification_source,
            claim_policy=_smart_home_group_claim_policy(
                verification_source,
                partial=False,
            ),
        )
    if not successes:
        return make_tool_result(
            FAILURE,
            f"{group_id}: all failed — {'; '.join(failure_messages)}",
            data=data,
            error_code="group_all_failed",
            outcome_type=OUTCOME_FAILED,
            verified=False,
            verification_source="none",
            claim_policy=_failed_claim_policy(),
        )
    return make_tool_result(
        PARTIAL_SUCCESS,
        (
            f"{group_id}: {len(successes)}/{len(successes) + len(failures)} OK "
            f"(failed: {'; '.join(failure_messages)})"
        ),
        data=data,
        error_code="group_partial_failure",
        outcome={
            "type": CHANGED,
            "verified": False,
            "verification_source": "partial_result",
        },
        claim_policy=_smart_home_group_claim_policy("partial_result", partial=True),
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
                data={
                    "entity": _entity_payload(device),
                    "device_id": device_id,
                    "status": status,
                    "freshness": _freshness_payload(),
                },
                outcome_type=OBSERVED,
                verified=True,
                verification_source="read_result",
            )
        except KeyError:
            return make_tool_result(
                FAILURE,
                f"Device not found: {device_id}",
                data={"entity": _unknown_entity(device_id), "device_id": device_id},
                error_code="device_not_found",
                outcome_type=OUTCOME_FAILED,
                verified=False,
                verification_source="none",
                claim_policy=_failed_claim_policy(),
            )
    status = _device_manager.get_all_status()
    inventory = [
        _inventory_entity(device_id, current_state)
        for device_id, current_state in status.items()
    ]
    return make_tool_result(
        SUCCESS,
        json.dumps(status, ensure_ascii=False, indent=2),
        data={
            "entities": status,
            "entity_inventory": inventory,
            "freshness": _freshness_payload(),
        },
        outcome_type=OBSERVED,
        verified=True,
        verification_source="read_result",
    )


def _entity_payload(
    device: Any,
    *,
    matched_alias: str = "",
    resolution_source: str = "tool_input",
) -> dict[str, Any]:
    device_id = str(getattr(device, "device_id", "") or "")
    display_name = str(getattr(device, "name", "") or device_id)
    entity_type = str(getattr(device, "device_type", "") or "device")
    alias = matched_alias or display_name or device_id
    return {
        "entity_id": device_id,
        "display_name": display_name,
        "entity_type": entity_type,
        "matched_alias": alias,
        "alias_source": resolution_source,
        "resolution_source": resolution_source,
        "capabilities": _capabilities_for_entity(entity_type),
    }


def _unknown_entity(
    entity_id: str,
    *,
    matched_alias: str = "",
    resolution_source: str = "tool_input",
) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "display_name": matched_alias or entity_id,
        "entity_type": "unknown",
        "matched_alias": matched_alias or entity_id,
        "alias_source": resolution_source,
        "resolution_source": resolution_source,
        "capabilities": [],
    }


def _inventory_entity(device_id: str, current_state: Any) -> dict[str, Any]:
    if isinstance(current_state, dict):
        display_name = str(current_state.get("name") or device_id)
        entity_type = str(current_state.get("device_type") or "device")
        last_updated = current_state.get("last_updated")
    else:
        display_name = device_id
        entity_type = "device"
        last_updated = None
    return {
        "entity_id": device_id,
        "display_name": display_name,
        "aliases": [display_name] if display_name != device_id else [],
        "alias_source": "device_inventory",
        "entity_type": entity_type,
        "capabilities": _capabilities_for_entity(entity_type),
        "current_state": current_state,
        "last_updated": last_updated,
    }


def _verification_payload(device: Any, action: str, value: str) -> dict[str, Any]:
    post_state = _safe_status(device)
    postcondition_confirmed = _postcondition_confirmed(post_state, action, value)
    verification_source = "postcondition" if postcondition_confirmed else "controller_ack"
    return {
        "controller_ack": True,
        "postcondition_confirmed": postcondition_confirmed,
        "verification_source": verification_source,
        "post_state": post_state,
    }


def _safe_status(device: Any) -> dict[str, Any] | None:
    try:
        status = device.get_status()
    except Exception as exc:  # pragma: no cover - live backend dependent
        LOGGER.debug("Could not read post-action status for %s: %s", device, exc)
        return None
    return status if isinstance(status, dict) else None


def _postcondition_confirmed(
    post_state: dict[str, Any] | None,
    action: str,
    value: str,
) -> bool:
    if not post_state:
        return False
    normalized_action = str(action).strip().lower()
    if normalized_action == "turn_on":
        return _state_power(post_state) is True
    if normalized_action == "turn_off":
        return _state_power(post_state) is False
    if normalized_action == "set_brightness":
        try:
            target = int(value)
            current = post_state.get("brightness")
            return current is not None and int(current) == target
        except (TypeError, ValueError):
            return False
    return False


def _state_power(post_state: dict[str, Any]) -> bool | None:
    for key in ("is_on", "on", "all_on", "any_on"):
        if key in post_state:
            return bool(post_state[key])
    return None


def _capabilities_for_entity(entity_type: str) -> list[str]:
    normalized = str(entity_type).strip().lower()
    if "light" in normalized:
        return [
            "turn_on",
            "turn_off",
            "set_brightness",
            "set_color_temp",
            "set_color",
        ]
    return ["turn_on", "turn_off"]


def _freshness_payload() -> dict[str, Any]:
    return {
        "observed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "max_age_seconds": 5,
    }


def _smart_home_claim_policy(verification_source: str) -> dict[str, Any]:
    if verification_source == "postcondition":
        return {
            "allowed_claims": [
                "smart_home_command_accepted",
                "device_state_changed",
                "actual_device_state_confirmed",
            ],
            "forbidden_claims": [],
        }
    return {
        "allowed_claims": ["smart_home_command_accepted"],
        "forbidden_claims": ["actual_device_state_confirmed"],
    }


def _smart_home_group_claim_policy(
    verification_source: str,
    *,
    partial: bool,
) -> dict[str, Any]:
    forbidden = ["all_devices_changed"] if partial else []
    if verification_source == "postcondition" and not partial:
        return {
            "allowed_claims": [
                "smart_home_group_command_accepted",
                "device_state_changed",
                "actual_device_state_confirmed",
            ],
            "forbidden_claims": forbidden,
        }
    return {
        "allowed_claims": ["smart_home_group_command_accepted"],
        "forbidden_claims": forbidden + ["actual_device_state_confirmed"],
    }


def _failed_claim_policy() -> dict[str, Any]:
    return {
        "allowed_claims": ["tool_failed_contract_validation"],
        "forbidden_claims": [
            "action_completed",
            "state_changed",
            "device_state_changed",
            "actual_device_state_confirmed",
            "all_devices_changed",
        ],
    }
