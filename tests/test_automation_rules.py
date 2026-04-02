"""Tests for automation_rules."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from core.automation_rules import AutomationRule, AutomationRuleManager


@pytest.fixture
def tmp_rules_path(tmp_path):
    return tmp_path / "rules.json"


@pytest.fixture
def mock_scheduler():
    s = MagicMock()
    s.available = True
    return s


@pytest.fixture
def manager(tmp_rules_path, mock_scheduler):
    executed = []
    return AutomationRuleManager(
        rules_path=tmp_rules_path,
        scheduler=mock_scheduler,
        action_executor=lambda actions: executed.extend(actions),
    ), executed


# --- AutomationRule ---

class TestAutomationRule:
    def test_to_dict_roundtrip(self):
        rule = AutomationRule(
            name="test",
            trigger={"type": "keyword", "keyword": "晚安"},
            actions=[{"device_id": "light", "action": "turn_off"}],
        )
        data = rule.to_dict()
        restored = AutomationRule.from_dict(data)
        assert restored.name == "test"
        assert restored.trigger["keyword"] == "晚安"
        assert len(restored.actions) == 1
        assert restored.enabled is True


# --- Create ---

class TestCreateRule:
    def test_create_keyword_rule(self, manager):
        mgr, _ = manager
        result = mgr.create_rule({
            "name": "晚安模式",
            "trigger": {"type": "keyword", "keyword": "晚安"},
            "actions": [
                {"device_id": "living_room_light", "action": "turn_off"},
                {"device_id": "front_door_lock", "action": "lock"},
            ],
        })
        assert "已创建" in result
        assert "晚安模式" in mgr.list_rules()

    def test_create_cron_rule(self, manager, mock_scheduler):
        mgr, _ = manager
        result = mgr.create_rule({
            "name": "早起开灯",
            "trigger": {"type": "cron", "hour": 7, "minute": 0, "days": "weekdays"},
            "actions": [{"device_id": "living_room_light", "action": "turn_on"}],
        })
        assert "已创建" in result
        mock_scheduler.add_cron_job.assert_called_once()

    def test_create_once_rule(self, manager, mock_scheduler):
        mgr, _ = manager
        result = mgr.create_rule({
            "name": "30分钟后关空调",
            "trigger": {"type": "once", "delay_minutes": 30},
            "actions": [{"device_id": "home_thermostat", "action": "turn_off"}],
        })
        assert "已创建" in result
        mock_scheduler.add_date_job.assert_called_once()

    def test_create_empty_name(self, manager):
        mgr, _ = manager
        result = mgr.create_rule({"name": "", "trigger": {}, "actions": []})
        assert "不能为空" in result

    def test_create_no_trigger(self, manager):
        mgr, _ = manager
        result = mgr.create_rule({"name": "test", "trigger": {}, "actions": [{"x": 1}]})
        assert "缺少" in result

    def test_create_no_actions(self, manager):
        mgr, _ = manager
        result = mgr.create_rule({"name": "test", "trigger": {"type": "keyword"}, "actions": []})
        assert "缺少" in result


# --- Delete ---

class TestDeleteRule:
    def test_delete_existing(self, manager):
        mgr, _ = manager
        mgr.create_rule({
            "name": "test",
            "trigger": {"type": "keyword", "keyword": "test"},
            "actions": [{"device_id": "x", "action": "y"}],
        })
        result = mgr.delete_rule("test")
        assert "已删除" in result
        assert "test" not in mgr.list_rules()

    def test_delete_nonexistent(self, manager):
        mgr, _ = manager
        result = mgr.delete_rule("不存在")
        assert "没有找到" in result


# --- List ---

class TestListRules:
    def test_list_empty(self, manager):
        mgr, _ = manager
        result = mgr.list_rules()
        assert "没有" in result

    def test_list_with_rules(self, manager):
        mgr, _ = manager
        mgr.create_rule({
            "name": "rule1",
            "trigger": {"type": "keyword", "keyword": "hello"},
            "actions": [{"device_id": "x", "action": "y"}],
        })
        mgr.create_rule({
            "name": "rule2",
            "trigger": {"type": "cron", "hour": 8, "minute": 0, "days": "everyday"},
            "actions": [{"device_id": "x", "action": "y"}],
        })
        result = mgr.list_rules()
        assert "rule1" in result
        assert "rule2" in result
        assert "2 条" in result


# --- Keyword matching ---

class TestKeywordMatching:
    def test_exact_match(self, manager):
        mgr, _ = manager
        mgr.create_rule({
            "name": "晚安模式",
            "trigger": {"type": "keyword", "keyword": "晚安"},
            "actions": [{"device_id": "light", "action": "turn_off"}],
        })
        result = mgr.check_keyword("晚安")
        assert result is not None
        actions, name = result
        assert actions[0]["device_id"] == "light"
        assert name == "晚安模式"

    def test_prefix_match(self, manager):
        mgr, _ = manager
        mgr.create_rule({
            "name": "晚安模式",
            "trigger": {"type": "keyword", "keyword": "晚安"},
            "actions": [{"device_id": "light", "action": "turn_off"}],
        })
        result = mgr.check_keyword("晚安吧")
        assert result is not None

    def test_no_match_keyword_in_middle(self, manager):
        mgr, _ = manager
        mgr.create_rule({
            "name": "晚安模式",
            "trigger": {"type": "keyword", "keyword": "晚安"},
            "actions": [{"device_id": "light", "action": "turn_off"}],
        })
        # "删除晚安规则" 不应触发
        actions = mgr.check_keyword("删除晚安规则")
        assert actions is None

    def test_no_match_empty(self, manager):
        mgr, _ = manager
        assert mgr.check_keyword("随便说点什么") is None

    def test_disabled_rule_not_triggered(self, manager):
        mgr, _ = manager
        mgr.create_rule({
            "name": "test",
            "trigger": {"type": "keyword", "keyword": "test"},
            "actions": [{"device_id": "x", "action": "y"}],
        })
        mgr._rules["test"].enabled = False
        assert mgr.check_keyword("test") is None

    def test_cron_rule_not_keyword_triggered(self, manager):
        mgr, _ = manager
        mgr.create_rule({
            "name": "早起",
            "trigger": {"type": "cron", "hour": 7, "minute": 0, "days": "everyday"},
            "actions": [{"device_id": "x", "action": "y"}],
        })
        assert mgr.check_keyword("早起") is None


# --- Persistence ---

class TestPersistence:
    def test_save_and_reload(self, tmp_rules_path, mock_scheduler):
        mgr1 = AutomationRuleManager(
            rules_path=tmp_rules_path, scheduler=mock_scheduler,
        )
        mgr1.create_rule({
            "name": "persist_test",
            "trigger": {"type": "keyword", "keyword": "hello"},
            "actions": [{"device_id": "x", "action": "y"}],
        })

        # 新实例应该加载到之前的规则
        mgr2 = AutomationRuleManager(
            rules_path=tmp_rules_path, scheduler=mock_scheduler,
        )
        assert "persist_test" in mgr2.list_rules()
        result = mgr2.check_keyword("hello")
        assert result is not None
