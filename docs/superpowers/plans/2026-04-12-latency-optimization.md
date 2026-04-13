# Latency Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce cloud conversation latency from 2-3s to ~600-800ms by merging routing+response into a single Groq call, adding template responses for info queries, user-controlled model switching, TTS pre-cache, and filler playback on escalation.

**Architecture:** Replace the serial Groq-route → Grok-respond pipeline with a single Groq call that both routes and responds. Smart home commands output JSON for local execution; everything else outputs natural language directly. Model switching via new skill + config presets.

**Tech Stack:** Python 3.13, requests, Groq/Cerebras/xAI APIs (OpenAI-compatible), existing TTSEngine cache system.

---

### Task 1: TTS Pre-cache at Startup

The simplest, most isolated change. Add a `precache()` method to TTSEngine that pre-synthesizes common phrases on startup.

**Files:**
- Modify: `core/tts.py:65-148` (TTSEngine class, cache section)
- Modify: `jarvis.py:245-249` (startup warmup section)
- Test: `tests/test_tts.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_tts.py`, add:

```python
class TestTTSPrecache:
    def test_precache_creates_cache_files(self, tmp_path, monkeypatch):
        """precache() should synthesize phrases and store them in cache dir."""
        config = {"tts": {"engine": "minimax", "cache_dir": str(tmp_path), "minimax_key": "fake"}}
        tts = TTSEngine(config)

        # Mock _synth_minimax to write a dummy file instead of calling API
        def fake_synth(text, emotion=""):
            path = tmp_path / f"{text}.mp3"
            path.write_bytes(b"fake audio")
            return str(path), False

        monkeypatch.setattr(tts, "_synth_minimax", fake_synth)

        tts.precache(["好的", "再见"])
        # Verify cache was populated — subsequent speak() should hit cache
        assert len(list(tmp_path.glob("*.mp3"))) >= 2

    def test_precache_skips_on_failure(self, tmp_path, monkeypatch):
        """precache() should log warning but not crash if synthesis fails."""
        config = {"tts": {"engine": "minimax", "cache_dir": str(tmp_path), "minimax_key": "fake"}}
        tts = TTSEngine(config)
        monkeypatch.setattr(tts, "_synth_minimax", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("fail")))

        # Should not raise
        tts.precache(["好的"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tts.py::TestTTSPrecache -v`
Expected: FAIL — `TTSEngine` has no `precache` method

- [ ] **Step 3: Add `precache()` to TTSEngine**

In `core/tts.py`, add after the `_evict_tts_cache` method (around line 148):

```python
def precache(self, phrases: list[str]) -> None:
    """Pre-synthesize common phrases into cache at startup.

    Args:
        phrases: List of short text strings to pre-cache.
    """
    for text in phrases:
        if len(text) > 50:
            continue  # only cache short phrases
        cache_key = self._tts_cache_key(text, "calm")
        cache_path = self._tts_cache_dir / f"{cache_key}.mp3"
        if cache_path.exists():
            self.logger.debug("TTS precache already exists: %r", text)
            continue
        try:
            self.synth_to_file(text, emotion="")
            self.logger.info("TTS precached: %r", text)
        except Exception as exc:
            self.logger.warning("TTS precache failed for %r: %s", text, exc)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_tts.py::TestTTSPrecache -v`
Expected: PASS

- [ ] **Step 5: Wire up precache in jarvis.py startup**

In `jarvis.py`, after the existing warmup section (around line 249), add:

```python
# 预缓存常用 TTS 短句（后台，不阻塞启动）
_PRECACHE_PHRASES = ["好的", "嗯，让我想想", "好的，灯开了", "好的，灯关了", "再见", "在的"]
self._executor.submit(lambda: self._get_tts().precache(_PRECACHE_PHRASES))
```

- [ ] **Step 6: Run full test suite**

Run: `python -m pytest tests/ -q`
Expected: 811 passed (same pre-existing failures, no new ones)

- [ ] **Step 7: Commit**

```bash
git add core/tts.py jarvis.py tests/test_tts.py
git commit -m "perf: TTS pre-cache common phrases at startup"
```

---

### Task 2: info_query Template Responses

Remove the LLM rephrase round-trip for weather/stocks/news. Template format the data directly.

**Files:**
- Modify: `core/local_executor.py:111-147` (execute_info_query)
- Test: `tests/test_local_executor.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_local_executor.py`, add:

```python
class TestInfoQueryTemplate:
    def test_weather_returns_response_not_reqllm(self):
        """Weather query should return Action.RESPONSE (template), not REQLLM."""
        mock_registry = MagicMock()
        mock_registry.execute.return_value = (
            "Weather in Toronto: Cloudy, temperature 12°C (feels like 9°C), "
            "humidity 75%, wind 20 km/h."
        )
        executor = LocalExecutor(mock_registry)
        result = executor.execute_info_query("weather", None)
        assert result.action == Action.RESPONSE
        assert "12" in result.text
        assert "多云" in result.text or "Cloudy" in result.text or "°C" in result.text

    def test_stocks_returns_response_not_reqllm(self):
        """Stocks query should return Action.RESPONSE (template), not REQLLM."""
        mock_registry = MagicMock()
        mock_registry.execute.return_value = "AAPL: $195.50 (+1.2%), NVDA: $920.30 (-0.5%)"
        executor = LocalExecutor(mock_registry)
        result = executor.execute_info_query("stocks", ["AAPL", "NVDA"])
        assert result.action == Action.RESPONSE

    def test_news_returns_response_not_reqllm(self):
        """News query should return Action.RESPONSE (template), not REQLLM."""
        mock_registry = MagicMock()
        mock_registry.execute.return_value = "1. AI breakthrough... 2. Market update..."
        executor = LocalExecutor(mock_registry)
        result = executor.execute_info_query("news", "tech")
        assert result.action == Action.RESPONSE

    def test_failed_query_still_response(self):
        """Failed query should return a response, not REQLLM."""
        mock_registry = MagicMock()
        mock_registry.execute.return_value = None
        executor = LocalExecutor(mock_registry)
        result = executor.execute_info_query("weather", None)
        assert result.action == Action.RESPONSE
        assert "没查到" in result.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_local_executor.py::TestInfoQueryTemplate -v`
Expected: FAIL — weather returns `Action.REQLLM` instead of `Action.RESPONSE`

- [ ] **Step 3: Change execute_info_query to return RESPONSE**

In `core/local_executor.py`, replace `execute_info_query` method (lines 111-147):

```python
def execute_info_query(
    self, sub_type: str | None, query: Any, user_role: str = "owner",
) -> ActionResponse:
    """执行信息查询，模板直出，不走 LLM 转述.

    Args:
        sub_type: 查询子类型（stocks/news/weather）。
        query: 查询参数。
        user_role: 用户角色。

    Returns:
        ActionResponse — RESPONSE 直接播报。
    """
    result: str | None = None

    if sub_type == "stocks":
        symbols = query if isinstance(query, list) else None
        tool_input = {"symbols": symbols} if symbols else {}
        result = self.skill_registry.execute(
            "get_stock_watchlist", tool_input, user_role=user_role,
        )

    elif sub_type == "news":
        focus = query if isinstance(query, str) else "all"
        result = self.skill_registry.execute(
            "get_news_briefing", {"focus": focus}, user_role=user_role,
        )

    elif sub_type == "weather":
        result = self.skill_registry.execute(
            "get_weather", {}, user_role=user_role,
        )

    if not result:
        return ActionResponse(Action.RESPONSE, "没查到相关信息。")

    return ActionResponse(Action.RESPONSE, result)
```

The key change: `Action.REQLLM` → `Action.RESPONSE`. The skill's raw output is already human-readable (WeatherSkill returns "Weather in Toronto: Cloudy, 12°C..."). For a more natural feel, callers in `jarvis.py` that previously handled REQLLM will now get a direct response and skip the LLM rephrase entirely.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_local_executor.py::TestInfoQueryTemplate -v`
Expected: PASS

- [ ] **Step 5: Run full test suite**

Run: `python -m pytest tests/ -q`
Expected: 811 passed (no regressions)

- [ ] **Step 6: Commit**

```bash
git add core/local_executor.py tests/test_local_executor.py
git commit -m "perf: info_query template responses — skip LLM rephrase for weather/stocks/news"
```

---

### Task 3: LLM Preset Config + switch_model()

Add model presets to config.yaml and a `switch_model()` method to LLMClient. This is the foundation for both the unified Groq call and the ModelSwitchSkill.

**Files:**
- Modify: `config.yaml:304-319` (llm section)
- Modify: `core/llm.py:20-62` (LLMClient.__init__ and new switch_model)
- Test: `tests/test_llm.py`

- [ ] **Step 1: Add presets to config.yaml**

In `config.yaml`, replace the `llm:` section (lines 304-319) with:

```yaml
llm:
  provider: openai
  default_preset: fast

  presets:
    fast:
      model: llama-3.3-70b-versatile
      base_url: "https://api.groq.com/openai/v1"
      api_key_env: GROQ_API_KEY
      max_tokens: 1024
    deep:
      model: grok-4-1-fast-non-reasoning
      base_url: "https://api.x.ai/v1"
      api_key_env: XAI_API_KEY
      max_tokens: 1024

  # Fallback if no presets match (backward compat)
  model: grok-4-1-fast-non-reasoning
  base_url: "https://api.x.ai/v1"
  max_tokens: 1024
  api_key: ""
```

- [ ] **Step 2: Write the failing test for switch_model**

In `tests/test_llm.py`, add:

```python
class TestModelSwitch:
    def test_switch_model_changes_active_preset(self):
        """switch_model() should update model, base_url, and api_key."""
        config = {
            "llm": {
                "provider": "openai",
                "default_preset": "fast",
                "presets": {
                    "fast": {
                        "model": "llama-3.3-70b",
                        "base_url": "https://api.groq.com/openai/v1",
                        "api_key_env": "GROQ_API_KEY",
                        "max_tokens": 1024,
                    },
                    "deep": {
                        "model": "grok-4-1-fast",
                        "base_url": "https://api.x.ai/v1",
                        "api_key_env": "XAI_API_KEY",
                        "max_tokens": 2048,
                    },
                },
                "model": "fallback-model",
                "base_url": "",
                "max_tokens": 1024,
            },
        }
        client = LLMClient(config)
        assert client.model == "llama-3.3-70b"
        assert client.active_preset == "fast"

        client.switch_model("deep")
        assert client.model == "grok-4-1-fast"
        assert client.active_preset == "deep"

    def test_switch_model_unknown_preset_raises(self):
        """switch_model() with unknown preset should raise ValueError."""
        config = {
            "llm": {
                "provider": "openai",
                "default_preset": "fast",
                "presets": {"fast": {"model": "llama", "base_url": "http://x", "api_key_env": "X"}},
                "model": "fallback",
                "base_url": "",
                "max_tokens": 1024,
            },
        }
        client = LLMClient(config)
        with pytest.raises(ValueError, match="Unknown preset"):
            client.switch_model("nonexistent")

    def test_get_presets(self):
        """get_presets() should return dict of available presets."""
        config = {
            "llm": {
                "provider": "openai",
                "presets": {
                    "fast": {"model": "llama", "base_url": "http://a", "api_key_env": "X"},
                    "deep": {"model": "grok", "base_url": "http://b", "api_key_env": "Y"},
                },
                "model": "fallback",
                "base_url": "",
                "max_tokens": 1024,
            },
        }
        client = LLMClient(config)
        presets = client.get_presets()
        assert set(presets.keys()) == {"fast", "deep"}
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/test_llm.py::TestModelSwitch -v`
Expected: FAIL — `LLMClient` has no `switch_model` or `active_preset`

- [ ] **Step 4: Implement preset loading + switch_model in LLMClient**

In `core/llm.py`, modify `__init__` (after line 58, inside the `if self.provider == "openai":` block) and add new methods:

Replace the openai init block (lines 44-58) with:

```python
        if self.provider == "openai":
            # Load presets
            self._presets = dict(llm_config.get("presets", {}))
            default_preset = llm_config.get("default_preset", "")

            if default_preset and default_preset in self._presets:
                self._apply_preset(default_preset)
            else:
                # Backward-compatible: use flat config
                self.model = str(llm_config.get("model", "gpt-4o"))
                base_url = llm_config.get("base_url") or ""
                self._base_url = base_url or None
                self._api_key = self._resolve_api_key(llm_config, base_url)
                self.active_preset = ""
```

Add these methods after `__init__`:

```python
    def _apply_preset(self, name: str) -> None:
        """Apply a named model preset."""
        preset = self._presets[name]
        self.model = str(preset["model"])
        self._base_url = str(preset.get("base_url", "")) or None
        env_var = preset.get("api_key_env", "")
        self._api_key = os.environ.get(env_var, "") if env_var else ""
        self.max_tokens = int(preset.get("max_tokens", self.max_tokens))
        self.active_preset = name
        self._client = None  # Force re-init on next call
        self.logger.info("LLM preset switched to '%s' (model=%s)", name, self.model)

    def _resolve_api_key(self, llm_config: dict, base_url: str) -> str | None:
        """Resolve API key from config or environment."""
        if llm_config.get("api_key"):
            return llm_config["api_key"]
        if "x.ai" in base_url:
            return os.environ.get("XAI_API_KEY")
        if "deepseek" in base_url:
            return os.environ.get("DEEPSEEK_API_KEY")
        if "moonshot" in base_url:
            return os.environ.get("MOONSHOT_API_KEY")
        if "groq" in base_url:
            return os.environ.get("GROQ_API_KEY")
        return os.environ.get("OPENAI_API_KEY")

    def switch_model(self, preset_name: str) -> str:
        """Switch to a named model preset at runtime.

        Args:
            preset_name: Key from config llm.presets.

        Returns:
            Confirmation message.

        Raises:
            ValueError: If preset_name is not found.
        """
        if preset_name not in self._presets:
            raise ValueError(f"Unknown preset '{preset_name}'. Available: {list(self._presets.keys())}")
        self._apply_preset(preset_name)
        return f"已切换到 {preset_name} 模式（{self.model}）"

    def get_presets(self) -> dict:
        """Return available model presets."""
        return dict(self._presets)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_llm.py::TestModelSwitch -v`
Expected: PASS

- [ ] **Step 6: Run full test suite**

Run: `python -m pytest tests/ -q`
Expected: 811 passed

- [ ] **Step 7: Commit**

```bash
git add config.yaml core/llm.py tests/test_llm.py
git commit -m "feat: LLM model presets + runtime switch_model()"
```

---

### Task 4: Unified Groq Route-and-Respond

The core latency change. Add `route_and_respond()` to IntentRouter that returns either structured JSON (for local execution) or a natural language response (for direct TTS).

**Files:**
- Modify: `core/intent_router.py:100-314` (IntentRouter class)
- Modify: `core/personality.py` (add a short personality string getter)
- Modify: `jarvis.py:640-830` (_handle_utterance_inner routing+cloud sections)
- Modify: `jarvis.py:980-1110` (handle_text routing+cloud sections)
- Test: `tests/test_intent_router.py`

- [ ] **Step 1: Add `get_short_personality()` to personality.py**

In `core/personality.py`, add a function that returns the compact personality for the unified prompt (below `_nsfw_mode = False`, around line 49):

```python
def get_short_personality() -> str:
    """Return a compact personality description for fast routing+response prompts."""
    return (
        "你叫小月，Allen的私人管家。说话干脆、略带幽默、中文为主。"
        "能一句话说清楚的绝不说两句。不说'您'，不用emoji。"
        "别人开心你就跟着笑，别人难过你就陪着。你是小月，不是客服。"
    )
```

- [ ] **Step 2: Write the failing test for route_and_respond**

In `tests/test_intent_router.py`, add:

```python
class TestRouteAndRespond:
    def test_smart_home_returns_route_result(self, monkeypatch):
        """Smart home commands should return a RouteResult with actions."""
        config = _make_config()  # use existing test helper
        router = IntentRouter(config)

        # Mock _call_cloud to return smart home JSON
        def mock_call(url, key, model, text, start, ctx=None, unified=False, **kw):
            return RouteResult(
                tier="local", intent="smart_home", confidence=0.95,
                duration_ms=100, provider="groq",
                actions=[{"device_id": "light1", "action": "turn_on"}],
                response="好的，灯开了。",
            )
        monkeypatch.setattr(router, "_call_cloud", mock_call)

        result = router.route_and_respond("开灯")
        assert isinstance(result, RouteResult)
        assert result.intent == "smart_home"
        assert result.actions

    def test_chat_returns_text_response(self, monkeypatch):
        """General chat should return a RouteResult with text_response set."""
        config = _make_config()
        router = IntentRouter(config)

        def mock_call(url, key, model, text, start, ctx=None, unified=False, **kw):
            return RouteResult(
                tier="local", intent="chat", confidence=0.9,
                duration_ms=100, provider="groq",
                text_response="今天天气不错！",
            )
        monkeypatch.setattr(router, "_call_cloud", mock_call)

        result = router.route_and_respond("今天怎么样")
        assert result.text_response == "今天天气不错！"

    def test_cache_hit_skips_api_call(self, monkeypatch):
        """Cached smart_home routes should not make an API call."""
        config = _make_config()
        router = IntentRouter(config)
        call_count = [0]

        def mock_call(url, key, model, text, start, ctx=None, unified=False, **kw):
            call_count[0] += 1
            return RouteResult(
                tier="local", intent="smart_home", confidence=0.95,
                duration_ms=100, provider="groq",
                actions=[{"device_id": "light1", "action": "turn_on"}],
            )
        monkeypatch.setattr(router, "_call_cloud", mock_call)

        router.route_and_respond("开灯")
        assert call_count[0] == 1
        router.route_and_respond("开灯")
        assert call_count[0] == 1  # cache hit, no second call
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/test_intent_router.py::TestRouteAndRespond -v`
Expected: FAIL — `RouteResult` has no `text_response`, `route_and_respond` doesn't exist

- [ ] **Step 4: Add `text_response` to RouteResult**

In `core/intent_router.py`, update the RouteResult dataclass (around line 101):

```python
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
    text_response: str | None = None  # 统一调用时的自然语言回复
```

- [ ] **Step 5: Build unified system prompt**

In `core/intent_router.py`, add a new function after `build_system_prompt` (around line 99):

```python
def build_unified_prompt(config: dict, memory_context: str = "", user_emotion: str = "") -> str:
    """Build a unified prompt for route+respond in a single call.

    Smart home → JSON output (same format as routing-only).
    Everything else → natural language response as 小月.
    """
    from core.personality import get_short_personality

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

    device_list = "\n".join(devices_desc) if devices_desc else "(无设备)"

    personality = get_short_personality()

    prompt = f"""{personality}

处理用户指令，按以下规则回复：

【设备控制】输出JSON：
{{"intent":"smart_home","actions":[{{"device_id":"xxx","action":"turn_on","value":null}}],"response":"好的，灯开了。"}}
设备：
{device_list}
多设备用actions数组。"所有灯"=列出全部灯。隐含意图如"好暗"=开灯。

【信息查询】输出JSON：
{{"intent":"info_query","sub_type":"weather|stocks|news","query":"..."}}

【时间】输出JSON：
{{"intent":"time","sub_type":"current_time|date|weekday"}}

【自动化】输出JSON：
{{"intent":"automation","sub_type":"create|list|delete","rule":{{...}},"response":"..."}}
trigger类型：keyword/cron/once

【其他所有情况】直接用小月的语气回复用户（不要输出JSON）。
记忆/个人信息/需要工具的问题/情感表达/闲聊→直接回复。"""

    if memory_context:
        prompt += f"\n\n<memory>\n{memory_context}\n</memory>"

    return prompt
```

- [ ] **Step 6: Add `route_and_respond()` method**

In `core/intent_router.py`, add to IntentRouter class (after `route` method):

```python
def route_and_respond(
    self,
    text: str,
    conversation_history: list[dict] | None = None,
    memory_context: str = "",
    user_emotion: str = "",
) -> RouteResult:
    """Route + respond in a single Groq call.

    Smart home / info_query / time / automation → JSON (same as route()).
    Everything else → text_response filled with natural language.
    """
    key = " ".join(_PUNCT_RE.sub("", text.strip()).split())
    recent_context = self._build_context(conversation_history) if conversation_history else []

    # Cache hit — only for commands without conversation context
    if not recent_context and key in self._route_cache:
        self._route_cache.move_to_end(key)
        cached = self._route_cache[key]
        self.logger.info("Route cache hit: '%s' → %s/%s", key[:20], cached.tier, cached.intent)
        return copy.copy(cached)

    start = time.time()

    unified_prompt = build_unified_prompt(
        self.config, memory_context=memory_context, user_emotion=user_emotion,
    )

    # 1. Groq (primary)
    if self.groq_key and (not self._tracker or self._tracker.is_available("intent.groq")):
        result = self._call_unified(
            self.groq_url, self.groq_key, self.groq_model,
            text, start, recent_context, unified_prompt,
        )
        if result:
            if self._tracker:
                self._tracker.record_success("intent.groq")
            result.provider = "groq"
            # Only cache structured routes (JSON), not free-text responses
            if not recent_context and result.text_response is None:
                self._cache_result(key, result)
            return result
        if self._tracker:
            self._tracker.record_failure("intent.groq")

    # 2. Cerebras (fallback)
    if self.cerebras_key and (not self._tracker or self._tracker.is_available("intent.cerebras")):
        result = self._call_unified(
            self.cerebras_url, self.cerebras_key, self.cerebras_model,
            text, start, recent_context, unified_prompt,
        )
        if result:
            if self._tracker:
                self._tracker.record_success("intent.cerebras")
            result.provider = "cerebras"
            if not recent_context and result.text_response is None:
                self._cache_result(key, result)
            return result
        if self._tracker:
            self._tracker.record_failure("intent.cerebras")

    # All failed → return empty result, caller falls back to cloud LLM
    return RouteResult(
        tier="cloud", intent="complex", confidence=0.0,
        duration_ms=int((time.time() - start) * 1000), provider="none",
    )

def _call_unified(
    self, url: str, api_key: str, model: str, text: str, start: float,
    recent_context: list[dict], system_prompt: str,
) -> RouteResult | None:
    """Call cloud API with unified route+respond prompt."""
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
            timeout=8,
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

        return self._parse_unified_response(raw, start)

    except requests.Timeout:
        self.logger.warning("Timeout calling %s", url)
        return None
    except requests.RequestException as exc:
        self.logger.warning("Request failed (%s): %s", url, exc)
        return None

def _parse_unified_response(self, raw: str, start: float) -> RouteResult | None:
    """Parse unified response — JSON for structured intents, text for everything else."""
    duration_ms = int((time.time() - start) * 1000)

    # Try JSON parse — if it starts with { it's a structured route
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    if cleaned.startswith("{"):
        try:
            parsed = json.loads(cleaned)
            intent = parsed.get("intent", "")
            if intent in VALID_INTENTS:
                confidence = float(parsed.get("confidence", 0.9))
                tier = "local" if intent in ("smart_home", "info_query", "time", "automation") else "cloud"
                return RouteResult(
                    tier=tier, intent=intent,
                    confidence=confidence, duration_ms=duration_ms,
                    provider="", actions=parsed.get("actions", []),
                    response=parsed.get("response"),
                    sub_type=parsed.get("sub_type"),
                    query=parsed.get("query"),
                    rule=parsed.get("rule"),
                )
        except json.JSONDecodeError:
            pass

    # Not JSON → it's a natural language response from 小月
    self.logger.info("Unified response (text, %dms): %s", duration_ms, raw[:60])
    return RouteResult(
        tier="local", intent="chat", confidence=0.9,
        duration_ms=duration_ms, provider="",
        text_response=raw,
    )
```

Note: `temperature=0.7` for the unified call (natural responses need some creativity), vs `temperature=0` for the routing-only call. `max_tokens=500` (responses can be longer than routing JSON). No `response_format: json_object` (output can be text).

- [ ] **Step 7: Run test to verify it passes**

Run: `python -m pytest tests/test_intent_router.py::TestRouteAndRespond -v`
Expected: PASS

- [ ] **Step 8: Wire up route_and_respond in jarvis.py _handle_utterance_inner**

In `jarvis.py`, modify the routing section (around lines 640-830). The key change: replace `route_future` dispatching `self.intent_router.route()` with `self.intent_router.route_and_respond()`, and check for `route.text_response` before falling through to cloud LLM.

In the parallel launch section (around line 649), change:

```python
# Before:
route_future = self._executor.submit(
    self.intent_router.route, text, conversation_history=history
)

# After:
route_future = self._executor.submit(
    self.intent_router.route_and_respond, text,
    conversation_history=history,
    memory_context=memory_context if memory_context else "",
    user_emotion=detected_emotion,
)
```

Wait — `memory_context` is collected later from `memory_future`. We need memory before the unified call. Restructure:

Replace the parallel route+memory launch (around lines 645-690) with:

```python
# 4a. Memory query (need it before unified route call)
memory_context = ""
if user_id:
    try:
        memory_context = self.memory_manager.query(text, user_id) or ""
    except Exception as exc:
        self.logger.warning("Memory query failed: %s", exc)

_t_think = time.monotonic()

# Direct answer probe
if user_id and not response_text:
    try:
        mem_answer = self.direct_answerer.try_answer(text, user_id)
        if mem_answer:
            response_text = mem_answer
            self.logger.info("Direct answer hit")
    except Exception:
        pass

_t_da = time.monotonic()
if response_text:
    print(f"⏱ 直答: {(_t_da - _t_think)*1000:.0f}ms")

# 5. Unified route + respond (single Groq call)
route = None
if response_text is None and self.intent_router:
    try:
        route = self.intent_router.route_and_respond(
            text,
            conversation_history=history,
            memory_context=memory_context,
            user_emotion=detected_emotion,
        )
    except Exception as exc:
        self.logger.warning("Unified route failed: %s", exc)

_t_route = time.monotonic()
if route:
    print(f"⏱ 统一路由: {(_t_route - _t_da)*1000:.0f}ms → {route.intent} ({route.provider})")

    # Text response from Groq → direct TTS, skip cloud LLM
    if route.text_response:
        response_text = route.text_response
```

Then the existing local execution block stays the same (smart_home/info_query/time/automation handling). Only the cloud LLM fallback section changes — it now only triggers when `route.text_response` is None AND no local execution succeeded.

- [ ] **Step 9: Apply same change to handle_text**

Apply the parallel change to `handle_text()` (around lines 980-1110). Same pattern: collect memory first, call `route_and_respond`, check `text_response`.

- [ ] **Step 10: Run full test suite**

Run: `python -m pytest tests/ -q`
Expected: 811 passed

- [ ] **Step 11: Commit**

```bash
git add core/intent_router.py core/personality.py jarvis.py tests/test_intent_router.py
git commit -m "feat: unified route+respond single Groq call — 3-4x latency reduction"
```

---

### Task 5: ModelSwitchSkill

New skill for voice-controlled model switching. Supports persistent presets and per-turn escalation keywords.

**Files:**
- Create: `skills/model_switch.py`
- Modify: `jarvis.py:1248-1300` (_register_skills)
- Modify: `jarvis.py:555-610` (escalation keyword detection in _handle_utterance_inner)
- Modify: `jarvis.py:940-960` (escalation keyword detection in handle_text)
- Test: `tests/test_model_switch.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_model_switch.py`:

```python
"""Tests for ModelSwitchSkill."""

import pytest
from unittest.mock import MagicMock

from skills.model_switch import ModelSwitchSkill


class TestModelSwitchSkill:
    def _make_skill(self):
        mock_llm = MagicMock()
        mock_llm.get_presets.return_value = {
            "fast": {"model": "llama-3.3-70b"},
            "deep": {"model": "grok-4-1-fast"},
        }
        mock_llm.active_preset = "fast"
        mock_llm.model = "llama-3.3-70b"
        mock_llm.switch_model.return_value = "已切换到 deep 模式（grok-4-1-fast）"
        return ModelSwitchSkill(mock_llm), mock_llm

    def test_skill_name(self):
        skill, _ = self._make_skill()
        assert skill.skill_name == "model_switch"

    def test_switch_to_preset(self):
        skill, mock_llm = self._make_skill()
        result = skill.execute("switch_model", {"preset": "deep"})
        mock_llm.switch_model.assert_called_once_with("deep")
        assert "deep" in result

    def test_query_current_model(self):
        skill, _ = self._make_skill()
        result = skill.execute("switch_model", {"preset": ""})
        assert "fast" in result
        assert "llama" in result

    def test_list_presets(self):
        skill, _ = self._make_skill()
        result = skill.execute("switch_model", {"preset": "list"})
        assert "fast" in result
        assert "deep" in result

    def test_switch_unknown_preset(self):
        skill, mock_llm = self._make_skill()
        mock_llm.switch_model.side_effect = ValueError("Unknown preset 'bad'")
        result = skill.execute("switch_model", {"preset": "bad"})
        assert "不存在" in result or "Unknown" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_model_switch.py -v`
Expected: FAIL — `skills.model_switch` module doesn't exist

- [ ] **Step 3: Create skills/model_switch.py**

```python
"""ModelSwitchSkill — voice-controlled LLM model switching."""

from __future__ import annotations

import logging
from typing import Any

from skills import Skill

LOGGER = logging.getLogger(__name__)

# 中文别名 → preset name
_PRESET_ALIASES = {
    "快速": "fast",
    "快速模式": "fast",
    "快": "fast",
    "深度": "deep",
    "深度模式": "deep",
    "聪明": "deep",
    "聪明模式": "deep",
}


class ModelSwitchSkill(Skill):
    """Switch LLM model preset via voice command."""

    def __init__(self, llm_client: Any) -> None:
        self._llm = llm_client
        self.logger = LOGGER

    @property
    def skill_name(self) -> str:
        return "model_switch"

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "switch_model",
                "description": (
                    "Switch the LLM model preset. "
                    "Use preset='list' to show available presets. "
                    "Use preset='' to query current model. "
                    "Use preset name (e.g. 'fast', 'deep') to switch."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "preset": {
                            "type": "string",
                            "description": "Preset name: 'fast', 'deep', 'list', or '' for current status.",
                        },
                    },
                },
            },
        ]

    def execute(self, tool_name: str, tool_input: dict[str, Any], **context: Any) -> str:
        preset = str(tool_input.get("preset", "")).strip().lower()

        # Resolve Chinese aliases
        preset = _PRESET_ALIASES.get(preset, preset)

        # Query current model
        if not preset:
            return f"当前模式：{self._llm.active_preset}（{self._llm.model}）"

        # List available presets
        if preset == "list":
            presets = self._llm.get_presets()
            lines = []
            for name, cfg in presets.items():
                marker = " ← 当前" if name == self._llm.active_preset else ""
                lines.append(f"- {name}: {cfg.get('model', '?')}{marker}")
            return "可用模式：\n" + "\n".join(lines)

        # Switch preset
        try:
            return self._llm.switch_model(preset)
        except ValueError as exc:
            return f"切换失败：该模式不存在。{exc}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_model_switch.py -v`
Expected: PASS

- [ ] **Step 5: Register the skill in jarvis.py**

In `jarvis.py` `_register_skills`, add after the existing registrations (around line 1265):

```python
# Model switch skill
from skills.model_switch import ModelSwitchSkill
self.skill_registry.register(ModelSwitchSkill(self.llm))
```

- [ ] **Step 6: Add per-turn escalation keyword detection**

In `jarvis.py`, add escalation keyword constants near the top (after `_REMEMBER_KEYWORDS`, around line 57):

```python
# Per-turn escalation keywords — trigger slow model for this utterance only
_ESCALATION_KEYWORDS = ("仔细想想", "详细分析", "认真想", "好好想")
```

In `_handle_utterance_inner`, after the farewell check (around line 573), add:

```python
# Escalation keyword: single-turn upgrade to deep model
_escalation_active = False
_original_preset = ""
for kw in _ESCALATION_KEYWORDS:
    if text.startswith(kw):
        text = text[len(kw):].lstrip("，, 。")  # Strip keyword from query
        if hasattr(self.llm, "active_preset") and hasattr(self.llm, "switch_model"):
            _original_preset = self.llm.active_preset
            try:
                self.llm.switch_model("deep")
                _escalation_active = True
                self.logger.info("Escalation: '%s' → deep mode for this turn", kw)
            except (ValueError, AttributeError):
                pass
        break
```

At the end of `_handle_utterance_inner`, before the return (around line 870), add:

```python
# Restore original preset after escalation
if _escalation_active and _original_preset:
    try:
        self.llm.switch_model(_original_preset)
    except (ValueError, AttributeError):
        pass
```

Apply the same pattern to `handle_text()`.

- [ ] **Step 7: Run full test suite**

Run: `python -m pytest tests/ -q`
Expected: 811 passed

- [ ] **Step 8: Commit**

```bash
git add skills/model_switch.py jarvis.py tests/test_model_switch.py
git commit -m "feat: ModelSwitchSkill — voice-controlled model switching + per-turn escalation"
```

---

### Task 6: Filler Playback on Escalation

When escalation keyword is detected, play pre-cached filler ("嗯，让我想想") immediately before the slow model call.

**Files:**
- Modify: `jarvis.py` (escalation section from Task 5)

- [ ] **Step 1: Add filler playback to escalation block**

In `jarvis.py`, in the escalation keyword detection block (added in Task 5), after `_escalation_active = True`:

```python
# Play filler immediately while slow model processes
self._speak_nonblocking("嗯，让我想想", emotion="")
```

This uses the pre-cached "嗯，让我想想" from Task 1 (TTS cache hit ~1ms), giving the user immediate audio feedback while the slow model generates.

- [ ] **Step 2: Apply same to handle_text escalation block**

Same addition in the `handle_text()` escalation block. But `handle_text` doesn't have TTS — it's for web frontend. So only add filler for `_handle_utterance_inner`, not `handle_text`.

- [ ] **Step 3: Run full test suite**

Run: `python -m pytest tests/ -q`
Expected: 811 passed

- [ ] **Step 4: Commit**

```bash
git add jarvis.py
git commit -m "feat: filler playback on escalation — immediate audio feedback while slow model processes"
```

---

### Task 7: Integration Verification

End-to-end verification that all 5 changes work together.

**Files:**
- Test: manual + existing tests

- [ ] **Step 1: Run full test suite**

Run: `python -m pytest tests/ -q`
Expected: 811 passed (same pre-existing failures)

- [ ] **Step 2: Verify interactive mode works**

Run: `python jarvis.py --no-wake`
Test these scenarios:
1. "开灯" → should get response from Groq unified call, execute locally
2. "今天天气怎么样" → should get template response, no LLM rephrase
3. "你好" → should get natural language response from Groq directly
4. "小月，换成深度模式" → should switch to Grok
5. "小月，现在用什么模型" → should report current preset
6. "小月，快速模式" → should switch back to Groq

- [ ] **Step 3: Update bugs.md**

Mark the latency-related P0 items as completed in `notes/bugs.md`.

- [ ] **Step 4: Final commit**

```bash
git add notes/bugs.md
git commit -m "docs: mark latency optimizations as completed in bugs.md"
```
