"""Device registry and execution layer for simulated and live device backends."""

from __future__ import annotations

import logging
from typing import Any

from devices.base_device import SmartDevice
from devices.hue.hue_bridge import HueBridge, HueBridgeConnectionError
from devices.hue.hue_group import HueGroup
from devices.hue.hue_light import HueLight
from devices.hue.hue_scene import HueSceneDevice
from devices.sim.sim_door_lock import SimDoorLock
from devices.sim.sim_light import SimLight
from devices.sim.sim_thermostat import SimThermostat

LOGGER = logging.getLogger(__name__)


class DeviceManager:
    """Manage smart devices across simulated and live backends.

    Args:
        config: Parsed application configuration dictionary.
    """

    def __init__(self, config: dict, event_bus: Any | None = None) -> None:
        """Initialize the device manager from config."""

        devices_config = config.get("devices", {})
        self.mode = str(devices_config.get("mode", "sim")).lower()
        self._devices: dict[str, SmartDevice] = {}
        self._event_bus = event_bus
        self.logger = LOGGER

        if self.mode == "sim":
            self._init_sim_devices(devices_config)
        elif self.mode == "live":
            self._init_hue_devices(config)
        else:
            raise ValueError(f"Unsupported device mode: {self.mode}")

        # MQTT devices are composable — loaded alongside sim or live
        self._mqtt_client = None
        self._init_mqtt_devices(config)

    def execute_command(
        self,
        device_id: str,
        action: str,
        value: Any | None = None,
    ) -> str:
        """Execute a command on a specific device."""

        device = self.get_device(device_id)
        self.logger.info(
            "Executing command on device=%s action=%s value=%r",
            device_id,
            action,
            value,
        )
        return device.execute(action, value)

    def get_all_status(self) -> dict[str, dict[str, Any]]:
        """Return status for all managed devices keyed by device ID."""

        return {
            device_id: device.get_status()
            for device_id, device in self._devices.items()
        }

    def get_device(self, device_id: str) -> SmartDevice:
        """Return a device by ID or raise if it does not exist."""

        normalized_device_id = device_id.strip()
        try:
            return self._devices[normalized_device_id]
        except KeyError as exc:
            raise KeyError(f"Unknown device: {normalized_device_id}") from exc

    def _init_sim_devices(self, devices_config: dict[str, Any]) -> None:
        """Load simulated devices from config."""

        device_factory = {
            "light": SimLight,
            "door_lock": SimDoorLock,
            "thermostat": SimThermostat,
        }
        for item in devices_config.get("sim_devices", []):
            device_type = str(item.get("device_type", "")).strip().lower()
            device_class = device_factory.get(device_type)
            if device_class is None:
                raise ValueError(f"Unsupported simulated device type: {device_type}")

            device = device_class(
                device_id=str(item["device_id"]),
                name=str(item["name"]),
                required_role=str(item.get("required_role", "guest")),
                is_available=bool(item.get("is_available", True)),
                initial_state=item.get("initial_state", {}),
            )
            self._devices[device.device_id] = device
            self.logger.info(
                "Initialized simulated device %s of type %s.",
                device.device_id,
                device.device_type,
            )

    def _init_hue_devices(self, config: dict[str, Any]) -> None:
        """Initialize live Hue devices."""

        hue_config = config.get("hue", {})
        bridge = HueBridge(config)
        self._hue_bridge = bridge

        try:
            bridge.connect()
        except HueBridgeConnectionError as exc:
            self.logger.warning("Hue Bridge unavailable: %s", exc)
            raise

        light_aliases = hue_config.get("light_aliases", {})
        group_aliases = hue_config.get("group_aliases", {})
        scene_aliases = hue_config.get("scene_aliases", {})
        default_scene_group = str(hue_config.get("default_scene_group", "")).strip() or None

        for light_id, light in bridge.get_all_lights().items():
            name = str(light.get("name", f"Hue Light {light_id}"))
            device_id = self._resolve_hue_device_id(name, light_aliases, f"hue_light_{light_id}")
            required_role = self._resolve_required_role(hue_config, device_id, default_role="guest")
            state = light.get("state", {})
            device = HueLight(
                bridge=bridge,
                light_id=str(light_id),
                device_id=device_id,
                name=name,
                required_role=required_role,
                is_available=bool(state.get("reachable", True)),
            )
            self._devices[device_id] = device
            self.logger.info("Initialized Hue light %s from bridge resource %s.", device_id, light_id)

        for group_id, group in bridge.get_all_groups().items():
            if str(group_id) == "0":
                continue
            name = str(group.get("name", f"Hue Group {group_id}"))
            device_id = self._resolve_hue_device_id(name, group_aliases, f"hue_group_{group_id}")
            required_role = self._resolve_required_role(hue_config, device_id, default_role="guest")
            device = HueGroup(
                bridge=bridge,
                group_id=str(group_id),
                device_id=device_id,
                name=name,
                required_role=required_role,
                is_available=True,
            )
            self._devices[device_id] = device
            self.logger.info("Initialized Hue group %s from bridge resource %s.", device_id, group_id)

        self._devices["scene"] = HueSceneDevice(
            bridge=bridge,
            scene_aliases=scene_aliases,
            default_group_id=default_scene_group,
            required_role=self._resolve_required_role(hue_config, "scene", default_role="guest"),
        )

    def _resolve_hue_device_id(
        self,
        resource_name: str,
        alias_mapping: dict[str, list[str] | str],
        fallback: str,
    ) -> str:
        """Resolve a live Hue resource name to the canonical device ID from config."""

        normalized_name = self._normalize_text(resource_name)
        for canonical_device_id, aliases in alias_mapping.items():
            candidates = [canonical_device_id]
            if isinstance(aliases, str):
                candidates.append(aliases)
            else:
                candidates.extend(aliases)
            for candidate in candidates:
                if normalized_name == self._normalize_text(str(candidate)):
                    return canonical_device_id
        return fallback

    def _resolve_required_role(
        self,
        hue_config: dict[str, Any],
        device_id: str,
        default_role: str,
    ) -> str:
        """Resolve an optional required role override from hue config."""

        required_roles = hue_config.get("required_roles", {})
        return str(required_roles.get(device_id, default_role))

    def _normalize_text(self, text: str) -> str:
        """Normalize text for alias matching."""

        return "".join(text.strip().lower().split())

    def _init_mqtt_devices(self, config: dict[str, Any]) -> None:
        """Load MQTT devices if enabled. Composable with sim or live mode."""

        mqtt_config = config.get("mqtt", {})
        if not mqtt_config.get("enabled", False):
            return

        try:
            from devices.mqtt.mqtt_client import MqttClient
            from devices.mqtt.mqtt_device import MqttDevice
            from devices.mqtt.mqtt_sensor import MqttSensor
        except ImportError:
            self.logger.warning("paho-mqtt not installed. MQTT devices disabled.")
            return

        client = MqttClient(config, event_bus=self._event_bus)
        if not client.connect():
            self.logger.warning("MQTT broker unreachable. MQTT devices skipped.")
            return

        self._mqtt_client = client

        for item in mqtt_config.get("devices", []):
            device_id = str(item["device_id"])
            name = str(item["name"])
            device_type = str(item.get("device_type", "sensor"))
            topic = str(item.get("topic", f"jarvis/devices/{device_id}"))
            required_role = str(item.get("required_role", "guest"))

            if device_type == "sensor":
                device = MqttSensor(
                    mqtt_client=client,
                    device_id=device_id,
                    name=name,
                    sensor_type=str(item.get("sensor_type", "generic")),
                    state_topic=f"{topic}/state",
                    required_role=required_role,
                )
            else:
                device = MqttDevice(
                    mqtt_client=client,
                    device_id=device_id,
                    name=name,
                    device_type=device_type,
                    command_topic=f"{topic}/set",
                    state_topic=f"{topic}/state",
                    required_role=required_role,
                )

            self._devices[device_id] = device
            self.logger.info(
                "Initialized MQTT device %s (%s) on topic %s.",
                device_id, device_type, topic,
            )
