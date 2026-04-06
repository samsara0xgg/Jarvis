# 小月 API 总览 — 2026-04-01

## 核心管线（每次语音交互）

| 环节 | 服务 | 用途 | 价格 | 必需 | 环境变量 |
|------|------|------|------|:---:|------|
| **ASR** | SenseVoice (本地, sherpa-onnx) | 语音→文字 | 免费 | 是 | — |
| ASR 回退 | Whisper base (本地) | 离线回退 | 免费 | 否 | — |
| **意图路由** | Groq (llama-3.3-70b) | 分类意图+提取参数 | 免费 tier | 是 | `GROQ_API_KEY` |
| 路由回退 | DeepSeek | Groq 失败时回退 | ~$0.14/M input | 否 | `DEEPSEEK_API_KEY` |
| 路由回退 | Ollama (本地 qwen2.5:7b) | 断网兜底 | 免费 | 否 | — |
| **云端 LLM** | OpenAI (gpt-4o) | 闲聊/复杂问题 | ~$2.5/M in, $10/M out | 仅 complex | `OPENAI_API_KEY` |
| 云端 LLM 备选 | Anthropic (Claude) | 可替代 OpenAI | ~$3/M in, $15/M out | 二选一 | `ANTHROPIC_API_KEY` |
| **TTS** | Edge TTS (微软) | 文字→语音 | 免费 | 是 | — |
| TTS 回退 | pyttsx3 (本地) | 离线回退 | 免费 | 否 | — |

## 唤醒词（仅 always-listening 模式）

| 服务 | 用途 | 价格 | 配置 |
|------|------|------|------|
| Picovoice Porcupine | "Hey Jarvis" 检测 | 免费 tier (3 关键词) | config.yaml `picovoice_access_key` |

## 数据服务（按需）

| 服务 | 用途 | 价格 | 环境变量 |
|------|------|------|------|
| wttr.in | 天气查询 | 免费, 无需 key | — |
| GNews | 新闻聚合 | 免费 100 req/天, 付费 $84/年 | `GNEWS_API_KEY` |
| Yahoo Finance | 股票行情 | 免费 (非官方 API) | — |
| Alpha Vantage | 股票备选 | 免费 25 req/天 | `ALPHA_VANTAGE_API_KEY` |

## 月费估算（50次/天, 10次走云端 LLM）

| 服务 | 月用量 | 月费 |
|------|--------|:---:|
| SenseVoice / Edge TTS / Groq 路由 | 本地或免费 tier | $0 |
| OpenAI GPT-4o | ~300 次, ~150K tokens | ~$1-3 |
| GNews | ~50 次/天 (免费够用) | $0 |
| **总计** | | **~$1-3/月** |

## 云端 LLM 对比

| 方案 | 输入/M tokens | 输出/M tokens | 速度 | 中文质量 |
|------|:---:|:---:|:---:|:---:|
| GPT-4o | $2.50 | $10.00 | 快 | 优秀 |
| GPT-4o-mini | $0.15 | $0.60 | 更快 | 良好 |
| Claude Sonnet | $3.00 | $15.00 | 快 | 优秀 |
| Claude Haiku | $0.25 | $1.25 | 最快 | 良好 |
| DeepSeek V3 | $0.14 | $0.28 | 中 | 优秀(中文最强) |
| Groq (llama-3.3-70b) | 免费 tier | 免费 tier | 极快 | 良好 |

## 省钱策略

- **极致省钱**: complex 意图也走 Groq llama-3.3-70b → $0/月
- **中文最优+便宜**: DeepSeek V3 → ~$0.5/月
- **质量优先**: GPT-4o 或 Claude Sonnet → ~$1-3/月
