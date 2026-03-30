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
                    LOGGER.warning(f"Rate limit hit: {data['Note']}")
                    break

                quote_data = data.get("Global Quote", {})
                if not quote_data:
                    LOGGER.warning(f"No data for {symbol}")
                    continue

                price = float(quote_data["05. price"])
                change = float(quote_data["09. change"])
                change_pct = float(quote_data["10. change percent"].rstrip("%"))

                quotes.append(StockQuote(
                    symbol=symbol,
                    price=price,
                    change=change,
                    change_percent=change_pct,
                    as_of=datetime.now(),
                    source="alphavantage",
                ))

            except Exception as e:
                LOGGER.error(f"Alpha Vantage fetch failed for {symbol}: {e}")

        LOGGER.info(f"Alpha Vantage: fetched {len(quotes)}/{len(symbols)} quotes")
        return quotes
