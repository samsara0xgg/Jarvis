"""Unit tests for LocalExecutor."""

from __future__ import annotations

from unittest.mock import MagicMock

from core.local_executor import Action, ActionResponse, LocalExecutor


def test_execute_skill_alias():
    registry = MagicMock()
    registry.execute.return_value = "NVDA: $120.50, AAPL: $185.20"

    executor = LocalExecutor(registry)
    actions = [{"skill": "realtime_data", "tool": "get_stock_watchlist", "params": {"symbols": ["NVDA"]}}]
    result = executor.execute_skill_alias(actions, "owner")

    assert result.action == Action.REQLLM
    assert "NVDA" in result.text
    registry.execute.assert_called_once_with("get_stock_watchlist", {"symbols": ["NVDA"]}, user_role="owner")


class TestExecuteInfoQuery:
    """Verify info_query returns Action.RESPONSE directly without going through LLM."""

    def _make_executor(self, return_value: str) -> tuple[LocalExecutor, MagicMock]:
        registry = MagicMock()
        registry.execute.return_value = return_value
        return LocalExecutor(registry), registry

    def test_weather_returns_response(self) -> None:
        executor, _ = self._make_executor("今天多伦多晴，最高18°C，最低8°C。")
        result = executor.execute_info_query("weather", None)
        assert result.action == Action.RESPONSE
        assert "18" in result.text

    def test_stocks_returns_response(self) -> None:
        executor, _ = self._make_executor("NVDA: $120.50, AAPL: $185.20")
        result = executor.execute_info_query("stocks", ["NVDA", "AAPL"])
        assert result.action == Action.RESPONSE
        assert "NVDA" in result.text

    def test_news_returns_response(self) -> None:
        executor, _ = self._make_executor("今日要闻：AI立法草案提交国会。")
        result = executor.execute_info_query("news", "tech")
        assert result.action == Action.RESPONSE
        assert "AI" in result.text

    def test_failed_query_returns_response_with_fallback(self) -> None:
        registry = MagicMock()
        registry.execute.return_value = ""  # empty string → falsy
        executor = LocalExecutor(registry)
        result = executor.execute_info_query("weather", None)
        assert result.action == Action.RESPONSE
        assert "没查到" in result.text
