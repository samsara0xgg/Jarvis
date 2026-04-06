# 小月代码全解 — 逐模块走读

> 2026-04-04 · 按数据流顺序，一块一块理解整个项目

## 总览

| #   | 模块          | 核心文件                                                                                     | 一句话               |
| --- | ----------- | ---------------------------------------------------------------------------------------- | ----------------- |
| 1   | **输入层**     | `jarvis.py`, `core/audio_recorder.py`, `core/speech_recognizer.py`, `auth/`              | 唤醒→录音→ASR+声纹      |
| 2   | **大脑层**     | `core/intent_router.py`, `core/llm.py`, `core/personality.py`                            | 意图路由→LLM→人格prompt |
| 3   | **记忆系统**    | `memory/manager.py`, `memory/store.py`, `memory/retriever.py`, `memory/direct_answer.py` | 提取→去重→存储→检索→注入    |
| 4   | **执行层**     | `core/local_executor.py`, `skills/`, `devices/`                                          | 意图→技能→设备控制        |
| 5   | **输出+基础设施** | `core/tts.py`, `core/health.py`, `core/scheduler.py`                                     | TTS降级链+熔断器+调度     |

---

## 模块 1：输入层

### 整体流程

```
用户说 "Hey Jarvis" 或按 Enter
        │
        ▼
  ┌─ AudioRecorder.record() ──────────────────┐
  │  16kHz mono, sounddevice InputStream       │
  │  VAD: 检测到说话后，静音 0.5s 自动停       │
  │  实时显示 ASCII 音量条                      │
  │  输出: float32 numpy array [-1.0, 1.0]     │
  └────────────┬──────────────────────────────┘
               │
        ┌──────┴──────┐   ← ThreadPoolExecutor 并行
        ▼              ▼
  SpeakerVerifier   SpeechRecognizer
  (声纹验证)         (语音转文字)
        │              │
        ▼              ▼
  user_id/None     text + emotion + event
```

### 4 个组件详解

**1. AudioRecorder** (`core/audio_recorder.py`)
- 用 `sounddevice.InputStream` 录音，callback 模式逐块收集
- VAD 逻辑很简单：RMS 音量 > 阈值 → 标记 `speech_detected`，之后连续静音超 0.5s → 停录
- 录完做质量检查：时长够不够、音量够不够
- 保存时转 int16 PCM WAV

**2. SpeakerEncoder** (`core/speaker_encoder.py`)
- SpeechBrain 的 ECAPA-TDNN 模型，输出 **192 维** 声纹向量
- lazy load：第一次调用才加载模型
- 有个 `_patch_torchaudio_compatibility()` 垫片，处理新版 torchaudio 删掉 `list_audio_backends` 的兼容问题

**3. SpeakerVerifier** (`core/speaker_verifier.py`)
- 拿当前音频的 192 维向量，和 `UserStore` 里所有注册用户的向量逐一做 **余弦相似度**
- 最高分 ≥ 0.70 → 验证通过，返回 `user_id`
- 没注册用户时 → jarvis.py 里 fallback 成 `"default_user"` 当 owner 处理

**4. SpeechRecognizer** (`core/speech_recognizer.py`)
- 主引擎：**SenseVoice INT8**（sherpa-onnx），本地推理，额外输出 emotion 和 event 标签
- 回退引擎：OpenAI Whisper（本地模型）
- SenseVoice 有个正则修复：`"学会查汇率。了"` → `"学会查汇率了"`（模型会在语气词前误插句号）
- 置信度是启发式的：RMS < 0.01 或文字 ≤ 1 字 → 0.1（当作没听到）

### jarvis.py 里怎么串起来的

`_handle_utterance_inner()` 第 507-519 行是关键：

```python
# 并行启动声纹 + ASR
verify_future = self._executor.submit(self.speaker_verifier.verify, audio)
asr_future = self._executor.submit(self.speech_recognizer.transcribe, audio)
verification = verify_future.result()
transcription = asr_future.result()
```

拿到结果后：
1. `text` 为空 → "没听清，能再说一遍吗？"
2. 解析 `user_id`（验证通过用匹配的，没注册用户用 `default_user`）
3. 然后进入下游：快路径/意图路由/LLM（这是模块 2 的事了）

### 两种运行模式

| 模式 | 入口 | 触发方式 |
|------|------|---------|
| `--no-wake` | `run_interactive()` | 按 Enter 录音，再按 Enter 打断 |
| 默认 | `run_always_listening()` | Porcupine 检测 "Hey Jarvis"，30s 静默超时回到监听 |

---

## 模块 2：大脑层

### 整体流程

```
text (来自输入层)
    │
    ├── 快路径检查（不走任何 API）
    │   ├─ "记住xxx" → 直接 "好的，记住了"
    │   ├─ LearningRouter → create 模式 → SkillFactory 造技能
    │   └─ RuleManager.check_keyword() → 关键词触发自动化
    │
    │── 并行启动（ThreadPoolExecutor）
    │   ├─ IntentRouter.route(text)     → RouteResult
    │   └─ MemoryManager.query(text)    → memory_context
    │
    │── DirectAnswerer.try_answer()     → cosine>0.75? 直接回
    │
    ▼
    路由结果分发
    ├── tier="local" → LocalExecutor 处理
    │   └─ smart_home / info_query / time / automation
    └── tier="cloud" → LLMClient.chat_stream()
                       └─ 流式逐句 → on_sentence → TTS 管道
```

### 3 个组件详解

**1. IntentRouter** (`core/intent_router.py`)

本质是一个 **分类器**——把用户说的话归类成 6 种意图：

| 意图 | tier | 说明 |
|------|------|------|
| `smart_home` | local | 开灯关灯调温度 |
| `info_query` | local | 新闻/股票/天气 |
| `time` | local | 几点了/今天星期几 |
| `automation` | local | 创建/删除自动化规则 |
| `complex` | cloud | 需要 LLM 才能回答 |
| `uncertain` | cloud | 模型没把握 |

实现方式：
- 一次 LLM API 调用（`temperature=0, response_format=json_object`），prompt 里列出了所有设备和 JSON 格式要求
- **两层 fallback**：Groq → Cerebras → 放弃（直接走云端 LLM）
- **LRU 缓存 256 条**：去掉标点后当 key，重复指令零延迟
- 5 秒超时，429 限流自动降级

关键设计：system prompt 是**动态生成**的（`build_system_prompt()`），从 config 读设备列表注入 prompt，这样添加新设备不需要改代码。

**2. Personality** (`core/personality.py`)

给 LLM 的 system prompt，由 4 层叠加组成：

```
<personality>    ← 核心人设："小月"，克制的老朋友
<output_rules>   ← 口语化、不用 markdown、最多 3-4 句
<memory>         ← MemoryManager.query() 注入的记忆（≤500 tokens）
<situation>      ← 动态拼接：
    时段语气（凌晨轻声、深夜简短）
    + 用户情绪（SenseVoice 检测的 HAPPY/SAD/ANGRY...）
    + 情境修饰（urgent/error/rapid）
    + 用户身份（"现在是 Allen 在跟你说话"）
```

重点：
- `_BASE_PERSONALITY` 是硬编码的，**不从 config 读**（只有用户允许才能改 prompt）
- 情绪映射很细腻，不是简单的"开心就开心"，而是给具体行为指导："他在气头上。别火上浇油，也别说教"

**3. LLMClient** (`core/llm.py`)

两个后端，统一接口：

| 后端 | 模型 | 场景 |
|------|------|------|
| Anthropic | Claude Sonnet | 默认 |
| OpenAI | GPT-4o / DeepSeek / Moonshot | 配 base_url 可切 |

核心机制：

- **Tool-use 循环**：最多 10 轮。LLM 返回 tool_use → 调 `tool_executor` → 把结果塞回 messages → 再请求 LLM
- **流式逐句输出**（`chat_stream()`）：
  - 收到 token → 累积到 buffer
  - 遇到句末标点（`。！？.!?；\n`）→ 调 `on_sentence` → 立即送 TTS
  - 特殊处理：小数点 `3.14` 不算分句
  - 如果流中检测到 tool_use → 中断流式，回退到普通 `chat()` 走完整 tool loop
- **历史截断**：按 `max_history_tokens`（默认 8000）从旧到新删，保留最近上下文
- **重试**：指数退避 `2^n` 秒，只重试瞬态错误（timeout, 429, 503, 500）

### jarvis.py 里怎么串的

`_handle_utterance_inner()` 第 614-744 行：

1. **并行**启动 `route_future` + `memory_future`
2. 等两个 future 回来
3. `route.tier == "local"` → `LocalExecutor` 处理（模块 4 的事）
4. 本地处理返回 `REQLLM` → 让 LLM 用"小月语气"转述数据
5. `response_text` 还是 None → 走 `chat_stream()`，全权交给 LLM

---

## 模块 3：记忆系统

### 整体架构（6 个模块，3 层）

```
┌─ 编排层 ─────────────────────────────────────────────┐
│  MemoryManager (manager.py)                          │
│  总调度：save / query / maintain                      │
│  调 LLM 做提取、去重、合并决策                         │
└──┬──────────┬──────────┬─────────────────────────────┘
   │          │          │
┌──▼──┐  ┌───▼───┐  ┌───▼────┐
│Store│  │Embedder│  │Retriever│   ← 存储+检索层
└──┬──┘  └───────┘  └────────┘
   │
   │  SQLite (data/memory/jarvis_memory.db, WAL 模式)
   │
独立模块（不走 SQLite）：
  ConversationStore → data/conversations/{user_id}.json（滑动窗口 20 轮）
  UserPreferences  → data/memory/{user_id}.json
  DirectAnswerer   → 无存储，读 Store + Embedder 做即时判断
```

### Save 管线（用户说完话，后台发生什么）

```
用户对话 messages[]
    │
    ▼
① _call_llm_extract()
   把整段对话发给 GPT-4o-mini（temperature=0, json_object 格式）
   prompt 包含：提取规则 + 用户名 + 当前画像 + 已有记忆（最近30条）
   返回 JSON：memories[], corrections[], profile_update, episode_summary
    │
    ▼
② 处理 corrections
   遍历 corrections[]，在 Store 里按 content LIKE 模糊匹配，deactivate 旧记忆
    │
    ▼
③ 逐条处理 memories[]（三层递进去重）

   对每条提取出的记忆：
   ┌─ 有 key？── 是 ──→ find_by_key(user_id, category, key)
   │                     找到？→ supersede 旧记忆，写入新记忆
   │                     没找到？→ 直接写入
   │
   └─ 无 key ──→ Embedder.encode(content) 生成向量
                  Retriever.find_similar(embedding, top_k=5)
                  │
                  ├─ 最高相似度 < 阈值？→ 直接写入（全新信息）
                  │   同类阈值: 0.55  跨类阈值: 0.70
                  │
                  └─ ≥ 阈值 → _call_llm_dedup() LLM 最终裁决
                     返回 ADD / UPDATE / NONE
                     UPDATE → supersede 目标，写入新记忆
                     NONE → 跳过（已有相同信息）
    │
    ▼
④ 更新用户档案
   LLM 返回了 profile_update → 直接 set_profile()
   没返回但有 identity/preference/relationship 变化 → _rebuild_profile() 自动重建
    │
    ▼
⑤ 存储 episode
   add_episode(summary, mood, topics) → episodes 表
```

### Query 管线（用户问了一句话，注入什么记忆）

```
用户当前输入 text
    │
    ▼
先走 DirectAnswerer.try_answer()
  └─ 只看 {identity, preference, knowledge} 类记忆
     纯余弦相似度 > 0.75？
     是 → 直接返回模板回答，不走 LLM（<100ms）
         "你跟我说过，{content}"
         "我记得，{content}"
     否 → 继续往下
    │
    ▼
MemoryManager.query(text, user_id)
    │
    ▼
_format_memory_context() 三层分级注入（总预算 1200 字符 ≈ 500 tokens）

  Tier 1: 用户档案（最高优先）
    get_profile() → JSON → 自然语言
    "Allen，住加拿大，开发者..."

  Tier 2: 最近事件（次高优先）
    get_recent_episodes(days=3) → 最近3天的对话摘要

  Tier 3: 相关记忆
    记忆数 < 100 → 全部注入（小库直接全给）
    记忆数 ≥ 100 → Embedder.encode(text) → Retriever.retrieve(top_k=5)

  Tier 4: 待关心（profile.pending 里到期的项目）

  按顺序填充，直到 1200 字符用完
    │
    ▼
返回 <memory>...</memory> XML 块，注入到 LLM 的 system prompt
末尾附使用原则："像朋友一样自然运用，不要强行提起无关记忆"
```

### 6 个组件详解

**1. MemoryManager** (`memory/manager.py`)
- 唯一的公开接口：`query()` / `save()` / `maintain()`
- save 跑在后台线程（`_executor.submit()`），不阻塞主管线
- 3 种 LLM 调用：提取（`_call_llm_extract`）、去重（`_call_llm_dedup`）、合并（`_call_llm_merge`）
- 都走 OpenAI 兼容接口（`_call_openai_json()`），直接用 requests，不走 LLMClient
- `_rebuild_profile()`：从活跃记忆自动重建用户画像，按 category 分拣到 identity/preferences/relationships/pending

**2. MemoryStore** (`memory/store.py`)
- SQLite 持久化，WAL 模式（写不阻塞读）
- 3 张主表 + 自动 migration（v2 加 key 列，v3 加 expires 列）
- `supersede_memory(old, new)`：**不删旧记忆**，标记 `superseded_by` + `active=0`，保留历史链
- `find_by_key(user_id, category, key)`：确定性去重的核心——同 category+key 视为同一事实
- `deactivate_memory(user_id, content_match)`：correction 场景用的模糊匹配停用
- `touch_many(ids)`：批量更新 access_count + last_accessed（被检索时自动触发）
- `get_embedding_index()`：为 maintain 优化——只取 id/content/category/embedding，不反序列化全部字段
- embedding 序列化：numpy float32 → `tobytes()` → BLOB 列

**3. Embedder** (`memory/embedder.py`)
- 模型：**BAAI/bge-small-zh-v1.5**，512 维，中文优化，~90MB
- 运行时：FastEmbed（ONNX Runtime），CPU 推理
- lazy load + 线程安全（`threading.Lock`）
- **单条缓存**：连续两次编码同一文本，直接返回上次结果
- 输出自动 L2 归一化（unit-norm），余弦相似度 = 点积

**4. MemoryRetriever** (`memory/retriever.py`)
- 多信号加权评分（retrieve 模式，用于 query）：

  ```
  score = 0.40 × cosine_similarity        # 语义相关性
        + 0.25 × recency_score            # 最近访问 1/(1+days)
        + 0.20 × importance_score          # 重要度×衰减×强化
        + 0.15 × access_frequency_score    # 被查频率 min(count,10)/10
  ```

- importance 衰减按类别：identity/knowledge 365天半衰期，event 30天，task 14天
- reinforcement = 1 + 0.05 × min(access_count, 20)，上限 2.0（越常用越不容易衰减）
- 过期记忆（expires < today）打 0.5 倍惩罚
- `find_similar()`：纯余弦，用于 save 管线去重，**不更新 access_count**

**5. DirectAnswerer** (`memory/direct_answer.py`)
- 快路径：绕过 LLM，直接从记忆回答简单事实查询
- 只看 3 类记忆：identity / preference / knowledge
- 全部加载到内存 → 矩阵点积 → 最高分 ≥ 0.75 → 用模板回答
- 3 个模板：
  - preference: "你跟我说过，{content}"
  - identity: "我记得，{content}"
  - knowledge: "你之前告诉过我，{content}"
- 命中后自动 touch_memory 更新访问计数

**6. ConversationStore** (`memory/conversation.py`)
- 短期记忆：最近 20 轮对话（`max_turns * 2` 条消息）
- 按 user_id 分文件存 JSON：`data/conversations/{user_id}.json`
- 首次访问从磁盘加载，之后内存 serve
- 每次 append/replace 后自动 trim + 持久化

### 6 类记忆的规则

| 类别 | key 必填？ | expires 必填？ | 半衰期 | 去重方式 | 举例 |
|------|-----------|---------------|--------|---------|------|
| identity | 是 | 否 | 365天 | 确定性（key） | 名字、职业 |
| preference | 是 | 否 | 180天 | 确定性（key） | 喜欢咖啡 |
| relationship | 是 | 否 | 180天 | 确定性（key） | 女朋友叫小美 |
| knowledge | 是 | 否 | 365天 | 确定性（key） | WiFi密码 |
| event | 否 | 是(+1天) | 30天 | 嵌入+LLM | 明天有面试 |
| task | 否 | 是(+1天) | 14天 | 嵌入+LLM | 下周交报告 |

### Maintain 管线（定时维护）

- 定时跑（scheduler 触发）
- 遍历所有活跃记忆，两两余弦相似度 > 0.8 → 调 `_call_llm_merge()` 裁决
- 每次最多检查 10 对（控制 LLM 成本）
- MERGE → supersede 信息量少的那条
- KEEP_BOTH → 跳过

### SQLite Schema（3 表）

```sql
memories (
  id, user_id, content, category, key,
  importance, created_at, updated_at, last_accessed, access_count,
  source, time_ref, expires, tags,
  superseded_by, active, embedding BLOB
)

user_profiles (
  user_id PRIMARY KEY, profile TEXT(JSON), updated_at
)

episodes (
  id, user_id, session_id, summary, date, mood, topics TEXT(JSON), created_at
)
```

### 关键设计决策

1. **supersede 而非 delete** — 旧记忆不删，标记 `superseded_by`，保留历史链
2. **三层去重** — key 确定性 → 嵌入模糊 → LLM 最终裁决，平衡精度和成本
3. **注入预算硬限制** — 1200 字符（~500 tokens），防止记忆淹没 LLM 上下文
4. **小库全注入** — 记忆 < 100 条时不走向量检索，直接全给
5. **DirectAnswer 快路径** — 简单事实 cosine > 0.75 直接回答，省掉 LLM 调用
6. **WAL 模式** — SQLite 写不阻塞读，支持 save 后台线程和 query 主线程并发
7. **记忆 LLM 独立于对话 LLM** — manager 直接用 requests 调 OpenAI 接口，不经过 LLMClient

---

## 模块 4：执行层

### 整体流程

```
RouteResult (来自 IntentRouter)
    │
    ▼
LocalExecutor — 根据 intent 分发
    │
    ├─ smart_home → SkillRegistry.execute("smart_home_control", {...})
    │                └─ SmartHomeSkill → PermissionManager → DeviceManager
    │                    └─ SimLight / HueLight / MqttDevice.execute()
    │
    ├─ info_query → SkillRegistry.execute("get_stock_watchlist" / "get_news_briefing" / ...)
    │                └─ RealTimeDataSkill → YahooFinance / GNews
    │                返回 REQLLM → LLM 转述
    │
    ├─ time → 本地 datetime，直接返回文字，不走任何外部调用
    │
    ├─ automation → RuleManager.create_rule / list_rules / delete_rule
    │
    └─ skill_alias → SkillRegistry.execute(tool_name, params)
                     关键词触发指定 skill，返回 REQLLM
```

### 3 个核心组件

**1. LocalExecutor** (`core/local_executor.py`)

纯分发器，**没有业务逻辑**，只做两件事：
- 根据 `intent` 调对应的 skill/manager
- 决定返回类型：`RESPONSE`（直接 TTS 播报）还是 `REQLLM`（交给 LLM 转述）

| 方法 | intent | 返回类型 | 说明 |
|------|--------|---------|------|
| `execute_smart_home` | smart_home | RESPONSE | 遍历 actions[]，逐个调 skill，失败则报错 |
| `execute_info_query` | info_query | REQLLM | 数据丢给 LLM 用小月语气转述 |
| `execute_time` | time | RESPONSE | 本地 datetime，零延迟 |
| `execute_automation` | automation | RESPONSE | CRUD 自动化规则 |
| `execute_skill_alias` | skill_alias | REQLLM | 关键词触发的 skill 快捷方式 |

**2. SkillRegistry + Skill ABC** (`skills/__init__.py`)

技能框架，仿 DeviceManager 的注册模式：

```python
class Skill(ABC):
    skill_name: str               # 唯一标识
    get_tool_definitions()        # 返回 Claude tool schema
    execute(tool_name, input)     # 执行并返回文本
    get_required_role() -> str    # 最低权限（默认 guest）
```

SkillRegistry 维护两个映射：
- `_skills`: skill_name → Skill 实例
- `_tool_map`: tool_name → Skill 实例（一个 skill 可以暴露多个 tool）

**角色过滤**：`get_tool_definitions(user_role)` 只返回用户权限够的 tool。4 级角色：guest(0) < member/resident(1) < family/admin(2) < owner(3)

两种使用场景：
- **LocalExecutor 直接调** — `registry.execute("smart_home_control", {...})`
- **LLM tool-use 循环** — `registry.get_tool_definitions()` 给 LLM，LLM 返回 tool_use 时 `registry.execute()` 做 dispatcher

**14 个内置 skill**：

| Skill | Tool(s) | 说明 |
|-------|---------|------|
| smart_home | smart_home_control, smart_home_status | 桥接 DeviceManager |
| weather | get_weather | 天气查询 |
| time_skill | - | 时间/日期 |
| todos | create_todo, list_todos, complete_todo | 待办事项 |
| reminders | create_reminder | 提醒 |
| automation | manage_automation | 自动化规则 CRUD |
| realtime_data | get_news_briefing, get_stock_watchlist | GNews + Yahoo Finance，后台定时刷新 |
| remote_control | - | WebSocket 远程控制 |
| memory_skill | - | 记忆相关操作 |
| health_skill | - | 系统健康查询 |
| system_control | - | 系统管理 |
| scheduler_skill | - | 定时任务管理 |
| skill_mgmt | - | 技能管理（学习技能） |

加上 `skills/learned/` 下的动态加载技能（目前 1 个：exchange_rate 汇率查询）。

**3. DeviceManager + SmartDevice ABC** (`devices/`)

三种后端，统一接口：

```python
class SmartDevice(ABC):
    device_id: str
    name: str
    device_type: str          # light / door_lock / thermostat
    required_role: str
    execute(action, value) -> str
    get_status() -> dict
```

| 后端 | 实现 | 来源 |
|------|------|------|
| **sim/** | SimLight, SimDoorLock, SimThermostat | 内存模拟，开发用 |
| **hue/** | HueBridge → HueLight, HueGroup, HueScene | Philips Hue REST API |
| **mqtt/** | MqttClient → MqttDevice, MqttSensor | MQTT broker（ESP32 等） |

DeviceManager 初始化逻辑：
- `mode=sim` → 从 `config.yaml` 的 `sim_devices` 列表实例化模拟设备
- `mode=live` → 连 Hue Bridge，自动发现灯/组/场景，用 `light_aliases` 映射为 device_id
- MQTT **可组合**：无论 sim 还是 live 模式都可以额外加载 MQTT 设备

SmartHomeSkill 是桥接层：
- 接收 LLM 的 tool_use 请求
- 通过 PermissionManager 检查权限
- 调 DeviceManager.execute_command() 执行
- 返回文本结果给 LLM

### 权限模型

```
PermissionManager.check_permission(user_role, device, action)
    └─ 比较 user_role 级别 vs device.required_role 级别
       guest(0) < member(1) < family(2) < owner(3)
```

每个设备在 config.yaml 里配 `required_role`，SmartHomeSkill 在执行前检查。

### 关键设计决策

1. **两种返回模式** — RESPONSE 直接播报（零延迟），REQLLM 交给 LLM 转述（有温度）
2. **Skill 框架仿 Device 模式** — 统一 ABC + Registry + dispatch，扩展只需继承
3. **设备后端可组合** — sim/live 是互斥的，但 MQTT 可以叠加在任何模式上
4. **权限贯穿始终** — 从 SkillRegistry 的 tool 过滤到 SmartHomeSkill 的执行检查，双重保险
5. **info_query 返回 REQLLM** — 原始数据不直接播报，让 LLM 用人话转述

---

## 补充模块：学习系统 + 自动化 + UI

### 学习系统（LearningRouter + SkillFactory）

用户可以通过语音教小月新技能，3 级复杂度：

| 模式 | 关键词 | 说明 | 实现 |
|------|--------|------|------|
| **config** | "以后说X就Y" | 给现有 skill 设快捷方式 | 正则匹配 → 创建 keyword 自动化规则 |
| **compose** | "每天/每周/定时" | 串联多个 skill + 定时 | 交给 LLM + automation skill 处理 |
| **create** | "学会/帮我加一个" | 需要全新代码 | SkillFactory → Claude Code CLI 生成 |

**SkillFactory** 的 create 流程（`core/skill_factory.py`）：
1. 准备上下文：Skill ABC 源码 + weather.py 范例
2. 构造 prompt → 调 `claude -p` CLI（120s 超时）
3. 检测 `skills/learned/` 下新增/变更的 .py 文件
4. **安全扫描**：正则检查 16 种危险模式（os.system, subprocess, eval, exec, pickle, ctypes, socket...）
5. 跑 pytest 测试
6. 全部通过 → 返回成功，skill 可以被 SkillLoader 动态加载

### 自动化系统

两个组件：

**AutomationRuleManager** (`core/automation_rules.py`) — 规则 CRUD + 触发
- 3 种触发：keyword（说"晚安"触发）、cron（每天 7:00）、once（30 分钟后）
- JSON 持久化：`data/automation_rules.json`，原子写入（mkstemp → os.replace）
- cron/once 规则自动注册到 JarvisScheduler
- `check_keyword(text)` 在主管线里每次都跑，完全匹配或前缀匹配

**AutomationEngine** (`core/automation_engine.py`) — 场景执行
- 从 config.yaml 的 `automations` 注册预设场景（晚安/回家/出门）
- 每个场景是一组步骤，5 种 step type：
  - `device` — 调 DeviceManager 控制设备
  - `speak` — TTS 说一句话
  - `delay` — sleep N 秒
  - `oled` — 通过 EventBus 切换 OLED 显示帧
  - `event` — 发射自定义事件

### Web UI (`ui/dashboard.py`)

Gradio Dashboard，5 个面板：
- 语音输入 → ASR + 意图路由 + LLM → 文本回复（**不播 TTS**，纯文本交互）
- 设备控制面板
- 系统状态
- 对话历史
- 配置

`DashboardController` 是 JarvisApp 的轻量包装，跳过 TTS 和声纹验证，专注文本交互。

---

## 全链路数据流总结

```
                        ┌─────────────────────────────────────────────────┐
                        │               jarvis.py (JarvisApp)             │
                        │                                                 │
  Hey Jarvis ──→ Porcupine ──→ AudioRecorder ──→ float32 audio           │
  或 Enter                      (16kHz, VAD)      │                       │
                                                   │                       │
                        ┌──────────────────────────┼──────────┐           │
                        │         并行              │          │           │
                        │  SpeakerVerifier    SpeechRecognizer│           │
                        │  (ECAPA-TDNN)       (SenseVoice)    │           │
                        │     ↓                    ↓          │           │
                        │  user_id/None    text+emotion+event │           │
                        └──────────────┬──────────────────────┘           │
                                       │                                   │
                        快路径 ─────────┤                                   │
                        "记住xxx"       │ "学会xxx" → SkillFactory          │
                        → 直接确认      │ keyword → RuleManager             │
                        DirectAnswer    │ cosine>0.75 → 模板回答            │
                                       │                                   │
                        ┌──────────────┼──────────┐                       │
                        │    并行       │          │                       │
                        │ IntentRouter  │  MemoryManager.query()          │
                        │ (Groq/Cerebras│  (三层注入 ≤500 tokens)          │
                        │  LRU 256)     │          │                       │
                        └──────┬───────┴──────────┘                       │
                               │                                           │
                  ┌────────────┼────────────┐                             │
                  │ tier=local  │ tier=cloud │                             │
                  │             │            │                             │
            LocalExecutor       │   LLMClient.chat_stream()               │
            ├─ smart_home       │   (Claude/GPT-4o + tool-use)            │
            ├─ info_query       │   personality prompt:                    │
            ├─ time             │   人设+时段+情绪+记忆                     │
            ├─ automation       │            │                             │
            └─ skill_alias      │            │                             │
                  │             │            │                             │
                  │  REQLLM ────┘   逐句输出  │                             │
                  │                    │      │                             │
                  │             on_sentence() │                             │
                  │                    │      │                             │
                  └────────────────────┼──────┘                            │
                                       │                                   │
                                TTSPipeline                                │
                          text_queue → [合成线程] → audio_queue → [播放线程]│
                          5引擎降级 + 情感映射 + 磁盘缓存                    │
                                       │                                   │
                                     喇叭 🔊                               │
                                       │                                   │
                        后台 ──────────┤                                   │
                        MemoryManager.save() → LLM 提取 → 三层去重 → SQLite│
                        ConversationStore → 滑动窗口 20 轮 → JSON          │
                        BehaviorLog → 行为事件追加                          │
                        └─────────────────────────────────────────────────┘
```

### 数字汇总

- **~9,500 行**核心代码（core + skills + memory + devices + auth）
- **742 个测试**，53 个测试文件
- **14 个内置技能** + 1 个学习技能（exchange_rate）
- **5 个 TTS 引擎**（OpenAI → MiniMax → Azure → edge-tts → pyttsx3）
- **3 种设备后端**（sim / hue / mqtt）
- **6 类记忆** + 三层去重 + 三层注入
- **2 种 LLM 后端**（Anthropic Claude / OpenAI 兼容）
- **2 层意图路由 fallback**（Groq → Cerebras → 直接云端）

---

## 模块 5：输出 + 基础设施

### 5A. TTS 引擎 (`core/tts.py`)

#### 5 引擎降级链

```
配置的首选引擎（engine_name）
    │
    ├─ openai_tts  → gpt-4o-mini-tts（ChatGPT 同款，最富表现力）
    ├─ minimax     → MiniMax speech-02-turbo（中文音质好，有情感参数）
    ├─ azure       → Azure Neural TTS（SSML 情感风格控制）
    ├─ edge-tts    → 免费微软神经网络语音（无情感）
    └─ pyttsx3     → 离线本地引擎（机器人音，零延迟）
```

每个引擎失败后自动 fallback 到下一个，最终兜底 pyttsx3。熔断器（ComponentTracker）参与：如果某引擎标记为 UNAVAILABLE，直接跳过。

#### 情感映射（3 套）

SenseVoice 检测到的用户情绪 → 转化为小月的**回应风格**（不是复读用户情绪）：

| 用户情绪 | OpenAI instruction | Azure SSML style | MiniMax |
|---------|-------------------|-------------------|---------|
| HAPPY | "语气愉快轻松" | cheerful | happy |
| SAD | "语气温柔关心" | gentle | sad |
| ANGRY | "语气平静沉稳" | calm | calm |

OpenAI TTS 用自然语言 instruction，Azure 用 SSML `<mstts:express-as style>`，MiniMax 直接传 emotion 参数。

#### 磁盘缓存

- 短文本 ≤ 50 字 → MD5 hash 做 key → `data/cache/tts/{hash}.mp3`
- 缓存命中直接播放，省掉 API 调用
- LRU 淘汰：超过 500 个文件删最旧的
- 原子写入：先写 `.tmp`，再 `os.rename`

#### TTSPipeline — 双线程管道

```
LLM 流式逐句输出
    │ on_sentence("第一句话。")
    ▼
text_queue → [TTS 合成线程] → audio_queue → [播放线程]
             synth_to_file()                  _play_audio_file()
```

- **TTS 线程**：从 text_queue 取句子 → 合成到临时文件 → 放入 audio_queue
- **Play 线程**：从 audio_queue 取文件 → 播放（Mac: afplay, Linux: mpv/ffplay/aplay）
- 效果：句子 N+1 在句子 N 播放时就开始合成，**消除句间停顿**
- 支持 abort()：用户打断时清空两个队列
- `_SENTINEL` 对象做终止信号

#### 播放器跨平台

| 平台 | 播放命令 |
|------|---------|
| macOS | `afplay` |
| Linux | `mpv` → `ffplay` → `aplay`（按序尝试） |
| Windows | PowerShell `SoundPlayer` |

### 5B. 熔断器 (`core/health.py`)

三态状态机，每个组件独立跟踪：

```
HEALTHY ──3次连续失败──→ DEGRADED ──10次连续失败──→ UNAVAILABLE
   ↑                        │                          │
   └── 任何成功 ←───────────┘                          │
   ↑                                                   │
   └── probe 成功 ←────────────────────────────────────┘
```

| 状态 | is_available() 返回 | 行为 |
|------|-------------------|------|
| HEALTHY | true | 正常使用 |
| DEGRADED | cooldown 60s 内 false，过后 true（试一次） | 降级中，偶尔探测 |
| UNAVAILABLE | false | 只有 probe 成功才能恢复 |

关键 API：
- `record_success(component)` — 任何成功立即回 HEALTHY
- `record_failure(component)` — 累积失败计数，触发状态转换
- `is_available(component)` — fallback 链用来决定跳不跳过
- `register_probe(component, fn)` — 注册健康探测函数
- `run_all_probes()` — 定时调所有探测（scheduler 每 60s 跑一次）

已注册的 probes：
- `intent.groq` — GET /v1/models（免费，不消耗 token）
- `tts.openai` — GET /v1/models
- `asr.sensevoice` — 检查模型文件是否存在

EventBus 事件：
- `health.status_changed` — 状态转换时发（jarvis.py 监听，首次降级语音提醒）
- `health.recovery` — 恢复时发（含 downtime 时长）
- `health.check_completed` — 全部 probe 跑完时发

### 5C. 调度器 (`core/scheduler.py`)

APScheduler 薄封装 + SQLite 持久化：

- 3 种触发方式：`add_date_job`（一次性）、`add_cron_job`（周期）、`add_interval_job`（固定间隔）
- `coalesce=True` — 错过的执行合并成一次
- `max_instances=1` — 同一 job 不并发
- APScheduler 未安装时优雅降级（`available=False`）

在 jarvis.py 里注册的定时任务：
- 健康探测：每 60s `run_all_probes()`
- 记忆维护：定时 `memory_manager.maintain_all()`
- 新闻/股票刷新：RealTimeDataSkill 注册的后台刷新
- 自动化规则：cron 类型的规则通过 scheduler 触发

### 5D. EventBus (`core/event_bus.py`)

轻量级同步 pub/sub，支持通配符：

```python
bus.on("device.*", callback)       # 匹配 device.light, device.thermostat 等
bus.emit("device.light", {...})    # 触发上面的 callback
```

- 线程安全（`threading.Lock`）
- listener 异常不影响其他 listener 和 caller（catch + log）
- 用途：健康状态变化通知、OLED 显示更新、设备事件

### 关键设计决策

1. **双线程管道** — TTS 合成和播放解耦，句间零停顿
2. **三态熔断器** — 不是简单的开/关，DEGRADED 状态允许探测恢复
3. **磁盘缓存 + 原子写入** — 常见短回复秒出声，不怕写入中断
4. **情感贯穿全链路** — SenseVoice 检测 → personality prompt → TTS 风格，三层都用
5. **APScheduler + SQLite** — 重启后定时任务不丢
6. **EventBus 通配符** — 松耦合，新模块订阅事件不需要改发送方
