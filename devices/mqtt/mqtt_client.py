"""MQTT client wrapper with graceful degradation when paho-mqtt is absent."""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable

LOGGER = logging.getLogger(__name__)

try:
    import paho.mqtt.client as paho_mqtt
    from paho.mqtt.client import CallbackAPIVersion

    _PAHO_AVAILABLE = True
except ImportError:
    _PAHO_AVAILABLE = False


class MqttClient:
    """Singleton-style MQTT client wrapping paho-mqtt.

    Gracefully degrades if paho-mqtt is not installed: every public method
    returns a safe no-op value so the rest of the system keeps running.
    """

    def __init__(self, config: dict, event_bus: Any | None = None) -> None:
        mqtt_cfg = config.get("mqtt", {})
        self._broker_host: str = str(mqtt_cfg.get("broker_host", "localhost"))
        self._broker_port: int = int(mqtt_cfg.get("broker_port", 1883))
        self._client_id: str = str(mqtt_cfg.get("client_id", "jarvis"))
        self._username: str = str(mqtt_cfg.get("username", "")).strip()
        self._password: str = str(mqtt_cfg.get("password", "")).strip()
        self._event_bus = event_bus
        self._available = _PAHO_AVAILABLE
        self._connected = False
        self._lock = threading.Lock()
        self._subscriptions: dict[str, list[Callable]] = {}
        self._client: Any | None = None

        if not self._available:
            LOGGER.warning(
                "paho-mqtt is not installed. MQTT functionality is disabled."
            )
            return

        self._client = paho_mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=self._client_id,
        )
        if self._username:
            self._client.username_pw_set(self._username, self._password)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Connect to the MQTT broker. Returns True on success."""
        if not self._available or self._client is None:
            return False
        try:
            self._client.connect(self._broker_host, self._broker_port, keepalive=60)
            self._client.loop_start()
            self._connected = True
            LOGGER.info(
                "MQTT connected to %s:%s", self._broker_host, self._broker_port
            )
            return True
        except Exception:
            LOGGER.exception("Failed to connect to MQTT broker")
            self._connected = False
            return False

    def disconnect(self) -> None:
        """Disconnect from the broker and stop the network loop."""
        if not self._available or self._client is None:
            return
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            LOGGER.exception("Error during MQTT disconnect")
        finally:
            self._connected = False

    def subscribe(self, topic: str, callback: Callable) -> None:
        """Subscribe to *topic* with a ``callback(topic, payload_dict)``."""
        if not self._available or self._client is None:
            return
        with self._lock:
            self._subscriptions.setdefault(topic, []).append(callback)
        self._client.subscribe(topic)
        LOGGER.debug("Subscribed to MQTT topic: %s", topic)

    def publish(self, topic: str, payload: dict) -> bool:
        """Publish a JSON-serialised *payload* to *topic*."""
        if not self._available or self._client is None:
            return False
        try:
            message = json.dumps(payload)
            result = self._client.publish(topic, message)
            return result.rc == 0
        except Exception:
            LOGGER.exception("Failed to publish to %s", topic)
            return False

    def is_connected(self) -> bool:
        """Return whether the client is currently connected."""
        return self._connected

    @property
    def available(self) -> bool:
        """Whether paho-mqtt is installed and the client can operate."""
        return self._available

    # ------------------------------------------------------------------
    # Internal callbacks
    # ------------------------------------------------------------------

    def _on_connect(self, client: Any, userdata: Any, flags: Any, rc: Any, properties: Any = None) -> None:
        self._connected = True
        LOGGER.info("MQTT broker connection established (rc=%s)", rc)
        # Re-subscribe to all topics after reconnect
        with self._lock:
            for topic in self._subscriptions:
                client.subscribe(topic)

    def _on_disconnect(self, client: Any, userdata: Any, flags: Any, rc: Any, properties: Any = None) -> None:
        self._connected = False
        if rc != 0:
            LOGGER.warning("Unexpected MQTT disconnect (rc=%s). Auto-reconnect will retry.", rc)

    def _on_message(self, client: Any, userdata: Any, msg: Any) -> None:
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            LOGGER.warning("Non-JSON MQTT payload on %s", msg.topic)
            return

        # Dispatch to topic-specific callbacks
        with self._lock:
            callbacks = list(self._subscriptions.get(msg.topic, []))

        for cb in callbacks:
            try:
                cb(msg.topic, payload)
            except Exception:
                LOGGER.exception("MQTT callback error on topic %s", msg.topic)

        # Emit on the event bus so other subsystems can react
        if self._event_bus is not None:
            self._event_bus.emit("sensor.data", {"topic": msg.topic, "payload": payload})
