"""Tests for Philips Hue discovery, bridge integration, and live device setup."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from devices.device_manager import DeviceManager
from devices.hue.hue_bridge import (
    HueBridge,
    HueBridgeAuthenticationError,
    HueBridgeConnectionError,
)
from devices.hue.hue_discovery import BridgeDiscoveryResult
from devices.hue.hue_discovery import HueDiscovery
from devices.hue.hue_group import HueGroup
from devices.hue.hue_light import HueLight
from devices.hue.hue_scene import HueSceneDevice
from tests.helpers import load_config


def _build_live_config() -> dict:
    """Create a config copy for live Hue tests."""

    config = load_config()
    config.setdefault("devices", {})
    config["devices"]["mode"] = "live"
    config.setdefault("hue", {}).setdefault("bridge", {})
    config["hue"]["bridge"]["ip"] = "192.168.1.2"
    config["hue"]["bridge"]["username"] = "test-user"
    config["hue"]["bridge"]["verify_ssl"] = False
    config["hue"]["bridge"]["allow_http_fallback"] = True
    return config


class _FakeResponse:
    """Minimal response object used to fake requests responses."""

    def __init__(self, payload, status_code: int = 200) -> None:
        """Store the response payload and status code."""

        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        """Raise for HTTP error status codes."""

        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        """Return the fake JSON payload."""

        return self._payload


class _FakePhueBridge:
    """Minimal fake phue/phue2 Bridge class."""

    def __init__(self, ip: str, username: str, **kwargs) -> None:
        """Capture the bridge constructor arguments for later inspection."""

        self.ip = ip
        self.username = username
        self.kwargs = kwargs


def test_hue_discovery_validates_bridge_via_discovery_and_config_probe() -> None:
    """Bridge discovery should query discovery and then validate bridge config."""

    config = _build_live_config()
    config["hue"]["bridge"]["ip"] = ""
    discovery = HueDiscovery(config)

    with patch("devices.hue.hue_discovery.requests.get") as mock_get:
        mock_get.side_effect = [
            _FakeResponse([{"id": "bridge-1", "internalipaddress": "192.168.1.2"}]),
            _FakeResponse({"name": "Hue Bridge"}),
        ]

        results = discovery.discover_bridges()

    assert len(results) == 1
    assert results[0].ip == "192.168.1.2"
    assert results[0].online is True


def test_hue_bridge_connects_and_reads_resources() -> None:
    """Hue bridge should prefer phue2/phue Bridge and validate the username."""

    config = _build_live_config()
    bridge = HueBridge(config)

    with patch.object(HueBridge, "_load_bridge_class", return_value=_FakePhueBridge):
        with patch.object(bridge._session, "request") as mock_request:
            mock_request.side_effect = [
                _FakeResponse({"name": "Hue Bridge"}),
                _FakeResponse({"name": "Hue Bridge"}),
                _FakeResponse({"1": {"name": "卧室灯", "state": {"reachable": True}}}),
                _FakeResponse({"1": {"name": "客厅所有灯"}}),
                _FakeResponse({"scene-1": {"name": "阅读模式", "group": "1"}}),
            ]

            bridge.connect()
            lights = bridge.get_all_lights()
            groups = bridge.get_all_groups()
            scenes = bridge.get_all_scenes()

    assert bridge.username == "test-user"
    assert lights["1"]["name"] == "卧室灯"
    assert groups["1"]["name"] == "客厅所有灯"
    assert scenes["scene-1"]["name"] == "阅读模式"


def test_hue_bridge_raises_on_auth_error() -> None:
    """Bridge connect should surface authentication failures clearly."""

    config = _build_live_config()
    bridge = HueBridge(config)

    with patch.object(HueBridge, "_load_bridge_class", return_value=_FakePhueBridge):
        with patch.object(bridge._session, "request") as mock_request:
            mock_request.side_effect = [
                _FakeResponse({"name": "Hue Bridge"}),
                _FakeResponse([{"error": {"type": 1, "description": "unauthorized user"}}]),
            ]

            with pytest.raises(HueBridgeAuthenticationError):
                bridge.connect()


def test_hue_bridge_raises_connection_error_when_bridge_is_unreachable() -> None:
    """Bridge connect should wrap network failures as connection errors."""

    config = _build_live_config()
    bridge = HueBridge(config)

    with patch.object(HueBridge, "_load_bridge_class", return_value=_FakePhueBridge):
        with patch.object(bridge._session, "request") as mock_request:
            mock_request.side_effect = requests.RequestException("timeout")

            with pytest.raises(HueBridgeConnectionError, match="Failed to connect"):
                bridge.connect()


def test_hue_bridge_reconnect_updates_ip_and_is_connected_handles_errors() -> None:
    """Reconnect should refresh the bridge IP and failed probes should report False."""

    config = _build_live_config()
    bridge = HueBridge(config)
    bridge._base_url = "http://192.168.1.2"

    with patch.object(
        bridge.discovery,
        "choose_bridge",
        return_value=BridgeDiscoveryResult(
            ip="192.168.1.9",
            bridge_id="bridge-1",
            online=True,
            config={"name": "Hue Bridge"},
        ),
    ):
        with patch.object(bridge, "connect") as mock_connect:
            bridge.reconnect()

    assert bridge.ip == "192.168.1.9"
    assert bridge._base_url is None
    assert mock_connect.called

    with patch.object(
        bridge,
        "request",
        side_effect=HueBridgeConnectionError("network down"),
    ):
        assert bridge.is_connected() is False


def test_hue_bridge_create_username_raises_when_pairing_is_rejected() -> None:
    """Pairing should raise an auth error when the bridge button was not pressed."""

    config = _build_live_config()
    config["hue"]["bridge"]["username"] = ""
    bridge = HueBridge(config)

    with patch.object(bridge, "_choose_base_url", return_value="http://192.168.1.2"):
        with patch.object(bridge._session, "request") as mock_request:
            mock_request.return_value = _FakeResponse(
                [{"error": {"type": 101, "description": "link button not pressed"}}]
            )

            with pytest.raises(
                HueBridgeAuthenticationError,
                match="link button not pressed",
            ):
                bridge.create_username()


def test_hue_light_translates_brightness_and_color_commands() -> None:
    """HueLight should translate app-level commands into Hue payloads."""

    bridge = MagicMock()
    bridge.username = "user"
    bridge.request.side_effect = [
        None,
        {"state": {"xy": [0.1, 0.2], "ct": 250, "reachable": True}, "type": "Extended color light"},
        None,
    ]
    light = HueLight(bridge=bridge, light_id="1", device_id="bedroom_light", name="卧室灯")

    result_brightness = light.execute("set_brightness", 50)
    result_color = light.execute("set_color", "blue")

    assert "50%" in result_brightness
    assert "blue" in result_color
    first_call = bridge.request.call_args_list[0]
    assert first_call.args[0] == "PUT"
    assert first_call.args[1].endswith("/lights/1/state")
    assert first_call.args[2]["bri"] == 127


def test_hue_light_reports_status_and_degrades_when_capabilities_are_missing() -> None:
    """HueLight should report normalized status and gracefully skip unsupported features."""

    bridge = MagicMock()
    bridge.username = "user"
    bridge.request.side_effect = [
        {"state": {"reachable": True}, "type": "Dimmable light"},
        {"state": {"reachable": True}, "type": "Dimmable light"},
        {
            "state": {
                "on": True,
                "bri": 127,
                "ct": 250,
                "xy": [0.2, 0.3],
                "effect": "none",
                "reachable": False,
            },
            "type": "Extended color light",
        },
    ]
    light = HueLight(bridge=bridge, light_id="1", device_id="bedroom_light", name="卧室灯")

    color_temp_result = light.execute("set_color_temp", "warm")
    color_result = light.execute("set_color", "blue")
    status = light.get_status()

    assert "不支持色温控制" in color_temp_result
    assert "不支持颜色控制" in color_result
    assert status["status_text"] == "不可达"
    assert status["brightness"] == 50
    assert status["color_temp_kelvin"] == 4000
    assert status["color_xy"] == [0.2, 0.3]


def test_hue_group_and_scene_support_live_actions() -> None:
    """Hue groups and scenes should execute through the bridge action endpoint."""

    bridge = MagicMock()
    bridge.username = "user"
    bridge.get_all_scenes.return_value = {"scene-1": {"name": "阅读模式", "group": "2"}}
    group = HueGroup(bridge=bridge, group_id="2", device_id="living_room_group", name="客厅")
    scene_device = HueSceneDevice(bridge=bridge, scene_aliases={"阅读模式": ["阅读场景"]})

    group.execute("turn_off")
    result = scene_device.execute("activate", "阅读场景")

    assert "已激活场景" in result
    assert bridge.request.call_args_list[0].args[1].endswith("/groups/2/action")
    assert bridge.request.call_args_list[-1].args[2] == {"scene": "scene-1"}


def test_hue_group_reports_status_and_validates_effect_values() -> None:
    """HueGroup should normalize group status and reject unsupported effects."""

    bridge = MagicMock()
    bridge.username = "user"
    bridge.request.side_effect = [
        None,
        {
            "action": {
                "on": True,
                "bri": 64,
                "ct": 250,
                "xy": [0.1, 0.2],
                "effect": "colorloop",
            },
            "state": {"any_on": True, "all_on": False},
            "lights": ["1", "2"],
            "class": "Room",
        },
    ]
    group = HueGroup(
        bridge=bridge,
        group_id="2",
        device_id="living_room_group",
        name="客厅所有灯",
    )

    result = group.execute("set_effect", "colorloop")
    status = group.get_status()

    assert "colorloop" in result
    assert status["brightness"] == 25
    assert status["any_on"] is True
    assert status["lights"] == ["1", "2"]

    with pytest.raises(ValueError, match="Supported Hue effects"):
        group.execute("set_effect", "blink")


def test_device_manager_initializes_live_hue_devices_from_aliases() -> None:
    """Live mode should scan Hue resources and assign config-based device IDs."""

    config = _build_live_config()
    fake_bridge_instance = MagicMock()
    fake_bridge_instance.get_all_lights.return_value = {
        "1": {"name": "卧室灯", "state": {"reachable": True}},
        "2": {"name": "客厅灯", "state": {"reachable": True}},
    }
    fake_bridge_instance.get_all_groups.return_value = {
        "1": {"name": "客厅所有灯"},
        "0": {"name": "All lights"},
    }
    fake_bridge_instance.get_all_scenes.return_value = {
        "scene-1": {"name": "阅读模式", "group": "1"},
    }

    with patch("devices.device_manager.HueBridge", return_value=fake_bridge_instance):
        manager = DeviceManager(config)

    assert fake_bridge_instance.connect.called
    assert isinstance(manager.get_device("bedroom_light"), HueLight)
    assert isinstance(manager.get_device("living_room_group"), HueGroup)
    assert isinstance(manager.get_device("scene"), HueSceneDevice)
