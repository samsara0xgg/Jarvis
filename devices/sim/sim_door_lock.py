"""In-memory simulated smart door lock with lock and unlock operations."""

from __future__ import annotations

import logging
from typing import Any

from devices.base_device import SmartDevice

LOGGER = logging.getLogger(__name__)


class SimDoorLock(SmartDevice):
    """A simulated door lock that requires elevated permissions."""

    def __init__(
        self,
        device_id: str,
        name: str,
        required_role: str = "admin",
        is_available: bool = True,
        initial_state: dict[str, Any] | None = None,
    ) -> None:
        """Initialize the simulated door lock."""

        super().__init__(
            device_id=device_id,
            name=name,
            device_type="door_lock",
            required_role=required_role,
            is_available=is_available,
        )
        state = initial_state or {}
        self._is_locked = bool(state.get("is_locked", True))
        self.logger = LOGGER

    def execute(self, action: str, value: Any | None = None) -> str:
        """Lock or unlock the simulated door."""

        del value
        self._ensure_available()
        normalized_action = action.strip().lower()

        if normalized_action == "lock":
            self._is_locked = True
            return f"{self.name} 已锁定。"
        if normalized_action == "unlock":
            self._is_locked = False
            return f"{self.name} 已解锁。"

        raise ValueError(f"Unsupported door lock action: {action}")

    def get_status(self) -> dict[str, Any]:
        """Return the current lock state."""

        return {
            "device_id": self.device_id,
            "name": self.name,
            "device_type": self.device_type,
            "required_role": self.required_role,
            "is_available": self.is_available,
            "is_locked": self._is_locked,
        }
