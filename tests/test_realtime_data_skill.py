"""Tests for the RealTimeData Jarvis skill adapter."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from skills.realtime_data import RealTimeDataSkill


def _make_config(mock_mode=True):
    return {
        "skills": {
            "realtime_data": {
                "mock_mode": mock_mode,
                "cache": {"dir": "/tmp/test_realtime_cache"},
                "stocks": {"watchlist": ["AAPL", "NVDA"]},
                "news": {"language": "en"},
            }
        }
    }


@pytest.fixture
def skill():
    return RealTimeDataSkill(_make_config(mock_mode=True))


class TestRealTimeDataSkillInit:
    def test_skill_name(self, skill):
        assert skill.skill_name == "realtime_data"

    def test_tool_definitions_count(self, skill):
        tools = skill.get_tool_definitions()
        assert len(tools) == 3
        names = {t["name"] for t in tools}
        assert names == {"get_news_briefing", "get_stock_watchlist", "get_realtime_data_briefing"}

    def test_mock_mode_uses_mock_providers(self):
        s = RealTimeDataSkill(_make_config(mock_mode=True))
        assert s.service is not None


class TestExecuteNewsBriefing:
    def test_get_news_all(self, skill):
        result = skill.execute("get_news_briefing", {"focus": "all"})
        assert isinstance(result, str)
        assert len(result) > 0

    def test_get_news_single_topic(self, skill):
        result = skill.execute("get_news_briefing", {"focus": "ai", "limit": 3})
        assert isinstance(result, str)

    def test_get_news_force_refresh(self, skill):
        result = skill.execute("get_news_briefing", {"force_refresh": True})
        assert isinstance(result, str)

    def test_get_news_defaults(self, skill):
        result = skill.execute("get_news_briefing", {})
        assert isinstance(result, str)


class TestExecuteStockWatchlist:
    def test_get_stocks_default_watchlist(self, skill):
        result = skill.execute("get_stock_watchlist", {})
        assert isinstance(result, str)

    def test_get_stocks_custom_symbols(self, skill):
        result = skill.execute("get_stock_watchlist", {"symbols": ["TSLA"]})
        assert isinstance(result, str)


class TestExecuteBriefing:
    def test_combined_briefing(self, skill):
        result = skill.execute("get_realtime_data_briefing", {})
        assert isinstance(result, str)

    def test_news_only(self, skill):
        result = skill.execute("get_realtime_data_briefing", {"include_stocks": False})
        assert isinstance(result, str)

    def test_stocks_only(self, skill):
        result = skill.execute("get_realtime_data_briefing", {"include_news": False})
        assert isinstance(result, str)


class TestExecuteEdgeCases:
    def test_unknown_tool(self, skill):
        result = skill.execute("nonexistent_tool", {})
        assert "Unknown tool" in result

    def test_get_briefing_text(self, skill):
        result = skill.get_briefing_text()
        assert isinstance(result, str)


class TestScheduler:
    def test_set_scheduler_disabled(self):
        skill = RealTimeDataSkill(_make_config())
        mock_sched = MagicMock()
        skill.set_scheduler(mock_sched)  # scheduler config not enabled
        mock_sched.add_cron_job.assert_not_called()

    def test_set_scheduler_enabled(self):
        config = _make_config()
        config["skills"]["realtime_data"]["scheduler"] = {
            "enabled": True,
            "refresh_news_minutes": 30,
            "refresh_stocks_minutes": 15,
        }
        skill = RealTimeDataSkill(config)
        mock_sched = MagicMock()
        skill.set_scheduler(mock_sched)
        assert mock_sched.add_interval_job.call_count == 2

    def test_refresh_jobs(self):
        skill = RealTimeDataSkill(_make_config())
        skill._refresh_news_job()  # should not raise
        skill._refresh_stocks_job()
