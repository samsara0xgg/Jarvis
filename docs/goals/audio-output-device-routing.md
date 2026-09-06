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
  `_build_tts_pipeline` (`jarvis/runtime/inherent_loop.py:1775`):
  streaming at `:1840-1845`, legacy fallback at `:1889-1898`. (Line numbers
  re-pinned at launch on `lane/a` at `352f2d1`, after the boot playback
  reconciler moved the file again; the card's `:1755`/`:1816`/`:1864` and the
  recon's `:1313`/`:1361` are both stale.)
- `realtime` is already read with the Mapping-guard idiom at
  `jarvis/runtime/inherent_loop.py:1814-1815`, in the same function, above both
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
  It governs BOTH construction sites — the legacy path at `:1889` is not under
  `realtime.streaming_output` and must honour the same setting.
- The configured string is passed STRAIGHT THROUGH to
  `AudioStreamPlayer(device=...)`. `sounddevice` does the name-to-index
  resolution; Jarvis resolves nothing.
- Read once, next to the existing `realtime` read at `:1814-1815`, with the same
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
  (`tests/scenarios/test_live_crash_recovery.py:213-257`, `realtime` mutated at
  `:241-254`, dumped at `:256`).
  Read-only `SwitchAudioSource` calls stay: the `-a -t output` presence guard
  (skip when BlackHole is absent) and the `-c -t output` readings, which become
  this card's canary. The `_echo` at `:848` stops calling the value a restore
  target.

## Affected contracts and files
- config `config/jarvis.yaml:144-145` — new `realtime.output_device` key, absent
  or null by default, with a one-line comment that it names the TTS output
  device and that unset means the system default.
- runtime `jarvis/runtime/inherent_loop.py:_build_tts_pipeline` — one read at
  `:1814-1815`, two `device=` pass-throughs at `:1840-1845` and `:1889-1898`.
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
  `:1889` is not governed by that block and must honour the same setting.
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
- Pre-edit baseline on `lane/a` at `352f2d1` (after `git merge realtime-integration`,
  a clean fast-forward): `1047 passed, 64 deselected in 49.54s`. A first run of the
  same command printed `1 failed, 1046 passed` on
  `test_post_ingress_construction_failure_closes_capability_dispatcher[silero]`
  ("condition did not become true before bounded deadline"); that item passes
  alone (`2 passed in 0.03s`) and passed in every later full run — a timing flake
  under load, not a branch state.
- Hub-added slice, NOT part of this card's acceptance — boot playback reconciler
  gate widened — `cb4bafa` — the actor that produces a playback generation is
  gated on `realtime.streaming_output` while the boot reconciler was gated on
  `response_run_lifecycle`, so "streaming on, lifecycle off" was a legal config
  accumulating orphans no boot closed. The guard is gone; the fold appends
  nothing when no generation is open. Pinned by an AST test in
  `tests/integration/test_boot_playback_reconciliation.py` (the helper is named
  once inside `serve_inherent` and no `if` encloses it), verified failing with
  the guard temporarily restored. Suite 1048 / 64. ADR-0008 §4.4 already says
  "runtime asks L5 to close any active playback state" with no flag, so the
  change moves the code toward the ADR, not away.
- Config key + both pass-throughs + failure-reason string — `242c2f3` —
  `realtime.output_device` (null by default) read once at
  `inherent_loop.py:1814-1819` and forwarded to both `_build_tts_pipeline`
  player sites (`:1845`, `:1897`). No jarvis-side resolver was written: the
  string goes straight to `AudioStreamPlayer(device=...)` and `sounddevice`
  maps it inside `sd.OutputStream`. `voice_tts.py` names the device in the
  failed_closed reason. New hermetic tests: both sites receive
  `"BlackHole 16ch"` when configured and `None` when absent, with
  `_open_output_stream` patched so no device opens; the unresolvable name
  yields `status='failed_closed'` and a reason carrying the name. Suite 1050 / 64.
- Both live rigs converted, system-default switching deleted — `3a7d771` —
  `scripts/replay_barge_in.py` lost `_SPEAKER_FALLBACK`,
  `_current_output_device`, `_set_output_device`, the switch/collision guard,
  the restore `finally` and `import subprocess`; `--output-device` now reaches
  the daemon through a copied config tree (bootstrap derives Tier-0/grammar/cue
  paths from the config's parent and pricing from its grandparent; each of those
  loaders degrades to an empty table on a missing file, so a lone tmp yaml would
  boot but silently without Tier 0 — the copy is for fidelity, not for booting).
  The `silent_output_device` fixture keeps only
  read-only `SwitchAudioSource` calls and asserts the system route is identical
  before and after; `_build_overlay` sets `realtime.output_device`.
  `grep -n "SwitchAudioSource -s\|_set_output_device"` over both files: no hits.
  Suite 1050 / 64.
- Live burn — `1 passed in 29.01s`, root
  `~/.jarvis-lane-b-test/crash-20260905T230921Z`, port 55244, overlay line 76
  `output_device: BlackHole 16ch`. Warm-up spoke with
  `playback provider='minimax_ws_streaming'`; SIGKILL pid 37510 left orphan pair
  `(RESP5ec41e3027794c50b76e162c5a91ba59, 2)`; boot 2 logged both
  `boot reconciliation closed 1 open response run(s)` and
  `closed 1 open playback generation(s)`, `COUNT(*)` = 1, restart 3 appended
  nothing. `SwitchAudioSource -c -t output` = `'MacBook Pro Speakers'` BEFORE and
  `'MacBook Pro Speakers'` AFTER (fixture echo `unchanged=True`). The run never
  switched the system default, so the standing audio rule is satisfied by not
  switching; no restore trap was added because there is nothing to restore.
- Real-device canary (proves the name reaches CoreAudio, not just the kwarg):
  `AudioStreamPlayer(device='BlackHole 16ch').start()` →
  `status='started' reason='stream_started'` against real PortAudio, and
  `device='No Such Device'` → `status='failed_closed'
  reason="open:ValueError device='No Such Device'"`. System route
  `'MacBook Pro Speakers'` before and after.
- Docs to sync: `config/jarvis.yaml` — the key's comment is the documentation,
  written. `docs/spec.html` — unchanged: zero occurrences of `streaming_output`,
  no config-key inventory, and no sentence claiming the TTS stream always uses
  the system default (`grep -in "output device\|system default\|OutputStream"`
  finds nothing; the single `output_device` hit at `docs/spec.html:2197` is a
  WorldState Room slice field, a different fact under the same name).
  `docs/adr/` — unchanged: the only output-device mentions are
  ADR-0006:125/675/694/872 (input+output stream shape, prewarm, and F10
  device-CHANGE handling, an explicit non-goal here) and ADR-0014:1231; none
  states which device the stream opens, so no ADR owns this fact.
- Verifier pass (`verifier`, opus, fresh context, `352f2d1..HEAD`) — one
  CONFIRMED defect and two weak checks, all fixed in `9412700` and this commit:
  the deleted `_FALLBACK_DEVICE`'s docstring had dangled onto `_SILENT_DEVICE`,
  still describing the loopback as a restore target; the fail-closed test
  asserted no fallback without pinning it; and the `isinstance(str)` guard on
  the config read made a mistyped key degrade silently to the system default.
  The verifier independently re-ran the gates (1050 / 64, KEPT, clean, Success)
  and mutation-tested the new pins: deleting either `device=` pass-through, or
  restoring the reconciler gate, fails the corresponding test. Its two
  speculative notes are recorded, not acted on: the response reconciler at
  `inherent_loop.py:3613` keeps the symmetric `response_run_lifecycle` gate
  (out of scope, and reachable only by flipping the flag off between boots),
  and `--config` pointing at a directory that also holds a runtime root would
  make `replay_barge_in.py` copy that root into tmp (wasteful, not wrong).
- Live burn re-run on the final tip (`4b669cd`, after the verifier fixes changed
  the config read; intentionally not re-pinned to a later sha) — `1 passed in
  32.19s`, root
  `~/.jarvis-lane-b-test/crash-20260905T232547Z`, port 56714. Warm-up spoke with
  `playback provider='minimax_ws_streaming'`; SIGKILL pid 59044; boot 2 logged
  `boot reconciliation closed 1 open playback generation(s)`, `COUNT(*)` = 1,
  restart 3 appended nothing. `SwitchAudioSource -c -t output` =
  `'MacBook Pro Speakers'` BEFORE and AFTER, fixture echo `unchanged=True`.
- Owner follow-up, not a blocker: nothing in the burn is mic-in-the-loop, and
  the loopback route was verified by device open rather than by listening.
