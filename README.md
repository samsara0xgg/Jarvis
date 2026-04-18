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

TTS 播放期间有一条独立的 VAD 门控音频通路。说一句"停"或"等等"能在大约 700ms 内中断当前发声，走的是与主对话同一个 SenseVoice ASR 模型——没有第二个模型，也没有流式 partial-results 的特殊处理。早期方案用过 streaming Zipformer 转录器，benchmark 显示它无法稳定 commit 中文单字关键词，所以架构最终统一到一个 ASR 栈。软停采用 PortAudio callback 内的 sample-accurate 增益 ducking，根治了 SIGSTOP 路径在 macOS 上引发的 CoreAudio underrun loop-tail 假象。

### 累积型记忆

记忆子系统是这个项目最大的赌注：不依赖 LLM 的 stateful context window，也不走 per-query 向量检索，而是把观察记录捕获成结构化文本 bullets，整段塞进每轮对话的 system prompt——靠 LLM prompt cache 拿到 sub-cent 级别的读取成本，让模型自己跨时间排序。

**模块构成。** 11 个模块，按 write/read 分工：

| 路径 | 模块 |
|------|------|
| 写入（cold path） | `observer.py` 通过 LLM function calling 从每个完成的 turn 抽取结构化 bullets；`trace.py` 记录每轮的全量分析数据（path、tool calls、emotion、latency、outcome signal） |
| 读取（hot path） | `stable_prefix.py` 把 personality + observations + 最近 10 轮对话 + 当前输入拼成 cache 友好的 prompt 前缀；`direct_answer.py` 跳过 LLM 直接回答高置信度的事实查询 |
| 存储 | `store.py` 管理 6 张 SQLite 表：`memories`、`user_profiles`、`episodes`、`episode_digests`、`memory_relations`、`observations` |
| 排序 | `retriever.py` 多信号加权评分：40% cosine + 25% recency + 20% importance + 15% access frequency，新用户走 cold-start 权重 |
| 编排 | `manager.py` 暴露公开 API：`query`、`save`、`build_stable_prefix`、`write_observation`、`maintain` |

**Observation 格式。** 每个完成的 turn 产出零到多条 bullet：

```
Date: 2026-04-17
* [HIGH] (14:30) User prefers warm yellow (2700K) in living room
* [MED]  (15:12) User mentioned weekend trip to Vancouver to see friends
* [DONE] (15:45) Reminder set for coffee machine descaling
```

四档优先级——`HIGH`（用户明确事实、未完成目标）、`MED`（学习到的上下文、工具结果）、`LOW`（不确定）、`DONE`（已完成任务）——决定下一轮 prefix 里浮上来什么。整段 bullet 流直接塞进 prompt，跨时序推理交给 LLM 原生处理，read 路径上没有单独的检索模块。

**抽取模型。** 主路 xAI Grok-4.20-0309（non-reasoning），选它是因为成本低、p95 延迟稳；fallback Gemini 2.5 Flash，选它是因为基准测试里 0% 幻觉率。每个模型独立的 `base_url` 和 `api_key_env`——之前一个 bug 把所有 Gemini fallback 调用都路由到 xAI 端点，悄无声息地遮掉了 13 天的故障。

**Benchmark 实验（2026 年 4 月）。** 8 个抽取模型在 20 条中文家庭场景 fixture 上对比，覆盖 smart-home、preference、state-change、temporal、emotion、correction、multi-entity、completion 8 类模式。指标：幻觉感知 F1= matched / (matched + 幻觉额外项)，加优先级准确率和 p50/p95 延迟。Grok 4.20 在性价比上胜出（每 100 turn 0.031 美元，p95 4.8s，F1 0.88）；DeepSeek 因 p95 9.4s 被淘汰；Gemini 2.5 Flash 0% 幻觉但成本翻倍，留作 fallback。总开销 5.20 美元跑了 160 次调用。

并行做了 LOCOMO 风格的对比研究，排除了几个备选：Mem0 比 full-context 准确率掉了 6 个百分点（66.9% vs 72.9%）；Zep 准确率到 76.6% 但单次查询要 600k token——对 sub-2-second 的语音回路完全不可接受。直接注入方案是 Mac 和 Raspberry Pi 上延迟预算的最优解。

**当前状态。** 存储、抽取、注入三层已上线；FastEmbed `bge-small-zh-v1.5` 向量管线作为 `direct_answer` bypass 保留（cosine > 0.5 闸门 + 多信号评分），同时结构化 observation 流逐步接管主 read 路径。老的 `behavior_log.py` 模块逐个被 `trace.py` 替换中。

### 自演化技能闭环

技能位于"固定工具"和"学习行为"之间。Yue 走混合路线：少量手写的 Python 函数处理需要代码的事，YAML 声明式格式覆盖大多数 API wrapper，再加一条规划中的 discovery pipeline——从 trace 表里挖掘新技能候选。

**两层注册。** `core/tool_registry.py`（195 行）把两种格式统一到同一个 dispatch table：

| 层 | 定义方式 | 当前在线 |
|------|----------|----------|
| Python | `@jarvis_tool` 装饰器作用在 `tools/` 下的函数 | 11 个函数，分散在 `reminders.py`、`smart_home.py`、`time_utils.py`、`todos.py` |
| YAML | 声明式 spec，放在 `skills/` 或 `skills/learned/` | `skills/weather.yaml`（生产）、`skills/learned/exchange_rate.yaml`（迁移自学习目录） |

每个工具携带一组 annotation（`read_only`、`destructive`、`idempotent`、`required_role`），并通过四级 RBAC（`guest` < `member`/`resident` < `family`/`admin` < `owner`）过滤后才会暴露给 LLM 作为 function-calling schema。

**YAML 技能 schema。** 生产示例（`skills/weather.yaml`）：

```yaml
name: get_weather
description: "Get current weather for a city."
version: 1
status: live
parameters:
  - {name: city, type: string, required: false, default: Victoria}
annotations: {read_only: true, idempotent: true}
action:
  type: http_get
  url: "https://wttr.in/{{ city }}?format=j1"
  timeout_ms: 10000
  retry: {max: 3, delay_ms: 1000, backoff: exponential}
response:
  extract:
    temp_c: "{{ result.current_condition[0].temp_C }}"
    desc:   "{{ result.current_condition[0].lang_zh[0].value }}"
  template: "{{ city }} weather: {{ desc }}, {{ temp_c }}C..."
  error_template: "Weather query failed."
security:
  allowed_domains: [wttr.in]
```

解释器（`core/yaml_interpreter.py`，249 行）在 Jinja2 `ImmutableSandboxedEnvironment` 里执行 action，强制每个技能的 `allowed_domains` 白名单加上硬编码的私网 IP 段拦截（RFC1918 + loopback），防 SSRF。`to_tool_definition()` 方法生成 OpenAI 兼容的 function-calling schema——LLM 调用时 YAML 技能和 Python 工具完全无差别。

**为什么大多数技能用 YAML。** 设计上的赌注是：大约一半的有用技能本质上是 HTTP wrapper + JSON 整形——这一类用 Python 写只会增加 bug 面积，没有表达力收益。强制走规范化的声明式格式（单一 `http_get` 关键字、命名抽取、明确的 retry 语义）能消除 LLM 自由选择 Python 写法时引入的失败模式。多步任务上的对比研究显示，约束 DSL 的准确率明显高于开放式 Python 生成，所以新自动生成的技能默认走 YAML。

**Discovery pipeline（已设计，未实装）。** trace 表让闭环可行：每轮对话都记录 `path_taken`、`tool_calls`（JSON）、`outcome_signal`、`latency_ms` 和检测到的 emotion。规划中的 nightly batch 会：

1. 聚类过去 24 小时的 `(intent, skill_match, success, user_correction)` 元组。
2. 通过混合信号检测热点——频率（≥3 次出现且 embedding cosine > 0.85）、重要性加权（语调强调、重复请求）、失败驱动触发（用户对现有技能做出修正）。
3. 编译候选：LLM（Grok 4.20 生成 spec，Claude 留给少见的 novel-Python 场景）从 3-5 个代表性 trace 样本生成 YAML 技能。
4. 三道验证 gate：schema 正确性、对原 trace 样本回放（≥80% 命中）、与现有库的 embedding 相似度（< 0.9 防重复）。

**Shadow + canary 上线流程。** 候选技能先经过 7 天 shadow 期，与现有路径并行执行，输出记录但不返回给用户。输出相似度走三层判断——结构匹配（免费，name 和参数严格匹配）、自然语言输出的 embedding 相似度（免费，约 10ms）、灰区交给 LLM-as-judge（约每轮 0.003 美元，约 2 秒）——总体对齐率 ≥85% 才能 promote，安全敏感类需 ≥95%。Promote 后 48 小时 canary 监控，若预期触发率下降 > 20% 自动 rollback。技能执行失败 fall through 到原始 LLM dispatch，不会让用户看到硬错误。

**当前状态。** 静态层已完整上线：11 个 Python 工具、2 个 YAML 技能（生产 + 学习）、统一注册表、RBAC、沙箱执行。Discovery loop 设计完成但还未接通——trace 表已就位，但 nightly batch、热点检测、编译 prompt、shadow 框架都待实现。学习候选队列里目前躺着一个 `fifa_tickets`（status `pending_review`）等手动审核。

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
