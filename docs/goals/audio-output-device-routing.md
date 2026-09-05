# Goal: audio-output-device-routing

## Goal
A daemon sends its TTS playback to a named output device set in config, so a
live-test run routes to `BlackHole 16ch` without ever switching the macOS
system default output.

## Why
Both in-repo live rigs get a silent run by switching the SYSTEM default output
and restoring it afterwards. When two lanes overlapped, the second lane
captured the first lane's loopback device as its restore target and faithfully
restored it, leaving the owner with no speaker output. `AudioStreamPlayer`
already accepts a `device`; nothing passes one. Routing per-process removes the
shared mutable global instead of guarding it.

## Current behavior
- `AudioStreamPlayer.__init__` takes `device: Any | None = None`
  (`jarvis/surface/voice_tts.py:615-628`, signature at `:621`), stores it at
  `:647`, and its only other use is `device=self._device` at `:735`.
- `_open_output_stream` (`jarvis/surface/voice_tts.py:517-543`) forwards it
  verbatim to `sd.OutputStream(..., device=device, ...)` at `:535-543`.
  `sounddevice` is lazy-imported at `:533`. `device=None` means the CoreAudio
  system default output.
- `sounddevice._get_device_id` already accepts a human device name (an int or a
  name substring, lower-cased and tokenized, matched against
  `"{device_name}, {hostapi_name}"`) and returns the index, raising `ValueError`
  on zero matches and on an ambiguous non-exact match under
  `raise_on_error=True`, which `_get_stream_parameters` always uses.
- Neither production construction site passes `device`, both inside
  `_build_tts_pipeline` (`jarvis/runtime/inherent_loop.py:1755`):
  streaming at `:1816-1821`, legacy fallback at `:1864-1873`. (Line numbers
  re-verified on `realtime-integration` at `0f670d5`; the recon's `:1313`/`:1361`
  are pre-merge and stale.)
- `realtime` is already read with the Mapping-guard idiom at
  `jarvis/runtime/inherent_loop.py:1794-1795`, in the same function, above both
  sites.
- `start()` already wraps the open in `except BaseException`
  (`jarvis/surface/voice_tts.py:709-749`, catch at `:738`) and returns
  `PlayerStartResult("failed_closed", ..., f"open:{type(exc).__name__}")`
  (`:744-749`). A bad device name already fails closed; the reason says only
  `open:ValueError` and never names the device.
- No config key names an output device. `realtime:` (`config/jarvis.yaml:144`)
  has `enabled` (`:145`), `input` (`:206`), `streaming_output` (`:213`),
  `single_audio_ingress` (`:240`); `backend: sounddevice` (`:242`) is the INPUT
  side.
- `scripts/replay_barge_in.py` switches the system default: `_SPEAKER_FALLBACK`
  (`:50`), `_current_output_device` (`:80-91`), `_set_output_device` (`:94-107`),
  `--output-device` defaulting to `"BlackHole 16ch"` (`:348`), the
  switch/collision guard in `main()` (`:353-363`) and the restore `finally`
  (`:377-381`).
- `tests/scenarios/test_live_crash_recovery.py` switches the system default in
  the `silent_output_device` fixture (`:336-386`): captures `before` at
  `:354-359`, switches with `-s` at `:361-365`, restores in `finally` at
  `:371-376`. Its yielded value is consumed only as
  `audio_restore_target` in an `_echo` at `:725`.

## Target behavior
- One config key, `realtime.output_device`, a string device name or absent/null.
  It governs BOTH construction sites — the legacy path at `:1864` is not under
  `realtime.streaming_output` and must honour the same setting.
- The configured string is passed STRAIGHT THROUGH to
  `AudioStreamPlayer(device=...)`. `sounddevice` does the name-to-index
  resolution; Jarvis resolves nothing.
- Read once, next to the existing `realtime` read at `:1794-1795`, with the same
  Mapping-guard idiom. Absent/null yields `device=None` at both sites, which is
  today's behavior byte-for-byte.
- Unresolvable name fails CLOSED. Today's generic catch already does this; keep
  it. The `PlayerStartResult` reason names the configured device when one was
  configured, instead of only `open:ValueError`. One string, not a redesign.
- `scripts/replay_barge_in.py --output-device` targets the player, not the OS:
  the value reaches the daemon through `realtime.output_device`. Nothing in the
  script switches the system default. Note: `runtime.config` is declared
  `Mapping[str, Any]` (`jarvis/runtime/__init__.py:414`) and must not be mutated
  in place; the script already reads the YAML itself at `:215` and already has a
  `TemporaryDirectory` in `main()`.
- The `silent_output_device` fixture stops MUTATING the system default: the `-s`
  switch and the restore `finally` go away, and the device name reaches the
  daemon through the overlay config `_build_overlay` writes
  (`tests/scenarios/test_live_crash_recovery.py:206-250`, `realtime` mutated at
  `:234-247`, dumped at `:249`).
  Read-only `SwitchAudioSource` calls stay: the `-a -t output` presence guard
  (skip when BlackHole is absent) and the `-c -t output` readings, which become
  this card's canary. The `_echo` at `:725` stops calling the value a restore
  target.

## Affected contracts and files
- config `config/jarvis.yaml:144-145` — new `realtime.output_device` key, absent
  or null by default, with a one-line comment that it names the TTS output
  device and that unset means the system default.
- runtime `jarvis/runtime/inherent_loop.py:_build_tts_pipeline` — one read at
  `:1794-1795`, two `device=` pass-throughs at `:1816-1821` and `:1864-1873`.
- L5 `jarvis/surface/voice_tts.py:744-749` — the `failed_closed` reason string
  names the configured device when `self._device` is not None.
- `scripts/replay_barge_in.py` — dead after the conversion, delete all of it:
  `_SPEAKER_FALLBACK` (`:50`), `_current_output_device` (`:80-91`),
  `_set_output_device` (`:94-107`), the switch/collision-guard block in `main()`
  (`:353-363`), the restore `finally` (`:377-381`), and `import subprocess`
  (`:23`), which has no other use in the file. `main()`'s docstring (`:341`)
  states the switch/restore and becomes wrong.
- `tests/scenarios/test_live_crash_recovery.py` — fixture conversion as above.
- `tests/integration/test_wave2_streaming_media.py:2473+` — the existing
  `_build_tts_pipeline` harness (`_open_output_stream` patched at `:2496-2500`,
  `SimpleNamespace` runtime config at `:2482-2487`) is where the pass-through
  test belongs.
- `tests/integration/test_voice_output_lifecycle.py` — where the failure-reason
  test belongs.

`scripts/replay_barge_in.py` and `tests/scenarios/test_live_crash_recovery.py`
are normally lane B's files. Lane A takes them for this card only. If either has
changed shape by launch — the fixture no longer at `:336-386`, or `main()` no
longer holding the switch/restore — re-pin the card against the branch rather
than forcing the edit.

## Boundaries and non-goals
- Layers that may change: runtime (wiring), L5 (one error string), config,
  tests, scripts. No new module, no new import edge.
- Must not change: the input side (`realtime.single_audio_ingress`,
  `_default_input_device_profile`); `device-profile-resolver` and barge-mode
  gating; `_coreaudio_default_output_route`
  (`jarvis/surface/voice_backend.py:469-495`), which only OBSERVES the current
  default; the test/bench construction sites
  (`tests/integration/test_realtime_observability.py:137`,
  `test_wave2_streaming_media.py:239,:382`,
  `test_wave3_single_audio_ingress.py:3047,:3145`,
  `test_voice_output_lifecycle.py:88,:120,:160,:190,:391`,
  `scripts/bench_voice_streaming_output.py:91,:255,:395,:733`), which keep
  passing no device.
- Non-goals: hot-swapping the device on an already-open stream; any Swift or
  desktop device picker; enumerating or validating devices ahead of open.

## Rejected approaches
- A jarvis-side device-enumeration or name-to-index helper — `sounddevice`
  `_get_device_id` already maps a name to an index inside `sd.OutputStream`, and
  raises on no match or an ambiguous match. A helper would duplicate that,
  drift from PortAudio's matching rules, and add an L5 module for zero
  behavior. This is the obvious wrong turn on this card: feeling tempted to
  write a resolver is the signal the card was misread.
- Falling back to the system default when the configured name does not resolve
  — REJECTED. That routes a test run out of the owner's real speakers, which is
  the exact failure this card exists to remove. Fail closed instead.
- Putting the key under `realtime.streaming_output` — the legacy path at
  `:1864` is not governed by that block and must honour the same setting.
- Extending `device-profile-resolver` — it classifies the CURRENT default route
  to gate `allowed_barge_mode` and explicitly disclaims this work
  (`docs/goals/device-profile-resolver.md`, Boundaries and non-goals).
- Keeping the switch/restore and only hardening the collision guard — a guard on
  a shared mutable global is what already failed. Per-process routing removes
  the global.

## Acceptance evidence
- Pre-change baseline: run
  `PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and not live_codex"`
  BEFORE any edit and record the actual passed/deselected counts in Progress.
  It was 1014 passed / 64 deselected after lane/a merged (`2140407`), and a
  lane/b merge adding 4 more was in flight, so the number at launch will differ;
  use the recorded count, not this one.
- Regression: the same command after the change prints the recorded branch
  baseline at launch plus this card's new tests, same deselected count.
- Pass-through, hermetic, no audio device opened: a new test in
  `tests/integration/test_wave2_streaming_media.py` records the `device=` kwarg
  reaching `voice_tts.AudioStreamPlayer` at both `_build_tts_pipeline` sites
  (wrap the class so the real player is still constructed), with
  `_open_output_stream` patched as at `:2496-2500`. With
  `realtime.output_device: "BlackHole 16ch"` both sites receive
  `"BlackHole 16ch"`; with the key absent both receive `None`.
- Fail-closed legibility: a new test in
  `tests/integration/test_voice_output_lifecycle.py` patches
  `_open_output_stream` to raise `ValueError`, and asserts
  `AudioStreamPlayer(device="No Such Device").start()` returns
  `status == "failed_closed"` with the configured device name present in
  `reason`, and that a `device=None` player's reason is unchanged.
- Gates: `PYTHONPATH=. .venv/bin/lint-imports`,
  `PYTHONPATH=. .venv/bin/ruff check .`,
  `PYTHONPATH=. .venv/bin/mypy --strict jarvis tests scripts tools`.
  `PYTHONPATH=.` is mandatory on every one — without it a provenance test fails
  spuriously because the editable install resolves to the main checkout.
- Swift: baseline 174. No `desktop/` change is expected; run it only if
  `desktop/` is touched.
- Live run: REQUIRED. Reuse the converted crash-recovery rig
  (`PYTHONPATH=. .venv/bin/python -m pytest -q -s --live-llm -m live_llm
  tests/scenarios/test_live_crash_recovery.py`; `--live-llm` is required or
  `tests/conftest.py:105` skips the item). Do not build a new rig.
  Canary: with `realtime.output_device: "BlackHole 16ch"` in the overlay,
  playback reaches that device (the run speaks and completes, nothing audible
  from the speakers), and `SwitchAudioSource -c -t output` reads the SAME value
  BEFORE and AFTER the run. Quote both readings raw. That equality is the proof
  the hazard is gone.
  Audio rule: this run never switches the system default, so the standing rule
  is satisfied by not switching. Do not add a restore trap — there is nothing to
  restore.

## Docs to sync
- `config/jarvis.yaml` — the key's own comment is the documentation; nothing
  duplicates it elsewhere.
- `docs/spec.html` — expected unchanged: it has zero occurrences of
  `streaming_output` and carries no config-key inventory. Judge explicitly
  unchanged, or update only if a sentence there states the TTS output stream
  always uses the system default and is now wrong.
- `docs/adr/` — expected unchanged: no ADR owns the output device route. Judge
  explicitly unchanged.
- Existing goal cards and `docs/live-burn-*` files are per-run historical
  records; do not rewrite them.

## Open questions
(none)

## /goal condition

The transcript shows all of the following as raw command output, pasted in full
and not summarized:

1. Pre-change baseline: the raw output of
`PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and not live_codex"`
run BEFORE any edit, with its passed/deselected counts stated as the branch
baseline at launch.
2. Regression: the same command after the change, raw output shown, passing at
the recorded baseline plus this card's new tests, same deselected count.
3. Gates, each raw: `PYTHONPATH=. .venv/bin/lint-imports` printing the contract
KEPT; `PYTHONPATH=. .venv/bin/ruff check .` clean; `PYTHONPATH=. .venv/bin/mypy
--strict jarvis tests scripts tools` printing Success. `PYTHONPATH=.` is visible
in every command line.
4. Pass-through: raw passing output of the new hermetic test, and the transcript
states its assertions — with `realtime.output_device: "BlackHole 16ch"` BOTH
`AudioStreamPlayer` construction sites in `_build_tts_pipeline` receive
`device="BlackHole 16ch"`, and with the key absent both receive `device=None`;
no real audio device is opened because `_open_output_stream` is patched.
5. Fail-closed: raw passing output of the new test showing that an unresolvable
device name yields `status == "failed_closed"` with the configured device name
inside `reason`, and that no fallback to the system default occurs.
6. The transcript states that no jarvis-side device-enumeration or
name-to-index resolver was written, and that the configured string is passed
straight through to `AudioStreamPlayer(device=...)`.
7. Dead code: the raw `git diff --stat` plus a statement that
`_current_output_device`, `_set_output_device`, `_SPEAKER_FALLBACK`, the
switch/restore in `main()` and `import subprocess` are deleted from
`scripts/replay_barge_in.py`, and that the `silent_output_device` fixture in
`tests/scenarios/test_live_crash_recovery.py` no longer runs
`SwitchAudioSource -s`. A raw `grep -n "SwitchAudioSource -s\|_set_output_device"
scripts/replay_barge_in.py tests/scenarios/test_live_crash_recovery.py` shows no
hits.
8. Live canary: raw output of the live crash-recovery run, ending in a pass
line, with the overlay carrying `realtime.output_device: "BlackHole 16ch"`,
showing playback reaching that device and the run completing spoken; and the raw
`SwitchAudioSource -c -t output` reading quoted BEFORE the run and AFTER the
run, with the transcript stating the two readings are identical. The transcript
also states that this run never switched the system default, so the standing
audio rule is satisfied by not switching, and that no restore trap was added.
9. Swift: stated explicitly — either `desktop/` was not touched, so no Swift run
was needed, or raw Swift output shows 174 passing.
10. Docs: each entry under "Docs to sync" is shown as updated or explicitly
judged unchanged with a reason. Where the change altered a documented contract,
invariant, ownership boundary, or externally relevant behavior, the canonical
document that owns that fact was updated; no fact was duplicated across
documents, and nothing the code already makes clear was documented.
11. `git status` shows a clean tree, and each committed slice is named under
Progress with its sha.

Stop and report rather than redesigning if the card contradicts the repository,
if `scripts/replay_barge_in.py` or `tests/scenarios/test_live_crash_recovery.py`
has changed shape since this card was pinned, or if the change grows past one
config key, one read plus two pass-throughs, one error string and the two
call-site conversions.

Or stop after 25 turns.

## Progress
- (none yet)
