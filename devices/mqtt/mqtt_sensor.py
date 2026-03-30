"""Read-only MQTT sensor device."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from devices.base_device import SmartDevice

LOGGER = logging.getLogger(__name__)


class MqttSensor(SmartDevice):
    """Read-only sensor that receives data via MQTT subscriptions.

    Supported sensor types: temperature, humidity, motion, door.
    """

    def __init__(
        self,
        mqtt_client: Any,
        device_id: str,
        name: str,
        sensor_type: str,
        state_topic: str,
        required_role: str = "guest",
    ) -> None:
        super().__init__(
            device_id=device_id,
            name=name,
            device_type=f"sensor_{sensor_type}",
            required_role=required_role,
            is_available=True,
        )
        self._mqtt_client = mqtt_client
        self._sensor_type = sensor_type
        self._state_topic = state_topic
        self._last_reading: dict[str, Any] = {}
        self._lock = threading.Lock()

        self._mqtt_client.subscribe(state_topic, self._on_reading)

    # ------------------------------------------------------------------
    # SmartDevice interface
    # ------------------------------------------------------------------

    def execute(self, action: str, value: Any | None = None) -> str:
        """Sensors are read-only; commands are not supported."""
        return f"{self.name} is a read-only sensor."

    def get_status(self) -> dict[str, Any]:
        """Return the last sensor reading with a timestamp."""
        with self._lock:
            return {
                "device_id": self.device_id,
                "name": self.name,
                "type": self._sensor_type,
                "available": self.is_available,
                "reading": dict(self._last_reading),
            }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _on_reading(self, topic: str, payload: dict) -> None:
        with self._lock:
            self._last_reading = payload
            self._last_reading["last_updated"] = time.time()
        LOGGER.debug("Sensor reading for %s: %s", self.device_id, payload)
