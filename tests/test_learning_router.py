"""Tests for core.learning_router — detect and classify learning intent."""

from __future__ import annotations

import pytest

from core.learning_router import LearningRouter, LearningIntent


@pytest.fixture()
def router():
    return LearningRouter(skill_names=["weather", "realtime_data", "time", "todos"])


class TestDetectNone:
    def test_normal_question(self, router):
        assert router.detect("今天天气怎么样") is None

    def test_command(self, router):
        assert router.detect("开客厅灯") is None

    def test_greeting(self, router):
        assert router.detect("你好") is None


class TestDetectConfig:
    def test_alias_with_explicit_trigger(self, router):
        result = router.detect("以后我说收盘就帮我查 NVDA 和 AAPL")
        assert result is not None
        assert result.mode == "config"
        assert "收盘" in result.trigger

    def test_alias_morning(self, router):
        result = router.detect("以后说早安就帮我查天气")
        assert result is not None
        assert result.mode == "config"
        assert "早安" in result.trigger

    def test_remember_every_time(self, router):
        result = router.detect("记住每次说开工就打开VS Code")
        assert result is not None
        assert result.mode == "config"


class TestDetectCompose:
    def test_daily_multi_skill(self, router):
        result = router.detect("每天早上8点帮我查天气和股票")
        assert result is not None
        assert result.mode == "compose"

    def test_weekly_reminder(self, router):
        result = router.detect("每周一早上提醒我开周会")
        assert result is not None
        assert result.mode == "compose"

    def test_daily_single_skill(self, router):
        result = router.detect("每天晚上10点帮我查一下新闻")
        assert result is not None
        assert result.mode == "compose"


class TestDetectCreate:
    def test_learn_new_skill(self, router):
        result = router.detect("学会查航班信息")
        assert result is not None
        assert result.mode == "create"
        assert "航班" in result.description

    def test_add_skill(self, router):
        result = router.detect("帮我加一个查快递的技能")
        assert result is not None
        assert result.mode == "create"

    def test_learn_with_prefix(self, router):
        result = router.detect("学一下帮我查汇率")
        assert result is not None
        assert result.mode == "create"
