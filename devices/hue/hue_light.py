"""Live Philips Hue light device backed by the local bridge REST API."""

from __future__ import annotations

import logging
from typing import Any

from core.command_parser import COLOR_XY_MAP
from devices.base_device import SmartDevice
from devices.hue.hue_bridge import HueBridge

LOGGER = logging.getLogger(__name__)

_COLOR_TEMP_PRESETS = {
    "warm": 2700,
    "neutral": 4000,
    "cool": 6500,
}


class HueLight(SmartDevice):
    """Control a real Hue light through a connected Hue Bridge."""

    def __init__(
        self,
        bridge: HueBridge,
        light_id: str,
        device_id: str,
        name: str,
        required_role: str = "guest",
        is_available: bool = True,
    ) -> None:
        """Initialize the Hue light wrapper."""

        super().__init__(
            device_id=device_id,
            name=name,
            device_type="light",
            required_role=required_role,
            is_available=is_available,
        )
        self.bridge = bridge
        self.light_id = str(light_id)
        self.logger = LOGGER

    def execute(self, action: str, value: Any | None = None) -> str:
        """Execute a Hue light action using the bridge REST API."""

        normalized_action = action.strip().lower()
        if normalized_action == "turn_on":
            self._put_state({"on": True})
            return f"{self.name} 已打开。"
        if normalized_action == "turn_off":
            self._put_state({"on": False})
            return f"{self.name} 已关闭。"
        if normalized_action == "set_brightness":
            brightness_percent = int(value)
            brightness = self._percent_to_hue_brightness(brightness_percent)
            self._put_state({"on": True, "bri": brightness})
            return f"{self.name} 亮度已设置为 {brightness_percent}%。"
        if normalized_action == "set_color_temp":
            kelvin = self._normalize_color_temp_value(value)
            if not self._supports_color_temperature():
                return f"{self.name} 不支持色温控制。"
            self._put_state({"on": True, "ct": self._kelvin_to_mirek(kelvin)})
            return f"{self.name} 色温已设置为 {kelvin}K。"
        if normalized_action == "set_color":
            xy = self._resolve_color_xy(value)
            if xy is None:
                raise ValueError(f"Unsupported color value: {value}")
            if not self._supports_color():
                return f"{self.name} 不支持颜色控制。"
            self._put_state({"on": True, "xy": xy})
            return f"{self.name} 颜色已设置为 {str(value).strip().lower()}。"
        if normalized_action == "set_effect":
            effect = str(value).strip().lower()
            if effect not in {"colorloop", "none"}:
                raise ValueError("Supported Hue effects are 'colorloop' and 'none'.")
            self._put_state({"effect": effect})
            return f"{self.name} 特效已设置为 {effect}。"

        raise ValueError(f"Unsupported Hue light action: {action}")

    def get_status(self) -> dict[str, Any]:
        """Return a normalized status payload for the Hue light."""

        light = self._get_light_payload()
        state = light.get("state", {})
        self.is_available = bool(state.get("reachable", False))
        brightness = int(round(self._hue_brightness_to_percent(state.get("bri"))))
        ct = state.get("ct")
        xy = state.get("xy")
        return {
            "device_id": self.device_id,
            "name": self.name,
            "device_type": self.device_type,
            "required_role": self.required_role,
            "is_available": self.is_available,
            "status_text": "正常" if self.is_available else "不可达",
            "is_on": bool(state.get("on", False)),
            "brightness": brightness,
            "color_temp_kelvin": self._mirek_to_kelvin(ct) if ct else None,
            "effect": state.get("effect"),
            "reachable": bool(state.get("reachable", False)),
            "color_xy": xy,
            "raw_type": light.get("type"),
        }

    def _put_state(self, data: dict[str, Any]) -> None:
        """Send a state update to the light."""

        self.bridge.request(
            "PUT",
            f"/api/{self.bridge.username}/lights/{self.light_id}/state",
            data,
        )

    def _get_light_payload(self) -> dict[str, Any]:
        """Fetch the raw Hue light payload."""

        payload = self.bridge.request(
            "GET",
            f"/api/{self.bridge.username}/lights/{self.light_id}",
        )
        if not isinstance(payload, dict):
            raise RuntimeError(f"Unexpected Hue light payload for {self.light_id}")
        return payload

    def _supports_color_temperature(self) -> bool:
        """Return whether the light reports color temperature support."""

        state = self._get_light_payload().get("state", {})
        return "ct" in state

    def _supports_color(self) -> bool:
        """Return whether the light reports XY color support."""

        state = self._get_light_payload().get("state", {})
        return "xy" in state

    def _normalize_color_temp_value(self, value: Any) -> int:
        """Normalize symbolic or numeric color temperature input to Kelvin."""

        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in _COLOR_TEMP_PRESETS:
                return _COLOR_TEMP_PRESETS[normalized]
            if normalized.isdigit():
                return int(normalized)
            raise ValueError(f"Unsupported color temperature value: {value}")
        return int(value)

    def _resolve_color_xy(self, value: Any) -> list[float] | None:
        """Resolve a color name to a Hue-compatible CIE xy pair."""

        normalized = str(value).strip().lower()
        xy = COLOR_XY_MAP.get(normalized)
        if xy is None:
            return None
        return [float(xy[0]), float(xy[1])]

    def _percent_to_hue_brightness(self, brightness: int) -> int:
        """Convert user brightness percent into Hue's 1-254 brightness scale."""

        clamped = max(0, min(100, brightness))
        if clamped == 0:
            return 1
        return max(1, min(254, int(round((clamped / 100.0) * 254.0))))

    def _hue_brightness_to_percent(self, brightness: Any) -> float:
        """Convert a Hue 1-254 brightness value into a user-facing percent."""

        if brightness is None:
            return 0.0
        return max(0.0, min(100.0, (float(brightness) / 254.0) * 100.0))

    def _kelvin_to_mirek(self, kelvin: int) -> int:
        """Convert Kelvin color temperature to Hue mirek units."""

        clamped_kelvin = max(2000, min(6500, kelvin))
        mirek = int(round(1000000 / clamped_kelvin))
        return max(153, min(500, mirek))

    def _mirek_to_kelvin(self, mirek: Any) -> int | None:
        """Convert Hue mirek values back to Kelvin."""

        if mirek in (None, 0):
            return None
        return int(round(1000000 / float(mirek)))
