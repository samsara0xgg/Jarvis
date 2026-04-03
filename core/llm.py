"""Multi-provider LLM client with tool-use loop for Jarvis conversations.

Supports Anthropic Claude and OpenAI GPT via config.yaml provider switch.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from core.personality import build_personality_prompt

LOGGER = logging.getLogger(__name__)

# Rough token estimate: 1 token ≈ 3 characters for mixed CJK/English
_CHARS_PER_TOKEN = 3


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

        if self.provider == "openai":
            self.model = str(llm_config.get("model", "gpt-4o"))
            base_url = llm_config.get("base_url") or ""
            self._base_url = base_url or None
            # 根据 base_url 选对应的 API key，避免用错 key 调错服务
            if llm_config.get("api_key"):
                self._api_key = llm_config["api_key"]
            elif "deepseek" in base_url:
                self._api_key = os.environ.get("DEEPSEEK_API_KEY")
            elif "moonshot" in base_url:
                self._api_key = os.environ.get("MOONSHOT_API_KEY")
            else:
                self._api_key = os.environ.get("OPENAI_API_KEY")
        else:
            self.model = str(llm_config.get("model", "claude-sonnet-4-20250514"))
            self._api_key = llm_config.get("api_key") or os.environ.get("ANTHROPIC_API_KEY")

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
        memory_context: str = "",
    ) -> tuple[str, list[dict[str, Any]]]:
        """Send a message and run the full tool-use loop.

        Works identically regardless of the underlying provider.

        Returns:
            A tuple of (final_text_response, updated_messages_list).
        """
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
                    memory_context=memory_context,
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
                    memory_context=memory_context,
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
        memory_context: str = "",
    ) -> tuple[str, list[dict[str, Any]]]:
        client = self._get_anthropic_client()
        system = self._personalize_system(user_name, user_role, user_emotion, memory_context=memory_context)
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
        memory_context: str = "",
    ) -> tuple[str, list[dict[str, Any]]]:
        client = self._get_openai_client()
        system = self._personalize_system(user_name, user_role, user_emotion, memory_context=memory_context)

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

            self.logger.info("Sending request to OpenAI (%s)", self.model)
            response = self._call_with_retry(lambda: client.chat.completions.create(**kwargs))
            choice = response.choices[0]
            assistant_msg = choice.message

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
        return result

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
        memory_context: str = "",
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
                memory_context=memory_context,
            )

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
                memory_context=memory_context,
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
            memory_context=memory_context,
        )

    def _flush_sentences(self, buffer: str, on_sentence: Any, force: bool = False) -> str:
        """Extract complete sentences from buffer, call on_sentence, return remainder."""
        while True:
            # Find the earliest sentence delimiter
            earliest = -1
            for delim in self._SENTENCE_DELIMITERS:
                pos = buffer.find(delim)
                if pos != -1 and (earliest == -1 or pos < earliest):
                    # 跳过小数点：前后是数字的 "." 不是句子分隔符
                    if delim == "." and pos > 0 and buffer[pos - 1].isdigit():
                        # 往后看：如果后面也是数字，说明是小数（如 "3.14"）
                        if pos + 1 < len(buffer) and buffer[pos + 1].isdigit():
                            continue
                        # 后面还没有字符（可能数字还在 streaming），也跳过
                        if pos + 1 >= len(buffer) and not force:
                            continue
                    earliest = pos

            if earliest == -1:
                if force and buffer.strip():
                    on_sentence(buffer.strip())
                    return ""
                return buffer

            sentence = buffer[:earliest + 1].strip()
            buffer = buffer[earliest + 1:]
            if sentence:
                on_sentence(sentence)

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
        memory_context: str = "",
    ) -> tuple[str, list[dict[str, Any]]]:
        client = self._get_anthropic_client()
        system = self._personalize_system(user_name, user_role, user_emotion, memory_context=memory_context)
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

                # Flush remaining buffer
                self._flush_sentences(buffer, on_sentence, force=True)

                if has_tool_use or not full_text.strip():
                    reason = "tool use" if has_tool_use else "empty stream"
                    self.logger.info("%s detected, falling back to chat()", reason)
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
                        memory_context=memory_context,
                    )

                messages.append({
                    "role": "assistant",
                    "content": [{"type": "text", "text": full_text}],
                })
                self.logger.info("Anthropic stream complete: %s", full_text[:200])
                return full_text, messages

        except Exception as exc:
            self.logger.warning("Streaming failed: %s, falling back to chat()", exc)
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
                memory_context=memory_context,
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
        memory_context: str = "",
    ) -> tuple[str, list[dict[str, Any]]]:
        client = self._get_openai_client()
        system = self._personalize_system(user_name, user_role, user_emotion, memory_context=memory_context)

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
        }
        if openai_tools:
            kwargs["tools"] = openai_tools

        self.logger.info("Streaming request to OpenAI (%s)", self.model)

        stored_messages = list(conversation_history or [])
        stored_messages.append({"role": "user", "content": user_message})

        try:
            response = self._call_with_retry(
                lambda: client.chat.completions.create(**kwargs)
            )
            full_text = ""
            buffer = ""
            has_tool_calls = False

            for chunk in response:
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
                return self._chat_openai(
                    user_message,
                    conversation_history=conversation_history,
                    tools=tools,
                    tool_executor=tool_executor,
                    user_name=user_name,
                    user_id=user_id,
                    user_role=user_role,
                    user_emotion=user_emotion,
                    memory_context=memory_context,
                )

            stored_messages.append({
                "role": "assistant",
                "content": [{"type": "text", "text": full_text}],
            })
            self.logger.info("OpenAI stream complete: %s", full_text[:200])
            return full_text, stored_messages

        except Exception as exc:
            self.logger.warning("Streaming failed: %s, falling back to chat()", exc)
            return self._chat_openai(
                user_message,
                conversation_history=conversation_history,
                tools=tools,
                tool_executor=tool_executor,
                user_name=user_name,
                user_id=user_id,
                user_role=user_role,
                user_emotion=user_emotion,
                memory_context=memory_context,
            )

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _personalize_system(
        self, user_name: str | None, user_role: str, user_emotion: str = "",
        memory_context: str = "",
    ) -> str:
        return build_personality_prompt(
            user_name=user_name,
            user_role=user_role,
            user_emotion=user_emotion,
            memory_context=memory_context,
        )
