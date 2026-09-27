---
status: blocked
owner_lane: a
depends_on: []
---
# Goal: tts-segment-junction-continuity

## Goal

One resampler span covers a whole playback generation, so the canonical PCM
handed to the player is continuous across every provider segment junction
instead of dipping to silence at each one.

## Why

The owner reports an audible click that scales with the length of the answer:
one short click at the end of a short response, and repeated clicks spread
through a long one, "not only at the end, all the way to the end". The three
click mechanisms already fixed (ADR-0006 D11 interrupt declick, D12
end-of-generation tail ramp, and the MiniMax `vol` 5 to 3 headroom fix) are all
present and unreverted on the running build, and none of them can reach a
segment junction inside a continuously fed ring. The count of junctions equals
the count of segments, which is exactly the scaling the owner describes.

## Current behavior

- `_SegmentResampler` is constructed fresh for every provider segment, inside
  the per-segment provider loop, keyed on `resampler is None`
  (`jarvis/surface/voice_media.py:2426-2430`).
- `_SegmentResampler.finish()` flushes the soxr stream with
  `resample_chunk(np.zeros(0), last=True)` and marks itself closed; it is
  called once per segment and its output is written into the ring
  (`jarvis/surface/voice_media.py:574-586`, written at
  `jarvis/surface/voice_media.py:2452-2459`).
- The resampler is always engaged in production: `tts_sample_rate_in_hz: 32000`
  (`config/jarvis.yaml:194`) against
  `StreamingMediaConfig.canonical_sample_rate_hz: int = 48_000`
  (`jarvis/surface/voice_media.py:228`).
- Consecutive segments are written back to back into one persistent
  `sounddevice.OutputStream` (`jarvis/surface/voice_tts.py:539-565`, opened
  once at `jarvis/surface/voice_tts.py:748`). No padding, no crossfade and no
  zero-crossing alignment exists between segments.
- Consequence at each junction: the closing segment's `last=True` flush emits
  the filter's response to zero-padded input, so its tail decays toward
  silence; the next segment's fresh `soxr.ResampleStream` has no filter
  history, so its opening output samples are convolved against implicit zeros
  and rise from silence. The source audio is continuous across that boundary,
  but the delivered audio contains a short amplitude notch there.
- Neither existing declick covers it. `_emit_declick`
  (`jarvis/surface/voice_tts.py:1580-1612`) fires only when a callback block
  would present a fully silent block after audio. `_TAIL_RAMP_STEPS`
  (`jarvis/surface/voice_tts.py:1722-1741`) fires only on a short ring read
  (`0 < actual < frames`). A segment junction in a continuously fed ring
  produces neither a silent block nor a short read.
- Live evidence, 2026-09-07 (`~/.jarvis-allen-test/mac_events.db`): turn
  `Tf20062d0`, `surface.playback_completed` at 19:52:26 -0700, carries
  `heard_through_sequence: 13`, `submitted_samples: 3141945`,
  `total_samples: 3141945`, `host_underflows: 0`, `starvation_gaps: 0`,
  `tail_ramp_samples: 57`, `provider: minimax_ws_streaming`. Thirteen segments,
  thirteen resampler spans, no starvation and no underrun; the owner heard
  clicks throughout that answer. Turn `Tf2358165` in the same window
  (`surface.playback_completed` 19:51:08 -0700) was a single segment
  (`heard_through_sequence: 0`) and the owner heard one short click at the end.
- The running daemon is the current source: `ps` shows
  `/Users/alllllenshi/Projects/jarvis/.venv/bin/python -m jarvis serve
  --runtime-root /Users/alllllenshi/.jarvis-allen-test ... --port 8009`, the
  main checkout is at `e0c1734` with no modified Python sources, and the
  runtime overlay sets no `tts_volume` override. This is not a regression and
  not a stale build.

## Target behavior

- A generation's canonical PCM is produced by one resampler span. The span is
  created when the generation's first provider chunk arrives and is flushed
  once, at the generation's last segment, not at each segment.
- A new span begins only when continuity is genuinely broken: the playback
  generation changes, a discard or interrupt boundary is consumed, the
  provider's `event.sample_rate_hz` differs from the rate the live span was
  built for, or the segment stream is restarted on a different endpoint after
  a provider failure. In each of those cases the previous span is abandoned
  rather than flushed into the ring of the new generation.
- `finish_generation_segment` continues to close each segment's ledger span
  exactly as it does today. Only the resampler flush moves.
- Playback terminals carry `resampler_spans`: the number of resampler spans
  used to produce that generation's canonical PCM. It is `1` for a generation
  whose provider rate never changed and that was not restarted, whatever its
  segment count. Without it, a terminal cannot distinguish a continuous
  generation from a segmented one: `submitted_samples == total_samples`,
  `starvation_gaps == 0` and `host_underflows == 0` hold in both cases, which
  is why the owner's event log could not localise these clicks.

## Affected contracts and files

- L5 `jarvis/surface/voice_media.py:_SegmentResampler` — lifetime and name move
  from segment to generation; its docstring at `:538` and the "per-segment
  canonical resampler" comment at `jarvis/surface/voice_tts.py:1809` stop
  describing it as per-segment.
- L5 `jarvis/surface/voice_media.py` per-segment provider loop (~`:2413-2470`)
  — the construction and the `finish()` call move out of the segment scope.
- L5 `jarvis/surface/voice_media.py` terminal payload (`_commit_terminal` and
  its callers, ~`:2822`, `:2831`, `:2987`, `:3094`, `:3120`) — carries
  `resampler_spans`.
- L2 `jarvis/state/event_log.py` — registers `resampler_spans` on the same
  event types that already carry `tail_ramp_samples`.
- `docs/adr/0006-full-duplex-voice-session.md` — a D13 entry after D12.

## Boundaries and non-goals

- Layers that may change: L5 surface, and L2 state for the field registration
  only, mirroring how `tail_ramp_samples` was registered.
- Must not change: `_emit_declick` / `_DECLICK_SAMPLES` / `_DECLICK_RAMP`
  (D11); `_TAIL_RAMP_STEPS` and the `tail_ramp_samples` field (D12);
  `volume: int = 3` (`jarvis/surface/voice_tts.py:1914`);
  `canonical_sample_rate_hz = 48000`; `tts_sample_rate_in_hz`; the
  one-`OutputStream`-per-pipeline lifetime; interrupt discard semantics
  (`request_discard`, `interrupt_generation`); the ledger's submitted and
  audible span accounting.
- Non-goals: a fade-in symmetric to `_emit_declick` for audio resuming from
  silence (no evidence it fired — `starvation_gaps: 0`); the system-volume
  pops in other applications (a separate defect with a separate root cause in
  `jarvis/surface/voice_ducking.py`, not this card); barge-in `_GainRamp`
  ducking, which ADR-0006 D8 keeps deferred and unbuilt; changing the TTS
  provider, voice, model or request rate.

## Rejected approaches

- Crossfade adjacent segments inside the ring — needs lookahead into the next
  segment, which the streaming path does not have, and it would attenuate real
  content. The notch is an artifact of resampler state, not of the source
  material, so carrying the filter state across the junction is both the
  correct fix and the smaller one.
- Extend D12's tail ramp to every segment — that converts each junction from a
  notch into a deliberate gap. The source audio is continuous across a
  junction and must stay continuous.
- Set `tts_sample_rate_in_hz` equal to `canonical_sample_rate_hz` so the
  resampler is never constructed — this only masks the defect for one provider
  and rate combination and leaves the junction bug in place for every other
  one, and it changes the provider request shape, which this card must not do.
- Reuse `_GainRamp` — it carries persistent gain state and has no production
  caller, and ADR-0006 D11 and D12 both record why it is unusable on this path.

## Acceptance evidence

- Baseline: before the change, replay or observe one generation of at least
  eight segments and record what `resampler_spans` would have been. Report the
  number in Progress. It is expected to equal the segment count.
- Positive, hermetic: `PYTHONPATH=. /Users/alllllenshi/Projects/jarvis/.venv/bin/python -m pytest -q -m "not live_llm and not live_codex"`
  passes, and the run includes a check that drives the streaming pipeline
  across at least three provider segments at a provider rate different from
  the canonical rate and asserts the emitted `surface.playback_completed`
  payload carries `resampler_spans: 1` while `heard_through_sequence` is at
  least `2`.
- Positive, live: required. Allen asks a question whose answer spans at least
  eight segments. The resulting `surface.playback_completed` in
  `~/.jarvis-allen-test/mac_events.db` carries `resampler_spans: 1`,
  `heard_through_sequence >= 8`, `host_underflows: 0`, `starvation_gaps: 0`,
  and Allen confirms he hears no clicks during that answer. The canary is
  `resampler_spans: 1` against a `heard_through_sequence` of at least `8`.
- Regression: a single-segment generation still emits a terminal with
  `tail_ramp_samples > 0` where it did before, proving D12 still fires; an
  interrupted generation still emits `surface.playback_interrupted` and still
  discards its queued PCM.
- Regression gates, all with `PYTHONPATH=.` and the main checkout venv at
  `/Users/alllllenshi/Projects/jarvis/.venv/bin/`: `lint-imports`,
  `ruff check .` (must be ruff 0.15.13 from that venv, not a fresh worktree
  venv), `mypy --strict jarvis tests scripts tools`, and the pytest command
  above. Swift is untouched, so `scripts/test_inherent_swift.sh` is not
  required.

## Docs to sync

- `docs/adr/0006-full-duplex-voice-session.md` — add D13 after D12: the
  resampler span is a property of a generation, not of a segment; why D11 and
  D12 cannot reach a junction inside a continuously fed ring; what
  `resampler_spans` is for and when it is legitimately greater than `1`.
- `docs/spec.html` — only if a section states that canonical resampling is
  per segment. If no section owns that fact, record that it was judged
  unchanged rather than adding one.

## Open questions

(empty)

## /goal condition

Implement docs/goals/tts-segment-junction-continuity.md. Read it fully before
touching code. Stop when all of the following are shown in this transcript:

1. `git status` shows a clean tree on branch `lane/a`, and `git log --oneline`
   shows each slice committed with a Conventional Commit subject and no
   `Co-Authored-By` trailer.
2. The raw output of `PYTHONPATH=. /Users/alllllenshi/Projects/jarvis/.venv/bin/python -m pytest -q -m "not live_llm and not live_codex"`
   is shown and reports no failures, and the transcript names the specific
   check that asserts `resampler_spans: 1` on an emitted
   `surface.playback_completed` payload for a generation of at least two
   segments produced at a provider rate different from the canonical rate.
3. The raw final lines of `lint-imports`, `ruff check .` and
   `mypy --strict jarvis tests scripts tools` are shown, each run with
   `PYTHONPATH=.` and the interpreter and ruff from
   `/Users/alllllenshi/Projects/jarvis/.venv/bin/`, and each is clean. If ruff
   reports on the order of 285 errors on a clean tree you are running 0.16.6
   from a worktree venv; use the main checkout venv instead and say so.
4. The pre-change baseline value of `resampler_spans` for a multi-segment
   generation is stated, together with the post-change value, and the Progress
   section of the card records both.
5. The regression evidence is shown: a single-segment generation still carries
   `tail_ramp_samples > 0`, and an interrupted generation still emits
   `surface.playback_interrupted`.
6. Each entry under "Docs to sync" was either updated or explicitly judged
   unchanged, with the judgement stated. When the implementation changes a
   documented contract, invariant, ownership boundary, or externally relevant
   behavior, update the canonical document that owns that fact; do not
   document what the code already makes clear; do not duplicate a fact across
   documents.
7. The live run is NOT yours to perform. State plainly that the live
   acceptance (Allen asking an answer of at least eight segments and
   confirming no clicks, with `resampler_spans: 1` in the event log) remains
   outstanding, and write a report to
   `$(git rev-parse --git-common-dir)/claude-harness/inbox/tts-segment-junction-continuity.md`
   naming the branch, the commit range, the gate tails and that one
   outstanding item. Never stop, restart or rebuild the daemon on port 8009.

Or stop after 20 turns.

## Progress

- (not started)
- 2026-09-07, blocked. The owner's hand test refutes this card's premise. In a
  43s answer with 7 provider segments he counted 29 pops, roughly one every
  1.4s; a short single-segment answer produced one pop. The junction theory
  predicts a pop count equal to the segment count, so the dominant mechanism
  produces pops *inside* a segment, not at its boundaries. Separately, E1 (the
  exact `osascript` mute/unmute pair `jarvis/surface/voice_ducking.py` runs)
  produced no pop on his machine, ruling the ducker out for the other-apps
  symptom. Work returns to the diagnosis branch: a measurement that localises
  the pop in the signal chain must come before any fix card. This card is not
  wrong about the junction notch existing; it is wrong that the notch is what
  the owner hears. Revisit only if the diagnosis puts the artifact at segment
  junctions.
