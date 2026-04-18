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

Voice assistants that don't let you cut them off feel adversarial. The naive design — buffer 500ms of mic audio, run full ASR, check for stop keywords — produces a 600-700ms latency floor that users can already regret an interruption inside. Yue's interrupt path trades the "instant detection" fantasy for tighter alignment with how people actually interrupt: at clause boundaries, with short single-character markers, expecting the system to fade rather than guillotine-cut.

**Architecture choice.** Seven alternatives were evaluated across five architectural levels (parameter tuning → component swap → pipeline redesign → UX paradigm shift → hardware reconstruction). KWS-only detection was rejected for two reasons: Chinese single-character homophones in the keyword set (`灯` vs `等`, `挺` vs `停`) cause unfixable false positives, and a fixed keyword vocabulary is brittle on unseen phrasings (`别说了`, `够了够了`). Speculative pause and continuous probabilistic ducking were deferred. The shipped design is two-stage soft-stop / hard-stop on a shared SenseVoice ASR path, with proactive yielding (insert silence at sentence boundaries) and DOA filtering via XVF3800 as the long-term roadmap.

**Migration story.** The first implementation ran a separate streaming Zipformer transducer — the textbook low-latency-KWS approach. Benchmarking on April 17 told a different story: speech-to-detect median for "停" was **1760ms** because the streaming decoder needed 500ms of buffered audio to commit a single-character Chinese token, and its training corpus was sparse on isolated syllables. The migration unified the interrupt path onto the main SenseVoice INT8 stack with VAD-gated segments. New median: **1179ms (-33%)**, with the same reliability as the main ASR loop because it is literally the same model and same normalizer (injected as constructor arguments, not re-instantiated).

**Pipeline.** During TTS playback, a dedicated mic thread feeds frames into Silero VAD with mode-based dBFS thresholds (`headphones` = -40 dBFS, `speakers` = -22 dBFS to reject TTS bleed). On IDLE→ACTIVE transition, `on_soft_pause` ducks playback to 30% volume over a 30ms ramp. The active segment buffers with **500ms pre-roll** (catches the initial consonant of "停" that the VAD required-hits gate would otherwise swallow into "嗯") and **200ms post-roll** (trailing fricatives). Segments under 150ms are dropped as noise. Closed segments dispatch async to a worker thread: SenseVoice transcribes, the three-layer `ASRNormalizer` (manual corrections → structured aliases → optional fuzzy match) cleans the text, and substring matching against `INTERRUPT_KEYWORDS = {"停", "等一下", "打住", "暂停", "等等", "你听我说", ...}` decides whether `on_interrupt` fires. On match, the ring buffer flushes and the LLM cancels.

**The player.** Gain-ducking soft-stop required writing `core/audio_stream_player.py` (485 lines) because both `afplay` and `ffplay` exhibit a ~300ms loop-tail artifact when paused via SIGSTOP — a macOS CoreAudio HAL behavior, not a player bug. The replacement is a single long-lived `sd.OutputStream` fed by a lockless SPSC ring buffer, with sample-accurate gain ramps applied inside the PortAudio callback (no allocation, no locks, GIL-protected atomic indices). A subtle bug in `GainRamp` had to be fixed: the denominator must be `step - 1` so the last ramp sample exactly equals the target value, otherwise a 0.0015 jump at 48 kHz produces an audible -56 dBFS click. The custom player also eliminates the per-sentence subprocess spawn cost (~30-150ms per sentence in the legacy path), giving zero inter-sentence gap.

**Status and metrics.** Production benchmarks (after WP6/WP7): **1179ms median** speech-to-detect for "停", **911ms** for multi-character "等一下", p95 under 1850ms, **0.0/s false-positive rate** on 30s of controlled silence with XVF3800 input. Soft-stop and unduck audio verified clean (no loop-tail, no click) over 10818 callbacks with zero underflows. 54 unit tests across three files exercise ring-buffer wrap-around, gain-ramp boundary correctness (the click-regression test lives in `test_audio_stream_player.py`), VAD-segment dispatch, the fired-flag lifecycle, and the IDLE↔ACTIVE state machine with its 3-second no-keyword auto-resume timeout. Next: proactive yielding — insert silence at clause boundaries where the empirical ~80% of natural interrupts happen anyway — followed by XVF3800 spatial gating once the hardware arrives.

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

Skills sit at the boundary between fixed tools and learned behavior. Yue runs a hybrid: a small set of hand-authored Python functions for things that need code, a declarative YAML format for the API-wrapping majority, and a planned discovery pipeline that mines the trace table for new skill candidates.

**Two-tier registration.** `core/tool_registry.py` (195 lines) unifies both formats behind a single dispatch table:

| Tier | Defined as | Currently live |
|------|------------|----------------|
| Python | `@jarvis_tool` decorator on a function in `tools/` | 11 functions across `reminders.py`, `smart_home.py`, `time_utils.py`, `todos.py` |
| YAML | declarative spec under `skills/` or `skills/learned/` | `skills/weather.yaml` (production), `skills/learned/exchange_rate.yaml` (migrated) |

Each tool carries an annotation set (`read_only`, `destructive`, `idempotent`, `required_role`) and is filtered through a four-tier RBAC hierarchy (`guest` < `member`/`resident` < `family`/`admin` < `owner`) before being exposed to the LLM as a function-calling schema.

**YAML skill schema.** Production example (`skills/weather.yaml`):

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

The interpreter (`core/yaml_interpreter.py`, 249 lines) executes the action against a Jinja2 `ImmutableSandboxedEnvironment` and enforces both a per-skill `allowed_domains` whitelist and a hardcoded private-IP block (RFC1918 + loopback) to prevent SSRF. The `to_tool_definition()` method emits an OpenAI-compatible function-calling schema, so YAML skills are indistinguishable from native Python tools at the LLM call site.

**Why YAML for the majority.** The bet is that roughly half of useful skills are HTTP wrappers plus JSON shaping — a class where Python adds bug surface but no expressiveness. Forcing a canonical declarative form (single `http_get` keyword, named extracts, explicit retry semantics) eliminates the failure modes that show up when an LLM picks Python idioms freely. Side-by-side research on equivalent multi-step tasks showed a constrained DSL substantially outperforming open-ended Python generation, which informed the choice to default new auto-generated skills to YAML.

**The discovery pipeline (designed, not yet wired).** The `trace` table makes the loop possible: every conversation turn records `path_taken`, `tool_calls` (JSON), `outcome_signal`, `latency_ms`, and detected emotion. A planned nightly batch will:

1. Cluster the last 24h of `(intent, skill_match, success, user_correction)` tuples.
2. Detect hotspots via mixed signal — frequency (≥3 occurrences with embedding cosine > 0.85), importance weighting (vocal emphasis, repeated requests), and failure-driven triggers (user correction on an existing skill).
3. Compile candidates: an LLM (Grok 4.20 for spec generation, Claude reserved for the rare novel-Python case) drafts a YAML skill from 3-5 representative trace examples.
4. Validate through three gates: schema correctness, replay against the original trace examples (≥80% match), and embedding similarity check vs the existing library (<0.9 to avoid duplicates).

**Shadow then canary.** Promotion design follows a 7-day shadow period where the candidate runs in parallel with the existing path, output recorded but not returned. Output similarity is judged in three layers — structural matching (free, exact name and parameter match), embedding similarity on natural language outputs (free, ~10ms), LLM-as-judge on the gray zone (~$0.003 per turn, ~2s) — with promotion gated at ≥85% alignment overall and ≥95% for safety-critical categories. After promotion, a 48h canary monitors for >20% drop in expected trigger rate and auto-rolls back on regression. Skill execution failures fall through to raw LLM dispatch rather than surfacing a hard error to the user.

**Status.** The static layer is fully live: 11 Python tools, 2 YAML skills (production + learned), unified registry, RBAC, sandboxed execution. The discovery loop is designed but not yet wired — the trace table is in place, but the nightly batch, hotspot detector, compilation prompt, and shadow framework are pending implementation. The pending learned-skill queue currently holds one candidate (`fifa_tickets`, status `pending_review`) awaiting manual approval.

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
