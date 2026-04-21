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


# ======================================================================
# last_metadata / last_finish_reason / last_cache_read_tokens tests
# ======================================================================


class _FakeUsage:
    """Minimal usage object matching Anthropic SDK shape."""

    def __init__(
        self,
        cache_read_input_tokens: int = 0,
        cache_creation_input_tokens: int | None = None,
        input_tokens: int = 10,
        output_tokens: int = 5,
    ):
        self.cache_read_input_tokens = cache_read_input_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeAnthropicResponse:
    """Minimal Anthropic Messages.create response with usage and id."""

    def __init__(
        self,
        content: list,
        stop_reason: str = "end_turn",
        response_id: str = "msg_test001",
        usage: _FakeUsage | None = None,
    ):
        self.content = content
        self.stop_reason = stop_reason
        self.id = response_id
        self.usage = usage or _FakeUsage()


class _FakeOAIUsageDetails:
    def __init__(self, cached_tokens: int = 0):
        self.cached_tokens = cached_tokens


class _FakeOAIUsage:
    def __init__(self, prompt_tokens: int = 10, completion_tokens: int = 5, cached_tokens: int = 0):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.prompt_tokens_details = _FakeOAIUsageDetails(cached_tokens)


class _FakeOAIChoiceWithFinish:
    def __init__(self, message: "_FakeOAIMessage", finish_reason: str = "stop"):
        self.message = message
        self.finish_reason = finish_reason


class _FakeOAIResponseFull:
    """OpenAI response with id, usage, and finish_reason."""

    def __init__(
        self,
        choices: list,
        response_id: str = "chatcmpl-test001",
        usage: _FakeOAIUsage | None = None,
    ):
        self.choices = choices
        self.id = response_id
        self.usage = usage or _FakeOAIUsage()


def test_last_metadata_anthropic_non_streaming():
    """Non-streaming Anthropic call populates last_metadata, finish_reason, cache_read_tokens."""
    _install_fake_anthropic()
    try:
        client = LLMClient(_make_config(provider="anthropic"))
        mock_anthropic = client._get_anthropic_client()
        mock_anthropic.messages.create = MagicMock(
            return_value=_FakeAnthropicResponse(
                content=[_FakeTextBlock("Hi there.")],
                stop_reason="end_turn",
                response_id="msg_abc123",
                usage=_FakeUsage(
                    cache_read_input_tokens=42,
                    cache_creation_input_tokens=100,
                ),
            )
        )

        text, _ = client.chat("Hello")
        assert text == "Hi there."

        meta = client.last_metadata
        assert meta["provider"] == "anthropic"
        assert meta["response_id"] == "msg_abc123"
        assert meta["streaming"] is False
        assert meta["fallback_used"] is False
        assert meta["cache_creation_input_tokens"] == 100
        assert client.last_finish_reason == "end_turn"
        assert client.last_cache_read_tokens == 42
    finally:
        sys.modules.pop("anthropic", None)


def test_last_metadata_openai_non_streaming():
    """Non-streaming OpenAI/xAI call populates last_metadata with correct provider."""
    _install_fake_openai()
    try:
        client = LLMClient(_make_config(provider="openai", base_url="https://api.x.ai/v1"))
        mock_oai = client._get_openai_client()
        mock_oai.chat.completions.create = MagicMock(
            return_value=_FakeOAIResponseFull(
                choices=[_FakeOAIChoiceWithFinish(
                    message=_FakeOAIMessage(content="From xAI."),
                    finish_reason="stop",
                )],
                response_id="chatcmpl-xai001",
                usage=_FakeOAIUsage(cached_tokens=8),
            )
        )

        text, _ = client.chat("Hello")
        assert text == "From xAI."

        meta = client.last_metadata
        assert meta["provider"] == "xai"
        assert meta["response_id"] == "chatcmpl-xai001"
        assert meta["streaming"] is False
        assert meta["fallback_used"] is False
        assert meta["cache_creation_input_tokens"] is None
        assert client.last_finish_reason == "stop"
        assert client.last_cache_read_tokens == 8
    finally:
        sys.modules.pop("openai", None)


def test_last_metadata_no_stale_leak_between_turns():
    """Turn 2 metadata is fresh — fallback_used from turn 1 does not bleed in."""
    _install_fake_anthropic()
    try:
        client = LLMClient(_make_config(provider="anthropic"))
        mock_anthropic = client._get_anthropic_client()

        # Turn 1: simulate a tool-use stream fallback by directly setting the flag,
        # then calling chat() for a clean non-tool response.
        client._last_metadata["fallback_used"] = True  # manually poison state

        mock_anthropic.messages.create = MagicMock(
            return_value=_FakeAnthropicResponse(
                content=[_FakeTextBlock("Fresh answer.")],
                stop_reason="end_turn",
            )
        )

        # Turn 2: chat() must reset first.
        text, _ = client.chat("New question")
        assert text == "Fresh answer."
        assert client.last_metadata["fallback_used"] is False, (
            "fallback_used leaked from poisoned turn 1 into turn 2"
        )
    finally:
        sys.modules.pop("anthropic", None)


def test_last_metadata_anthropic_cache_creation_tokens():
    """Anthropic cache_creation_input_tokens surfaces in last_metadata."""
    _install_fake_anthropic()
    try:
        client = LLMClient(_make_config(provider="anthropic"))
        mock_anthropic = client._get_anthropic_client()
        mock_anthropic.messages.create = MagicMock(
            return_value=_FakeAnthropicResponse(
                content=[_FakeTextBlock("Cached.")],
                usage=_FakeUsage(cache_creation_input_tokens=512, cache_read_input_tokens=0),
            )
        )

        client.chat("prompt with long system")
        meta = client.last_metadata
        assert meta["cache_creation_input_tokens"] == 512
        assert client.last_cache_read_tokens == 0
    finally:
        sys.modules.pop("anthropic", None)


def test_last_metadata_openai_no_cache_creation_tokens():
    """OpenAI responses always have cache_creation_input_tokens=None in last_metadata."""
    _install_fake_openai()
    try:
        client = LLMClient(_make_config(provider="openai"))
        mock_oai = client._get_openai_client()
        mock_oai.chat.completions.create = MagicMock(
            return_value=_FakeOAIResponseFull(
                choices=[_FakeOAIChoiceWithFinish(
                    message=_FakeOAIMessage(content="OpenAI answer."),
                    finish_reason="stop",
                )],
                usage=_FakeOAIUsage(cached_tokens=16),
            )
        )

        client.chat("hello")
        meta = client.last_metadata
        assert meta["cache_creation_input_tokens"] is None
        assert client.last_cache_read_tokens == 16
    finally:
        sys.modules.pop("openai", None)


def test_last_metadata_streaming_openai_populated_before_return():
    """After streaming chat_stream(), metadata is populated BEFORE the call returns."""
    _install_fake_openai()
    try:
        client = LLMClient(_make_config(provider="openai", base_url="https://api.x.ai/v1"))
        mock_oai = client._get_openai_client()

        # Build fake streaming chunks
        class _Chunk:
            def __init__(self, content=None, finish_reason=None, usage=None):
                self.choices = [_StreamChoice(content, finish_reason)]
                self.usage = usage

        class _StreamChoice:
            def __init__(self, content, finish_reason):
                self.delta = _Delta(content)
                self.finish_reason = finish_reason

        class _Delta:
            def __init__(self, content):
                self.content = content
                self.tool_calls = None

        usage_chunk = _FakeOAIUsage(cached_tokens=5)
        chunks = [
            _Chunk(content="Hello "),
            _Chunk(content="world."),
            _Chunk(finish_reason="stop", usage=usage_chunk),
        ]
        mock_oai.chat.completions.create = MagicMock(return_value=iter(chunks))

        sentences: list[str] = []
        text, _ = client.chat_stream("Hi", on_sentence=sentences.append)

        # Constraint 4: metadata must be populated immediately on return
        meta = client.last_metadata
        assert meta["provider"] == "xai", f"expected 'xai', got {meta['provider']!r}"
        assert meta["streaming"] is True
        assert client.last_cache_read_tokens == 5
    finally:
        sys.modules.pop("openai", None)


# ---------------------------------------------------------------------------
# prompt_context path — verifies Assembler-backed system construction
# ---------------------------------------------------------------------------

from memory.hot.assembler import PromptBlock, PromptContext


def _make_prompt_context():
    """Build a minimal PromptContext with identity/profile/observations/situation."""
    return PromptContext(
        blocks=[
            PromptBlock(content="IDENTITY", cache=True, name="identity"),
            PromptBlock(content="PROFILE", cache=True, name="profile"),
            PromptBlock(content="OBSERVATIONS", cache=True, name="observations"),
            PromptBlock(content="SITUATION", cache=False, name="situation"),
        ],
        messages=[{"role": "user", "content": "Hi"}],
    )


def test_chat_anthropic_uses_prompt_context_system_as_list_with_cache_control():
    """When prompt_context is given, Anthropic receives system=list[dict] with cache_control."""
    _install_fake_anthropic()
    try:
        client = LLMClient(_make_config())
        mock_anthropic = client._get_anthropic_client()
        captured_kwargs: dict = {}

        def _capture(**kwargs):
            captured_kwargs.update(kwargs)
            return _FakeResponse([_FakeTextBlock("ok")])

        mock_anthropic.messages.create = MagicMock(side_effect=_capture)

        client.chat("Hi Jarvis", prompt_context=_make_prompt_context())

        system = captured_kwargs["system"]
        assert isinstance(system, list), f"expected list[dict], got {type(system)}"
        assert len(system) == 4
        for entry in system:
            assert entry["type"] == "text"
        # First three cached, last not
        assert system[0]["cache_control"] == {"type": "ephemeral"}
        assert system[1]["cache_control"] == {"type": "ephemeral"}
        assert system[2]["cache_control"] == {"type": "ephemeral"}
        assert "cache_control" not in system[3]
        texts = [e["text"] for e in system]
        assert texts == ["IDENTITY", "PROFILE", "OBSERVATIONS", "SITUATION"]
    finally:
        sys.modules.pop("anthropic", None)


def test_chat_anthropic_falls_back_to_memory_context_when_prompt_context_none():
    """Without prompt_context, system must remain a plain string (legacy path)."""
    _install_fake_anthropic()
    try:
        client = LLMClient(_make_config())
        mock_anthropic = client._get_anthropic_client()
        captured_kwargs: dict = {}

        def _capture(**kwargs):
            captured_kwargs.update(kwargs)
            return _FakeResponse([_FakeTextBlock("ok")])

        mock_anthropic.messages.create = MagicMock(side_effect=_capture)

        client.chat("Hi")

        assert isinstance(captured_kwargs["system"], str)
    finally:
        sys.modules.pop("anthropic", None)


def test_chat_openai_uses_prompt_context_system_str():
    """When prompt_context is given, OpenAI receives blocks joined with blank lines."""
    _install_fake_openai()
    try:
        client = LLMClient(_make_config(provider="openai"))
        mock_openai = client._get_openai_client()
        captured_kwargs: dict = {}

        def _capture(**kwargs):
            captured_kwargs.update(kwargs)
            # Simulate the full OpenAI response shape expected by _chat_openai
            return _FakeOAIResponse([
                _FakeOAIChoice(_FakeOAIMessage(content="ok", tool_calls=[]))
            ])

        mock_openai.chat.completions.create = MagicMock(side_effect=_capture)

        client.chat("Hi", prompt_context=_make_prompt_context())

        messages = captured_kwargs["messages"]
        system_msg = messages[0]
        assert system_msg["role"] == "system"
        assert system_msg["content"] == "IDENTITY\n\nPROFILE\n\nOBSERVATIONS\n\nSITUATION"
    finally:
        sys.modules.pop("openai", None)
