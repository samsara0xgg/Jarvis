# Goal: voice-model-paths-from-config

## Goal

Twenty-eight realtime voice values that are hard-coded Python constants today
become keys of the top-level `realtime:` block, parsed once into typed objects,
and the daemon logs one startup record naming every resolved value plus which
input owner it spawned and why. Setting nothing keeps today's behavior byte for
byte.

Two of the twenty-eight are artifact locations — `realtime.sensevoice_dir` and
`realtime.silero_vad_path` — which additionally stop depending on the process's
current working directory: a configured relative path is anchored at the config
file's own directory.

This is the largest slice of ADR-0006 §5's unimplemented decision
(`docs/adr/0006-full-duplex-voice-session.md:716`): "New configuration is parsed
into a typed object; no realtime constant remains hard-coded in
`inherent_loop.py`." It does not finish that decision — see
"What `:716` still owes after this card" under Boundaries for exactly what is
left and why.

## Why

### The twenty-eight

`:716` is a ruling, not an idea: it was approved and implemented zero times.
The cost is not hypothetical. Every value below is one a deployment has a
legitimate reason to move — a different microphone needs a different wake
threshold, a noisier room needs a different VAD floor, a slower network needs
longer MiniMax deadlines, a different region needs a different endpoint — and
today moving any of them means editing Python and restarting from a checkout.
The owner's own machine already needs a non-default value for two of them
(the artifact paths), which is why those two go with a live consequence
attached.

The card ships them as one slice rather than twenty-eight because they share
exactly one parser, one startup record, one YAML block and one set of expensive
daemon restarts on a machine where a restart costs the owner his microphone.

### The two paths, and the record

Two further facts that only look unrelated.

**The process's cwd is load-bearing and nothing can override it.** The two
artifact locations are relative module constants, the one production caller never
overrides them, and no config key or CLI flag reaches them — so the daemon finds
its models only when started from a directory that happens to have a `data/`
subtree. The owner's live setup satisfies that with a symlink chain whose last
link points into `Projects/jarvis-legacy/data/silero_vad.onnx` — a retired
repository — through a disposable checkout.

These two are the only values in the set with a live consequence today, and the
only ones whose resolution needs a fact — the config file's own directory — that
exists nowhere but `bootstrap_runtime_app`. That is why they, alone of the
twenty-eight, are resolved at the composition root and carried on
`JarvisRuntime` rather than parsed inside the daemon module.

**Startup does not say what it spawned.** The legacy wake path and the
single-ingress path each construct a `voice_wake.WakeEngine`, which logs the same
line either way. Nothing distinguishes them. In one session that ambiguity cost
two agents and their coordinator a wrong conclusion ("the feature downgraded"),
reversed only by grepping for the `AudioIngress(` construction site.

Both are the same missing thing: startup does not report what it resolved. One
record fixes both, and the cwd acceptance needs that record anyway — a check that
the daemon resolved models correctly from an arbitrary cwd has no observable
without it.

## Current behavior

- **`serve_inherent`'s two model kwargs default to relative paths.**
  `_DEFAULT_SENSEVOICE_DIR = Path("data/sensevoice-small-int8")` and
  `_DEFAULT_SILERO_PATH = Path("data/silero_vad.onnx")`
  (`jarvis/runtime/inherent_loop.py:290-291`) are the defaults of
  `sensevoice_dir` / `silero_path` in the signature at
  `jarvis/runtime/inherent_loop.py:3623-3624`. Relative `Path`s resolve against
  `os.getcwd()` at `.exists()` time.
- **The only production caller passes neither.** `jarvis/cli/__init__.py:736-741`
  calls `serve_inherent(runtime, host=..., port=..., lock_path=...)`. Nothing
  else in `jarvis/` calls it.
- **No CLI flag exists.** The `serve` parser (`jarvis/cli/__init__.py:668-712`)
  defines exactly `--host`, `--port`, `--runtime-root`, `--config`, `--prompt`,
  `--force-manual`.
- **No config key is read.** `config/jarvis.yaml`'s top-level keys are exactly
  `llm`, `supervisor`, `observer`, `tools`, `confirmation`, `realtime`; nothing
  reads an artifact path from any of them. `_load_full_config`
  (`jarvis/runtime/__init__.py:518-525`) parses exactly one file with no
  layering, so a key absent from the operator's config falls back to a Python
  constant, never to the repo's YAML.
- **`realtime:` already carries ungated flat keys of exactly this kind.**
  `_build_tts_pipeline` reads `realtime.tts_volume` (`inherent_loop.py:1873`)
  and `realtime.output_device` (`:1889`) straight off the `realtime` mapping;
  `realtime.enabled` is consulted separately at `:1891` for
  `streaming_requested` only. Both are device/artifact facts that apply whatever
  wave is on, which is the same category as the two model paths.
- **The legacy wake path reads no config at all.** `_spawn_wake_listener`
  (`jarvis/runtime/inherent_loop.py:2036-2043`) takes `pipeline`, `broadcaster`,
  `silero_path`, `tts`, `ducker` — it is never handed `runtime`, so it cannot
  read a key under `realtime.single_audio_ingress` even in principle. Both owners
  get `silero_path` from the single `_spawn_voice_input_owners` call site
  (`:2809-2843`), and pre-flight (`:311-340`) and `_build_voice_pipeline`
  (`:1803`) run once in `serve_inherent` before either owner exists.
- **These are the only cwd-dependent paths in the boot.** `prompt_path` is
  `repo_root / prompts/jarvis_v1.md` (`jarvis/runtime/__init__.py:1411`,
  `:174`) and the pricing table is `repo_root / "data" / "pricing.json"`
  (`:1492`), both anchored at `repo_root` = `config_path.resolve().parent.parent`
  (`:1412`), which is cwd-independent. The composition root's own comment says
  "we never look at `os.getcwd()`" (`:1402-1404`) — true of everything it
  resolves, and false of the two paths it never resolves.
- **The other twenty-six values have no reader at all.** They are module
  constants and call-site literals, listed with their live sites under Target
  behavior. Three sub-facts decide how each is wired:
  - `MiniMaxWSClient.__init__` (`jarvis/surface/voice_tts.py:1855-1881`) already
    takes `voice`, `model`, `primary_endpoint`, `fallback_endpoint`, `volume`,
    `sample_rate_in`, `sample_rate_out`, `connect_timeout_s` and
    `total_timeout_s` as keyword arguments; `_new_provider()`
    (`jarvis/runtime/inherent_loop.py:1876-1882`) passes four of the nine. For
    those the work is config → existing parameter, and `realtime.tts_volume`
    (`:1873`) is the shipped template.
  - The other four MiniMax timeouts are NOT constructor parameters. They are
    class attributes read as `self._TASK_START_TIMEOUT` (`voice_tts.py:2065`),
    `self._FIRST_CHUNK_TIMEOUT` / `self._BETWEEN_CHUNK_TIMEOUT` (`:1916-1917`,
    `:2084`) and `self._SESSION_CLOSE_TIMEOUT` (`:2039`). Each needs a new
    constructor parameter before a key can reach it.
  - `_CONNECT_TIMEOUT` (`voice_tts.py:1848`) is **dead**. Nothing reads it:
    `connect_timeout_s` defaults to a duplicated literal `3.0` (`:1866`) and the
    live value is `self._connect_timeout` (`:1878`). See Rejected approaches.
- **`_MODE_THRESHOLDS` is a module-global the constructor cannot bypass.**
  `SileroVad.__init__` binds `self._t = _MODE_THRESHOLDS[mode]`
  (`jarvis/surface/voice_audio.py:140`) and `set_mode` re-reads the same table
  through the `thresholds` classmethod (`:190`), so a per-instance override that
  did not also cover `set_mode` would be silently discarded the first time the
  session switched profiles (`voice_session.py:1491` is the other reader).
- **Both input owners construct the same two objects from the same constants.**
  `SileroVad(mode="record", model_path=silero_path)` at
  `jarvis/runtime/inherent_loop.py:2082` (legacy wake) and `:2743` (single
  ingress); `_DEFAULT_WAKE_THRESHOLD` at `:2095` and `:2751`. A key that reaches
  only one of them is the trap this card's Boundaries already names.
- **Pre-flight is soft and stays soft.** `_voice_models_preflight`
  (`jarvis/runtime/inherent_loop.py:311-340`) checks
  `sensevoice_dir/model.int8.onnx`, `sensevoice_dir/tokens.txt` and
  `silero_path` with `.exists()` and returns `(ok, missing)`; on `not ok`
  `serve_inherent` logs one ERROR (`:3769-3773`) and continues text-only.
- **The wake log line cannot discriminate.**
  `"wake: openwakeword engine started (model=%s, framework=%s)"`
  (`jarvis/surface/voice_wake.py:193-197`) is emitted by `WakeEngine.start()`,
  constructed by legacy wake at `jarvis/runtime/inherent_loop.py:2079` and by
  single ingress at `:2676`. The sole discriminator in the log is the absence
  of anything: `AudioIngress(` is constructed at `:2738` and says nothing.
- **The activation reason exists and is dropped on success.**
  `_single_ingress_activation` (`jarvis/runtime/inherent_loop.py:2571-2637`)
  returns a named `reason` for every outcome (`feature_disabled`,
  `realtime_parent_disabled`, `unsupported_input_backend`,
  `invalid_input_config:<exc>`, `wave1_capability_missing`,
  `wave2_streaming_output_missing`, `validated`). Every failing branch of
  `_spawn_single_ingress_session` records it — `record_realtime_trace(
  "audio_input_activation_downgraded", reason=...)` at `:2664-2669`, `:2775-2781`,
  `:2798-2803`. The success return at `:2806` records nothing and logs nothing.
- **`_spawn_voice_input_owners` (`:2809-2843`) already knows the answer** — it
  holds `duplex_session`, `wake_listener` and `single_ingress_attempted` — and
  reports none of it.

## Target behavior

- `realtime.sensevoice_dir` and `realtime.silero_vad_path` — flat children of the
  top-level `realtime:` block, siblings of the existing `output_device` and
  `tts_volume` — when present in the config the daemon was pointed at, decide
  where the daemon looks for the SenseVoice directory and the Silero ONNX file.
  Both are `~`-expanded. Like `output_device` (`inherent_loop.py:1889`) and
  `tts_volume` (`:1873`), they are read regardless of `realtime.enabled`: the
  artifacts load on the legacy path too.
- A configured value that is still relative after `~`-expansion is resolved
  against **the directory containing the config file that supplied it**, then
  made absolute. Where the process was started never affects a configured path.
- **Absent keys resolve exactly as they do today**: the unchanged relative
  constants `Path("data/sensevoice-small-int8")` / `Path("data/silero_vad.onnx")`,
  still interpreted against cwd. This is deliberate — see Boundaries.
- A malformed value (non-string, empty, whitespace) degrades to the same
  constant with one warning and never fails boot, matching `_obsidian_vault_root`
  (`jarvis/runtime/__init__.py:931-950`).
- Pre-flight semantics are unchanged: a configured-but-missing artifact is
  exactly as soft as an unconfigured-and-missing one — one ERROR, text path
  keeps running, PTT ASR still 501s only when the pipeline is absent.
- Exactly one new startup record, emitted once per boot on **every** path
  (models present, models missing, construction failed), naming:
  the resolved absolute SenseVoice directory, the resolved absolute Silero path,
  whether pre-flight passed, which input owner was spawned
  (`single_ingress` | `legacy_wake` | `none`), and the reason for that outcome.
- The reason value on the no-owner / legacy paths is the already-computed
  `_SingleIngressActivation.reason`, plus the two outcomes that never reach it:
  the `JARVIS_VOICE_DISABLE_WAKE=1` skip (`:3787-3790`) and the missing-models
  skip.

### The twenty-eight keys, as they appear in `config/jarvis.yaml`

Every default below is today's constant, unchanged. **An absent key resolves to
exactly that value**, so a config that sets none of them — the owner's — is
byte-identical to today.

**Flat children of `realtime:`** (siblings of `output_device` / `tts_volume`) —
sixteen. Each is a fact that applies whatever wave is on:

| key | default | today's site |
| --- | --- | --- |
| `sensevoice_dir` | `data/sensevoice-small-int8` | `inherent_loop.py:290` |
| `silero_vad_path` | `data/silero_vad.onnx` | `:291` |
| `wake_threshold` | `0.5` | `_DEFAULT_WAKE_THRESHOLD` `:294` |
| `wake_join_timeout_s` | `2.0` | `_WAKE_JOIN_TIMEOUT_S` `:308` |
| `tts_voice` | `Chinese (Mandarin)_ExplorativeGirl` | `voice_tts.py:1859` |
| `tts_model` | `speech-2.8-turbo` | `voice_tts.py:1862` |
| `tts_primary_endpoint` | `https://api-uw.minimax.io` | `voice_tts.py:1860` |
| `tts_fallback_endpoint` | `https://api.minimax.chat` | `voice_tts.py:1861` |
| `tts_sample_rate_in_hz` | `32000` | literal at `inherent_loop.py:1879` |
| `tts_ring_seconds` | `30.0` | literal at `inherent_loop.py:1966` |
| `tts_connect_timeout_s` | `3.0` | `voice_tts.py:1848` / `:1866` |
| `tts_task_start_timeout_s` | `3.0` | `_TASK_START_TIMEOUT` `:1849` |
| `tts_first_chunk_timeout_s` | `8.0` | `_FIRST_CHUNK_TIMEOUT` `:1850` |
| `tts_between_chunk_timeout_s` | `5.0` | `_BETWEEN_CHUNK_TIMEOUT` `:1851` |
| `tts_total_timeout_s` | `30.0` | `_TOTAL_TIMEOUT` `:1852` |
| `tts_session_close_timeout_s` | `1.0` | `_SESSION_CLOSE_TIMEOUT` `:1853` |

**`realtime.vad:`** — a flat child holding the two `_MODE_THRESHOLDS` profiles
(`voice_audio.py:78-81`), ten keys. Both owners construct the `record` profile;
`tts` is inert while `barge_in` is off and is exposed anyway, so the operator who
turns barge-in on has one place to tune rather than a Python edit:

    realtime:
      vad:
        record: {prob_threshold: 0.4, db_threshold: -45.0,
                 smoothing_window: 5, required_hits: 3, required_misses: 24}
        tts:    {prob_threshold: 0.5, db_threshold: -22.0,
                 smoothing_window: 5, required_hits: 3, required_misses: 24}

**`realtime.streaming_output.ring_seconds`** = `2.0` — the Wave-2 streaming
player's ring (`inherent_loop.py:1912`). Wave-scoped, so it joins
`StreamingMediaConfig` and its existing parser.

**`realtime.single_audio_ingress.device_miss_limit`** = `3` —
`_DEFAULT_DEVICE_MISS_LIMIT` (`voice_audio.py:53`), read only by `AudioIngress`
(`:1645`, `:1762`), which exists only in that wave. It joins `AudioIngressConfig`
and its existing parser. This is the one key legitimately placed inside
`single_audio_ingress`, and it is legitimate for the exact reason the two model
paths are not: its only consumer is the object that block configures.

### `_DEFAULT_TTS_SAMPLE_RATE_HZ` gets no key; it is deleted

`_DEFAULT_TTS_SAMPLE_RATE_HZ = 48000` (`inherent_loop.py:300`) and
`realtime.streaming_output.canonical_sample_rate_hz` (default `48000`,
`voice_media.py:228`, set explicitly to `48000` in the shipped config) are one
value written twice. Today `_build_tts_pipeline` does not reconcile them — it
*asserts* them equal at `:1907`:

    and media_config.canonical_sample_rate_hz == _DEFAULT_TTS_SAMPLE_RATE_HZ

**What breaks today if the two disagree:** nothing loudly. `streaming_capable`
goes `False`, so a fully-enabled Wave-2 rollout silently downgrades to the legacy
TTS path with one generic warning ("capability/config validation failed") that
never names the sample rate as the cause. The operator who set
`canonical_sample_rate_hz: 44100` gets a working daemon that has quietly turned
off the feature they were enabling.

The constant is deleted. The player rate, the MiniMax `sample_rate_out` and the
streaming canonical rate all become `media_config.canonical_sample_rate_hz`
(falling back to `StreamingMediaConfig()`'s own default when the streaming block
failed to parse), and the equality term at `:1907` goes with it. One value, one
source, and the disagreement it guarded against can no longer be constructed.

### Where each value is parsed

- The two paths: `bootstrap_runtime_app`, the only scope holding `config_path`.
- `streaming_output.ring_seconds` and `single_audio_ingress.device_miss_limit`:
  the existing L5 parsers that already own those blocks.
- The other twenty-four: **one** frozen `_VoiceKnobs` object parsed once in
  `serve_inherent` from `runtime.config["realtime"]` and threaded to
  `_build_tts_pipeline`, `_shutdown_wake` and both input owners. This is
  `:716`'s "typed object". It lives in `inherent_loop.py` rather than on
  `JarvisRuntime` because it holds `voice_audio.VadThresholds`, and importing
  `jarvis.surface.voice_audio` into `jarvis/runtime/__init__.py` would pull
  numpy into every `jarvis` CLI invocation — measured: `import jarvis.runtime`
  loads neither numpy nor `voice_audio` today.

### Malformed values

Two postures, each inherited from the owner of the block, not chosen per key:

- Flat `realtime.*` and `realtime.vad.*` are read at the composition root,
  outside any downgrade boundary. A malformed value degrades to the constant
  with one warning and never fails boot — the `_obsidian_vault_root` /
  `_positive_float` posture of `jarvis/runtime/__init__.py:528-536`.
- `streaming_output.ring_seconds` and `single_audio_ingress.device_miss_limit`
  join parsers that already `raise ValueError` naming the key, which the
  existing callers already catch into a named downgrade
  (`invalid_input_config:<msg>` / "realtime.streaming_output config invalid").
  Changing that posture for one field would be the inconsistency.

## Affected contracts and files

- L6/L3 boundary `jarvis/runtime/__init__.py` — a reader beside
  `_obsidian_vault_root` (`:931-950`) that takes the parsed config **and** the
  config file's path and returns the two resolved absolute `Path`s; called from
  `bootstrap_runtime_app`, which is the only place that holds `config_path`
  (`:1405-1412`).
- L6 `jarvis/runtime/__init__.py:424-445` (`JarvisRuntime`) — two new frozen
  fields carrying the resolved paths, defaulted to the same two relative
  constants so hand-assembled test runtimes are byte-identical.
- L6 `jarvis/runtime/inherent_loop.py:3616-3624` (`serve_inherent`) — the
  `sensevoice_dir` / `silero_path` kwargs are **removed**; the body reads the
  runtime fields. No caller in `jarvis/` or `tests/` passes either (the
  `sensevoice_dir=` / `silero_path=` hits in `tests/` are all calls to
  `_voice_models_preflight`, `_spawn_wake_listener` and
  `_spawn_single_ingress_session`, which keep their kwargs).
- L6 `jarvis/runtime/inherent_loop.py:2137-2144` (`_VoiceInputOwners`) and
  `:2809-2843` (`_spawn_voice_input_owners`) — carry the activation reason out so the record
  can name it.
- L6 `jarvis/runtime/inherent_loop.py` — a new frozen `_VoiceKnobs` and its
  reader; `_build_tts_pipeline` (`:1834`), `_shutdown_wake` (`:3422`),
  `_spawn_wake_listener` (`:2036`), `_spawn_single_ingress_session` (`:2640`)
  and `_spawn_voice_input_owners` (`:2809`) each gain **one defaulted
  keyword-only parameter** carrying it. Defaulted, so every existing caller —
  production and the eight tests under `tests/integration/` that construct these
  helpers directly — compiles and behaves unchanged.
- L6 `jarvis/runtime/inherent_loop.py:294`, `:300`, `:308` — `_DEFAULT_WAKE_THRESHOLD`
  and `_WAKE_JOIN_TIMEOUT_S` keep their values as the absent-key fallback;
  `_DEFAULT_TTS_SAMPLE_RATE_HZ` is deleted.
- L5 `jarvis/surface/voice_tts.py:1855-1881` (`MiniMaxWSClient.__init__`) — four
  new keyword-only timeout parameters for the four class attributes that are not
  parameters today, and `connect_timeout_s`'s duplicated `3.0` literal repointed
  at `_CONNECT_TIMEOUT` so that constant stops being dead. The five read sites
  (`:1916-1917`, `:2039`, `:2065`, `:2084`) switch from `self._SCREAMING` to the
  instance value.
- L5 `jarvis/surface/voice_audio.py:109-190` (`SileroVad`) — one optional
  `thresholds: Mapping[str, VadThresholds] | None` parameter, stored as a
  per-instance table that `set_mode` also consults, so an override survives a
  mid-utterance profile switch. `_MODE_THRESHOLDS` stays the default table and
  the `thresholds` classmethod keeps its signature (`voice_session.py:1491` is
  an unmodified caller).
- L5 `jarvis/surface/voice_audio.py:441-461` (`AudioIngressConfig`) and
  `:2522` (its parser) — one `device_miss_limit: int = 3` field; the two
  `_DEFAULT_DEVICE_MISS_LIMIT` reads (`:1645`, `:1762`) become
  `self._config.device_miss_limit` and the constant is deleted.
- L5 `jarvis/surface/voice_media.py:225-237` (`StreamingMediaConfig`) and
  `:3309` (its parser) — one `ring_seconds: float = 2.0` field.
- L5 `jarvis/surface/voice_tts.py:447-452` — the `_DECLICK_SAMPLES` comment
  cites `inherent_loop._DEFAULT_TTS_SAMPLE_RATE_HZ`, which this card deletes;
  it must name the streaming canonical rate instead.
- L6 `jarvis/runtime/inherent_loop.py` — the one new startup record, placed
  after the whole voice-construction block (which ends at `:3812`) so it is
  reached on every path.
- `config/jarvis.yaml` — the two keys added **commented out**, inside the
  existing `realtime:` block next to `output_device` / `tts_volume` (`:150-165`),
  with the shipped defaults and the anchoring rule stated. Live uncommented
  values would be anchored at `<repo>/config/`, i.e. `<repo>/config/data/...`,
  which is not where the artifacts are; and because there is no config layering,
  a value here never reaches an operator running `--config` elsewhere.
- `jarvis/runtime/inherent_loop.py:286-291` — the constants keep their meaning;
  their comment gains the sentence that they are the absent-key fallback, not
  the only source.

## Boundaries and non-goals

- Layers that may change: L6 (`jarvis/runtime/`), L5 (`voice_tts.py`,
  `voice_audio.py`, `voice_media.py` — each gains parameters or config fields
  for values it already owns; no L5 module reads YAML), plus
  `config/jarvis.yaml`. `jarvis/surface/voice_wake.py:193` stays exactly as it
  is: the record, not the wake line, is what discriminates the two owners.
- **Must not change: what an absent key resolves to.** The owner's live config
  has neither key. Anything that makes the absent-key case resolve somewhere new
  — including the tempting `repo_root / "data" / ...` — silently relocates a
  running system's models. The escape hatch is the deliverable; the default's
  meaning is not. If the implementation cannot preserve this, STOP AND REPORT.
- Must not change: pre-flight softness. A missing artifact stays one ERROR plus
  a live text path. Never a hard boot failure, never a raise.
- Must not change: the existing `audio_input_activation_downgraded` trace points
  or their reason strings — the downgrade family is already correct.
- **Must not move these keys under `realtime.single_audio_ingress` later.** That
  block is read only by `_single_ingress_activation` (`inherent_loop.py:2571`);
  `_spawn_wake_listener` (`:2036-2043`) is never handed `runtime` and cannot read
  it. A key placed there is honoured by one of the two input owners. These two
  paths are immune to that trap by construction — resolved once in
  `bootstrap_runtime_app` and threaded to both owners from the single
  `_spawn_voice_input_owners` call site — but the placement must not invite the
  next reader to assume the block is a general home for voice config.
- **The naming convention, applied throughout and inherited by the next
  slice:** a fact that applies whatever wave is on is a flat child of `realtime:`
  alongside `output_device` / `tts_volume`; a fact that only means something
  inside one wave belongs in that wave's sub-block (`streaming_output`,
  `single_audio_ingress`), **and must then be wired into every owner that
  consumes it**. The second clause is what places `wake_threshold` and the VAD
  profiles flat: both input owners construct them.
- **Non-goal: the legacy wake path's capture bounds.**
  `_DEFAULT_CAPTURE_MAX_DURATION_S` (`:295`) and `_DEFAULT_CAPTURE_MIN_VOICED_S`
  (`:296`) stay hard-coded. The single-ingress owner already has
  `max_utterance_s` / `min_voiced_s` (`RealtimeInputSessionConfig`), which is the
  path the owner runs; a second pair of keys reaching only the legacy owner would
  be the honoured-by-one-owner trap, in the direction the convention forbids.
- **What `:716` still owes after this card.** Say it here so the next slice does
  not rediscover it. `:716` says *no* realtime constant remains hard-coded in
  `inherent_loop.py`. After this card three remain, each deliberately:
  `_DEFAULT_CAPTURE_MAX_DURATION_S` and `_DEFAULT_CAPTURE_MIN_VOICED_S` (above),
  and `_WAKE_SAMPLE_RATE_HZ` / `_WAKE_FRAME_SAMPLES` (`:306-307`), which are
  external model input contracts and never become keys. So this card satisfies
  `:716` **partially**: everything it leaves is either a ruled-out external
  contract or the legacy capture pair, whose real fix is retiring the legacy wake
  owner, not configuring it.
- **Non-goal: exposing a knob whose only legal value is its default.** ADR-0008
  (`docs/adr/0008-real-time-response-streaming.md:1005`) — "a knob whose only
  legal value is its default is not configuration." Applied to all thirty
  candidates, the filter killed three:
  - `_DEFAULT_TTS_SAMPLE_RATE_HZ` — not because its value is fixed but because
    a *second* name for `streaming_output.canonical_sample_rate_hz` is not
    configuration either. Deleted rather than exposed (see Target behavior).
  - An "anchor mode" key for the two paths (cwd vs config-dir vs repo-root):
    one usable setting.
  - A "strict pre-flight" key turning a missing artifact into a boot failure:
    one usable setting, and it contradicts ADR-0005 §12.
  The filter did **not** kill `_CONNECT_TIMEOUT`, which fails a different test —
  it is dead, not fixed. The key is wired to the live `connect_timeout_s`
  parameter and the dead constant becomes that parameter's default. See Rejected
  approaches.
- Non-goal: reviving the `voice.*` namespace of ADR-0005 §9. It is superseded
  (see Rejected approaches) and none of its sixteen rows was ever implemented.
- Non-goal: a CLI flag. Under the LaunchAgent the daemon's argv is pinned to
  `["<interp>", "-m", "jarvis", "serve"]` (`jarvis/deployment/launchd.py:273`)
  with `WorkingDirectory` set to the install-time repo root (`:274`). A flag no
  operator can reach in the deployment that actually runs is not a fix.
- Non-goal: touching the owner's overlay, his symlink farm, or his running
  daemon. This card makes the symlink farm *unnecessary*; dismantling it is his
  call, on his next restart.
- Non-goal: config layering, schema validation, or a config-migration path.

## Rejected approaches

- **Anchor a relative configured path at `repo_root`.** `repo_root` is itself a
  guess — `config_path.resolve().parent.parent`
  (`jarvis/runtime/__init__.py:1412`) — correct only for a config exactly two
  levels deep. Anchoring an operator's hand-written path to a derived guess
  compounds the guess. The config file's own directory is the anchor the
  operator chose explicitly with `--config`.
- **Anchor at cwd (i.e. just `.resolve()` the configured value).** That is the
  defect, re-shipped with a key in front of it.
- **Change the absent-key fallback to `repo_root / "data" / ...`.** Under
  launchd it is a no-op (`WorkingDirectory` is the repo root already), but for
  the owner's manual overlay start it silently re-points a live system's model
  lookup on evidence nobody verified. Ship the escape hatch; do not move the
  default.
- **ADR-0005 §9's `voice.asr.sensevoice_model_dir` / `voice.vad.model_path`.**
  This card's first draft chose these on the ground that the ADR had already
  named them. It is the wrong document. ADR-0006 §5
  (`docs/adr/0006-full-duplex-voice-session.md:718`) states: "Canonical
  configuration is the top-level `realtime:` block in `config/jarvis.yaml`, a
  sibling of `llm`/`supervisor`/`observer`/`tools`/`confirmation`; **there is no
  `voice:` namespace**." That sentence is later (ADR-0006 `**Status:** Approved
  (2026-08-31, Allen)` vs ADR-0005 `**Status:** Accepted`, undated), it decides
  the namespace question head-on rather than in passing, and the shipped file
  agrees with it — `config/jarvis.yaml`'s top-level keys are exactly the six
  0006 lists, and no `voice:` block has ever existed. ADR-0005 §9's table is a
  16-row proposal that was never implemented in any part; its namespace is dead.
  Implementing two of its rows would have resurrected a namespace a later
  approved ADR explicitly negates.
- **A new top-level `voice.models.*` or any other new top-level block.** Same
  sentence forbids it, and more directly: `realtime:` is named as *the* canonical
  block for this subsystem.
- **Put the keys under `realtime.single_audio_ingress`.** Two independent
  reasons. Semantically that block is gated by `realtime.enabled` while the
  artifacts load with every realtime flag off, so the name would lie about who
  reads the value. Mechanically it is the trap described under Boundaries:
  `_spawn_wake_listener` never receives `runtime` and cannot read that block, so
  a key placed there is honoured by one input owner and silently ignored by the
  other. Flat children of `realtime:` avoid both.
- **Keep `serve_inherent`'s two kwargs alongside the new runtime fields.** Two
  sources for one value that can disagree, with the kwarg silently winning. No
  caller passes them.
- **Fix the input-owner ambiguity by changing `voice_wake.py:193`.** The
  `WakeEngine` is constructed identically by both owners
  (`inherent_loop.py:2079`, `:2676`); no field visible to it discriminates. The
  record belongs at the composition root that made the choice.
- **Add a matching `audio_input_activation_selected` trace point.** The trace
  family's success sibling is genuinely missing, but the log line is the
  acceptance observable and nothing consumes the JSONL sink today. Add it when a
  trace-reading harness needs it.
- **A `realtime.tts:` sub-block for the eleven MiniMax keys.** Prettier, and
  wrong: `realtime.tts_volume` is already shipped flat and read at
  `inherent_loop.py:1873`. Moving it into a new block breaks a key an operator
  may already set, which the additive mandate forbids; leaving it out gives TTS
  config two homes and guarantees the next reader puts a key in the wrong one.
  Eleven `tts_*` siblings of the existing `tts_volume` is one naming rule and no
  migration.
- **Expose `_CONNECT_TIMEOUT` as a key that sets the class attribute.** It would
  be a key with no effect: nothing reads `_CONNECT_TIMEOUT`. The live value is
  `self._connect_timeout`, assigned from the `connect_timeout_s` parameter whose
  default is a separately-typed `3.0` literal. `realtime.tts_connect_timeout_s`
  is wired to the parameter, and the parameter's default is repointed at the
  constant so the two can no longer drift. Shipping the key without noticing
  this would have produced the one thing worse than a hard-coded constant: a
  documented knob that silently does nothing.
- **Leave `_spawn_wake_listener`'s parameter list alone.** Tempting — it is the
  legacy owner and the owner runs single ingress. But `wake_threshold` and the
  VAD profiles are flat `realtime:` keys precisely because *both* owners
  construct them, and a flat key honoured by one owner is the trap this card's
  Boundaries names. Resolved by **adding** defaulted keyword-only parameters
  rather than changing the existing ones: no existing caller changes, the
  legacy owner honours the config, and the function still never receives
  `runtime` — so the structural argument that `realtime.single_audio_ingress` is
  unreachable from it survives intact, which is the property that matters.
- **Hand `_spawn_wake_listener` the `JarvisRuntime` instead.** One parameter
  instead of one object, and it would destroy that argument: the block would
  become reachable, and the next reader would put a shared key there.
- **Put `_VoiceKnobs` on `JarvisRuntime` beside the two paths.** One typed
  object instead of two, but it holds `voice_audio.VadThresholds`, so
  `jarvis/runtime/__init__.py` would import `jarvis.surface.voice_audio` and
  pull numpy into every `jarvis` CLI invocation — measured absent today. The
  paths cannot move the other way (they need `config_path`), so the seam is
  forced; it is drawn where the import cost is.
- **Validate that `streaming_output.canonical_sample_rate_hz` equals the player
  rate.** That is what `:1907` does today, and the two values it compares are
  the same fact written twice. Deleting one is smaller than validating both.
- **Split the input-owner record into its own card.** Both defects are the same
  omission, both land on one new log statement at one site, and the cwd
  acceptance requires that statement to exist regardless. Two cards would edit
  the same new line and cost two daemon restarts on a machine where a restart is
  expensive. Folded in, with the scope bounded to exactly one new record.

## Acceptance evidence

Every check below names the artifact it asserts on. None of them is a unit test.

### The startup record, which is most of the observable

One `LOGGER.info` line per boot, prefixed `voice startup config:` and followed by
one JSON object (`json.dumps(..., sort_keys=True)`) naming every resolved value
`_VoiceKnobs` and the two paths hold, plus `models_ok`, `input_owner` and
`reason`. JSON rather than `key=value` because there are twenty-nine fields and
a `key=value` line of that width is not readable; and because every check below
reads one field out of it with `jq`, which a flat line would not support.

### Run constraints — every daemon start below

    env -u MINIMAX_API_KEY JARVIS_VOICE_DISABLE_WAKE=1 <interp> -m jarvis serve \
      --config <scratch>.yaml --force-manual --port 8011 \
      --runtime-root /tmp/jarvis-voicecfg-root

**`env -u MINIMAX_API_KEY` is a safety requirement, not a convenience.** With the
key present `_build_tts_pipeline` opens a PortAudio `OutputStream` on the system
default output — the owner's speakers, which he is listening to — and any turn
that reaches `surface.response_emitted` speaks out loud. `--force-manual` does
NOT prevent this: it only bypasses the launchd-installed guard
(`jarvis/cli/__init__.py:714`). Without the key `_build_tts_pipeline` returns
`None` at `:1857` before constructing anything, so no audio device is ever
opened. The voice pipeline (ASR) is built at `:3776`, before TTS, so every ASR
check below still runs. **Do not remove it, and do not change the system default
output device for any check here — none of them needs a loopback.**

The port and runtime root keep this off the owner's daemon (pid 85617, port
8009) and off his `daemon.lock`; `JARVIS_VOICE_DISABLE_WAKE=1` guarantees no
second process opens the microphone he owns. **Do not remove any of the four.**
Kill each daemon when its check is done.

**Setup.** Copy the real Silero artifact out of the retired repository first —
severing that dependency is the point:

    cp /Users/alllllenshi/Projects/jarvis-legacy/data/silero_vad.onnx \
       ~/Models/silero_vad.onnx

Scratch configs go in `<repo>/config/` (so `repo_root` still resolves the prompt
and `data/pricing.json`, leaving the keys under test as the only variable) and
are **deleted before the final commit**. Each is a copy of `config/jarvis.yaml`
with keys **merged into the existing `realtime:` block** — never appended as a
second top-level `realtime:`, which `yaml.safe_load` would resolve by keeping
only the last, silently dropping every flag the file sets.

### A. The two paths

- **Positive — resolution from an arbitrary cwd.** `<repo>/config/jarvis-paths.yaml`
  merges `sensevoice_dir: /Users/alllllenshi/Models/sensevoice-small-int8` and
  `silero_vad_path: /Users/alllllenshi/Models/silero_vad.onnx`. Start from
  `cwd=/`, a directory with no `data/`.
  Observable: the record's `sensevoice_dir` / `silero_vad_path` are those two
  absolute paths and `models_ok` is `true`; the line
  `voice models missing; running text-only` does NOT appear. Paste raw stderr.
- **Negative control.** Byte-identical run against the unmodified
  `config/jarvis.yaml`, still `cwd=/`. Observable: `models_ok` is `false`, the
  two paths are the cwd-relative `/data/...`, and the missing-models ERROR DOES
  appear. Without this the positive proves nothing.
- **Positive — a configured relative path anchors at the config file.** A second
  scratch config with `sensevoice_dir: ../../../Models/sensevoice-small-int8`,
  started from `cwd=/`. Observable: the record names the same absolute path as
  the first check. Two different cwds, one absolute path.
- **Positive — the models actually load, not merely `exists()`.** In the `cwd=/`
  run of the first check, post a WAV to the live PTT ASR route:

      say -o /tmp/ptt.wav --data-format=LEI16@16000 "小月，现在几点了"
      curl -sS -F 'audio=@/tmp/ptt.wav' http://127.0.0.1:8011/inherent/asr-submit

  Observable: the HTTP response body carries a non-empty transcript — a real
  SenseVoice decode out of the configured directory, from a process whose cwd
  has no `data/`. A 501 means the pipeline was never constructed: failure, not
  caveat.

### B. The representative subset — a value set in YAML reaches its consumer

Three keys, one from each wiring mechanism, each with its negative control. The
record field named is the observable; it is written from the same `_VoiceKnobs`
instance that is threaded to the consumer, so a field showing the configured
value and a consumer receiving the default cannot both be true.

- **`realtime.wake_threshold` — threaded to both input owners.**
  Set `0.87`. Observable: record field `wake_threshold` is `0.87`.
  **Negative control:** same run, key absent → `0.5`.
- **`realtime.tts_voice` — a MiniMax knob wired to an existing parameter.**
  Set `Chinese (Mandarin)_GentleGirl`. Observable: record field `tts_voice` is
  that string. **Negative control:** key absent →
  `Chinese (Mandarin)_ExplorativeGirl`.
- **`realtime.vad.record.prob_threshold` — a nested profile field.**
  Set `0.61`. Observable: record field `vad_record_prob_threshold` is `0.61`
  AND `vad_record_db_threshold` is still `-45.0` and `vad_tts_prob_threshold`
  still `0.5` — a partial profile override must not blank its siblings or the
  other profile. **Negative control:** key absent → `0.4`.
- **`realtime.tts_task_start_timeout_s` — one of the four that needed a new
  constructor parameter.** Set `7.5`. Observable: record field
  `tts_task_start_timeout_s` is `7.5`. **Negative control:** absent → `3.0`.
- **Malformed degrades, never fails boot.** `wake_threshold: "loud"` in the same
  config. Observable: one WARNING naming `realtime.wake_threshold`, the record
  field back at `0.5`, and the daemon serving —
  `POST /inherent/submit -d '{"text":"hi"}'` returns a `turn_id`.

### C. The two wave-scoped keys

Their consumers (`AudioIngress`, the streaming player) require a device open,
which this window forbids, so the observable is that the key reaches the parser
that owns it — which is where a wrong placement would fail.

- **`realtime.single_audio_ingress.device_miss_limit`.** With
  `realtime.enabled: true` and `single_audio_ingress.enabled: true`, set
  `device_miss_limit: 0`. Observable: the record's `reason` is
  `invalid_input_config:realtime.single_audio_ingress.device_miss_limit must be
  a positive integer`. `_single_ingress_activation` returns this before
  `engine.start()`, so no device is touched. **Negative control:** the same
  config with the key absent → `reason` is `wave1_capability_missing` (the next
  gate), i.e. the field parsed clean.
- **`realtime.streaming_output.ring_seconds`.** Same shape against
  `streaming_media_config_from_mapping`; observable is the same `reason` field
  quoting `realtime.streaming_output.ring_seconds must be a positive number`.

### D. The deleted `_DEFAULT_TTS_SAMPLE_RATE_HZ`

The behavior change is that a `canonical_sample_rate_hz` other than 48000 no
longer silently disables Wave-2 streaming. Proving it needs `MINIMAX_API_KEY`,
which is exactly what section B forbids — so this one run additionally sets
`realtime.output_device: jarvis-no-such-output-device`. sounddevice raises on an
unresolvable device name before opening any hardware, so both players fail
closed and no audio device is opened. Config: `realtime.enabled: true`,
`concurrency_safety.{transactional_event_append,lifecycle_terminal_cas}: true`,
`streaming_output: {enabled: true, canonical_sample_rate_hz: 44100}`.

Note the blast radius, which is wider than "streaming works now": the derived
rate also feeds the LEGACY player and the provider's `sample_rate_out`. A config
that sets `canonical_sample_rate_hz: 44100` while `realtime.enabled` is `false`
gets a legacy player at 44100 where today it would get 48000. That follows from
"one value, not two" and is the intended consequence, but it is the one way this
card can change behavior for a config that sets none of the twenty-eight new
keys. The shipped value is 48000, so nothing moves unless an operator already
changed that key.

Observable: the line
`realtime.streaming_output capability/config validation failed; downgraded to
legacy TTS.` — present before the change (the equality term at `:1907` fails),
absent after. Show both, by running the same config against `git stash`-free
before/after builds or by quoting the removed line. **Negative control:** the
same config with `canonical_sample_rate_hz: 48000` shows the line in neither.
Confirm no `OutputStream` opened: the run must log
`legacy TTS startup failed (...); downgraded to text-only`.

### E. Regressions

- **Backward compatibility — the mandate.** From the repo root (`cwd == <repo>`,
  the launchd shape) with the **unmodified** `config/jarvis.yaml`. Observable:
  every one of the record's twenty-nine fields equals today's constant, the two
  paths are the cwd-relative `<repo>/data/...`, the missing-models ERROR appears
  exactly once (this worktree's `data/` holds only `pricing.json`), and
  `POST /inherent/submit` still returns a `turn_id`. That single ERROR plus a
  live text path is the pre-flight semantic, preserved. If any field differs
  from its constant, STOP AND REPORT.
- **Hermetic suite.** `env -u MINIMAX_API_KEY uv run pytest -q -m "not live_llm
  and not live_codex"`. Measured on this worktree at `68b8024`: **1069 passed,
  1 skipped, 64 deselected**. Re-measure before the first edit; never take a
  count from a card.
- **Tier 1 gates.** `lint-imports`, `ruff check`, `mypy --strict`, each with its
  printed count pasted. A deleted `serve_inherent` kwarg, a deleted module
  constant and six new dataclass fields are exactly what `mypy --strict` catches.
- **Live run: required, and A–D are it.** The daemon really starts, really
  resolves twenty-eight values from one file, and really decodes speech through
  SenseVoice from the configured directory. A wake run would need the microphone
  the owner's daemon holds and audio output he is using. **Do not stop, restart
  or contend with the owner's daemon.** If you believe a wake run is genuinely
  needed, STOP AND REPORT and let the owner schedule it.

## Docs to sync

- `docs/adr/0006-full-duplex-voice-session.md` §5 — ADR-0006 owns the `realtime:`
  namespace, so it owns the facts this card creates. (a) Record that `:716` is
  now implemented for twenty-eight values and name what it still owes (the two
  legacy capture bounds; the two openwakeword frame constants, ruled out as
  external contracts). (b) Add the naming convention as a namespace-wide rule —
  flat child if the fact applies whatever wave is on, wave sub-block otherwise,
  and a flat key must be wired into every owner that consumes it. (c) Add the
  resolution rule for any path-valued `realtime.*` key. (d) Record that the TTS
  output rate is `streaming_output.canonical_sample_rate_hz` and that no separate
  player-rate constant exists. (e) Record the one-per-boot startup record. Do NOT
  restate the twenty-eight key names or their values: `:725` already rules that
  `config/jarvis.yaml` owns them.
- `docs/adr/0005-inherent-voice.md` §9 — one paragraph under the table: the
  `voice.*` namespace is superseded by ADR-0006 §5, none of its sixteen rows was
  implemented, and the values that exist live under `realtime:`. Do not
  restructure or delete the table — a superseded proposal is still the record of
  what was once intended.
- `docs/adr/0005-inherent-voice.md` §12 — the pre-flight sentence stays true as
  written. Judge it explicitly and say so; do not duplicate the resolution rule
  into it.
- `docs/adr/0006-full-duplex-voice-session.md` D11 (`:562`) — the declick fade
  length is "a property of human hearing, not of a deployment", and this card
  deletes the constant that sentence's *code comment* cites. The ADR text stays
  true; only the code comment moves. Judge the ADR unchanged and say so.
- `docs/spec.html` — no section owns model-artifact resolution or realtime voice
  tuning. Judge unchanged and say so. Add nothing.
- No new ADR. This card is one slice of ADR-0006 §5 (`:716`); a second ADR
  restating it would be the duplication the working contract forbids.

## Open questions

(none)

## /goal condition

Implement `docs/goals/voice-model-paths-from-config.md`. Done when ALL of the
following appear in this transcript as raw command output, not as claims:

1. The re-pinned tip — `git rev-parse HEAD` and `git log -1 --oneline` before any
   edit — and a statement of whether each `path:line` in the card still resolves
   to the cited symbol. Report any that moved; never silently follow a stale
   line number.
2. The measured hermetic baseline BEFORE any edit: the raw tail of
   `env -u MINIMAX_API_KEY uv run pytest -q -m "not live_llm and not live_codex"`.
   Card measured 1069 / 1 / 64 at `68b8024`. If yours differs, EXPLAIN and
   proceed against your own number.
3. The `config/jarvis.yaml` diff, showing all twenty-eight keys in their decided
   placement. A `voice:` namespace is forbidden by ADR-0006 §5 (`:718`);
   `realtime.single_audio_ingress` is forbidden for anything but
   `device_miss_limit`, whose sole consumer is that block's own object. If you
   believe otherwise, STOP AND REPORT — do not re-decide the namespace mid-run.
4. Section A: the four raw outputs — arbitrary-cwd positive, its negative
   control, the relative-path run, and the `asr-submit` response body.
5. Section B: the five raw record lines and their five negative controls, with
   the asserted field quoted from each.
6. Section C: the two `reason` values and their two negative controls.
7. Section D: the presence and absence of the capability-validation line, and
   the text-only downgrade line proving no device was opened.
8. Section E: the backward-compatibility record with every field at its
   constant; the post-change hermetic tail with no regression against (2); the
   printed counts of `lint-imports`, `ruff check`, `mypy --strict`.
9. Each entry under "Docs to sync" either updated or explicitly judged
   unchanged, with the reason.
10. `git status` clean, the scratch configs under `config/` deleted, and each
    slice committed per the commit skill with a Progress line appended.

Constraints: every daemon you start uses `env -u MINIMAX_API_KEY`,
`JARVIS_VOICE_DISABLE_WAKE=1`, `--force-manual`, `--port 8011` and
`--runtime-root /tmp/jarvis-voicecfg-root`, and you kill it when its check is
done. Never start, stop, restart or otherwise interfere with the owner's daemon
(pid 85617, port 8009). Never change the system default output device. Never
edit anything under `/Users/alllllenshi/.jarvis-allen-test/`. Never touch, build
or delete anything under `.claude/worktrees/realtime-live-test`. Read config
values from `config/jarvis.yaml` directly, never from this card.

If the card contradicts the repository, stop and report; do not redesign.

## Progress

- Baseline at `68b8024`: 1069 passed, 1 skipped, 64 deselected — the card's
  measured number, re-measured before the first edit.
- Six slices landed: model paths (`b9bab6f`), `_VoiceKnobs` and the
  twenty-four flat keys (`3c2b369`), the derived output rate (`a670b63`), the
  two wave-scoped keys (`64cd4d4`), the startup record (`b2d5b61`),
  `config/jarvis.yaml` (`74b3d29`).
- Acceptance A (paths): positive from `cwd=/` resolved both configured
  absolute paths with `models_ok=true` and no missing-models ERROR; the
  negative control on the pre-change config from the same cwd showed
  `models_ok=false` and the ERROR; a configured relative path resolved to the
  same absolute path from `cwd=/`; `POST /inherent/asr-submit` returned
  `"What time is it right now?"` and `"小月现在几点了？"` — real SenseVoice
  decodes out of the configured directory.
- Acceptance B (subset reaches its consumer): `wake_threshold` 0.87,
  `tts_voice` GentleGirl, `tts_task_start_timeout_s` 7.5 and
  `vad.record.prob_threshold` 0.61 all appeared in the record; the negative
  control showed 0.5 / ExplorativeGirl / 3.0 / 0.4. A partial VAD override
  left `vad_record_db_threshold` and the whole `tts` profile untouched. A
  malformed `wake_threshold` warned, fell back to 0.5 and kept the daemon
  serving. Two profiles with disagreeing debounce triples were refused whole.
- Acceptance C: `streaming_output.ring_seconds: 0` produced
  `realtime.streaming_output config invalid (realtime.streaming_output.
  ring_seconds must be a positive number)`; `4.0` produced no such line.
  `single_audio_ingress.device_miss_limit` has no device-free live
  observable — see below.
- Acceptance D: at `canonical_sample_rate_hz: 44100` the
  `capability/config validation failed` line appears with the equality term
  restored and is absent without it; at 48000 it appears in neither. Every
  run used an unresolvable `output_device`, so no audio device was opened
  (`open:ValueError device='jarvis-no-such-output-device'` on both paths).
- Acceptance E: from the repo root with the pre-change config, all 27 record
  fields equal their `68b8024` source constants (checked mechanically), the
  missing-models ERROR appears exactly once, and `POST /inherent/submit`
  returns a `turn_id`. The shipped config produces a byte-identical record.
  `device_miss_limit` (3) and `ring_seconds` (2.0) match their pre-change
  constants through their own parsers.
- **Not verified live: `realtime.single_audio_ingress.device_miss_limit`.**
  Its only consumer is `AudioIngress`, and the parse error that would name it
  is only reachable through `_spawn_voice_input_owners`, which
  `JARVIS_VOICE_DISABLE_WAKE=1` skips — and dropping that flag would open the
  microphone the owner's daemon holds. Value and absent-key fallback are
  verified through the shipped parser; the boot that exercises it needs a free
  microphone and is the owner's to schedule.
- Deviation from the card as written: every daemon run also sets
  `JARVIS_LOG_LEVEL=INFO`. Without it `logging.basicConfig` is never called
  (`jarvis/__main__.py:14-18`) and no INFO record reaches the log. The owner's
  live daemon runs at INFO (347 INFO lines in its `daemon.log`), so the record
  reaches production.
- `sherpa-onnx` was missing from this worktree's venv and installed per
  `pyproject.toml:159`, which names it an operator-installed voice wheel. The
  ASR acceptance is unrunnable in a fresh worktree without it.
- Verifier pass found one real defect, fixed and re-verified:
  `Path.expanduser()` RAISES on a `~` it cannot resolve (unlike
  `os.path.expanduser`), so `silero_vad_path: ~models/x.onnx` — one missing
  slash — took down `bootstrap_runtime_app` and with it every CLI command, not
  just `serve`. Now degrades with one warning naming the key. Two smaller ones
  with it: `_knob_number` accepted `.nan`/`.inf` (an `.inf`
  `wake_join_timeout_s` would block shutdown's join forever), and the three
  nested VAD debounce warnings named `realtime.smoothing_window` instead of
  `realtime.vad.record.smoothing_window`.
