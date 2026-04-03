"""RealTimeData 核心服务."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime
from typing import Any

from realtime_data.cache import Cache
from realtime_data.models import ALL_TOPICS, NewsDigest, RealTimeSnapshot, StockDigest
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
        self._last_news_stale = False
        self._last_stocks_stale = False


    def get_news(
        self,
        topic: str = "all",
        limit: int = 10,
        force_refresh: bool = False,
    ) -> NewsDigest:
        """获取新闻。topic="all" 时聚合 4 个分类."""
        if topic == "all":
            return self._get_all_news(limit, force_refresh)

        cache_key = f"news_{topic}"
        ttl = self.config.get("cache", {}).get("news_ttl_seconds", 1800)

        if not force_refresh and self.cache:
            cached_data, is_stale = self.cache.get(cache_key, ttl)
            if cached_data and not is_stale:
                self.logger.info("News cache hit: %s", topic)
                return NewsDigest.from_dict(cached_data)

        try:
            articles = self.news_provider.fetch_news(topic, limit)
            digest = NewsDigest(articles=articles, topic=topic, generated_at=datetime.now())

            if self.cache:
                self.cache.set(cache_key, digest.to_dict())

            self._last_news_stale = False
            return digest

        except Exception as e:
            self.logger.error("News fetch failed for %s: %s", topic, e)

            if self.cache:
                cached_data, _ = self.cache.get(cache_key, ttl * 10)
                if cached_data:
                    self.logger.info("Returning stale news cache for %s", topic)
                    self._last_news_stale = True
                    return NewsDigest.from_dict(cached_data)

            return NewsDigest(articles=[], topic=topic)

    def _get_all_news(self, limit: int, force_refresh: bool) -> NewsDigest:
        """聚合 world/ai/technology/business 四个分类."""
        all_articles = []
        for t in ALL_TOPICS:
            digest = self.get_news(t, limit, force_refresh)
            all_articles.extend(replace(a, topic=t) for a in digest.articles)

        return NewsDigest(articles=all_articles, topic="all", generated_at=datetime.now())

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

            self._last_stocks_stale = False
            return digest

        except Exception as e:
            self.logger.error("Stock fetch failed: %s", e)

            if self.cache:
                cached_data, _ = self.cache.get(cache_key, ttl * 10)
                if cached_data:
                    self.logger.info("Returning stale stock cache")
                    self._last_stocks_stale = True
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

        snapshot.is_stale = self._last_news_stale or self._last_stocks_stale
        return snapshot
