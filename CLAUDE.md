# Jarvis — 小月私人语音管家

## Rules

- Answer first, explore later. No unsolicited long outputs.
- Minimal diffs. "Simplify" means refactor in place, not rewrite.
- Ask before switching libraries/frameworks.
- Run `python -m pytest tests/ -q` after every change.
- Commit OK, **never push** unless explicitly asked.
- No Co-Authored-By in commit messages.
- Use Grep/Glob before spawning Agents.
- Don't modify model files under `data/speechbrain_model/` or `data/sensevoice-small-int8/`.
- Don't hardcode IPs, API keys, or file paths. Read from config.yaml.
- Don't bypass `permission_manager` for device operations.
- Don't use `print` — use `logging`.

## Quick Reference

```bash
python jarvis.py --no-wake        # dev (press Enter to record)
python jarvis.py                  # prod (wake word "Hey Jarvis")
python -m pytest tests/ -q        # unit tests (~900)
python system_tests/runner.py --mode cc --suite <name>  # system test (CC mode)
python system_tests/runner.py     # system test (human interactive)
```

## System Tests

After implementing or fixing a **feature that touches the pipeline** (routing, device control, memory, skills, LLM response), run the relevant system test suite in CC mode:

```bash
python system_tests/runner.py --mode cc --suite <relevant_suite>
```

Suites: `smart_home`, `memory`, `routing`, `multi_turn`, `error_handling`, `skill_learning`, `cloud_chat`

- Parse the JSON output. Fix any `failures` automatically.
- Collect all `needs_review` items and present to user in one batch for feedback.
- Do NOT run system tests for changes that only affect unit-test-level code (utils, refactors with no behavior change).

## Stack

Python 3.13 · config.yaml for all settings
ASR: SenseVoice-Small INT8 (sherpa-onnx) + Whisper fallback
LLM: xAI Grok-4.1-fast (main) · Groq Llama-3.3-70B (intent router)
TTS: MiniMax (primary) → edge-tts → pyttsx3 (fallback chain) + emotion mapping
Memory: SQLite + FastEmbed (bge-small-zh-v1.5) + GPT-4o-mini extraction
Wake: openwakeword (hey_jarvis_v0.1)
Devices: Philips Hue (live) + MQTT + sim
Auth: SpeechBrain ECAPA-TDNN voiceprint + 4-tier roles

## Architecture

```
Mic → Wake word → Record(VAD) → [Voiceprint + ASR] parallel
  → Farewell shortcut (120ms)
  → DirectAnswer from memory (no LLM)
  → [Intent route (Groq) + Memory query] parallel
  → Local exec OR Cloud LLM (streaming, tool-use loop)
  → TTS pipeline (dual-thread, emotion-aware) → Speaker
  → Background: memory extraction + dedup + behavior log
```

## Coding Standards

- Type hints everywhere · Google-style docstrings
- Config from config.yaml, never hardcode
- Graceful degradation when hardware unavailable
- New skills: inherit `skills.Skill`, implement `name/description/parameters/execute`
