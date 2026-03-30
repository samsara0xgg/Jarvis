"""Tests for MQTT device infrastructure with mocked paho-mqtt."""

from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# MqttClient tests
# ---------------------------------------------------------------------------

class TestMqttClient:

    def test_unavailable_when_paho_missing(self):
        from devices.mqtt.mqtt_client import MqttClient
        client = MqttClient.__new__(MqttClient)
        client._available = False
        client._connected = False
        client._client = None
        client._subscriptions = {}
        client._lock = threading.Lock()
        client._event_bus = None
        client._broker_host = "localhost"
        client._broker_port = 1883
        client._client_id = "test"

        assert client.available is False
        assert client.connect() is False
        assert client.publish("t", {}) is False
        assert client.is_connected() is False
        client.subscribe("t", lambda t, p: None)
        client.disconnect()

    def _make_client(self, event_bus=None):
        from devices.mqtt.mqtt_client import MqttClient
        client = MqttClient.__new__(MqttClient)
        client._broker_host = "localhost"
        client._broker_port = 1883
        client._client_id = "jarvis"
        client._event_bus = event_bus
        client._available = True
        client._connected = False
        client._lock = threading.Lock()
        client._subscriptions = {}
        client._client = MagicMock()
        return client

    def test_connect_success(self):
        client = self._make_client()
        result = client.connect()
        assert result is True
        client._client.connect.assert_called_once_with("localhost", 1883, keepalive=60)
        client._client.loop_start.assert_called_once()

    def test_connect_failure(self):
        client = self._make_client()
        client._client.connect.side_effect = ConnectionRefusedError("refused")
        assert client.connect() is False
        assert client.is_connected() is False

    def test_disconnect(self):
        client = self._make_client()
        client._connected = True
        client.disconnect()
        client._client.loop_stop.assert_called_once()
        client._client.disconnect.assert_called_once()

    def test_publish(self):
        client = self._make_client()
        mock_result = MagicMock()
        mock_result.rc = 0
        client._client.publish.return_value = mock_result
        payload = {"action": "on", "value": 80}
        assert client.publish("jarvis/test", payload) is True
        client._client.publish.assert_called_once_with("jarvis/test", json.dumps(payload))

    def test_subscribe_and_dispatch(self):
        client = self._make_client()
        received = []
        client.subscribe("jarvis/test", lambda t, p: received.append((t, p)))

        fake_msg = MagicMock()
        fake_msg.topic = "jarvis/test"
        fake_msg.payload = json.dumps({"temp": 22.5}).encode()
        client._on_message(None, None, fake_msg)

        assert len(received) == 1
        assert received[0] == ("jarvis/test", {"temp": 22.5})

    def test_event_bus_emission(self):
        bus = MagicMock()
        client = self._make_client(event_bus=bus)

        fake_msg = MagicMock()
        fake_msg.topic = "jarvis/sensors/s1/state"
        fake_msg.payload = json.dumps({"humidity": 55}).encode()
        client._on_message(None, None, fake_msg)

        bus.emit.assert_called_once_with(
            "sensor.data",
            {"topic": "jarvis/sensors/s1/state", "payload": {"humidity": 55}},
        )

    def test_on_connect_resubscribes(self):
        client = self._make_client()
        client.subscribe("topic/a", lambda t, p: None)
        client.subscribe("topic/b", lambda t, p: None)

        mock_inner = MagicMock()
        client._on_connect(mock_inner, None, None, 0)

        subscribed_topics = {call.args[0] for call in mock_inner.subscribe.call_args_list}
        assert subscribed_topics == {"topic/a", "topic/b"}


# ---------------------------------------------------------------------------
# MqttDevice tests
# ---------------------------------------------------------------------------

class TestMqttDevice:

    def _make_device(self):
        from devices.mqtt.mqtt_device import MqttDevice
        mqtt_client = MagicMock()
        device = MqttDevice(
            mqtt_client=mqtt_client,
            device_id="desk_lamp",
            name="Desk Lamp",
            device_type="light",
            command_topic="jarvis/devices/desk_lamp/set",
            state_topic="jarvis/devices/desk_lamp/state",
        )
        return device, mqtt_client

    def test_execute_publishes_command(self):
        device, mqtt_client = self._make_device()
        mqtt_client.publish.return_value = True
        result = device.execute("on", 80)
        mqtt_client.publish.assert_called_once_with(
            "jarvis/devices/desk_lamp/set",
            {"action": "on", "value": 80},
        )
        assert "command sent" in result

    def test_execute_reports_failure(self):
        device, mqtt_client = self._make_device()
        mqtt_client.publish.return_value = False
        result = device.execute("off")
        assert "failed" in result

    def test_get_status_returns_last_state(self):
        device, mqtt_client = self._make_device()
        callback = mqtt_client.subscribe.call_args[0][1]
        callback("jarvis/devices/desk_lamp/state", {"on": True, "brightness": 80})
        status = device.get_status()
        assert status["device_id"] == "desk_lamp"
        assert status["state"]["on"] is True
        assert status["state"]["brightness"] == 80
        assert "last_updated" in status["state"]

    def test_get_status_empty_initially(self):
        device, _ = self._make_device()
        status = device.get_status()
        assert status["state"] == {}

    def test_execute_raises_when_unavailable(self):
        from devices.mqtt.mqtt_device import MqttDevice
        mqtt_client = MagicMock()
        device = MqttDevice(
            mqtt_client=mqtt_client,
            device_id="broken",
            name="Broken",
            device_type="light",
            command_topic="t/set",
            state_topic="t/state",
            is_available=False,
        )
        with pytest.raises(RuntimeError, match="unavailable"):
            device.execute("on")


# ---------------------------------------------------------------------------
# MqttSensor tests
# ---------------------------------------------------------------------------

class TestMqttSensor:

    def _make_sensor(self):
        from devices.mqtt.mqtt_sensor import MqttSensor
        mqtt_client = MagicMock()
        sensor = MqttSensor(
            mqtt_client=mqtt_client,
            device_id="living_room_temp",
            name="Living Room Temperature",
            sensor_type="temperature",
            state_topic="jarvis/sensors/living_room/state",
        )
        return sensor, mqtt_client

    def test_execute_returns_read_only_message(self):
        sensor, _ = self._make_sensor()
        result = sensor.execute("on")
        assert "read-only sensor" in result

    def test_get_status_returns_sensor_data(self):
        sensor, mqtt_client = self._make_sensor()
        callback = mqtt_client.subscribe.call_args[0][1]
        callback("jarvis/sensors/living_room/state", {"temperature": 23.1, "humidity": 48})
        status = sensor.get_status()
        assert status["device_id"] == "living_room_temp"
        assert status["type"] == "temperature"
        assert status["reading"]["temperature"] == 23.1
        assert "last_updated" in status["reading"]

    def test_get_status_empty_initially(self):
        sensor, _ = self._make_sensor()
        status = sensor.get_status()
        assert status["reading"] == {}

    def test_device_type_includes_sensor_prefix(self):
        sensor, _ = self._make_sensor()
        assert sensor.device_type == "sensor_temperature"


# ---------------------------------------------------------------------------
# DeviceManager MQTT integration tests
# ---------------------------------------------------------------------------

class TestDeviceManagerMqtt:

    def _sim_config(self, mqtt_enabled=False, mqtt_devices=None):
        return {
            "devices": {
                "mode": "sim",
                "sim_devices": [
                    {
                        "device_id": "light_1",
                        "name": "Test Light",
                        "device_type": "light",
                    }
                ],
            },
            "mqtt": {
                "enabled": mqtt_enabled,
                "broker_host": "localhost",
                "broker_port": 1883,
                "devices": mqtt_devices or [],
            },
        }

    @patch("devices.mqtt.mqtt_client.MqttClient")
    def test_mqtt_devices_loaded_when_enabled(self, MockMqttClient):
        from devices.device_manager import DeviceManager

        mock_client = MagicMock()
        mock_client.connect.return_value = True
        MockMqttClient.return_value = mock_client

        config = self._sim_config(
            mqtt_enabled=True,
            mqtt_devices=[
                {
                    "device_id": "mqtt_lamp",
                    "name": "MQTT Lamp",
                    "device_type": "light",
                    "topic": "jarvis/devices/lamp",
                },
                {
                    "device_id": "mqtt_temp",
                    "name": "MQTT Temp",
                    "device_type": "sensor",
                    "sensor_type": "temperature",
                    "topic": "jarvis/sensors/temp",
                },
            ],
        )

        dm = DeviceManager(config)
        assert "light_1" in dm._devices
        assert "mqtt_lamp" in dm._devices
        assert "mqtt_temp" in dm._devices

    def test_no_mqtt_when_disabled(self):
        from devices.device_manager import DeviceManager
        config = self._sim_config(mqtt_enabled=False)
        dm = DeviceManager(config)
        assert "light_1" in dm._devices
        assert dm._mqtt_client is None

    def test_mqtt_import_failure_does_not_break_sim(self):
        from devices.device_manager import DeviceManager
        config = self._sim_config(mqtt_enabled=True)

        with patch.dict("sys.modules", {"devices.mqtt.mqtt_client": None}):
            dm = DeviceManager(config)

        assert "light_1" in dm._devices
        assert dm._mqtt_client is None
