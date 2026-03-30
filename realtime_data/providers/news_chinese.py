"""GNews 中文新闻 provider."""

from __future__ import annotations

import logging
import os
from datetime import datetime

import requests

from realtime_data.models import NewsArticle
from realtime_data.providers.base import NewsProvider

LOGGER = logging.getLogger(__name__)


class GNewsChineseProvider(NewsProvider):
    """GNews 中文新闻源."""

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key or os.getenv("GNEWS_API_KEY", "")
        self.base_url = "https://gnews.io/api/v4"

    def fetch_news(self, topic: str, limit: int = 5) -> list[NewsArticle]:
        """获取中文新闻."""
        if not self.api_key:
            LOGGER.error("GNews API key not configured")
            return []

        params = {
            "apikey": self.api_key,
            "lang": "zh",
            "country": "cn",
            "max": limit,
        }

        if topic == "ai":
            params["q"] = "人工智能 OR AI OR 大模型"
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

            LOGGER.info(f"GNews中文: 获取 {len(articles)} 篇 {topic} 新闻")
            return articles

        except Exception as e:
            LOGGER.error(f"GNews中文获取失败 {topic}: {e}")
            return []
