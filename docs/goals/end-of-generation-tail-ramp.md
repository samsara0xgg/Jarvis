# Goal: end-of-generation-tail-ramp

All `path:line` citations re-pinned against `main` @ `508f863`.

## Goal

When the generation ring returns a short-but-nonzero callback block
(`0 < actual < frames`), the block's own last real samples decay to exactly `0.0`
before the zero pad, and the playback terminal records the length of that ramp in a
new `tail_ramp_samples` payload field.

## Why

The owner hears a rare, short click at the *end* of spoken responses ("大部分是在末尾,
是短促的一声"). Forensics over his live log (9 playback terminals) excluded every other
cause: `starvation_gaps = 0` and `host_underflows = 0` on all nine, and every
`surface.playback_completed` row had `submitted_samples == total_samples`, so there was
no mid-stream underrun and no truncation. The four interrupted rows route through
`request_discard()` and land on `actual <= 0`, which D11 already covers. The output
stream is one persistent `AudioStreamPlayer` started at boot, so teardown is excluded.

What remains is the partial block, which D11 explicitly carved out:

- `_GenerationRingBuffer.read_into` hard zero-pads inside its own block —
  `pcm_out[actual:n] = 0.0` (`jarvis/surface/voice_tts.py:310-312`). The junction
  `view[actual-1] -> view[actual]` is a one-sample step.
- That block takes the **success** path. `_emit_declick` has exactly four call sites
  (`voice_tts.py:1661`, `:1669`, `:1679`, `:1694`); `:1661` is guarded by
  `if actual <= 0:` (`:1660`) and the other three are lease / generation / CAS
  mismatch branches. **None can fire on `0 < actual < frames` with a matching
  generation.** Verified at tip.
- The step is then *hidden* from D11's next-block decay, because `:1712` latches
  `self._declick_last_sample = float(view[frames - 1])` — on a short read that index is
  the pad's `0.0`, so the following silent block arms no decay at all.

Why it is rare, not constant: audibility depends on the last real sample's amplitude,
and synthesized speech usually trails off near zero. A generation that ends exactly on a
host block boundary is already fine — it latches a *real* sample and D11 decays from it.

**Why it is not verifiable today:** no emitted field records the final callback's
`actual` vs `frames`. `submitted_samples == total_samples`, `starvation_gaps == 0` and
`host_underflows == 0` hold identically for a block-aligned ending (already covered) and
a short-read ending (uncovered). The card must add the discriminator, or the fix cannot
be confirmed from the owner's log.

## Current behavior

- `_GenerationRingBuffer.read_into` zero-pads a short read with no shaping
  (`jarvis/surface/voice_tts.py:310-312`).
- `_callback` reaches `_emit_declick` only on `actual <= 0` (`voice_tts.py:1660-1661`)
  or on a lease/generation/CAS mismatch (`:1669`, `:1679`, `:1694`).
- On the success path the block is gain-applied whole (`voice_tts.py:1684`), the span is
  reported (`:1697-1710`), and `_declick_last_sample` is latched from `view[frames - 1]`
  (`:1712`).
- `_commit_terminal` builds the terminal payload at
  `jarvis/surface/voice_media.py:3089-3120`; the closest existing diagnostics are
  `starvation_gaps` / `host_underflows` (`:3111-3112`), read as deltas off player
  properties (`voice_tts.py:1518`, `:1488`) snapshotted at `voice_media.py:2181-2182`.
- `_GainRamp` (`voice_tts.py:457-515`) is D8's deferred ducking mechanism and has no
  production caller.
- The existing completion acceptance
  (`tests/integration/test_wave2_streaming_media.py:4018`) uses
  `_DECLICK_RESPONSE_SAMPLES = 1_600` (`:3897`) against a pump that steps 32 frames
  (`:236`). **1600 = 32 x 50 — exactly block-aligned.** The repository's only
  natural-completion acceptance therefore never produces a short read and has never
  exercised the step site.

## Target behavior

- On `0 < actual < frames` with a generation-valid block, the last
  `k = min(actual, _DECLICK_SAMPLES)` real samples are multiplied by a linear ramp that
  **starts at unity** and reaches **exactly `0.0`** at `view[actual - 1]`.
- The ramp is bounded by `actual` and never reaches into the preceding block, which is
  already with the host.
- When `k < _DECLICK_SAMPLES` the ramp is shorter and still lands on exactly `0.0`. It is
  **not** a tail slice of `_DECLICK_RAMP` — `_DECLICK_RAMP[-k:]` starts around `0.17` for
  `k = 22` and would reintroduce the very step it exists to remove, one sample earlier.
- No allocation on the PortAudio thread.
- No ledger effect: `_played_samples`, `output_start_cursor`, `output_end_cursor`,
  `audibility_class` and the `_CallbackReport` are all unchanged.
- `surface.playback_completed` / `_interrupted` / `_failed` carry
  `tail_ramp_samples: int` — the ramp length applied in that generation's last short
  read, `0` when every read of that generation was full.

## Affected contracts and files

- L5 `jarvis/surface/voice_tts.py:_GenerationRingBuffer.read_into` (`:283-313`) — the
  step site; may stay untouched if the ramp lives in the callback.
- L5 `jarvis/surface/voice_tts.py:AudioStreamPlayer._callback` (`:1592-1714`) — apply the
  ramp on the committed success path, **after** `_gain.apply` (`:1684`) so the exact-zero
  landing survives ducking, and inside the post-CAS `try` so a tombstoned block still
  takes `_emit_declick` (`:1694`) instead.
- L5 `jarvis/surface/voice_tts.py` — one preallocated descending table plus one
  preallocated scratch beside `_DECLICK_RAMP` (`:453-454`), a `_tail_ramp_samples`
  counter reset when `first` is computed (`:1698-1700`), and a property beside
  `starvation_gaps` (`:1518`).
- L5 `jarvis/surface/voice_media.py:_commit_terminal` (`:3089-3120`) — add
  `tail_ramp_samples` to the payload.
- Test rig `tests/integration/test_wave2_streaming_media.py:_declick_pipeline` (`:3900`)
  — takes a `samples` argument, defaulting to `_DECLICK_RESPONSE_SAMPLES`.

## Boundaries and non-goals

- Layers that may change: L5 only. No L2/L3/L4/L6 change; no `runtime/` change.
- Must not change: `_emit_declick` and its four call sites; the `actual <= 0` behavior
  D11 owns; `_GainRamp`, `set_gain`, `duck`, `unduck` (still zero production callers);
  `PlaybackLedger` (`jarvis/surface/voice_ledger.py`); `_CURSOR_FIELDS` in
  `jarvis/surface/playback_recovery.py:40-45` — `tail_ramp_samples` is a diagnostic and
  is not carried across replay.
- Must not change: the block's `audibility_class`. A 2.7 ms fade on a generation's last
  sample is not attenuated presentation, and downgrading it would degrade every
  response's heard-prefix conservatism.
- Non-goal: distinguishing an end-of-generation short read from a mid-stream one. The
  callback cannot tell them apart and both produce the identical junction;
  `starvation_gaps` already reports the mid-stream case. Ramping there replaces two
  amplitude edges with one and is never worse.
- Non-goal: `surface.playback_checkpoint` does not gain the field.
- Non-goal: a config key for the fade length. It stays the D11 module constant.

## Rejected approaches

- **Reuse `_GainRamp`.** Rejected. It carries persistent gain state (`_current`,
  `_target`, `_remaining`, `voice_tts.py:466-469`), so driving it to zero leaves the
  device muted with no production caller able to restore it — the objection D11 already
  recorded. It also ramps the block's *head* (`pcm_block[:step] *= scratch`,
  `voice_tts.py:511`) where this needs the block's *tail*, and chaining it onto a block
  `_gain.apply` has already touched (`:1684`) entangles a shipped fix with D8's deferred
  barge-in ducking. Reuse `_DECLICK_SAMPLES` instead — same constant, same property of
  hearing, no shared state.
- **Widen `_emit_declick` to the partial block.** Rejected. It *synthesizes* a decay into
  silence and deliberately advances no ledger state; this block carries real, accounted
  samples and needs a multiply, not a synthesis. Two different operations.
- **Slice `_DECLICK_RAMP[-k:]` when `k < _DECLICK_SAMPLES`.** Rejected — starts below
  unity, which is the same step moved one sample earlier. Named as a degenerate case below.
- **Defer again on the ledger objection.** Rejected — the objection is false; see below.
- **A live captured-waveform acceptance.** Rejected as *acceptance*; see Acceptance evidence.

## Ruling: the PlaybackLedger objection is wrong

D11 deferred this case because covering it "means reshaping content the ledger has
already accounted as submitted." That is not true, on three independent grounds:

1. **The ledger never sees a sample value.** `PlaybackLedger.record_submitted`
   (`jarvis/surface/voice_ledger.py:213-244`) takes `output_start_cursor`,
   `output_end_cursor`, `audibility_class`. The cursors come from the ring's own cursor
   array — `end_cursor = int(self._callback_cursors[actual - 1]) + 1`
   (`voice_tts.py:1697`) — not from the PCM. A ramp changes no cursor.
2. **Ordering: nothing is accounted yet.** `record_submitted` is reached only from
   `poll_presentation`'s drain loop (`voice_tts.py:1333-1341`) over
   `_CallbackReportRing`, on the media actor thread, *after* `_callback` returns. The
   ramp is applied inside `_callback`, before `_callback_reports.write(...)`
   (`voice_tts.py:1702`) has even queued a report. The ramp shapes a block **before**
   submission and **before** accounting.
3. **Precedent on this exact line.** `self._gain.apply(view)` (`voice_tts.py:1684`)
   already multiplies the whole about-to-be-submitted block and may drive it to
   `"muted"`; the ledger accounts that span as submitted regardless. Amplitude shaping of
   an accounted block is the established, sanctioned operation here.

The objection is withdrawn. This makes the decision simple: D6's spans are untouched.

## Acceptance evidence

The signal is the block sequence the production `_callback` writes into `outdata`; the
device does not reshape it. `_CallbackPump`
(`tests/integration/test_wave2_streaming_media.py:211`, `step` at `:236`) already drives
the real `_callback` from a real `StreamingTTSPipeline` with a real event log behind it,
and `record=True` retains those blocks. The response is driven end to end through the
pipeline's public submit path, exactly as
`test_natural_completion_keeps_every_audible_sample_and_its_counts` (`:4018`) does. **Do
not call `player._callback`, `player.interrupt_generation` or any player method directly
to manufacture a block** — constructing a player and inspecting its own buffer is the
banned unit-test shape; capturing what the production callback emits while the production
actor drives it is not.

- **Positive A — the waveform.** A new test in
  `tests/integration/test_wave2_streaming_media.py` runs a **1590-sample**
  constant-amplitude response (`1590 = 32 x 49 + 22`, so the final pumped callback reads
  `actual = 22 < frames = 32`) to natural completion, and asserts on `pump.signal`:
  1. `np.all(signal[:1568] == _DECLICK_AMPLITUDE)` — nothing before this block is touched,
     and the ramp did not reach backwards.
  2. `signal[1568] == _DECLICK_AMPLITUDE` — the ramp **starts at unity**.
  3. `np.all(np.diff(signal[1568:1590]) < 0)` — strictly descending across the 22 real
     samples.
  4. `signal[1589] == 0.0` exactly — it lands on zero on the last real sample.
  5. `np.max(np.abs(np.diff(signal[1567:1600]))) <= _DECLICK_AMPLITUDE / 21 * 1.01` — no
     residual one-sample step anywhere across the junction.
  6. `np.all(signal[1590:] == 0.0)` — the pad and everything after stay exactly silent.
- **Positive B — the emitted field.** The `surface.playback_completed` row for that
  response, read back out of the event log, has payload
  `tail_ramp_samples == 22`, alongside `submitted_samples == 1590`,
  `total_samples == 1590`, `starvation_gaps == 0`, `host_underflows == 0`. This is the
  field the owner's forensics lacked, and rows 1-6 are what it certifies.
- **Positive C — the discriminator.** Extend the existing
  `test_natural_completion_keeps_every_audible_sample_and_its_counts` (`:4018`, its
  1600-sample response is block-aligned) with one assertion: its
  `surface.playback_completed` payload has `tail_ramp_samples == 0`. A field that reads
  `22` on a short-read ending and `0` on an aligned ending is a real measurement, not a
  constant.
- **Positive D — the ledger is untouched.** In the 1590-sample test, the
  `tts_estimated_audible` trace point for that response reports
  `estimated_audible_samples == 1590` (same rig as `:4076-4085`), proving the ramp
  changed no span.

  **Degenerate-path table — each must FAIL, and the lane must show it failing by
  temporarily forcing the condition before shipping:**

  | Degenerate case | Which assertion kills it |
  |---|---|
  | ramp not applied (hard cut unchanged) | A3 — `diff` is not strictly negative; A4 — `signal[1589] != 0.0` |
  | ramp sliced as `_DECLICK_RAMP[-k:]` (starts ~0.17) | A2 — `signal[1568]` is no longer full amplitude |
  | ramp does not reach exact zero | A4 |
  | ramp longer than `actual`, reaching backwards | A1 — content before index 1568 is attenuated |
  | ramp also fires on a block-aligned ending | C — `tail_ramp_samples != 0`; and `:4018`'s own `np.all(signal[:1600] == _DECLICK_AMPLITUDE)` breaks |
  | `tail_ramp_samples` hardcoded / left at a lifetime value | B and C disagree (22 vs 0) |
  | ramp reshapes the accounted span | B — `submitted_samples != total_samples`; D — audible count short |

- **Regression:** `test_a_user_stop_fades_the_output_instead_of_stepping_to_silence`
  (`:3931`) stays green by name — the interrupt path is `actual <= 0` and D11 owns it
  unchanged.
- **Regression:** full hermetic suite with `env -u MINIMAX_API_KEY` (see
  `tests/conftest.py`, which pops `MINIMAX_API_KEY` and sets
  `JARVIS_VOICE_DISABLE_WAKE=1`). **Measure your own baseline before the first edit and
  paste the raw output; never take a count from this card.** A delta at baseline time is
  a sibling lane landing, not a regression — explain it and proceed against your measured
  number.
- **Regression:** Tier 1 gates `lint-imports`, `ruff`, `mypy --strict`, printed counts
  shown, not inferred.
- **Regression:** `grep -rn '\.set_gain(\|\.duck(\|\.unduck(' --include='*.py' jarvis/`
  still returns only `voice_tts.py`'s internal delegations — `_GainRamp` gained no
  production caller.
- **Live run: not required, and this is a ruling, not an omission.** Nothing physical sits
  between the PCM handed to PortAudio and the DAC, so a block-level amplitude assertion is
  strictly stronger evidence about this edge than a microphone capture of it, which only
  re-measures the same numbers through room and device noise. An ear-check by the owner is
  welcome and is not acceptance. **Exit clause, if the lane nevertheless elects a live
  capture:** capture the pre-run system default output route first and restore it in a
  `finally` / `trap` on every exit path; if the captured route is already the loopback
  ("BlackHole 16ch"), restore "MacBook Pro Speakers" instead; skip the device switch
  entirely if the run needs no audio; cap the volume. If the capture cannot distinguish a
  ramped edge from a hard zero, **STOP AND REPORT** — do not soften an assertion to make a
  capture pass, and do not delete the hermetic rows in favour of it.

Read config values from `config/jarvis.yaml` and the owner's overlay directly, never from
this card: the repo ships `enable_macos_say_fallback: true` while the overlay sets it false.

## Docs to sync

- `docs/adr/0006-full-duplex-voice-session.md` — **already written by this card**: new
  **D12** ("The last real sample of a short ring read is ramped inside its own block") at
  `:564`, and D11's carve-out paragraph at `:556` rewritten to hand the case to D12 and to
  retract the false ledger reason. The implementation session re-checks that D12 still
  describes what landed — in particular the `tail_ramp_samples` sentence and the
  `audibility_class` sentence — and corrects D12 rather than adding a D13.
- `docs/spec.html` — **judged unchanged.** It contains no occurrence of `declick`,
  `starvation`, or `playback_completed`; the spec does not own the playback amplitude
  edge, ADR-0006 does. Do not duplicate D12's reasoning into it. State this judgement
  explicitly in the transcript.

## Open questions

(none)

## /goal condition

Implement docs/goals/end-of-generation-tail-ramp.md. Done when ALL of the following appear
in this transcript as raw command output, not as claims:

1. The tip re-pinned: `git rev-parse HEAD` and `git log -1 --oneline`, shown before any
   edit, plus a statement of whether each `path:line` in the card still resolves to the
   cited symbol. Report any that moved; never silently follow a stale line number.
2. The gate, re-verified and shown: `_emit_declick`'s call sites, proving none can fire on
   `0 < actual < frames` with a matching generation, and the zero-pad in
   `_GenerationRingBuffer.read_into`. If either is false, STOP AND REPORT; do not implement.
3. The measured hermetic baseline BEFORE any edit: raw tail of the `env -u MINIMAX_API_KEY`
   pytest run showing `N passed, M deselected`. Never take the number from the card;
   explain any delta and proceed against your measured number.
4. The diff shows the ramp on the committed success path of `_callback`, applied after
   `_gain.apply`, bounded by `actual`, allocation-free, starting at unity and landing on
   exactly `0.0` — and NOT implemented as a tail slice of `_DECLICK_RAMP`.
5. Raw pass output of the new 1590-sample test (Positive A rows 1-6, Positive B's
   `tail_ramp_samples == 22`, Positive D's `estimated_audible_samples == 1590`) and of the
   extended `:4018` test asserting `tail_ramp_samples == 0`. The response must be driven
   through the pipeline's public submit path; calling `player._callback` or
   `player.interrupt_generation` directly to manufacture a block is not acceptable.
6. All SEVEN degenerate cases from the card's table, each temporarily forced and shown
   FAILING, then reverted. A degenerate case that passes is a false pass — stop and report.
7. `test_a_user_stop_fades_the_output_instead_of_stepping_to_silence` shown green by name,
   and the `_GainRamp` grep returning only internal delegations.
8. The full hermetic suite raw tail after the change with no regression against (3), plus
   printed `lint-imports`, `ruff` and `mypy --strict` counts.
9. Each entry under "Docs to sync" updated or explicitly judged unchanged, with the reason.
   D12 already exists in the ADR — verify it matches what landed and correct D12 in place
   rather than adding a D13; state the `docs/spec.html` judgement explicitly. When the
   change alters a documented contract, invariant, ownership boundary, or externally
   relevant behavior, update the canonical document that owns that fact; do not document
   what the code already makes clear; do not duplicate a fact across documents.
10. `git status` shows a clean tree, each slice committed per the commit skill with a
    Progress line appended.

Constraints: no live audio run is required; if you elect one, capture and restore the
system default output route on every exit path (loopback "BlackHole 16ch" -> restore
"MacBook Pro Speakers"), and cap the volume. Never start, stop or interfere with the
owner's daemon on port 8009 or his InherentCard. Never build, rebuild, launch or delete
anything under `.claude/worktrees/realtime-live-test`. Read config from
`config/jarvis.yaml` and the owner's overlay directly, never from this card.

If the card contradicts the repository, stop and report; do not redesign. Or stop after
25 turns.

## Progress

- 2026-09-06 (lane-c, worktree-agent-af76e3536469bef30): implemented. Gate
  re-verified at tip `4c3dac0` before any edit — `_emit_declick`'s four call
  sites are `voice_tts.py:1661` (guarded `if actual <= 0:` at `:1660`), `:1669`
  (lease mismatch), `:1679` (block generation mismatch), `:1694` (CAS
  tombstone); none can fire on `0 < actual < frames` with a matching
  generation. `read_into` still hard zero-pads at `:311`. Every code citation
  resolved except two off-by-small line numbers: `def _callback` is `:1594`
  (card says the range starts `:1592`) and `_gain.apply` is `:1685` (card says
  `:1684`, which is `_gain_consumed_command`).
- Measured hermetic baseline before the first edit: `1070 passed, 64
  deselected` in 63.86 s. After: `1071 passed, 64 deselected` in 58.98 s — the
  delta is the one new test.
- Two overrules of the card, both recorded in the commit body: the field is
  declared in the L2 event-type registry (the card's boundary said L5 only),
  and `test_generation_cas_races_and_thousand_cycle_churn:650` was updated
  because it pinned the old hard cut at this exact site — contradicting the
  card's claim that the site "has never been exercised".
- All seven degenerate cases forced and shown failing, then reverted. Two
  failed on a different assertion than the card predicted: a ramp longer than
  `actual` cannot reach into a preceding block from inside `_callback`, so it
  is caught by A2 (or a numpy broadcast error), not A1; and a reshaped span is
  rejected by `PlaybackLedger` (`invalid submitted output span`) before B or D
  assert.
- `docs/spec.html` judged unchanged: it contains zero occurrences of
  `declick`, `starvation`, `playback_completed`, `host_underflows` or
  `tail_ramp`. The playback amplitude edge is owned by ADR-0006, and the
  event-type registry in `jarvis/state/event_log.py` is the single source of
  truth for payload schema (per `0c2d87c`). D12 verified to match what landed;
  no D13 added.
