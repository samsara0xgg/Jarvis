"""RealTimeData 数据模型."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

ALL_TOPICS = ("world", "ai", "technology", "business")


@dataclass
class NewsArticle:
    """新闻文章."""

    title: str
    source: str
    published_at: datetime
    url: str
    summary: str = ""
    topic: str = ""
    priority_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "source": self.source,
            "published_at": self.published_at.isoformat(),
            "url": self.url,
            "summary": self.summary,
            "topic": self.topic,
            "priority_score": self.priority_score,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NewsArticle:
        return cls(
            title=data["title"],
            source=data["source"],
            published_at=datetime.fromisoformat(data["published_at"]),
            url=data["url"],
            summary=data.get("summary", ""),
            topic=data.get("topic", ""),
            priority_score=data.get("priority_score", 0.0),
        )


@dataclass
class StockQuote:
    """股票报价."""

    symbol: str
    price: float
    change: float
    change_percent: float
    currency: str = "USD"
    as_of: datetime = field(default_factory=datetime.now)
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "price": self.price,
            "change": self.change,
            "change_percent": self.change_percent,
            "currency": self.currency,
            "as_of": self.as_of.isoformat(),
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StockQuote:
        return cls(
            symbol=data["symbol"],
            price=data["price"],
            change=data["change"],
            change_percent=data["change_percent"],
            currency=data.get("currency", "USD"),
            as_of=datetime.fromisoformat(data["as_of"]),
            source=data.get("source", ""),
        )


@dataclass
class NewsDigest:
    """新闻摘要."""

    articles: list[NewsArticle] = field(default_factory=list)
    generated_at: datetime = field(default_factory=datetime.now)
    topic: str = "all"

    def to_dict(self) -> dict[str, Any]:
        return {
            "articles": [a.to_dict() for a in self.articles],
            "generated_at": self.generated_at.isoformat(),
            "topic": self.topic,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NewsDigest:
        return cls(
            articles=[NewsArticle.from_dict(a) for a in data["articles"]],
            generated_at=datetime.fromisoformat(data["generated_at"]),
            topic=data.get("topic", "all"),
        )


@dataclass
class StockDigest:
    """股票摘要."""

    quotes: list[StockQuote] = field(default_factory=list)
    generated_at: datetime = field(default_factory=datetime.now)
    watchlist_name: str = "default"

    def to_dict(self) -> dict[str, Any]:
        return {
            "quotes": [q.to_dict() for q in self.quotes],
            "generated_at": self.generated_at.isoformat(),
            "watchlist_name": self.watchlist_name,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StockDigest:
        return cls(
            quotes=[StockQuote.from_dict(q) for q in data["quotes"]],
            generated_at=datetime.fromisoformat(data["generated_at"]),
            watchlist_name=data.get("watchlist_name", "default"),
        )


@dataclass
class RealTimeSnapshot:
    """完整快照."""

    news_digest: NewsDigest | None = None
    stock_digest: StockDigest | None = None
    generated_at: datetime = field(default_factory=datetime.now)
    is_stale: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "news_digest": self.news_digest.to_dict() if self.news_digest else None,
            "stock_digest": self.stock_digest.to_dict() if self.stock_digest else None,
            "generated_at": self.generated_at.isoformat(),
            "is_stale": self.is_stale,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RealTimeSnapshot:
        return cls(
            news_digest=NewsDigest.from_dict(data["news_digest"]) if data.get("news_digest") else None,
            stock_digest=StockDigest.from_dict(data["stock_digest"]) if data.get("stock_digest") else None,
            generated_at=datetime.fromisoformat(data["generated_at"]),
            is_stale=data.get("is_stale", False),
        )
