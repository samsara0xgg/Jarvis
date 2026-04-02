# 小贾 (J.A.R.V.I.S.) — 私人语音管家

声纹驱动的中文语音助手，支持智能家居控制、实时信息查询、情感感知对话和多层模型路由。

## 功能

- **唤醒词** — Porcupine "Hey Jarvis"，免提激活
- **声纹验证** — SpeechBrain ECAPA-TDNN，5 级权限（guest → owner）
- **语音识别** — SenseVoice-Small INT8（75ms 推理，CER 2.96%），Whisper 离线备用
- **VAD 录音** — 语音结束自动停止，短指令 ~1.5s
- **意图路由** — Groq（~300ms）→ DeepSeek → 本地 Ollama 三级回退
- **LLM 对话** — GPT-4o-mini / DeepSeek / Kimi K2.5，流式逐句播报
- **情感感知** — SenseVoice 检测 7 种情绪 → LLM 调整语气 → TTS 匹配风格
- **人格系统** — "小贾"人设，时段语气 + 用户情绪 + 动态 prompt
- **TTS 语音合成** — 多引擎：OpenAI TTS / MiniMax / Azure Neural / Edge TTS / pyttsx3
- **智能家居** — Philips Hue + 模拟设备 + MQTT
- **自动化规则** — 自然语言创建：keyword / cron / 延时触发
- **技能系统** — 天气、提醒、待办、新闻、股票、记忆、系统控制、远程控制
- **远程控制** — WebSocket 控制 Mac

## 架构

```
麦克风 → 唤醒词 → 录音(VAD) → [声纹验证 + SenseVoice ASR 并行]
                                          ↓
                              意图路由 (Groq/DeepSeek/Ollama)
                                          ↓
                  ┌───────────┬───────────┼───────────┬──────────┐
                  ↓           ↓           ↓           ↓          ↓
             smart_home   info_query    time    automation    complex
             本地执行      技能调用    本地生成   规则管理    云端 LLM (流式)
                  └───────────┴───────────┼───────────┴──────────┘
                                          ↓
                              TTS (情感风格) → 喇叭
```

## 快速开始

```bash
# 安装
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 下载 SenseVoice 模型 (~228MB)
cd data && wget https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2
tar xf sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2
mv sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17 sensevoice-small-int8 && cd ..

# 配置 API keys
export GROQ_API_KEY="..."        # 意图路由（免费）
export OPENAI_API_KEY="..."      # 云端 LLM + TTS
# 可选:
export DEEPSEEK_API_KEY="..."    # DeepSeek LLM
export AZURE_SPEECH_KEY="..."    # Azure TTS
export MINIMAX_API_KEY="..."     # MiniMax TTS

# 运行
python jarvis.py --no-wake    # 开发模式（按键录音）
python jarvis.py              # 生产模式（唤醒词 "Hey Jarvis"）
```

## 延迟性能

| 环节 | 之前 | 现在 |
|------|:---:|:---:|
| 录音 | 3-5s (固定) | **1-2s** (VAD) |
| ASR | 3-7s (Whisper) | **75ms** (SenseVoice) |
| 路由 | 200-1500ms | 200-500ms |
| TTS | 0.5-2s (阻塞) | **非阻塞** |
| **总计** | **~10-25s** | **~2-3s** |

## 项目结构

```
Jarvis/
├── jarvis.py                # 主入口
├── config.yaml              # 统一配置
├── core/                    # 核心模块
│   ├── speech_recognizer.py # SenseVoice + Whisper 双引擎 ASR
│   ├── personality.py       # "小贾" 人格系统（动态 prompt）
│   ├── intent_router.py     # 三层意图路由
│   ├── local_executor.py    # 本地指令执行
│   ├── llm.py               # 多 LLM 后端 (OpenAI/Anthropic/DeepSeek/Kimi)
│   ├── tts.py               # 多 TTS 引擎 (OpenAI/MiniMax/Azure/Edge/pyttsx3)
│   ├── automation_rules.py  # 自动化规则引擎
│   ├── audio_recorder.py    # 录音 + VAD
│   ├── speaker_verifier.py  # 声纹验证
│   ├── event_bus.py         # 事件总线
│   └── scheduler.py         # 定时任务
├── skills/                  # LLM function calling 技能 (10+)
├── devices/                 # 智能家居 (sim / hue / mqtt)
├── auth/                    # 声纹注册 + 角色权限
├── memory/                  # 对话历史 + 用户偏好
├── realtime_data/           # 新闻/股票数据服务
├── notes/                   # 调研笔记和工作记录
├── tests/                   # 490 tests, 82% coverage
├── remote/                  # WebSocket 远程控制
├── ui/                      # Gradio 仪表盘 + OLED
├── esp32/                   # MicroPython 固件模板
└── deploy/                  # RPi 部署脚本
```

## 配置

所有参数在 `config.yaml` 统一管理，API key 通过环境变量传入。

| 配置项 | 说明 | 可选值 |
|--------|------|--------|
| `asr.provider` | ASR 引擎 | `sensevoice` / `local` (Whisper) |
| `llm.provider` | LLM 后端 | `openai` (兼容 DeepSeek/Kimi) / `anthropic` |
| `llm.base_url` | LLM API 地址 | 留空=OpenAI / DeepSeek / Moonshot URL |
| `tts.engine` | TTS 引擎 | `openai_tts` / `minimax` / `azure` / `edge-tts` / `pyttsx3` |
| `devices.mode` | 设备模式 | `sim` / `live` (Hue) |
| `audio.vad_enabled` | VAD 提前终止 | `true` / `false` |

## 测试

```bash
python -m pytest tests/ -v                    # 全部测试
python -m pytest tests/ --cov=core            # 覆盖率报告
python -m pytest tests/test_tts.py -v         # 单模块
```

## 路线图

| # | 功能 | 状态 |
|:---:|------|:---:|
| F0 | 新闻/股票数据 | ✅ |
| F1 | 意图路由 + 延迟优化 | ✅ |
| F2 | 人格系统 ("小贾") | ✅ |
| F3 | 情境感知 (ESP32) | ⏸️ 等硬件 |
| F4 | 主动通知 | ⏸️ 等 F3 |
| F5 | Telegram 多渠道 | 🔲 |
| F6 | 自然语言自动化 | ✅ |
| F7 | OLED Ambient | ⏸️ 等硬件 |
| F8 | 自诊断自修复 | 🔲 |
| F9-F17 | 行为学习/渐进信任/自编程/... | 🔲 |

详见 `notes/` 目录下的调研笔记和工作记录。
