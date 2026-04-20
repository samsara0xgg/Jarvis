# YUE — allen的私人语音管家

## Rules

- Answer first, explore later. discuss first, write code later.
- Run `python -m pytest tests/ -q` only after a major change.
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
python system_tests/runner.py --mode cc --suite <name>  # system test (Claude code mode)
python system_tests/runner.py     # system test (human interactive)
```

## Git

- Commit 消息：`type(scope): English description`，type ∈ `feat fix refactor test docs chore perf data`
- 一件事一个 commit。禁 `git add .` / `-am` 混提交 / `push -f` / `--no-verify`
- 大重构走 `feat/xxx` branch，其他直接 main
- 详见 `docs/git-guide.md`（完整 cheatsheet + gitignore 原则 + 恢复命令）

## System Tests

After changes that affect runtime behavior (skip for pure refactors):

1. Read the diff, draft 3-4 prompts that exercise the change.
2. **Present prompts to user and wait for explicit approval before running.**
3. On approval, run:
   ```bash
   python system_tests/runner.py --mode cc --prompts "<p1>|<p2>|..."
   ```
4. Parse JSON, fix `failures` automatically, batch `needs_review` items to user.

`general.yaml` available as smoke-test baseline:
```bash
python system_tests/runner.py --mode cc --suite general
```
 ## Stack

  Python 3.12 · config.yaml for all settings
  ASR: SenseVoice-Small INT8 (sherpa-onnx) + Whisper fallback
  VAD: Silero (silero_direct via onnxruntime)
  LLM: xAI grok-4.20-0309-non-reasoning (default) · grok-reasoning / Claude Opus 4.7 (voice-switchable)
  Intent router: Groq Llama-3.3-70B (primary) → Cerebras llama3.1-8b (fallback)
  TTS: MiniMax speech-02-turbo (primary) → OpenAI gpt-4o-mini-tts → Azure → edge-tts → pyttsx3
  TTS player: AudioStreamPlayer (persistent sd.OutputStream + ring buffer + gain ducking, replaces afplay subprocess)
  Memory: SQLite + FastEmbed (bge-small-zh-v1.5) + Observer (Grok primary, Gemini-2.5-flash fallback) + GPT-4o-mini extraction
  Wake: openwakeword (hey_jarvis_v0.1)
  Devices: Philips Hue (live) + sim
  Auth: SpeechBrain ECAPA-TDNN voiceprint + 4-tier roles
  Frontend: `desktop/` (Electron Pet Mode) + `ui/web/` (browser UI + Live2D)

  ## Architecture

  ```
  Mic → Wake word → Record(VAD) → [Voiceprint + ASR] parallel
    → Farewell shortcut (120ms)
    → DirectAnswer from memory (no LLM)
    → [Intent route + Memory query] parallel
    → Local exec OR Cloud LLM (streaming, tool-use loop)
    → TTS pipeline (AudioStreamPlayer, emotion-aware) → Speaker
    ↑ InterruptMonitor (Silero VAD + SenseVoice re-decode) → soft-stop via ducking
    → Background: memory extraction + dedup + behavior log
  ```

  ## Coding Standards

  - Type hints everywhere · Google-style docstrings
  - Config from config.yaml, never hardcode
  - Graceful degradation when hardware unavailable
  - New skills: inherit `skills.Skill`, implement `name/description/parameters/execute`
  - ASR misrecognition: add to `asr_corrections` (with `require_context`) or `asr_aliases` in config.yaml — don't patch model
  or prompts

  ## Obsidian Wiki

  继承全局协议（`~/.claude/CLAUDE.md` + `~/Documents/Obsidian Vault/_SCHEMA.md`）。项目 vault：`~/Documents/Obsidian Vault/`（根目录即 jarvis vault，无子目录）。开场读 `_hot.md`；`/clear`/"收尾"/30+ 轮时主动提议写 `sessions/`。