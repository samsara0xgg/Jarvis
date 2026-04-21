"""Multi-provider LLM client with tool-use loop for Jarvis conversations.

Supports Anthropic Claude and OpenAI GPT via config.yaml provider switch.
"""

from __future__ import annotations

import logging
import os
import uuid
import time
from typing import Any

from core.personality import build_personality_prompt
from memory.hot.assembler import PromptContext

LOGGER = logging.getLogger(__name__)

# Rough token estimate: Chinese ≈ 1.5 chars/token, English ≈ 4 chars/token.
# Use 1.5 (conservative) to avoid sending more context than max_history_tokens.
_CHARS_PER_TOKEN = 1.5


class LLMClient:
    """Power Jarvis conversations via Claude or OpenAI with tool calling.

    The provider is selected by ``config["llm"]["provider"]``:
      - ``"anthropic"`` (default) — uses the Anthropic Python SDK
      - ``"openai"`` — uses the OpenAI Python SDK

    Both backends share the same ``chat()`` interface and tool-use loop.

    Args:
        config: Parsed application configuration.
    """

    def __init__(self, config: dict, tracker: Any = None) -> None:
        llm_config = config.get("llm", {})
        self.provider = str(llm_config.get("provider", "anthropic")).strip().lower()
        self.max_tokens = int(llm_config.get("max_tokens", 1024))
        self.max_history_tokens = int(llm_config.get("max_history_tokens", 8000))
        self.max_retries = int(llm_config.get("max_retries", 2))
        self.logger = LOGGER
        self._tracker = tracker
        self._client: Any = None

        # Sentence divider tweaks (WP4): abbreviation guard + faster first response.
        sd_cfg = llm_config.get("sentence_divider", {}) or {}
        self._abbrev_protect = bool(sd_cfg.get("abbreviation_protect", True))
        self._faster_first_response = bool(sd_cfg.get("faster_first_response", True))
        self._is_first_sentence = True  # reset at start of each stream

        # Store presets (OpenAI-only for now)
        self._presets: dict[str, dict[str, Any]] = dict(llm_config.get("presets") or {})
        self.active_preset: str | None = None
        # Trace for system testing — single state bag, reset on every chat() entry.
        # last_metadata matches the llm_metadata JSON column in trace v3.
        # finish_reason and cache_read_input_tokens are kept separate (own columns).
        self._last_metadata: dict = self._empty_metadata()
        self._last_finish_reason: str | None = None
        self._last_cache_read_tokens: int | None = None
        # Total prompt + completion tokens for the most recent chat() call.
        # Populated by every concrete chat path (anthropic / openai non-stream
        # and stream). None when the path doesn't surface usage (Anthropic
        # pure-stream without get_final_message). Reset by chat() entry.
        self._last_input_tokens: int | None = None
        self._last_output_tokens: int | None = None

        # Defaults — may be overwritten by preset or flat config below
        self.model: str = str(llm_config.get("model", "gpt-4o"))
        self._base_url: str | None = None
        self._api_key: str | None = None

        # Sticky-routing ID for xAI automatic prompt caching. Without this
        # header the gateway load-balances across replicas; each replica has
        # its own local cache, so prefix hit rate drops to ~50-80% in
        # practice (see 2026-04-19 probes). A stable UUID pins all requests
        # from this process to one replica and lifts hits to ~100%.
        # App-level UUID (single user): highest cache utilisation.
        self._grok_conv_id = str(uuid.uuid4())

        if self.provider == "openai":
            default_preset = llm_config.get("default_preset", "")
            if default_preset and default_preset in self._presets:
                self._apply_preset(default_preset)
            else:
                # Backward compat: use flat config fields
                self.model = str(llm_config.get("model", "gpt-4o"))
                base_url = llm_config.get("base_url") or ""
                self._base_url = base_url or None
                self._api_key = self._resolve_api_key(llm_config.get("api_key", ""), base_url)
        else:
            self.model = str(llm_config.get("model", "claude-sonnet-4-20250514"))
            self._api_key = llm_config.get("api_key") or os.environ.get("ANTHROPIC_API_KEY")

    # ------------------------------------------------------------------
    # Last-call metadata accessors (trace v3)
    # ------------------------------------------------------------------

    @staticmethod
    def _empty_metadata() -> dict:
        """Return a fresh default-shaped metadata dict for one LLM call.

        Shape matches the ``llm_metadata`` JSON column in trace v3.
        Fields ``truncated_by_interrupt`` and ``full_response`` are intentionally
        left None here — they are set by the caller (``jarvis.py``) after the
        stream is cancelled, not at the LLM layer.

        Returns:
            A dict with all keys present and default values.
        """
        return {
            "provider": None,
            "conv_id": None,
            "response_id": None,
            "streaming": False,
            "fallback_used": False,
            "truncated_by_interrupt": False,
            "full_response": None,
            "cache_creation_input_tokens": None,
        }

    @property
    def last_metadata(self) -> dict:
        """Metadata for the most recent ``chat()`` / ``chat_stream()`` call.

        Shape matches the ``llm_metadata`` JSON column in trace v3 exactly so
        ``jarvis.py`` can store it without transformation::

            {
                "provider": "xai" | "openai" | "anthropic" | ...,
                "conv_id": "<x-grok-conv-id>" | None,
                "response_id": "chatcmpl-..." | None,
                "streaming": bool,
                "fallback_used": bool,
                "truncated_by_interrupt": False,   # set by jarvis.py after cancel
                "full_response": None,             # set by jarvis.py after cancel
                "cache_creation_input_tokens": int | None,  # Anthropic only
            }

        Returns:
            A shallow copy of the internal metadata dict.
        """
        return dict(self._last_metadata)

    @property
    def last_finish_reason(self) -> str | None:
        """Finish reason from the last LLM response (separate trace column).

        OpenAI/xAI: ``choice.finish_reason`` (e.g. ``"stop"``, ``"length"``).
        Anthropic: ``response.stop_reason`` (e.g. ``"end_turn"``, ``"max_tokens"``).

        Returns:
            The finish reason string, or ``None`` if not yet populated.
        """
        return self._last_finish_reason

    @property
    def last_cache_read_tokens(self) -> int | None:
        """Cache-read input tokens from the last LLM response (separate trace column).

        OpenAI/xAI: ``response.usage.prompt_tokens_details.cached_tokens``.
        Anthropic:  ``response.usage.cache_read_input_tokens``.
        Defaults to 0 when the provider returns no cache info.

        Returns:
            Token count, or ``None`` if not yet populated.
        """
        return self._last_cache_read_tokens

    @property
    def last_input_tokens(self) -> int | None:
        """Total prompt tokens for the most recent chat() call.

        Sums OpenAI ``usage.prompt_tokens`` (or Anthropic
        ``usage.input_tokens``) across the inner provider request — for
        tool-use loops the value reflects the FINAL request only (each
        loop iteration overwrites). None when the path doesn't surface
        usage (Anthropic pure-stream without get_final_message).
        """
        return self._last_input_tokens

    @property
    def last_output_tokens(self) -> int | None:
        """Total completion tokens for the most recent chat() call.

        See ``last_input_tokens``. Same semantics for tool-use loops and
        provider quirks.
        """
        return self._last_output_tokens

    # ------------------------------------------------------------------
    # Preset / model switching helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_api_key(explicit_key: str, base_url: str) -> str | None:
        """Resolve the API key from an explicit value or environment variable.

        Args:
            explicit_key: Key provided directly in config (may be empty).
            base_url: The base URL, used to infer the correct env var.

        Returns:
            The resolved API key, or ``None`` if unavailable.
        """
        if explicit_key:
            return explicit_key
        if "x.ai" in base_url:
            return os.environ.get("XAI_API_KEY")
        if "deepseek" in base_url:
            return os.environ.get("DEEPSEEK_API_KEY")
        if "moonshot" in base_url:
            return os.environ.get("MOONSHOT_API_KEY")
        if "groq" in base_url:
            return os.environ.get("GROQ_API_KEY")
        return os.environ.get("OPENAI_API_KEY")

    def _xai_cache_headers(self) -> dict[str, str]:
        """Sticky-routing header for xAI so repeated requests land on the
        same replica and its prompt cache actually hits. No-op on other
        OpenAI-compat providers (Groq / OpenAI native / DeepSeek etc.)."""
        if self._base_url and "x.ai" in self._base_url:
            return {"x-grok-conv-id": self._grok_conv_id}
        return {}

    def _apply_preset(self, name: str) -> None:
        """Apply a named model preset. Resets ``_client`` to force re-init.

        Args:
            name: Key in the ``presets`` dict from config.

        Raises:
            ValueError: If *name* is not found in available presets.
        """
        if name not in self._presets:
            raise ValueError(
                f"Unknown preset '{name}'. Available: {list(self._presets)}"
            )
        preset = self._presets[name]

        # Optional provider switch — lets presets like "opus" swap between
        # OpenAI-compatible clients and the native Anthropic SDK at runtime.
        # If absent, keep current provider.
        new_provider = preset.get("provider")
        if new_provider:
            self.provider = str(new_provider).strip().lower()

        self.model = str(preset.get("model", self.model))
        base_url = preset.get("base_url") or ""
        self._base_url = base_url or None
        self.max_tokens = int(preset.get("max_tokens", self.max_tokens))

        # Resolve API key: prefer api_key_env, then fallback heuristic
        api_key_env = preset.get("api_key_env", "")
        if api_key_env:
            self._api_key = os.environ.get(api_key_env)
        else:
            self._api_key = self._resolve_api_key("", base_url)

        # Reset client so next call re-inits the correct SDK for self.provider
        self._client = None
        self.active_preset = name
        self.logger.info(
            "Applied preset '%s': provider=%s model=%s base_url=%s",
            name, self.provider, self.model, self._base_url,
        )

    def switch_model(self, preset_name: str) -> str:
        """Switch to a named preset at runtime.

        Args:
            preset_name: Key in the ``presets`` dict from config.

        Returns:
            Human-readable confirmation message.

        Raises:
            ValueError: If *preset_name* is not found in available presets.
        """
        self._apply_preset(preset_name)
        return f"已切换到 {preset_name} 模式 (model={self.model})"

    def get_presets(self) -> dict[str, dict[str, Any]]:
        """Return available model presets.

        Returns:
            A dict mapping preset names to their configuration.
        """
        return dict(self._presets)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def chat(
        self,
        user_message: str,
        *,
        conversation_history: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_executor: Any | None = None,
        user_name: str | None = None,
        user_id: str | None = None,
        user_role: str = "guest",
        user_emotion: str = "",
        prompt_context: PromptContext | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Send a message and run the full tool-use loop.

        Works identically regardless of the underlying provider.

        When ``prompt_context`` is given, its blocks define the ``system``
        payload (list[dict] for Anthropic with cache_control, joined string
        for OpenAI/xAI). Otherwise ``_personalize_system`` produces a single
        string from the caller's user/role/emotion.

        Returns:
            A tuple of (final_text_response, updated_messages_list).
        """
        # Reset per-call state so stale values from turn N never bleed into N+1.
        self._last_metadata = self._empty_metadata()
        self._last_finish_reason = None
        self._last_cache_read_tokens = None
        self._last_input_tokens = None
        self._last_output_tokens = None

        component = f"llm.{self.provider}"
        try:
            if self.provider == "openai":
                result = self._chat_openai(
                    user_message,
                    conversation_history=conversation_history,
                    tools=tools,
                    tool_executor=tool_executor,
                    user_name=user_name,
                    user_id=user_id,
                    user_role=user_role,
                    user_emotion=user_emotion,
                    prompt_context=prompt_context,
                )
            else:
                result = self._chat_anthropic(
                    user_message,
                    conversation_history=conversation_history,
                    tools=tools,
                    tool_executor=tool_executor,
                    user_name=user_name,
                    user_id=user_id,
                    user_role=user_role,
                    user_emotion=user_emotion,
                    prompt_context=prompt_context,
                )
            if self._tracker:
                self._tracker.record_success(component)
            return result
        except Exception:
            if self._tracker:
                self._tracker.record_failure(component)
            raise

    # ------------------------------------------------------------------
    # Anthropic backend
    # ------------------------------------------------------------------

    def _chat_anthropic(
        self,
        user_message: str,
        *,
        conversation_history: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_executor: Any | None = None,
        user_name: str | None = None,
        user_id: str | None = None,
        user_role: str = "guest",
        user_emotion: str = "",
        prompt_context: PromptContext | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        client = self._get_anthropic_client()
        if prompt_context is not None:
            system: Any = prompt_context.to_anthropic_system()
        else:
            system = self._personalize_system(
                user_name, user_role, user_emotion,
            )
        messages = self._truncate_history(list(conversation_history or []))
        messages.append({"role": "user", "content": user_message})

        for _ in range(10):
            kwargs: dict[str, Any] = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "system": system,
                "messages": messages,
            }
            if tools:
                kwargs["tools"] = tools

            self.logger.info("Sending request to Anthropic (%s)", self.model)
            response = self._call_with_retry(lambda: client.messages.create(**kwargs))

            # Populate metadata from this response (overwritten each loop turn,
            # ending with the final call's data when the loop exits).
            usage = getattr(response, "usage", None)
            self._last_metadata["provider"] = "anthropic"
            self._last_metadata["response_id"] = getattr(response, "id", None)
            self._last_metadata["streaming"] = False
            self._last_metadata["cache_creation_input_tokens"] = (
                getattr(usage, "cache_creation_input_tokens", None) if usage else None
            )
            self._last_finish_reason = getattr(response, "stop_reason", None)
            self._last_cache_read_tokens = (
                getattr(usage, "cache_read_input_tokens", None) if usage else 0
            ) or 0
            self._last_input_tokens = (
                getattr(usage, "input_tokens", None) if usage else None
            )
            self._last_output_tokens = (
                getattr(usage, "output_tokens", None) if usage else None
            )

            assistant_content = self._serialize_anthropic_content(response.content)
            messages.append({"role": "assistant", "content": assistant_content})

            tool_use_blocks = [
                block for block in response.content
                if getattr(block, "type", None) == "tool_use"
            ]

            if not tool_use_blocks:
                text = self._extract_anthropic_text(response.content)
                self.logger.info("Anthropic response: %s", text[:200])
                return text, messages

            if tool_executor is None:
                text = self._extract_anthropic_text(response.content)
                return text or "I wanted to use a tool but no executor is available.", messages

            tool_results = []
            for block in tool_use_blocks:
                self.logger.info("Tool call: %s(%s)", block.name, block.input)
                result_text = tool_executor(
                    block.name, block.input,
                    user_id=user_id, user_role=user_role,
                )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(result_text),
                })
            messages.append({"role": "user", "content": tool_results})

        return "I seem to be going in circles. Let me try a different approach.", messages

    def _get_anthropic_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import anthropic
        except ImportError as exc:
            raise RuntimeError(
                "The anthropic package is required. Install with: pip install anthropic"
            ) from exc
        self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    def _extract_anthropic_text(self, content: Any) -> str:
        parts = []
        for block in content:
            if hasattr(block, "text"):
                parts.append(block.text)
        return "".join(parts).strip()

    def _serialize_anthropic_content(self, content: Any) -> list[dict[str, Any]]:
        result = []
        for block in content:
            if getattr(block, "type", None) == "text":
                result.append({"type": "text", "text": block.text})
            elif getattr(block, "type", None) == "tool_use":
                result.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })
            else:
                result.append({"type": "text", "text": str(block)})
        return result

    # ------------------------------------------------------------------
    # OpenAI backend
    # ------------------------------------------------------------------

    def _chat_openai(
        self,
        user_message: str,
        *,
        conversation_history: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_executor: Any | None = None,
        user_name: str | None = None,
        user_id: str | None = None,
        user_role: str = "guest",
        user_emotion: str = "",
        prompt_context: PromptContext | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        client = self._get_openai_client()
        if prompt_context is not None:
            system = prompt_context.to_openai_system_str()
        else:
            system = self._personalize_system(
                user_name, user_role, user_emotion,
            )

        # Convert Anthropic-style history to OpenAI format
        truncated = self._truncate_history(list(conversation_history or []))
        messages = [{"role": "system", "content": system}]
        messages.extend(self._history_to_openai(truncated))
        messages.append({"role": "user", "content": user_message})

        # Convert Anthropic tool schemas to OpenAI function format
        openai_tools = self._tools_to_openai(tools) if tools else None

        # Keep a parallel Anthropic-style history for storage
        stored_messages = list(conversation_history or [])
        stored_messages.append({"role": "user", "content": user_message})

        import json as _json

        for _ in range(10):
            kwargs: dict[str, Any] = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "messages": messages,
            }
            if openai_tools:
                kwargs["tools"] = openai_tools
            headers = self._xai_cache_headers()
            if headers:
                kwargs["extra_headers"] = headers

            self.logger.info("Sending request to OpenAI (%s)", self.model)
            response = self._call_with_retry(lambda: client.chat.completions.create(**kwargs))
            choice = response.choices[0]
            assistant_msg = choice.message

            # Populate metadata from this response (overwritten each loop turn).
            usage = getattr(response, "usage", None)
            ptd = getattr(usage, "prompt_tokens_details", None) if usage else None
            _base_url = self._base_url or ""
            _provider = "xai" if "x.ai" in _base_url else (
                "groq" if "groq" in _base_url else (
                    "cerebras" if "cerebras" in _base_url else "openai"
                )
            )
            self._last_metadata["provider"] = _provider
            self._last_metadata["response_id"] = getattr(response, "id", None)
            self._last_metadata["streaming"] = False
            self._last_metadata["cache_creation_input_tokens"] = None
            if _provider == "xai":
                self._last_metadata["conv_id"] = self._grok_conv_id
            self._last_finish_reason = getattr(choice, "finish_reason", None)
            self._last_cache_read_tokens = (
                getattr(ptd, "cached_tokens", None) if ptd else None
            ) or 0
            self._last_input_tokens = (
                getattr(usage, "prompt_tokens", None) if usage else None
            )
            self._last_output_tokens = (
                getattr(usage, "completion_tokens", None) if usage else None
            )

            # Store assistant message in OpenAI format for the loop
            oai_assistant: dict[str, Any] = {
                "role": "assistant",
                "content": assistant_msg.content or "",
            }
            if assistant_msg.tool_calls:
                oai_assistant["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in assistant_msg.tool_calls
                ]
            messages.append(oai_assistant)

            # Store in Anthropic-compatible format for persistence
            stored_content: list[dict[str, Any]] = []
            if assistant_msg.content:
                stored_content.append({"type": "text", "text": assistant_msg.content})
            if assistant_msg.tool_calls:
                for tc in assistant_msg.tool_calls:
                    stored_content.append({
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.function.name,
                        "input": _json.loads(tc.function.arguments),
                    })
            stored_messages.append({"role": "assistant", "content": stored_content or assistant_msg.content or ""})

            if not assistant_msg.tool_calls:
                text = (assistant_msg.content or "").strip()
                self.logger.info("OpenAI response: %s", text[:200])
                return text, stored_messages

            if tool_executor is None:
                text = (assistant_msg.content or "").strip()
                return text or "I wanted to use a tool but no executor is available.", stored_messages

            # Execute tool calls
            tool_results_for_stored = []
            for tc in assistant_msg.tool_calls:
                func_name = tc.function.name
                func_args = _json.loads(tc.function.arguments)
                self.logger.info("Tool call: %s(%s)", func_name, func_args)
                result_text = tool_executor(
                    func_name, func_args,
                    user_id=user_id, user_role=user_role,
                )
                # OpenAI expects tool results as separate messages
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": str(result_text),
                })
                tool_results_for_stored.append({
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "content": str(result_text),
                })

            stored_messages.append({"role": "user", "content": tool_results_for_stored})

        return "I seem to be going in circles. Let me try a different approach.", stored_messages

    def _get_openai_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError(
                "The openai package is required. Install with: pip install openai"
            ) from exc
        self._client = OpenAI(api_key=self._api_key, base_url=self._base_url)
        return self._client

    def _tools_to_openai(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert Anthropic-style tool definitions to OpenAI function calling format."""
        result = []
        for tool in tools:
            result.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                },
            })
        return result

    def _history_to_openai(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert stored Anthropic-style history to OpenAI message format."""
        import json as _json
        result = []
        for msg in history:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if isinstance(content, str):
                result.append({"role": role, "content": content})
                continue

            if isinstance(content, list):
                # Check if it's tool results
                if content and isinstance(content[0], dict) and content[0].get("type") == "tool_result":
                    for tr in content:
                        result.append({
                            "role": "tool",
                            "tool_call_id": tr.get("tool_use_id", ""),
                            "content": str(tr.get("content", "")),
                        })
                    continue

                # Assistant content blocks
                text_parts = []
                tool_calls = []
                for block in content:
                    if not isinstance(block, dict):
                        text_parts.append(str(block))
                        continue
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        tool_calls.append({
                            "id": block.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": _json.dumps(block.get("input", {})),
                            },
                        })

                oai_msg: dict[str, Any] = {
                    "role": role,
                    "content": "".join(text_parts) or None,
                }
                if tool_calls:
                    oai_msg["tool_calls"] = tool_calls
                result.append(oai_msg)
                continue

            result.append({"role": role, "content": str(content)})

        # Sanitize: remove orphaned tool messages that lack a preceding
        # assistant message with tool_calls (OpenAI rejects these with 400).
        sanitized = []
        for msg in result:
            if msg.get("role") == "tool":
                # Walk back past other tool messages to find the assistant
                prev = next(
                    (m for m in reversed(sanitized) if m.get("role") != "tool"),
                    None,
                )
                if prev and prev.get("role") == "assistant" and prev.get("tool_calls"):
                    sanitized.append(msg)
                else:
                    sanitized.append({
                        "role": "user",
                        "content": f"[Tool result: {msg.get('content', '')}]",
                    })
            else:
                sanitized.append(msg)
        return sanitized

    # ------------------------------------------------------------------
    # History truncation and retry
    # ------------------------------------------------------------------

    def _truncate_history(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Trim conversation history to fit within max_history_tokens.

        Drops oldest messages first, keeping the most recent context.
        """
        if not messages:
            return messages

        total_chars = sum(self._estimate_message_chars(m) for m in messages)
        max_chars = self.max_history_tokens * _CHARS_PER_TOKEN

        while total_chars > max_chars and len(messages) > 2:
            dropped = messages.pop(0)
            total_chars -= self._estimate_message_chars(dropped)

        return messages

    def _estimate_message_chars(self, msg: dict[str, Any]) -> int:
        """Rough character count for a message."""
        content = msg.get("content", "")
        if isinstance(content, str):
            return len(content)
        if isinstance(content, list):
            total = 0
            for block in content:
                if isinstance(block, dict):
                    total += len(str(block.get("text", ""))) + len(str(block.get("content", "")))
                else:
                    total += len(str(block))
            return total
        return len(str(content))

    def _call_with_retry(self, fn: Any) -> Any:
        """Call fn() with exponential backoff on transient failures."""
        last_exc = None
        for attempt in range(self.max_retries + 1):
            try:
                return fn()
            except Exception as exc:
                last_exc = exc
                exc_str = str(exc).lower()
                is_transient = any(k in exc_str for k in [
                    "timeout", "rate_limit", "overloaded", "529", "503", "500",
                    "connection", "temporary",
                ])
                if not is_transient or attempt >= self.max_retries:
                    raise
                wait = 2 ** attempt
                self.logger.warning(
                    "LLM request failed (attempt %d/%d): %s. Retrying in %ds...",
                    attempt + 1, self.max_retries + 1, exc, wait,
                )
                time.sleep(wait)
        raise last_exc  # unreachable but satisfies type checker

    # ------------------------------------------------------------------
    # Streaming API
    # ------------------------------------------------------------------

    _SENTENCE_DELIMITERS = {"。", "！", "？", ".", "!", "?", "；", "\n"}
    # Faster-first-response: also break on commas for the first sentence only
    # (perceived TTS latency drops because shorter first segment ships earlier).
    _FIRST_RESPONSE_DELIMITERS = _SENTENCE_DELIMITERS | {",", "，"}
    # English abbreviations whose trailing "." should NOT trigger a split.
    # Keep ordered by length-desc so longer matches (e.g. "Mrs.") win over short
    # prefixes — endswith() doesn't care, but it's a useful invariant.
    _ABBREVIATIONS: tuple[str, ...] = (
        "Mrs.", "Prof.", "e.g.", "i.e.",
        "Mr.", "Ms.", "Dr.", "Jr.", "Sr.", "St.", "Rd.",
        "Inc.", "Ltd.", "vs.",
    )

    def chat_stream(
        self,
        user_message: str,
        *,
        conversation_history: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_executor: Any | None = None,
        user_name: str | None = None,
        user_id: str | None = None,
        user_role: str = "guest",
        on_sentence: Any | None = None,
        user_emotion: str = "",
        prompt_context: PromptContext | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Stream LLM response, calling on_sentence for each complete sentence.

        Falls back to non-streaming chat() if tool calls are detected.

        Args:
            on_sentence: Callback ``fn(text: str)`` invoked per sentence.
                If None, behaves identically to ``chat()``.

        Returns:
            Same as ``chat()`` — (full_text, updated_messages).
        """
        if on_sentence is None or self.provider not in ("anthropic", "openai"):
            return self.chat(
                user_message,
                conversation_history=conversation_history,
                tools=tools,
                tool_executor=tool_executor,
                user_name=user_name,
                user_id=user_id,
                user_role=user_role,
                user_emotion=user_emotion,
                prompt_context=prompt_context,
            )

        # Reset first-sentence flag at the start of each streaming turn so
        # faster_first_response only fires on the very first sentence emitted.
        self._is_first_sentence = True

        if self.provider == "openai":
            return self._stream_openai(
                user_message,
                conversation_history=conversation_history,
                tools=tools,
                tool_executor=tool_executor,
                user_name=user_name,
                user_id=user_id,
                user_role=user_role,
                on_sentence=on_sentence,
                user_emotion=user_emotion,
                prompt_context=prompt_context,
            )
        return self._stream_anthropic(
            user_message,
            conversation_history=conversation_history,
            tools=tools,
            tool_executor=tool_executor,
            user_name=user_name,
            user_id=user_id,
            user_role=user_role,
            on_sentence=on_sentence,
            user_emotion=user_emotion,
            prompt_context=prompt_context,
        )

    def _flush_sentences(self, buffer: str, on_sentence: Any, force: bool = False) -> str:
        """Extract complete sentences from buffer, call on_sentence, return remainder.

        Honors:
          - decimal-point guard (``3.14`` not split)
          - abbreviation guard (``Dr.`` ``e.g.`` etc., toggleable via config)
          - faster-first-response (commas count as splits for sentence #1)
        """
        while True:
            delimiters = self._SENTENCE_DELIMITERS
            if self._faster_first_response and self._is_first_sentence:
                delimiters = self._FIRST_RESPONSE_DELIMITERS

            protected_dots = self._protected_dot_positions(buffer)
            split_at = self._find_split_point(
                buffer, delimiters, force, protected_dots,
            )
            if split_at == -1:
                if force and buffer.strip():
                    on_sentence(buffer.strip())
                    self._is_first_sentence = False
                    return ""
                return buffer

            sentence = buffer[:split_at + 1].strip()
            buffer = buffer[split_at + 1:]
            if sentence:
                on_sentence(sentence)
                self._is_first_sentence = False

    def _protected_dot_positions(self, buffer: str) -> set[int]:
        """Indices of every '.' inside an abbreviation match within *buffer*.

        Multi-dot abbreviations like ``e.g.`` need every internal dot
        protected — endswith() alone only catches the final one.
        """
        if not self._abbrev_protect:
            return set()
        bad: set[int] = set()
        for abbr in self._ABBREVIATIONS:
            start = 0
            while True:
                idx = buffer.find(abbr, start)
                if idx == -1:
                    break
                for k, ch in enumerate(abbr):
                    if ch == ".":
                        bad.add(idx + k)
                start = idx + 1
        return bad

    def _find_split_point(
        self,
        buffer: str,
        delimiters: set[str],
        force: bool,
        protected_dots: set[int],
    ) -> int:
        """Return the earliest index in *buffer* eligible to end a sentence.

        Walks char-by-char so the abbreviation/decimal guards can advance
        past a vetoed delimiter and find a later valid split point.
        Returns -1 if no eligible split point exists.
        """
        n = len(buffer)
        for i in range(n):
            ch = buffer[i]
            if ch not in delimiters:
                continue
            if ch == ".":
                # Decimal point: 3.14 — skip when surrounded by digits (or
                # when next char hasn't streamed in yet and we can wait).
                if i > 0 and buffer[i - 1].isdigit():
                    if i + 1 < n and buffer[i + 1].isdigit():
                        continue
                    if i + 1 >= n and not force:
                        continue
                if i in protected_dots:
                    continue
                # Streaming edge case: the dot might be the first char of an
                # abbreviation that hasn't fully arrived yet (e.g., buffer
                # ends in "Dr" then "." comes next chunk; or "e." waiting
                # for "g."). If we're at end-of-buffer and not forced, hold.
                if not force and i + 1 >= n and self._abbrev_protect:
                    if self._possible_abbreviation_prefix(buffer, i):
                        continue
            return i
        return -1

    def _possible_abbreviation_prefix(self, buffer: str, dot_idx: int) -> bool:
        """True if buffer[:dot_idx+1] could be the start of an abbreviation.

        Used at end-of-stream-buffer to defer splitting on a trailing dot
        when more chars might still arrive. Example: buffer ends in "e."
        and "g." would land in the next delta — splitting now would emit
        a partial sentence; waiting one delta lets "e.g." form properly.

        T1.3 fix: requires a word boundary before the head match. Without it,
        "Welcome." matches head "e." (prefix of "e.g.") because the last 2
        chars ARE "e." — but the "e" is the tail of the word "Welcome", not
        an abbreviation start. The guard: char at `start - 1` must be
        non-alpha (or `start == 0`, i.e., buffer beginning).
        """
        for abbr in self._ABBREVIATIONS:
            # Look for abbreviations that start somewhere at/before dot_idx
            # and extend past dot_idx (i.e., not yet fully present).
            for offset in range(min(len(abbr), dot_idx + 1)):
                head = abbr[: offset + 1]
                if not head or head[-1] != ".":
                    continue
                start = dot_idx + 1 - len(head)
                if start < 0:
                    continue
                if buffer[start: start + len(head)] != head:
                    continue
                # Word-boundary guard: the char just before `start` must NOT
                # be alphabetic. `start == 0` counts as valid boundary too.
                if start > 0 and buffer[start - 1].isalpha():
                    continue
                if offset + 1 < len(abbr):
                    return True
        return False

    def _stream_anthropic(
        self,
        user_message: str,
        *,
        conversation_history: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_executor: Any | None = None,
        user_name: str | None = None,
        user_id: str | None = None,
        user_role: str = "guest",
        on_sentence: Any,
        user_emotion: str = "",
        prompt_context: PromptContext | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        client = self._get_anthropic_client()
        if prompt_context is not None:
            system: Any = prompt_context.to_anthropic_system()
        else:
            system = self._personalize_system(
                user_name, user_role, user_emotion,
            )
        messages = self._truncate_history(list(conversation_history or []))
        messages.append({"role": "user", "content": user_message})

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools

        self.logger.info("Streaming request to Anthropic (%s)", self.model)

        try:
            with client.messages.stream(**kwargs) as stream:
                full_text = ""
                buffer = ""
                has_tool_use = False

                for event in stream:
                    if hasattr(event, "type"):
                        if event.type == "content_block_start" and getattr(
                            getattr(event, "content_block", None), "type", None
                        ) == "tool_use":
                            has_tool_use = True
                            break
                        if event.type == "content_block_delta" and hasattr(event, "delta"):
                            text_chunk = getattr(event.delta, "text", "")
                            if text_chunk:
                                full_text += text_chunk
                                buffer += text_chunk
                                buffer = self._flush_sentences(buffer, on_sentence)

                # 只在纯文本响应时 flush 剩余 buffer；
                # 如果检测到 tool_use，跳过——fallback 会重新生成完整回复，
                # 否则用户会听到重复内容（流式播了一遍，fallback 又播一遍）
                if not has_tool_use:
                    self._flush_sentences(buffer, on_sentence, force=True)

                if has_tool_use or not full_text.strip():
                    reason = "tool use" if has_tool_use else "empty stream"
                    self.logger.info("%s detected, falling back to chat()", reason)
                    self._last_metadata["fallback_used"] = True
                    messages.pop()
                    return self._chat_anthropic(
                        user_message,
                        conversation_history=conversation_history,
                        tools=tools,
                        tool_executor=tool_executor,
                        user_name=user_name,
                        user_id=user_id,
                        user_role=user_role,
                        user_emotion=user_emotion,
                        )

                messages.append({
                    "role": "assistant",
                    "content": [{"type": "text", "text": full_text}],
                })

                # Commit streaming metadata BEFORE returning (constraint 4).
                # response_id is not available in Anthropic stream events;
                # finish_reason and cache tokens require get_final_message().
                # Those are populated by _chat_anthropic on fallback; for the
                # pure-stream path we record what we know.
                self._last_metadata["provider"] = "anthropic"
                self._last_metadata["response_id"] = None
                self._last_metadata["streaming"] = True
                self._last_metadata["cache_creation_input_tokens"] = None
                # finish_reason and cache_read_tokens stay None for pure-stream path
                # (Anthropic stream does not surface usage without get_final_message).

                self.logger.info("Anthropic stream complete: %s", full_text[:200])
                return full_text, messages

        except Exception as exc:
            self.logger.warning("Streaming failed: %s, falling back to chat()", exc)
            self._last_metadata["fallback_used"] = True
            messages.pop()
            return self._chat_anthropic(
                user_message,
                conversation_history=conversation_history,
                tools=tools,
                tool_executor=tool_executor,
                user_name=user_name,
                user_id=user_id,
                user_role=user_role,
                user_emotion=user_emotion,
            )

    def _stream_openai(
        self,
        user_message: str,
        *,
        conversation_history: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_executor: Any | None = None,
        user_name: str | None = None,
        user_id: str | None = None,
        user_role: str = "guest",
        on_sentence: Any,
        user_emotion: str = "",
        prompt_context: PromptContext | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        client = self._get_openai_client()
        if prompt_context is not None:
            system = prompt_context.to_openai_system_str()
        else:
            system = self._personalize_system(
                user_name, user_role, user_emotion,
            )

        truncated = self._truncate_history(list(conversation_history or []))
        oai_messages = [{"role": "system", "content": system}]
        oai_messages.extend(self._history_to_openai(truncated))
        oai_messages.append({"role": "user", "content": user_message})

        openai_tools = self._tools_to_openai(tools) if tools else None
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": oai_messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if openai_tools:
            kwargs["tools"] = openai_tools
        headers = self._xai_cache_headers()
        if headers:
            kwargs["extra_headers"] = headers

        self.logger.info("Streaming request to OpenAI (%s)", self.model)

        stored_messages = list(conversation_history or [])
        stored_messages.append({"role": "user", "content": user_message})

        # Reset streaming token accumulators; metadata already reset by chat().
        _stream_input_tokens = 0
        _stream_output_tokens = 0
        _stream_finish_reason: str | None = None
        _stream_cached_tokens: int = 0
        # OpenAI/xAI stream chunks carry the same response id (chatcmpl-...)
        # in every chunk. Capture once from the first chunk that has one.
        _stream_response_id: str | None = None

        try:
            response = self._call_with_retry(
                lambda: client.chat.completions.create(**kwargs)
            )
            full_text = ""
            buffer = ""
            has_tool_calls = False

            for chunk in response:
                if _stream_response_id is None:
                    _cid = getattr(chunk, "id", None)
                    if _cid:
                        _stream_response_id = _cid
                if getattr(chunk, "usage", None):
                    _stream_input_tokens += getattr(chunk.usage, "prompt_tokens", 0) or 0
                    _stream_output_tokens += getattr(chunk.usage, "completion_tokens", 0) or 0
                    _ptd = getattr(chunk.usage, "prompt_tokens_details", None)
                    _stream_cached_tokens = getattr(_ptd, "cached_tokens", None) or 0
                if chunk.choices:
                    _fr = getattr(chunk.choices[0], "finish_reason", None)
                    if _fr:
                        _stream_finish_reason = _fr
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta is None:
                    continue
                if delta.tool_calls:
                    has_tool_calls = True
                    break
                if delta.content:
                    full_text += delta.content
                    buffer += delta.content
                    buffer = self._flush_sentences(buffer, on_sentence)

            self._flush_sentences(buffer, on_sentence, force=True)

            if has_tool_calls or not full_text.strip():
                reason = "tool use" if has_tool_calls else "empty stream"
                self.logger.info("%s detected, falling back to chat()", reason)
                self._last_metadata["fallback_used"] = True
                return self._chat_openai(
                    user_message,
                    conversation_history=conversation_history,
                    tools=tools,
                    tool_executor=tool_executor,
                    user_name=user_name,
                    user_id=user_id,
                    user_role=user_role,
                    user_emotion=user_emotion,
                )

            stored_messages.append({
                "role": "assistant",
                "content": [{"type": "text", "text": full_text}],
            })

            # Commit streaming metadata BEFORE returning so callers see it
            # immediately (constraint 4 — no async worker delay).
            _base_url = self._base_url or ""
            _provider = "xai" if "x.ai" in _base_url else (
                "groq" if "groq" in _base_url else (
                    "cerebras" if "cerebras" in _base_url else "openai"
                )
            )
            self._last_metadata["provider"] = _provider
            self._last_metadata["response_id"] = _stream_response_id
            self._last_metadata["streaming"] = True
            self._last_metadata["cache_creation_input_tokens"] = None
            if _provider == "xai":
                self._last_metadata["conv_id"] = self._grok_conv_id
            self._last_finish_reason = _stream_finish_reason
            self._last_cache_read_tokens = _stream_cached_tokens
            # Streaming tokens come piecewise via the chunk.usage stat
            # event (sent at end-of-stream). Commit the totals so callers
            # can compute cost from prompt + completion before chat() returns.
            self._last_input_tokens = _stream_input_tokens or None
            self._last_output_tokens = _stream_output_tokens or None

            self.logger.info("OpenAI stream complete: %s", full_text[:200])
            return full_text, stored_messages

        except Exception as exc:
            self.logger.warning("Streaming failed: %s, falling back to chat()", exc)
            self._last_metadata["fallback_used"] = True
            return self._chat_openai(
                user_message,
                conversation_history=conversation_history,
                tools=tools,
                tool_executor=tool_executor,
                user_name=user_name,
                user_id=user_id,
                user_role=user_role,
                user_emotion=user_emotion,
            )

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _personalize_system(
        self, user_name: str | None, user_role: str, user_emotion: str = "",
    ) -> str:
        """Fallback single-string system builder used only when no PromptContext was supplied.

        Production paths should pass ``prompt_context`` to the chat/stream
        entry points; this helper survives for rephrase / tests that still
        call in without an Assembler-built context.
        """
        return build_personality_prompt(
            user_name=user_name,
            user_role=user_role,
            user_emotion=user_emotion,
        )
