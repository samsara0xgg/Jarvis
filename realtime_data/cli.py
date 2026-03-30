"""独立 CLI 入口."""

from __future__ import annotations

import argparse
import logging
import sys

from realtime_data.cache import Cache
from realtime_data.formatter import Formatter
from realtime_data.providers.mock_news import MockNewsProvider
from realtime_data.providers.mock_stocks import MockStockProvider
from realtime_data.providers.news import GNewsProvider
from realtime_data.providers.yahoo_stocks import YahooFinanceProvider
from realtime_data.service import RealTimeDataService

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def main() -> None:
    parser = argparse.ArgumentParser(description="RealTimeData CLI")
    parser.add_argument("command", choices=["news", "stocks", "briefing"], help="Command to run")
    parser.add_argument("--mock", action="store_true", help="Use mock providers")
    parser.add_argument("--topic", default="world", help="News topic (world/ai/technology/business)")
    parser.add_argument("--symbols", nargs="+", help="Stock symbols")
    parser.add_argument("--force-refresh", action="store_true", help="Force cache refresh")
    parser.add_argument("--cache-dir", default="data/realtime_data", help="Cache directory")

    args = parser.parse_args()

    config = {
        "cache": {
            "news_ttl_seconds": 1800,
            "stocks_ttl_seconds": 600,
        },
        "stocks": {
            "watchlist": args.symbols or ["AAPL", "NVDA", "MSFT", "GOOGL"],
        },
    }

    cache = Cache(args.cache_dir)

    if args.mock:
        news_provider = MockNewsProvider()
        stock_provider = MockStockProvider()
    else:
        news_provider = GNewsProvider(api_key="", language="en", country="us")
        stock_provider = YahooFinanceProvider()

    service = RealTimeDataService(config, news_provider, stock_provider, cache)
    formatter = Formatter()

    if args.command == "news":
        digest = service.get_news(args.topic, force_refresh=args.force_refresh)
        print(formatter.format_news_digest(digest))

    elif args.command == "stocks":
        digest = service.get_stocks(args.symbols, force_refresh=args.force_refresh)
        print(formatter.format_stock_digest(digest))

    elif args.command == "briefing":
        snapshot = service.get_briefing(force_refresh=args.force_refresh)
        print(formatter.format_briefing(snapshot))


if __name__ == "__main__":
    main()
