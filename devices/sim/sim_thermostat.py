"""In-memory simulated thermostat supporting power and temperature control."""

from __future__ import annotations

import logging
from typing import Any

from devices.base_device import SmartDevice

LOGGER = logging.getLogger(__name__)


class SimThermostat(SmartDevice):
    """A simulated thermostat with a fixed supported temperature range."""

    def __init__(
        self,
        device_id: str,
        name: str,
        required_role: str = "member",
        is_available: bool = True,
        initial_state: dict[str, Any] | None = None,
    ) -> None:
        """Initialize the simulated thermostat."""

        super().__init__(
            device_id=device_id,
            name=name,
            device_type="thermostat",
            required_role=required_role,
            is_available=is_available,
        )
        state = initial_state or {}
        self._is_on = bool(state.get("is_on", False))
        self._temperature = int(state.get("temperature", 24))
        self.logger = LOGGER

    def execute(self, action: str, value: Any | None = None) -> str:
        """Execute thermostat actions in the supported temperature range."""

        self._ensure_available()
        normalized_action = action.strip().lower()

        if normalized_action == "turn_on":
            self._is_on = True
            return f"{self.name} 已开启。"
        if normalized_action == "turn_off":
            self._is_on = False
            return f"{self.name} 已关闭。"
        if normalized_action == "set_temperature":
            temperature = int(value)
            if not 16 <= temperature <= 30:
                raise ValueError("Temperature must be between 16 and 30.")
            self._temperature = temperature
            self._is_on = True
            return f"{self.name} 温度已设置为 {temperature} 度。"

        raise ValueError(f"Unsupported thermostat action: {action}")

    def get_status(self) -> dict[str, Any]:
        """Return the current thermostat state."""

        return {
            "device_id": self.device_id,
            "name": self.name,
            "device_type": self.device_type,
            "required_role": self.required_role,
            "is_available": self.is_available,
            "is_on": self._is_on,
            "temperature": self._temperature,
        }
