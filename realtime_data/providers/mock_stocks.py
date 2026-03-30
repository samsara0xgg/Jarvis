"""Mock 股票 provider."""

from __future__ import annotations

from datetime import datetime

from realtime_data.models import StockQuote
from realtime_data.providers.base import StockProvider


class MockStockProvider(StockProvider):
    """Mock 股票数据源."""

    def fetch_quotes(self, symbols: list[str]) -> list[StockQuote]:
        mock_data = {
            "AAPL": (178.50, 2.30, 1.31),
            "NVDA": (485.20, -5.80, -1.18),
            "MSFT": (412.30, 3.50, 0.86),
            "GOOGL": (142.80, 1.20, 0.85),
        }

        quotes = []
        now = datetime.now()

        for symbol in symbols:
            if symbol in mock_data:
                price, change, change_pct = mock_data[symbol]
                quotes.append(StockQuote(
                    symbol=symbol,
                    price=price,
                    change=change,
                    change_percent=change_pct,
                    as_of=now,
                    source="mock",
                ))

        return quotes
