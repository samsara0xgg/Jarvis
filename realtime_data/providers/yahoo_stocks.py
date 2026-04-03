"""Yahoo Finance provider（免费、无需 API key）."""

from __future__ import annotations

import logging
import time
from datetime import datetime

import requests

from realtime_data.models import StockQuote
from realtime_data.providers.base import StockProvider

LOGGER = logging.getLogger(__name__)


class YahooFinanceProvider(StockProvider):
    """Yahoo Finance 股票源（免费）."""

    def __init__(self) -> None:
        self.base_url = "https://query1.finance.yahoo.com/v8/finance/chart"
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        }

    def fetch_quotes(self, symbols: list[str]) -> list[StockQuote]:
        """获取股票报价."""
        quotes = []

        for i, symbol in enumerate(symbols):
            if i > 0:
                time.sleep(1)  # 避免速率限制

            try:
                url = f"{self.base_url}/{symbol}"
                params = {"interval": "1d", "range": "1d"}

                resp = requests.get(url, params=params, headers=self.headers, timeout=10)
                resp.raise_for_status()
                data = resp.json()

                chart = data.get("chart", {})
                results = chart.get("result") or []
                if not results:
                    LOGGER.warning("Yahoo Finance: empty result for %s", symbol)
                    continue
                meta = results[0].get("meta", {})

                current_price = meta.get("regularMarketPrice", 0.0)
                prev_close = meta.get("chartPreviousClose", 0.0)
                change = current_price - prev_close
                change_pct = (change / prev_close) * 100 if prev_close else 0.0

                quotes.append(StockQuote(
                    symbol=symbol,
                    price=current_price,
                    change=change,
                    change_percent=change_pct,
                    currency=meta.get("currency", "USD"),
                    as_of=datetime.now(),
                    source="yahoo",
                ))

            except Exception as e:
                LOGGER.error("Yahoo Finance fetch failed for %s: %s", symbol, e)

        LOGGER.info("Yahoo Finance: fetched %d/%d quotes", len(quotes), len(symbols))
        return quotes
