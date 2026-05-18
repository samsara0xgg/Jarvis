# Legacy Reuse Audit — Day-1 Scope

Output of Build Step 0 (see ADR 0001 § Build order). Walks
`/Users/alllllenshi/Projects/jarvis-legacy/` and classifies every significant
file as **REUSE-VERBATIM**, **ADAPT**, **DISCARD**, or **DEFER-STAGE-2**
against the Day-1 Mac-only flagship scope (`architecture-mac-only.md` +
ADR 0001).

## TL;DR

- ~110 Python source files audited across `core/`, `tools_v2/`, `auth/`,
  `memory/`, `tools/`, plus `jarvis.py` and `setup_hue.py`.
- **Day-1 mandatory reuses match the ADR 0001 initial steal map exactly**:
  `core/llm.py` (ADAPT, trim memory + personality deps), `config.yaml` LLM
  section (REUSE-VERBATIM), `prompts/phase1/v1.md` (REUSE-VERBATIM). The
  scan adds three Day-1 ADAPT candidates the initial map didn't surface:
  `core/tool_result.py` (claim_policy + verification source vocabulary maps
  almost 1:1 to spec §3.4.11 Result Interpreter + §5.3 evidence
  semantics), `core/response_channels.py` (`<voice>` / `<document>` parser
  for Pre-emit Gate output framing), and `tools_v2/registry.py` (lighter,
  cleaner registration mechanics than `core/tool_registry.py` — better
  starting point for `execution/tools.py`).
- One **surprise discard**: `core/event_bus.py` is the only L2/L3 plumbing
  candidate Legacy has, and it is fundamentally pub/sub — incompatible
  with spec §3.3.1 append-only Event Log + read-only projection fold.
  Confirms ADR 0001's expected Legacy-bypass.
- `auth/permission_manager.py` (only 71 lines) is a thin role-hierarchy
  check tied to `devices.base_device.SmartDevice`. **Not** reusable for
  Pre-action Gate — spec wants `caller_principal` + `AuthorizationLease`
  on `ActionRequest`, not role-string → device check. DISCARD.
- All of `memory/` is DEFER-STAGE-2 with one possible exception:
  `memory/hot/assembler.py`'s `PromptBlock` / `PromptContext` shape is a
  pattern worth carrying forward when L3 Situation Packet assembler grows
  multi-block prompts, but on Day-1 the Packet is plain dicts and the
  Block 1–4 model is not built.
- All audio (asr / vad / tts / wake / inherent), `devices/`, `desktop/`,
  `ui/`, `esp32/`, `deploy/`, `tools/` (legacy YAML/Python tools), and
  `core/yaml_interpreter.py` are DEFER-STAGE-2 by category — they map to
  surface, devices, and high-impact action infra that Day-1 explicitly
  excludes.
- **Blocking concern (for Allen review)**: `core/llm.py` carries two
  hard-coded import-time dependencies (`core.personality` for system
  prompts, `memory.hot.assembler.PromptContext` as a typed parameter on
  every entry point). Trimming those is required but is more than
  "remove two imports" — see Open Questions §3.

## Initial steal map (from ADR 0001) — verification status

| Legacy file | ADR initial classification | Your finding | Notes |
|---|---|---|---|
| `core/llm.py` (1650 lines) | ADAPT → `jarvis/decision/llm.py`; drop `memory.hot.assembler` + `core.personality` deps | **Confirmed ADAPT.** The two import-time deps are surgical to remove (see §Historical questions resolved by ADR 0001 for the prompt-context replacement decision). Multi-provider client + tool-use loop + presets + metadata + cache-control headers + sticky `x-grok-conv-id` routing are all keep-worthy. | Trim list: 14 import `core.personality`, 15 import `memory.hot.assembler`, all `prompt_context` kwargs (6 sites), `_personalize_system()` (1630-1654). Provider switch and `_chat_anthropic` / `_chat_openai` / `chat_stream` paths stay as-is. Tracker (`self._tracker.record_success(...)`) is decoupled — keep, wire to L4 health later. |
| `config.yaml` LLM section (lines 277–301) | REUSE-VERBATIM | **Confirmed.** Section uses `provider: openai`, `default_preset: deep`, presets `fast=gpt-5.4-mini` + `deep=gpt-5.5`, both pointed at `https://openrouter.icu/v1` with `OPENROUTER_PROXY_KEY`. Matches ADR. | Take lines 277–301 byte-for-byte. Take nothing else from config.yaml — see §config.yaml section. |
| `prompts/phase1/v1.md` | REUSE-VERBATIM | **Confirmed.** 164 lines, 11 XML-tagged sections (identity, current_control_surface, communication, evidence framing, voice/document split etc.). Already neutral re: voice (does not assume TTS surface exists). Drops in clean as `prompts/jarvis_v1.md`. | No edits. |
| `tools_v2/registry.py` | ADAPT pattern only (Day-1 rewrite uses `caller_principal` + `AuthorizationLease`) | **Confirmed ADAPT-pattern.** 178 lines, clean and Hermes-pared-down: `ToolEntry { name, schema, handler, caller_scope }` + `ToolRegistry { register, dispatch, get_definitions }`. The `caller_scope: set[str]` field is exactly the shape Day-1 wants — rename `caller_scope` → `caller_principal_scope` and swap `str` for the L1 caller principal enum. Anthropic-shape `{name, description, input_schema}` output already matches Day-1 LLM consumption. Borrow the file outright; rewrite the type. | Drop `caller_scope=set[str]` strings, use `Set[CallerPrincipal]` enum from `jarvis/constitution`. Add `ActionLifecycle 8-state` orchestration — not in Legacy. |
| `core/regex_router.py` + `core/command_parser.py` | Borrow `^...$` full-anchor convention only | **Confirmed.** `regex_router.py` (325 lines) has 21 closed patterns; Day-1 needs ~3. Anchor pattern (`re.compile(r"^...$")`) and `RegexMatch` dataclass shape are the only carries. `command_parser.py` (404 lines) is Hue-specific (color XY tables, scene aliases, etc.) — pure DISCARD for Day-1. | Don't import — write the 3 patterns inline in `jarvis/decision/intent.py`. |

ADR initial map verified. Three additions surfaced (see § Surprises).

## Inventory

### core/

| file | LOC | classification | maps to | notes / required edits |
|---|---|---|---|---|
| `__init__.py` | 12 | DISCARD | — | Re-exports `AudioRecorder` / `CommandParser` / `SpeechRecognizer` — all audio, all DEFER. |
| `llm.py` | 1650 | ADAPT (mandatory) | `jarvis/decision/llm.py` | See initial steal map row. Provider switch, `_chat_anthropic`, `_chat_openai`, `chat_stream`, presets, metadata. Trim deps on `memory.hot.assembler` + `core.personality`. |
| `event_bus.py` | 69 | DISCARD | — | Pub/sub with wildcard. Conflicts with spec §3.3.1 append-only Event Log + projection fold. Confirmed Legacy-bypass per ADR Build Step 4. |
| `tool_registry.py` | 398 | DISCARD | — | Unified Python + YAML registry, RBAC by `user_role` string. Spec wants `caller_principal` + `ActionLifecycle`; YAML skills are DEFER-STAGE-2. `tools_v2/registry.py` is the cleaner starting point (see surprise). |
| `tool_result.py` | 500 | **ADAPT** (surprise) | `jarvis/state/projections.py` or `jarvis/decision/__init__.py` (Result Interpreter) | Defines `outcome.type` vocabulary (`observed/changed/created/updated/deleted/delivered/queued/interrupt_requested/no_change/legacy_unverified/failed`), `verified` flag, `verification_source` string, and a `claim_policy` with `allowed_claims` / `forbidden_claims` — this is **almost a verbatim implementation of spec §3.4.11 (Result Interpreter) + §5.3 (Evidence semantics) + §8 (Evidence model levels)**. Reuse the vocabulary and the `claim_policy` derivation; rewrite the normalize path to fit the new schema. Highest-value reuse the initial map missed. |
| `response_channels.py` | 52 | **ADAPT** (surprise) | `jarvis/surface/cli.py` (Pre-emit Gate boundary) | Parses `<voice>...</voice>` + `<document>...</document>` from LLM output. The Day-1 prompt (`prompts/phase1/v1.md`) emits this structure. The CLI surface needs to extract the `<document>` channel for stdout — this is the parser. Drop verbatim. |
| `regex_router.py` | 325 | ADAPT-pattern | `jarvis/decision/intent.py` (Tier 0) | See initial map. Use anchor convention + `RegexMatch` shape; ~3 patterns Day-1. |
| `command_parser.py` | 404 | DISCARD | — | Hue-specific color / scene / device parsing. DEFER-STAGE-2 only if Hue ever returns. |
| `personality.py` | 317 | DEFER-STAGE-2 | (none Day-1) | Builds the system prompt blocks (identity + situation + emotion + NSFW addon). Day-1 prompt is `prompts/jarvis_v1.md` (verbatim), whose prompt copy names Jarvis. Personality.py is also entangled with `user_emotion` (SenseVoice → audio surface, DEFER). Sensitive content (NSFW addon) is a separate review — keep out of Day-1 entirely. ADR 0001 resolves identity: architectural identity remains L1/L2; the prompt is an L3 prompt text asset, not the identity source of truth. |
| `scheduler.py` | 219 | DEFER-STAGE-2 | `jarvis/deployment/` (Stage 2) | APScheduler + SQLite persistent jobstore. Day-1 ADR explicitly defers DeferredExecution + sleep/wake + launchd. Useful skeleton when Stage 2 wires `scheduler.fired` triggers. |
| `health.py` | 330 | DEFER-STAGE-2 | (probably `jarvis/deployment/` or `jarvis/runtime/`) | `ComponentTracker` with 3-state circuit breaker + probe registration + EventBus emission. Day-1 doesn't need this — only one LLM, single process, no fallback chain. Useful in Stage 2 when multiple providers / TTS / ASR are wired. The `record_success/record_failure` API is what `core/llm.py` calls; we'll need a no-op stub in Day-1 (`self._tracker = None` is already supported). |
| `mcp_server.py` | 234 | DEFER-STAGE-2 | (out-of-scope read-only MCP) | Stdio MCP exposing trace queries. Day-1 has no trace v3 schema; this is a tooling-around-Memory feature, not core. |
| `inherent_wake_listener.py` | 203 | DEFER-STAGE-2 | (Inherent panel later) | Background openwakeword listener feeding voice into Inherent surface. Audio + Inherent both DEFER. |
| `media_ducking.py` | 159 | DEFER-STAGE-2 | (audio surface later) | macOS osascript volume ducker. Audio surface DEFER. |
| `asr_normalizer.py` | 231 | DEFER-STAGE-2 | (ASR surface later) | Three-layer (manual / alias / fuzzy) ASR correction. Audio DEFER. |
| `audio_recorder.py` | 327 | DEFER-STAGE-2 | (audio surface later) | 16k mono WAV recorder. Audio DEFER. |
| `audio_stream_player.py` | 546 | DEFER-STAGE-2 | (TTS surface later) | Persistent-stream sounddevice player with gain ramp + ring buffer. TTS DEFER. |
| `cc_jsonl_reader.py` | 341 | DEFER-STAGE-2 | (worker integration later) | Reads `~/.claude/projects/<encoded-cwd>/*.jsonl`. Day-1 spawn_worker is a stub; real CC integration is Stage 2. Pattern worth borrowing later for `worker.artifact_observed`. |
| `interrupt_monitor.py` | 591 | DEFER-STAGE-2 | (audio + barge-in later) | VAD-gated full-duplex barge-in. Audio DEFER. |
| `speech_recognizer.py` | 340 | DEFER-STAGE-2 | (ASR surface later) | SenseVoice / MLX-whisper / openai-whisper backends. Audio DEFER. |
| `tts.py` | 1378 | DEFER-STAGE-2 | (TTS surface later) | Azure / Edge / pyttsx3 / MiniMax pipeline. Audio DEFER. |
| `tts_minimax_ws.py` | 297 | DEFER-STAGE-2 | (TTS surface later) | MiniMax WS T2A streaming client. Audio DEFER. |
| `tts_preprocessor.py` | 148 | DEFER-STAGE-2 | (TTS surface later) | TTS text cleaning. Audio DEFER. |
| `vad_silero.py` | 334 | DEFER-STAGE-2 | (audio surface later) | Silero VAD ONNX runtime. Audio DEFER. |
| `wake_word.py` | 124 | DEFER-STAGE-2 | (audio surface later) | openwakeword detector. Audio DEFER. |
| `yaml_interpreter.py` | 2363 | DEFER-STAGE-2 | (high-impact actions later) | YAML skill interpreter — http_get/post, file_write with allowed_root, macos_paste with AppleScript Cmd+V, zellij_send. Massive surface area, all higher-risk than Day-1 needs. Stage 2 candidate when L4 grows side-effect tools. Note: `cc_read_state` action (line 1574) is the only piece adjacent to Day-1's worker semantics; revisit when real Codex integration replaces the stub. |

### tools_v2/

Day-1 explicitly **does not** take the thick file-operation tools (ADR
"Day-1 does not take" list: `file_operations.py`, `terminal_tool.py`,
`read_file.py`, etc.). Day-1 needs only two stub tools (`spawn_worker`,
`verify_diff`) plus `read_state`. So most of this directory is
DEFER-STAGE-2 wholesale.

| file | LOC | classification | maps to | notes |
|---|---|---|---|---|
| `__init__.py` | 23 | DISCARD | — | Side-effect imports of file tools that DEFER. |
| `registry.py` | 178 | **ADAPT** (mandatory per ADR) | `jarvis/execution/tools.py` | See initial steal map. Borrow `ToolEntry` shape + `register` / `dispatch` / `get_definitions`. Swap `caller_scope: set[str]` for `Set[CallerPrincipal]`. **Add ActionLifecycle 8-state orchestration around dispatch** — not in Legacy, this is new per spec §3.5.7. |
| `helpers.py` | 59 | ADAPT (tiny) | `jarvis/execution/tools.py` (helpers) | `tool_error()` / `tool_result()` JSON serializers — 30 lines. Drop in alongside the registry. |
| `_paths.py` | 51 | DEFER-STAGE-2 | (Stage 2 file tools) | `is_safe_resolved_path` denylist + HOME/tmp allowlist. Useful when real file tools land. |
| `file_safety.py` | 69 | DEFER-STAGE-2 | (Stage 2 file tools) | Write-denied path list (`~/.ssh/*`, `/etc/passwd` etc.). Stage 2. |
| `file_state.py` | 242 | DEFER-STAGE-2 | (Stage 2 file tools) | Cross-tool read/write coordination locks. Stage 2. |
| `binary_extensions.py` | 146 | DEFER-STAGE-2 | (Stage 2 file tools) | Binary extension denylist. Stage 2. |
| `tool_output_limits.py` | 90 | DEFER-STAGE-2 | (Stage 2 file tools) | Configurable truncation caps. Stage 2. |
| `file_operations.py` | 1275 | DEFER-STAGE-2 | (Stage 2 file tools) | LocalFileOperations stdlib reader / writer / patch / linter chain. Stage 2 thick tool. |
| `fuzzy_match.py` | 725 | DEFER-STAGE-2 | (Stage 2 file tools) | 8-strategy fuzzy find/replace for patch tool. Stage 2. |
| `patch.py` | 246 | DEFER-STAGE-2 | (Stage 2 file tools) | V4A multi-file patch tool. Stage 2. |
| `patch_parser.py` | 599 | DEFER-STAGE-2 | (Stage 2 file tools) | V4A format parser. Stage 2. |
| `read_file.py` | 452 | DEFER-STAGE-2 | (Stage 2 file tools) | Thick read with dedup + loop guard + redaction. Stage 2. |
| `write_file.py` | 208 | DEFER-STAGE-2 | (Stage 2 file tools) | Thick write with safety + locks. Stage 2. |
| `search_files.py` | 271 | DEFER-STAGE-2 | (Stage 2 file tools) | rg/grep/fd wrapper. Stage 2. |
| `terminal_tool.py` | 468 | DEFER-STAGE-2 | (Stage 2 high-risk tool) | Foreground shell with destructive guard + workdir denylist. Stage 2, will need AuthorizationLease. |
| `redact.py` | 412 | DEFER-STAGE-2 | (Stage 2) | Regex secret redaction. Stage 2 when logs ship. |

### auth/

| file | LOC | classification | maps to | notes |
|---|---|---|---|---|
| `__init__.py` | 3 | DISCARD | — | Trivial. |
| `permission_manager.py` | 71 | **DISCARD** | — | Role-hierarchy check (`guest < member/resident < family/admin < owner`) against `SmartDevice.required_role`. Tightly bound to `devices.base_device`. Spec §3.4.10 Pre-action Gate uses `caller_principal` + `risk_level` + `AuthorizationLease` + entity-resolve, not a role-string → device check. Not adaptable — this is a different concept under the same name. **Initial map was right to omit it.** |

### memory/

The ADR is explicit: Memory system is **not used** by Day-1 and **not
built**. Spot-checking for cross-cutting helpers, only the prompt-block
shape in `assembler.py` is a candidate, and only as a future pattern
reference.

| file | LOC | classification | maps to | notes |
|---|---|---|---|---|
| `__init__.py` | 1 | DEFER-STAGE-2 | — | — |
| `manager.py` | 168 | DEFER-STAGE-2 | (Stage 2 Memory) | v3 MemoryManager: `build_prompt_context`, `write_observation`. Stage 2. |
| `trace.py` | 553 | DEFER-STAGE-2 | (Stage 2 trace) | Trace v3 34-column SQLite schema. Conflicts with spec's append-only Event Log model — not a refactor target. Stage 2 may pull pieces (cost / latency analytics) but not the schema. |
| `trace_migration.py` | 138 | DISCARD | — | v2 → v3 migration. No Day-1 use; Stage 2 will not preserve v3 schema. |
| `core/store.py` | 716 | DEFER-STAGE-2 | (Stage 2 Memory) | SQLite memory + profile + episode store w/ embeddings. Stage 2. |
| `cold/observer.py` | 278 | DEFER-STAGE-2 | (Stage 2 Memory cold path) | LLM-based observation extraction. Stage 2. |
| `cold/nli_classifier.py` | 149 | DEFER-STAGE-2 | (Stage 2 outcome detection) | Erlangshen NLI. Stage 2. |
| `cold/pricing.py` | 149 | DEFER-STAGE-2 | (Stage 2 cost analytics) | LLM cost calculator from `data/pricing.json`. ADR 0001 Day-1 records token use only (`tests/_artifacts/llm_use_<ts>.json`); USD cost tracking is Stage 2. |
| `cold/outcome_detector.py` | 89 | DISCARD | — | Marked DEPRECATED in source — kept for rollback only. Stage 2 NLI replaces it. |
| `hot/conversation.py` | 119 | DEFER-STAGE-2 | (Stage 2 Memory) | Per-user sliding-window history with JSON persistence. Day-1 `decision/llm.py` will use `conversation_history` pass-in instead; Memory Stage 2 will own persistence. |
| `hot/assembler.py` | 261 | DEFER-STAGE-2 (note pattern) | (future L3 Situation Packet Assembler) | `PromptBlock(content, cache: bool, name)` + `PromptContext.to_anthropic_system() / to_openai_system_str()` is a clean multi-block prompt model. **Day-1 doesn't need it** — Day-1's `prompts/jarvis_v1.md` is a single block, no per-block cache_control. Carry the pattern forward when L3 Situation Packet starts assembling multi-source context blocks. |

### top-level

| file | LOC | classification | maps to | notes |
|---|---|---|---|---|
| `jarvis.py` | ~2300 | **ADAPT-as-template** | `jarvis/runtime/__init__.py` | `JarvisApp.__init__` (lines 76–~280) is the composition root: wires EventBus, ComponentTracker, AudioRecorder, SpeechRecognizer, ASRNormalizer, DeviceManager, PermissionManager, LLMClient, ConversationStore, MemoryManager, TraceLog, pricing, NLI, audio capture, prompt version hash, session id. **Borrow the wiring pattern (single class, single config, single owner of cross-cutting deps), not the contents.** Day-1 `runtime/__init__.py` wires L1 → L2 → L3 → L4 → L5 → L6 in that order; it's the only place crossing layers (per `CLAUDE.md`). The rest of the file is voice loop + interrupt orchestration — DEFER-STAGE-2. |
| `setup_hue.py` | 116 | DISCARD | — | Interactive Hue pairing CLI. Out of scope (no Hue Day-1, no Hue ever in Mac-only). |

### prompts/

| file | LOC | classification | maps to | notes |
|---|---|---|---|---|
| `phase1/v1.md` | 164 | REUSE-VERBATIM | `prompts/jarvis_v1.md` | Confirmed (per initial map). 11 XML sections, voice/document split, evidence framing, Chinese-default, no audio-surface assumptions. Drops in clean. |

### config.yaml

23.8KB file with 21 top-level sections. Day-1 takes **only the `llm:`
section** (lines 277–301) per ADR. Per-section disposition:

| section (line) | classification | notes |
|---|---|---|
| `audio:` (14) | DEFER-STAGE-2 | Audio capture / device / sample rate. Audio surface DEFER. |
| `asr:` (67) | DEFER-STAGE-2 | ASR backend config. Audio DEFER. |
| `devices:` (95) | DEFER-STAGE-2 (likely DISCARD) | Device manager registry. Hue / smart-home, out of Mac-only forever. |
| `hue:` (173) | DISCARD | Hue bridge / aliases / scenes. Out of Mac-only scope. |
| `llm:` (277–301) | **REUSE-VERBATIM** | Day-1 mandatory. `provider: openai`, `default_preset: deep`, presets fast (gpt-5.4-mini) + deep (gpt-5.5), `base_url: https://openrouter.icu/v1`, `api_key_env: OPENROUTER_PROXY_KEY`. Take byte-for-byte. |
| `tts:` (306) | DEFER-STAGE-2 | MiniMax / Azure / Edge. Audio DEFER. |
| `wake_word:` (349) | DEFER-STAGE-2 | openwakeword. Audio DEFER. |
| `audio_ducking:` (374) | DEFER-STAGE-2 | macOS volume ducker config. Audio DEFER. |
| `session:` (381) | DEFER-STAGE-2 | Voice session timing (utterance_duration etc.). Audio DEFER. |
| `memory:` (391) | DEFER-STAGE-2 | DB paths, max_conversation_turns, NLI config. Memory DEFER. |
| `skills:` (423) | DEFER-STAGE-2 | YAML skill loader paths. Tools DEFER. |
| `oled:` (439) | DEFER-STAGE-2 | RPi OLED display. RPi DEFER. |
| `mqtt:` (450) | DEFER-STAGE-2 | MQTT broker. Cross-domain DEFER. |
| `scheduler:` (461) | DEFER-STAGE-2 | APScheduler `db_path`. Scheduler Stage 2. |
| `health:` (475) | DEFER-STAGE-2 | Circuit-breaker thresholds. Health Stage 2. |
| `logging:` (494) | DEFER-STAGE-2 (or ADAPT trivially) | Log level / format. Trivial — Day-1 `jarvis/deployment/__init__.py` can hardcode or read its own minimal section. |
| `interrupt:` (501) | DEFER-STAGE-2 | Barge-in keywords. Audio DEFER. |
| `asr_corrections:` (556) | DEFER-STAGE-2 | ASR override rules. Audio DEFER. |
| `asr_aliases:` (568) | DEFER-STAGE-2 | ASR alias table. Audio DEFER. |
| `asr_normalizer_fuzzy:` (574) | DEFER-STAGE-2 | Layer-3 fuzzy ASR config. Audio DEFER. |
| `regex_router:` (585) | DEFER-STAGE-2 | Device aliases, scene aliases, templates. Hue / audio DEFER. |

Resulting Day-1 `config/jarvis.yaml`: just the `llm:` block (lines
277–301), copied verbatim.

### Inventory-only groups

- **`data/`** — Runtime state, not code. `cache/`, `conversations/`,
  `memory/` (SQLite DBs + ONNX models + Sherpa models + Silero VAD ONNX +
  `pricing.json` + `reminders.json` + `scheduler.db`), `realtime_data/`,
  `todos/`. **Classification**: not source — irrelevant to scan. Day-1
  creates its own `~/.jarvis/mac_events.db` + `~/.jarvis/artifacts/` under
  `jarvis/deployment/__init__.py`. The only Day-1 candidate from here is
  `data/pricing.json` (static LLM cost table, if Stage 2 USD cost
  tracking wants per-model rate lookups instead of inline constants).
- **`scripts/`** — bench_llm_v2/v3, bench_voice_pipeline, compute_trace_costs,
  download_silero_vad, dump_prompt_context, eval_runner, export_nli_onnx,
  golden_set yaml fixtures, observer_bench, phase1_smoke, refresh_pricing,
  verify_cache_live, watch_memory. **All DEFER-STAGE-2.** None are needed
  Day-1; bench / eval harness will be redesigned around Tier-1 / Tier-2
  per ADR 0001 acceptance criteria.
- **`deploy/`** — `install.sh`, `jarvis.service` (systemd), `mosquitto.conf`
  (MQTT broker), `pi.yaml` (RPi config), `README.md`. **DEFER-STAGE-2**
  wholesale (RPi). Mac-only deployment is one process + `~/.jarvis/`; no
  install script needed Day-1.
- **`devices/`** — `base_device.py`, `device_manager.py`, `hue/`, `mqtt/`,
  `sim/`. **DEFER-STAGE-2 / DISCARD.** Out of Mac-only scope; even Stage 2
  Mac-only doesn't reintroduce devices. If RPi comes back, this is a fresh
  build per `architecture-mac-only.md` "When RPi comes back" plan.
- **`desktop/`** — Electron shell (`main.js`, `menu.js`, `preload.js`,
  `electron-builder.yml`, `package.json`, `package-lock.json`).
  **DEFER-STAGE-2** (Inherent panel).
- **`ui/`** — `web/` (browser UI with `audio_cache/`, `css/`, `js/`,
  `index.html`, `browser_ws_player.py`), `oled_display.py`, `oled_frames.py`.
  **DEFER-STAGE-2** (web UI Inherent + RPi OLED).
- **`esp32/`** — `relay_node/`, `sensor_node/`. **DEFER-STAGE-2** (RPi
  device fleet).
- **`evals/`** — `runs/`, `regex-bench/`, `router-bench/`, `surrogate_v1/`.
  Eval data + scratch. **DEFER-STAGE-2** (will be redesigned).
- **`experiments/`** — `observer_cn/` and dated observation runs.
  **DEFER-STAGE-2** (Memory observer Stage 2).
- **`overnight/`** — `_runner*.log` + `run*.sh` (overnight benchmark runs).
  **DISCARD** for Day-1 (artifacts, not source).
- **`bench_fixtures/`, `bench_results/`** — golden-set / benchmark
  artifacts. **DISCARD** for Day-1.
- **`system_tests/`** — `assertions.py`, `baseline.py`, `harness.py`,
  `models.py`, `reporter.py`, `runner.py` (~76 KB total). Custom test
  harness. **DEFER-STAGE-2.** Day-1 uses pytest tiers per ADR — much
  smaller, no custom harness needed yet.
- **`tests/`** — ~85 pytest files, ~750 KB. **DEFER-STAGE-2 / DISCARD.**
  Day-1 builds a fresh test tree per ADR 0001 module map
  (`tests/conftest.py`, `tests/unit/`, `tests/scenarios/`, `tests/canary/`).
  Each Legacy test is tied to a Legacy module — when that module is reused
  (e.g. `core/llm.py` → `jarvis/decision/llm.py`), spot-check the
  corresponding test (`tests/test_llm.py`, 32 KB) for fixture patterns,
  but rewrite from scratch.
- **`notes/`** — `draft.txt`, `promptv1.txt`, `fake_notes_*.txt` (test
  fixtures up to 222 KB). Test data. **DISCARD.**
- **`docs/`** — `PROJECT_COCKPIT.md`, `cockpit-maintenance.md`, `git-guide.md`,
  `project-dashboard.md`, `verification-log.md`. **DISCARD** for Day-1
  (project-management notes; not architecture docs and not reusable
  content).
- **`logs/`** — `operations.log`, `web_run.log`, `web_server.log*`. Runtime
  artifacts. **DISCARD.**
- **`realtime_data/`** — empty / artifact. **DISCARD.**
- **`tools/`** (165 LOC `__init__.py` + `reminders.py` (13.9 KB) +
  `smart_home.py` (17.7 KB) + `time_utils.py` (4.5 KB) + `todos.py`
  (13.2 KB)) — Legacy Python `@jarvis_tool` decorator framework + tool
  implementations. **`__init__.py` ADAPT-pattern** — `@jarvis_tool`
  decorator pattern (type-hint reflection → input_schema, RBAC fields,
  `_EXECUTION_CONTEXT` thread-local) is well-designed but Day-1 only needs
  the much-simpler `tools_v2/registry.py` pattern. **All concrete tools
  (`reminders.py`, `smart_home.py`, `time_utils.py`, `todos.py`)
  DEFER-STAGE-2** — Day-1 only has `spawn_worker` and `verify_diff` stubs.
- **`remote/`** — `inherent-swift-migration.md`. **DISCARD** (Inherent doc).
- **`skills/`** — YAML skill files (`cc_approve.yaml`, `cc_show.yaml`,
  `cc_slash.yaml`, `cc_tell.yaml`, `mac_gui.yaml`, `obsidian_inbox.yaml`,
  `type_to_focused.yaml`, `weather.yaml`, `learned/`).
  **DEFER-STAGE-2.** Day-1 doesn't load YAML skills. Some shapes
  (`cc_tell`, `cc_show`) inform Stage 2 Codex integration.
- **`Harness/`** — single Markdown migration doc. **DISCARD.**

## Surprises

### Reusable components NOT in the initial steal map

1. **`core/tool_result.py`** (500 LOC). The `outcome.type` /
   `verification_source` / `claim_policy { allowed_claims, forbidden_claims }`
   vocabulary maps almost 1:1 onto spec §3.4.11 Result Interpreter,
   §5.3 Evidence semantics, and §8 Evidence Model. The `claim_policy`
   computed in `_default_claim_policy()` is exactly the kind of
   raw-tool-result → allowed-claim mapping the Result Interpreter must
   perform. **Recommendation**: ADAPT into `jarvis/decision/__init__.py`
   (Result Interpreter) — borrow the vocabulary + the mapping function,
   rewrite the persistence path to emit `claim.created` /
   `evidence.attached` events instead of returning a JSON envelope.
   Day-1 stub tools (`spawn_worker`, `verify_diff`) need this vocabulary
   to satisfy acceptance criteria D2 / F1 / F2.

2. **`core/response_channels.py`** (52 LOC). Parses `<voice>` /
   `<document>` from LLM output — the prompt `prompts/jarvis_v1.md`
   produces this structure. The CLI surface needs the `<document>` block.
   **Recommendation**: REUSE-VERBATIM, drop into
   `jarvis/surface/cli.py` (or a `surface/_response_channels.py` helper).
   Lighter than rewriting.

3. **`tools_v2/registry.py`** (178 LOC). The initial map mentioned this
   as "pattern only — rewrite using caller_principal". After reading both
   `tools_v2/registry.py` and `core/tool_registry.py`, the former is
   substantially cleaner (no YAML interpreter dep, no RBAC role hierarchy
   baked in, no `_EXECUTION_CONTEXT` thread-local) and the `caller_scope`
   field is structurally identical to the spec's `caller_principal` set.
   **Recommendation**: stronger ADAPT than the initial map implies — copy
   the file, rename `caller_scope` → `caller_principal_scope`, swap the
   type, layer ActionLifecycle 8-state on top.

4. **`tools_v2/helpers.py`** (59 LOC). 30-line JSON serializers
   (`tool_error`, `tool_result`). Tiny but used by every tool. Copy
   verbatim alongside the registry.

5. **`core/scheduler.py`** (219 LOC). Day-1 doesn't need it, but the
   APScheduler + SQLite persistent jobstore wrapper is a clean Stage 2
   starting point for `scheduler.fired` triggers. Flagging for ADR
   awareness — not a Day-1 reuse.

6. **`data/pricing.json`** (8.5 KB static data, ~80 model rate entries
   per `memory/cold/pricing.py` consumer). Day-1 does not compute USD
   cost; if Stage 2 wants real per-model rates instead of inline
   constants, this file + a 30-line copy of `pricing.py`'s
   `compute_cost_usd` are reusable.

### Components in the initial steal map that turn out NOT to be reusable

None outright wrong, but two caveats:

- The initial map says "Drop dependencies on `memory.hot.assembler` and
  `core.personality`" for `core/llm.py`. The actual coupling is deeper
  than two import lines — see Historical questions §3. Day-1 needs a concrete
  replacement strategy for `PromptContext` typed parameters and
  `_personalize_system` fallback.
- The initial map suggests `core/regex_router.py` patterns are borrowable
  for `jarvis/decision/intent.py` Tier 0. They are, but the Day-1 Tier 0
  pattern set is closer to ~1 pattern (the closed shortcuts like `^现在几点了?[?？]?$`
  are tied to legacy `get_current_time` / `weather` / `cc_*` tools that
  don't exist Day-1). Tier 0 may end up empty Day-1 — see Open
  question §4.

### Dead / abandoned modules

- `memory/cold/outcome_detector.py` (89 LOC) — explicitly marked
  DEPRECATED in source ("regex layer kept for historical reference
  only"). Already superseded by `nli_classifier.py`. DISCARD.
- `data/jarvis.db` (0 B) — empty file.
- `data/jarvis_memory.db` (0 B) — empty file. Stale.
- `overnight/_runner.log` (0 B) — empty.

### Architecturally important but doesn't fit clean classification — flag for Allen review

- **`auth/permission_manager.py` name collision.** It is *called* a
  permission manager but is actually a 71-line device-RBAC checker. Spec
  §3.4.10 Pre-action Gate also performs "permission checks" but with a
  totally different model (`caller_principal` + `risk_level` +
  `AuthorizationLease` + entity resolve). When a Day-1 implementer
  searches Legacy for "permission" they will find this file first; it
  may mislead them into thinking the Pre-action Gate concept already
  exists. **Recommendation**: in the Day-1 implementation, explicitly
  commit-log the bypass (`Legacy-bypass: auth/permission_manager.py —
  concept mismatch with spec §3.4.10 Pre-action Gate; legacy is
  device-RBAC, spec is caller_principal + lease`).
- **`core/llm.py` `_tracker` parameter.** Legacy LLM wraps every chat
  call in `self._tracker.record_success(component)` /
  `record_failure(component)`. Day-1 can pass `tracker=None` (Legacy
  supports this), but be aware: Stage 2 will want this tracker re-wired
  to Event Log as `health.check_completed` / `health.status_changed`
  events. Don't strip the `tracker` parameter from the constructor.
- **`core/llm.py` `chat_stream`** (line 900) supports streaming. Day-1
  ADR doesn't specify streaming vs non-streaming for CLI output. Bare
  `chat()` is enough for Day-1 acceptance, but if Allen wants the CLI to
  stream tokens, the streaming path is already there.
- **`core/llm.py` `x-grok-conv-id` sticky routing**. Lines 78–81 + 1604+
  set a process-stable UUID to pin the gateway load-balancer to one
  replica for cache-hit rates. This is xAI-specific. With Day-1 using
  OpenRouter→OpenAI-compat, this header may be ignored upstream — but
  preserving it is harmless and matches Legacy proven behavior. Keep.

## Day-1 build step references

Cross-referenced against ADR 0001 Build order.

- **Step 0 — Legacy scan**. This document. Done.
- **Step 1 — `pyproject.toml` ratchet**. No legacy equivalent. Legacy uses
  `pyproject.toml` + `requirements.txt` + `mise.toml`; ratchet is a
  fresh ruff ALL + mypy --strict config. Expect Legacy-bypass.
- **Step 2 — `constitution/` + `shared/`**. No legacy equivalent.
  `core/__init__.py` re-exports unrelated audio classes; no constitution
  module. Spec §3.2.1 + §3.5.2 defines C1–C6 + caller principals + lease
  — all new. **Legacy-bypass expected.**
- **Step 3 — `deployment/__init__.py` (paths + bootstrap)**. No clean
  Legacy equivalent. Legacy hardcodes `data/scheduler.db`,
  `data/memory/jarvis_memory.db` in many places. Day-1 wants `~/.jarvis/`
  per ADR `architecture-mac-only.md`. **Legacy-bypass expected.** Maybe
  borrow 5 lines of `Path.mkdir(parents=True, exist_ok=True)` patterns
  from `core/scheduler.py:27`.
- **Step 4 — `state/event_log.py`**. **Legacy-bypass mandatory.** Legacy
  has no append-only Event Log; `core/event_bus.py` is in-memory pub/sub,
  incompatible per spec §3.3.1. `memory/trace.py` is a 34-column trace
  table but doesn't enforce append-only and conflates many concerns
  (cost, latency, NLI). Build from scratch. Commit message:
  `Legacy-bypass: core/event_bus.py — conflicts with append-only Event
  Log requirement, spec §3.3.1`.
- **Step 5 — `state/projections.py`**. **Largely Legacy-bypass.**
  `memory/hot/conversation.py` has a sliding-window store but it's not a
  projection over events. **Spot-check during implementation**: borrow
  `memory/hot/assembler.py`'s `PromptBlock` shape if Situation Packet
  grows multi-block context. **Borrow** `core/tool_result.py`
  vocabulary for Claim/Evidence projection (this is the surprise — see
  §Surprises 1).
- **Step 6 — `execution/tools.py`**. **MANDATORY REUSE of
  `tools_v2/registry.py`** + `tools_v2/helpers.py`. Add 8-state
  ActionLifecycle (Legacy has no equivalent; spec §3.5.7 — Legacy-bypass
  for that part). Both stub tools (`spawn_worker`, `verify_diff`) are
  net-new — model them as canned-event emitters per ADR Stub strategy
  table.
- **Step 7 — `prompts/jarvis_v1.md` + `config/jarvis.yaml` (LLM section)**.
  **MANDATORY VERBATIM** from `prompts/phase1/v1.md` + `config.yaml`
  lines 277–301. Verified.
- **Step 8 — `decision/llm.py`**. **MANDATORY REUSE of `core/llm.py`.**
  Trim: imports of `core.personality` + `memory.hot.assembler`;
  `_personalize_system()` fallback (lines 1630–1654); all `prompt_context`
  kwargs (6 sites). Keep: provider switch, `_chat_anthropic`,
  `_chat_openai`, `chat_stream`, preset mechanism, metadata accessors
  (`last_metadata`, `last_finish_reason`, `last_cache_read_tokens`,
  `last_input_tokens`, `last_output_tokens` — these satisfy
  acceptance G1), sticky `x-grok-conv-id` UUID, tool-use loop iteration
  limit (`for _ in range(10)`). See Historical questions §3 for the
  prompt-context replacement design.
- **Step 9 — `decision/__init__.py` (packet + policy + intent + 3 gates +
  decide)**. **Largely Legacy-bypass.** Pre-action / Result Interpreter /
  Pre-emit Gates are new (spec §3.4.10 / §3.4.11 / §3.4.12). Tier 0
  regex: borrow anchor convention from `core/regex_router.py`, write
  inline (~3 patterns or fewer). **Result Interpreter borrows**
  `core/tool_result.py` vocabulary + claim_policy derivation (surprise
  reuse). Effective Policy Resolver and Situation Packet are new.
  `auth/permission_manager.py` is NOT a Pre-action Gate source — explicit
  Legacy-bypass.
- **Step 10 — `surface/cli.py` + `runtime/__init__.py` + `cli/__init__.py`**.
  `surface/cli.py` borrows **`core/response_channels.py`** (surprise reuse)
  for `<voice>` / `<document>` parsing. `runtime/__init__.py` borrows
  the *composition root pattern* from `jarvis.py` `JarvisApp.__init__`
  (single class, single config, owns cross-layer wiring) — **the pattern,
  not the contents**. `cli/__init__.py` is new (Legacy has no CLI surface
  — only voice + web).
- **Step 11 — `tests/canary/`**. No Legacy equivalent. Legacy tests are
  per-module pytest, no canary anti-bypass class. **Legacy-bypass
  expected.**
- **Step 12 — `tests/scenarios/test_flagship.py`**. No Legacy equivalent.
  Legacy `scripts/phase1_smoke.py` and `scripts/eval_runner.py` may
  inform the fixture-loading pattern, but the scenario is new.
- **Step 13 — `tests/scenarios/test_flagship_verify_fails.py`**. As
  above. No Legacy equivalent.

## Historical questions resolved by ADR 0001

1. **Identity / personality location.** `core/personality.py` (317 LOC)
   builds the system prompt — kernel block (`<xiaoyue_kernel>`),
   identity, time-of-day context, emotion context, situation context,
   NSFW addon. The Day-1 prompt `prompts/jarvis_v1.md` (verbatim) is a
   different identity ("Jarvis, Allen's local command center") and is
   self-contained. Spec §3.2 puts identity in **L1 Constitution**.
   Question: is `prompts/jarvis_v1.md` *the* identity source (so
   L1 Constitution emits it / references it), or does L1 Constitution
   hold a separate identity block that the prompt references? The two
   files have different voices ("Xiaoyue/小月" vs "Jarvis"); they cannot
   both be source-of-truth. **Resolved by ADR 0001:** architectural
   identity remains L1 Constitution plus durable L2 state. Day-1 copies
   `prompts/jarvis_v1.md` as the L3 prompt text asset whose prompt copy
   names Jarvis; it is not the identity source of truth.

2. **`auth/permission_manager.py` naming clash.** As flagged above, the
   Legacy file is device-RBAC, the spec concept is `caller_principal` +
   `AuthorizationLease`. **Recommend explicit Legacy-bypass commit** when
   Pre-action Gate ships in Step 9. Confirming this so the Day-1
   implementer doesn't accidentally adapt the wrong concept.

3. **`core/llm.py` `PromptContext` / `_personalize_system` removal —
   replacement strategy?** Six entry points (`chat`, `_chat_anthropic`,
   `_chat_openai`, `chat_stream`, and two helpers) accept
   `prompt_context: PromptContext | None = None`. When None, they fall
   back to `_personalize_system(user_name, user_role, user_emotion)`
   which calls `build_identity_block()` + `build_situation_block()` from
   `core/personality`. Day-1 wants neither path — it wants to pass the
   `prompts/jarvis_v1.md` content + maybe `conversation_history`. Two
   options:
   - (a) **Strip both** — remove `prompt_context` parameter, remove
     `_personalize_system`, add a `system: str` parameter. Cleaner, but
     touches every signature.
   - (b) **Strip dependencies only** — keep `prompt_context` parameter
     but redefine `PromptContext` in `jarvis/shared/__init__.py` as a
     thin local type (`@dataclass class PromptContext: system: str`),
     keep `_personalize_system` as a one-line `return system_default`
     fallback. Less churn, structurally weirder.
   Recommend (a). **Resolved by ADR 0001: use option (a) before Step 8.**

4. **Tier 0 regex set for Day-1.** ADR initial map says "3 patterns
   Day-1". Looking at Legacy `core/regex_router.py`'s 21 patterns, none
   of them fit the flagship scenario ("昨天那个 task 给 codex 跑一下…")
   — that utterance is Tier 2 LLM territory. Day-1 Tier 0 may end up
   empty (no patterns fire), with all routing going to Tier 2.
   Question: keep Tier 0 as an empty scaffold (one no-op `match()` that
   always returns None) for the architecture, or only build Tier 0 when
   the first deterministic shortcut is added? ADR module map implies the
   former (Tier 0 is wired even if empty) — confirming. **Resolved by
   ADR 0001:** Day-1 ships an empty scaffold (`match()` returns None).

5. **`core/llm.py` `chat_stream` Day-1 use?** Legacy has full streaming
   support. Day-1 ADR says "CLI prints response" — non-streaming is
   simpler. But streaming would surface tokens to stdout sooner.
   Confirm: bare `chat()` for Day-1, defer `chat_stream` wiring to
   Stage 2? **Resolved by ADR 0001:** bare `chat()` for Day-1;
   `chat_stream` preserved but unwired until Stage 2.

6. **`core/llm.py` `_tracker` parameter — Day-1 = None?** Legacy LLM
   constructor signature is `__init__(self, config, tracker=None)`.
   Day-1 will pass `tracker=None`. Confirming this is the intent (vs
   building a no-op tracker stub) so the L4 health system can wire in
   cleanly Stage 2. **Resolved by ADR 0001:** pass `tracker=None`.

7. **`data/pricing.json` for cost ceiling?** Earlier drafts considered
   USD cost artifacts. **Resolved by ADR 0001:** Day-1 records token
   use at `tests/_artifacts/llm_use_<ts>.json`; USD cost tracking is
   Stage 2.
