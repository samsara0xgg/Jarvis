# Goal: voice-model-paths-from-config

## Goal

The daemon resolves its SenseVoice and Silero artifacts from two new keys,
`realtime.sensevoice_dir` and `realtime.silero_vad_path`, anchored at the config
file's own directory rather than the process's current working directory, and
logs one startup record naming the resolved absolute paths, whether pre-flight
passed, and which input owner it spawned and why.

This is the first slice of ADR-0006 §5's unimplemented decision
(`docs/adr/0006-full-duplex-voice-session.md:716`): "New configuration is parsed
into a typed object; no realtime constant remains hard-coded in
`inherent_loop.py`." See Boundaries for the naming convention the next slice
inherits and for the seven constants this card deliberately leaves alone.

## Why

Two facts that only look unrelated.

**The process's cwd is load-bearing and nothing can override it.** The two
artifact locations are relative module constants, the one production caller never
overrides them, and no config key or CLI flag reaches them — so the daemon finds
its models only when started from a directory that happens to have a `data/`
subtree. The owner's live setup satisfies that with a symlink chain whose last
link points into `Projects/jarvis-legacy/data/silero_vad.onnx` — a retired
repository — through a disposable checkout.

The general form of this defect was already decided against: ADR-0006 §5 (`:716`)
rules that no realtime constant remains hard-coded in `inherent_loop.py`. Nine
still are. These two are the slice with a live consequence, so they go first, and
they establish the naming the rest inherit.

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

- Layers that may change: L6 (`jarvis/runtime/`), plus `config/jarvis.yaml`.
  L5 does not change; `jarvis/surface/voice_wake.py:193` stays exactly as it is.
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
- **Non-goal: the other seven hard-coded constants** covered by the same
  ADR-0006 §5 (`:716`) decision. `_DEFAULT_WAKE_THRESHOLD` (`:294`),
  `_DEFAULT_CAPTURE_MAX_DURATION_S` (`:295`), `_DEFAULT_CAPTURE_MIN_VOICED_S`
  (`:296`), `_DEFAULT_TTS_SAMPLE_RATE_HZ` (`:300`), `_WAKE_JOIN_TIMEOUT_S`
  (`:308`) and the two `ring_seconds` literals (`:1912`, `:1966`) stay exactly as
  they are. The owner has a separate plan for them. **The convention this card
  sets, which that plan should inherit rather than re-derive:** a fact that
  applies whatever wave is on is a flat child of `realtime:` alongside
  `output_device` / `tts_volume`; a fact that only means something inside one
  wave belongs in that wave's sub-block (`streaming_output`,
  `single_audio_ingress`), and must then be wired into every owner that consumes
  it.
- **Non-goal: exposing a knob whose only legal value is its default.** ADR-0008
  (`docs/adr/0008-real-time-response-streaming.md:1005`) — "a knob whose only
  legal value is its default is not configuration." Both keys pass: any
  filesystem path is legal, and the owner's own machine needs a non-default value
  because his artifacts live outside every repo. The filter kills two adjacent
  temptations — an "anchor mode" key and a "strict pre-flight" key — each of
  which would have exactly one usable setting.
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
- **Split the input-owner record into its own card.** Both defects are the same
  omission, both land on one new log statement at one site, and the cwd
  acceptance requires that statement to exist regardless. Two cards would edit
  the same new line and cost two daemon restarts on a machine where a restart is
  expensive. Folded in, with the scope bounded to exactly one new record.

## Acceptance evidence

Every check below names the artifact it asserts on. None of them is a unit test.

**Setup, used by the positive checks.** Copy the real Silero artifact out of the
retired repository first — severing that dependency is the point:

    mkdir -p ~/Models
    cp /Users/alllllenshi/Projects/jarvis-legacy/data/silero_vad.onnx ~/Models/silero_vad.onnx

Then write `<repo>/config/jarvis-cwdcheck.yaml` — a copy of `config/jarvis.yaml`
plus:

    realtime:
      sensevoice_dir: /Users/alllllenshi/Models/sensevoice-small-int8
      silero_vad_path: /Users/alllllenshi/Models/silero_vad.onnx

(merged into the existing `realtime:` block, not appended as a second one —
`yaml.safe_load` keeps only the last duplicate top-level key, which would drop
every realtime flag the file already sets.)

Keeping it inside `<repo>/config/` means `repo_root` still resolves the prompt
and `data/pricing.json`, so the model paths are the only variable under test.
Delete the file before the final commit; it is scaffolding, not a deliverable.

**Every daemon start below MUST use** `--force-manual`, `--port 8011`,
`--runtime-root /tmp/jarvis-cwdcheck-root` and `JARVIS_VOICE_DISABLE_WAKE=1`.
The port and runtime root keep it off the owner's daemon (pid 85617, port 8009)
and off his `daemon.lock`; `JARVIS_VOICE_DISABLE_WAKE=1` guarantees no second
process ever opens the microphone he owns. **Do not remove any of the four.**

- **Positive — the actual goal. Resolution from an arbitrary cwd.**
  Start the daemon with `cwd=/` (a directory with no `data/`):

      cd / && JARVIS_VOICE_DISABLE_WAKE=1 <interp> -m jarvis serve \
        --config <repo>/config/jarvis-cwdcheck.yaml \
        --force-manual --port 8011 --runtime-root /tmp/jarvis-cwdcheck-root

  Observable: the new startup record on stderr names
  `sensevoice_dir=/Users/alllllenshi/Models/sensevoice-small-int8`,
  `silero_vad_path=/Users/alllllenshi/Models/silero_vad.onnx`, `models_ok=true`,
  `input_owner=none`, `reason=wake_disabled_env`. The line
  `voice models missing; running text-only` must NOT appear. Paste the raw
  startup lines.

- **Negative control for the same run.** Repeat it byte-for-byte against
  `--config <repo>/config/jarvis.yaml` (neither key set), still from `cwd=/`.
  Observable: `voice models missing; running text-only` DOES appear, the record
  shows `models_ok=false` and the two paths as the cwd-relative
  `/data/sensevoice-small-int8` / `/data/silero_vad.onnx`. Without this the
  positive check proves nothing — it would pass identically if the keys were
  ignored and some unrelated `data/` were reachable.

- **Positive — a configured *relative* path is anchored at the config file, not
  cwd.** Add a second scratch config in the same directory with
  `sensevoice_dir: ../../../Models/sensevoice-small-int8` (relative,
  reaching `~/Models` from `<repo>/config/`). Start from `cwd=/` again.
  Observable: the record names the same resolved absolute path as the first
  check and `models_ok=true`. Started from two different cwds, the record shows
  the identical absolute path.

- **Positive — the models actually load, not merely `exists()`.** Pre-flight is
  only a stat. In the same `cwd=/` run as the first check, the voice pipeline is
  constructed before the `JARVIS_VOICE_DISABLE_WAKE` branch
  (`inherent_loop.py:3776` precedes `:3787`), so PTT ASR is live. Record a WAV
  and post it:

      say -o /tmp/ptt.wav --data-format=LEI16@16000 "小月，现在几点了"
      curl -sS -F 'audio=@/tmp/ptt.wav' http://127.0.0.1:8011/inherent/asr-submit

  Observable: the HTTP response body carries a non-empty transcript — a real
  SenseVoice decode out of the configured directory, from a process whose cwd
  contains no `data/`. Paste the raw response. If it 501s, the pipeline was
  never constructed and the check has failed regardless of what the record said.

- **Input-owner discrimination.** Two runs, both from `cwd=/` with the scratch
  config, differing only in `realtime.single_audio_ingress.enabled`:
  with it `false`, the record reads `reason=feature_disabled`; with it `true`
  while `realtime.enabled` is `false`, it reads
  `reason=realtime_parent_disabled`. Both must show `input_owner=none` because
  wake is disabled by env. This asserts the record carries the activation reason
  and that two different configurations produce two different, correct reasons —
  the discrimination the incident needed. Paste both lines.

- **Regression — backward compatibility, the mandate.** From the repo root
  (`cwd == <repo>`, the launchd shape), start with the unmodified
  `config/jarvis.yaml`. Observable: the record's two paths are
  `<repo>/data/sensevoice-small-int8` and `<repo>/data/silero_vad.onnx` — the
  literal cwd-relative resolution of today's constants — and the missing-models
  ERROR appears exactly once (this worktree's `data/` holds only
  `pricing.json`), with the daemon still serving. Confirm the text path survives
  the ERROR: `curl -sS -X POST http://127.0.0.1:8011/inherent/submit -H
  'content-type: application/json' -d '{"text":"hi"}'` returns a `turn_id`.
  That single ERROR plus a live text path is the pre-flight semantic, preserved.

- **Regression — hermetic suite.** `env -u MINIMAX_API_KEY uv run pytest -q -m
  "not live_llm and not live_codex"`. Measured on this worktree at `508f863`:
  **1069 passed, 1 skipped, 64 deselected**. Confirm your own baseline before the
  first edit and paste the raw tail anyway; if yours differs, explain the delta
  and work against your measured number. Never take a count from a card.

- **Regression — Tier 1 gates.** `lint-imports`, `ruff check`, `mypy --strict`,
  each with its printed count pasted. A new `JarvisRuntime` field and a removed
  `serve_inherent` kwarg are exactly the kind of change `mypy --strict` catches.

- **Live run: required, and the four checks above are it.** The daemon really
  starts, really resolves, really decodes speech through SenseVoice from the
  configured directory. There is nothing further a microphone would add that the
  ASR round-trip does not already prove, and a wake run would need the
  microphone the owner's daemon holds. **Do not stop, restart, or contend with
  the owner's daemon to get a wake-path run.** If you believe one is genuinely
  needed, STOP AND REPORT and let the owner schedule it.

## Docs to sync

- `docs/adr/0006-full-duplex-voice-session.md` §5 — ADR-0006 owns the `realtime:`
  namespace, so it owns two facts this card creates. (a) Add the two keys to the
  "Keys this ADR gates" list that opens at `:718` and runs `:720-723`, marked as
  ungated artifact locations read whatever the rollout mode, unlike the four
  switches above them. Note that `:725` ("Per-field tuning … this ADR does not
  restate it") is why the list carries the key names but not their values. (b) Add
  one sentence giving the resolution rule for any path-valued `realtime.*` key —
  `~`-expanded, and if still relative anchored at the config file's directory,
  never at cwd; an absent key falls back to the module constant, which stays
  cwd-relative. Write (b) as a namespace-wide rule, not a per-key note: the
  remaining `:716` slices will add more paths and should inherit it.
- `docs/adr/0005-inherent-voice.md` §9 (`:302-322`) — **one line only**, under
  the table: the `voice.*` namespace is superseded by ADR-0006 §5 (`:718`), none
  of these sixteen rows was implemented, and the two artifact paths now live at
  `realtime.sensevoice_dir` / `realtime.silero_vad_path`. This card's own first
  draft was misled by that table; leaving it unmarked guarantees the next reader
  repeats the mistake. Do not restructure or delete the table — a superseded
  proposal is still the record of what was once intended.
- `docs/adr/0005-inherent-voice.md` §12 (`:378`) — the pre-flight sentence stays
  true as written. Judge it explicitly and say so; do not duplicate the
  resolution rule into it.
- `docs/spec.html` — no section owns model-artifact resolution (no `sensevoice`
  or `silero` string appears in it). Judge unchanged and say so. Add nothing.
- No new ADR. This card is one slice of ADR-0006 §5 (`:716`); a second ADR
  restating it would be the duplication the working contract forbids.

## Open questions

(none)

## /goal condition

Implement `docs/goals/voice-model-paths-from-config.md`. Done when ALL of the
following appear in this transcript as raw command output, not as claims:

1. The re-pinned tip — `git rev-parse HEAD` and `git log -1 --oneline` shown
   before any edit — and a statement of whether each `path:line` in the card
   still resolves to the cited symbol. Report any that moved; never silently
   follow a stale line number.
2. The two keys as shipped are named `realtime.sensevoice_dir` and
   `realtime.silero_vad_path`, flat children of the existing `realtime:` block.
   Show the diff of `config/jarvis.yaml`. A `voice:` namespace is forbidden by
   ADR-0006 §5 (`:718`); a placement under `realtime.single_audio_ingress` is
   forbidden because `_spawn_wake_listener` cannot read that block. If you
   believe either is nonetheless right, STOP AND REPORT — do not re-decide the
   namespace mid-run.
3. The measured hermetic baseline BEFORE any edit: the raw tail of
   `env -u MINIMAX_API_KEY uv run pytest -q -m "not live_llm and not live_codex"`.
   Card measured 1069 passed / 1 skipped / 64 deselected at `508f863`. If yours
   differs, EXPLAIN the delta and proceed against your own number.
4. The raw stderr startup lines of the arbitrary-cwd positive run (`cd /`, the
   scratch config), showing the new record with both absolute paths,
   `models_ok=true`, and no `voice models missing` line.
5. The raw startup lines of the negative control (`cd /`, the unmodified
   `config/jarvis.yaml`), showing `models_ok=false` and the missing-models
   ERROR. A positive without its negative control is not accepted.
6. The raw startup lines of the relative-path run, showing the same resolved
   absolute path as (3) from a different cwd.
7. The raw HTTP response of `POST /inherent/asr-submit` with a `say`-generated
   WAV, carrying a non-empty transcript, from the `cwd=/` daemon. A 501 is a
   failure, not a caveat.
8. Both input-owner runs' record lines, showing `reason=feature_disabled` and
   `reason=realtime_parent_disabled` respectively.
9. The backward-compatibility run from the repo root with the UNMODIFIED
   `config/jarvis.yaml`, showing the two cwd-relative `<repo>/data/...` paths,
   exactly one missing-models ERROR, and a `POST /inherent/submit` that still
   returns a `turn_id`. If the absent-key case resolves anywhere other than
   today's cwd-relative path, STOP AND REPORT — that is the one thing this card
   forbids.
10. The raw tail of the full hermetic suite after the change with no regression
   against (2), plus the printed counts of `lint-imports`, `ruff check` and
   `mypy --strict`. Report the counts; never infer them.
11. Each entry under "Docs to sync" either updated or explicitly judged
    unchanged, with the reason. When the change alters a documented contract,
    invariant, ownership boundary, or externally relevant behavior, update the
    canonical document that owns that fact; do not document what the code
    already makes clear; do not duplicate a fact across documents.
12. `git status` shows a clean tree, the scratch configs under `config/` are
    deleted, and each slice is committed per the commit skill with a Progress
    line appended.

Constraints: every daemon you start uses `--force-manual --port 8011
--runtime-root /tmp/jarvis-cwdcheck-root` and `JARVIS_VOICE_DISABLE_WAKE=1`, and
you kill it when its check is done. Never start, stop, restart or otherwise
interfere with the owner's daemon (pid 85617, port 8009). Never edit anything
under `/Users/alllllenshi/.jarvis-allen-test/`. Never touch, build or delete
anything under `.claude/worktrees/realtime-live-test`. Read config values from
`config/jarvis.yaml` directly, never from this card.

If the card contradicts the repository, stop and report; do not redesign. Or
stop after 25 turns.

## Progress

- (not started)
