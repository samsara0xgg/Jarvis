# Yue

[简体中文](README.md) · **English**

**A personal voice AI that grows with you.**

*Compounding memory, room-aware perception, and a self-improving skill loop.*

<!-- TODO: add Pet Mode demo GIF here -->

## Overview

Yue is an end-to-end voice assistant designed around a single thesis: assistant utility compounds. Most voice AIs today — Alexa, Siri, ChatGPT — treat each interaction as stateless. Yue inverts this: an observer extracts priority-tagged observations from each conversation, a stable-prefix builder injects them into the next session's prompt context, a trace table records every tool call for the skill-discovery loop. The longer it runs, the less you have to repeat yourself.

Built end-to-end without LangChain or any agent framework. ~25 core modules, 1060 unit tests, designed to run continuously on Mac (development) and Raspberry Pi 5 (production), with an Electron desktop pet on the side.

## Capabilities

### Full-duplex interrupt

During TTS playback, a dedicated mic thread gates audio through Silero VAD into per-utterance segments and dispatches each closed segment asynchronously to the same SenseVoice ASR used by the main loop. On VAD trigger, playback ducks to 30% volume over a 30ms ramp via a custom PortAudio stream player. On confirmed keyword match (`{"停", "等一下", "打住", "暂停", "等等", ...}`) the ring buffer flushes and the LLM cancels. Pre-roll (500ms) captures the initial consonant of single-character keywords; post-roll (200ms) catches trailing fricatives.

| Metric | Value |
|---|---|
| speech-to-detect ("停"), median | 1179 ms |
| speech-to-detect ("等一下"), median | 911 ms |
| p95 latency | < 1850 ms |
| False positives on 30s controlled silence | 0.0 / s |
| Audio underflows over 10818 callbacks | 0 |

In practice, mid-sentence "停" drops the volume within roughly 350ms of speech onset and reaches full stop within another 700ms — smooth fade, no restart artifacts, no inter-sentence gap, no swallowed first consonants. Long-term: insert silence at clause boundaries (the empirical ~80% of natural interrupts happen there) to bring perceived latency near zero, and add XMOS XVF3800 directional gating once hardware lands so the system ignores TV and family voices.

### Compounding memory

Each completed conversation triggers an observer (LLM function calling, Grok-4.20 primary / Gemini 2.5 Flash fallback) that extracts priority-tagged text bullets, grouped by date and stored in SQLite. A stable-prefix builder injects the relevant bullets into the next session's system prompt — prompt-cache-friendly, deterministic, no per-query vector retrieval on the read path. A direct-answer fast path uses multi-signal scoring (40% cosine + 25% recency + 20% importance + 15% access frequency) for high-confidence factual recall without invoking the LLM at all.

| Module | Role |
|---|---|
| `observer.py` | Async extraction with four priority tiers (HIGH / MED / LOW / DONE) |
| `stable_prefix.py` | Assembles personality + observations + last ten turns into the LLM context |
| `trace.py` | Per-turn analytics (path, tool calls, emotion, latency, outcome) for the skill-discovery loop |
| `store.py` | SQLite, six tables: memories / user_profiles / episodes / episode_digests / memory_relations / observations |
| `direct_answer.py` | Multi-signal LLM-bypass for repeated queries |

A typical observation log:

```
Date: 2026-04-17
* [HIGH] (14:30) User prefers warm yellow (2700K) in living room
* [MED]  (15:12) User mentioned weekend trip to Vancouver to see friends
* [DONE] (15:45) Reminder set for coffee machine descaling
```

Eight extraction models were benchmarked across twenty Chinese home-dialogue fixtures (smart-home, preference, state-change, temporal, emotion, correction, multi-entity, completion). Grok 4.20 won on price-per-performance: F1 0.88, p95 4.8s, $0.031 per 100 turns. Gemini 2.5 Flash held zero hallucination at twice the cost, kept as fallback. In practice, mention something next week that came up today and it is already part of the prompt context — no "I don't have access to previous conversations" wall, no manual replay.

### Self-improving skill loop

Skills register through a unified `tool_registry` in two formats: Python `@jarvis_tool` decorators for things that need code (11 live functions across `tools/reminders.py`, `tools/smart_home.py`, `tools/time_utils.py`, `tools/todos.py`) and YAML declarative specs for HTTP-wrapper-style skills (`skills/weather.yaml` plus auto-migrated `skills/learned/exchange_rate.yaml`). Both surface to the LLM as identical OpenAI-compatible function-calling schemas. Annotations (`read_only`, `destructive`, `idempotent`, `required_role`) gate each tool through a four-tier RBAC hierarchy: guest < family < trusted < owner.

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

YAML actions execute through a Jinja2 sandbox with per-skill domain whitelisting and an RFC1918 loopback block for SSRF protection. The discovery loop builds on the trace table that's already in place: a nightly batch will detect hot-spot intent patterns (frequency + importance + user-correction signal), draft new YAML candidates from 3-5 representative examples, run a 7-day shadow period with three-tier output similarity judging (structural / embedding / LLM-as-judge), and promote through canary monitoring with auto-rollback on regression. The static layer is live; the discovery pipeline lands incrementally on the same registry, so new skills appear without restarting the assistant.

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
