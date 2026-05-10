"""Tests for tools/smart_home.py — @jarvis_tool smart-home functions."""

from __future__ import annotations

import importlib
from typing import Any
from unittest.mock import MagicMock

import pytest

from core.tool_result import parse_tool_result, tool_message
from tools import _TOOL_REGISTRY, _EXECUTION_CONTEXT


@pytest.fixture(autouse=True)
def _ensure_registered():
    """Re-register smart_home tools if another test cleared the registry."""
    import tools.smart_home as shm
    if "smart_home_control" not in _TOOL_REGISTRY:
        importlib.reload(shm)
    yield


import tools.smart_home as shm  # noqa: E402  — initial import for init()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_device(
    device_id: str = "living_room_light",
    name: str = "Living Room Light",
    status: dict | None = None,
) -> MagicMock:
    dev = MagicMock()
    dev.device_id = device_id
    dev.name = name
    dev.get_status.return_value = status or {"on": True, "brightness": 80}
    return dev


def _inject(
    dm: MagicMock | None = None,
    pm: MagicMock | None = None,
) -> tuple[MagicMock, MagicMock]:
    dm = dm or MagicMock()
    pm = pm or MagicMock()
    pm.check_permission.return_value = True
    shm.init(dm, pm)
    return dm, pm


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_smart_home_control_registered():
    """Both smart_home_control and smart_home_status are in the registry."""
    assert "smart_home_control" in _TOOL_REGISTRY
    assert "smart_home_status" in _TOOL_REGISTRY


def test_smart_home_control_definition_shape():
    entry = _TOOL_REGISTRY["smart_home_control"]
    defn = entry["definition"]
    assert defn["name"] == "smart_home_control"
    assert "input_schema" in defn
    assert "device_id" in defn["input_schema"]["properties"]
    assert "action" in defn["input_schema"]["properties"]
    assert "verified device_id" in defn["description"]
    assert "Do not pass casual" in defn["description"]
    assert entry["destructive"] is True
    assert entry["read_only"] is False


def test_smart_home_status_definition_shape():
    entry = _TOOL_REGISTRY["smart_home_status"]
    defn = entry["definition"]
    assert defn["name"] == "smart_home_status"
    assert "Read smart-home inventory/status" in defn["description"]
    assert "ambiguous entity reference" in defn["description"]
    assert entry["read_only"] is True


# ---------------------------------------------------------------------------
# smart_home_control
# ---------------------------------------------------------------------------


def test_smart_home_control_execute():
    """Successful control delegates to device_manager.execute_command."""
    dm, pm = _inject()
    device = _make_device()
    dm.get_device.return_value = device
    dm.execute_command.return_value = "Living Room Light turned on"

    _EXECUTION_CONTEXT["user_role"] = "owner"
    entry = _TOOL_REGISTRY["smart_home_control"]
    result = entry["execute"](
        "smart_home_control",
        {"device_id": "living_room_light", "action": "turn_on"},
    )

    dm.get_device.assert_called_once_with("living_room_light")
    pm.check_permission.assert_called_once_with("owner", device, "turn_on")
    dm.execute_command.assert_called_once_with("living_room_light", "turn_on", None)
    parsed = parse_tool_result(result)
    assert parsed["status"] == "success"
    assert parsed["message"] == "Living Room Light turned on"


def test_smart_home_control_with_value():
    dm, pm = _inject()
    dm.get_device.return_value = _make_device()
    dm.execute_command.return_value = "brightness set to 50"

    _EXECUTION_CONTEXT["user_role"] = "owner"
    entry = _TOOL_REGISTRY["smart_home_control"]
    result = entry["execute"](
        "smart_home_control",
        {"device_id": "living_room_light", "action": "set_brightness", "value": "50"},
    )

    dm.execute_command.assert_called_once_with("living_room_light", "set_brightness", "50")
    assert "50" in tool_message(result)


def test_smart_home_control_device_not_found():
    dm, pm = _inject()
    dm.get_device.side_effect = KeyError("Unknown device: ghost")

    _EXECUTION_CONTEXT["user_role"] = "owner"
    entry = _TOOL_REGISTRY["smart_home_control"]
    result = entry["execute"](
        "smart_home_control",
        {"device_id": "ghost", "action": "turn_on"},
    )
    assert "not found" in tool_message(result).lower() or "ghost" in tool_message(result)


def test_permission_denied():
    dm, pm = _inject()
    device = _make_device()
    dm.get_device.return_value = device
    pm.check_permission.return_value = False

    _EXECUTION_CONTEXT["user_role"] = "guest"
    entry = _TOOL_REGISTRY["smart_home_control"]
    result = entry["execute"](
        "smart_home_control",
        {"device_id": "living_room_light", "action": "turn_on"},
    )
    assert "permission denied" in tool_message(result).lower()


def test_smart_home_control_execution_failure():
    dm, pm = _inject()
    device = _make_device()
    dm.get_device.return_value = device
    dm.execute_command.side_effect = RuntimeError("bridge timeout")

    _EXECUTION_CONTEXT["user_role"] = "owner"
    entry = _TOOL_REGISTRY["smart_home_control"]
    result = entry["execute"](
        "smart_home_control",
        {"device_id": "living_room_light", "action": "turn_on"},
    )
    assert "failed" in tool_message(result).lower() or "bridge timeout" in tool_message(result).lower()


def test_smart_home_control_default_role():
    """When EXECUTION_CONTEXT has no user_role, defaults to 'owner'."""
    dm, pm = _inject()
    dm.get_device.return_value = _make_device()
    dm.execute_command.return_value = "ok"

    _EXECUTION_CONTEXT.pop("user_role", None)
    entry = _TOOL_REGISTRY["smart_home_control"]
    entry["execute"](
        "smart_home_control",
        {"device_id": "living_room_light", "action": "turn_on"},
    )
    pm.check_permission.assert_called_once()
    assert pm.check_permission.call_args[0][0] == "owner"


# ---------------------------------------------------------------------------
# smart_home_status
# ---------------------------------------------------------------------------


def test_smart_home_status_execute():
    """Status with no device_id returns all devices."""
    dm, _ = _inject()
    dm.get_all_status.return_value = {
        "light1": {"on": True},
        "thermo": {"temp": 22},
    }

    entry = _TOOL_REGISTRY["smart_home_status"]
    result = entry["execute"]("smart_home_status", {})

    dm.get_all_status.assert_called_once()
    parsed = parse_tool_result(result)["data"]["entities"]
    assert "light1" in parsed
    assert "thermo" in parsed


def test_smart_home_status_single_device():
    dm, _ = _inject()
    device = _make_device(status={"on": False, "brightness": 0})
    dm.get_device.return_value = device

    entry = _TOOL_REGISTRY["smart_home_status"]
    result = entry["execute"](
        "smart_home_status",
        {"device_id": "living_room_light"},
    )

    dm.get_device.assert_called_with("living_room_light")
    parsed = parse_tool_result(result)["data"]["status"]
    assert parsed["on"] is False


def test_smart_home_status_device_not_found():
    dm, _ = _inject()
    dm.get_device.side_effect = KeyError("Unknown device: ghost")

    entry = _TOOL_REGISTRY["smart_home_status"]
    result = entry["execute"](
        "smart_home_status",
        {"device_id": "ghost"},
    )
    assert "not found" in tool_message(result).lower() or "ghost" in tool_message(result)


# ---------------------------------------------------------------------------
# Virtual group fan-out
# ---------------------------------------------------------------------------


def test_virtual_group_fanout_all_success():
    """bedroom_group fans out to both whitelamps; all succeed -> aggregate OK."""
    dm, pm = _inject()
    devices = {
        "bedroom_lamp_1": _make_device("bedroom_lamp_1", "Bedroom Lamp 1"),
        "bedroom_lamp_2": _make_device("bedroom_lamp_2", "Bedroom Lamp 2"),
    }
    dm.get_device.side_effect = lambda did: devices[did]
    dm.execute_command.return_value = "ok"

    _EXECUTION_CONTEXT["user_role"] = "owner"
    entry = _TOOL_REGISTRY["smart_home_control"]
    result = entry["execute"](
        "smart_home_control",
        {"device_id": "bedroom_group", "action": "turn_on"},
    )
    assert dm.execute_command.call_count == 2
    dm.execute_command.assert_any_call("bedroom_lamp_1", "turn_on", None)
    dm.execute_command.assert_any_call("bedroom_lamp_2", "turn_on", None)
    assert "2/2" in result


def test_virtual_group_fanout_partial_failure():
    """One member raises, the other succeeds -> partial result."""
    dm, pm = _inject()
    devices = {
        "bedroom_lamp_1": _make_device("bedroom_lamp_1", "Bedroom Lamp 1"),
        "bedroom_lamp_2": _make_device("bedroom_lamp_2", "Bedroom Lamp 2"),
    }
    dm.get_device.side_effect = lambda did: devices[did]

    def _exec(did: str, action: str, value: Any) -> str:
        if did == "bedroom_lamp_2":
            raise RuntimeError("bridge timeout")
        return "ok"

    dm.execute_command.side_effect = _exec

    _EXECUTION_CONTEXT["user_role"] = "owner"
    entry = _TOOL_REGISTRY["smart_home_control"]
    result = entry["execute"](
        "smart_home_control",
        {"device_id": "bedroom_group", "action": "set_brightness", "value": "60"},
    )
    assert "1/2" in result
    assert "Bedroom Lamp 2" in result
    assert "bridge timeout" in result


def test_virtual_group_fanout_value_passed():
    """Value (e.g. brightness) is forwarded to every member."""
    dm, pm = _inject()
    devices = {
        "bedroom_lamp_1": _make_device("bedroom_lamp_1", "Bedroom Lamp 1"),
        "bedroom_lamp_2": _make_device("bedroom_lamp_2", "Bedroom Lamp 2"),
    }
    dm.get_device.side_effect = lambda did: devices[did]
    dm.execute_command.return_value = "ok"

    _EXECUTION_CONTEXT["user_role"] = "owner"
    entry = _TOOL_REGISTRY["smart_home_control"]
    entry["execute"](
        "smart_home_control",
        {"device_id": "bedroom_group", "action": "set_brightness", "value": "60"},
    )
    dm.execute_command.assert_any_call("bedroom_lamp_1", "set_brightness", "60")
    dm.execute_command.assert_any_call("bedroom_lamp_2", "set_brightness", "60")
