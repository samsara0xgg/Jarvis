"""Observer — LLM-based observation extraction for Jarvis v2.

Uses LLM function calling to extract structured observations from
conversation turns. Runs asynchronously on the cold path (after the
user hears the response).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime

import requests

LOGGER = logging.getLogger(__name__)

# Reuse HTTP connections (same pattern as memory/manager.py)
_SESSION = requests.Session()

OBSERVER_SYSTEM_PROMPT = """\
You are the memory consciousness of an AI assistant.
Your observations will be the ONLY information the assistant has about past interactions.

## YOUR JOB
Extract structured observations from the conversation below.
Call the `record_observations` tool with your results.
ALWAYS respond in Chinese (中文). English output will be rejected.

## PRIORITY EMOJI
- 🔴 HIGH: explicit user facts/preferences, unresolved goals, critical context
- 🟡 MEDIUM: learned info, tool results, mild observations, user emotions
- 🟢 LOW: minor, uncertain, speculative
- ✅ DONE: task completed, question answered, issue resolved

## FORMAT RULES
- Each observation MUST have: priority (emoji), time (HH:MM 24h), text (中文)
- text field: 用中文撰写, 第三人称描述, 简洁 (10-50 字理想)
- Use the TIME from the message that triggered this observation

## CRITICAL: DISTINGUISH USER ASSERTIONS FROM QUESTIONS
- "我对虾过敏" → 🔴 assertion: 用户声明对虾过敏
- "虾过敏严重吗？" → question, 不要当作断言

## STATE CHANGES
If user indicates change, frame as state change that supersedes:
- "我不在 Acme 了换到 Stripe" → 🔴 用户从 Acme 换到 Stripe (不再在 Acme)

## PRESERVE UNUSUAL PHRASING
- 用户说 "累死了" → observation 写 "用户说累死了" 或 "用户疲惫 (原话: 累死了)"
- 不要"洗成"教科书普通话

## PRECISE VERBS — 动词保真
动词必须忠于原意·不弱化·不强化·不推断。
- "我买了 X" → "用户买了 X" ✓
- "我讨厌 Y" → "用户讨厌 Y" ✓

## DETAILS IN ASSISTANT CONTENT — 保留具体信息
assistant 生成的具体数值·名称·参数必须保留进 observation。
- assistant "已调为暖黄 2700K" → observation 应记 "2700K 暖黄"

## EMOTION DETECTION
If user message has emotion hint (tired/angry/happy/...) → add 🟡 observation

## USER ASSERTIONS ARE AUTHORITATIVE
User assertions are authoritative. The question doesn't invalidate an assertion.

## OUTPUT
Call tool `record_observations` ONLY. Do not output free text."""

OBSERVER_TOOL_SCHEMA = {
    "name": "record_observations",
    "description": "Record observations extracted from the conversation.",
    "parameters": {
        "type": "object",
        "properties": {
            "observations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "priority": {
                            "type": "string",
                            "enum": ["🔴", "🟡", "🟢", "✅"],
                        },
                        "time": {
                            "type": "string",
                            "description": "HH:MM 24h format",
                        },
                        "text": {
                            "type": "string",
                            "description": "Observation text in Chinese",
                        },
                    },
                    "required": ["priority", "time", "text"],
                },
            },
        },
        "required": ["observations"],
    },
}


class Observer:
    """Extract structured observations from conversation turns via LLM.

    Uses OpenAI-compatible function calling API (works with xAI Grok,
    OpenAI, Groq). Tries primary model first, falls back on failure.

    Args:
        config: Application config dict with llm and memory sections.
    """

    def __init__(self, config: dict) -> None:
        obs_cfg = config.get("memory", {}).get("observer", {})
        self._primary_model = obs_cfg.get(
            "primary_model", "grok-4.20-0309-non-reasoning",
        )
        self._fallback_model = obs_cfg.get("fallback_model", "gemini-2.5-flash")
        self._enabled = obs_cfg.get("enabled", True)

        # Per-model endpoint + key. Primary and fallback can point at different
        # providers (e.g. xAI for primary, Gemini OpenAI-compat for fallback).
        # Backward compat: if a per-model key_env / base_url isn't set, fall
        # back to the legacy llm.{base_url, api_key} — that's the old single-
        # endpoint behaviour.
        llm_cfg = config.get("llm", {})
        legacy_base = llm_cfg.get("base_url", "https://api.x.ai/v1")
        legacy_key = llm_cfg.get("api_key", "") or os.environ.get("XAI_API_KEY", "")

        self._primary_base_url = obs_cfg.get("primary_base_url") or legacy_base
        primary_env = obs_cfg.get("primary_api_key_env", "")
        self._primary_api_key = (
            os.environ.get(primary_env, "") if primary_env else legacy_key
        )

        self._fallback_base_url = obs_cfg.get("fallback_base_url") or legacy_base
        fallback_env = obs_cfg.get("fallback_api_key_env", "")
        self._fallback_api_key = (
            os.environ.get(fallback_env, "") if fallback_env else legacy_key
        )

    def extract(self, turn_data: dict) -> list[dict]:
        """Extract 0-N observations from a conversation turn.

        Args:
            turn_data: Dict with user_text, assistant_text, and optional
                tool_calls and user_emotion.

        Returns:
            List of observation dicts with priority, time, text.
            On failure, logs warning and returns [].
        """
        if not self._enabled:
            return []

        messages = self._build_prompt(turn_data)

        # Try primary model
        result = self._call_llm(
            messages,
            self._primary_model,
            self._primary_base_url,
            self._primary_api_key,
        )
        if result is not None:
            return result

        # Fallback — use fallback endpoint + key (may be a different provider)
        LOGGER.warning(
            "Observer primary model %s failed, trying fallback %s",
            self._primary_model,
            self._fallback_model,
        )
        result = self._call_llm(
            messages,
            self._fallback_model,
            self._fallback_base_url,
            self._fallback_api_key,
        )
        if result is not None:
            return result

        LOGGER.warning("Observer fallback model also failed, returning []")
        return []

    def _build_prompt(self, turn_data: dict) -> list[dict]:
        """Build messages list for the LLM call.

        Args:
            turn_data: Conversation turn data.

        Returns:
            List of message dicts: [system prompt, user message].
        """
        parts = [f"用户：{turn_data.get('user_text', '')}"]

        tool_calls = turn_data.get("tool_calls")
        if tool_calls:
            for tc in tool_calls:
                name = tc.get("name", "")
                args = tc.get("args", "")
                result = str(tc.get("result", ""))[:200]
                parts.append(f"[工具调用: {name}({args}) → {result}]")

        parts.append(f"助手：{turn_data.get('assistant_text', '')}")

        emotion = turn_data.get("user_emotion")
        if emotion:
            parts.append(f"[用户情绪检测: {emotion}]")

        return [
            {"role": "system", "content": OBSERVER_SYSTEM_PROMPT},
            {"role": "user", "content": "\n".join(parts)},
        ]

    def _call_llm(
        self,
        messages: list[dict],
        model: str,
        base_url: str,
        api_key: str,
    ) -> list[dict] | None:
        """Call LLM with function calling to extract observations.

        Args:
            messages: Chat messages list.
            model: Model identifier.
            base_url: OpenAI-compatible API base URL for this model.
            api_key: Bearer token for ``base_url``.

        Returns:
            List of observation dicts, or None on failure.
        """
        try:
            resp = _SESSION.post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": messages,
                    "temperature": 0,
                    "max_tokens": 1024,
                    "tools": [{"type": "function", "function": OBSERVER_TOOL_SCHEMA}],
                    "tool_choice": {
                        "type": "function",
                        "function": {"name": "record_observations"},
                    },
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            args_str = (
                data["choices"][0]["message"]["tool_calls"][0]
                ["function"]["arguments"]
            )
            parsed = json.loads(args_str)
            return parsed.get("observations", [])
        except Exception:
            LOGGER.warning("Observer _call_llm failed for model=%s", model, exc_info=True)
            return None

    def format_markdown(self, observations: list[dict]) -> str:
        """Format observations into markdown.

        Args:
            observations: List of observation dicts.

        Returns:
            Formatted markdown string, or empty string if no observations.
        """
        if not observations:
            return ""

        today = datetime.now().strftime("%Y-%m-%d")
        lines = [f"Date: {today}"]
        for obs in observations:
            lines.append(f"* {obs['priority']} ({obs['time']}) {obs['text']}")
        return "\n".join(lines)
