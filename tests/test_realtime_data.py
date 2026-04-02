"""Tests for the realtime_data module — models, formatter, service, and providers."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from realtime_data.models import (
    ALL_TOPICS,
    NewsArticle,
    NewsDigest,
    RealTimeSnapshot,
    StockDigest,
    StockQuote,
)
from realtime_data.formatter import Formatter, _group_by_topic
from realtime_data.providers.mock_news import MockNewsProvider
from realtime_data.providers.mock_stocks import MockStockProvider
from realtime_data.service import RealTimeDataService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_article(
    title: str = "Test Article",
    source: str = "TestSource",
    topic: str = "ai",
    summary: str = "A brief summary.",
    hours_ago: int = 0,
) -> NewsArticle:
    return NewsArticle(
        title=title,
        source=source,
        published_at=datetime.now() - timedelta(hours=hours_ago),
        url="https://example.com/article",
        summary=summary,
        topic=topic,
        priority_score=0.8,
    )


def _make_quote(
    symbol: str = "AAPL",
    price: float = 180.0,
    change: float = 2.0,
    change_percent: float = 1.12,
) -> StockQuote:
    return StockQuote(
        symbol=symbol,
        price=price,
        change=change,
        change_percent=change_percent,
        currency="USD",
        as_of=datetime.now(),
        source="mock",
    )


# ===========================================================================
# Priority 1a — Model tests
# ===========================================================================


class TestNewsArticle:
    def test_creation_with_defaults(self):
        article = NewsArticle(
            title="Headline",
            source="Reuters",
            published_at=datetime(2026, 1, 1, 12, 0),
            url="https://example.com",
        )
        assert article.summary == ""
        assert article.topic == ""
        assert article.priority_score == 0.0

    def test_to_dict(self):
        dt = datetime(2026, 3, 15, 10, 30)
        article = NewsArticle(
            title="Test",
            source="AP",
            published_at=dt,
            url="https://example.com/1",
            summary="Short",
            topic="world",
            priority_score=0.9,
        )
        d = article.to_dict()
        assert d["title"] == "Test"
        assert d["source"] == "AP"
        assert d["published_at"] == dt.isoformat()
        assert d["url"] == "https://example.com/1"
        assert d["summary"] == "Short"
        assert d["topic"] == "world"
        assert d["priority_score"] == 0.9

    def test_from_dict_roundtrip(self):
        original = _make_article(title="Roundtrip", topic="technology")
        restored = NewsArticle.from_dict(original.to_dict())
        assert restored.title == original.title
        assert restored.topic == original.topic
        assert restored.source == original.source
        assert restored.priority_score == original.priority_score

    def test_from_dict_missing_optional_fields(self):
        data = {
            "title": "Minimal",
            "source": "BBC",
            "published_at": datetime.now().isoformat(),
            "url": "https://example.com",
        }
        article = NewsArticle.from_dict(data)
        assert article.summary == ""
        assert article.topic == ""
        assert article.priority_score == 0.0


class TestStockQuote:
    def test_creation_with_defaults(self):
        quote = StockQuote(symbol="GOOG", price=140.0, change=1.0, change_percent=0.72)
        assert quote.currency == "USD"
        assert quote.source == ""

    def test_to_dict(self):
        dt = datetime(2026, 4, 1, 9, 30)
        quote = StockQuote(
            symbol="NVDA", price=500.0, change=-3.0, change_percent=-0.6,
            currency="USD", as_of=dt, source="test",
        )
        d = quote.to_dict()
        assert d["symbol"] == "NVDA"
        assert d["price"] == 500.0
        assert d["change"] == -3.0
        assert d["as_of"] == dt.isoformat()

    def test_from_dict_roundtrip(self):
        original = _make_quote(symbol="MSFT", price=420.0, change=5.0, change_percent=1.2)
        restored = StockQuote.from_dict(original.to_dict())
        assert restored.symbol == original.symbol
        assert restored.price == original.price
        assert restored.change_percent == original.change_percent

    def test_from_dict_missing_optional_fields(self):
        data = {
            "symbol": "AAPL",
            "price": 180.0,
            "change": 2.0,
            "change_percent": 1.12,
            "as_of": datetime.now().isoformat(),
        }
        quote = StockQuote.from_dict(data)
        assert quote.currency == "USD"
        assert quote.source == ""


class TestNewsDigest:
    def test_empty_digest(self):
        digest = NewsDigest()
        assert digest.articles == []
        assert digest.topic == "all"

    def test_to_dict_with_articles(self):
        articles = [_make_article(title=f"Art {i}") for i in range(3)]
        digest = NewsDigest(articles=articles, topic="ai")
        d = digest.to_dict()
        assert len(d["articles"]) == 3
        assert d["topic"] == "ai"
        assert "generated_at" in d

    def test_from_dict_roundtrip(self):
        articles = [_make_article(title="RT1"), _make_article(title="RT2")]
        original = NewsDigest(articles=articles, topic="technology")
        restored = NewsDigest.from_dict(original.to_dict())
        assert len(restored.articles) == 2
        assert restored.articles[0].title == "RT1"
        assert restored.topic == "technology"


class TestStockDigest:
    def test_empty_digest(self):
        digest = StockDigest()
        assert digest.quotes == []
        assert digest.watchlist_name == "default"

    def test_to_dict_with_quotes(self):
        quotes = [_make_quote(symbol="A"), _make_quote(symbol="B")]
        digest = StockDigest(quotes=quotes, watchlist_name="tech")
        d = digest.to_dict()
        assert len(d["quotes"]) == 2
        assert d["watchlist_name"] == "tech"

    def test_from_dict_roundtrip(self):
        quotes = [_make_quote(symbol="TSLA", price=250.0, change=-5.0, change_percent=-1.96)]
        original = StockDigest(quotes=quotes)
        restored = StockDigest.from_dict(original.to_dict())
        assert restored.quotes[0].symbol == "TSLA"


class TestRealTimeSnapshot:
    def test_empty_snapshot(self):
        snap = RealTimeSnapshot()
        assert snap.news_digest is None
        assert snap.stock_digest is None
        assert snap.is_stale is False

    def test_to_dict_with_none_digests(self):
        snap = RealTimeSnapshot()
        d = snap.to_dict()
        assert d["news_digest"] is None
        assert d["stock_digest"] is None
        assert d["is_stale"] is False

    def test_to_dict_with_digests(self):
        news = NewsDigest(articles=[_make_article()], topic="all")
        stocks = StockDigest(quotes=[_make_quote()])
        snap = RealTimeSnapshot(news_digest=news, stock_digest=stocks)
        d = snap.to_dict()
        assert d["news_digest"] is not None
        assert len(d["news_digest"]["articles"]) == 1
        assert d["stock_digest"] is not None
        assert len(d["stock_digest"]["quotes"]) == 1

    def test_from_dict_roundtrip(self):
        news = NewsDigest(articles=[_make_article()], topic="all")
        stocks = StockDigest(quotes=[_make_quote()])
        original = RealTimeSnapshot(news_digest=news, stock_digest=stocks, is_stale=True)
        restored = RealTimeSnapshot.from_dict(original.to_dict())
        assert restored.is_stale is True
        assert len(restored.news_digest.articles) == 1
        assert restored.stock_digest.quotes[0].symbol == "AAPL"

    def test_from_dict_with_none_digests(self):
        data = {
            "news_digest": None,
            "stock_digest": None,
            "generated_at": datetime.now().isoformat(),
            "is_stale": False,
        }
        snap = RealTimeSnapshot.from_dict(data)
        assert snap.news_digest is None
        assert snap.stock_digest is None


# ===========================================================================
# Priority 1b — Formatter tests
# ===========================================================================


class TestGroupByTopic:
    def test_groups_articles_correctly(self):
        articles = [
            _make_article(title="AI News", topic="ai"),
            _make_article(title="World News", topic="world"),
            _make_article(title="AI News 2", topic="ai"),
        ]
        digest = NewsDigest(articles=articles)
        grouped = _group_by_topic(digest)
        assert len(grouped["ai"]) == 2
        assert len(grouped["world"]) == 1

    def test_empty_articles(self):
        digest = NewsDigest(articles=[])
        grouped = _group_by_topic(digest)
        assert grouped == {}

    def test_missing_topic_defaults_to_other(self):
        article = _make_article(title="No Topic")
        article.topic = ""
        digest = NewsDigest(articles=[article])
        grouped = _group_by_topic(digest)
        assert "other" in grouped
        assert len(grouped["other"]) == 1


class TestFormatterNewsDigest:
    def test_empty_articles_returns_message(self):
        digest = NewsDigest(articles=[], topic="ai")
        result = Formatter.format_news_digest(digest)
        assert "AI" in result
        assert "暂无" in result

    def test_empty_all_topic_returns_message(self):
        digest = NewsDigest(articles=[], topic="all")
        result = Formatter.format_news_digest(digest)
        assert "暂无" in result

    def test_single_topic_format(self):
        articles = [_make_article(title="Headline 1", source="Reuters", topic="world")]
        digest = NewsDigest(articles=articles, topic="world")
        result = Formatter.format_news_digest(digest)
        assert "国际" in result
        assert "Headline 1" in result
        assert "Reuters" in result
        assert "链接" in result

    def test_summary_truncation(self):
        long_summary = "X" * 200
        articles = [_make_article(title="Long", summary=long_summary, topic="ai")]
        digest = NewsDigest(articles=articles, topic="ai")
        result = Formatter.format_news_digest(digest)
        # Summary should be truncated to 120 chars
        assert "X" * 120 in result
        assert "X" * 121 not in result

    def test_all_topic_groups_by_category(self):
        articles = [
            _make_article(title="World A", topic="world"),
            _make_article(title="AI A", topic="ai"),
            _make_article(title="Tech A", topic="technology"),
        ]
        digest = NewsDigest(articles=articles, topic="all")
        result = Formatter.format_news_digest(digest)
        assert "国际" in result
        assert "AI" in result
        assert "科技" in result


class TestFormatterStockDigest:
    def test_empty_quotes_returns_message(self):
        digest = StockDigest(quotes=[])
        result = Formatter.format_stock_digest(digest)
        assert "暂无" in result

    def test_positive_change(self):
        quotes = [_make_quote(symbol="AAPL", price=180.0, change=2.5, change_percent=1.41)]
        digest = StockDigest(quotes=quotes)
        result = Formatter.format_stock_digest(digest)
        assert "AAPL" in result
        assert "180.00" in result
        assert "1.41%" in result

    def test_negative_change_arrow(self):
        quotes = [_make_quote(symbol="NVDA", price=480.0, change=-5.0, change_percent=-1.03)]
        digest = StockDigest(quotes=quotes)
        result = Formatter.format_stock_digest(digest)
        assert "NVDA" in result


class TestFormatterBriefing:
    def test_empty_snapshot(self):
        snap = RealTimeSnapshot()
        result = Formatter.format_briefing(snap)
        assert "暂无" in result

    def test_with_news_and_stocks(self):
        news = NewsDigest(articles=[_make_article(topic="ai")], topic="ai")
        stocks = StockDigest(quotes=[_make_quote()])
        snap = RealTimeSnapshot(news_digest=news, stock_digest=stocks)
        result = Formatter.format_briefing(snap)
        assert "AI" in result
        assert "AAPL" in result

    def test_stale_cache_message(self):
        snap = RealTimeSnapshot(
            generated_at=datetime.now() - timedelta(minutes=15),
            is_stale=True,
        )
        result = Formatter.format_briefing(snap)
        assert "缓存" in result
        assert "分钟" in result


class TestFormatterVoice:
    def test_empty_news_voice(self):
        digest = NewsDigest(articles=[], topic="ai")
        result = Formatter.format_news_voice(digest)
        assert "暂时没有新闻" in result

    def test_single_topic_voice(self):
        articles = [_make_article(title="Story One", topic="ai")]
        digest = NewsDigest(articles=articles, topic="ai")
        result = Formatter.format_news_voice(digest)
        assert "AI" in result
        assert "Story One" in result

    def test_voice_max_items_limit(self):
        articles = [_make_article(title=f"Story {i}", topic="world") for i in range(10)]
        digest = NewsDigest(articles=articles, topic="world")
        result = Formatter.format_news_voice(digest, max_items=2)
        assert "Story 0" in result
        assert "Story 1" in result
        assert "Story 2" not in result

    def test_all_topic_voice_groups(self):
        articles = [
            _make_article(title="World News", topic="world"),
            _make_article(title="AI News", topic="ai"),
        ]
        digest = NewsDigest(articles=articles, topic="all")
        result = Formatter.format_news_voice(digest)
        assert "国际" in result
        assert "AI" in result

    def test_all_topic_voice_empty(self):
        digest = NewsDigest(articles=[], topic="all")
        result = Formatter.format_news_voice(digest)
        assert "暂时没有新闻" in result

    def test_empty_stock_voice(self):
        digest = StockDigest(quotes=[])
        result = Formatter.format_stock_voice(digest)
        assert "暂时没有" in result

    def test_stock_voice_positive(self):
        quotes = [_make_quote(symbol="AAPL", price=180.0, change=2.0, change_percent=1.1)]
        digest = StockDigest(quotes=quotes)
        result = Formatter.format_stock_voice(digest)
        assert "AAPL" in result
        assert "涨" in result

    def test_stock_voice_negative(self):
        quotes = [_make_quote(symbol="NVDA", price=480.0, change=-5.0, change_percent=-1.0)]
        digest = StockDigest(quotes=quotes)
        result = Formatter.format_stock_voice(digest)
        assert "NVDA" in result
        assert "跌" in result

    def test_briefing_voice_empty(self):
        snap = RealTimeSnapshot()
        result = Formatter.format_briefing_voice(snap)
        assert "暂无" in result

    def test_briefing_voice_with_data(self):
        news = NewsDigest(articles=[_make_article(title="Big Event", topic="world")], topic="world")
        stocks = StockDigest(quotes=[_make_quote()])
        snap = RealTimeSnapshot(news_digest=news, stock_digest=stocks)
        result = Formatter.format_briefing_voice(snap)
        assert "Big Event" in result
        assert "AAPL" in result


# ===========================================================================
# Priority 1c — Mock providers tests
# ===========================================================================


class TestMockNewsProvider:
    def test_fetch_known_topics(self):
        provider = MockNewsProvider()
        for topic in ALL_TOPICS:
            articles = provider.fetch_news(topic, limit=5)
            assert len(articles) > 0
            assert all(isinstance(a, NewsArticle) for a in articles)
            assert all(a.topic == topic for a in articles)

    def test_fetch_respects_limit(self):
        provider = MockNewsProvider()
        articles = provider.fetch_news("ai", limit=1)
        assert len(articles) == 1

    def test_fetch_unknown_topic_defaults_to_world(self):
        provider = MockNewsProvider()
        articles = provider.fetch_news("nonexistent", limit=3)
        assert len(articles) > 0

    def test_articles_have_decreasing_time(self):
        provider = MockNewsProvider()
        articles = provider.fetch_news("ai", limit=3)
        for i in range(len(articles) - 1):
            assert articles[i].published_at >= articles[i + 1].published_at

    def test_articles_have_decreasing_priority(self):
        provider = MockNewsProvider()
        articles = provider.fetch_news("world", limit=3)
        for i in range(len(articles) - 1):
            assert articles[i].priority_score >= articles[i + 1].priority_score


class TestMockStockProvider:
    def test_fetch_known_symbols(self):
        provider = MockStockProvider()
        quotes = provider.fetch_quotes(["AAPL", "NVDA"])
        assert len(quotes) == 2
        symbols = {q.symbol for q in quotes}
        assert symbols == {"AAPL", "NVDA"}

    def test_fetch_unknown_symbol_skipped(self):
        provider = MockStockProvider()
        quotes = provider.fetch_quotes(["AAPL", "UNKNOWN"])
        assert len(quotes) == 1
        assert quotes[0].symbol == "AAPL"

    def test_fetch_empty_list(self):
        provider = MockStockProvider()
        quotes = provider.fetch_quotes([])
        assert quotes == []

    def test_quotes_have_source_mock(self):
        provider = MockStockProvider()
        quotes = provider.fetch_quotes(["MSFT"])
        assert quotes[0].source == "mock"


# ===========================================================================
# Priority 1d — Service tests
# ===========================================================================


class TestRealTimeDataService:
    @staticmethod
    def _make_service(cache=None, config_override=None):
        config = config_override or {"cache": {"news_ttl_seconds": 1800, "stocks_ttl_seconds": 600}}
        return RealTimeDataService(
            config=config,
            news_provider=MockNewsProvider(),
            stock_provider=MockStockProvider(),
            cache=cache,
        )

    def test_get_news_single_topic(self):
        svc = self._make_service()
        digest = svc.get_news("ai", limit=3)
        assert digest.topic == "ai"
        assert len(digest.articles) > 0
        assert all(a.topic == "ai" for a in digest.articles)

    def test_get_news_all_topics(self):
        svc = self._make_service()
        digest = svc.get_news("all", limit=5)
        assert digest.topic == "all"
        topics_present = {a.topic for a in digest.articles}
        assert topics_present == set(ALL_TOPICS)

    def test_get_stocks_default_watchlist(self):
        config = {
            "cache": {},
            "stocks": {"watchlist": ["AAPL", "MSFT"]},
        }
        svc = self._make_service(config_override=config)
        digest = svc.get_stocks()
        symbols = {q.symbol for q in digest.quotes}
        assert symbols == {"AAPL", "MSFT"}

    def test_get_stocks_explicit_symbols(self):
        svc = self._make_service()
        digest = svc.get_stocks(symbols=["NVDA", "GOOGL"])
        symbols = {q.symbol for q in digest.quotes}
        assert symbols == {"NVDA", "GOOGL"}

    def test_get_briefing_includes_both(self):
        config = {
            "cache": {},
            "stocks": {"watchlist": ["AAPL"]},
        }
        svc = self._make_service(config_override=config)
        snap = svc.get_briefing()
        assert snap.news_digest is not None
        assert snap.stock_digest is not None

    def test_get_briefing_news_only(self):
        svc = self._make_service()
        snap = svc.get_briefing(include_stocks=False)
        assert snap.news_digest is not None
        assert snap.stock_digest is None

    def test_get_briefing_stocks_only(self):
        config = {
            "cache": {},
            "stocks": {"watchlist": ["AAPL"]},
        }
        svc = self._make_service(config_override=config)
        snap = svc.get_briefing(include_news=False)
        assert snap.news_digest is None
        assert snap.stock_digest is not None


class TestServiceCaching:
    @staticmethod
    def _make_service_with_cache():
        mock_cache = MagicMock()
        config = {
            "cache": {"news_ttl_seconds": 1800, "stocks_ttl_seconds": 600},
            "stocks": {"watchlist": ["AAPL"]},
        }
        svc = RealTimeDataService(
            config=config,
            news_provider=MockNewsProvider(),
            stock_provider=MockStockProvider(),
            cache=mock_cache,
        )
        return svc, mock_cache

    def test_news_cache_hit(self):
        svc, mock_cache = self._make_service_with_cache()
        cached_digest = NewsDigest(
            articles=[_make_article(title="Cached")],
            topic="ai",
        )
        mock_cache.get.return_value = (cached_digest.to_dict(), False)

        result = svc.get_news("ai")
        assert result.articles[0].title == "Cached"
        mock_cache.get.assert_called_once()

    def test_news_cache_miss_stores_result(self):
        svc, mock_cache = self._make_service_with_cache()
        mock_cache.get.return_value = (None, False)

        result = svc.get_news("ai", limit=2)
        assert len(result.articles) > 0
        mock_cache.set.assert_called_once()

    def test_news_force_refresh_skips_cache(self):
        svc, mock_cache = self._make_service_with_cache()

        result = svc.get_news("ai", force_refresh=True)
        assert len(result.articles) > 0
        # get should not be called when force_refresh=True
        mock_cache.get.assert_not_called()
        mock_cache.set.assert_called_once()

    def test_news_fetch_error_returns_stale_cache(self):
        mock_cache = MagicMock()
        config = {"cache": {"news_ttl_seconds": 1800}}
        failing_provider = MagicMock()
        failing_provider.fetch_news.side_effect = RuntimeError("API down")

        svc = RealTimeDataService(
            config=config,
            news_provider=failing_provider,
            stock_provider=MockStockProvider(),
            cache=mock_cache,
        )
        # First call to get (within normal ttl) returns None (no cache)
        # Second call (stale lookup with ttl*10) returns stale data
        stale_data = NewsDigest(articles=[_make_article(title="Stale")], topic="ai").to_dict()
        mock_cache.get.side_effect = [(None, False), (stale_data, True)]

        result = svc.get_news("ai")
        assert result.articles[0].title == "Stale"

    def test_news_fetch_error_no_cache_returns_empty(self):
        config = {"cache": {"news_ttl_seconds": 1800}}
        failing_provider = MagicMock()
        failing_provider.fetch_news.side_effect = RuntimeError("API down")

        svc = RealTimeDataService(
            config=config,
            news_provider=failing_provider,
            stock_provider=MockStockProvider(),
            cache=None,
        )
        result = svc.get_news("ai")
        assert result.articles == []
        assert result.topic == "ai"

    def test_stock_cache_hit(self):
        svc, mock_cache = self._make_service_with_cache()
        cached_digest = StockDigest(quotes=[_make_quote(symbol="CACHED")])
        mock_cache.get.return_value = (cached_digest.to_dict(), False)

        result = svc.get_stocks(symbols=["AAPL"])
        assert result.quotes[0].symbol == "CACHED"

    def test_stock_force_refresh_skips_cache(self):
        svc, mock_cache = self._make_service_with_cache()

        result = svc.get_stocks(symbols=["AAPL"], force_refresh=True)
        assert len(result.quotes) > 0
        mock_cache.get.assert_not_called()
        mock_cache.set.assert_called_once()

    def test_stock_fetch_error_returns_stale_cache(self):
        mock_cache = MagicMock()
        config = {"cache": {"stocks_ttl_seconds": 600}, "stocks": {"watchlist": ["AAPL"]}}
        failing_provider = MagicMock()
        failing_provider.fetch_quotes.side_effect = RuntimeError("API down")

        svc = RealTimeDataService(
            config=config,
            news_provider=MockNewsProvider(),
            stock_provider=failing_provider,
            cache=mock_cache,
        )
        stale_data = StockDigest(quotes=[_make_quote(symbol="STALE")]).to_dict()
        mock_cache.get.side_effect = [(None, False), (stale_data, True)]

        result = svc.get_stocks(symbols=["AAPL"])
        assert result.quotes[0].symbol == "STALE"

    def test_stock_fetch_error_no_cache_returns_empty(self):
        config = {"cache": {"stocks_ttl_seconds": 600}}
        failing_provider = MagicMock()
        failing_provider.fetch_quotes.side_effect = RuntimeError("API down")

        svc = RealTimeDataService(
            config=config,
            news_provider=MockNewsProvider(),
            stock_provider=failing_provider,
            cache=None,
        )
        result = svc.get_stocks(symbols=["AAPL"])
        assert result.quotes == []
