# XIAOYUE

A voice-first personal assistant built for Raspberry Pi 5, featuring voiceprint authentication, emotion-aware conversation, persistent memory, smart home control, and multi-model routing with graceful degradation.

> **Status:** 812 tests passing | 14 built-in skills | 5 TTS engines | Runs on RPi5 4GB

## Features

- **Wake Word** — OpenWakeWord for hands-free activation
- **Voiceprint Auth** — SpeechBrain ECAPA-TDNN speaker verification with 4-tier role-based permissions (guest → owner)
- **Speech Recognition** — SenseVoice-Small INT8 via sherpa-onnx (75ms inference, CER 2.96%), Whisper fallback
- **VAD Recording** — Auto-stop on speech end, ~1.5s for short commands
- **Intent Routing** — Groq (~300ms) → Cerebras fallback, LRU-256 cache
- **LLM Backend** — GPT-4o-mini / DeepSeek, streaming sentence-by-sentence with tool-use loop
- **Emotion Pipeline** — SenseVoice detects 7 emotions → LLM adjusts tone → TTS matches vocal style
- **Personality** — Custom persona with time-of-day tone, user mood tracking, dynamic prompt injection
- **TTS** — 5-engine degradation chain: OpenAI TTS → MiniMax → Azure Neural → edge-tts → pyttsx3, with disk cache and emotion-style mapping
- **Memory System** — SQLite + FastEmbed (bge-small-zh-v1.5), 6 memory types, relation index, direct-answer fast path (cosine + multi-signal scoring), episode digests, contradiction detection
- **Smart Home** — Philips Hue live control + simulated devices + MQTT
- **Automation** — Natural language rules: keyword / cron / delay triggers
- **Skills** — 14 built-in (weather, reminders, todos, news, stocks, memory, smart home, system control, etc.) + runtime skill learning via Claude Code CLI
- **Remote Control** — WebSocket bridge to Mac
- **Web UI** — Gradio dashboard with 5 panels

## Architecture

```
Mic → Wake Word → Record (VAD) → [Voiceprint + SenseVoice ASR in parallel]
         → DirectAnswer fast path (cosine > threshold? answer without LLM)
         → [Intent Routing (Groq → Cerebras) + Memory Query] in parallel
         → Local Execution or Cloud LLM (streaming, memory injected ≤500 tokens)
         → TTS dual-thread pipeline (5-engine fallback + emotion style) → Speaker
         → Background: memory extraction + dedup + storage, conversation history, behavior log
```

## Quick Start

```bash
# Install
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Download SenseVoice model (~228MB)
cd data && wget https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2
tar xf sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2
mv sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17 sensevoice-small-int8 && cd ..

# Configure API keys
export GROQ_API_KEY="..."        # Intent routing (free tier)
export OPENAI_API_KEY="..."      # Cloud LLM + TTS
# Optional:
export DEEPSEEK_API_KEY="..."    # DeepSeek LLM
export AZURE_SPEECH_KEY="..."    # Azure TTS
export MINIMAX_API_KEY="..."     # MiniMax TTS

# Run
python jarvis.py --no-wake    # Dev mode (push-to-talk)
python jarvis.py              # Production (wake word activated)
```

## Latency

| Stage | Before | After |
|-------|:------:|:-----:|
| Recording | 3-5s (fixed) | **1-2s** (VAD) |
| ASR | 3-7s (Whisper) | **75ms** (SenseVoice) |
| Routing | 200-1500ms | **200-500ms** (cached) |
| TTS | 0.5-2s (blocking) | **non-blocking** (pipelined) |
| **End-to-end** | **~10-25s** | **~2-3s** |

## Project Structure

```
jarvis/
├── jarvis.py                 # Entry point — initializes all subsystems
├── config.yaml               # Unified config (500+ params)
├── core/                     # Core modules
│   ├── speech_recognizer.py  #   SenseVoice + Whisper dual-engine ASR
│   ├── personality.py        #   Persona system (dynamic prompt)
│   ├── intent_router.py      #   Intent routing with LRU cache + 2-tier fallback
│   ├── local_executor.py     #   Local command dispatch
│   ├── llm.py                #   Multi-LLM backend (streaming + tool-use loop)
│   ├── tts.py                #   5-engine TTS pipeline (dual-thread + disk cache)
│   ├── automation_engine.py  #   Rule engine (keyword/cron/delay)
│   ├── audio_recorder.py     #   Recording with VAD
│   ├── speaker_verifier.py   #   Voiceprint verification
│   ├── health.py             #   Circuit breaker (HEALTHY → DEGRADED → UNAVAILABLE)
│   ├── wake_word.py          #   OpenWakeWord integration
│   ├── learning_router.py    #   Detect user teaching intent
│   ├── skill_factory.py      #   Runtime skill generation via Claude Code CLI
│   ├── event_bus.py          #   Pub/sub event bus
│   └── scheduler.py          #   Cron scheduler
├── skills/                   # 14 built-in + learned/ dynamic skills
├── memory/                   # Memory subsystem
│   ├── manager.py            #   Orchestrator: save → extract → dedup → store → query
│   ├── store.py              #   SQLite (memories / profiles / episodes)
│   ├── retriever.py          #   4-signal scoring (cosine + recency + importance + access)
│   ├── direct_answer.py      #   Fast path: skip LLM for high-confidence factual recall
│   ├── embedder.py           #   FastEmbed bge-small-zh-v1.5
│   ├── behavior_log.py       #   Action logging for behavioral learning
│   └── conversation.py       #   Sliding window conversation history
├── devices/                  # Smart home (sim / hue / mqtt backends)
├── auth/                     # Voiceprint enrollment + role-based permissions
├── realtime_data/            # News & stock data services
├── remote/                   # WebSocket remote control
├── ui/                       # Gradio dashboard + OLED display
├── esp32/                    # MicroPython firmware template
├── deploy/                   # RPi deployment scripts
└── tests/                    # 812 tests
```

## Configuration

All parameters live in `config.yaml`. API keys are passed via environment variables.

| Key | Description | Options |
|-----|-------------|---------|
| `asr.provider` | ASR engine | `sensevoice` / `local` (Whisper) |
| `llm.provider` | LLM backend | `openai` (compatible with DeepSeek) / `anthropic` |
| `llm.base_url` | LLM API endpoint | empty = OpenAI / DeepSeek URL |
| `tts.engine` | TTS engine | `openai_tts` / `minimax` / `azure` / `edge-tts` / `pyttsx3` |
| `devices.mode` | Device mode | `sim` / `live` (Hue) |
| `audio.vad_enabled` | VAD early stop | `true` / `false` |

## Testing

```bash
python -m pytest tests/ -v                    # All tests
python -m pytest tests/ --cov=core            # Coverage report
python -m pytest tests/test_tts.py -v         # Single module
```

## Roadmap

| Feature | Status |
|---------|:------:|
| Real-time data (news/stocks) | Done |
| Intent routing + latency optimization | Done |
| Persona system | Done |
| Natural language automation | Done |
| Memory system (13 optimizations, eval framework) | Done |
| Skill learning (3-tier + code generation) | Done |
| Performance tuning (cache, parallelism, preload) | Done |
| Hue smart home live control | Done |
| Hidden mode (continuous voice) | Done |
| Context awareness (mmWave + sensors) | Waiting for hardware |
| Display (ST7789 / GC9A01) | Waiting for hardware |
| ESP32 sensor node | Waiting for hardware |
| Behavioral learning (T2) | Planned |
| Proactive notifications | Planned |
| Multi-channel (Telegram) | Planned |
| Self-diagnosis & self-repair | Planned |
