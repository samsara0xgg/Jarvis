"""Tests for intent_router and local_executor."""

from __future__ import annotations

import json
import os
from collections import OrderedDict
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from core.intent_router import IntentRouter, RouteResult, build_system_prompt, VALID_INTENTS
from core.local_executor import LocalExecutor


# --- Fixtures ---

@pytest.fixture
def config():
    """Minimal config for testing."""
    return {
        "devices": {
            "sim_devices": [
                {"device_id": "living_room_light", "name": "客厅灯", "device_type": "light"},
                {"device_id": "bedroom_light", "name": "卧室灯", "device_type": "light"},
                {"device_id": "home_thermostat", "name": "客厅空调", "device_type": "thermostat"},
                {"device_id": "front_door_lock", "name": "入户门锁", "device_type": "door_lock"},
            ],
        },
        "models": {
            "groq": {"api_key": "", "router_model": "llama-3.1-8b-instant"},
            "cerebras": {"api_key": "", "router_model": "llama3.1-8b"},
            "local": {"provider": "ollama", "model": "qwen2.5:7b", "base_url": "http://localhost:11434"},
            "routing": {"confidence_threshold": 0.7},
        },
    }


@pytest.fixture
def mock_registry():
    """Mock SkillRegistry that records calls."""
    registry = MagicMock()
    registry.execute.return_value = "OK"
    return registry


@pytest.fixture
def executor(mock_registry):
    return LocalExecutor(mock_registry)


# --- build_system_prompt ---

class TestBuildSystemPrompt:
    def test_includes_device_ids(self, config):
        prompt = build_system_prompt(config)
        assert "living_room_light" in prompt
        assert "bedroom_light" in prompt
        assert "home_thermostat" in prompt
        assert "front_door_lock" in prompt

    def test_includes_device_names(self, config):
        prompt = build_system_prompt(config)
        assert "客厅灯" in prompt
        assert "客厅空调" in prompt

    def test_includes_actions(self, config):
        prompt = build_system_prompt(config)
        assert "turn_on" in prompt
        assert "set_brightness" in prompt
        assert "set_temperature" in prompt
        assert "lock" in prompt

    def test_empty_devices(self):
        prompt = build_system_prompt({"devices": {"sim_devices": []}})
        assert "设备" in prompt or "JSON" in prompt


# --- IntentRouter ---

class TestIntentRouter:
    def _make_groq_response(self, intent_data: dict) -> dict:
        return {
            "choices": [{
                "message": {"content": json.dumps(intent_data)}
            }]
        }

    def test_init_no_keys(self, config):
        """No API keys → all providers show 'no key'."""
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("GROQ_API_KEY", None)
            os.environ.pop("CEREBRAS_API_KEY", None)
            router = IntentRouter(config)
            assert router.groq_key == ""
            assert router.cerebras_key == ""

    def test_route_all_providers_down(self, config):
        """All providers unavailable → returns cloud/complex."""
        with patch.object(IntentRouter, '__init__', lambda self, cfg: None):
            router = IntentRouter.__new__(IntentRouter)
            router.config = config
            router.system_prompt = ""
            router.groq_key = ""
            router.cerebras_key = ""
            router.logger = MagicMock()
            router._tracker = None
            router._route_cache = OrderedDict()
            router._cache_max = 256

            result = router.route("开灯")
            assert result.tier == "cloud"
            assert result.intent == "complex"
            assert result.provider == "none"

    @patch("core.intent_router._SESSION")
    def test_groq_success(self, mock_session, config):
        config["models"]["groq"]["api_key"] = "test_key"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = self._make_groq_response({
            "intent": "smart_home",
            "confidence": 0.95,
            "actions": [{"device_id": "living_room_light", "action": "turn_on", "value": None}],
            "response": "好的，已开灯。",
        })
        mock_session.post.return_value = mock_resp

        router = IntentRouter(config)
        result = router.route("开灯")

        assert result.intent == "smart_home"
        assert result.tier == "local"
        assert result.provider == "groq"
        assert len(result.actions) == 1
        assert result.actions[0]["device_id"] == "living_room_light"
        assert result.response == "好的，已开灯。"

    @patch("core.intent_router._SESSION")
    def test_groq_rate_limit_falls_to_cerebras(self, mock_session, config):
        config["models"]["groq"]["api_key"] = "test_key"
        config["models"]["cerebras"]["api_key"] = "test_key"

        # Groq returns 429
        groq_resp = MagicMock()
        groq_resp.status_code = 429

        # Cerebras returns success
        cerebras_resp = MagicMock()
        cerebras_resp.status_code = 200
        cerebras_resp.raise_for_status.return_value = None
        cerebras_resp.json.return_value = self._make_groq_response({
            "intent": "complex", "confidence": 0.9, "response": None,
        })

        mock_session.post.side_effect = [groq_resp, cerebras_resp]

        router = IntentRouter(config)
        result = router.route("帮我写封邮件")

        assert result.intent == "complex"
        assert result.tier == "cloud"
        assert result.provider == "cerebras"

    @patch("core.intent_router._SESSION")
    def test_invalid_json_returns_none(self, mock_session, config):
        config["models"]["groq"]["api_key"] = "test_key"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"choices": [{"message": {"content": "not json at all"}}]}
        mock_session.post.return_value = mock_resp

        router = IntentRouter(config)
        # Should fall through to next provider or return cloud/complex
        result = router.route("开灯")
        # With no other providers, should be cloud/complex
        assert result.tier == "cloud"

    @patch("core.intent_router._SESSION")
    def test_complex_routes_to_cloud(self, mock_session, config):
        config["models"]["groq"]["api_key"] = "test_key"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = self._make_groq_response({
            "intent": "complex", "confidence": 0.85, "response": None,
        })
        mock_session.post.return_value = mock_resp

        router = IntentRouter(config)
        result = router.route("帮我写封邮件")
        assert result.tier == "cloud"
        assert result.intent == "complex"

    @patch("core.intent_router._SESSION")
    def test_uncertain_routes_to_cloud(self, mock_session, config):
        config["models"]["groq"]["api_key"] = "test_key"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = self._make_groq_response({
            "intent": "uncertain", "confidence": 0.3, "response": None,
        })
        mock_session.post.return_value = mock_resp

        router = IntentRouter(config)
        result = router.route("嗯")
        assert result.tier == "cloud"

    @patch("core.intent_router._SESSION")
    def test_info_query_routes_local(self, mock_session, config):
        config["models"]["groq"]["api_key"] = "test_key"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = self._make_groq_response({
            "intent": "info_query", "confidence": 0.95,
            "sub_type": "stocks", "query": ["NVDA"], "response": None,
        })
        mock_session.post.return_value = mock_resp

        router = IntentRouter(config)
        result = router.route("NVDA多少钱")
        assert result.tier == "local"
        assert result.sub_type == "stocks"
        assert result.query == ["NVDA"]


# --- LocalExecutor ---

class TestLocalExecutor:
    def test_execute_smart_home_single(self, executor, mock_registry):
        from core.local_executor import Action
        actions = [{"device_id": "living_room_light", "action": "turn_on", "value": None}]
        result = executor.execute_smart_home(actions, "owner")
        mock_registry.execute.assert_called_once_with(
            "smart_home_control",
            {"device_id": "living_room_light", "action": "turn_on"},
            user_role="owner",
        )
        assert result.action == Action.RESPONSE

    def test_execute_smart_home_with_value(self, executor, mock_registry):
        actions = [{"device_id": "home_thermostat", "action": "set_temperature", "value": 25}]
        executor.execute_smart_home(actions, "owner")
        mock_registry.execute.assert_called_once_with(
            "smart_home_control",
            {"device_id": "home_thermostat", "action": "set_temperature", "value": 25},
            user_role="owner",
        )

    def test_execute_smart_home_multiple(self, executor, mock_registry):
        actions = [
            {"device_id": "living_room_light", "action": "turn_on", "value": None},
            {"device_id": "home_thermostat", "action": "turn_on", "value": None},
        ]
        executor.execute_smart_home(actions, "owner")
        assert mock_registry.execute.call_count == 2

    def test_execute_smart_home_empty(self, executor):
        from core.local_executor import Action
        result = executor.execute_smart_home([], "owner")
        assert result.action == Action.RESPONSE

    def test_execute_smart_home_error(self, executor, mock_registry):
        from core.local_executor import Action
        mock_registry.execute.return_value = "Error: device not found"
        actions = [{"device_id": "nonexistent", "action": "turn_on", "value": None}]
        result = executor.execute_smart_home(actions, "owner")
        assert "失败" in result.text

    def test_execute_smart_home_skips_empty_fields(self, executor, mock_registry):
        actions = [{"device_id": "", "action": "turn_on"}, {"device_id": "x", "action": ""}]
        executor.execute_smart_home(actions, "owner")
        mock_registry.execute.assert_not_called()

    def test_execute_info_query_stocks(self, executor, mock_registry):
        from core.local_executor import Action
        mock_registry.execute.return_value = "AAPL: $248"
        result = executor.execute_info_query("stocks", ["AAPL"], "owner")
        mock_registry.execute.assert_called_once_with(
            "get_stock_watchlist", {"symbols": ["AAPL"]}, user_role="owner",
        )
        assert "248" in result.text
        assert result.action == Action.REQLLM

    def test_execute_info_query_news(self, executor, mock_registry):
        mock_registry.execute.return_value = "AI新闻..."
        result = executor.execute_info_query("news", "AI", "owner")
        mock_registry.execute.assert_called_once_with(
            "get_news_briefing", {"focus": "AI"}, user_role="owner",
        )

    def test_execute_info_query_weather(self, executor, mock_registry):
        executor.execute_info_query("weather", None, "owner")
        mock_registry.execute.assert_called_once_with(
            "get_weather", {}, user_role="owner",
        )

    def test_execute_info_query_unknown(self, executor):
        from core.local_executor import Action
        result = executor.execute_info_query("unknown_type", None, "owner")
        assert result.action == Action.RESPONSE
        assert "没查到" in result.text

    def test_execute_time_current(self, executor):
        from core.local_executor import Action
        result = executor.execute_time("current_time")
        assert "点" in result.text
        assert result.action == Action.RESPONSE

    def test_execute_time_date(self, executor):
        result = executor.execute_time("date")
        assert "年" in result.text and "月" in result.text

    def test_execute_time_weekday(self, executor):
        result = executor.execute_time("weekday")
        assert "周" in result.text or "年" in result.text

    def test_execute_time_default(self, executor):
        from core.local_executor import Action
        result = executor.execute_time(None)
        assert "点" in result.text
        assert result.action == Action.RESPONSE

    def test_execute_smart_home_response_has_text(self, executor, mock_registry):
        """ActionResponse.text should be a string, not None — for rule callbacks."""
        from core.local_executor import Action, ActionResponse
        actions = [{"device_id": "living_room_light", "action": "turn_on"}]
        result = executor.execute_smart_home(actions, "owner", response="好的，灯开了。")
        assert isinstance(result, ActionResponse)
        assert isinstance(result.text, str)
        assert result.action == Action.RESPONSE
        assert result.text == "好的，灯开了。"

    def test_execute_info_query_reqllm_type(self, executor, mock_registry):
        """info_query should return REQLLM so LLM can rephrase the data."""
        from core.local_executor import Action
        mock_registry.execute.return_value = "AAPL: $248, +2.3%"
        result = executor.execute_info_query("stocks", ["AAPL"], "owner")
        assert result.action == Action.REQLLM
        assert "248" in result.text


class TestRouteCache:
    """Tests for the LRU route cache in IntentRouter."""

    @patch("core.intent_router._SESSION")
    def test_cache_hit_skips_api_call(self, mock_session, config):
        config["models"]["groq"]["api_key"] = "test_key"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": json.dumps({
                "intent": "smart_home", "confidence": 0.95,
                "actions": [{"device_id": "living_room_light", "action": "turn_on", "value": None}],
                "response": "好的",
            })}}]
        }
        mock_session.post.return_value = mock_resp

        router = IntentRouter(config)
        r1 = router.route("开灯")
        r2 = router.route("开灯")

        assert mock_session.post.call_count == 1
        assert r2.intent == "smart_home"
        assert r2.provider == "groq"

    @patch("core.intent_router._SESSION")
    def test_cache_miss_on_different_text(self, mock_session, config):
        config["models"]["groq"]["api_key"] = "test_key"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": json.dumps({
                "intent": "smart_home", "confidence": 0.95,
                "actions": [], "response": "好的",
            })}}]
        }
        mock_session.post.return_value = mock_resp

        router = IntentRouter(config)
        router.route("开灯")
        router.route("关灯")
        assert mock_session.post.call_count == 2

    def test_failed_route_not_cached(self, config):
        """provider='none' results should NOT be cached."""
        router = IntentRouter(config)
        router.groq_key = ""
        router.cerebras_key = ""
        router.route("开灯")
        router.route("开灯")
        assert router.cache_size == 0

    @patch("core.intent_router._SESSION")
    def test_cache_lru_eviction(self, mock_session, config):
        config["models"]["groq"]["api_key"] = "test_key"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": json.dumps({
                "intent": "complex", "confidence": 0.9, "response": None,
            })}}]
        }
        mock_session.post.return_value = mock_resp

        router = IntentRouter(config)
        router._cache_max = 3

        for i in range(5):
            router.route(f"query_{i}")

        assert router.cache_size == 3

    @patch("core.intent_router._SESSION")
    def test_cached_result_is_independent_copy(self, mock_session, config):
        """Mutating a returned RouteResult should not affect cache."""
        config["models"]["groq"]["api_key"] = "test_key"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": json.dumps({
                "intent": "smart_home", "confidence": 0.95,
                "actions": [{"device_id": "x", "action": "turn_on", "value": None}],
                "response": "OK",
            })}}]
        }
        mock_session.post.return_value = mock_resp

        router = IntentRouter(config)
        r1 = router.route("开灯")
        r1.intent = "MUTATED"
        r2 = router.route("开灯")
        assert r2.intent == "smart_home"
