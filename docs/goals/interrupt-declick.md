# Goal: interrupt-declick

> **Citations pinned at `realtime-integration` tip `f047690`, re-verified.** Every line
> number below was read at `04cbf93` ("Merge branch 'lane/a' into realtime-integration",
> 2026-09-06 10:34:27 -0700) — the merge that touched `voice_tts.py` / `voice_media.py` /
> `playback_recovery.py`. The branch then advanced to `f047690` during drafting;
> `git diff --stat 04cbf93..f047690` over every cited file is **empty** (the two new
> commits are `79f56f1` docs and `f047690` `.gitignore`), so all citations still resolve.
>
> **The lane MUST re-pin every citation against the tip at launch anyway** —
> `realtime-integration` moves under lanes, and a two-dot diff against a stale base shows
> sibling lanes inverted (use three dots).

## Goal

Interrupting playback fades the output to silence over a few milliseconds instead of
stepping from full amplitude to zero in one sample, so a barge-in, stop, sleep,
supersede, shutdown, or failure no longer produces an audible click.

## Why

The owner authorized this on 2026-09-06, choosing "add a few-millisecond fade" over
leaving it and over measuring first.

This needs a **new recorded decision in ADR-0006**. It must NOT be filed under the
existing D8 deferral: `docs/adr/0006-full-duplex-voice-session.md:439-441` defers
"Phase 1's `duck_gain` ramp and F11's unduck" — that is barge-in **ducking**
(attenuate-while-listening, then restore). D8 is silent on the amplitude shape at the
moment of the cut. Declick and ducking are separate decisions; the owner authorized
only the first.

## Current behavior

### Read this first: the click is not at the site it looks like it is

**The ordinary interrupt click is emitted at `jarvis/surface/voice_tts.py:1608`, the
`if actual <= 0: return` early exit — not at any of the three `view[:actual] = 0.0`
sites (`:1616`, `:1626`, `:1641`).**

Those three sites are real, but each is a **one-block race**. They fire only for a
callback that straddles `_active_lease = None` (`:1227`) before `request_discard()`
(`:1230`) lands, and only if its `read_into` beat the discard.

The guaranteed path is different. `request_discard` sets
`_discard_before_idx = _write_idx` (`:318`); the next `_GenerationRingBuffer.read_into`
advances `_read_idx = max(_read_idx, _discard_before_idx)` (`:291`), finds
`available == 0`, `actual == 0`, and **zeroes the entire callback block itself** at
`pcm_out[actual:n] = 0.0` (`:310-311`). `_callback` then returns at `:1608` with a fully
zeroed buffer, having touched none of the three sites. Every block from then until the
next generation is this one.

So a fix confined to the three `view[:actual] = 0.0` sites **fixes nothing on the common
path**, while looking correct and reviewing clean. That is recorded here, in the card
body, because the wrong fix is the intuitive one and a future reader will reach for it.
The shared site that must carry the decay is the point where the callback is about to
hand PortAudio silence after audio was playing — which means `:1608` **as well as**
`:1616` / `:1626` / `:1641`.

### Supporting facts, verified by reading

- Every interrupt path converges on **one** function:
  `jarvis/surface/voice_media.py:3029 _interrupt_snapshot`, whose first statement is
  `self._player.interrupt_generation(...)`. It has exactly two callers:
  `_interrupt_active` (`voice_media.py:2866`) and `_fail_active` (`voice_media.py:2832`).
- `_interrupt_active` has **five** call sites, not the four the brief implies:
  - `voice_media.py:1445` — `reason="system_sleep"`, in `_suspend_for_sleep_owned` (:1442)
  - `voice_media.py:1473` — `reason="user_stop"`, in `_stop_foreground_output_owned` (:1458)
  - `voice_media.py:2012` — in `_response_terminal` (:1995)
  - `voice_media.py:2153` — `reason="foreground_superseded"`, in `_schedule_response` (:2061)
  - `voice_media.py:3257` — `reason="media_owner_shutdown"`
  With `_fail_active` that is **six** distinct entry points.
- **`voice_media.py:1473` (`user_stop`) is the path the owner will actually trigger**,
  since stop-speaking just shipped. Its acceptance matters most, and it is the entry
  point the positive test must drive.
- `AudioStreamPlayer.interrupt_generation` (`jarvis/surface/voice_tts.py:1209`) does two
  things that matter here, in this order: `self._active_lease = None` (:1227), then
  `self._generation_ring.request_discard()` (:1230) under `_write_lock`. This ordering is
  what produces the race/guaranteed split described above.
- `_GenerationRingBuffer` is at `voice_tts.py:219`, `read_into` at `:283`,
  `request_discard` at `:316`. `_callback` is at `voice_tts.py:1542`.
- `_GainRamp` (`voice_tts.py:446`, `set_target` :478, `apply` :484) is confirmed **dead**
  in production: reachable only via `AudioStreamPlayer.set_gain` (:1448) / `duck` (:1453)
  / `unduck` (:1457) — note `unduck`, not `undock` — and the sole repo-wide caller of any
  of the three is `tests/integration/test_wave2_streaming_media.py:938`.
  (`voice_wake.py:447`'s `self._ducker.duck()` is `SystemAudioDucker` in
  `jarvis/surface/voice_ducking.py` — a different object, system-level, not the player.)
- Production runs `generation_safe=True` (`jarvis/runtime/inherent_loop.py:1914`), so the
  legacy `_RingBuffer` branch (`voice_tts.py:1567-1578`) is not the production path.

## Target behavior

- After any of the six entry points above tombstones the active generation, the samples
  the callback hands PortAudio decay from the last emitted amplitude to exactly `0.0`
  over a bounded, non-zero number of samples, and stay at `0.0` afterwards.
- The decay is produced regardless of whether the callback took the `actual == 0` path
  (:1608) or one of the three `view[:actual] = 0.0` paths (:1616 / :1626 / :1641). It
  must work when the block content is already all zeros — i.e. it is **synthesized from
  the last emitted amplitude**, not obtained by scaling buffer content that is already
  silent. Multiplying a zeroed block by a ramp is a no-op and does not fix this.
- No allocation on the callback thread (any ramp table is preallocated, matching the
  existing `_GainRamp` scratch-buffer discipline at `voice_tts.py:454-455`).
- The declick advances **no** ledger state: it writes no `_CallbackReport`, does not
  change `_played_samples`, and does not move ring cursors. `estimated_audible_samples`,
  `heard_text`, `heard_through_sequence` and the `surface.playback_interrupted` payload
  are byte-identical to today for the same input.
- Nothing sets a persistent gain that could survive into the next generation. After the
  decay completes, the next activated generation plays at full amplitude with no
  un-mute call required.
- The fade length is a module constant. No new config key.

### Intended widening beyond the literal authorization

Fixing at the shared site means the decay **also** applies at natural end-of-generation
and at underrun — anywhere the callback emits silence after audio. This is an **intended
consequence, not scope creep**, and the hub accepted it on 2026-09-06 on the grounds that
a root-cause fix at the one place all six entry points converge is the correct shape, and
that fragmenting it into per-reason special cases to match the authorization's literal
wording would be worse engineering. The hub is telling the owner about this widening
directly.

What it costs: **no audible content, and a few milliseconds of added decaying tail.** The
decay is *synthesized into a block that would otherwise be silence*; it never attenuates
audio that would have been heard. At natural completion the ring's final short read has
already zero-padded inside its own block (`voice_tts.py:310-311`), so the last emitted
sample is `0.0` and the decay ramps from zero to zero — a no-op. If the audio instead
ends exactly on a block boundary, the decay is appended after the last real sample rather
than replacing it. At the three race sites the decay overwrites `view[:actual]` content
that today is zeroed outright, so nothing is lost there either.

Acceptance row 7 proves this, and it is honest to write precisely because the decay is
additive rather than subtractive.

## Affected contracts and files

- L5 `jarvis/surface/voice_tts.py:_callback` (:1542) — the shared decay, covering the
  `actual <= 0` return at :1608 and the three `view[:actual] = 0.0` sites.
- L5 `jarvis/surface/voice_tts.py:AudioStreamPlayer.__init__` (~:644-649) — preallocated
  ramp table / decay state, alongside the existing `_gain` scratch buffers.
- Test `tests/integration/test_wave2_streaming_media.py:_CallbackPump` (:206) — retain
  the blocks the production callback wrote instead of discarding them (see Acceptance).
- Docs `docs/adr/0006-full-duplex-voice-session.md` — a **new** decision entry.

### Not checked by the drafter

- `jarvis/surface/playback_recovery.py` — touched by merge `04cbf93`. No path was found
  from it into the callback zeroing and no change is expected, but it was **not read end
  to end**. Re-check at launch.
- The hermetic and Swift baselines were not run (the drafting pass was read-only on the
  repo). The figures under Acceptance are expectations to measure, not verified facts.

## Boundaries and non-goals

- Layers that may change: L5 only. No L2/L3/L4/L6 change; no new event type, no new
  event payload field, no wire change.
- Must not change: `estimated_audible_samples` / `heard_text` / `heard_through_sequence`
  accounting; the `surface.playback_interrupted` and `surface.playback_failed` payloads;
  the `interrupt_generation` → `settle_interrupted_generation` linearization; the
  `_callback_commit_generation` CAS window; starvation accounting
  (`_starvation_gaps`, `_starvation_dry_generation`).
- Non-goals:
  - Enabling ducking, or wiring `set_gain` / `duck` / `unduck` into production. D8 stays
    deferred. `_GainRamp` stays dead.
  - `_purge_after_drain` clearing the whole lane rather than the stopped group.
  - The `macos_say` single-window timeout residual.
  - Any change to `cursor_quality` labelling or to the output-latency clamp that just
    shipped (`718343d`).
  - The `frames > self._callback_max_frames` fail-silent zero at `voice_tts.py:1582` —
    different trigger (unexpected host block size), not an interrupt.
  - The legacy non-generation-safe `_RingBuffer` branch (`voice_tts.py:1567-1578`).

## Rejected approaches

- **Fix at the three `view[:actual] = 0.0` sites only** — does not fix the dominant path.
  Those sites are narrow races; the ordinary interrupt block is zeroed inside
  `read_into` (:310-311) and returns at :1608 without touching them. Evidence: the
  ordering in `interrupt_generation` (:1227 lease clear, then :1230 discard) plus
  `read_into`'s `_read_idx = max(_read_idx, _discard_before_idx)` (:291).
- **Fix per caller in `voice_media.py`** — six entry points, all converging on
  `_interrupt_snapshot` (:3029); six guards where one shared change suffices, and the
  guards would sit in the async actor with no access to the callback's amplitude state.
- **Reuse `_GainRamp` for the declick** — rejected on two independent grounds.
  1. *It cannot work.* `_GainRamp.apply` (:484) **multiplies** the block. On the dominant
     path the block is already all zeros, so any ramp multiplied into it stays zero. It
     is structurally incapable of removing this step.
  2. *It would mute the device permanently.* All four zeroing paths `return` before
     `self._gain.apply(view)` is reached. Driving `set_target(0.0, n)` would leave
     `_GainRamp._current == 0.0`, and the next successful block calls `apply` (:1632) and
     is multiplied by zero. Nothing in production ever calls `set_gain(1.0, …)` to undo
     it — the sole caller repo-wide is one test (:938). Restoring gain would mean
     building the un-mute half, i.e. exactly the D8 ducking machinery the owner did not
     authorize.
- **Fade the real tail by deferring the discard** — let the ring keep serving N more
  stale samples under a descending ramp. Better fidelity, but `interrupt_generation`
  publishes the discard boundary and `mark_software_drained()` synchronously, and
  `_interrupt_snapshot` snapshots the ledger immediately after. Deferring the discard
  changes the interrupt linearization and the cursor/snapshot contract — a cross-layer
  change far larger than a declick, for ~3 ms of audio nobody can distinguish.
- **Acceptance via `~/.jarvis-audio-test/`** — the rig substitutes a zero array for a
  missing capture: `~/.jarvis-audio-test/captures/report.py:8` and
  `~/.jarvis-audio-test/captures-vol3/report.py:8` both read
  `np.load(p32).astype(np.int32) if os.path.exists(p32) else np.zeros(1, np.int32)`. A
  capture that never happened prints a clean-looking result. Not needed here anyway —
  see Acceptance.

## Acceptance evidence

There **is** an honest observable, and it is not a live waveform.

The block sequence `_callback` writes into `outdata` **is** the signal handed to
PortAudio; the device does not reshape it. `_CallbackPump`
(`tests/integration/test_wave2_streaming_media.py:206`) already drives the real
`_callback` on a real thread at ~0.5 ms cadence with a real
`StreamingTTSPipeline` + real event log behind it — it just throws `out` away each
iteration. Retaining those blocks yields the production output signal, not
test-authored state.

**The line that keeps this an integration test and not a banned unit test:** the
interrupt MUST be driven through the pipeline's public event/command surface (the
`user_stop` path via `_stop_foreground_output_owned`, reached from the pipeline's public
stop entry point), with a real event log. The test must NOT call
`player.interrupt_generation`, `player.set_gain`, or any other player method directly.
Constructing an `AudioStreamPlayer` and poking its own methods to inspect its own buffer
is the banned shape; capturing what the production callback emits while the production
actor interrupts it is not.

**Do not "simplify" this later by calling the player directly.** Driving the interrupt
through the public stop path is the entire reason this test is admissible under the
no-unit-tests rule. A future refactor that replaces the pipeline drive with a direct
`player.interrupt_generation(...)` call converts a legitimate acceptance into a banned
unit test while keeping every assertion green.

- Positive: a new test in `tests/integration/test_wave2_streaming_media.py` drives a
  constant-amplitude response through the real pipeline, interrupts it through the
  public stop path, and asserts on the concatenated captured blocks:
  1. **Pre-cut amplitude guard** — the last sample before the decay has `abs(x) >= 0.5`.
  2. **Decay present** — between that sample and the first `0.0`, there are at least 2
     samples with `0.0 < abs(x) < ` the pre-cut amplitude, non-increasing in magnitude.
  3. **No residual step** — `max(abs(diff(signal)))` across the cut region is at most
     `pre_cut_amplitude / (declick_samples - 1)` times a small tolerance.
  4. **Terminates and stays silent** — the signal reaches exactly `0.0` and every
     later sample is `0.0`.
  5. **Ledger untouched** — the post-interrupt snapshot's `estimated_audible_samples`
     equals the sample count actually written to the ring before the interrupt, proving
     the synthesized decay was not counted as generation audio and emitted no
     `_CallbackReport`.
  6. The `surface.playback_interrupted` event still lands in the event log.
  7. **Natural completion is unharmed** (this row covers the intended widening above). A
     second response runs to natural completion with **no** interrupt. Assert: the
     captured PCM reproduces every written sample of the response with no attenuation of
     any sample that carries content; the post-completion snapshot reports
     `fully_presented`, `estimated_audible_samples` equal to the full written sample
     count, and `heard_through_sequence` equal to the last finished segment sequence.
     Any decay samples appear only *after* the last content sample, and number at most
     `declick_samples`. If this row cannot be made to pass honestly, **STOP AND REPORT**
     — the hub will reconsider the shared-site shape rather than accept a weakened row.

  **Degenerate-path table — each must FAIL, and the lane must show it failing by
  temporarily forcing the condition before shipping:**

  | Degenerate case | Which assertion kills it |
  |---|---|
  | fade length zero (hard cut unchanged) | (2) — zero intermediate samples exist |
  | fade applied to an already-silent buffer | (1) — pre-cut amplitude below floor |
  | fade too short to matter | (3) — step exceeds the per-sample bound |
  | ramp never completes / device left muted | (4) — signal never returns to a clean 0.0 |
  | declick faked by writing a callback report | (5) — audible-sample count inflated |
  | multiply-a-zero-block implementation | (2) — the block is still all zeros |
  | decay eats real audio at natural end | (7) — content sample attenuated / counts short |

- Regression: full hermetic suite, run with `env -u MINIMAX_API_KEY` (see
  `tests/conftest.py:178-190`, which pops `MINIMAX_API_KEY` and sets
  `JARVIS_VOICE_DISABLE_WAKE=1` for the session). Expected: **1068 passed / 64 deselected**
  hermetic and **Swift 191**. The hub verified 1068/64 on the merged tip. **Confirm both
  yourself before your first edit and paste the raw output anyway** — never take a number
  from a card. If your measured baseline differs, **explain the delta** (a sibling lane
  landed, a merge moved the tip) and proceed against your measured number; a delta at
  baseline time is not a regression and must not be treated as one.
- Named regression canary: `tests/integration/test_wave2_streaming_media.py` around
  `:938` (`assert np.all(first_gain_output[:, 0] == 1.0)` /
  `assert np.count_nonzero(second_gain_output) == 0`) is the tightest existing
  amplitude assertion in the repo and is the test that will catch a declick leaking into
  the normal, non-interrupted path. It must stay green, and its raw result must be shown.
- Live run: **not required — and this was ruled deliberately, not skipped.** Nothing
  physical intervenes between the PCM we hand PortAudio and the DAC. The block-level
  assertion is therefore *strictly stronger* evidence about the amplitude edge than a
  microphone capture of the same edge, which only re-measures it through added room and
  device noise, at lower resolution, via a rig with the zero-array trap cited above.
  **Do not re-add a live run out of caution**; it would weaken the evidence, not
  strengthen it. An optional ear-check by the owner is welcome but is not acceptance.
  **If the lane nevertheless elects a live run:** capture the pre-run system default
  output route first; restore it in a `finally`/`trap` on every exit path; if the
  captured route is already the loopback ("BlackHole 16ch"), restore "MacBook Pro
  Speakers" instead; cap the volume; skip the switch entirely if the run needs no audio.
  If the capture cannot distinguish a ramped edge from a hard zero, **STOP AND REPORT** —
  do not soften the assertion to make it pass.

Read config values from `config/jarvis.yaml` and from the owner's overlay directly, never
from this card or a handoff: the repo ships `enable_macos_say_fallback: true` while the
overlay sets it false.

## Docs to sync

- `docs/adr/0006-full-duplex-voice-session.md` — a **new** decision recording that the
  interrupt amplitude edge is ramped, and stating explicitly that this is distinct from
  the D8 `duck_gain` / F11 unduck deferral at `:439-441`, which remains deferred and
  unbuilt. Do not amend D8; do not file this under it.
- `docs/spec.html` — only if the ADR entry establishes an invariant the spec owns
  (e.g. "playback output never steps discontinuously to silence"). Judge and say so
  either way; do not duplicate the ADR's reasoning into the spec.
- Errata candidate, **do not fix in this card, just report**: ADR-0006:261 cites
  `jarvis/surface/voice_tts.py:1255` and `:1208` for `complete_generation` /
  `interrupt_generation`; at tip `04cbf93` they are at `:1256` and `:1209`.

## Open questions

(none)

## /goal condition

Implement the interrupt declick described in the card. Done when ALL of the following
appear in this transcript as raw command output, not as claims:

1. The re-pinned tip: `git rev-parse HEAD` and `git log -1 --oneline` on
   `realtime-integration`, shown before any edit, and a statement of whether each
   `path:line` citation in the card still resolves to the cited symbol. Report any that
   moved; do not silently follow a stale line number.
2. The measured hermetic baseline BEFORE any edit: the raw tail of
   `env -u MINIMAX_API_KEY <hermetic pytest command>` showing `N passed, M deselected`,
   plus the Swift count. Expected 1068/64 and 191. If yours differs, EXPLAIN the delta
   and proceed against your measured number — a delta at baseline time is not a
   regression. Never take the number from the card.
3. Evidence that the fix is at the shared callback site: the diff shows the decay
   covering the `if actual <= 0: return` path (voice_tts.py:1608) in
   `AudioStreamPlayer._callback` as well as the `view[:actual] = 0.0` paths. A diff that
   touches only the three `view[:actual] = 0.0` sites does NOT satisfy this and fixes
   nothing on the common path — the card's "Read this first" section explains why.
4. `_GainRamp`, `set_gain`, `duck`, `unduck` are unchanged and still have no production
   caller: show `grep -rn '\.set_gain(\|\.duck(\|\.unduck(' --include='*.py' jarvis/`
   returning only `voice_tts.py`'s internal `set_gain` delegations.
5. The new integration test's raw pass output — including acceptance row 7, natural
   completion losing no audible content and still reporting full sample counts — and the
   raw output of all SEVEN degenerate-path checks from the card's table, each temporarily
   forced and shown FAILING, then reverted. A degenerate case that passes is a false
   pass; stop and report. The interrupt must be driven through the pipeline's public stop
   path (`user_stop`), never by calling `player.interrupt_generation` or `set_gain`.
6. The named regression canary at `tests/integration/test_wave2_streaming_media.py:938`
   shown green by name.
7. The full hermetic suite raw tail after the change, with no regression against the
   baseline from (2).
8. Each entry under "Docs to sync" either updated or explicitly judged unchanged, with
   the reason. The ADR entry is NEW and must not be filed under D8. When the change
   alters a documented contract, invariant, ownership boundary, or externally relevant
   behavior, update the canonical document that owns that fact; do not document what the
   code already makes clear; do not duplicate a fact across documents.
9. `git status` shows a clean tree and each slice is committed per the commit skill,
   with a Progress line appended per slice.

Constraints: no live audio run is required; if you elect one, capture and restore the
system default output route on every exit path (loopback "BlackHole 16ch" → restore
"MacBook Pro Speakers"), and cap the volume. Never stop or restart the owner's daemon
(pid 45429, port 8009) or his InherentCard (pid 96300). Never build, rebuild, launch or
delete anything under `.claude/worktrees/realtime-live-test`. Read config from
`config/jarvis.yaml` and the owner's overlay directly, never from this card.

If the card contradicts the repository, stop and report; do not redesign. Or stop after
25 turns.

## Progress

- 2026-09-06 lane A — Re-pinned every citation at `realtime-integration` tip
  `a29b1e8`. Two moved: `_active_lease = None` is `voice_tts.py:1224` (card said
  `:1227`) and `request_discard()` is `:1227` (card said `:1230`) — the ordering
  claim the card rests on is unchanged; the legacy `_RingBuffer` branch is
  `:1560-1562`, not `:1567-1578`; the named canary asserts are `:946`/`:947`, not
  `:938`. Everything else resolved. `playback_recovery.py` read: boot-time
  reconciler, no callback path, unaffected. Measured baselines before editing:
  hermetic **1068 passed / 64 deselected** in 60.06s, Swift **191** — both match
  the card exactly.
- 2026-09-06 lane A — Declick landed at the shared callback site
  (`_emit_declick`, covering the `actual <= 0` early return as well as the three
  `view[:actual] = 0.0` race sites). Two acceptance tests added, driving the
  interrupt through `pipeline.stop_foreground_output` (`user_stop`); all seven
  degenerate-path forcings shown failing on the predicted assertion, then
  reverted. Two existing assertions that pinned the hard cut were re-expressed,
  not weakened: the post-CAS block and the 1000-cycle churn block now assert that
  neither generation's PCM reaches the host and that what does is a monotone
  decay. Hermetic **1070 passed / 64 deselected** (1068 + 2 new), Swift **191**.
  No live run: the block handed to PortAudio is the signal, and nothing physical
  intervenes before the DAC.
