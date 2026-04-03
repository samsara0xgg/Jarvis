"""Tests for skill execute methods — TimeSkill, SmartHomeSkill, WeatherSkill,
TodoSkill, ReminderSkill, MemorySkill, SystemControlSkill.

Targets low-coverage skills with focused execute-path testing.
"""

from __future__ import annotations

import json
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# TimeSkill
# ---------------------------------------------------------------------------
from skills.time_skill import TimeSkill


class TestTimeSkill:
    @staticmethod
    def _make_skill() -> TimeSkill:
        return TimeSkill(config={})

    def test_get_current_time(self):
        skill = self._make_skill()
        result = skill.execute("get_current_time", {})
        assert "Current time:" in result
        # Should contain day-of-week
        now = datetime.now()
        assert now.strftime("%A") in result

    def test_set_timer_positive(self):
        skill = self._make_skill()
        result = skill.execute("set_timer", {"seconds": 5, "label": "test"})
        assert "Timer set" in result
        assert "'test'" in result
        assert "5 seconds" in result
        # Clean up
        skill.cancel_all()

    def test_set_timer_minutes_display(self):
        skill = self._make_skill()
        result = skill.execute("set_timer", {"seconds": 90, "label": "pasta"})
        assert "1 minutes 30 seconds" in result
        skill.cancel_all()

    def test_set_timer_zero_rejected(self):
        skill = self._make_skill()
        result = skill.execute("set_timer", {"seconds": 0, "label": "bad"})
        assert "positive" in result.lower()

    def test_set_timer_negative_rejected(self):
        skill = self._make_skill()
        result = skill.execute("set_timer", {"seconds": -5, "label": "bad"})
        assert "positive" in result.lower()

    def test_set_timer_default_label(self):
        skill = self._make_skill()
        result = skill.execute("set_timer", {"seconds": 10})
        assert "'timer'" in result
        skill.cancel_all()

    def test_unknown_tool(self):
        skill = self._make_skill()
        result = skill.execute("nonexistent", {})
        assert "Unknown" in result

    def test_timer_fires_callback(self):
        skill = self._make_skill()
        callback = MagicMock()
        skill.set_timer_callback(callback)

        # Use a very short timer for testing
        skill.execute("set_timer", {"seconds": 1, "label": "quick"})

        # Wait for timer to fire
        import time
        time.sleep(1.5)

        callback.assert_called_once()
        assert "quick" in callback.call_args[0][0]

    def test_timer_replaces_existing(self):
        skill = self._make_skill()
        skill.execute("set_timer", {"seconds": 100, "label": "dup"})
        skill.execute("set_timer", {"seconds": 100, "label": "dup"})
        # Same timer_id should only have one entry
        assert len(skill._active_timers) == 1
        skill.cancel_all()

    def test_cancel_all(self):
        skill = self._make_skill()
        skill.execute("set_timer", {"seconds": 100, "label": "a"})
        skill.execute("set_timer", {"seconds": 100, "label": "b"})
        assert len(skill._active_timers) == 2
        skill.cancel_all()
        assert len(skill._active_timers) == 0

    def test_skill_name(self):
        skill = self._make_skill()
        assert skill.skill_name == "time"

    def test_tool_definitions_structure(self):
        skill = self._make_skill()
        defs = skill.get_tool_definitions()
        names = {d["name"] for d in defs}
        assert "get_current_time" in names
        assert "set_timer" in names


# ---------------------------------------------------------------------------
# SmartHomeSkill
# ---------------------------------------------------------------------------
from skills.smart_home import SmartHomeSkill


class TestSmartHomeSkill:
    @staticmethod
    def _make_skill():
        dm = MagicMock()
        pm = MagicMock()
        dm.get_all_status.return_value = {"light_1": {"on": True}, "thermostat": {"temp": 22}}
        return SmartHomeSkill(dm, pm), dm, pm

    def test_skill_name(self):
        skill, _, _ = self._make_skill()
        assert skill.skill_name == "smart_home"

    def test_status_all_devices(self):
        skill, dm, _ = self._make_skill()
        dm.get_all_status.return_value = {"light": {"on": True}}
        result = skill.execute("smart_home_status", {})
        parsed = json.loads(result)
        assert "light" in parsed

    def test_status_specific_device(self):
        skill, dm, _ = self._make_skill()
        mock_device = MagicMock()
        mock_device.get_status.return_value = {"on": True, "brightness": 80}
        dm.get_device.return_value = mock_device
        result = skill.execute("smart_home_status", {"device_id": "light_1"})
        parsed = json.loads(result)
        assert parsed["on"] is True

    def test_status_device_not_found(self):
        skill, dm, _ = self._make_skill()
        dm.get_device.side_effect = KeyError("Unknown device: bad_id")
        result = skill.execute("smart_home_status", {"device_id": "bad_id"})
        assert "not found" in result.lower()

    def test_control_success(self):
        skill, dm, pm = self._make_skill()
        mock_device = MagicMock()
        mock_device.name = "Living Room Light"
        dm.get_device.return_value = mock_device
        pm.check_permission.return_value = True
        dm.execute_command.return_value = "Light turned on."

        result = skill.execute(
            "smart_home_control",
            {"device_id": "light_1", "action": "turn_on"},
            user_role="owner",
        )
        assert "Light turned on" in result
        dm.execute_command.assert_called_once_with("light_1", "turn_on", None)

    def test_control_with_value(self):
        skill, dm, pm = self._make_skill()
        mock_device = MagicMock()
        mock_device.name = "Light"
        dm.get_device.return_value = mock_device
        pm.check_permission.return_value = True
        dm.execute_command.return_value = "Brightness set to 50."

        result = skill.execute(
            "smart_home_control",
            {"device_id": "light_1", "action": "set_brightness", "value": 50},
            user_role="owner",
        )
        assert "Brightness" in result
        dm.execute_command.assert_called_once_with("light_1", "set_brightness", 50)

    def test_control_device_not_found(self):
        skill, dm, pm = self._make_skill()
        dm.get_device.side_effect = KeyError("Unknown device: nope")

        result = skill.execute(
            "smart_home_control",
            {"device_id": "nope", "action": "turn_on"},
            user_role="owner",
        )
        assert "not found" in result.lower()

    def test_control_permission_denied(self):
        skill, dm, pm = self._make_skill()
        mock_device = MagicMock()
        mock_device.name = "Front Door"
        dm.get_device.return_value = mock_device
        pm.check_permission.return_value = False

        result = skill.execute(
            "smart_home_control",
            {"device_id": "door", "action": "unlock"},
            user_role="guest",
        )
        assert "Permission denied" in result

    def test_control_execute_error(self):
        skill, dm, pm = self._make_skill()
        mock_device = MagicMock()
        mock_device.name = "Thermostat"
        dm.get_device.return_value = mock_device
        pm.check_permission.return_value = True
        dm.execute_command.side_effect = RuntimeError("Hardware error")

        result = skill.execute(
            "smart_home_control",
            {"device_id": "thermo", "action": "set_temperature", "value": 25},
            user_role="owner",
        )
        assert "Failed" in result
        assert "Hardware error" in result

    def test_unknown_tool(self):
        skill, _, _ = self._make_skill()
        result = skill.execute("smart_home_bogus", {})
        assert "Unknown" in result

    def test_default_role_is_guest(self):
        skill, dm, pm = self._make_skill()
        mock_device = MagicMock()
        mock_device.name = "Light"
        dm.get_device.return_value = mock_device
        pm.check_permission.return_value = True
        dm.execute_command.return_value = "OK"

        # No user_role in context -> defaults to "guest"
        skill.execute("smart_home_control", {"device_id": "l", "action": "turn_on"})
        pm.check_permission.assert_called_once_with("guest", mock_device, "turn_on")


# ---------------------------------------------------------------------------
# WeatherSkill
# ---------------------------------------------------------------------------
from skills.weather import WeatherSkill


class TestWeatherSkill:
    @staticmethod
    def _make_skill(default_city="Vancouver"):
        config = {"skills": {"weather": {"default_city": default_city}}}
        return WeatherSkill(config)

    @staticmethod
    def _mock_weather_response():
        return {
            "current_condition": [
                {
                    "temp_C": "18",
                    "FeelsLikeC": "16",
                    "humidity": "65",
                    "windspeedKmph": "12",
                    "lang_zh": [{"value": "晴天"}],
                    "weatherDesc": [{"value": "Sunny"}],
                }
            ]
        }

    def test_skill_name(self):
        skill = self._make_skill()
        assert skill.skill_name == "weather"

    @patch("skills.weather.requests.get")
    def test_get_weather_success(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = self._mock_weather_response()
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        skill = self._make_skill()
        result = skill.execute("get_weather", {"city": "Toronto"})
        assert "Toronto" in result
        assert "18" in result
        assert "晴天" in result
        assert "65" in result

    @patch("skills.weather.requests.get")
    def test_get_weather_default_city(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = self._mock_weather_response()
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        skill = self._make_skill(default_city="Vancouver")
        result = skill.execute("get_weather", {})
        assert "Vancouver" in result
        mock_get.assert_called_once()
        assert "Vancouver" in mock_get.call_args[0][0]

    @patch("skills.weather.requests.get")
    def test_get_weather_empty_city_uses_default(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = self._mock_weather_response()
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        skill = self._make_skill(default_city="Montreal")
        result = skill.execute("get_weather", {"city": "  "})
        assert "Montreal" in result

    @patch("skills.weather.requests.get")
    def test_get_weather_network_error(self, mock_get):
        mock_get.side_effect = ConnectionError("Network unreachable")

        skill = self._make_skill()
        result = skill.execute("get_weather", {"city": "Tokyo"})
        assert "Failed" in result
        assert "Tokyo" in result

    @patch("skills.weather.requests.get")
    def test_get_weather_http_error(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("404 Not Found")
        mock_get.return_value = mock_resp

        skill = self._make_skill()
        result = skill.execute("get_weather", {"city": "Nowhere"})
        assert "Failed" in result

    @patch("skills.weather.requests.get")
    def test_get_weather_missing_zh_fallback(self, mock_get):
        """When lang_zh is missing, should fall back to weatherDesc."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "current_condition": [
                {
                    "temp_C": "20",
                    "FeelsLikeC": "19",
                    "humidity": "50",
                    "windspeedKmph": "8",
                    "lang_zh": [{}],
                    "weatherDesc": [{"value": "Cloudy"}],
                }
            ]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        skill = self._make_skill()
        result = skill.execute("get_weather", {"city": "London"})
        assert "Cloudy" in result

    def test_tool_definitions(self):
        skill = self._make_skill()
        defs = skill.get_tool_definitions()
        assert len(defs) == 1
        assert defs[0]["name"] == "get_weather"


# ---------------------------------------------------------------------------
# TodoSkill
# ---------------------------------------------------------------------------
from skills.todos import TodoSkill


class TestTodoSkill:
    @pytest.fixture()
    def skill(self, tmp_path):
        config = {"skills": {"todos": {"dir": str(tmp_path / "todos")}}}
        return TodoSkill(config)

    def test_skill_name(self, skill):
        assert skill.skill_name == "todos"

    def test_add_todo(self, skill):
        result = skill.execute("add_todo", {"content": "Buy milk"}, user_id="alice")
        assert "Todo added" in result
        assert "Buy milk" in result

    def test_add_todo_with_priority(self, skill):
        result = skill.execute("add_todo", {"content": "Urgent task", "priority": "high"}, user_id="alice")
        assert "high" in result

    def test_add_todo_empty_content_rejected(self, skill):
        result = skill.execute("add_todo", {"content": ""}, user_id="alice")
        assert "empty" in result.lower()

    def test_list_todos_empty(self, skill):
        result = skill.execute("list_todos", {}, user_id="alice")
        assert "No active" in result

    def test_list_todos_with_items(self, skill):
        skill.execute("add_todo", {"content": "Task 1"}, user_id="alice")
        skill.execute("add_todo", {"content": "Task 2"}, user_id="alice")
        result = skill.execute("list_todos", {}, user_id="alice")
        assert "Task 1" in result
        assert "Task 2" in result

    def test_complete_todo(self, skill):
        skill.execute("add_todo", {"content": "Complete me"}, user_id="alice")
        # Get the todo ID from the list
        list_result = skill.execute("list_todos", {}, user_id="alice")
        # Extract ID: format is "- [<id>] ..."
        todo_id = list_result.split("[")[1].split("]")[0]

        result = skill.execute("complete_todo", {"todo_id": todo_id}, user_id="alice")
        assert "completed" in result.lower()

        # Should no longer appear in active list
        list_after = skill.execute("list_todos", {}, user_id="alice")
        assert "No active" in list_after

    def test_complete_nonexistent_todo(self, skill):
        result = skill.execute("complete_todo", {"todo_id": "nonexist"}, user_id="alice")
        assert "not found" in result.lower()

    def test_delete_todo(self, skill):
        skill.execute("add_todo", {"content": "Delete me"}, user_id="alice")
        list_result = skill.execute("list_todos", {}, user_id="alice")
        todo_id = list_result.split("[")[1].split("]")[0]

        result = skill.execute("delete_todo", {"todo_id": todo_id}, user_id="alice")
        assert "deleted" in result.lower()

    def test_delete_nonexistent_todo(self, skill):
        result = skill.execute("delete_todo", {"todo_id": "nope"}, user_id="alice")
        assert "not found" in result.lower()

    def test_user_isolation(self, skill):
        skill.execute("add_todo", {"content": "Alice task"}, user_id="alice")
        skill.execute("add_todo", {"content": "Bob task"}, user_id="bob")

        alice_list = skill.execute("list_todos", {}, user_id="alice")
        bob_list = skill.execute("list_todos", {}, user_id="bob")

        assert "Alice task" in alice_list
        assert "Bob task" not in alice_list
        assert "Bob task" in bob_list
        assert "Alice task" not in bob_list

    def test_anonymous_user(self, skill):
        result = skill.execute("add_todo", {"content": "Anon task"})
        assert "Todo added" in result

    def test_unknown_tool(self, skill):
        result = skill.execute("bad_tool", {}, user_id="alice")
        assert "Unknown" in result

    def test_tool_definitions(self, skill):
        defs = skill.get_tool_definitions()
        names = {d["name"] for d in defs}
        assert names == {"add_todo", "list_todos", "complete_todo", "delete_todo"}


# ---------------------------------------------------------------------------
# ReminderSkill
# ---------------------------------------------------------------------------
from skills.reminders import ReminderSkill


class TestReminderSkill:
    @pytest.fixture()
    def skill(self, tmp_path):
        config = {"skills": {"reminders": {"path": str(tmp_path / "reminders.json")}}}
        return ReminderSkill(config)

    def test_skill_name(self, skill):
        assert skill.skill_name == "reminders"

    def test_create_reminder(self, skill):
        result = skill.execute("create_reminder", {"content": "Call mom"}, user_id="alice")
        assert "Reminder created" in result
        assert "Call mom" in result

    def test_create_reminder_with_time(self, skill):
        result = skill.execute(
            "create_reminder",
            {"content": "Meeting", "remind_at": "2026-12-01 09:00"},
            user_id="alice",
        )
        assert "2026-12-01 09:00" in result

    def test_create_reminder_empty_content_rejected(self, skill):
        result = skill.execute("create_reminder", {"content": ""}, user_id="alice")
        assert "empty" in result.lower()

    def test_list_reminders_empty(self, skill):
        result = skill.execute("list_reminders", {}, user_id="alice")
        assert "No active" in result

    def test_list_reminders_with_items(self, skill):
        skill.execute("create_reminder", {"content": "Reminder 1"}, user_id="alice")
        skill.execute("create_reminder", {"content": "Reminder 2"}, user_id="alice")
        result = skill.execute("list_reminders", {}, user_id="alice")
        assert "Reminder 1" in result
        assert "Reminder 2" in result

    def test_complete_reminder(self, skill):
        skill.execute("create_reminder", {"content": "Done soon"}, user_id="alice")
        list_result = skill.execute("list_reminders", {}, user_id="alice")
        reminder_id = list_result.split("[")[1].split("]")[0]

        result = skill.execute("complete_reminder", {"reminder_id": reminder_id}, user_id="alice")
        assert "done" in result.lower()

        list_after = skill.execute("list_reminders", {}, user_id="alice")
        assert "No active" in list_after

    def test_complete_nonexistent_reminder(self, skill):
        result = skill.execute("complete_reminder", {"reminder_id": "nope"}, user_id="alice")
        assert "not found" in result.lower()

    def test_user_isolation(self, skill):
        skill.execute("create_reminder", {"content": "Alice R"}, user_id="alice")
        skill.execute("create_reminder", {"content": "Bob R"}, user_id="bob")

        alice_list = skill.execute("list_reminders", {}, user_id="alice")
        bob_list = skill.execute("list_reminders", {}, user_id="bob")

        assert "Alice R" in alice_list
        assert "Bob R" not in alice_list
        assert "Bob R" in bob_list

    def test_unknown_tool(self, skill):
        result = skill.execute("bad_tool", {}, user_id="alice")
        assert "Unknown" in result

    def test_create_with_scheduler(self, tmp_path):
        mock_scheduler = MagicMock()
        mock_scheduler.available = True
        config = {"skills": {"reminders": {"path": str(tmp_path / "rem.json")}}}
        skill = ReminderSkill(config, scheduler=mock_scheduler)

        skill.execute(
            "create_reminder",
            {"content": "Scheduled", "remind_at": "2026-12-01 09:00"},
            user_id="alice",
        )
        mock_scheduler.add_date_job.assert_called_once()

    def test_tool_definitions(self, skill):
        defs = skill.get_tool_definitions()
        names = {d["name"] for d in defs}
        assert names == {"create_reminder", "list_reminders", "complete_reminder"}


# ---------------------------------------------------------------------------
# MemorySkill
# ---------------------------------------------------------------------------
from skills.memory_skill import MemorySkill


class TestMemorySkill:
    @pytest.fixture()
    def skill(self, tmp_path):
        from memory.user_preferences import UserPreferenceStore
        config = {"memory": {"preferences_dir": str(tmp_path / "prefs")}}
        store = UserPreferenceStore(config)
        return MemorySkill(store)

    def test_skill_name(self, skill):
        assert skill.skill_name == "memory"

    def test_remember_and_recall(self, skill):
        skill.execute("remember", {"key": "drink", "value": "coffee"}, user_id="alice")
        result = skill.execute("recall", {"key": "drink"}, user_id="alice")
        assert "coffee" in result

    def test_remember_missing_key(self, skill):
        result = skill.execute("remember", {"key": "", "value": "x"}, user_id="alice")
        assert "required" in result.lower()

    def test_remember_missing_value(self, skill):
        result = skill.execute("remember", {"key": "k", "value": ""}, user_id="alice")
        assert "required" in result.lower()

    def test_recall_nonexistent_key(self, skill):
        result = skill.execute("recall", {"key": "nope"}, user_id="alice")
        assert "No stored value" in result

    def test_recall_all(self, skill):
        skill.execute("remember", {"key": "color", "value": "blue"}, user_id="alice")
        skill.execute("remember", {"key": "food", "value": "sushi"}, user_id="alice")
        result = skill.execute("recall", {}, user_id="alice")
        assert "color" in result
        assert "blue" in result
        assert "food" in result
        assert "sushi" in result

    def test_recall_all_empty(self, skill):
        result = skill.execute("recall", {}, user_id="newuser")
        assert "No stored preferences" in result

    def test_forget(self, skill):
        skill.execute("remember", {"key": "temp", "value": "data"}, user_id="alice")
        result = skill.execute("forget", {"key": "temp"}, user_id="alice")
        assert "Forgotten" in result

        # Verify gone
        result2 = skill.execute("recall", {"key": "temp"}, user_id="alice")
        assert "No stored value" in result2

    def test_forget_nonexistent(self, skill):
        result = skill.execute("forget", {"key": "nope"}, user_id="alice")
        assert "No stored value" in result

    def test_no_user_id_rejected(self, skill):
        result = skill.execute("remember", {"key": "k", "value": "v"})
        assert "unidentified" in result.lower()

    def test_unknown_tool(self, skill):
        result = skill.execute("bad_tool", {}, user_id="alice")
        assert "Unknown" in result

    def test_tool_definitions(self, skill):
        defs = skill.get_tool_definitions()
        names = {d["name"] for d in defs}
        assert names == {"recall", "forget"}


# ---------------------------------------------------------------------------
# SystemControlSkill
# ---------------------------------------------------------------------------
from skills.system_control import SystemControlSkill, _ALLOWED_COMMANDS


class TestSystemControlSkill:
    @staticmethod
    def _make_skill():
        return SystemControlSkill(config={})

    def test_skill_name(self):
        skill = self._make_skill()
        assert skill.skill_name == "system_control"

    def test_required_role_is_owner(self):
        skill = self._make_skill()
        assert skill.get_required_role() == "owner"

    @patch("skills.system_control.subprocess.run")
    def test_set_volume_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        skill = self._make_skill()
        result = skill.execute("set_system_volume", {"percent": 50})
        assert "50%" in result
        mock_run.assert_called_once()

    @patch("skills.system_control.subprocess.run")
    def test_set_volume_clamps_range(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        skill = self._make_skill()

        # Over 100 should clamp to 100
        result = skill.execute("set_system_volume", {"percent": 150})
        assert "100%" in result

        # Below 0 should clamp to 0
        result = skill.execute("set_system_volume", {"percent": -10})
        assert "0%" in result

    @patch("skills.system_control.subprocess.run")
    def test_set_volume_osascript_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError()
        skill = self._make_skill()
        result = skill.execute("set_system_volume", {"percent": 50})
        assert "macOS" in result

    @patch("skills.system_control.subprocess.run")
    def test_open_application_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        skill = self._make_skill()
        result = skill.execute("open_application", {"app_name": "Safari"})
        assert "Opened Safari" in result

    def test_open_application_empty_name(self):
        skill = self._make_skill()
        result = skill.execute("open_application", {"app_name": ""})
        assert "required" in result.lower()

    @patch("skills.system_control.subprocess.run")
    def test_open_application_failure(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(1, "open")
        skill = self._make_skill()
        result = skill.execute("open_application", {"app_name": "NonExistentApp"})
        assert "Failed" in result

    @patch("skills.system_control.subprocess.run")
    def test_open_application_not_macos(self, mock_run):
        mock_run.side_effect = FileNotFoundError()
        skill = self._make_skill()
        result = skill.execute("open_application", {"app_name": "Safari"})
        assert "macOS" in result

    def test_get_system_info(self):
        skill = self._make_skill()
        result = skill.execute("get_system_info", {})
        assert "OS:" in result
        assert "Machine:" in result
        assert "Hostname:" in result
        assert "Python:" in result

    @patch("skills.system_control.subprocess.run")
    def test_run_allowed_command(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="hello world", stderr="", returncode=0,
        )
        skill = self._make_skill()
        result = skill.execute("run_shell_command", {"command": "echo hello"})
        assert "hello world" in result

    def test_run_disallowed_command(self):
        skill = self._make_skill()
        result = skill.execute("run_shell_command", {"command": "rm -rf /"})
        assert "not allowed" in result.lower()

    def test_run_empty_command(self):
        skill = self._make_skill()
        result = skill.execute("run_shell_command", {"command": ""})
        assert "empty" in result.lower()

    def test_run_invalid_syntax(self):
        skill = self._make_skill()
        result = skill.execute("run_shell_command", {"command": "echo 'unterminated"})
        assert "Invalid" in result or "syntax" in result.lower()

    @patch("skills.system_control.subprocess.run")
    def test_run_command_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired("ls", 30)
        skill = self._make_skill()
        result = skill.execute("run_shell_command", {"command": "ls"})
        assert "timed out" in result.lower()

    @patch("skills.system_control.subprocess.run")
    def test_run_command_truncates_long_output(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="x" * 3000, stderr="", returncode=0,
        )
        skill = self._make_skill()
        result = skill.execute("run_shell_command", {"command": "ls"})
        assert len(result) <= 2000

    @patch("skills.system_control.subprocess.run")
    def test_run_command_includes_stderr(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="", stderr="warning: something", returncode=1,
        )
        skill = self._make_skill()
        result = skill.execute("run_shell_command", {"command": "ls"})
        assert "STDERR" in result

    @patch("skills.system_control.subprocess.run")
    def test_run_command_empty_output(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="", stderr="", returncode=0,
        )
        skill = self._make_skill()
        result = skill.execute("run_shell_command", {"command": "echo"})
        assert "exit code 0" in result.lower()

    def test_unknown_tool(self):
        skill = self._make_skill()
        result = skill.execute("nonexistent_tool", {})
        assert "Unknown" in result

    def test_tool_definitions(self):
        skill = self._make_skill()
        defs = skill.get_tool_definitions()
        names = {d["name"] for d in defs}
        assert names == {"set_system_volume", "open_application", "get_system_info", "run_shell_command"}
