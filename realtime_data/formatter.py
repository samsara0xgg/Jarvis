"""格式化输出 — CLI 详细版 + Jarvis 语音精简版."""

from __future__ import annotations

from datetime import datetime

from realtime_data.models import ALL_TOPICS, NewsDigest, RealTimeSnapshot, StockDigest

# topic 中文映射
_TOPIC_NAMES = {
    "world": "国际",
    "ai": "AI",
    "technology": "科技",
    "business": "财经",
    "all": "综合",
}


def _group_by_topic(digest: NewsDigest) -> dict[str, list]:
    """按 topic 字段分组文章."""
    grouped: dict[str, list] = {}
    for article in digest.articles:
        grouped.setdefault(article.topic or "other", []).append(article)
    return grouped


class Formatter:
    """将结构化数据转为自然语言."""

    # --- CLI 详细版（标题+来源+链接+摘要）---

    @staticmethod
    def format_news_digest(digest: NewsDigest) -> str:
        """CLI 详细版新闻."""
        if not digest.articles:
            return f"暂无{_TOPIC_NAMES.get(digest.topic, digest.topic)}新闻。"

        if digest.topic == "all":
            return Formatter._format_all_news_detailed(digest)

        topic_name = _TOPIC_NAMES.get(digest.topic, digest.topic)
        lines = [f"📰 {topic_name}新闻:"]
        for i, article in enumerate(digest.articles, 1):
            lines.append(f"{i}. {article.title}")
            lines.append(f"   来源: {article.source}")
            lines.append(f"   链接: {article.url}")
            if article.summary:
                lines.append(f"   摘要: {article.summary[:120]}")
            lines.append("")

        return "\n".join(lines)

    @staticmethod
    def _format_all_news_detailed(digest: NewsDigest) -> str:
        """CLI 详细版 — 按分类分组显示."""
        grouped = _group_by_topic(digest)
        parts = []
        for topic in ALL_TOPICS:
            articles = grouped.get(topic, [])
            if articles:
                sub = NewsDigest(articles=articles, topic=topic)
                parts.append(Formatter.format_news_digest(sub))
        return "\n".join(parts) if parts else "暂无新闻。"

    @staticmethod
    def format_stock_digest(digest: StockDigest) -> str:
        """CLI 详细版股票."""
        if not digest.quotes:
            return "暂无股票数据。"

        lines = ["📈 股票行情:"]
        for quote in digest.quotes:
            arrow = "↑" if quote.change >= 0 else "↓"
            lines.append(
                f"  {quote.symbol}: ${quote.price:.2f} "
                f"{arrow} {abs(quote.change_percent):.2f}%"
            )

        return "\n".join(lines)

    @staticmethod
    def format_briefing(snapshot: RealTimeSnapshot) -> str:
        """CLI 详细版综合简报."""
        parts = []

        if snapshot.news_digest:
            parts.append(Formatter.format_news_digest(snapshot.news_digest))

        if snapshot.stock_digest:
            parts.append(Formatter.format_stock_digest(snapshot.stock_digest))

        if snapshot.is_stale:
            age = (datetime.now() - snapshot.generated_at).total_seconds() / 60
            parts.append(f"(缓存数据，{age:.0f} 分钟前)")

        return "\n\n".join(parts) if parts else "暂无数据。"

    # --- Jarvis 语音精简版（只报标题）---

    @staticmethod
    def format_news_voice(digest: NewsDigest, max_items: int = 3) -> str:
        """语音精简版新闻 — 只报标题."""
        if not digest.articles:
            return "暂时没有新闻。"

        if digest.topic == "all":
            return Formatter._format_all_news_voice(digest, max_items)

        topic_name = _TOPIC_NAMES.get(digest.topic, digest.topic)
        titles = [a.title for a in digest.articles[:max_items]]
        joined = "；".join(titles)
        return f"{topic_name}新闻：{joined}。"

    @staticmethod
    def _format_all_news_voice(digest: NewsDigest, max_per_topic: int = 2) -> str:
        """语音精简版 — 每个分类报 2 条标题."""
        grouped = _group_by_topic(digest)
        parts = []
        for topic in ALL_TOPICS:
            articles = grouped.get(topic, [])
            if not articles:
                continue
            topic_name = _TOPIC_NAMES.get(topic, topic)
            titles = [a.title for a in articles[:max_per_topic]]
            parts.append(f"{topic_name}：{'；'.join(titles)}")

        return "。".join(parts) + "。" if parts else "暂时没有新闻。"

    @staticmethod
    def format_stock_voice(digest: StockDigest) -> str:
        """语音精简版股票."""
        if not digest.quotes:
            return "暂时没有股票数据。"

        parts = []
        for q in digest.quotes:
            direction = "涨" if q.change >= 0 else "跌"
            parts.append(f"{q.symbol} {q.price:.0f}美元，{direction}{abs(q.change_percent):.1f}%")

        return "股票行情：" + "，".join(parts) + "。"

    @staticmethod
    def format_briefing_voice(snapshot: RealTimeSnapshot) -> str:
        """语音精简版综合简报."""
        parts = []

        if snapshot.news_digest:
            parts.append(Formatter.format_news_voice(snapshot.news_digest))

        if snapshot.stock_digest:
            parts.append(Formatter.format_stock_voice(snapshot.stock_digest))

        return " ".join(parts) if parts else "暂无数据。"
