"""Provider 抽象接口."""

from __future__ import annotations

from abc import ABC, abstractmethod

from realtime_data.models import NewsArticle, StockQuote


class NewsProvider(ABC):
    """新闻数据源抽象."""

    @abstractmethod
    def fetch_news(self, topic: str, limit: int = 5) -> list[NewsArticle]:
        """获取新闻."""


class StockProvider(ABC):
    """股票数据源抽象."""

    @abstractmethod
    def fetch_quotes(self, symbols: list[str]) -> list[StockQuote]:
        """获取股票报价."""
