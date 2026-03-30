"""In-memory simulated light device supporting power, brightness, color, and color temperature."""

from __future__ import annotations

import logging
from typing import Any

from core.command_parser import COLOR_TEMP_MAP, COLOR_XY_MAP
from devices.base_device import SmartDevice

LOGGER = logging.getLogger(__name__)

_SUPPORTED_COLOR_TEMPERATURES = {"warm", "neutral", "cool"}
_CANONICAL_COLORS = {
    "red",
    "orange",
    "yellow",
    "green",
    "cyan",
    "blue",
    "purple",
    "pink",
    "white",
}


class SimLight(SmartDevice):
    """A simulated light whose state is kept entirely in memory."""

    def __init__(
        self,
        device_id: str,
        name: str,
        required_role: str = "guest",
        is_available: bool = True,
        initial_state: dict[str, Any] | None = None,
    ) -> None:
        """Initialize the simulated light from config-provided state."""

        super().__init__(
            device_id=device_id,
            name=name,
            device_type="light",
            required_role=required_role,
            is_available=is_available,
        )
        state = initial_state or {}
        self._is_on = bool(state.get("is_on", False))
        self._brightness = int(state.get("brightness", 100))
        self._color_temp = str(state.get("color_temp", "neutral"))
        self._color = str(state.get("color", "white"))
        self.logger = LOGGER

    def execute(self, action: str, value: Any | None = None) -> str:
        """Execute a supported light action and update in-memory state."""

        self._ensure_available()
        normalized_action = action.strip().lower()

        if normalized_action == "turn_on":
            self._is_on = True
            return f"{self.name} 已打开。"
        if normalized_action == "turn_off":
            self._is_on = False
            return f"{self.name} 已关闭。"
        if normalized_action == "set_brightness":
            brightness = int(value)
            if not 0 <= brightness <= 100:
                raise ValueError("Brightness must be between 0 and 100.")
            self._brightness = brightness
            self._is_on = brightness > 0
            return f"{self.name} 亮度已设置为 {brightness}% 。"
        if normalized_action == "set_color_temp":
            color_temp = str(value).strip().lower()
            if color_temp not in _SUPPORTED_COLOR_TEMPERATURES:
                raise ValueError(
                    f"Unsupported color temperature: {value}. Supported values: {sorted(_SUPPORTED_COLOR_TEMPERATURES)}"
                )
            self._color_temp = color_temp
            self._is_on = True
            return f"{self.name} 色温已设置为 {color_temp}。"
        if normalized_action == "set_color":
            color = str(value).strip().lower()
            if color not in _CANONICAL_COLORS:
                raise ValueError(
                    f"Unsupported color: {value}. Supported values: {sorted(_CANONICAL_COLORS)}"
                )
            self._color = color
            self._is_on = True
            return f"{self.name} 颜色已设置为 {color}。"

        raise ValueError(f"Unsupported light action: {action}")

    def get_status(self) -> dict[str, Any]:
        """Return the current simulated light state."""

        color_xy = COLOR_XY_MAP.get(self._color)
        return {
            "device_id": self.device_id,
            "name": self.name,
            "device_type": self.device_type,
            "required_role": self.required_role,
            "is_available": self.is_available,
            "is_on": self._is_on,
            "brightness": self._brightness,
            "color_temp": self._color_temp,
            "color": self._color,
            "color_temp_map": COLOR_TEMP_MAP,
            "color_xy": color_xy,
        }
