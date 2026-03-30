"""GNews API provider."""

from __future__ import annotations

import logging
import os
from datetime import datetime

import requests

from realtime_data.models import NewsArticle
from realtime_data.providers.base import NewsProvider

LOGGER = logging.getLogger(__name__)


class GNewsProvider(NewsProvider):
    """GNews API 新闻源."""

    def __init__(self, api_key: str, language: str = "en", country: str = "us") -> None:
        self.api_key = api_key or os.getenv("GNEWS_API_KEY", "")
        self.language = language
        self.country = country
        self.base_url = "https://gnews.io/api/v4"

    def fetch_news(self, topic: str, limit: int = 5) -> list[NewsArticle]:
        """获取新闻."""
        if not self.api_key:
            LOGGER.error("GNews API key not configured")
            return []

        params = {
            "apikey": self.api_key,
            "lang": self.language,
            "country": self.country,
            "max": limit,
        }

        if topic == "ai":
            params["q"] = "AI OR artificial intelligence OR LLM"
            endpoint = f"{self.base_url}/search"
        elif topic in ["technology", "business"]:
            params["topic"] = topic
            endpoint = f"{self.base_url}/top-headlines"
        else:
            params["topic"] = "world"
            endpoint = f"{self.base_url}/top-headlines"

        try:
            resp = requests.get(endpoint, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            articles = []
            for item in data.get("articles", [])[:limit]:
                articles.append(NewsArticle(
                    title=item["title"],
                    source=item["source"]["name"],
                    published_at=datetime.fromisoformat(item["publishedAt"].replace("Z", "+00:00")),
                    url=item["url"],
                    summary=item.get("description", ""),
                    topic=topic,
                ))

            LOGGER.info(f"GNews: fetched {len(articles)} articles for {topic}")
            return articles

        except Exception as e:
            LOGGER.error(f"GNews fetch failed for {topic}: {e}")
            return []
