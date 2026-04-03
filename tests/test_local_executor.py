"""Unit tests for LocalExecutor."""

from __future__ import annotations

from unittest.mock import MagicMock

from core.local_executor import Action, LocalExecutor


def test_execute_skill_alias():
    registry = MagicMock()
    registry.execute.return_value = "NVDA: $120.50, AAPL: $185.20"

    executor = LocalExecutor(registry)
    actions = [{"skill": "realtime_data", "tool": "get_stock_watchlist", "params": {"symbols": ["NVDA"]}}]
    result = executor.execute_skill_alias(actions, "owner")

    assert result.action == Action.REQLLM
    assert "NVDA" in result.text
    registry.execute.assert_called_once_with("get_stock_watchlist", {"symbols": ["NVDA"]}, user_role="owner")
