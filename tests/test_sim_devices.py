"""Tests for direct simulated device state transitions and validation paths."""

from __future__ import annotations

import pytest

from devices.sim.sim_door_lock import SimDoorLock
from devices.sim.sim_light import SimLight
from devices.sim.sim_thermostat import SimThermostat


def test_sim_light_updates_state_and_validates_inputs() -> None:
    """SimLight should handle all supported actions and reject invalid values."""

    light = SimLight(device_id="desk_light", name="书桌灯")

    assert "已打开" in light.execute("turn_on")
    assert "0%" in light.execute("set_brightness", 0)
    assert light.get_status()["is_on"] is False
    assert "cool" in light.execute("set_color_temp", "cool")
    assert "blue" in light.execute("set_color", "blue")
    assert light.get_status()["color"] == "blue"

    with pytest.raises(ValueError, match="Brightness must be between 0 and 100"):
        light.execute("set_brightness", 120)

    with pytest.raises(ValueError, match="Unsupported color"):
        light.execute("set_color", "beige")


def test_sim_door_lock_supports_lock_and_unlock_only() -> None:
    """SimDoorLock should expose lock state changes and reject unsupported actions."""

    lock = SimDoorLock(device_id="front_door", name="入户门锁")

    assert "已解锁" in lock.execute("unlock")
    assert lock.get_status()["is_locked"] is False
    assert "已锁定" in lock.execute("lock")
    assert lock.get_status()["is_locked"] is True

    with pytest.raises(ValueError, match="Unsupported door lock action"):
        lock.execute("open")


def test_sim_thermostat_validates_temperature_range() -> None:
    """SimThermostat should support power control and enforce the valid range."""

    thermostat = SimThermostat(device_id="living_room_ac", name="客厅空调")

    assert "已开启" in thermostat.execute("turn_on")
    assert "30 度" in thermostat.execute("set_temperature", 30)
    assert thermostat.get_status()["temperature"] == 30
    assert "已关闭" in thermostat.execute("turn_off")

    with pytest.raises(ValueError, match="Temperature must be between 16 and 30"):
        thermostat.execute("set_temperature", 31)
