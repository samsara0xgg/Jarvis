"""Tests for scheduler and automation modules."""

from unittest.mock import MagicMock

from core.automation_engine import AutomationEngine
from core.event_bus import EventBus
from skills.automation import AutomationSkill
from skills.scheduler_skill import SchedulerSkill


class TestAutomationEngine:
    def _make_engine(self):
        dm = MagicMock()
        dm.execute_command.return_value = "OK"
        bus = EventBus()
        spoken = []
        engine = AutomationEngine(dm, bus, tts_callback=lambda t: spoken.append(t))
        return engine, dm, bus, spoken

    def test_register_and_list_scenes(self):
        engine, *_ = self._make_engine()
        engine.register_scene("晚安", [{"type": "speak", "text": "晚安"}])
        assert "晚安" in engine.list_scenes()

    def test_execute_device_step(self):
        engine, dm, *_ = self._make_engine()
        engine.register_scene("test", [
            {"type": "device", "device_id": "light1", "action": "turn_off"},
        ])
        results = engine.execute_scene("test")
        dm.execute_command.assert_called_once_with("light1", "turn_off", None)
        assert len(results) == 1

    def test_execute_speak_step(self):
        engine, _, _, spoken = self._make_engine()
        engine.register_scene("test", [
            {"type": "speak", "text": "hello"},
        ])
        engine.execute_scene("test")
        assert spoken == ["hello"]

    def test_execute_event_step(self):
        engine, _, bus, _ = self._make_engine()
        received = []
        bus.on("test.event", lambda d: received.append(d))
        engine.register_scene("test", [
            {"type": "event", "event": "test.event", "data": {"key": "val"}},
        ])
        engine.execute_scene("test")
        assert received == [{"key": "val"}]

    def test_execute_unknown_scene(self):
        engine, *_ = self._make_engine()
        results = engine.execute_scene("nonexistent")
        assert "Unknown scene" in results[0]

    def test_execute_unknown_step_type(self):
        engine, *_ = self._make_engine()
        engine.register_scene("test", [{"type": "fly_to_moon"}])
        results = engine.execute_scene("test")
        assert "Unknown step type" in results[0]

    def test_multi_step_scene(self):
        engine, dm, _, spoken = self._make_engine()
        engine.register_scene("出门", [
            {"type": "device", "device_id": "light1", "action": "turn_off"},
            {"type": "device", "device_id": "lock1", "action": "lock"},
            {"type": "speak", "text": "一路顺风"},
        ])
        results = engine.execute_scene("出门")
        assert len(results) == 3
        assert dm.execute_command.call_count == 2
        assert spoken == ["一路顺风"]

    def test_step_failure_does_not_stop_scene(self):
        engine, dm, _, spoken = self._make_engine()
        dm.execute_command.side_effect = RuntimeError("device offline")
        engine.register_scene("test", [
            {"type": "device", "device_id": "light1", "action": "turn_off"},
            {"type": "speak", "text": "done"},
        ])
        results = engine.execute_scene("test")
        assert "failed" in results[0]
        assert spoken == ["done"]


class TestAutomationSkill:
    def test_skill_name(self):
        engine = MagicMock()
        skill = AutomationSkill(engine)
        assert skill.skill_name == "automation"

    def test_tool_definitions(self):
        engine = MagicMock()
        skill = AutomationSkill(engine)
        tools = skill.get_tool_definitions()
        names = {t["name"] for t in tools}
        assert "run_automation" in names
        assert "list_automations" in names

    def test_run_automation(self):
        engine = MagicMock()
        engine.execute_scene.return_value = ["Step 1: OK"]
        skill = AutomationSkill(engine)
        result = skill.execute("run_automation", {"scene_name": "晚安"})
        engine.execute_scene.assert_called_once_with("晚安")
        assert "晚安" in result

    def test_list_automations(self):
        engine = MagicMock()
        engine.list_scenes.return_value = ["晚安", "出门"]
        skill = AutomationSkill(engine)
        result = skill.execute("list_automations", {})
        assert "晚安" in result
        assert "出门" in result

    def test_list_empty(self):
        engine = MagicMock()
        engine.list_scenes.return_value = []
        skill = AutomationSkill(engine)
        result = skill.execute("list_automations", {})
        assert "No automation" in result


class TestSchedulerSkill:
    def test_skill_name(self):
        skill = SchedulerSkill({})
        assert skill.skill_name == "scheduler"

    def test_tool_definitions(self):
        skill = SchedulerSkill({})
        tools = skill.get_tool_definitions()
        names = {t["name"] for t in tools}
        assert "schedule_task" in names
        assert "list_scheduled_tasks" in names
        assert "cancel_scheduled_task" in names

    def test_schedule_without_scheduler(self):
        skill = SchedulerSkill({})
        result = skill.execute("schedule_task", {
            "task_id": "test",
            "description": "test task",
            "run_at": "2026-01-01T00:00:00",
        })
        assert "not available" in result

    def test_list_without_scheduler(self):
        skill = SchedulerSkill({})
        result = skill.execute("list_scheduled_tasks", {})
        assert "not available" in result

    def test_schedule_date_job(self):
        scheduler = MagicMock()
        scheduler.available = True
        scheduler.add_date_job.return_value = "job_1"
        skill = SchedulerSkill({}, scheduler=scheduler)
        result = skill.execute("schedule_task", {
            "task_id": "reminder1",
            "description": "Buy milk",
            "run_at": "2026-03-28T15:00:00",
        })
        scheduler.add_date_job.assert_called_once()
        assert "Buy milk" in result

    def test_schedule_cron_job(self):
        scheduler = MagicMock()
        scheduler.available = True
        scheduler.add_cron_job.return_value = "job_2"
        skill = SchedulerSkill({}, scheduler=scheduler)
        result = skill.execute("schedule_task", {
            "task_id": "morning",
            "description": "Morning briefing",
            "cron_hour": "7",
            "cron_minute": "0",
        })
        scheduler.add_cron_job.assert_called_once()
        assert "Morning briefing" in result

    def test_cancel_job(self):
        scheduler = MagicMock()
        scheduler.available = True
        scheduler.remove_job.return_value = True
        skill = SchedulerSkill({}, scheduler=scheduler)
        result = skill.execute("cancel_scheduled_task", {"task_id": "reminder1"})
        assert "Cancelled" in result
