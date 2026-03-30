"""Mock 新闻 provider."""

from __future__ import annotations

from datetime import datetime, timedelta

from realtime_data.models import NewsArticle
from realtime_data.providers.base import NewsProvider


class MockNewsProvider(NewsProvider):
    """Mock 新闻数据源."""

    def fetch_news(self, topic: str, limit: int = 5) -> list[NewsArticle]:
        base_time = datetime.now()

        templates = {
            "world": [
                ("Global Summit Addresses Climate Action", "Reuters", "Major world leaders convene..."),
                ("Trade Agreement Signed Between Nations", "AP", "Historic trade deal finalized..."),
                ("International Tensions Ease After Talks", "BBC", "Diplomatic breakthrough achieved..."),
            ],
            "ai": [
                ("New LLM Breakthrough Announced", "TechCrunch", "Researchers unveil advanced model..."),
                ("AI Safety Framework Proposed", "VentureBeat", "Industry leaders collaborate on guidelines..."),
                ("Generative AI Adoption Surges", "The Verge", "Enterprise usage grows rapidly..."),
            ],
            "technology": [
                ("Quantum Computing Milestone Reached", "Wired", "New quantum processor demonstrated..."),
                ("5G Rollout Expands Globally", "CNET", "Network coverage increases..."),
                ("Chip Shortage Eases", "ArsTechnica", "Supply chain improvements noted..."),
            ],
            "business": [
                ("Markets Rally on Economic Data", "Bloomberg", "Positive indicators drive gains..."),
                ("Tech Earnings Beat Expectations", "CNBC", "Major companies report strong results..."),
                ("Merger Announced in Finance Sector", "WSJ", "Two major banks to combine..."),
            ],
        }

        articles = []
        topic_templates = templates.get(topic, templates["world"])

        for i, (title, source, summary) in enumerate(topic_templates[:limit]):
            articles.append(NewsArticle(
                title=title,
                source=source,
                published_at=base_time - timedelta(hours=i),
                url=f"https://example.com/{topic}/{i}",
                summary=summary,
                topic=topic,
                priority_score=1.0 - (i * 0.1),
            ))

        return articles
