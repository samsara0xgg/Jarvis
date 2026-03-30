"""意图路由器 — 一次云端调用完成分类+参数提取+回复生成.

三层 fallback：Groq → DeepSeek → 本地 Ollama.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from core.local_llm import LocalLLM

LOGGER = logging.getLogger(__name__)

VALID_INTENTS = {"smart_home", "info_query", "time", "complex", "uncertain"}

# 设备能力描述模板，运行时从 config 动态生成
_DEVICE_ACTIONS = {
    "light": "turn_on / turn_off / set_brightness(0-100)",
    "door_lock": "lock / unlock",
    "thermostat": "turn_on / turn_off / set_temperature(16-30)",
}


def build_system_prompt(config: dict) -> str:
    """从 config 动态生成 system prompt，包含设备列表."""
    devices_desc = []
    for dev in config.get("devices", {}).get("sim_devices", []):
        did = dev["device_id"]
        name = dev.get("name", did)
        dtype = dev.get("device_type", "unknown")
        actions = _DEVICE_ACTIONS.get(dtype, "unknown")
        devices_desc.append(f"- {did}（{name}）: {actions}")

    device_list = "\n".join(devices_desc)

    return f"""你是智能家居语音助手的意图分析器。分析用户指令并返回 JSON。

可用设备：
{device_list}

返回 JSON 格式（不要输出其他内容）：

smart_home 示例：
{{"intent":"smart_home","confidence":0.95,"actions":[{{"device_id":"living_room_light","action":"turn_on","value":null}}],"response":"好的，已帮你打开客厅灯。"}}

多设备示例（"开灯和空调"）：
{{"intent":"smart_home","confidence":0.95,"actions":[{{"device_id":"living_room_light","action":"turn_on","value":null}},{{"device_id":"home_thermostat","action":"turn_on","value":null}}],"response":"好的，已帮你打开客厅灯和空调。"}}

所有灯示例（"关掉所有灯"）：
{{"intent":"smart_home","confidence":0.95,"actions":[{{"device_id":"living_room_light","action":"turn_off","value":null}},{{"device_id":"bedroom_light","action":"turn_off","value":null}},{{"device_id":"study_light","action":"turn_off","value":null}},{{"device_id":"living_room_group","action":"turn_off","value":null}}],"response":"好的，所有灯已关闭。"}}

隐含意图示例（"太冷了"=调高空调）：
{{"intent":"smart_home","confidence":0.9,"actions":[{{"device_id":"home_thermostat","action":"set_temperature","value":26}}],"response":"好的，空调已调到26度。"}}

info_query 示例：
{{"intent":"info_query","confidence":0.9,"sub_type":"stocks","query":["NVDA"],"response":null}}
{{"intent":"info_query","confidence":0.9,"sub_type":"news","query":"AI","response":null}}
{{"intent":"info_query","confidence":0.9,"sub_type":"weather","query":null,"response":null}}

time 示例：
{{"intent":"time","confidence":0.95,"sub_type":"current_time","response":null}}

complex 示例（闲聊/写作/分析/情感表达/抽象讨论）：
{{"intent":"complex","confidence":0.85,"response":null}}

uncertain 示例（无意义输入）：
{{"intent":"uncertain","confidence":0.3,"response":null}}

关键规则：
- "你太冷漠了""我感觉很温暖""把这个问题关闭" → complex（情感/抽象，不是设备控制）
- "有点暗""看不清" → smart_home（开灯）
- "好热""太冷" → smart_home（调空调）
- 只输出 JSON，不要输出其他内容。"""


@dataclass
class RouteResult:
    """路由结果."""

    tier: str
    intent: str
    confidence: float
    duration_ms: int
    provider: str
    actions: list[dict[str, Any]] = field(default_factory=list)
    response: str | None = None
    sub_type: str | None = None
    query: Any = None


class IntentRouter:
    """三层 fallback 意图路由器：Groq → DeepSeek → 本地 Ollama."""

    def __init__(self, config: dict) -> None:
        self.config = config
        self.system_prompt = build_system_prompt(config)
        self.logger = LOGGER

        # Groq
        groq_cfg = config.get("models", {}).get("groq", {})
        self.groq_key = groq_cfg.get("api_key") or os.environ.get("GROQ_API_KEY", "")
        self.groq_model = groq_cfg.get("model", "llama-3.3-70b-versatile")
        self.groq_url = "https://api.groq.com/openai/v1/chat/completions"

        # DeepSeek
        ds_cfg = config.get("models", {}).get("deepseek", {})
        self.deepseek_key = ds_cfg.get("api_key") or os.environ.get("DEEPSEEK_API_KEY", "")
        self.deepseek_model = ds_cfg.get("model", "deepseek-chat")
        self.deepseek_url = "https://api.deepseek.com/chat/completions"

        # 本地 Ollama
        self.local_llm = LocalLLM(config)

        self.logger.info(
            "IntentRouter: groq=%s, deepseek=%s, local=%s",
            "ready" if self.groq_key else "no key",
            "ready" if self.deepseek_key else "no key",
            "ready" if self.local_llm.is_available() else "unavailable",
        )

    def route(self, text: str) -> RouteResult:
        """分析用户指令。Groq → DeepSeek → 本地."""
        start = time.time()

        # 1. Groq
        if self.groq_key:
            result = self._call_cloud(self.groq_url, self.groq_key, self.groq_model, text, start)
            if result:
                result.provider = "groq"
                return result

        # 2. DeepSeek
        if self.deepseek_key:
            result = self._call_cloud(self.deepseek_url, self.deepseek_key, self.deepseek_model, text, start)
            if result:
                result.provider = "deepseek"
                return result

        # 3. 本地 Ollama
        if self.local_llm.is_available():
            raw = self.local_llm.generate(prompt=text, system=self.system_prompt)
            result = self._parse_json_response(raw, start, "local")
            if result:
                return result

        # 全部失败
        return RouteResult(
            tier="cloud", intent="complex", confidence=0.0,
            duration_ms=int((time.time() - start) * 1000), provider="none",
        )

    def _call_cloud(
        self, url: str, api_key: str, model: str, text: str, start: float,
    ) -> RouteResult | None:
        """调用云端 API."""
        try:
            resp = requests.post(
                url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": self.system_prompt},
                        {"role": "user", "content": text},
                    ],
                    "temperature": 0,
                    "max_tokens": 200,
                    "response_format": {"type": "json_object"},
                },
                timeout=5,
            )

            if resp.status_code == 429:
                self.logger.warning("Rate limited by %s", url)
                return None

            resp.raise_for_status()
            data = resp.json()
            raw = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
            )
            if not raw:
                return None

            return self._parse_json_response(raw, start, "cloud")

        except Exception as exc:
            self.logger.warning("Cloud call failed (%s): %s", url, exc)
            return None

    def _parse_json_response(self, raw: str, start: float, provider: str) -> RouteResult | None:
        """解析 JSON 响应为 RouteResult."""
        try:
            # 清理：有时模型会在 JSON 外包裹 markdown
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            self.logger.warning("Invalid JSON from %s: %s", provider, raw[:100])
            return None

        intent = parsed.get("intent", "uncertain")
        if intent not in VALID_INTENTS:
            intent = "uncertain"

        confidence = float(parsed.get("confidence", 0.5))
        actions = parsed.get("actions", [])
        response = parsed.get("response")
        sub_type = parsed.get("sub_type")
        query = parsed.get("query")

        if intent in ("smart_home", "info_query", "time"):
            tier = "local"
        else:
            tier = "cloud"

        duration_ms = int((time.time() - start) * 1000)

        self.logger.info(
            "Route(%s): '%s' → %s/%s (%.2f, %dms, %d actions)",
            provider, raw[:30] if len(raw) > 30 else raw,
            tier, intent, confidence, duration_ms, len(actions),
        )

        return RouteResult(
            tier=tier, intent=intent, confidence=confidence,
            duration_ms=duration_ms, provider=provider,
            actions=actions, response=response,
            sub_type=sub_type, query=query,
        )
