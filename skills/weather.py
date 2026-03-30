"""Weather skill — free weather lookup via wttr.in."""

from __future__ import annotations

import logging
from typing import Any

import requests

from skills import Skill

LOGGER = logging.getLogger(__name__)


class WeatherSkill(Skill):
    """Provides current weather information using wttr.in (no API key needed)."""

    def __init__(self, config: dict) -> None:
        self.default_city = str(config.get("skills", {}).get("weather", {}).get("default_city", "Vancouver"))
        self.logger = LOGGER

    @property
    def skill_name(self) -> str:
        return "weather"

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "get_weather",
                "description": (
                    "Get current weather for a city. "
                    "Returns temperature, conditions, humidity, and wind."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "city": {
                            "type": "string",
                            "description": f"City name in English. Defaults to '{self.default_city}' if omitted.",
                        },
                    },
                },
            },
        ]

    def execute(self, tool_name: str, tool_input: dict[str, Any], **context: Any) -> str:
        city = tool_input.get("city", self.default_city).strip() or self.default_city
        try:
            resp = requests.get(
                f"https://wttr.in/{city}",
                params={"format": "j1"},
                timeout=10,
                headers={"Accept-Language": "zh"},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            self.logger.warning("Weather fetch failed for %s: %s", city, exc)
            return f"Failed to get weather for {city}: {exc}"

        current = data.get("current_condition", [{}])[0]
        temp_c = current.get("temp_C", "?")
        feels_like = current.get("FeelsLikeC", "?")
        desc = current.get("lang_zh", [{}])[0].get("value", current.get("weatherDesc", [{}])[0].get("value", "unknown"))
        humidity = current.get("humidity", "?")
        wind_kmph = current.get("windspeedKmph", "?")

        return (
            f"Weather in {city}: {desc}, "
            f"temperature {temp_c}°C (feels like {feels_like}°C), "
            f"humidity {humidity}%, wind {wind_kmph} km/h."
        )
