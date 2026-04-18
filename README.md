# Yue

**A personal voice AI that grows with you.**

*Compounding memory, room-aware perception, and a self-improving skill loop.*

<!-- TODO: add Pet Mode demo GIF here -->

## Overview

Yue is an end-to-end voice assistant designed around a single thesis: assistant utility compounds. Most voice AIs today — Alexa, Siri, ChatGPT — treat each interaction as stateless. Yue inverts this: an observer extracts priority-tagged observations from each conversation, a stable-prefix builder injects them into the next session's prompt context, a trace table records every tool call for the skill-discovery loop. The longer it runs, the less you have to repeat yourself.

Built end-to-end without LangChain or any agent framework. ~25 core modules, 1060 unit tests, designed to run continuously on Mac (development) and Raspberry Pi 5 (production), with an Electron desktop pet on the side.

## Capabilities

### Full-duplex interrupt

A separate VAD-gated audio path runs during TTS playback. Mid-sentence "停" or "等等" stops speech via the same SenseVoice ASR model used for the main loop — no second model, no streaming partial-results gymnastics. An earlier design used a streaming Zipformer transducer; benchmarking showed it could not commit single-character Chinese keywords reliably, so the architecture was unified around one ASR stack. Soft-stop applies sample-accurate gain ducking inside a PortAudio callback, eliminating the macOS CoreAudio underrun artifacts that SIGSTOP-based pause introduces.

### Compounding memory

The memory subsystem is the project's main bet: instead of stateful LLM context windows or vector retrieval per query, observations are captured as structured text bullets and injected wholesale into every conversation's system prompt — leveraging LLM prompt caching for sub-cent reads and letting the model do its own ranking across temporal context.

**Components.** Eleven modules organized as a write/read split:

| Path | Modules |
|------|---------|
| Write (cold) | `observer.py` extracts structured bullets from each completed turn via LLM function calling; `trace.py` records per-turn analytics (path, tool calls, emotions, latency, outcome signal) |
| Read (hot) | `stable_prefix.py` assembles personality + observations + last ten turns + current input into a cache-friendly prompt prefix; `direct_answer.py` skips the LLM entirely for high-confidence factual recall |
| Storage | `store.py` manages six SQLite tables: `memories`, `user_profiles`, `episodes`, `episode_digests`, `memory_relations`, `observations` |
| Ranking | `retriever.py` uses a multi-signal weighted score: 40% cosine + 25% recency + 20% importance + 15% access frequency, with cold-start weights for new users |
| Orchestration | `manager.py` exposes the public API: `query`, `save`, `build_stable_prefix`, `write_observation`, `maintain` |

**Observation format.** Each completed turn produces zero or more bullets:

```
Date: 2026-04-17
* [HIGH] (14:30) User prefers warm yellow (2700K) in living room
* [MED]  (15:12) User mentioned weekend trip to Vancouver to see friends
* [DONE] (15:45) Reminder set for coffee machine descaling
```

Four priority tiers — `HIGH` (explicit user facts, unresolved goals), `MED` (learned context, tool results), `LOW` (uncertain), `DONE` (completed tasks) — determine what surfaces in the next conversation's prefix. The full bullet stream goes in; the LLM handles cross-temporal reasoning natively, no separate retrieval module on the read path.

**Extraction model.** Primary is xAI Grok-4.20-0309 (non-reasoning) for cost and p95 latency stability; fallback is Gemini 2.5 Flash for its observed zero-hallucination rate. Each provider has its own `base_url` and `api_key_env` — a prior bug routed all Gemini fallback calls to xAI's endpoint and silently masked an outage for thirteen days.

**Benchmark experiments (April 2026).** Eight extraction models compared across twenty Chinese home-dialogue fixtures spanning smart-home, preference, state-change, temporal, emotion, correction, multi-entity, and completion patterns. Metric: hallucination-aware F1 = matched / (matched + hallucinated extras), plus priority accuracy and p50/p95 latency. Grok 4.20 won on price/performance ($0.031 per 100 turns, p95 4.8s, F1 0.88); DeepSeek hit p95 9.4s and was dropped; Gemini 2.5 Flash held zero hallucination at twice the cost and was kept as fallback. Total spend: $5.20 for 160 calls.

A parallel LOCOMO-style comparison ruled out alternatives: Mem0 lost six accuracy points versus full-context (66.9% vs 72.9%); Zep reached 76.6% but at 600k tokens per query — prohibitive for sub-2-second voice loops. The direct-injection approach was chosen as the best fit for the latency budget on Mac and Raspberry Pi.

**Status.** Storage, extraction, and injection are live. The FastEmbed `bge-small-zh-v1.5` vector pipeline remains for the `direct_answer` bypass (cosine > 0.5 gates the multi-signal score) while the structured observation stream takes over the main read path. The legacy `behavior_log.py` is being replaced module-by-module with `trace.py`.

### Self-improving skill loop

Skills are defined in YAML for simple cases (`skills/weather.yaml`), backed by Python tool modules for the more complex ones (`tools/reminders.py`, `tools/smart_home.py`, etc.), and generated at runtime via Claude Code CLI for the missing ones. A behavior log surfaces patterns from conversation history and proposes new skills before they're explicitly requested. The `skills/learned/` directory is where these emerge.

### Multi-tier LLM resilience

xAI Grok-4.1-fast handles main response generation; Gemini is the streaming fallback when Grok degrades. Intent routing runs on Groq Llama-3.3-70B (~300ms, LRU-256 cache), with Cerebras Llama 3.1-8B as the routing backup. Every external call sits behind a circuit breaker (HEALTHY → DEGRADED → UNAVAILABLE) and falls through deterministically.

### Voiceprint authentication

SpeechBrain ECAPA-TDNN backs a four-tier permission model: guest → family → trusted → owner. Memory queries are scoped to the speaker's identity; device control and sensitive skills require trusted-or-above. Different users see different memories and unlock different actions.

## Architecture

```
   Mic ─→ Wake Word ─→ Record (VAD-gated)
                          │
                          ↓
            [Voiceprint  ║  SenseVoice ASR]    parallel
                          │
                          ↓
            DirectAnswer  (high-confidence recall, skips LLM)
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
            AudioStreamPlayer (sample-accurate gain ducking)
                          │
                          ↓
                       Speaker

   Background:    Observer extracts memories → SQLite + FastEmbed
                  Reflector dedupes and resolves contradictions
                  Behavior log feeds skill self-discovery

   During TTS:    Mic → VAD-gated segments → shared SenseVoice path
                       → keyword match → soft duck (30ms) or hard stop
```

## Hardware roadmap — spatial intelligence

The next iteration replaces the off-the-shelf USB microphone with an [XMOS XVF3800](https://www.xmos.com/xvf3800/) reference board. The chip provides direction-of-arrival, beamforming, distance estimation, and reverberation fingerprinting in hardware — turning Yue from an audio device into a spatial agent.

Concretely, this enables:

- **Room-aware control.** "Open the lights" without specifying which room — direction-of-arrival + acoustic fingerprint identify the space.
- **Zone-based personas.** Different tone, wake-word policy, and TTS volume by location (desk / sofa / bedroom / kitchen).
- **Distance-adaptive TTS.** Whisper at 0.5m, project at 3m, automatic.
- **Follow mode.** No-wake-word continuous conversation, gated by direction + voiceprint to suppress false triggers from TV or other people.
- **Family voiceprint atlas.** Passive presence map (who's home, where, when) for proactive routines and habit learning.
- **Cross-room handoff.** With multiple devices, the conversation follows you between rooms.

Full design analysis in [`notes/hardware-xvf3800-fulltest-2026-04-16.md`](notes/hardware-xvf3800-fulltest-2026-04-16.md). Hardware in transit.

## Tech stack

| Layer | Stack |
|-------|-------|
| Wake word | openwakeword (`hey_jarvis_v0.1`) |
| ASR | SenseVoice-Small INT8 via sherpa-onnx · Whisper fallback |
| Voiceprint | SpeechBrain ECAPA-TDNN |
| VAD | Silero VAD (ONNX), `headphones` / `speakers` mode-based thresholds |
| Intent router | Groq Llama-3.3-70B · Cerebras Llama 3.1-8B (backup) |
| LLM | xAI Grok-4.1-fast (primary) · Gemini (fallback) · Anthropic Claude (skill generation) |
| Memory | Structured observation stream on SQLite · function-calling extraction (Grok 4.20 / Gemini 2.5 Flash) · stable-prefix injection |
| TTS | MiniMax → edge-tts → pyttsx3 (3-engine fallback) |
| Audio I/O | sounddevice + custom `AudioStreamPlayer` (PortAudio callback + ring buffer) |
| Devices | Philips Hue (live) · MQTT · in-memory sim |
| Desktop | Electron Pet Mode + Cmd+Space command panel |
| Spatial (next) | XMOS XVF3800 |

## Getting started

```bash
git clone https://github.com/samsara0xgg/Jarvis.git && cd Jarvis
uv pip install -r requirements.txt

# SenseVoice INT8 model (~228MB)
cd data
wget https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2
tar xf sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2
mv sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17 sensevoice-small-int8
cd ..

# Minimum required env vars (config.yaml holds no secrets)
export XAI_API_KEY=...     # Primary cloud LLM
export GROQ_API_KEY=...    # Intent routing

python jarvis.py --no-wake     # Development: press Enter to talk
python jarvis.py               # Production: wake word "Hey Jarvis"
```

Optional secondary keys for fallback engines: `GEMINI_API_KEY` (LLM fallback), `CEREBRAS_API_KEY` (router backup), `MINIMAX_API_KEY` (primary TTS).

For the desktop pet:

```bash
python -m ui.web.server         # Terminal 1 — backend
cd desktop && npm start          # Terminal 2 — Electron
```

## Project structure

```
yue/
├── jarvis.py                   # Entry point — initializes all subsystems
├── config.yaml                 # Unified config (no secrets — env vars only)
├── core/                       # 25 modules — voice, ASR, LLM, TTS, interrupt, VAD
├── memory/                     # 11 modules — observer, stable_prefix, trace, store, retriever, direct_answer
├── auth/                       # Voiceprint enrollment + 4-tier permission model
├── devices/                    # Smart home backends (Hue / MQTT / sim)
├── desktop/                    # Electron Pet Mode + Cmd+Space command panel
├── ui/                         # Live2D web server + OLED display
├── skills/                     # YAML skills + learned/ runtime-generated skills
├── tools/                      # Built-in tool modules (reminders, smart-home, etc.)
├── realtime_data/              # News / stock data services
├── system_tests/               # End-to-end runner (interactive + Claude Code mode)
├── tests/                      # 1060 unit tests
├── deploy/                     # Raspberry Pi systemd + install scripts
├── esp32/                      # MicroPython firmware (sensor + relay nodes)
├── notes/                      # Research, plans, session logs
└── docs/                       # Design specs + git workflow
```

## Documentation

| Topic | File |
|-------|------|
| Git workflow + commit conventions | [`docs/git-guide.md`](docs/git-guide.md) |
| Voice pipeline optimization plan | [`notes/plans/voice-pipeline-optimization-2026-04-16.md`](notes/plans/voice-pipeline-optimization-2026-04-16.md) |
| Interrupt ASR migration design | [`notes/interrupt-asr-migration-2026-04-17.md`](notes/interrupt-asr-migration-2026-04-17.md) |
| XVF3800 spatial intelligence research | [`notes/hardware-xvf3800-fulltest-2026-04-16.md`](notes/hardware-xvf3800-fulltest-2026-04-16.md) |
| Open-LLM-VTuber architecture analysis | [`notes/olv-deep-dive-2026-04-16.md`](notes/olv-deep-dive-2026-04-16.md) |
| AudioStreamPlayer + bench design | [`notes/self-player-and-bench-2026-04-17.md`](notes/self-player-and-bench-2026-04-17.md) |

## Tests

```bash
python -m pytest tests/ -q                     # Unit (1060)
python system_tests/runner.py --mode cc        # End-to-end (Claude Code)
python system_tests/runner.py                  # End-to-end (interactive)
```

## License

MIT — see [`LICENSE`](LICENSE).
