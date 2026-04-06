# Voice Pipeline 6 Bug Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 6 issues discovered during first real-world voice testing of 小月.

**Architecture:** All fixes are prompt-level or config-level changes — no structural refactoring. Each task is independent.

**Tech Stack:** Python 3.9, GPT-4o-mini (memory extraction), Groq 70B (intent routing)

---

### Task 1: Fix memory extraction hallucination

The memory extraction LLM invents facts the user never said (e.g., "用户喜欢喝美式" when coffee was never mentioned). The prompt needs to strictly prohibit inference.

**Files:**
- Modify: `memory/manager.py:26-50` (`_EXTRACT_PROMPT_HEADER`)
- Test: `tests/test_memory_manager.py`

- [ ] **Step 1: Update extraction prompt to prohibit inference**

In `memory/manager.py`, replace `_EXTRACT_PROMPT_HEADER`:

```python
_EXTRACT_PROMPT_HEADER = """从以下对话中提取值得长期记住的**新**信息。只提取用户说的内容。
忽略打招呼、简单设备开关指令（"开灯"/"关灯"/"几点了"）、纯闲聊。
但要记住：颜色/色温偏好（如"我喜欢暖光"、"Tiffany蓝=#0ABAB5"）、使用习惯、设备昵称等。

严禁推断：只提取用户明确说出的事实。
- 用户调灯颜色 ≠ 用户"喜欢"那个颜色（除非用户说了"我喜欢"）
- 用户问天气 ≠ 用户"关心天气"
- 不要从助手的回复中提取信息，只看用户说的话
- 如果不确定用户是否真的说了某个事实，就不要提取

重要：下方"已有记忆"列表中的内容已经存储，不要重复提取。只提取对话中出现的、不在已有记忆中的新信息。

记忆粒度：
- event/task：输出自含的完整描述，包含时间和上下文
  例："用户 于2026年4月说下周一要去深圳参加技术峰会"
- 其他类型：输出原子事实
  例："用户 喜欢拿铁"

每条记忆必须自含（不依赖上下文就能理解），包含主语（用用户名，不用"我"或"他"）。

如果用户提到相对时间（如"下周一"），必须转为绝对日期填入 time_ref。今天是 {today}。

profile_update 规则：如果提取到了 identity/preference/relationship 类的新信息，
输出更新后的完整画像 JSON（结构：identity/preferences/routines/relationships/pending/status）。
否则输出 null。

如果用户纠正了之前的信息（如"不对，我喜欢美式不是拿铁"），
在 corrections 数组中记录。
如果没有纠正，corrections 为空数组。

如果对话中没有值得记住的内容，memories 数组留空。"""
```

- [ ] **Step 2: Run tests**

Run: `python -m pytest tests/test_memory_manager.py -q`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add memory/manager.py
git commit -m "fix: prohibit memory extraction from inferring unstated facts"
```

---

### Task 2: Fix false correction deactivation

When user adjusts brightness, the memory extraction LLM incorrectly treats it as a "correction" to the color memory, deactivating "灯带颜色是Tiffany蓝". The corrections definition needs tightening.

**Files:**
- Modify: `memory/manager.py:26-50` (`_EXTRACT_PROMPT_HEADER`, same block as Task 1)

- [ ] **Step 1: Add correction rules to the prompt**

After the existing corrections paragraph ("如果用户纠正了之前的信息..."), add:

```python
# Find and replace this block in _EXTRACT_PROMPT_HEADER:
如果用户纠正了之前的信息（如"不对，我喜欢美式不是拿铁"），
在 corrections 数组中记录。
如果没有纠正，corrections 为空数组。
```

Replace with:

```python
如果用户纠正了之前的信息（如"不对，我喜欢美式不是拿铁"），
在 corrections 数组中记录。
如果没有纠正，corrections 为空数组。

correction 的严格定义：
- 只有用户明确否定或更正旧信息才算（"不对"、"不是"、"其实是"、"我改主意了"）
- 设备操作不是 correction：调亮度不影响颜色记忆，关灯不影响偏好记忆
- 不确定是否是 correction 就不要放进 corrections 数组
```

- [ ] **Step 2: Run tests**

Run: `python -m pytest tests/test_memory_manager.py -q`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add memory/manager.py
git commit -m "fix: tighten correction rules — device operations are not corrections"
```

---

### Task 3: Fix weather default city

Weather defaults to Vancouver but user lives in Victoria. Change config and improve tool description so LLM passes the right city.

**Files:**
- Modify: `config.yaml:405` (default_city)
- Modify: `skills/weather.py:31-39` (tool description)

- [ ] **Step 1: Change default city in config**

In `config.yaml`, change:
```yaml
    default_city: Victoria
```

- [ ] **Step 2: Improve weather tool description**

In `skills/weather.py`, update the tool description to tell LLM to use user's location from memory:

```python
            {
                "name": "get_weather",
                "description": (
                    "Get current weather for a city. "
                    "Returns temperature, conditions, humidity, and wind. "
                    "IMPORTANT: Check the user's location from memory/conversation context. "
                    "Only use the default city if the user's location is unknown."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "city": {
                            "type": "string",
                            "description": f"City name in English. Use the user's known location if available. Defaults to '{self.default_city}' if unknown.",
                        },
                    },
                },
            },
```

- [ ] **Step 3: Run tests**

Run: `python -m pytest tests/ -q`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add config.yaml skills/weather.py
git commit -m "fix: weather default Victoria, tool desc uses user location from memory"
```

---

### Task 4: Fix intent router device context

When user says "调到30亮度" without specifying a device, Groq defaults to `all_lights` instead of using the device from the previous turn. Add a context rule to the intent router prompt.

**Files:**
- Modify: `core/intent_router.py:89-95` (rules section of `build_system_prompt`)

- [ ] **Step 1: Add context-awareness rule**

In `core/intent_router.py`, in the `build_system_prompt` function, find the rules section and add a context rule:

```python
规则：
- 多设备用actions数组，如"开灯和空调"输出两个action
- "所有灯"=列出全部灯的device_id
- 隐含意图："有点暗"=开灯，"好热"/"太冷"=调空调
- 上下文设备推断：如果用户没指定设备名，根据对话上下文推断是哪个设备。如果上下文也不明确，才用 all_lights
- 情感/抽象表达→complex，如"你太冷漠了""把这个问题关闭"
- 记忆/个人信息→complex：含"记住""记下""别忘了""我喜欢""我要去""我的xx是"等个人信息、偏好、计划一律走complex
- 关于用户自身的提问→complex：如"我喜欢喝什么""我最近有什么安排""我上次说了什么"
```

- [ ] **Step 2: Run tests**

Run: `python -m pytest tests/test_intent_router.py -q`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add core/intent_router.py
git commit -m "fix: intent router infers device from conversation context"
```

---

### Task 5: Fix Groq response containing ASR garbage

When ASR mishears "只调大灯" as "纸纸条大灯", Groq echoes the garbage in its response: "好的，纸纸条大灯打开了". Use the template reply as fallback when Groq's response contains device IDs or suspicious text.

**Files:**
- Modify: `core/local_executor.py:81-82` (response selection logic)

- [ ] **Step 1: Add garbage detection before using Groq response**

In `core/local_executor.py`, replace the final return in `execute_smart_home`:

```python
        # For simple actions (on/off/brightness), Groq's response is reliable
        return ActionResponse(Action.RESPONSE, response or self._build_smart_home_reply(actions))
```

with:

```python
        # Use Groq response if it looks clean, otherwise fall back to template
        if response and not self._response_has_garbage(response):
            return ActionResponse(Action.RESPONSE, response)
        return ActionResponse(Action.RESPONSE, self._build_smart_home_reply(actions))
```

- [ ] **Step 2: Add garbage detection method**

Add after `_build_smart_home_reply`:

```python
    @staticmethod
    def _response_has_garbage(response: str) -> bool:
        """Detect if Groq response contains ASR artifacts or raw device IDs."""
        # Raw device IDs that shouldn't appear in natural speech
        device_id_patterns = (
            "desk_lightstrip", "desk_play_", "bedroom_lamp_",
            "all_lights", "desk_lights", "hue_light_", "hue_group_",
        )
        lower = response.lower()
        if any(p in lower for p in device_id_patterns):
            return True
        # Common ASR garbage patterns (repeated characters, nonsensical sequences)
        if len(response) > 0:
            # More than 3 consecutive identical characters
            for i in range(len(response) - 2):
                if response[i] == response[i+1] == response[i+2] and response[i] not in "。，！？":
                    return True
        return False
```

- [ ] **Step 3: Run tests**

Run: `python -m pytest tests/test_intent_router.py -q`
Expected: All pass (test uses `response="好的，灯开了。"` which is clean)

- [ ] **Step 4: Commit**

```bash
git add core/local_executor.py
git commit -m "fix: detect ASR garbage in Groq response, fallback to template"
```

---

### Task 6: Update intent router prompt — 小月 not Jarvis

The intent router system prompt still says "你是Jarvis". Update to match the rename.

**Files:**
- Modify: `core/intent_router.py:70`

- [ ] **Step 1: Update prompt**

Change:
```python
    return f"""你是Jarvis，私人AI助手。性格简洁、略带幽默。分析用户指令，返回JSON。
```

to:
```python
    return f"""你是小月，私人AI管家。性格简洁、略带幽默。分析用户指令，返回JSON。
```

- [ ] **Step 2: Run full tests**

Run: `python -m pytest tests/ -q`
Expected: All pass (820+)

- [ ] **Step 3: Commit**

```bash
git add core/intent_router.py
git commit -m "fix: intent router prompt — Jarvis → 小月"
```
