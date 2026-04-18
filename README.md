# Yue

**简体中文** · [English](README.en.md)

**一款会越用越懂你的个人语音 AI。**

*累积型记忆、空间感知、可自演化的技能闭环。*

<!-- TODO: 这里加 Pet Mode 演示 GIF -->

## 项目概述

Yue 是一款端到端的语音助手，围绕一个核心命题设计：**助手的价值会复利**。今天主流的语音 AI——Alexa、Siri、ChatGPT——都把每轮对话当作无状态事件。Yue 反其道而行：observer 模块从每段对话里抽取带优先级的观察记录，stable-prefix builder 把它们注入下一轮对话的 prompt 前缀，trace 表记录每次工具调用喂给技能发现循环。它跑得越久，你越不用重复自己。

完全自建，不依赖 LangChain 或任何 agent 框架。约 25 个核心模块、1060 个单元测试，设计目标是在 Mac（开发）和 Raspberry Pi 5（生产）上长期常驻运行，附带一个 Electron 桌宠应用。

## 核心能力

### 全双工打断

TTS 播放期间，一条独立麦克风线程把音频经 Silero VAD 切成 per-utterance 段，每段关闭后异步派发到主对话同款的 SenseVoice ASR。VAD 触发时，自研 PortAudio stream player 在 30ms 内把音量 ramp 到 30%。命中关键词（`{"停", "等一下", "打住", "暂停", "等等", ...}`）后 ring buffer flush，LLM 取消。500ms 预滚捕单字关键词的初始辅音，200ms 后滚捕尾部清擦音。

| 指标 | 数值 |
|---|---|
| speech-to-detect（"停" 中位数） | 1179 ms |
| speech-to-detect（"等一下" 中位数） | 911 ms |
| p95 延迟 | < 1850 ms |
| 30 秒受控静默的误触发 | 0.0 / s |
| 10818 次 callback 中的 audio underflow | 0 |

实际体验：句中说"停"，约 350ms 内音量降下来，再过约 700ms 完全停止——平滑淡出、没有重启 artifact、句间零 gap、首字辅音不被吞掉。长期方向：在小句边界插静默（数据上 ~80% 的自然打断本来就发生在那里）把感知延迟拉近 0；XMOS XVF3800 硬件到位后接空间方向门控，过滤掉电视和家人的声音。

### 累积型记忆

每段对话结束触发一个 observer（LLM function calling，主用 Grok-4.20，fallback Gemini 2.5 Flash）抽取带优先级的文本 bullets，按日期分组存进 SQLite。stable-prefix builder 把相关 bullets 注入下一轮的 system prompt——cache 友好、确定性、read 路径上没有 per-query 向量检索。direct-answer fast path 用多信号加权评分（40% cosine + 25% recency + 20% importance + 15% access frequency）处理高置信度的事实复诵，完全跳过 LLM。

| 模块 | 角色 |
|---|---|
| `observer.py` | 异步抽取，四档优先级（HIGH / MED / LOW / DONE） |
| `stable_prefix.py` | 拼装 personality + observations + 最近 10 轮对话进 LLM 上下文 |
| `trace.py` | 每轮全量分析（path、tool calls、emotion、latency、outcome）喂给技能发现循环 |
| `store.py` | SQLite，6 张表：memories / user_profiles / episodes / episode_digests / memory_relations / observations |
| `direct_answer.py` | 多信号 LLM-bypass，专门给重复查询走 |

典型的 observation 日志：

```
Date: 2026-04-17
* [HIGH] (14:30) User prefers warm yellow (2700K) in living room
* [MED]  (15:12) User mentioned weekend trip to Vancouver to see friends
* [DONE] (15:45) Reminder set for coffee machine descaling
```

8 个抽取模型在 20 条中文家庭场景 fixture 上做了基准测试（smart-home、preference、state-change、temporal、emotion、correction、multi-entity、completion）。Grok 4.20 性价比胜出：F1 0.88，p95 4.8s，每 100 turn 0.031 美元；Gemini 2.5 Flash 零幻觉但成本翻倍，留作 fallback。实际体验上：今天讲过的事情下周再提，已经在 prompt 上下文里——没有"我无法访问之前的对话"这堵墙，也不用手动复述。

### 自演化技能闭环

技能通过统一的 `tool_registry` 注册，分两种格式：需要代码的走 Python `@jarvis_tool` 装饰器（目前 11 个 live 函数，分布在 `tools/reminders.py`、`tools/smart_home.py`、`tools/time_utils.py`、`tools/todos.py`），HTTP wrapper 类的走 YAML 声明式 spec（`skills/weather.yaml` + 自动迁移过来的 `skills/learned/exchange_rate.yaml`）。两种格式对 LLM 暴露的都是相同的 OpenAI 兼容 function-calling schema。每个工具的 annotation（`read_only`、`destructive`、`idempotent`、`required_role`）通过四级 RBAC 过滤：guest < family < trusted < owner。

```yaml
name: get_weather
parameters:
  - {name: city, type: string, default: Victoria}
action:
  type: http_get
  url: "https://wttr.in/{{ city }}?format=j1"
  retry: {max: 3, delay_ms: 1000, backoff: exponential}
response:
  template: "{{ city }} weather: {{ desc }}, {{ temp_c }}C..."
security:
  allowed_domains: [wttr.in]
```

YAML action 在 Jinja2 sandbox 里执行，每个技能强制 domain 白名单 + RFC1918 私网拦截防 SSRF。Discovery loop 复用已经在跑的 trace 表：nightly batch 检测热点 intent（频率 + 重要性 + 用户修正信号），从 3-5 个代表 trace 样本起草 YAML 候选，跑 7 天 shadow 期用三层判定（结构匹配 / embedding 相似度 / LLM-as-judge）评估输出对齐，再进 canary 监控自动 rollback。静态层已上线，discovery pipeline 增量接到同一个 registry——新技能不需要重启助手就出现。

### 多层 LLM 容错

xAI Grok-4.1-fast 负责主响应生成；Grok 降级时 Gemini 接 streaming fallback。意图路由跑在 Groq Llama-3.3-70B 上（约 300ms，LRU-256 cache），Cerebras Llama 3.1-8B 作为路由 backup。所有外部调用都包在熔断器里（HEALTHY → DEGRADED → UNAVAILABLE 三态切换），按观察到的失败率确定性地穿透 fallback。

### 声纹认证

SpeechBrain ECAPA-TDNN 支撑四级权限模型：guest → family → trusted → owner。记忆查询按说话人身份隔离；设备控制和敏感技能要求 trusted 及以上。不同用户看到不同记忆、能解锁不同操作。

## 架构

```
   Mic ─→ Wake Word ─→ Record (VAD-gated)
                          │
                          ↓
            [Voiceprint  ║  SenseVoice ASR]    parallel
                          │
                          ↓
            DirectAnswer  (高置信度记忆召回，跳过 LLM)
                          │
                          ↓
            [Intent route  ║  Memory query]    parallel
                          │
                          ↓
            Local executor   OR   Cloud LLM (streaming + tool-use loop)
                          │
                          ↓
            TTS pipeline (MiniMax → edge-tts → pyttsx3)
                          │
                          ↓
            AudioStreamPlayer (sample-accurate 增益 ducking)
                          │
                          ↓
                       Speaker

   Background:    Observer 抽取记忆 → SQLite + FastEmbed
                  Trace 表喂技能发现循环
                  (Reflector 去重 / 矛盾解决待实装)

   During TTS:    Mic → VAD 段切片 → 共享 SenseVoice 路径
                       → 关键词命中 → 软停（30ms ramp）或硬停
```

## 硬件路线图——空间智能

下一代硬件用 [XMOS XVF3800](https://www.xmos.com/xvf3800/) reference board 替换现有的 USB 麦克风。这颗芯片在硬件层面提供声源方向（DOA）、波束成形、距离估计、混响指纹——把 Yue 从一个音频设备升级为空间感知 agent。

具体能力：

- **房间感知控制。** 说"开灯"不用指明哪个房间——DOA + 声学指纹自动识别空间。
- **区域人格切换。** 按位置（书桌 / 沙发 / 卧室 / 厨房）切换语调、唤醒词策略和 TTS 音量。
- **距离自适应 TTS。** 0.5 米耳语，3 米洪亮，自动调节。
- **跟随模式。** 无唤醒词连续对话，靠方向 + 声纹双重过滤压制电视和他人误触发。
- **家人声纹图谱。** 被动 presence map（谁在家、在哪、什么时间）支持主动化日常。
- **跨房间设备接力。** 多设备时对话跟着人在房间间流转。

完整设计分析见 [`notes/hardware-xvf3800-fulltest-2026-04-16.md`](notes/hardware-xvf3800-fulltest-2026-04-16.md)。硬件运输中。

## 技术栈

| 层 | 栈 |
|-------|-------|
| Wake word | openwakeword (`hey_jarvis_v0.1`) |
| ASR | SenseVoice-Small INT8（sherpa-onnx） · Whisper fallback |
| Voiceprint | SpeechBrain ECAPA-TDNN |
| VAD | Silero VAD (ONNX)，按 `headphones` / `speakers` 模式切阈值 |
| 意图路由 | Groq Llama-3.3-70B · Cerebras Llama 3.1-8B（备用） |
| LLM | xAI Grok-4.1-fast（主） · Gemini（fallback） · Anthropic Claude（技能生成） |
| Memory | 结构化 observation 流 + SQLite · function-calling 抽取（Grok 4.20 / Gemini 2.5 Flash） · stable-prefix 注入 |
| TTS | MiniMax → edge-tts → pyttsx3（三引擎降级链） |
| 音频 I/O | sounddevice + 自研 `AudioStreamPlayer`（PortAudio callback + ring buffer） |
| 设备 | Philips Hue（live） · MQTT · 内存模拟 |
| Desktop | Electron Pet Mode + Cmd+Space 命令面板 |
| 空间感知（下一代） | XMOS XVF3800 |

## 快速上手

```bash
git clone https://github.com/samsara0xgg/Jarvis.git && cd Jarvis
uv pip install -r requirements.txt

# 下载 SenseVoice INT8 模型（约 228MB）
cd data
wget https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2
tar xf sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2
mv sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17 sensevoice-small-int8
cd ..

# 必需的环境变量（config.yaml 不存任何 secret）
export XAI_API_KEY=...     # 主云端 LLM
export GROQ_API_KEY=...    # 意图路由

python jarvis.py --no-wake     # 开发模式：按回车开始录音
python jarvis.py               # 生产模式：唤醒词 "Hey Jarvis"
```

可选的 fallback 引擎 key：`GEMINI_API_KEY`（LLM fallback）、`CEREBRAS_API_KEY`（路由备用）、`MINIMAX_API_KEY`（主 TTS）。

启动桌宠：

```bash
python -m ui.web.server         # 终端 1 — 后端
cd desktop && npm start          # 终端 2 — Electron
```

## 项目结构

```
yue/
├── jarvis.py                   # 入口——初始化所有子系统
├── config.yaml                 # 统一配置（无 secret——只走环境变量）
├── core/                       # 25 个模块——voice、ASR、LLM、TTS、interrupt、VAD
├── memory/                     # 11 个模块——observer、stable_prefix、trace、store、retriever、direct_answer
├── auth/                       # 声纹注册 + 四级权限
├── devices/                    # 智能家居后端（Hue / MQTT / sim）
├── desktop/                    # Electron Pet Mode + Cmd+Space 命令面板
├── ui/                         # Live2D web server + OLED display
├── skills/                     # YAML 技能 + learned/ 运行时生成
├── tools/                      # 内置工具模块（reminders、smart-home 等）
├── realtime_data/              # 新闻 / 股票数据服务
├── system_tests/               # 端到端测试 runner（交互 + Claude Code 模式）
├── tests/                      # 1060 个单元测试
├── deploy/                     # Raspberry Pi systemd + 安装脚本
├── esp32/                      # MicroPython 固件（传感器 + 继电器节点）
├── notes/                      # 研究、计划、session 日志
└── docs/                       # 设计 spec + git 工作流
```

## 文档

| 主题 | 文件 |
|-------|------|
| Git 工作流 + commit 规范 | [`docs/git-guide.md`](docs/git-guide.md) |
| 语音管线优化计划 | [`notes/plans/voice-pipeline-optimization-2026-04-16.md`](notes/plans/voice-pipeline-optimization-2026-04-16.md) |
| 打断 ASR 迁移设计 | [`notes/interrupt-asr-migration-2026-04-17.md`](notes/interrupt-asr-migration-2026-04-17.md) |
| XVF3800 空间智能调研 | [`notes/hardware-xvf3800-fulltest-2026-04-16.md`](notes/hardware-xvf3800-fulltest-2026-04-16.md) |
| Open-LLM-VTuber 架构分析 | [`notes/olv-deep-dive-2026-04-16.md`](notes/olv-deep-dive-2026-04-16.md) |
| AudioStreamPlayer + bench 设计 | [`notes/self-player-and-bench-2026-04-17.md`](notes/self-player-and-bench-2026-04-17.md) |

## 测试

```bash
python -m pytest tests/ -q                     # 单元测试（1060）
python system_tests/runner.py --mode cc        # 端到端（Claude Code 模式）
python system_tests/runner.py                  # 端到端（交互模式）
```

## License

MIT — 见 [`LICENSE`](LICENSE)。
