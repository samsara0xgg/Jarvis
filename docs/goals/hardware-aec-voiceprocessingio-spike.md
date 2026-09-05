# Goal: hardware-aec-voiceprocessingio-spike

## Goal
ADR-0006 D9's blocking question — whether macOS `VoiceProcessingIO` AEC is good enough on this MacBook to ever unblock natural speakerphone barge-in — is answered by measured numbers from one real acoustic run plus a written disposition, with nothing under `jarvis/` changed.

## Why
`docs/adr/0006-full-duplex-voice-session.md:472` keeps natural speakerphone barge-in disabled "until `VoiceProcessingIOBackend` ... proves residual echo, near-end recall, double-talk, and false-cancel targets". Nothing in the repository can produce those numbers: `SoundDeviceDuplexBackend.capabilities()` reports `aec=False` (`jarvis/surface/voice_backend.py:712-721`) and no code path ever opens an AEC-capable stream. This card buys the measurement, not the backend. Allen's standing decision: software spike only, no purchase, no production wiring.

## Current behavior
- Silero VAD runs on fixed 512-sample frames (32 ms @ 16 kHz) — `SILERO_CHUNK_SAMPLES = 512` (`jarvis/surface/voice_audio.py:48`); `SileroVad.feed` (`:203-228`) requires exactly 1024 bytes of little-endian int16 and raises otherwise, converting to float32 by `/ 32768.0`.
- Two threshold profiles only, `_MODE_THRESHOLDS` (`jarvis/surface/voice_audio.py:78-81`): `record` = prob ≥ 0.4 and dB ≥ -45; `tts` = prob ≥ 0.5 and dB ≥ -22. `VadThresholds` (`:65-73`) also fixes `smoothing_window=5`, `required_hits=3`, `required_misses=24`.
- `_advance_state` (`jarvis/surface/voice_audio.py:273-321`) gates on BOTH smoothed probability and smoothed dBFS, and its own comment says the dB gate exists to suppress "AEC residual + low-level background noise". IDLE→ACTIVE needs 3 consecutive gated frames. `_chunk_db` (`:324-327`) is plain RMS dBFS with a 1e-10 floor.
- `FileReplayBackend` (`jarvis/surface/voice_backend.py:1340-1366`) validates its WAV up front and accepts only 16 kHz, mono, 16-bit PCM.
- `scripts/replay_endpointing.py` is the existing "feed a WAV through the real path" script: it constructs `FileReplayBackend(wav, tail_silence_s=None)` (`:135`), hands it to `AudioIngress`, pairs it with `voice_audio.SileroVad(mode="record", model_path=silero)` (`:151`), sizes the wait from `_wav_duration_s` (`:75-77`, `:163`), and takes repeatable `--wav` paths on the CLI (`:178`).
- No WAV fixture exists anywhere in the repository: `find <repo> -name '*.wav' -not -path '*/.venv/*'` returns nothing. Every test that needs one synthesizes it in-process (`tests/integration/test_voice_file_replay.py:38`, `tests/integration/test_device_profile_resolver.py:583`, `tests/integration/test_voice_ptt_end_to_end.py:33`).
- The ctypes precedent in this layer is property reads, not audio callbacks: `import ctypes` / `ctypes.util` (`jarvis/surface/voice_backend.py:17-18`) is used only for `AudioObjectGetPropertyData` against CoreAudio/CoreFoundation to read the default output's uid/name/transport/data-source/rate (`:399-490`). No render or input callback is ever handed to a C library from Python.
- PyObjC was rejected once already, on the record: `jarvis/deployment/sleep_wake.py:12-14` — "ADR-0009 D3: PyObjC does not wrap IOKit at all, so ctypes is the mechanism and costs no dependency".
- The spike-script precedent is a standalone, uncommitted-to-production probe: `scripts/spike_power_observer.py:1-6` ("ADR-0009 Step 0 spike ... the exact architecture `sleep_wake.py` will use in Step 3"), whose findings were later cited by the production module.
- AVAudioEngine capture already exists in the tree, in Swift: `desktop/inherent-swift/InherentCard/NativeVoiceRecorder.swift:16` records "mono PCM from AVAudioEngine", building `AVAudioEngine()` and reading `input.inputFormat(forBus: 0)` (`:40-42`) before `installTap`. `setVoiceProcessingEnabled` appears nowhere in the repository.
- The resolver cannot express a validated-AEC speaker today: `RouteKind` (`jarvis/surface/voice_backend.py:105-111`) declares `HARDWARE_AEC` with the docstring "`hardware_aec` is never produced here"; `DeviceProfileKey.aec_mode` is a default-only field fixed to `"none"` (`:138`) with no writer in `jarvis/` (grep finds only reads and test literals); and `resolve_device_profile` (`:181-216`) sets `promotable = key.route_kind in {RouteKind.UNKNOWN, RouteKind.HEADPHONES}` (`:203`), so an observed SPEAKER route can never reach the `allowed_barge_mode="natural"` return at `:210-214`.
- Hardware on this machine, from `sd.query_devices()`: default device pair is `[3, 4]` — index 3 `MacBook Pro Microphone` (1 in / 0 out, 48000 Hz), index 4 `MacBook Pro Speakers` (0 in / 2 out, 48000 Hz). Also present: index 2 `BlackHole 16ch`, index 8 `Aggregate Device`, index 9 `Multi-Output Device`.
- Toolchain present: `/usr/bin/swift`, Apple Swift 6.3.3, target `arm64-apple-macosx26.0`, Xcode selected at `/Applications/Xcode.app/Contents/Developer`. In `.venv`: `sounddevice 0.5.6`, `numpy 2.4.6`, `scipy 1.18.1`, `onnxruntime 1.29.0`.

## Target behavior
- `scripts/spike_voiceprocessingio_aec.swift` exists as one self-contained file run with `swift scripts/spike_voiceprocessingio_aec.swift <args>` — no Xcode project, no `Project.yml`, no `desktop/` change. It imports AVFoundation (plus Foundation if it needs `URL`/file APIs, matching `NativeVoiceRecorder.swift:1-2`) and nothing else.
- One invocation captures one configuration: it plays a fixed far-end WAV through the default output while recording the default input to a WAV, with `inputNode.setVoiceProcessingEnabled(true)` on one run and left disabled on the other. Each capture has a silence lead-in, the far-end playback, and a silence tail in one file, and the script prints the sample offsets of those windows so the analysis side does not have to guess them.
- The capture is written as 16 kHz mono PCM16, so it satisfies the same WAV contract the repository already enforces (`jarvis/surface/voice_backend.py:1357-1366`) and can be fed to `SileroVad` in 512-sample frames without resampling in the analysis script.
- The Swift script prints, for each run: the format `inputNode` reports with voice processing in that state (sample rate, channel count, common format), whether enabling voice processing changed it, and whether 16 kHz mono int16 was still obtainable from the engine.
- `scripts/spike_voiceprocessingio_aec.py` exists, reads the two captured WAVs, and prints the numbers. It adds no dependency: `numpy`/`scipy`/`sounddevice` are already in `.venv`, and it imports `SileroVad` from `jarvis.surface.voice_audio` rather than re-implementing a VAD.
- The numbers it prints, per capture (AEC off and AEC on):
  - Residual echo — RMS dBFS over the far-end-playback window minus RMS dBFS over the silence window, plus the AEC-on-minus-AEC-off delta.
  - False-candidate count — IDLE→ACTIVE transitions of `SileroVad` over the far-end-only playback window, run once per profile (`record` and `tts`, the exact `_MODE_THRESHOLDS` values at `jarvis/surface/voice_audio.py:78-81`), with a fresh VAD per (profile, capture) and `prepare_utterance()` before feeding. Both the raw count and the extrapolated per-minute rate are printed, with the window length next to them.
  - The format facts carried through from the Swift run.
- The far-end WAV is generated once — no `*.wav` exists in the repository to reuse — through the existing MiniMax TTS path (`jarvis/surface/voice_tts.py:1717` `MiniMaxWSClient`), resampled to 16 kHz mono PCM16, roughly 15 s of speech. It is an artifact, not a committed file: it and the two captures live under `~/.jarvis-lane-b-test/aec-spike/`.
- `docs/live-burn-<run date YYYY-MM-DD>-voiceprocessingio-aec.md` exists in the shape of the other `docs/live-burn-2026-08-26-*.md` documents: an Invocation section with the exact commands, a Numbers table with AEC off vs AEC on side by side, the format facts, and a closing recommendation paragraph containing exactly one of `viable, proceed to a VoiceProcessingIOBackend card`, `not viable on this hardware`, or `inconclusive, needs the near-end trial`.
- That burn document also records the wiring a future card owes, as fact rather than intent: an observed SPEAKER route is never promotable today (`jarvis/surface/voice_backend.py:203-214`) and `aec_mode` has no writer (`:138`).
- `docs/adr/0006-full-duplex-voice-session.md` D9 gains exactly one sentence, next to the `:472` blocker, citing the burn document and its verdict. No other ADR text moves.
- One hermetic test under `tests/integration/` exercises the analysis script on synthetic WAVs it writes itself: an echo-like signal at -20 dBFS against a silence window yields the expected residual number, and the VAD crossing count is the expected one. The Swift script has no hermetic test; the live run is its only exercise.

## Affected contracts and files
- L5 surface (read only) — `jarvis/surface/voice_audio.py:78-81`, `:203-228` and `jarvis/surface/voice_backend.py:1357-1366` are imported and depended on by the analysis script; neither file changes.
- `scripts/spike_voiceprocessingio_aec.swift` — new; the capture host.
- `scripts/spike_voiceprocessingio_aec.py` — new; the analysis side.
- `tests/integration/test_spike_voiceprocessingio_aec.py` — new; the one hermetic check.
- `docs/live-burn-<run date YYYY-MM-DD>-voiceprocessingio-aec.md` — new; the disposition.
- `docs/adr/0006-full-duplex-voice-session.md` D9 (`:472`) — one sentence.
- `docs/goals/hardware-aec-voiceprocessingio-spike.md` — Progress lines.

## Boundaries and non-goals
- Layers that may change: none. Only `scripts/`, `tests/integration/`, `docs/live-burn-*.md`, and one sentence of ADR-0006 D9.
- Must not change: anything under `jarvis/`, `config/`, `desktop/`, `Project.yml`, or any pre-existing script or test. No `VoiceProcessingIOBackend`, no `RouteKind`/`resolve_device_profile` edit, no new config key, no promotion of any route to `natural`.
- Must not add a dependency, Python or Swift.
- Must not disturb the live daemon on port 8006 with runtime root `~/.jarvis-realtime-test`. If it holds the input device and the capture engine cannot start, report that and stop; do not signal or stop it.
- Household noise bound: read the current level with `osascript -e 'output volume of (get volume settings)'`, set `osascript -e 'set volume output volume 25'` for the run, restore the captured value afterwards on every path including failure. Playback stays under 20 s per configuration.
- Non-goals: near-end interrupt recall and double-talk recall (`docs/adr/0006-full-duplex-voice-session.md:474`) — they need a human speaking over playback, so they are recorded in Progress as an Allen follow-up with the exact command, never a blocker; the 10-aggregate-hour false-cancel figure (`:474`), which no single sitting can produce; any production wiring; any hardware purchase or XVF3800 evaluation.
- If the spike exposes a defect in production code, record it in Progress and report; do not fix it under this card.

## Rejected approaches
- ctypes AudioToolbox `VoiceProcessingIO` with render callbacks in Python — a real-time audio thread calling back into CPython is not reliable, and the ctypes precedent in this layer (`jarvis/surface/voice_backend.py:399-490`) is one-shot property reads, not callbacks. Nothing in the tree hands a C library a Python callback on an audio thread.
- PyObjC — no dependency precedent and an explicit prior rejection at `jarvis/deployment/sleep_wake.py:12-14`.
- An Xcode project or a change under `desktop/` to host the capture — `/usr/bin/swift` runs a single file directly (Swift 6.3.3 confirmed), which is the whole reason the ruling picked a script.
- BlackHole 16ch or the Aggregate Device as the loopback — a digital loopback carries the far-end signal with no acoustic path, so it cannot produce the echo D9 asks about. The measurement is speaker → air → mic on indices 4 and 3.
- Building `VoiceProcessingIOBackend` now, or making the resolver able to emit `hardware_aec` — `:472` makes proof the precondition for the backend, and this card is the proof. The resolver work (`jarvis/surface/voice_backend.py:203-214`, `:138`) is a later card, named in the burn document.
- Tightening the `tts` VAD thresholds instead — `docs/adr/0006-full-duplex-voice-session.md:470` says outright that a stricter VAD threshold is not AEC.
- A new resampling or DSP dependency — `scipy 1.18.1` and `numpy 2.4.6` are already installed.

## Acceptance evidence
- Live run: required, acoustic. The transcript shows both `swift scripts/spike_voiceprocessingio_aec.swift ...` invocations against `MacBook Pro Microphone` (index 3) and `MacBook Pro Speakers` (index 4), with voice processing enabled on one and disabled on the other, BlackHole used for neither leg; the `osascript` volume read before, the set to 25, and the restore afterwards with the same value shown; and each configuration's playback under 20 s.
- Positive: raw output of `PYTHONPATH=. .venv/bin/python scripts/spike_voiceprocessingio_aec.py ...` over the two captures, printing the residual echo dB for AEC off and on with the delta, the false-candidate raw count and per-minute rate for both the `record` and `tts` profiles on both captures, the analysis window lengths, and the format facts including whether 16 kHz mono int16 survived voice processing.
- Positive (hermetic): raw output of `PYTHONPATH=. .venv/bin/python -m pytest -q tests/integration/test_spike_voiceprocessingio_aec.py` passing on the synthetic -20 dBFS-versus-silence case and the VAD crossing count.
- Microphone permission is the first thing the live run must establish. A `swift` script has no bundle identifier, so if macOS denies input to the interpreter, say so with the exact denial, record granting the terminal application microphone access in System Settings as an Allen follow-up with the exact steps, and stop — do not build an app bundle to work around it.
- Regression: raw output of `PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and not live_codex"` with the passed count equal to the integration count at launch plus the new test's cases, and the deselected count unchanged (the new test is hermetic, not marked live). For reference, the count observed after lane A's A3 merges was 868 passed / 64 deselected; restate the actual count at launch rather than assuming that one.
- Gates: `lint-imports`, `ruff check .`, and `mypy --strict jarvis tests scripts tools`, each exit 0 with raw output.
- `git diff --stat` shows no file under `jarvis/`, `config/`, `desktop/`, and no pre-existing script or test touched.

## Docs to sync
- `docs/live-burn-<run date YYYY-MM-DD>-voiceprocessingio-aec.md` — new; the setup, the numbers table, the format facts, the one-verdict recommendation, and the resolver follow-up fact.
- `docs/adr/0006-full-duplex-voice-session.md` D9 — one sentence at `:472` citing the burn document's result. Nothing else in D9 or the ADR moves; the thresholds at `:474` and the route table at `:463-468` are unchanged by a measurement.
- `docs/spec.html` — judged unchanged; state that explicitly.

## Open questions
(none)

## /goal condition
Implement docs/goals/hardware-aec-voiceprocessingio-spike.md on the current branch. The goal is met when the transcript shows all of: (1) `git diff --stat` proving nothing under jarvis/, config/, desktop/, Project.yml, or any pre-existing script or test changed — the diff touches only scripts/spike_voiceprocessingio_aec.swift, scripts/spike_voiceprocessingio_aec.py, one new tests/integration/ module, docs/live-burn-<run date YYYY-MM-DD>-voiceprocessingio-aec.md, one sentence of docs/adr/0006-full-duplex-voice-session.md D9, and the card's Progress; (2) raw output of the live acoustic run: `swift scripts/spike_voiceprocessingio_aec.swift ...` invoked twice on the real MacBook Pro Microphone (index 3) and MacBook Pro Speakers (index 4), once with inputNode.setVoiceProcessingEnabled(true) and once with it disabled, with BlackHole used as neither the capture nor the playback route, `osascript -e 'output volume of (get volume settings)'` shown before, `set volume output volume 25` applied, the original value restored and shown afterwards, and each configuration's playback under 20 s; (3) raw output of `PYTHONPATH=. .venv/bin/python scripts/spike_voiceprocessingio_aec.py ...` over the two captured WAVs printing concrete numbers — residual echo (RMS dBFS over the far-end-playback window minus RMS dBFS over the silence window) for AEC off and AEC on plus the delta; false-candidate raw count and extrapolated per-minute rate with the window length, for the record profile (prob 0.4 / dB -45) and the tts profile (prob 0.5 / dB -22), on both captures; and the format facts — what sample rate and channel format inputNode reported with voice processing enabled and whether 16 kHz mono int16 was still obtainable; (4) an explicit statement of which far-end WAV was used, that it was generated because no *.wav exists in the repository, and its duration; (5) raw output of `PYTHONPATH=. .venv/bin/python -m pytest -q tests/integration/test_spike_voiceprocessingio_aec.py` passing, covering an echo-like signal at -20 dBFS versus silence yielding the expected residual and the expected VAD crossing count on synthetic WAVs; (6) raw output of `PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and not live_codex"` with the passed count equal to the integration count at launch plus the new test's cases and the deselected count unchanged, plus `lint-imports`, `ruff check .`, and `mypy --strict jarvis tests scripts tools` each exit 0 with raw output; (7) docs/live-burn-<run date YYYY-MM-DD>-voiceprocessingio-aec.md created with the invocation, an AEC-off vs AEC-on numbers table, the format facts, a recommendation containing exactly one of `viable, proceed to a VoiceProcessingIOBackend card`, `not viable on this hardware`, or `inconclusive, needs the near-end trial`, and the recorded follow-up that an observed SPEAKER route is never promotable today (jarvis/surface/voice_backend.py:203-214) and aec_mode has no writer (:138); (8) ADR-0006 D9 gained exactly one sentence citing that burn document, and docs/spec.html was explicitly judged unchanged — following the rule that a changed contract is updated in the canonical document owning it, what the code already makes clear is not documented, and no fact is duplicated across documents; (9) near-end recall and double-talk recall were not attempted and are recorded in Progress as an Allen follow-up with the exact command; (10) if microphone permission is denied to the swift process, that denial is shown verbatim, recorded as an Allen follow-up with the exact System Settings steps, and no app bundle was built to work around it; (11) the port-8006 daemon on runtime root ~/.jarvis-realtime-test was never signalled or stopped, stated explicitly; (12) each slice committed with the project commit skill, `git status` clean, and a Progress line per slice in the card. Or stop after 50 turns.

## Progress
- Slice 1 analysis half — 764ccbf — `scripts/spike_voiceprocessingio_aec.py` +
  `tests/integration/test_spike_voiceprocessingio_aec.py`; 3 passed hermetic,
  full suite 950 passed / 64 deselected (integration baseline 947 + 3),
  lint-imports KEPT 1/1, ruff clean, mypy strict clean (232 files).
- Slice 2 capture half — cdfa022 — `scripts/spike_voiceprocessingio_aec.swift`;
  `swiftc -typecheck` clean; live acoustic run, both configurations exit 0,
  15.97 s playback each (bound 20 s), 19.89 s captures at 16000 Hz 1ch 16-bit
  accepted by `FileReplayBackend`. Two platform findings folded into the
  script: `engine.start()` returns -10875 on `kAUInitialize` unless
  `mainMixerNode` is instantiated before `setVoiceProcessingEnabled`, and the
  processed input bus is 9 identical channels (channel 0 taken by an explicit
  `AVAudioConverter.channelMap`). Guard: volume 56 -> 25 -> 56 in a `trap
  EXIT`; output route `MacBook Pro Speakers` and input `MacBook Pro
  Microphone` before, during and after; `BlackHole 16ch` was neither leg.
- Slice 3 disposition — docs/live-burn-2026-09-05-voiceprocessingio-aec.md +
  one sentence in ADR-0006 D9. Numbers: residual echo +13.81 dB (AEC off) vs
  +31.49 dB (AEC on), delta +17.69 dB — the ratio inverted because voice
  processing dropped the idle floor 25 dB while dropping the echo 7.5 dB;
  false candidates `record` 22.48/min -> 7.53/min, `tts` 0.00/min -> 0.00/min;
  16 kHz mono int16 still obtainable with voice processing on, though
  `inputNode` goes 1 ch -> 9 ch. Verdict: `inconclusive, needs the near-end
  trial`. `docs/spec.html` judged unchanged: it carries no AEC or barge-in
  fact at all (`grep -ci "aec\|barge" docs/spec.html` = 0).
- Allen follow-up (not a blocker) — near-end interrupt recall and double-talk
  recall need a human speaking over playback and were not attempted. Exact
  command, one run per case, speaking a short interrupt over the far-end at
  roughly 5 s and again at 10 s:

      osascript -e 'output volume of (get volume settings)'
      osascript -e 'set volume output volume 25'
      swift scripts/spike_voiceprocessingio_aec.swift \
        --far-end ~/.jarvis-lane-b-test/aec-spike/far-end.wav \
        --out ~/.jarvis-lane-b-test/aec-spike/capture-nearend-on.wav \
        --aec on --lead 2.0 --tail 2.0
      osascript -e 'set volume output volume <captured value>'

  then score it against the far-end-only capture with
  `scripts/spike_voiceprocessingio_aec.py --aec-off <far-end-only> --aec-on
  <near-end>`: the crossing count above the far-end-only baseline is the
  near-end recall evidence.
- Microphone permission was already granted: `AVCaptureDevice
  .authorizationStatus(for: .audio)` returned rawValue 3 (authorized), so no
  denial had to be recorded and no app bundle was built.
- The port-8006 daemon on runtime root `~/.jarvis-realtime-test` was never
  signalled or stopped; this card started no daemon of its own.
- Card drift, recorded not redesigned: the card's device indices (`3` mic /
  `4` speakers) are a stale `sd.query_devices()` snapshot; today the same
  named devices enumerate at `4` and `5` (`BlackHole 16ch` moved to `3`). The
  run is bound to the device names and CoreAudio uids, not the indices.
  Likewise the card cites ADR-0006 D9 at `:472`; after the integration merge
  the blocker sentence is at `:516`.
- Slice 4 verifier corrections — the `verifier` agent (fresh context, opus)
  over `15192fc..HEAD` confirmed four defects, all fixed here. (a) The burn
  document called `tts` "the profile that actually runs while Jarvis is
  speaking"; it is the opposite — see the production finding below. (b) The
  claim that the −22 dB gate sits 15 dB above the loudest echo compared the
  gate to the window *mean*: the loudest 32 ms frame is −30.45 dBFS (AEC off)
  and −20.45 dBFS (AEC on), the latter already **over** the −22 gate and held
  to 0 crossings only by five-frame smoothing (peak smoothed −33.31). The
  analysis script now prints both peaks so the corrected sentence is
  reproducible from the committed tool, and `peak_levels` is pinned by two
  hermetic cases. (c) The 7.5 dB drop was attributed to cancellation in both
  the burn document and the ADR sentence, but `isVoiceProcessingAGCEnabled`
  is true by default and the peak frame *rose* 10 dB — cancellation and AGC
  are not separable in this run, and both documents now say so. (d)
  `--silero`'s "using the library default" fallback was a lie:
  `_load_silero_session` raises `ValueError` on `model_path=None`
  (`jarvis/surface/voice_audio.py:100-102`), so the branch and the two
  docstrings repeating it are gone; the path is now forwarded unconditionally
  as `scripts/replay_endpointing.py:181` does.
- Production finding, recorded not fixed (card boundary: "If the spike exposes
  a defect in production code, record it in Progress and report; do not fix it
  under this card"). **`_MODE_THRESHOLDS["tts"]` has no caller.** The only two
  `SileroVad` construction sites in `jarvis/` are
  `jarvis/runtime/inherent_loop.py:1467` and `:2128`, both `mode="record"`,
  and `SileroVad.thresholds()` is called from nowhere in `jarvis/`. The
  stricter prob ≥ 0.5 / dB ≥ −22 playback gate that ADR-0006 D9 leans on is
  never installed, so the detector that runs while Jarvis speaks uses the
  `record` gate — the profile this spike measures at 7.53/min against D9's
  0.5/min target. That makes the false-candidate gap real rather than
  academic, and it is a decision for the owner, not this card.
- Verifier finding not fixed: 764ccbf is typed `test(scripts):` while its
  larger artifact is the 309-line script, so `feat(scripts):` would have been
  the better type, and its Tier 1 line writes `ruff clean (all checks passed)`
  rather than the skill's `(N files)` and omits the `(< 30s budget)` note.
  Rewriting three commits' history to relabel one of them buys nothing the
  report cannot say, so the history stands and the mismatch is reported.
