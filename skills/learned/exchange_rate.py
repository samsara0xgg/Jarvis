"""Exchange rate skill — currency conversion via open.er-api.com (free, no key)."""

from __future__ import annotations

import logging
from typing import Any

import requests

from skills import Skill

LOGGER = logging.getLogger(__name__)

_API_URL = "https://open.er-api.com/v6/latest/{base}"


class ExchangeRateSkill(Skill):
    """Provides currency exchange rate lookup and conversion."""

    def __init__(self) -> None:
        self.default_base: str = "USD"
        self.logger = LOGGER

    @property
    def skill_name(self) -> str:
        return "exchange_rate"

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "get_exchange_rate",
                "description": (
                    "Get the current exchange rate between two currencies, "
                    "or convert an amount from one currency to another. "
                    "Uses ISO 4217 currency codes (e.g. USD, CNY, CAD, EUR, JPY)."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "base": {
                            "type": "string",
                            "description": "Source currency code (e.g. USD, CNY, EUR). Defaults to 'USD'.",
                        },
                        "target": {
                            "type": "string",
                            "description": "Target currency code (e.g. CNY, CAD, JPY).",
                        },
                        "amount": {
                            "type": "number",
                            "description": "Amount to convert. Defaults to 1 if omitted.",
                        },
                    },
                    "required": ["target"],
                },
            },
        ]

    def execute(self, tool_name: str, tool_input: dict[str, Any], **context: Any) -> str:
        base: str = tool_input.get("base", self.default_base).strip().upper() or self.default_base
        target: str = tool_input.get("target", "").strip().upper()
        amount: float = float(tool_input.get("amount", 1))

        if not target:
            return "Error: target currency is required."

        try:
            resp = requests.get(
                _API_URL.format(base=base),
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            self.logger.warning("Exchange rate fetch failed for %s: %s", base, exc)
            return f"Failed to get exchange rate for {base}: {exc}"

        if data.get("result") != "success":
            error_type = data.get("error-type", "unknown error")
            return f"API error: {error_type}"

        rates: dict[str, float] = data.get("rates", {})
        if target not in rates:
            return f"Unknown currency code: {target}"

        rate: float = rates[target]
        converted: float = round(amount * rate, 4)

        if amount == 1:
            return f"1 {base} = {rate} {target}"
        return f"{amount} {base} = {converted} {target} (rate: 1 {base} = {rate} {target})"
