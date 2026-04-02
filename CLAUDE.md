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
python -m pytest tests/ -v        # 490 tests, 82% coverage
python -m pytest tests/test_<module>.py -v

# 其他
python ui/dashboard.py            # Gradio Web UI
python setup_hue.py               # Hue Bridge 配对
```

## 技术栈

Python 3.11 · venv/ · config.yaml 统一配置
ASR: SenseVoice-Small INT8 (sherpa-onnx) · 声纹: SpeechBrain ECAPA-TDNN
LLM: GPT-4o-mini (主) / DeepSeek / Kimi K2.5 · 路由: Groq llama-3.3-70b
TTS: OpenAI TTS / MiniMax / Azure Neural / edge-tts / pyttsx3
唤醒词: Porcupine · 设备: Philips Hue + MQTT + 模拟 · 远程: WebSocket

## 架构

```
麦克风 → 唤醒词 → 录音(VAD) → [声纹验证 + SenseVoice ASR 并行]
  → 意图路由 (Groq/DeepSeek/Ollama)
  → 本地执行 or 云端 LLM (流式逐句)
  → TTS (情感风格) → 喇叭
```

## 项目结构

- `core/` — ASR、LLM、TTS、人格、意图路由、自动化规则、录音、声纹、事件总线
- `skills/` — LLM function calling 技能（继承 Skill ABC）
- `devices/` — SmartDevice ABC → sim/ hue/ mqtt/
- `auth/` — 声纹注册 + 角色权限
- `memory/` — 对话历史 + 用户偏好
- `realtime_data/` — 新闻/股票数据服务
- `notes/` — 调研笔记和工作记录

## 关键文件

- `config.yaml` — 所有可调参数
- `core/personality.py` — "小贾" 人格系统（动态 prompt）
- `core/speech_recognizer.py` — SenseVoice + Whisper 双引擎
- `core/tts.py` — 5 个 TTS 引擎 + 情感映射
- `core/llm.py` — 多 LLM 后端 + 流式输出
- `skills/__init__.py` — Skill ABC + SkillRegistry

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

