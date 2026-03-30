"""Tests for simulated devices, device manager orchestration, and permission checks."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from auth.permission_manager import PermissionManager
from core.command_parser import CommandParser
from devices.device_manager import DeviceManager
from devices.hue.hue_bridge import HueBridgeConnectionError
from devices.hue.hue_group import HueGroup
from devices.hue.hue_light import HueLight
from devices.hue.hue_scene import HueSceneDevice


def _load_config() -> dict:
    """Load the project configuration for device tests."""

    config_path = Path(__file__).resolve().parents[1] / "config.yaml"
    with config_path.open("r", encoding="utf-8") as config_file:
        return yaml.safe_load(config_file)


def test_sim_mode_end_to_end_with_command_parser_and_permissions() -> None:
    """Sim mode should support parse, lookup, permission check, and execution."""

    config = _load_config()
    parser = CommandParser(config)
    device_manager = DeviceManager(config)
    permission_manager = PermissionManager()

    parsed_command = parser.parse("把书房灯调到 50%")

    assert parsed_command == {
        "device": "study_light",
        "action": "set_brightness",
        "value": 50,
    }

    device = device_manager.get_device(parsed_command["device"])
    assert permission_manager.check_permission("guest", device, parsed_command["action"]) is True

    result = device_manager.execute_command(
        parsed_command["device"],
        parsed_command["action"],
        parsed_command.get("value"),
    )
    status = device_manager.get_device("study_light").get_status()

    assert "亮度已设置为 50%" in result
    assert status["brightness"] == 50
    assert status["is_on"] is True


def test_group_light_command_executes_in_sim_mode() -> None:
    """Configured simulated group-like light IDs should execute parser output."""

    config = _load_config()
    parser = CommandParser(config)
    device_manager = DeviceManager(config)

    parsed_command = parser.parse("关掉客厅所有灯")
    result = device_manager.execute_command(
        parsed_command["device"],
        parsed_command["action"],
        parsed_command.get("value"),
    )

    assert parsed_command["device"] == "living_room_group"
    assert "已关闭" in result
    assert device_manager.get_device("living_room_group").get_status()["is_on"] is False


def test_permission_manager_blocks_member_from_unlocking_door_lock() -> None:
    """Door lock access should require admin-level permissions."""

    config = _load_config()
    device_manager = DeviceManager(config)
    permission_manager = PermissionManager()
    door_lock = device_manager.get_device("front_door_lock")

    assert permission_manager.check_permission("member", door_lock, "unlock") is False
    assert permission_manager.check_permission("admin", door_lock, "unlock") is True


def test_get_all_status_contains_configured_sim_devices() -> None:
    """Device manager should return status for all configured simulated devices."""

    config = _load_config()
    device_manager = DeviceManager(config)

    statuses = device_manager.get_all_status()

    assert "bedroom_light" in statuses
    assert "front_door_lock" in statuses
    assert statuses["home_thermostat"]["temperature"] == 24


def test_live_mode_initializes_hue_devices_when_bridge_is_available() -> None:
    """Live mode should create Hue-backed devices when the bridge responds."""

    config = _load_config()
    config.setdefault("devices", {})
    config["devices"]["mode"] = "live"
    config.setdefault("hue", {}).setdefault("bridge", {})
    config["hue"]["bridge"]["ip"] = "192.168.1.2"
    config["hue"]["bridge"]["username"] = "test-user"

    fake_bridge_instance = type(
        "FakeBridge",
        (),
        {
            "connect": lambda self: None,
            "get_all_lights": lambda self: {
                "1": {"name": "卧室灯", "state": {"reachable": True}},
            },
            "get_all_groups": lambda self: {
                "1": {"name": "客厅所有灯"},
            },
            "get_all_scenes": lambda self: {
                "scene-1": {"name": "阅读模式", "group": "1"},
            },
        },
    )()

    with patch("devices.device_manager.HueBridge", return_value=fake_bridge_instance):
        manager = DeviceManager(config)

    assert isinstance(manager.get_device("bedroom_light"), HueLight)
    assert isinstance(manager.get_device("living_room_group"), HueGroup)
    assert isinstance(manager.get_device("scene"), HueSceneDevice)


def test_device_manager_rejects_unknown_mode() -> None:
    """Unsupported device modes should fail fast during initialization."""

    config = _load_config()
    config.setdefault("devices", {})
    config["devices"]["mode"] = "unsupported"

    with pytest.raises(ValueError, match="Unsupported device mode"):
        DeviceManager(config)


def test_live_mode_surfaces_bridge_connection_errors() -> None:
    """Live mode should propagate bridge connection failures to the caller."""

    config = _load_config()
    config.setdefault("devices", {})
    config["devices"]["mode"] = "live"
    config.setdefault("hue", {}).setdefault("bridge", {})
    config["hue"]["bridge"]["ip"] = "192.168.1.2"
    config["hue"]["bridge"]["username"] = "test-user"

    fake_bridge_instance = type(
        "FailingBridge",
        (),
        {
            "connect": lambda self: (_ for _ in ()).throw(
                HueBridgeConnectionError("bridge down")
            ),
        },
    )()

    with patch("devices.device_manager.HueBridge", return_value=fake_bridge_instance):
        with pytest.raises(HueBridgeConnectionError, match="bridge down"):
            DeviceManager(config)
