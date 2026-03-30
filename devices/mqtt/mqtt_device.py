"""MQTT-controllable smart device."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from devices.base_device import SmartDevice

LOGGER = logging.getLogger(__name__)


class MqttDevice(SmartDevice):
    """A controllable device that sends commands and receives state over MQTT."""

    def __init__(
        self,
        mqtt_client: Any,
        device_id: str,
        name: str,
        device_type: str,
        command_topic: str,
        state_topic: str,
        required_role: str = "guest",
        is_available: bool = True,
    ) -> None:
        super().__init__(
            device_id=device_id,
            name=name,
            device_type=device_type,
            required_role=required_role,
            is_available=is_available,
        )
        self._mqtt_client = mqtt_client
        self._command_topic = command_topic
        self._state_topic = state_topic
        self._last_state: dict[str, Any] = {}
        self._lock = threading.Lock()

        # Subscribe to state updates from the device
        self._mqtt_client.subscribe(state_topic, self._on_state_update)

    # ------------------------------------------------------------------
    # SmartDevice interface
    # ------------------------------------------------------------------

    def execute(self, action: str, value: Any | None = None) -> str:
        """Publish a command to the device's command topic."""
        self._ensure_available()
        payload = {"action": action, "value": value}
        success = self._mqtt_client.publish(self._command_topic, payload)
        if success:
            return f"{self.name}: {action} command sent."
        return f"{self.name}: failed to send {action} command."

    def get_status(self) -> dict[str, Any]:
        """Return the last known state received via MQTT."""
        with self._lock:
            return {
                "device_id": self.device_id,
                "name": self.name,
                "type": self.device_type,
                "available": self.is_available,
                "state": dict(self._last_state),
            }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _on_state_update(self, topic: str, payload: dict) -> None:
        with self._lock:
            self._last_state = payload
            self._last_state["last_updated"] = time.time()
        LOGGER.debug("State update for %s: %s", self.device_id, payload)
