"""Tests for the LLM client with mocked Anthropic and OpenAI SDKs."""

from __future__ import annotations

import json
import sys
import types
from typing import Any
from unittest.mock import MagicMock

from core.llm import LLMClient


def _make_config(provider="anthropic", **overrides):
    cfg: dict[str, Any] = {"provider": provider, "model": "test-model", "max_tokens": 256, "api_key": "test-key"}
    cfg.update(overrides)
    return {"llm": cfg}


class _FakeTextBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class _FakeToolUseBlock:
    def __init__(self, tool_id: str, name: str, tool_input: dict):
        self.type = "tool_use"
        self.id = tool_id
        self.name = name
        self.input = tool_input


class _FakeResponse:
    def __init__(self, content: list):
        self.content = content


def _install_fake_anthropic():
    """Install a fake anthropic module so LLMClient can import it."""
    fake_module = types.ModuleType("anthropic")

    class FakeAnthropic:
        def __init__(self, api_key=None):
            self.messages = MagicMock()

    fake_module.Anthropic = FakeAnthropic
    sys.modules["anthropic"] = fake_module
    return FakeAnthropic


def test_chat_returns_text_when_no_tools_called():
    FakeAnthropic = _install_fake_anthropic()
    try:
        client = LLMClient(_make_config())
        mock_anthropic = client._get_anthropic_client()
        mock_anthropic.messages.create = MagicMock(
            return_value=_FakeResponse([_FakeTextBlock("Hello, sir.")])
        )

        text, messages = client.chat("Hi Jarvis")
        assert text == "Hello, sir."
        assert len(messages) == 2  # user + assistant
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"
    finally:
        sys.modules.pop("anthropic", None)


def test_chat_executes_tool_and_returns_final_response():
    FakeAnthropic = _install_fake_anthropic()
    try:
        client = LLMClient(_make_config())
        mock_anthropic = client._get_anthropic_client()

        # First call: Claude wants to use a tool
        tool_response = _FakeResponse([
            _FakeToolUseBlock("call_1", "get_weather", {"city": "Vancouver"})
        ])
        # Second call: Claude gives final text after tool result
        final_response = _FakeResponse([
            _FakeTextBlock("It's 22 degrees in Vancouver.")
        ])
        mock_anthropic.messages.create = MagicMock(
            side_effect=[tool_response, final_response]
        )

        def fake_executor(tool_name, tool_input, **kwargs):
            return "22°C, sunny"

        text, messages = client.chat(
            "What's the weather?",
            tool_executor=fake_executor,
            tools=[{"name": "get_weather", "description": "test", "input_schema": {"type": "object", "properties": {}}}],
        )

        assert "22 degrees" in text
        assert mock_anthropic.messages.create.call_count == 2
    finally:
        sys.modules.pop("anthropic", None)


def test_chat_preserves_conversation_history():
    FakeAnthropic = _install_fake_anthropic()
    try:
        client = LLMClient(_make_config())
        mock_anthropic = client._get_anthropic_client()
        mock_anthropic.messages.create = MagicMock(
            return_value=_FakeResponse([_FakeTextBlock("Sure thing.")])
        )

        prior_history = [
            {"role": "user", "content": "Turn on the light"},
            {"role": "assistant", "content": [{"type": "text", "text": "Done."}]},
        ]

        text, messages = client.chat(
            "Make it brighter",
            conversation_history=prior_history,
        )

        assert text == "Sure thing."
        # prior 2 + new user + new assistant = 4
        assert len(messages) == 4
    finally:
        sys.modules.pop("anthropic", None)


def test_chat_caps_tool_call_rounds():
    FakeAnthropic = _install_fake_anthropic()
    try:
        client = LLMClient(_make_config())
        mock_anthropic = client._get_anthropic_client()

        # Always return a tool call — should hit the safety cap
        infinite_tool = _FakeResponse([
            _FakeToolUseBlock("call_n", "some_tool", {})
        ])
        mock_anthropic.messages.create = MagicMock(return_value=infinite_tool)

        def noop_executor(tool_name, tool_input, **kwargs):
            return "ok"

        text, messages = client.chat(
            "loop forever",
            tool_executor=noop_executor,
            tools=[{"name": "some_tool", "description": "test", "input_schema": {"type": "object", "properties": {}}}],
        )

        assert "circles" in text.lower() or "different approach" in text.lower()
        assert mock_anthropic.messages.create.call_count == 10
    finally:
        sys.modules.pop("anthropic", None)


# ======================================================================
# OpenAI backend tests
# ======================================================================

class _FakeOAIFunction:
    def __init__(self, name: str, arguments: str):
        self.name = name
        self.arguments = arguments


class _FakeOAIToolCall:
    def __init__(self, tc_id: str, name: str, arguments: str):
        self.id = tc_id
        self.type = "function"
        self.function = _FakeOAIFunction(name, arguments)


class _FakeOAIMessage:
    def __init__(self, content: str | None = None, tool_calls: list | None = None):
        self.content = content
        self.tool_calls = tool_calls


class _FakeOAIChoice:
    def __init__(self, message: _FakeOAIMessage):
        self.message = message


class _FakeOAIResponse:
    def __init__(self, choices: list[_FakeOAIChoice]):
        self.choices = choices


def _install_fake_openai():
    fake_module = types.ModuleType("openai")

    class FakeOpenAI:
        def __init__(self, api_key=None, base_url=None):
            self.chat = MagicMock()
            self.chat.completions = MagicMock()

    fake_module.OpenAI = FakeOpenAI
    sys.modules["openai"] = fake_module
    return FakeOpenAI


def test_openai_chat_returns_text():
    _install_fake_openai()
    try:
        client = LLMClient(_make_config(provider="openai"))
        mock_oai = client._get_openai_client()
        mock_oai.chat.completions.create = MagicMock(
            return_value=_FakeOAIResponse([
                _FakeOAIChoice(_FakeOAIMessage(content="Hello from GPT."))
            ])
        )

        text, messages = client.chat("Hi Jarvis")
        assert text == "Hello from GPT."
        assert any(m["role"] == "user" for m in messages)
    finally:
        sys.modules.pop("openai", None)


def test_openai_chat_executes_tool_call():
    _install_fake_openai()
    try:
        client = LLMClient(_make_config(provider="openai"))
        mock_oai = client._get_openai_client()

        tool_response = _FakeOAIResponse([
            _FakeOAIChoice(_FakeOAIMessage(
                content=None,
                tool_calls=[_FakeOAIToolCall("tc_1", "get_weather", '{"city": "Vancouver"}')],
            ))
        ])
        final_response = _FakeOAIResponse([
            _FakeOAIChoice(_FakeOAIMessage(content="It's 22 degrees."))
        ])
        mock_oai.chat.completions.create = MagicMock(
            side_effect=[tool_response, final_response]
        )

        def fake_executor(tool_name, tool_input, **kwargs):
            return "22°C, sunny"

        text, messages = client.chat(
            "What's the weather?",
            tool_executor=fake_executor,
            tools=[{"name": "get_weather", "description": "test", "input_schema": {"type": "object", "properties": {}}}],
        )

        assert "22 degrees" in text
        assert mock_oai.chat.completions.create.call_count == 2
    finally:
        sys.modules.pop("openai", None)


def test_openai_chat_caps_tool_rounds():
    _install_fake_openai()
    try:
        client = LLMClient(_make_config(provider="openai"))
        mock_oai = client._get_openai_client()

        infinite_tool = _FakeOAIResponse([
            _FakeOAIChoice(_FakeOAIMessage(
                content=None,
                tool_calls=[_FakeOAIToolCall("tc_n", "some_tool", '{}')],
            ))
        ])
        mock_oai.chat.completions.create = MagicMock(return_value=infinite_tool)

        text, _ = client.chat(
            "loop",
            tool_executor=lambda *a, **k: "ok",
            tools=[{"name": "some_tool", "description": "t", "input_schema": {"type": "object", "properties": {}}}],
        )

        assert "circles" in text.lower() or "different approach" in text.lower()
        assert mock_oai.chat.completions.create.call_count == 10
    finally:
        sys.modules.pop("openai", None)


# ======================================================================
# Streaming tests
# ======================================================================


def test_chat_stream_calls_on_sentence():
    """chat_stream should call on_sentence for each complete sentence."""
    from core.llm import LLMClient

    client = LLMClient(_make_config())
    sentences: list[str] = []

    # Test _flush_sentences directly
    buf = "你好。今天天气不错！明天"
    remainder = client._flush_sentences(buf, sentences.append)
    assert sentences == ["你好。", "今天天气不错！"]
    assert remainder == "明天"

    # Force flush remainder
    sentences.clear()
    client._flush_sentences("剩余内容", sentences.append, force=True)
    assert sentences == ["剩余内容"]


def test_chat_stream_no_callback_falls_back_to_chat():
    """chat_stream with on_sentence=None should behave like chat()."""
    FakeAnthropic = _install_fake_anthropic()
    try:
        client = LLMClient(_make_config())
        mock_anthropic = client._get_anthropic_client()
        mock_anthropic.messages.create = MagicMock(
            return_value=_FakeResponse([_FakeTextBlock("No streaming.")])
        )

        text, messages = client.chat_stream("test", on_sentence=None)
        assert text == "No streaming."
        assert mock_anthropic.messages.create.called
    finally:
        sys.modules.pop("anthropic", None)


# ======================================================================
# Model preset / switch tests
# ======================================================================

_PRESET_CONFIG = {
    "fast": {
        "model": "llama-3.3-70b-versatile",
        "base_url": "https://api.groq.com/openai/v1",
        "api_key_env": "GROQ_API_KEY",
        "max_tokens": 1024,
    },
    "deep": {
        "model": "grok-4-1-fast-non-reasoning",
        "base_url": "https://api.x.ai/v1",
        "api_key_env": "XAI_API_KEY",
        "max_tokens": 2048,
    },
}


class TestModelSwitch:
    """Tests for LLM model presets and runtime switching."""

    def test_default_preset_applied(self) -> None:
        """Init with default_preset='fast' applies the fast preset model."""
        _install_fake_openai()
        try:
            client = LLMClient(_make_config(
                provider="openai",
                default_preset="fast",
                presets=_PRESET_CONFIG,
                api_key="",
            ))
            assert client.model == "llama-3.3-70b-versatile"
            assert client.active_preset == "fast"
            assert client._base_url == "https://api.groq.com/openai/v1"
        finally:
            sys.modules.pop("openai", None)

    def test_switch_model_changes_active_preset(self) -> None:
        """switch_model from fast to deep updates model and preset."""
        _install_fake_openai()
        try:
            client = LLMClient(_make_config(
                provider="openai",
                default_preset="fast",
                presets=_PRESET_CONFIG,
                api_key="",
            ))
            assert client.model == "llama-3.3-70b-versatile"

            msg = client.switch_model("deep")
            assert client.model == "grok-4-1-fast-non-reasoning"
            assert client.active_preset == "deep"
            assert client._client is None  # client reset for re-init
            assert "deep" in msg
        finally:
            sys.modules.pop("openai", None)

    def test_switch_model_unknown_raises(self) -> None:
        """switch_model with unknown preset raises ValueError."""
        _install_fake_openai()
        try:
            client = LLMClient(_make_config(
                provider="openai",
                default_preset="fast",
                presets=_PRESET_CONFIG,
                api_key="",
            ))
            import pytest
            with pytest.raises(ValueError, match="Unknown preset"):
                client.switch_model("nonexistent")
        finally:
            sys.modules.pop("openai", None)

    def test_get_presets(self) -> None:
        """get_presets returns the correct preset dict."""
        _install_fake_openai()
        try:
            client = LLMClient(_make_config(
                provider="openai",
                default_preset="fast",
                presets=_PRESET_CONFIG,
                api_key="",
            ))
            presets = client.get_presets()
            assert set(presets.keys()) == {"fast", "deep"}
            assert presets["fast"]["model"] == "llama-3.3-70b-versatile"
            assert presets["deep"]["model"] == "grok-4-1-fast-non-reasoning"
        finally:
            sys.modules.pop("openai", None)

    def test_backward_compat_no_presets(self) -> None:
        """Config without presets section still works with flat fields."""
        _install_fake_openai()
        try:
            client = LLMClient(_make_config(
                provider="openai",
                model="gpt-4o",
                base_url="https://api.openai.com/v1",
                api_key="sk-test-key",
            ))
            assert client.model == "gpt-4o"
            assert client.active_preset is None
            assert client._api_key == "sk-test-key"
            assert client.get_presets() == {}
        finally:
            sys.modules.pop("openai", None)
