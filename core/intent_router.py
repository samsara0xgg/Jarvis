"""意图路由器 — 一次云端调用完成分类+参数提取+回复生成.

两层 fallback：Groq 8B → Cerebras 8B → 直接走云端 LLM.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import requests

LOGGER = logging.getLogger(__name__)

# 复用 HTTP 连接（IntentRouter 仅在主循环单线程调用，无需线程安全）
_SESSION = requests.Session()

VALID_INTENTS = {"smart_home", "info_query", "time", "complex", "uncertain", "automation"}

# Strip punctuation for route cache key normalization
_PUNCT_RE = re.compile(r'[。，！？、；：\u201c\u201d\u2018\u2019\u2026\u2014\u00b7.!?,;:\s]+')


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

    return f"""你是Jarvis，私人AI助手。性格简洁、略带幽默。分析用户指令，返回JSON。
response字段用中文，语气简洁自然（如"好的，灯开了。"而不是"好的，我已经帮你把客厅的灯打开了。"）

设备：
{device_list}

JSON格式：
smart_home: {{"intent":"smart_home","confidence":0.95,"actions":[{{"device_id":"xxx","action":"turn_on","value":null}}],"response":"好的，已开灯。"}}
info_query: {{"intent":"info_query","confidence":0.9,"sub_type":"news|stocks|weather","query":"AI","response":null}}
time: {{"intent":"time","confidence":0.95,"sub_type":"current_time|date|weekday","response":null}}
automation: {{"intent":"automation","confidence":0.9,"sub_type":"create|list|delete","rule":{{"name":"晚安模式","trigger":{{"type":"keyword","keyword":"晚安"}},"actions":[{{"device_id":"xxx","action":"turn_off","value":null}}]}},"response":"好的，以后说晚安就会关灯。"}}
complex: {{"intent":"complex","confidence":0.85,"response":null}}
uncertain: {{"intent":"uncertain","confidence":0.3,"response":null}}

automation trigger类型：
- keyword: {{"type":"keyword","keyword":"晚安"}} — 用户说这个词时触发
- cron: {{"type":"cron","hour":7,"minute":0,"days":"everyday|weekdays|weekends"}} — 定时触发
- once: {{"type":"once","delay_minutes":30}} — 一次性延时触发

规则：
- 多设备用actions数组，如"开灯和空调"输出两个action
- "所有灯"=列出全部灯的device_id
- 隐含意图："有点暗"=开灯，"好热"/"太冷"=调空调
- 情感/抽象表达→complex，如"你太冷漠了""把这个问题关闭"
- 记忆/个人信息→complex：含"记住""记下""别忘了""我喜欢""我要去""我的xx是"等个人信息、偏好、计划一律走complex
- 关于用户自身的提问→complex：如"我喜欢喝什么""我最近有什么安排""我上次说了什么"
- 需要工具调用的查询→complex：如"查汇率""换算货币""翻译""计算"等需要外部工具才能回答的问题
- 只输出JSON"""


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
    rule: dict[str, Any] | None = None


class IntentRouter:
    """两层 fallback 意图路由器：Groq 8B → Cerebras 8B → 直接走云端 LLM."""

    def __init__(self, config: dict, tracker: Any = None) -> None:
        self.config = config
        self.system_prompt = build_system_prompt(config)
        self.logger = LOGGER
        self._tracker = tracker
        self._route_cache: OrderedDict = OrderedDict()
        self._cache_max = 256

        # Groq (primary)
        groq_cfg = config.get("models", {}).get("groq", {})
        self.groq_key = groq_cfg.get("api_key") or os.environ.get("GROQ_API_KEY", "")
        self.groq_model = groq_cfg.get("router_model", "llama-3.3-70b-versatile")
        self.groq_url = "https://api.groq.com/openai/v1/chat/completions"

        # Cerebras (fallback)
        cerebras_cfg = config.get("models", {}).get("cerebras", {})
        self.cerebras_key = cerebras_cfg.get("api_key") or os.environ.get("CEREBRAS_API_KEY", "")
        self.cerebras_model = cerebras_cfg.get("router_model", "llama3.1-8b")
        self.cerebras_url = "https://api.cerebras.ai/v1/chat/completions"

        self.logger.info(
            "IntentRouter: groq=%s, cerebras=%s",
            "ready" if self.groq_key else "no key",
            "ready" if self.cerebras_key else "no key",
        )

    def _cache_result(self, key: str, result: RouteResult) -> None:
        """Store a successful route result in LRU cache."""
        if result.provider == "none":
            return
        self._route_cache[key] = copy.copy(result)
        if len(self._route_cache) > self._cache_max:
            self._route_cache.popitem(last=False)

    def route(self, text: str) -> RouteResult:
        """分析用户指令。Groq 8B → Cerebras 8B → 直接走云端 LLM."""
        # TODO: 目前只做 strip 标点，未来可探索模糊匹配（embedding 相似度等），
        #       但需注意 "开灯" vs "关灯" 语义相近却意图相反的问题。
        key = _PUNCT_RE.sub("", text.strip())

        # Cache hit
        if key in self._route_cache:
            self._route_cache.move_to_end(key)
            cached = self._route_cache[key]
            self.logger.info("Route cache hit: '%s' → %s/%s", key[:20], cached.tier, cached.intent)
            return copy.copy(cached)

        start = time.time()

        # 1. Groq (primary)
        if self.groq_key and (not self._tracker or self._tracker.is_available("intent.groq")):
            result = self._call_cloud(self.groq_url, self.groq_key, self.groq_model, text, start)
            if result:
                if self._tracker:
                    self._tracker.record_success("intent.groq")
                result.provider = "groq"
                self._cache_result(key, result)
                return result
            if self._tracker:
                self._tracker.record_failure("intent.groq")

        # 2. Cerebras (fallback)
        if self.cerebras_key and (not self._tracker or self._tracker.is_available("intent.cerebras")):
            result = self._call_cloud(self.cerebras_url, self.cerebras_key, self.cerebras_model, text, start)
            if result:
                if self._tracker:
                    self._tracker.record_success("intent.cerebras")
                result.provider = "cerebras"
                self._cache_result(key, result)
                return result
            if self._tracker:
                self._tracker.record_failure("intent.cerebras")

        # 都失败 → 直接走云端 LLM (don't cache)
        return RouteResult(
            tier="cloud", intent="complex", confidence=0.0,
            duration_ms=int((time.time() - start) * 1000), provider="none",
        )

    def _call_cloud(
        self, url: str, api_key: str, model: str, text: str, start: float,
    ) -> RouteResult | None:
        """调用云端 API."""
        try:
            resp = _SESSION.post(
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
        rule = parsed.get("rule")

        if intent in ("smart_home", "info_query", "time", "automation"):
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
            sub_type=sub_type, query=query, rule=rule,
        )
