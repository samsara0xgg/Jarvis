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

After any change that touches the dialogue pipeline, run a **targeted** system test in CC mode. Parse the JSON output, fix failures automatically, batch `needs_review` items to the user.

### When NOT to run system tests
- Pure refactor without behavior change
- Utility functions (formatting, parsing, etc.)
- Unit-test-only code paths
- Documentation, comments, type hints

### Two modes — pick the right one

**Mode A: Ad-hoc prompts** (preferred — fast, targeted, cheap)

Use `--prompts` with 2-5 hand-picked utterances that exercise exactly what you changed. Each `|` is a new turn within one session (multi-turn context preserved).

```bash
python system_tests/runner.py --mode cc --prompts "打开卧室灯|调到50%|调成暖光|关掉"
```

- Takes < 30s, costs < $0.01
- All 33 trace fields still captured in JSON (route, device state, memory, tokens, etc.)
- Use when change is narrow (one function, one skill, one prompt fragment)

**Mode B: YAML suite** (broader regression testing)

Use `--suite` when the change affects a whole pipeline stage (router logic, LLM client, memory extraction).

```bash
python system_tests/runner.py --mode cc --suite <name>
```

Available suites: `general`, `smart_home`, `memory`, `routing`, `multi_turn`, `error_handling`, `skill_learning`, `cloud_chat`

### File-to-test mapping (what to run based on what you changed)

| Changed file | Preferred mode | What to test |
|-------------|---------------|--------------|
| `core/intent_router.py` or route prompt | Mode B: `routing` | Plus Mode A with edge-case inputs of the changed logic |
| `core/local_executor.py` | Mode A | Prompts that hit the changed action dispatcher |
| `devices/*.py`, device drivers | Mode A | 2-3 prompts controlling the changed device |
| `memory/manager.py`, `memory/retriever.py` | Mode B: `memory` | Mode A for targeted extraction/retrieval changes |
| `memory/direct_answer.py` | Mode A | Prompts that should hit L1 direct answer |
| `core/llm.py` streaming / tool loop | Mode B: `cloud_chat` | Prompts that trigger tool_use |
| `core/tts.py` | Mode A with `--tts --live` | Hear the result; verify cache hit/miss |
| `core/skill_factory.py` / `core/learning_router.py` | Mode B: `skill_learning` | Detection only (don't trigger real generation) |
| `skills/*.py` new/modified skill | Mode A | Prompts exercising the skill directly |
| `core/command_parser.py` (colors, aliases) | Mode A | Prompts with the new color/alias + edge cases |
| `core/automation_engine.py`, keyword rules | Mode A | Trigger words for the rule |
| `auth/*`, `core/speaker_verifier.py` | None (unit tests suffice) | — |
| `ui/oled_display.py`, `ui/web/*` | None (visual, manual) | — |

### Ad-hoc prompt design rules (maximize single-test quality)

1. **Include a happy path** — the normal case the change should handle
2. **Include an edge case** — the specific bug being fixed or the unusual input
3. **Include multi-turn context if relevant** — test that conversation history still works
4. **Keep 2-5 prompts** — more is diminishing returns, costs API dollars
5. **Use `--live` only if the change affects real device interaction** — otherwise sim is faster and doesn't disturb your lights
6. **Use `--verbose` only when debugging the test itself** — normally the Tier 2 JSON is enough

### Example CC workflow after a code change

```bash
# Changed `core/command_parser.py` to add "tiffany蓝" color support
python system_tests/runner.py --mode cc \
  --prompts "把桌灯1调成tiffany蓝|桌灯1什么颜色|调成普通蓝色"

# Parse JSON → check route.actions[0].value == "tiffany_blue"
#            → check device_changes shows color change
#            → check response mentions color change
# If any failure, fix and re-run
# If needs_review items, batch them to user for feedback
```

### Always include in CC test invocation
- `--mode cc` (JSON output, parseable)
- `--no-interactive` implied by `--mode cc`
- Prefer `--live` only when verifying real Hue behavior

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
