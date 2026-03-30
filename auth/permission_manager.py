"""Role-based permission checks for smart-home device actions."""

from __future__ import annotations

import logging

from devices.base_device import SmartDevice

LOGGER = logging.getLogger(__name__)

_ROLE_HIERARCHY = {
    "guest": 0,
    "member": 1,
    "resident": 1,
    "family": 2,
    "admin": 2,
    "owner": 3,
}


class PermissionManager:
    """Evaluate whether a user role is allowed to control a device."""

    def __init__(self) -> None:
        """Initialize the permission manager."""

        self.logger = LOGGER

    def check_permission(self, user_role: str, device: SmartDevice, action: str) -> bool:
        """Check whether a user role can execute an action on a device.

        Args:
            user_role: Role associated with the authenticated user.
            device: Target smart device.
            action: Requested action name.

        Returns:
            `True` if the user is allowed to control the device, else `False`.
        """

        if not device.is_available:
            self.logger.warning(
                "Denied action=%s on unavailable device=%s.",
                action,
                device.device_id,
            )
            return False

        normalized_user_role = self._normalize_role(user_role)
        normalized_required_role = self._normalize_role(device.required_role)
        allowed = _ROLE_HIERARCHY.get(normalized_user_role, -1) >= _ROLE_HIERARCHY.get(
            normalized_required_role,
            -1,
        )
        self.logger.info(
            "Permission check user_role=%s required_role=%s device=%s action=%s allowed=%s",
            normalized_user_role,
            normalized_required_role,
            device.device_id,
            action,
            allowed,
        )
        return allowed

    def _normalize_role(self, role: str) -> str:
        """Normalize role aliases into the internal hierarchy vocabulary."""

        normalized_role = role.strip().lower()
        if normalized_role == "family(admin)":
            return "admin"
        return normalized_role
