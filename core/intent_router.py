"""意图路由器 — 一次云端调用完成分类+参数提取+回复生成.

两层 fallback：Groq 70B → Cerebras 70B → 直接走云端 LLM.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import requests

LOGGER = logging.getLogger(__name__)

# 复用 HTTP 连接（IntentRouter 仅在主循环单线程调用，无需线程安全）
_SESSION = requests.Session()

VALID_INTENTS = {"smart_home", "info_query", "time", "complex", "uncertain", "automation", "chat"}

# Strip punctuation for route cache key normalization
_PUNCT_RE = re.compile(r'[。，！？、；：\u201c\u201d\u2018\u2019\u2026\u2014\u00b7.!?,;:]+')

# Minimal traditional→simplified map for common command vocabulary.
# Goal: "開燈" and "开灯" hit the same cache entry without pulling in
# opencc/zhconv. Not a full converter — extend when real misses show up.
_TRAD_TO_SIMP = str.maketrans({
    "開": "开", "關": "关", "燈": "灯", "溫": "温", "設": "设", "調": "调",
    "亮": "亮", "暗": "暗", "暖": "暖", "涼": "凉", "風": "风", "電": "电",
    "幾": "几", "點": "点", "時": "时", "鐘": "钟", "間": "间", "秒": "秒",
    "過": "过", "後": "后", "來": "来", "這": "这", "個": "个", "誰": "谁",
    "麼": "么", "嗎": "吗", "還": "还", "會": "会", "對": "对", "請": "请",
    "說": "说", "話": "话", "問": "问", "記": "记", "聽": "听", "讀": "读",
    "寫": "写", "門": "门", "車": "车", "長": "长", "給": "给", "應": "应",
    "響": "响", "調": "调", "濕": "湿", "氣": "气",
})


def _normalize_cache_key(text: str) -> str:
    """Normalize text into a stable cache key.

    Applies NFKC (folds full-width → half-width, canonical compositions),
    maps common traditional Chinese chars to simplified, strips punctuation,
    and collapses whitespace. Semantically-different phrasings ('开灯' vs
    '开一下灯') are intentionally NOT collapsed — that needs embeddings.
    """
    normalized = unicodedata.normalize("NFKC", text.strip())
    simplified = normalized.translate(_TRAD_TO_SIMP)
    return " ".join(_PUNCT_RE.sub("", simplified).split())


# 设备能力描述模板，运行时从 config 动态生成
_DEVICE_ACTIONS = {
    "light": "turn_on / turn_off / set_brightness(0-100)",
    "color_light": "turn_on / turn_off / set_brightness(0-100) / set_color(用户原话传入，如red/蓝色/Tiffany蓝/珊瑚色/暖白) / set_color_temp(warm/neutral/cool) / set_effect(colorloop/none)",
    "door_lock": "lock / unlock",
    "thermostat": "turn_on / turn_off / set_temperature(16-30)",
}


def build_system_prompt(config: dict) -> str:
    """从 config 动态生成 system prompt，包含设备列表."""
    devices_desc = []
    mode = config.get("devices", {}).get("mode", "sim")

    if mode == "live":
        hue_config = config.get("hue", {})
        color_devices = set(hue_config.get("color_capable", []))
        for did, aliases in hue_config.get("light_aliases", {}).items():
            alias_list = aliases if isinstance(aliases, list) else [aliases]
            chinese_aliases = [a for a in alias_list if not a.startswith("Hue ")]
            name = chinese_aliases[0] if chinese_aliases else did
            actions = _DEVICE_ACTIONS["color_light"] if did in color_devices else _DEVICE_ACTIONS["light"]
            devices_desc.append(f"- {did}（{name}）: {actions}")
        for did, aliases in hue_config.get("group_aliases", {}).items():
            alias_list = aliases if isinstance(aliases, list) else [aliases]
            chinese_aliases = [a for a in alias_list if not a.startswith(("Hue ", "5 AM", "Gaming"))]
            name = chinese_aliases[0] if chinese_aliases else did
            actions = _DEVICE_ACTIONS["color_light"] if did in color_devices else _DEVICE_ACTIONS["light"]
            devices_desc.append(f"- {did}（{name}，灯组）: {actions}")
    else:
        for dev in config.get("devices", {}).get("sim_devices", []):
            did = dev["device_id"]
            name = dev.get("name", did)
            dtype = dev.get("device_type", "unknown")
            actions = _DEVICE_ACTIONS.get(dtype, "unknown")
            devices_desc.append(f"- {did}（{name}）: {actions}")

    device_list = "\n".join(devices_desc)

    return f"""你是小月，私人AI管家。性格简洁、略带幽默。分析用户指令，返回JSON。
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
- 上下文设备推断：如果用户没指定设备名，根据对话上下文推断是哪个设备。只有上下文也不明确时才用 all_lights
- 隐含意图："有点暗"=开灯，"好热"/"太冷"=调空调
- 情感/抽象表达→complex，如"你太冷漠了""把这个问题关闭"
- 记忆/个人信息→complex：含"记住""记下""别忘了""我喜欢""我要去""我的xx是"等个人信息、偏好、计划一律走complex
- 关于用户自身的提问→complex：如"我喜欢喝什么""我最近有什么安排""我上次说了什么"
- 需要工具调用的查询→complex：如"查汇率""换算货币""翻译""计算"等需要外部工具才能回答的问题
- 只输出JSON"""


def build_unified_prompt(
    config: dict,
    memory_context: str = "",
    user_emotion: str = "",
) -> str:
    """Build a system prompt for unified route+respond (single Groq call).

    For device/info/time/automation intents the model outputs JSON (same schema
    as ``build_system_prompt``).  For everything else it outputs a natural
    language response directly, avoiding the second LLM round-trip.

    Args:
        config: Application config dict.
        memory_context: Optional memory context from MemoryManager.query().
        user_emotion: Optional detected emotion label (e.g. "HAPPY").
    """
    from core.personality import get_short_personality

    # Reuse device enumeration logic from build_system_prompt
    devices_desc: list[str] = []
    mode = config.get("devices", {}).get("mode", "sim")

    if mode == "live":
        hue_config = config.get("hue", {})
        color_devices = set(hue_config.get("color_capable", []))
        for did, aliases in hue_config.get("light_aliases", {}).items():
            alias_list = aliases if isinstance(aliases, list) else [aliases]
            chinese_aliases = [a for a in alias_list if not a.startswith("Hue ")]
            name = chinese_aliases[0] if chinese_aliases else did
            actions = _DEVICE_ACTIONS["color_light"] if did in color_devices else _DEVICE_ACTIONS["light"]
            devices_desc.append(f"- {did}（{name}）: {actions}")
        for did, aliases in hue_config.get("group_aliases", {}).items():
            alias_list = aliases if isinstance(aliases, list) else [aliases]
            chinese_aliases = [a for a in alias_list if not a.startswith(("Hue ", "5 AM", "Gaming"))]
            name = chinese_aliases[0] if chinese_aliases else did
            actions = _DEVICE_ACTIONS["color_light"] if did in color_devices else _DEVICE_ACTIONS["light"]
            devices_desc.append(f"- {did}（{name}，灯组）: {actions}")
    else:
        for dev in config.get("devices", {}).get("sim_devices", []):
            did = dev["device_id"]
            name = dev.get("name", did)
            dtype = dev.get("device_type", "unknown")
            actions = _DEVICE_ACTIONS.get(dtype, "unknown")
            devices_desc.append(f"- {did}（{name}）: {actions}")

    device_list = "\n".join(devices_desc)
    personality = get_short_personality()

    # Optional sections
    memory_section = ""
    if memory_context:
        memory_section = f"\n\n你对用户的了解：\n{memory_context}"

    emotion_section = ""
    if user_emotion:
        emotion_section = f"\n用户当前情绪：{user_emotion}"

    return f"""{personality}{memory_section}{emotion_section}

你同时负责意图路由和回复生成。根据用户消息：

1. 设备控制 → 输出JSON：{{"intent":"smart_home","confidence":0.95,"actions":[{{"device_id":"xxx","action":"turn_on","value":null}}],"response":"好的，已开灯。"}}
2. 信息查询（仅限天气/股票/新闻这三种）→ 输出JSON：{{"intent":"info_query","confidence":0.9,"sub_type":"news|stocks|weather","query":"AI","response":null}}
   其他查询（门票、翻译、百科等）→ 直接自然语言回复，不要输出info_query JSON
3. 时间查询 → 输出JSON：{{"intent":"time","confidence":0.95,"sub_type":"current_time|date|weekday","response":null}}
4. 自动化规则 → 输出一个JSON（永远只输出一个JSON，不要输出多个）：
   持久规则（"以后每次…""每天…"）：{{"intent":"automation","confidence":0.9,"sub_type":"create","rule":{{"name":"晚安模式","trigger":{{"type":"keyword","keyword":"晚安"}},"actions":[{{"device_id":"xxx","action":"turn_off"}}]}},"response":"好的，以后说晚安就会关灯。"}}
   一次性延时（"关灯过3秒再开灯"）：{{"intent":"automation","confidence":0.9,"sub_type":"create","rule":{{"name":"延时操作","trigger":{{"type":"once","delay_minutes":0.05}},"actions":[{{"device_id":"all_lights","action":"turn_off"}},{{"device_id":"all_lights","action":"turn_on","delay_seconds":3}}]}},"response":"好的，先关灯，3秒后开灯。"}}
5. 其他所有情况 → 直接用自然语言回复，不要输出JSON

设备：
{device_list}

规则：
- 多设备用actions数组
- "所有灯"=列出全部灯的device_id
- 上下文设备推断：如果用户没指定设备名，根据对话上下文推断是哪个设备
- 隐含意图："有点暗"=开灯，"好热"=调空调
- 记忆/个人信息相关问题→直接回复
- 只有1-4类输出JSON，其他一律自然语言回复"""


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
    query: str | None = None
    rule: dict[str, Any] | None = None
    text_response: str | None = None


class IntentRouter:
    """两层 fallback 意图路由器：Groq 70B → Cerebras 70B → 直接走云端 LLM."""

    def __init__(self, config: dict, tracker: Any = None) -> None:
        self.config = config
        self.system_prompt = build_system_prompt(config)
        self.logger = LOGGER
        self._tracker = tracker
        self._route_cache: OrderedDict = OrderedDict()
        self._cache_max = 256
        # Trace attributes for system testing
        self._last_cache_hit: bool = False
        self._last_provider_attempts: list[dict] = []
        self._last_raw_response: str = ""
        self._last_prompt: str = ""

        # Groq (primary)
        groq_cfg = config.get("models", {}).get("groq", {})
        self.groq_key = groq_cfg.get("api_key") or os.environ.get("GROQ_API_KEY", "")
        self.groq_model = groq_cfg.get("router_model", "llama-3.3-70b-versatile")
        self.groq_url = "https://api.groq.com/openai/v1/chat/completions"

        # Cerebras (fallback)
        cerebras_cfg = config.get("models", {}).get("cerebras", {})
        self.cerebras_key = cerebras_cfg.get("api_key") or os.environ.get("CEREBRAS_API_KEY", "")
        self.cerebras_model = cerebras_cfg.get("router_model", "llama-3.3-70b")
        self.cerebras_url = "https://api.cerebras.ai/v1/chat/completions"

        self.logger.info(
            "IntentRouter: groq=%s, cerebras=%s",
            "ready" if self.groq_key else "no key",
            "ready" if self.cerebras_key else "no key",
        )

    @property
    def cache_size(self) -> int:
        """Return the number of entries in the route cache."""
        return len(self._route_cache)

    def _cache_result(self, key: str, result: RouteResult) -> None:
        """Store a successful route result in LRU cache."""
        if result.provider == "none":
            return
        self._route_cache[key] = copy.copy(result)
        if len(self._route_cache) > self._cache_max:
            self._route_cache.popitem(last=False)

    def _all_providers_failed(self, key: str, start: float) -> RouteResult:
        """All intent providers failed. Prefer a stale cached result over
        dumping the request onto the cloud LLM — offline/partial outages
        shouldn't kill routine commands like '开灯'."""
        if key in self._route_cache:
            cached = copy.copy(self._route_cache[key])
            cached.provider = "cache_fallback"
            self.logger.warning(
                "All intent providers down — returning stale cache for '%s' → %s/%s",
                key[:20], cached.tier, cached.intent,
            )
            return cached
        self.logger.warning(
            "All intent providers down and no cached match for '%s' — "
            "falling through to cloud LLM", key[:20],
        )
        return RouteResult(
            tier="cloud", intent="complex", confidence=0.0,
            duration_ms=int((time.time() - start) * 1000), provider="none",
        )

    def route(self, text: str, conversation_history: list[dict] | None = None) -> RouteResult:
        """分析用户指令。Groq 70B → Cerebras 70B → 直接走云端 LLM."""
        # 未来可探索模糊匹配（embedding 相似度等），但需注意 "开灯" vs "关灯"
        # 语义相近却意图相反的问题。
        key = _normalize_cache_key(text)

        # Build recent context for ambiguous commands (e.g., "关了" after "灯带调黄色")
        recent_context = self._build_context(conversation_history) if conversation_history else []

        # Cache hit — only use cache when no conversation context (ambiguous commands need context)
        if not recent_context and key in self._route_cache:
            self._route_cache.move_to_end(key)
            cached = self._route_cache[key]
            self.logger.info("Route cache hit: '%s' → %s/%s", key[:20], cached.tier, cached.intent)
            return copy.copy(cached)

        start = time.time()

        # 1. Groq (primary)
        if self.groq_key and (not self._tracker or self._tracker.is_available("intent.groq")):
            result = self._call_cloud(self.groq_url, self.groq_key, self.groq_model, text, start, recent_context)
            if result:
                if self._tracker:
                    self._tracker.record_success("intent.groq")
                result.provider = "groq"
                if not recent_context:
                    self._cache_result(key, result)
                return result
            if self._tracker:
                self._tracker.record_failure("intent.groq")

        # 2. Cerebras (fallback)
        if self.cerebras_key and (not self._tracker or self._tracker.is_available("intent.cerebras")):
            result = self._call_cloud(self.cerebras_url, self.cerebras_key, self.cerebras_model, text, start, recent_context)
            if result:
                if self._tracker:
                    self._tracker.record_success("intent.cerebras")
                result.provider = "cerebras"
                if not recent_context:
                    self._cache_result(key, result)
                return result
            if self._tracker:
                self._tracker.record_failure("intent.cerebras")

        return self._all_providers_failed(key, start)

    def _build_context(self, history: list[dict]) -> list[dict]:
        """Extract last 2 turns of user/assistant messages for context."""
        relevant = []
        for m in history:
            if m.get("role") not in ("user", "assistant"):
                continue
            # Skip messages with tool_calls or non-string content
            content = m.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            if "tool_calls" in m:
                continue
            relevant.append({"role": m["role"], "content": content})
        return relevant[-4:]  # last 2 turns = 4 messages

    def _call_cloud(
        self, url: str, api_key: str, model: str, text: str, start: float,
        recent_context: list[dict] | None = None,
    ) -> RouteResult | None:
        """调用云端 API."""
        try:
            messages = [{"role": "system", "content": self.system_prompt}]
            if recent_context:
                messages.extend(recent_context)
            messages.append({"role": "user", "content": text})
            resp = _SESSION.post(
                url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": messages,
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

        except requests.Timeout:
            self.logger.warning("Timeout calling %s", url)
            return None
        except requests.RequestException as exc:
            self.logger.warning("Request failed (%s): %s", url, exc)
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

        if intent in ("smart_home", "info_query", "time", "automation") and confidence >= 0.90:
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

    # ------------------------------------------------------------------
    # Unified route + respond (single Groq call)
    # ------------------------------------------------------------------

    def route_and_respond(
        self,
        text: str,
        conversation_history: list[dict] | None = None,
        memory_context: str = "",
        user_emotion: str = "",
    ) -> RouteResult:
        """Single Groq call that routes AND generates a response.

        For device/info/time/automation intents the output is JSON (same as
        ``route()``).  For everything else the model responds in natural
        language, stored in ``RouteResult.text_response``, eliminating the
        need for a second LLM call.

        Falls back: Groq → Cerebras → empty result (provider="none").
        """
        # Reset trace
        self._last_cache_hit = False
        self._last_provider_attempts = []
        self._last_raw_response = ""

        key = _normalize_cache_key(text)
        recent_context = self._build_context(conversation_history) if conversation_history else []

        # Cache hit — only for structured routes (not free-text), no conversation context
        if not recent_context and key in self._route_cache:
            self._route_cache.move_to_end(key)
            cached = self._route_cache[key]
            self._last_cache_hit = True
            self.logger.info("Unified cache hit: '%s' → %s/%s", key[:20], cached.tier, cached.intent)
            return copy.copy(cached)

        system_prompt = build_unified_prompt(self.config, memory_context, user_emotion)
        self._last_prompt = system_prompt
        start = time.time()

        # 1. Groq (primary)
        if self.groq_key and (not self._tracker or self._tracker.is_available("intent.groq")):
            t_attempt = time.time()
            result = self._call_unified(
                self.groq_url, self.groq_key, self.groq_model, text, start, recent_context, system_prompt,
            )
            attempt_ms = int((time.time() - t_attempt) * 1000)
            if result:
                self._last_provider_attempts.append({"provider": "groq", "status": "ok", "ms": attempt_ms})
                if self._tracker:
                    self._tracker.record_success("intent.groq")
                result.provider = "groq"
                if not recent_context and result.text_response is None:
                    self._cache_result(key, result)
                return result
            self._last_provider_attempts.append({"provider": "groq", "status": "fail", "ms": attempt_ms})
            if self._tracker:
                self._tracker.record_failure("intent.groq")

        # 2. Cerebras (fallback)
        if self.cerebras_key and (not self._tracker or self._tracker.is_available("intent.cerebras")):
            t_attempt = time.time()
            result = self._call_unified(
                self.cerebras_url, self.cerebras_key, self.cerebras_model, text, start, recent_context, system_prompt,
            )
            attempt_ms = int((time.time() - t_attempt) * 1000)
            if result:
                self._last_provider_attempts.append({"provider": "cerebras", "status": "ok", "ms": attempt_ms})
                if self._tracker:
                    self._tracker.record_success("intent.cerebras")
                result.provider = "cerebras"
                if not recent_context and result.text_response is None:
                    self._cache_result(key, result)
                return result
            self._last_provider_attempts.append({"provider": "cerebras", "status": "fail", "ms": attempt_ms})
            if self._tracker:
                self._tracker.record_failure("intent.cerebras")

        return self._all_providers_failed(key, start)

    def _call_unified(
        self,
        url: str,
        api_key: str,
        model: str,
        text: str,
        start: float,
        recent_context: list[dict],
        system_prompt: str,
    ) -> RouteResult | None:
        """Call cloud API with unified route+respond prompt (no JSON mode)."""
        try:
            messages = [{"role": "system", "content": system_prompt}]
            if recent_context:
                messages.extend(recent_context)
            messages.append({"role": "user", "content": text})
            resp = _SESSION.post(
                url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": messages,
                    "temperature": 0.7,
                    "max_tokens": 500,
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

            self._last_raw_response = raw
            return self._parse_unified_response(raw, start)

        except requests.Timeout:
            self.logger.warning("Timeout calling %s", url)
            return None
        except requests.RequestException as exc:
            self.logger.warning("Request failed (%s): %s", url, exc)
            return None

    def _parse_unified_response(self, raw: str, start: float) -> RouteResult | None:
        """Parse unified response: JSON for structured intents, text for chat."""
        duration_ms = int((time.time() - start) * 1000)

        # Strip markdown fences if present, then try JSON parse
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        if cleaned.startswith("{"):
            # Multiple JSON objects → take only the first one
            first_json = cleaned
            try:
                decoder = json.JSONDecoder()
                _, end_idx = decoder.raw_decode(cleaned)
                first_json = cleaned[:end_idx]
            except (json.JSONDecodeError, ValueError):
                pass
            # Try parsing; if fail, attempt common fixes
            parsed = None
            for attempt in (first_json, first_json + "}", first_json + "]}"):
                try:
                    parsed = json.loads(attempt)
                    break
                except json.JSONDecodeError:
                    continue
            if parsed is None:
                # Extract response from malformed JSON as fallback
                resp_match = re.search(r'"response"\s*:\s*"([^"]+)"', first_json)
                if resp_match:
                    self.logger.warning("Malformed JSON, extracted response: %s", resp_match.group(1))
                    return RouteResult(
                        tier="cloud", intent="chat", confidence=0.9,
                        duration_ms=duration_ms, provider="",
                        text_response=resp_match.group(1),
                    )
            if parsed:
                intent = parsed.get("intent", "uncertain")
                if intent not in VALID_INTENTS:
                    intent = "uncertain"

                confidence = float(parsed.get("confidence", 0.5))
                actions = parsed.get("actions", [])
                response = parsed.get("response")
                sub_type = parsed.get("sub_type")
                query = parsed.get("query")
                rule = parsed.get("rule")

                if intent in ("smart_home", "info_query", "time", "automation") and confidence >= 0.90:
                    tier = "local"
                else:
                    tier = "cloud"

                self.logger.info(
                    "Unified(json): %s/%s (%.2f, %dms) sub_type=%s query=%r actions=%d",
                    tier, intent, confidence, duration_ms,
                    sub_type, query, len(actions),
                )
                return RouteResult(
                    tier=tier, intent=intent, confidence=confidence,
                    duration_ms=duration_ms, provider="",
                    actions=actions, response=response,
                    sub_type=sub_type, query=query, rule=rule,
                )
        # Natural language response
        self.logger.info(
            "Unified(text): '%s' → chat (%dms)", raw[:40], duration_ms,
        )
        return RouteResult(
            tier="cloud", intent="chat", confidence=1.0,
            duration_ms=duration_ms, provider="",
            text_response=raw,
        )
