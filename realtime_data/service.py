"""RealTimeData 核心服务."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from realtime_data.cache import Cache
from realtime_data.models import NewsDigest, RealTimeSnapshot, StockDigest
from realtime_data.providers.base import NewsProvider, StockProvider

LOGGER = logging.getLogger(__name__)


class RealTimeDataService:
    """核心编排服务."""

    def __init__(
        self,
        config: dict[str, Any],
        news_provider: NewsProvider,
        stock_provider: StockProvider,
        cache: Cache | None = None,
    ) -> None:
        self.config = config
        self.news_provider = news_provider
        self.stock_provider = stock_provider
        self.cache = cache
        self.logger = LOGGER

    def get_news(
        self,
        topic: str = "all",
        limit: int = 5,
        force_refresh: bool = False,
    ) -> NewsDigest:
        """获取新闻."""
        cache_key = f"news_{topic}"
        ttl = self.config.get("cache", {}).get("news_ttl_seconds", 1800)

        if not force_refresh and self.cache:
            cached_data, is_stale = self.cache.get(cache_key, ttl)
            if cached_data and not is_stale:
                self.logger.info(f"News cache hit: {topic}")
                return NewsDigest.from_dict(cached_data)

        try:
            articles = self.news_provider.fetch_news(topic, limit)
            digest = NewsDigest(articles=articles, topic=topic, generated_at=datetime.now())

            if self.cache:
                self.cache.set(cache_key, digest.to_dict())

            return digest

        except Exception as e:
            self.logger.error(f"News fetch failed for {topic}: {e}")

            if self.cache:
                cached_data, _ = self.cache.get(cache_key, ttl * 10)
                if cached_data:
                    self.logger.info(f"Returning stale news cache for {topic}")
                    return NewsDigest.from_dict(cached_data)

            return NewsDigest(articles=[], topic=topic)

    def get_stocks(
        self,
        symbols: list[str] | None = None,
        force_refresh: bool = False,
    ) -> StockDigest:
        """获取股票."""
        if symbols is None:
            symbols = self.config.get("stocks", {}).get("watchlist", [])

        cache_key = "stocks_" + "_".join(sorted(symbols))
        ttl = self.config.get("cache", {}).get("stocks_ttl_seconds", 600)

        if not force_refresh and self.cache:
            cached_data, is_stale = self.cache.get(cache_key, ttl)
            if cached_data and not is_stale:
                self.logger.info("Stock cache hit")
                return StockDigest.from_dict(cached_data)

        try:
            quotes = self.stock_provider.fetch_quotes(symbols)
            digest = StockDigest(quotes=quotes, generated_at=datetime.now())

            if self.cache:
                self.cache.set(cache_key, digest.to_dict())

            return digest

        except Exception as e:
            self.logger.error(f"Stock fetch failed: {e}")

            if self.cache:
                cached_data, _ = self.cache.get(cache_key, ttl * 10)
                if cached_data:
                    self.logger.info("Returning stale stock cache")
                    return StockDigest.from_dict(cached_data)

            return StockDigest(quotes=[])

    def get_briefing(
        self,
        include_news: bool = True,
        include_stocks: bool = True,
        force_refresh: bool = False,
    ) -> RealTimeSnapshot:
        """获取综合简报."""
        snapshot = RealTimeSnapshot(generated_at=datetime.now())

        if include_news:
            snapshot.news_digest = self.get_news("all", force_refresh=force_refresh)

        if include_stocks:
            snapshot.stock_digest = self.get_stocks(force_refresh=force_refresh)

        return snapshot
