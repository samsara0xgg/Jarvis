"""RealTimeData Jarvis Skill 适配器."""

from __future__ import annotations

import logging
from typing import Any

from realtime_data.cache import Cache
from realtime_data.formatter import Formatter
from realtime_data.providers.mock_news import MockNewsProvider
from realtime_data.providers.mock_stocks import MockStockProvider
from realtime_data.providers.news import GNewsProvider
from realtime_data.providers.yahoo_stocks import YahooFinanceProvider
from realtime_data.service import RealTimeDataService
from skills import Skill

LOGGER = logging.getLogger(__name__)


class RealTimeDataSkill(Skill):
    """实时信息技能（新闻/股票）."""

    def __init__(self, config: dict) -> None:
        self.config = config.get("skills", {}).get("realtime_data", {})

        cache_dir = self.config.get("cache", {}).get("dir", "data/realtime_data")
        self.cache = Cache(cache_dir)

        use_mock = self.config.get("mock_mode", False)

        if use_mock:
            news_provider = MockNewsProvider()
            stock_provider = MockStockProvider()
        else:
            news_cfg = self.config.get("news", {})
            stock_cfg = self.config.get("stocks", {})

            language = news_cfg.get("language", "en")
            country = news_cfg.get("country", "cn" if language == "zh" else "us")
            news_provider = GNewsProvider(
                api_key=news_cfg.get("api_key", ""),
                language=language,
                country=country,
            )

            provider = stock_cfg.get("provider", "yahoo")
            if provider == "yahoo":
                stock_provider = YahooFinanceProvider()
            else:
                from realtime_data.providers.stocks import AlphaVantageProvider
                stock_provider = AlphaVantageProvider(
                    api_key=stock_cfg.get("api_key", ""),
                )

        self.service = RealTimeDataService(self.config, news_provider, stock_provider, self.cache)
        self.formatter = Formatter()
        self.logger = LOGGER
        self._watchlist = self.config.get("stocks", {}).get("watchlist", [])

    def set_scheduler(self, scheduler: Any) -> None:
        """注册到 Jarvis scheduler，启动后台定时刷新."""
        sched_cfg = self.config.get("scheduler", {})
        if not sched_cfg.get("enabled", False):
            return

        news_minutes = int(sched_cfg.get("refresh_news_minutes", 30))
        stocks_minutes = int(sched_cfg.get("refresh_stocks_minutes", 15))

        try:
            scheduler.add_interval_job(
                job_id="realtime_data_refresh_news",
                func=self._refresh_news_job,
                minutes=news_minutes,
            )
            scheduler.add_interval_job(
                job_id="realtime_data_refresh_stocks",
                func=self._refresh_stocks_job,
                minutes=stocks_minutes,
            )
            self.logger.info(
                "RealTimeData: news refresh every %d min, stocks every %d min",
                news_minutes, stocks_minutes,
            )
        except Exception as exc:
            self.logger.warning("RealTimeData scheduler setup failed: %s", exc)

    def _refresh_news_job(self) -> None:
        """后台定时刷新新闻缓存."""
        self.logger.info("RealTimeData: scheduled news refresh...")
        self.service.get_news("all", force_refresh=True)

    def _refresh_stocks_job(self) -> None:
        """后台定时刷新股票缓存."""
        self.logger.info("RealTimeData: scheduled stocks refresh...")
        self.service.get_stocks(self._watchlist, force_refresh=True)

    def get_briefing_text(self) -> str:
        """供 morning briefing 调用."""
        snapshot = self.service.get_briefing()
        return self.formatter.format_briefing(snapshot)

    @property
    def skill_name(self) -> str:
        return "realtime_data"

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "get_news_briefing",
                "description": "Get real-time news briefing covering world, AI, technology, or business news.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "focus": {
                            "type": "string",
                            "enum": ["world", "ai", "technology", "business", "all"],
                            "description": "News category to focus on. Default: all",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of articles. Default: 5",
                        },
                        "force_refresh": {
                            "type": "boolean",
                            "description": "Force cache refresh. Default: false",
                        },
                    },
                },
            },
            {
                "name": "get_stock_watchlist",
                "description": "Get current stock prices and changes for watchlist symbols.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "symbols": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Stock symbols (e.g., AAPL, NVDA). Uses config watchlist if omitted.",
                        },
                        "force_refresh": {
                            "type": "boolean",
                            "description": "Force cache refresh. Default: false",
                        },
                    },
                },
            },
            {
                "name": "get_realtime_data_briefing",
                "description": "Get combined briefing with news and stock updates.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "include_news": {
                            "type": "boolean",
                            "description": "Include news. Default: true",
                        },
                        "include_stocks": {
                            "type": "boolean",
                            "description": "Include stocks. Default: true",
                        },
                        "force_refresh": {
                            "type": "boolean",
                            "description": "Force cache refresh. Default: false",
                        },
                    },
                },
            },
        ]

    def execute(self, tool_name: str, tool_input: dict[str, Any], **context: Any) -> str:
        try:
            if tool_name == "get_news_briefing":
                focus = tool_input.get("focus", "all")
                limit = tool_input.get("limit", 5)
                force_refresh = tool_input.get("force_refresh", False)

                digest = self.service.get_news(focus, limit, force_refresh)
                return self.formatter.format_news_digest(digest)

            elif tool_name == "get_stock_watchlist":
                symbols = tool_input.get("symbols")
                force_refresh = tool_input.get("force_refresh", False)

                digest = self.service.get_stocks(symbols, force_refresh)
                return self.formatter.format_stock_digest(digest)

            elif tool_name == "get_realtime_data_briefing":
                include_news = tool_input.get("include_news", True)
                include_stocks = tool_input.get("include_stocks", True)
                force_refresh = tool_input.get("force_refresh", False)

                snapshot = self.service.get_briefing(include_news, include_stocks, force_refresh)
                return self.formatter.format_briefing(snapshot)

            else:
                return f"Unknown tool: {tool_name}"

        except Exception as e:
            self.logger.exception("RealTimeData tool %s failed", tool_name)
            return f"Error: {e}"
