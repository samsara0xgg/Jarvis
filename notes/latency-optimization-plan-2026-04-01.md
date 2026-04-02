# 端到端延迟优化计划 — 2026-04-01

## 当前延迟分析

```
简单指令（"打开卧室灯"）当前流程:

录音 3-5s → ASR 3-7s → 路由 0.2-1.5s → 执行 0-0.5s → TTS 0.5-2s (阻塞)
        ↘ 声纹 0.05-0.15s (并行) ↗
                                                总计: ~7-16 秒
```

## 优化后目标

```
录音 1-2s (VAD) → ASR 0.075s (SenseVoice) → 路由 0.3s → 执行 0.1s → TTS (非阻塞)
              ↘ 声纹 0.05-0.15s (并行) ↗
                                                总计: ~2-3 秒
```

## 四步优化（按收益排序）

| 步骤 | 改动 | 收益 | 复杂度 |
|:---:|------|:---:|:---:|
| 1 | SenseVoice INT8 替代本地 Whisper | **省 3-5s** | 低(~60行) |
| 2 | VAD 录音提前终止 | **省 2-3s** | 低(~30行) |
| 3 | 非阻塞 TTS + executor 复用 | 省 0.5-2s | 中(~40行) |
| 4 | LLM 流式输出 + 逐句 TTS | 感知省 2-5s | 中高(~80行) |

## Step 1: SenseVoice via sherpa-onnx

改动文件：
- `config.yaml` — ASR provider 配置
- `core/speech_recognizer.py` — sherpa-onnx SenseVoice 集成 + Whisper 回退
- `tests/test_speech_recognizer.py`

## Step 2: VAD 录音提前终止

改动文件：
- `core/audio_recorder.py` — Silero VAD 或 RMS 静音检测
- `config.yaml` — VAD 参数

注：sherpa-onnx 自带 Silero VAD，可以直接复用。

## Step 3: 非阻塞 TTS

改动文件：
- `jarvis.py` — 持久化 ThreadPoolExecutor + 非阻塞 TTS

## Step 4: LLM 流式 + 逐句 TTS

改动文件：
- `core/llm.py` — chat_stream() 方法
- `jarvis.py` — complex 意图流式播报

## 逐环节最优工具

| 环节 | 当前 | 最优 | 原因 |
|------|------|------|------|
| 唤醒词 | Porcupine | **不变** | 已是最优 |
| 录音 | 固定 3-5s | **Silero VAD** | 说完即停 |
| 声纹 | ECAPA-TDNN | **不变** | 已并行，50-150ms |
| ASR | Whisper base (3-7s) | **SenseVoice INT8** | 75ms + CER 2.96% |
| 路由 | Groq LLM | **不变** | 已是最快 |
| 执行 | Skills | **不变** | 已够快 |
| 云端 LLM | GPT-4o 阻塞 | **流式输出** | 逐句 TTS |
| TTS | edge-tts 阻塞 | **非阻塞播放** | 不卡主循环 |
