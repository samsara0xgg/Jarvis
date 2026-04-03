"""Alpha Vantage API provider."""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime

import requests

from realtime_data.models import StockQuote
from realtime_data.providers.base import StockProvider

LOGGER = logging.getLogger(__name__)


class AlphaVantageProvider(StockProvider):
    """Alpha Vantage 股票源."""

    def __init__(self, api_key: str, delay_seconds: float = 12.5) -> None:
        self.api_key = api_key or os.getenv("ALPHA_VANTAGE_API_KEY", "")
        self.base_url = "https://www.alphavantage.co/query"
        self.delay_seconds = delay_seconds  # 免费版：5 req/min = 12秒间隔

    def fetch_quotes(self, symbols: list[str]) -> list[StockQuote]:
        """获取股票报价."""
        if not self.api_key:
            LOGGER.error("Alpha Vantage API key not configured")
            return []

        quotes = []
        for i, symbol in enumerate(symbols):
            if i > 0:
                time.sleep(self.delay_seconds)

            try:
                params = {
                    "function": "GLOBAL_QUOTE",
                    "symbol": symbol,
                    "apikey": self.api_key,
                }

                resp = requests.get(self.base_url, params=params, timeout=10)
                resp.raise_for_status()
                data = resp.json()

                if "Note" in data:
                    LOGGER.warning("Alpha Vantage rate limit hit")
                    break

                quote_data = data.get("Global Quote", {})
                if not quote_data:
                    LOGGER.warning("No data for %s", symbol)
                    continue

                price_str = quote_data.get("05. price", "0")
                change_str = quote_data.get("09. change", "0")
                pct_str = quote_data.get("10. change percent", "0%")
                price = float(price_str)
                change = float(change_str)
                change_pct = float(pct_str.rstrip("%"))

                quotes.append(StockQuote(
                    symbol=symbol,
                    price=price,
                    change=change,
                    change_percent=change_pct,
                    as_of=datetime.now(),
                    source="alphavantage",
                ))

            except Exception as e:
                LOGGER.error("Alpha Vantage fetch failed for %s: %s", symbol, e)

        LOGGER.info("Alpha Vantage: fetched %d/%d quotes", len(quotes), len(symbols))
        return quotes
