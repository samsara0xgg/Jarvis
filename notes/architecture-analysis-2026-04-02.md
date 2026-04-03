# 项目架构全面分析 — 2026-04-02

## 一句话总结

**一个"云端大脑 + 本地四肢"的语音助手。** 大脑（理解力、回答问题）完全依赖云端，本地只负责听、说、和执行几个固定动作。

---

## 完整数据流（一次对话发生了什么）

```
你说了一句话
    │
    ▼
┌─────────────────────────────────────────────────────┐
│ 第一步：输入                                          │
│                                                      │
│  麦克风 → AudioRecorder (VAD 检测静音自动停)           │
│  或者：Porcupine 唤醒词 → "在的" → 再录音              │
└───────────────────────┬─────────────────────────────┘
                        │ 音频 (numpy array)
                        ▼
┌─────────────────────────────────────────────────────┐
│ 第二步：识别（并行）                                   │
│                                                      │
│  线程1: SpeakerVerifier → 这是谁？(ECAPA-TDNN 声纹)   │
│  线程2: SpeechRecognizer → 说了什么？+ 什么情绪？       │
│         (SenseVoice INT8, 75ms)                      │
│                                                      │
│  输出: text="开灯", user="Allen", emotion="NEUTRAL"   │
└───────────────────────┬─────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────┐
│ 第三步：关键词检查                                     │
│                                                      │
│  AutomationRuleManager.check_keyword(text)           │
│  如果匹配（如 text="晚安" 匹配了"晚安模式"规则）        │
│  → 直接执行 actions → TTS 播报 → 结束                  │
│                                                      │
│  不匹配 → 继续往下                                     │
└───────────────────────┬─────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────┐
│ 第四步：意图路由（☁️ 云端调用 #1）                      │
│                                                      │
│  IntentRouter.route(text)                            │
│  → Groq llama-3.3-70b (免费, ~200ms)                 │
│  → 失败则 DeepSeek → 失败则本地 Ollama                 │
│                                                      │
│  返回 JSON:                                           │
│  {"intent": "smart_home", "actions": [...]}           │
│  {"intent": "info_query", "sub_type": "news"}         │
│  {"intent": "complex", "confidence": 0.85}            │
│                                                      │
│  6种intent:                                           │
│  smart_home / info_query / time / automation → 本地    │
│  complex / uncertain → 云端                           │
└──────────┬──────────────────────────┬───────────────┘
           │ 本地                      │ 云端
           ▼                           ▼
┌─────────────────────┐  ┌────────────────────────────┐
│ 第五步A：本地执行     │  │ 第五步B：云端 LLM            │
│                      │  │ （☁️ 云端调用 #2）            │
│ LocalExecutor:       │  │                             │
│                      │  │ LLMClient.chat_stream()     │
│ smart_home:          │  │ → GPT-4o-mini               │
│   设备控制            │  │                             │
│   → SkillRegistry    │  │ system prompt 包含:          │
│   → SmartHomeSkill   │  │  ✅ 小贾人格 (468字)         │
│   → DeviceManager    │  │  ✅ 时段语气                  │
│                      │  │  ✅ 用户情绪                  │
│ info_query:          │  │  ✅ 用户名                    │
│   news → GNews API   │  │  ❌ 用户偏好 (没传)           │
│   stocks → Yahoo API │  │  ❌ 情境状态 (永远 normal)    │
│   weather → API      │  │                             │
│                      │  │ 还有:                        │
│ time:                │  │  ✅ 对话历史 (最近20轮)       │
│   系统时钟直接回答     │  │  ✅ 11个skill工具可调用      │
│                      │  │  ❌ 没有用户记忆/偏好         │
│ automation:          │  │                             │
│   规则 CRUD           │  │ 如果是 REQLLM:              │
│                      │  │  本地查的数据 → 让 LLM 转述   │
│ 返回 ActionResponse:  │  │                             │
│  RESPONSE → 直接TTS   │  │                             │
│  REQLLM → 交给LLM转述 │  │                             │
└──────────┬───────────┘  └──────────┬──────────────────┘
           │                          │
           └────────┬─────────────────┘
                    │ response_text
                    ▼
┌─────────────────────────────────────────────────────┐
│ 第六步：输出                                          │
│                                                      │
│  本地路径: 直接 TTS 播报                               │
│  云端路径: 流式逐句 TTS (TTSPipeline 双线程)           │
│            LLM 生成一句 → 立刻合成 → 边合成边播放       │
│                                                      │
│  TTSEngine: MiniMax (带情绪风格)                      │
│  fallback: Azure → edge-tts → pyttsx3                │
└───────────────────────┬─────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────┐
│ 第七步：存储                                          │
│                                                      │
│  ConversationStore.replace() → JSON 文件              │
│  保存这轮对话历史（下次调 LLM 会带上）                  │
│                                                      │
│  ⚠️ UserPreferenceStore 存在但没人主动读              │
│  ⚠️ 用户偏好不会自动注入 prompt                        │
└─────────────────────────────────────────────────────┘
```

---

## 各组件现状清单

| 组件 | 代码 | 依赖 | 说明 |
|------|------|------|------|
| 唤醒词 | `core/wake_word.py` | Porcupine ☁️ | 需要 Picovoice key |
| 录音 | `core/audio_recorder.py` | sounddevice | VAD 自动停 |
| 声纹 | `core/speaker_verifier.py` | SpeechBrain 本地 | ECAPA-TDNN |
| ASR | `core/speech_recognizer.py` | sherpa-onnx 本地 | SenseVoice INT8, 75ms |
| 意图路由 | `core/intent_router.py` | Groq/DeepSeek ☁️ | 分类+参数提取 |
| 本地执行 | `core/local_executor.py` | 无 | 分发4种本地intent |
| 自动化规则 | `core/automation_rules.py` | 无 | keyword/cron/once |
| 自动化引擎 | `core/automation_engine.py` | 无 | scene执行 |
| LLM | `core/llm.py` | OpenAI/Anthropic ☁️ | GPT-4o-mini |
| 人格 | `core/personality.py` | 无 | 动态 system prompt |
| TTS | `core/tts.py` | MiniMax ☁️ | 5引擎+情绪映射 |
| 事件总线 | `core/event_bus.py` | 无 | pub/sub |
| 定时器 | `core/scheduler.py` | APScheduler | cron/once |
| 设备 | `devices/` | sim/Hue/MQTT | SmartDevice ABC |
| 技能 | `skills/` (11个) | 各自不同 | Skill ABC + Registry |
| 对话记忆 | `memory/conversation.py` | 无 | JSON, 20轮滑动窗口 |
| 用户偏好 | `memory/user_preferences.py` | 无 | JSON key-value |
| 新闻 | `realtime_data/` | GNews ☁️ | 4分类+缓存 |
| 股票 | `realtime_data/` | Yahoo ☁️ | watchlist+缓存 |

---

## 哪些部分真正在"本地"工作

**完全本地（断网也能用）**：
- 录音、ASR、声纹（模型文件在本地）
- 时间查询
- 设备控制（sim 模式）
- 自动化规则的 keyword 触发
- 对话历史和偏好的存取
- TTS fallback（pyttsx3, edge-tts 部分场景）

**必须联网**：
- 意图路由（Groq）← 每一句话都要调
- 云端 LLM（GPT-4o-mini）← 所有"聊天"类问题
- TTS（MiniMax）← 每一句回复都要调
- 新闻、股票、天气 ← 数据来源全是 API

---

## "偏离目标"在哪

目标："**大部分情况下用本地知识回复问题**"

现实情况：
1. **每句话至少 1 次云端调用**（意图路由 Groq），大部分是 2 次（+LLM）
2. **小贾没有知识**——她能做事（开灯、查新闻），但不会"知道"任何东西
3. **记忆断裂**——偏好存了但不注入 prompt，每次聊天小贾都不记得你上次说了什么偏好
4. **所有"聊"的能力都在 GPT-4o-mini 上**——"今天适合跑步吗""Python 怎么读文件""给我讲个笑话"全走云端
5. **realtime_data 不是知识库**——是数据管道，断网就没数据

**现在的小贾是一个耳朵和嘴巴长在本地，但大脑在云端的人。她能听到你（本地 ASR），能说话（但要云端 TTS），但"想"的部分完全依赖远程。**

---

## 建了什么有价值的东西

基础设施很扎实：
1. **输入管线完整** — 唤醒→录音→VAD→声纹+ASR 并行，这条链路很成熟
2. **Skill 框架好用** — ABC + Registry + 权限，加新能力很方便
3. **人格系统写得好** — 动态 prompt 组装，有情绪感知
4. **自动化规则可用** — keyword/cron/once 三种触发器，JSON 持久化
5. **TTS 管线成熟** — 双线程 pipeline 消除句间停顿，5 引擎 fallback
6. **测试覆盖 82%** — 490 个测试，基础很稳

问题不是代码质量，是**架构方向**：建了一个很好的"四肢"系统，但"大脑"完全外包了。
