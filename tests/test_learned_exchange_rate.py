"""Tests for the exchange_rate skill."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from skills.learned.exchange_rate import ExchangeRateSkill


@pytest.fixture()
def skill() -> ExchangeRateSkill:
    return ExchangeRateSkill()


@pytest.fixture()
def mock_rates() -> dict:
    return {
        "result": "success",
        "rates": {
            "USD": 1.0,
            "CNY": 7.25,
            "CAD": 1.37,
            "EUR": 0.92,
            "JPY": 149.5,
        },
    }


class TestSkillProperties:
    def test_skill_name(self, skill: ExchangeRateSkill) -> None:
        assert skill.skill_name == "exchange_rate"

    def test_default_base(self, skill: ExchangeRateSkill) -> None:
        assert skill.default_base == "USD"

    def test_tool_definitions(self, skill: ExchangeRateSkill) -> None:
        defs = skill.get_tool_definitions()
        assert len(defs) == 1
        assert defs[0]["name"] == "get_exchange_rate"
        assert "input_schema" in defs[0]
        assert "target" in defs[0]["input_schema"]["required"]


class TestExecute:
    @patch("skills.learned.exchange_rate.requests.get")
    def test_basic_rate(self, mock_get: MagicMock, skill: ExchangeRateSkill, mock_rates: dict) -> None:
        mock_get.return_value = MagicMock(status_code=200)
        mock_get.return_value.json.return_value = mock_rates
        mock_get.return_value.raise_for_status = MagicMock()

        result = skill.execute("get_exchange_rate", {"target": "CNY"})
        assert "1 USD = 7.25 CNY" in result
        mock_get.assert_called_once()

    @patch("skills.learned.exchange_rate.requests.get")
    def test_with_amount(self, mock_get: MagicMock, skill: ExchangeRateSkill, mock_rates: dict) -> None:
        mock_get.return_value = MagicMock(status_code=200)
        mock_get.return_value.json.return_value = mock_rates
        mock_get.return_value.raise_for_status = MagicMock()

        result = skill.execute("get_exchange_rate", {"target": "CNY", "amount": 100})
        assert "725.0 CNY" in result
        assert "rate:" in result

    @patch("skills.learned.exchange_rate.requests.get")
    def test_custom_base(self, mock_get: MagicMock, skill: ExchangeRateSkill) -> None:
        mock_get.return_value = MagicMock(status_code=200)
        mock_get.return_value.json.return_value = {
            "result": "success",
            "rates": {"USD": 0.72, "CNY": 5.25},
        }
        mock_get.return_value.raise_for_status = MagicMock()

        result = skill.execute("get_exchange_rate", {"base": "CAD", "target": "USD"})
        assert "1 CAD = 0.72 USD" in result

    @patch("skills.learned.exchange_rate.requests.get")
    def test_unknown_target(self, mock_get: MagicMock, skill: ExchangeRateSkill, mock_rates: dict) -> None:
        mock_get.return_value = MagicMock(status_code=200)
        mock_get.return_value.json.return_value = mock_rates
        mock_get.return_value.raise_for_status = MagicMock()

        result = skill.execute("get_exchange_rate", {"target": "XYZ"})
        assert "Unknown currency code" in result

    def test_empty_target(self, skill: ExchangeRateSkill) -> None:
        result = skill.execute("get_exchange_rate", {"target": ""})
        assert "Error" in result

    @patch("skills.learned.exchange_rate.requests.get")
    def test_network_error(self, mock_get: MagicMock, skill: ExchangeRateSkill) -> None:
        mock_get.side_effect = ConnectionError("timeout")

        result = skill.execute("get_exchange_rate", {"target": "CNY"})
        assert "Failed" in result

    @patch("skills.learned.exchange_rate.requests.get")
    def test_api_error_response(self, mock_get: MagicMock, skill: ExchangeRateSkill) -> None:
        mock_get.return_value = MagicMock(status_code=200)
        mock_get.return_value.json.return_value = {
            "result": "error",
            "error-type": "unsupported-code",
        }
        mock_get.return_value.raise_for_status = MagicMock()

        result = skill.execute("get_exchange_rate", {"target": "CNY"})
        assert "API error" in result

    @patch("skills.learned.exchange_rate.requests.get")
    def test_case_insensitive_input(self, mock_get: MagicMock, skill: ExchangeRateSkill, mock_rates: dict) -> None:
        mock_get.return_value = MagicMock(status_code=200)
        mock_get.return_value.json.return_value = mock_rates
        mock_get.return_value.raise_for_status = MagicMock()

        result = skill.execute("get_exchange_rate", {"base": "usd", "target": "cny"})
        assert "CNY" in result
