# Goal: tts-volume-headroom

## Goal
MiniMax stops returning hard-clipped audio: the request volume drops from 5 to 3
and becomes tunable through one `realtime.tts_volume` config key.

## Why
A dedicated investigation captured the real PCM and measured it. `vol=5` drives
MiniMax's own int16 encoder into saturation before we ever touch the bytes:
across 5 utterances / 79.1 s the production stream carried 490 samples over full
scale, tracking 438 samples the provider returned already clipped, on 4 of the 5
utterances. A same-text sweep puts the knee at 3 — provider raw int16
peak/samples-at-full-scale: vol=1 8266/0, vol=2 20844/0, vol=3 30140/0 (second
text 20468/0), vol=4 32767/24, vol=5 32768/55 (clipped on every text tried),
vol=10 32768/8489 with flat-top runs up to 16 samples. Owner accepted the
loudness cost: RMS 0.21 -> 0.13, about 4.1-4.5 dB quieter.

MiniMax's own API reference (the T2A WebSocket, HTTP and async pages agree)
documents `voice_setting.vol` as type **number**, range **`(0, 10]`**, default
**`1.0`**. Our hardcoded 5 was five times the vendor's own default gain, and
saturating their int16 encoder is exactly what that predicts. So this is not
"we picked a new number" — it is coming back down from a value nobody chose
deliberately.

But 3, not the documented default of 1. The sweep shows `vol` is NOT a clean
linear scalar over this range: peak int16 runs 8266 / 20844 / 30140 / 32767
(clipped) / 32768 (clipped) for vol 1-5. At vol=1 the peak is only 0.25 of full
scale, throwing away roughly 12 dB of the available digital range. 3 is the
value that uses nearly the whole int16 range without touching the ceiling. Do
not "just use the vendor default of 1" — that would make Jarvis needlessly quiet.

The config key is not speculative generality. The same investigation established
that 3 is empirically safe **on the two texts it tried** and is not proven safe
across all content or all voices, so the value will need tuning by ear against
real speech. An audio level is a physical-world calibration, not a constant
anyone can derive; today tuning it means editing Python and redeploying.

## Current behavior
- `jarvis/surface/voice_tts.py:1747` `volume: int = 5` is the only place the
  number lives; `:1759` stores it as `self._volume`.
- Two emit sites read it and put it on the wire as `voice_setting.vol`:
  `voice_tts.py:1938` (`MiniMaxWSClient._handshake`, the legacy non-streaming
  `synthesize()` path) and `voice_tts.py:2150` (`MiniMaxTTSSession`, the
  streaming path). One default change covers both.
- **No caller anywhere passes `volume`.** `grep -rn --include='*.py'
  'MiniMaxWSClient(' .` returns seven construction sites, and
  `grep -rn --include='*.py' 'volume=' .` matches only the internal
  pass-through at `voice_tts.py:1797`, two explicit test sites, and the
  unrelated `voice_ducking.py:94`. No production or bench caller passes it.
  This is what makes a one-line default change safe globally instead of needing
  a scoping split. All seven sites take the default:
  - `jarvis/runtime/inherent_loop.py:1832` `_new_provider()` — production. Called
    at `:1838` and again at `:1898` / `:1905` on streaming downgrade, so one
    closure edit covers all three call sites.
  - `scripts/bench_voice_streaming_output.py:86,250,404,728` — four benches.
    They measure latency and lifecycle; nothing there asserts a level (grep for
    `peak|rms|clip` returns no hits), so they inherit 3 silently, which is what
    we want: the bench should match production.
  - `tests/integration/test_voice_output_lifecycle.py:720,762` — timeout and
    cancel tests with `_ws_connect` patched to never connect or to hang. They
    never reach the wire; volume is irrelevant to them.
  - `tests/integration/test_wave2_streaming_media.py:2400,2452` construct
    `MiniMaxTTSSession` with an explicit `volume=5` and are unaffected by a
    default change. `grep -rn '"vol"' tests/ scripts/` returns no hits, so
    nothing asserts the wire value today.
- No config key exists: `grep -n -i vol config/jarvis.yaml` returns nothing.
- Nothing clips or limits on our side: `np.clip` / `clip(` return no hits in
  `voice_tts.py` or `voice_media.py`. We faithfully reproduce provider clipping.
- Unrelated despite the name: `jarvis/surface/voice_ducking.py:72,94`
  `output_volume` is the macOS system output level, a different quantity.

## Target behavior
- The default request volume is 3. With no config key set, the wire frame's
  `voice_setting.vol` is 3 at both emit sites.
- `realtime.tts_volume` overrides it. Absent or null means the
  `MiniMaxWSClient` signature default, which stays the single place the number 3
  lives. A present value is passed straight through — no clamping, no type
  guard: `int(volume)` at `voice_tts.py:1759` fails the boot on a bad value,
  matching the deliberate no-validation stance already documented for
  `realtime.output_device` at `inherent_loop.py:1842-1844`.
- The key's accepted domain is the provider's, not a guess: MiniMax documents
  `vol` as a number in `(0, 10]`. Our parameter is annotated `int`
  (`voice_tts.py:1747`) and narrowed again by `int(volume)` at `:1759`; this
  card KEEPS that narrowing, so the effective domain is 1-10 integer. The card
  does not claim the API is integer-typed — it is `number`; we are deliberately
  narrower. No clamp and no range check: an out-of-domain value is the
  provider's to reject, consistent with `output_device`.
- At the new default, provider raw int16 has zero samples at full scale
  (`|s| >= 32767`) across a multi-utterance live capture.

## Affected contracts and files
- L5 `jarvis/surface/voice_tts.py:1747` — default 5 -> 3.
- L6/runtime `jarvis/runtime/inherent_loop.py:1831-1836` `_new_provider()` —
  pass the configured volume. Note the ordering: `_new_provider` is defined at
  `:1831` but `realtime` is not parsed until `:1839-1840`, so the config read has
  to move above the closure (or happen inside it).
- config `config/jarvis.yaml:150-155` — new `realtime.tts_volume` key beside
  `output_device`, with its own comment.
- tests `tests/integration/test_wave2_streaming_media.py` — one new hermetic
  test asserting the wire payload (see Acceptance).

## Boundaries and non-goals
- Layers that may change: L5 surface, runtime wiring, config, tests.
- Must not change: the resampling path (`voice_media._SegmentResampler`), the
  segment-join behavior, the interrupt path, and the `AudioStreamPlayer` ring /
  callback. No limiter, no normalizer, no gain stage is added anywhere.
- Non-goal — the no-fade interrupt click: `interrupt_generation`
  (`voice_tts.py:1193-1216`) zeroes the buffer instantly, ~0.5 full scale in one
  sample at current levels, a genuinely loud click. Deferred, and explicitly NOT
  this card: the owner confirmed his pop happens DURING an uninterrupted answer,
  and the measured bursts are spread through the speech, not at boundaries.
- Non-goal — the 68-138 ms of provider silence padding at every sentence join.
- Non-goal — the absent ring-starvation counter:
  `_GenerationRingBuffer.read_into` zero-pads silently
  (`voice_tts.py:283-314`) and `_callback` returns on `actual <= 0`
  (`voice_tts.py:1547`) with no counter; PortAudio cannot flag it because we
  hand it a full block of zeros.

## Rejected approaches
- Adding a limiter or soft-clipper in `voice_tts`/`voice_media` — the clipping
  is inside the bytes MiniMax returns. Limiting after the fact cannot restore
  the flattened peaks; it only adds a gain stage to maintain.
- Re-investigating the resampler, sample rate, or segment joins — all ruled out
  with measurements (sample jump 0.000000 at all 9 joins, bit-identical to a
  continuous-resampler control within +/-1500 samples of every join, every chunk
  32000 Hz, total length matching the control exactly). Do not revisit.
- Using MiniMax's documented default of `1.0` — measured peak there is 8266,
  0.25 of full scale, wasting about 12 dB of digital range for no benefit.
  `vol` is not linear over this range; 3 is the knee, 1 is just quiet.
- A clamp or range validator on the config value — `output_device` sets the
  precedent that a bad value fails the boot rather than silently degrading.

## Acceptance evidence
- **Pre-change baseline**: run
  `PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and not live_codex"`
  BEFORE any edit and record the actual passed/deselected counts in Progress.
  The tip was around 1043 passed / 64 deselected, but that number moves — use
  your own recorded count, never this one.
- **Regression**: the same command after the change, at the recorded baseline
  plus this card's new test, same deselected count.
- **Wire payload (the config key's observable)**: one new hermetic test in
  `tests/integration/test_wave2_streaming_media.py`. Build the provider through
  `inherent_loop._build_tts_pipeline` (the file already drives it at
  `:2503-2697`) and open a session against the existing `_FakeWebSocket`, whose
  `.sent` list holds the parsed frames (already used at `:2430`). Assert
  `ws.sent[0]["voice_setting"]["vol"]` is 3 with the key absent, and is the
  configured value with `realtime.tts_volume` set to something else. This
  asserts an emitted wire frame, not a config read-back.
- **Live run: REQUIRED** — this is provider behavior, so the observable that
  matters is the provider's returned audio, not any Python value.
  - Harness ALREADY EXISTS at `/Users/alllllenshi/.jarvis-audio-test/captures/`
    (note: the scripts are inside `captures/`, not the parent). Do not rebuild
    it. `capture.py` replays the provider -> `_SegmentResampler` -> player byte
    path verbatim and saves `.raw32k.npy` (provider int16), `.prod.npy` and
    `.meta.json`; `vol.py` fetches one sentence at a list of vol values and
    already prints exactly the metrics this card needs (`peak`, `rms`,
    `at_full_scale` = count of `|s| >= 32767`, `clip_runs>=2`, `longest_run`);
    `report.py` prints the per-utterance `raw32k: peak / at_full_scale / rms`
    line; `analyze.py` is the join/discontinuity analysis and is not needed here.
  - Three adaptations are required and are the whole of the work:
    `capture.py:10` and `vol.py:4` `sys.path.insert` point at the
    `realtime-live-test` worktree — repoint them at this checkout or the new
    default is not the code under test; `report.py:3` globs a dead hardcoded
    `/Users/alllllenshi/.claude/jobs/d1e55387/tmp/audio/` — point it at your
    capture output directory. Write new captures to a NEW directory; leave the
    vol=5 baseline files untouched.
  - Canary, new default: a fresh capture over at least 3 utterances, including
    at least one multi-sentence one, printing for each the provider raw int16
    **peak** and the **count of samples at full scale** (`|s| >= 32767`), which
    must be **0** on every utterance, plus the resulting **RMS** per utterance
    so the loudness cost lands on the record rather than being discovered later
    by ear.
  - Canary, vol=5 contrast: the same report over the existing baseline captures
    in `/Users/alllllenshi/.jarvis-audio-test/captures/`, whose full-scale
    counts are nonzero, so the transcript shows before and after.
    **USE `u2`-`u5` ONLY. DO NOT INCLUDE `u1`.** `u1` has no `.raw32k.npy`, and
    `report.py:8` substitutes a zero array for a missing file — so a `u1` raw
    line prints `peak=0 at_full_scale=0`, which reads as "no clipping" on a
    baseline that is in fact clipped. That is the harness manufacturing a false
    PASS for exactly the defect this card claims to fix. Confirm the file exists
    before quoting any raw line:
    `ls /Users/alllllenshi/.jarvis-audio-test/captures/*.raw32k.npy`.
  - **Audio rule, mandatory**: a live TTS capture must NOT change the macOS
    system default output device. Capture the provider bytes rather than playing
    them — `capture.py` and `vol.py` open no output stream, which is exactly why
    they are the rig. If any step would switch the system default, do not run it.
  - **Do not disturb the live daemon**: the owner has one on port 8009 with
    runtime root `/Users/alllllenshi/.jarvis-allen-test` and a running
    InherentCard. Do not stop, restart, or write into any of it. The capture rig
    talks to MiniMax directly and needs no daemon; it needs `MINIMAX_API_KEY` in
    the environment.
- **Gates**: `PYTHONPATH=. .venv/bin/lint-imports`,
  `PYTHONPATH=. .venv/bin/ruff check .`,
  `PYTHONPATH=. .venv/bin/mypy --strict jarvis tests scripts tools`.
  `PYTHONPATH=.` is mandatory on every one — without it a provenance test fails
  spuriously because the editable install resolves to the main checkout.
- **Swift**: no `desktop/` change expected; run it only if `desktop/` is touched.

## Docs to sync
- `config/jarvis.yaml` — the new key's own comment IS its documentation, same as
  `output_device` at `:152-155`. Nothing duplicates it elsewhere.
- `docs/spec.html` — expected unchanged. `grep -n -i minimax docs/spec.html`
  returns exactly one line (`:1058`, that `voice_notify` may be carried by
  MiniMax TTS) and there is no loudness contract and no config-key inventory.
  Judge explicitly unchanged.
- `docs/adr/` — expected unchanged.
  `grep -rn -i 'voice_setting|"vol"|tts volume' docs/adr/` returns no hits, so no
  ADR owns the request level. Judge explicitly unchanged.
- Existing goal cards and `docs/live-burn-*` files are historical records; do not
  rewrite them.

## Open questions
(none)

## /goal condition

The transcript shows all of the following as raw command output, pasted in full
and not summarized:

1. Pre-change baseline: raw output of
`PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and not live_codex"`
run BEFORE any edit, with its passed/deselected counts stated as the branch
baseline at launch.
2. Regression: the same command after the change, raw output shown, passing at
that recorded baseline plus this card's new test, same deselected count.
3. Gates, each raw: `PYTHONPATH=. .venv/bin/lint-imports` printing the contract
KEPT; `PYTHONPATH=. .venv/bin/ruff check .` clean; `PYTHONPATH=. .venv/bin/mypy
--strict jarvis tests scripts tools` printing Success. `PYTHONPATH=.` is visible
in every command line.
4. Wire payload: raw passing output of the new hermetic test, and the transcript
states its assertions — the `task_start` frame captured from the fake WebSocket
carries `voice_setting.vol == 3` when `realtime.tts_volume` is absent, and
carries the configured value when the key is set. The transcript states that no
test asserts a config value by reading it back.
5. LIVE CAPTURE at the new default: raw output of the capture and report run,
covering at least 3 utterances including at least one multi-sentence one, showing
for EACH utterance the provider raw int16 `peak`, the count of samples at full
scale (`|s| >= 32767`), and the `rms`. Every full-scale count is 0. The
transcript states the utterance texts and which one was multi-sentence.
6. BEFORE/AFTER contrast: raw output of the same report over the existing vol=5
baseline captures `u2`-`u5` in `/Users/alllllenshi/.jarvis-audio-test/captures/`,
showing their NONZERO full-scale counts next to the new zeros. The transcript
states that `u1` was excluded because it has no `.raw32k.npy` and the report
substitutes zeros for a missing file.
7. Loudness cost: the transcript names the measured RMS at the new default and
states it against the ~0.21 vol=5 figure.
8. Audio rule: the transcript states that no step changed the macOS system
default output device, and that the capture rig opens no output stream — it
captures provider bytes. It also states that the daemon on port 8009 and the
runtime root `/Users/alllllenshi/.jarvis-allen-test` were not stopped,
restarted, or written to.
9. Scope: the transcript states that no limiter, normalizer, or gain stage was
added, and that the resampling path, the segment-join behavior, and the
interrupt path are unchanged. Raw `git diff --stat` is shown.
10. Docs: each entry under "Docs to sync" is shown as updated or explicitly
judged unchanged with a reason. Where the change altered a documented contract,
invariant, ownership boundary, or externally relevant behavior, the canonical
document that owns that fact was updated; no fact was duplicated across
documents, and nothing the code already makes clear was documented.
11. `git status` shows a clean tree, and each committed slice is named under
Progress with its sha.

Stop and report rather than redesigning if the card contradicts the repository,
if the capture harness cannot be adapted, or if any full-scale count at the new
default is nonzero. Or stop after 30 turns.

## Progress
- (none yet)

---
