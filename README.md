# J.A.R.V.I.S. — Personal AI Voice Assistant

声纹驱动的 AI 语音助手，支持智能家居控制、实时数据查询、自然语言理解和多层模型路由。

## 功能

- **唤醒词检测** — Porcupine "Hey Jarvis"，免提激活
- **声纹验证** — SpeechBrain ECAPA-TDNN，角色权限控制（owner > family > member > guest）
- **语音识别** — OpenAI Whisper，中文
- **意图路由** — 三层 fallback：Groq（~300ms）→ DeepSeek → 本地 Ollama
- **LLM 对话** — Claude / GPT-4o，支持 function calling
- **TTS 语音合成** — edge-tts（中文 YunxiNeural），pyttsx3 备用
- **智能家居** — Philips Hue（真实）+ 模拟设备（开发用）+ MQTT
- **技能系统** — 天气、提醒、待办、定时、记忆、自动化、实时数据（新闻/股票）
- **远程控制** — WebSocket 协议控制 Mac
- **Web UI** — Gradio 仪表盘

## 架构

```
用户语音 → 唤醒词 → 录音 → [声纹验证 + ASR 并行]
                                    ↓
                            意图路由（Groq/DeepSeek/Ollama）
                                    ↓
                    ┌───────────────┼───────────────┐
                    ↓               ↓               ↓
               smart_home      info_query       complex
               本地执行         技能调用       云端 LLM (Claude)
                    ↓               ↓               ↓
                    └───────────────┼───────────────┘
                                    ↓
                                TTS → 喇叭
```

## 快速开始

```bash
# 安装
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 配置 API keys（在 ~/.bashrc 中）
export GROQ_API_KEY="..."
export DEEPSEEK_API_KEY="..."
export OPENAI_API_KEY="..."

# 运行
python jarvis.py --no-wake    # 开发模式（按键录音）
python jarvis.py              # 生产模式（唤醒词）
```

## 项目结构

```
Jarvis/
├── jarvis.py              # 主入口，语音助手完整 pipeline
├── main.py                # 旧版确定性指令模式（保留）
├── config.yaml            # 所有可调参数
├── core/                  # 核心模块
│   ├── intent_router.py   # 三层意图路由（Groq→DeepSeek→Ollama）
│   ├── local_executor.py  # 本地指令执行
│   ├── local_llm.py       # Ollama 客户端
│   ├── llm.py             # 云端 LLM（Claude/GPT-4o）
│   ├── speech_recognizer.py
│   ├── speaker_verifier.py
│   ├── speaker_encoder.py
│   ├── audio_recorder.py
│   ├── tts.py
│   ├── wake_word.py
│   ├── event_bus.py       # 模块间事件通信
│   ├── scheduler.py       # 定时任务
│   └── automation_engine.py
├── skills/                # LLM function calling 技能
│   ├── __init__.py        # Skill ABC + SkillRegistry
│   ├── smart_home.py
│   ├── weather.py
│   ├── time_skill.py
│   ├── reminders.py
│   ├── todos.py
│   ├── realtime_data.py   # 新闻/股票
│   ├── memory_skill.py
│   ├── automation.py
│   ├── system_control.py
│   └── remote_control.py
├── devices/               # 智能家居设备
│   ├── base_device.py     # SmartDevice ABC
│   ├── device_manager.py
│   ├── sim/               # 模拟设备
│   ├── hue/               # Philips Hue
│   └── mqtt/              # MQTT 设备
├── auth/                  # 声纹注册 + 权限
├── memory/                # 对话历史 + 用户偏好
├── realtime_data/         # 新闻/股票数据服务
├── remote/                # WebSocket 远程控制
├── ui/                    # Gradio 仪表盘 + OLED 显示
├── esp32/                 # MicroPython 固件模板
├── deploy/                # Pi 部署脚本
└── tests/                 # pytest 测试集
```

## 配置

所有参数在 `config.yaml` 中统一管理。API key 通过环境变量传入，不硬编码。

关键配置项：
- `devices.mode`: `sim`（模拟）或 `live`（Hue 真实设备）
- `models.groq/deepseek/local`: 意图路由模型配置
- `llm.provider`: `openai` 或 `anthropic`
- `tts.engine`: `edge-tts` 或 `pyttsx3`

## 测试

```bash
python -m pytest tests/ -v
python -m pytest tests/ --cov=core --cov=devices --cov=auth
```

## 路线图

详见 [CLAUDE.md](./CLAUDE.md) 和 Claude Code plan 文件。

当前进度：F1（意图路由）核心完成，优化中。
