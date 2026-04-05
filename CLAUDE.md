# 小贾 (Jarvis) — 私人语音管家

## 行为准则（最重要，必须遵守）

### 沟通方式
- 用户问什么先直接回答，不要先探索一圈再说
- 不要主动生成文件或长输出。先回答问题，再问要不要生成代码
- 用户要做什么就帮他做，不要 push back 或建议替代方案（除非用户问你）
- 快问快答。用户要简短回答就给简短回答

### 代码修改
- 最小化改动。不要从头重写文件
- 用户说"简化"意味着在原代码基础上简化，不是用另一种方式重写
- 改库/换框架前先问用户。不要自行决定把 sklearn 换成 numpy 之类的
- 每次改完跑 `python -m pytest tests/ -q` 确认没破坏

### Git 操作
- commit 可以做，但 **push 必须等用户明确要求**
- commit message 不要加 Co-Authored-By

### Agent 使用
- 不要随便启动大量 Agent 搜索。先用 Grep/Glob 直接找
- 启动 Agent 前想清楚范围，不要无目标地"全面探索"

## 快速参考

```bash
# 运行
python jarvis.py --no-wake        # 开发（按键录音）
python jarvis.py                  # 生产（唤醒词 "Hey Jarvis"）

# 测试
python -m pytest tests/ -v        # 806 tests
python -m pytest tests/test_<module>.py -v

# 其他
python ui/dashboard.py            # Gradio Web UI
python setup_hue.py               # Hue Bridge 配对
```

## 技术栈

Python 3.11 · venv/ · config.yaml 统一配置
ASR: SenseVoice-Small INT8 (sherpa-onnx) · 声纹: SpeechBrain ECAPA-TDNN
LLM: GPT-4o-mini (主) / DeepSeek · 路由: Groq 70B → Cerebras 8B fallback
TTS: OpenAI TTS / MiniMax / Azure Neural / edge-tts / pyttsx3
记忆: SQLite + FastEmbed (bge-small-zh) + GPT-4o-mini 提取
唤醒词: Porcupine · 设备: Philips Hue + MQTT + 模拟 · 远程: WebSocket

## 架构

```
麦克风 ��� 唤醒词 → 录音(VAD) → [声纹验证 + SenseVoice ASR 并行]
  → DirectAnswer 快路径 (cosine>0.55+margin? 直接回答，不走 LLM)
  → [意图路由 (Groq→Cerebras→云端) + 记忆查询] 并行
  → 本地执行 or 云端 LLM (流式逐句，注入记忆≤500tokens)
  → TTS 双线程管道 (5引擎降级 + 情感风格) → 喇叭
  → 后台: 记忆提取+去重+存储, 对话历史, 行为日志
```

## 项目结构

- `core/` — ASR、LLM、TTS、人格、意图路由(+LRU缓存)、健康监控(熔断器)、调度器、自动化规则/引擎、录音(+VAD)、声纹、事件总线、本地执行器、学习路由、技能加载/工厂
- `skills/` — LLM function calling 技能（继承 Skill ABC），14个内置 + `learned/` 动态加载
- `devices/` — SmartDevice ABC → sim/ hue/ mqtt/ 三种后端
- `auth/` — 声纹注册(3样本平均) + 角色权限(guest→owner 4级)
- `memory/` — MemoryManager(编排,function calling提取) + MemoryStore(SQLite,含relations表) + Embedder(bge-small-zh-v1.5) + Retriever(4信号+冷启动自适应) + DirectAnswerer(快路径) + BehaviorLog(行为日志) + ConversationStore(滑动窗口) + UserPreferences(KV)
- `ui/` — Gradio Dashboard(5面板) + OLED 显示控制
- `notes/` — 调研笔记和工作记录(14篇)

## 关键文��

- `jarvis.py` — 主入口，JarvisApp 初始化所有子系统
- `config.yaml` — 所有可调参数（500+ 行）
- `core/intent_router.py` — 意图路由，Groq→Cerebras 两层 fallback + LRU 256 缓存
- `core/llm.py` — 多 LLM 后端 + 流式逐句 + tool-use 循环 + 重试
- `core/tts.py` — 5 引擎降级链 + TTSPipeline 双线程 + 磁盘缓存 + 情感映射
- `core/personality.py` — "小贾" 人格系统（时段+情绪+记忆动态 prompt）
- `core/speech_recognizer.py` — SenseVoice INT8 + Whisper fallback
- `core/health.py` — 熔断器 (HEALTHY→DEGRADED→UNAVAILABLE) + 探针
- `core/local_executor.py` — 5种意图分发到对应 skill
- `core/learning_router.py` — 检测用户教学意图 (config/compose/create)
- `core/skill_factory.py` — Claude Code CLI 生成 skill + 安全扫描
- `memory/manager.py` — 记忆编排：save(LLM提取→去重→存储) / query(三层注入) / maintain
- `memory/store.py` — SQLite 持久化，3表 (memories/profiles/episodes)
- `memory/retriever.py` — 4信号加权评分 (cosine 0.4 + recency 0.25 + importance 0.2 + access 0.15)
- `memory/direct_answer.py` — 快路径：cosine>0.55 + margin>0.08 直接回答不走 LLM
- `skills/__init__.py` — Skill ABC + SkillRegistry（角色过滤）

## 编码规范

- Type Hints 全覆盖 · Google style docstring
- `logging` 模块，不用 `print`
- 配置从 config.yaml 读，不硬编码路径/阈值/API key
- 异常处理完善：硬件不可用时优雅降级
- 新技能继承 `skills.Skill`，实现 `name/description/parameters/execute`

## 不要做

- 不要修改 `data/speechbrain_model/` 或 `data/sensevoice-small-int8/` 下的模型文件
- 不要在代码中硬编码 IP、API key、文件路径
- 不要用 `print` 替代 `logging`
- 不要绕过 `permission_manager` 直接执行设备操作
- 不要自动 git push

