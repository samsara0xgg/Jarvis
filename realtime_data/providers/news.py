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
            if self.language == "zh":
                params["q"] = "人工智能 OR AI OR 大模型"
            else:
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
                source = item.get("source") or {}
                published = item.get("publishedAt", "")
                try:
                    pub_dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    pub_dt = datetime.now()
                articles.append(NewsArticle(
                    title=item.get("title", ""),
                    source=source.get("name", "unknown"),
                    published_at=pub_dt,
                    url=item.get("url", ""),
                    summary=item.get("description", ""),
                    topic=topic,
                ))

            LOGGER.info("GNews: fetched %d articles for %s", len(articles), topic)
            return articles

        except Exception as e:
            LOGGER.error("GNews fetch failed for %s: %s", topic, e)
            return []
