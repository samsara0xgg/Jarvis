"""格式化输出."""

from __future__ import annotations

from datetime import datetime

from realtime_data.models import NewsDigest, RealTimeSnapshot, StockDigest


class Formatter:
    """将结构化数据转为自然语言."""

    @staticmethod
    def format_news_digest(digest: NewsDigest) -> str:
        """格式化新闻摘要."""
        if not digest.articles:
            return f"No {digest.topic} news available."

        lines = [f"{digest.topic.upper()} 新闻:"]
        for i, article in enumerate(digest.articles, 1):
            lines.append(f"{i}. {article.title}")
            lines.append(f"   来源: {article.source}")
            lines.append(f"   链接: {article.url}")
            if article.summary:
                lines.append(f"   摘要: {article.summary[:100]}...")
            lines.append("")

        return "\n".join(lines)

    @staticmethod
    def format_stock_digest(digest: StockDigest) -> str:
        """格式化股票摘要."""
        if not digest.quotes:
            return "No stock quotes available."

        lines = ["Stock Watchlist:"]
        for quote in digest.quotes:
            direction = "up" if quote.change >= 0 else "down"
            lines.append(
                f"• {quote.symbol}: ${quote.price:.2f} "
                f"({direction} {abs(quote.change_percent):.2f}%)"
            )

        return "\n".join(lines)

    @staticmethod
    def format_briefing(snapshot: RealTimeSnapshot) -> str:
        """格式化综合简报."""
        parts = []

        if snapshot.news_digest:
            parts.append(Formatter.format_news_digest(snapshot.news_digest))

        if snapshot.stock_digest:
            parts.append(Formatter.format_stock_digest(snapshot.stock_digest))

        if snapshot.is_stale:
            age = (datetime.now() - snapshot.generated_at).total_seconds() / 60
            parts.append(f"\n(Cached data from {age:.0f} minutes ago)")

        return "\n\n".join(parts) if parts else "No data available."
