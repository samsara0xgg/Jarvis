# Goal: playback-underrun-observable

All line numbers re-pinned against `realtime-integration` @ `bb09c35`.

## Goal

Three facts the audio actor currently discards become durable Event Log rows:
whether a generation's output timeline was broken by ring starvation, what
output latency the host actually reported, and that the lane isolated itself
and will speak nothing further until the process restarts.

## Why

**1. Ring starvation is invisible by construction, and ADR-0006 already ruled
that it must not be.**

`_GenerationRingBuffer.read_into` (`jarvis/surface/voice_tts.py:283-313`)
zero-pads whenever the ring is dry: `pcm_out[actual:n] = 0.0`,
`generation_out[actual:n] = -1`, no counter, no signal beyond the returned
`actual`. `_callback` (`voice_tts.py:1496-1598`) discards that signal — on a
fully dry ring it takes `if actual <= 0: return` (`:1547-1548`) and counts
nothing. PortAudio cannot flag it: `read_into` has already filled the whole
block with legitimate zeros, so the host receives a complete, on-time,
perfectly valid block of silence. The one counter that exists,
`_underflow_count`, is bumped only from PortAudio's own
`status.output_underflow` (`:1511`) and from the oversized-host-block guard
(`:1537`), is exposed as a property nobody reads (`:1454-1456`), and is
printed exactly once — in the "stopped" log line at `:877-882`. A daemon that
never stops never prints it.

ADR-0006:347 already decided the shape of the answer: "Starvation, inserted
silence, device restart, the post-resample gain/kill envelope, and known
system-output mute state advance or reset explicit timeline epochs; they are
never hidden inside one cumulative counter." `_timeline_epoch`
(`voice_tts.py:684`) is incremented once per lease mint (`:1087`) and never on
starvation. The ruling is unimplemented: starvation currently advances
nothing and is recorded nowhere.

**Note for the next reader, so this is not re-derived wrongly.** Ring
starvation and the callback stall that causes lane isolation (below) are
*distinct, near-opposite* phenomena, and neither counter detects the other.
Starvation means the callback runs on time and the producer has nothing for
it — the ring is **empty**. A callback stall means the callback is stuck
inside its publication window (`voice_tts.py:1576` set, `:1597-1598` cleared),
so `settle_interrupted_generation` (`:1218-1235`) keeps returning `None` past
`_interrupt_snapshot`'s 0.5 s bound (`voice_media.py:3004`); while it is stuck
the producer keeps writing and the ring **fills**. A stalled callback also
makes PortAudio miss its deadline, which sets `status.output_underflow` — and
that path is already counted at `voice_tts.py:1511`. Slice 1 exists because
ADR-0006:347 is unimplemented and a mid-answer dropout is audible and
unrecorded, not because it detects anything about lane isolation.

**2. The one failure that silences the daemon for the rest of the process's
life writes no durable row at all.**

`_isolate_terminal_debt` (`jarvis/surface/voice_media.py:3140-3168`) sets
`self._lane_isolated = True` (`:3148`). Verified: the only three assignments
in the whole tree are `= False` at construction (`:656`) and `= True` at
`:2726` (`_isolate_fallback_cleanup`) and `:3148` — nothing ever resets it. It
clears `self._accepting` (`:3150`) so `_submit_owned` refuses every future
event (`:1509`), terminalizes the active response and everything queued, and
calls `_request_shutdown_deadline`. `_resume_after_wake_owned` explicitly
refuses to re-admit while isolated (`:1494`). There is one construction site
for the owner (`jarvis/runtime/inherent_loop.py:1918`) and nothing supervises
or recreates it. If it fires, the user gets total silence on every subsequent
turn until the process restarts.

The only record is `record_realtime_trace("media_terminal_debt_isolated", …)`
(`:3155-3161`) — and `jarvis/shared/realtime_trace.py:1-8` says in its own
words that trace points are "diagnostic observations, not canonical Event Log
facts", `telemetry_only`, `production_fact=false`, held in a bounded
in-process deque that dies with the process. So the failure whose symptom is
total silence is unprovable after the fact, and the ledger offers nothing to
explain it.

Worse than nothing: the *next* boot's reconciler
(`jarvis/surface/playback_recovery.py:73-150`) finds the orphaned
`surface.playback_started` and terminalizes it as
`surface.playback_interrupted` with `reason: "daemon_restart"` and
`_UNHEARD_CURSOR`. So the durable record eventually says the daemon restarted,
when the truth is that the audio lane isolated itself and the process went
deaf hours earlier. This card replaces silence-plus-a-wrong-label with a row
that names what happened.

**3. The output latency estimate is a hardcoded constant with no caller.**

`_open_output_stream` (`voice_tts.py:517-541`) builds `sd.OutputStream` and
returns it; `.latency` is never read anywhere in the file (grep: only the
`latency=` kwarg and `self._latency`, which is the *requested* `"low"`
string). PortAudio populates it at open from `Pa_GetStreamInfo()`
(sounddevice `_StreamBase.__init__`: `self._latency = info.outputLatency` for
an output-only stream), documented as "the most accurate estimate of
input/output latency available to the implementation". We discard it and use
`estimated_output_latency_s: float = 0.12` (`:628`, folded to ns at
`:689-692`, consumed as `presentation_delay_ns` at `:1594`). That parameter
has **no caller anywhere in `jarvis/`** — both player construction sites
(`inherent_loop.py:1910` and `:1960`) omit it — so 120 ms is not a tunable
default someone picked per machine; it is the only value that has ever been
used, on every device.

## Current behavior

- A dry generation ring produces a full block of zeros and no record
  (`voice_tts.py:283-313`, `:1547-1548`).
- `_underflow_count` surfaces only in a close-time log line
  (`voice_tts.py:877-882`); a long-lived daemon never emits it.
- `presentation_delay_ns` is always `120_000_000` (`voice_tts.py:628`,
  `:689-692`, `:1594`); no code path reads `stream.latency`.
- Both isolation sites (`voice_media.py:2724-2734`, `:3140-3168`) write only a
  telemetry-only trace point. `_lane_isolated` has no reset and no recovery
  path (`:656`, `:2726`, `:3148`, `:1494`).
- `_response_terminal` (`voice_media.py:1993-2005`) calls `_interrupt_active`
  without checking `active.terminal_commit_pending`.

## Target behavior

- Every `surface.playback_completed` / `playback_interrupted` /
  `playback_failed` row carries `starvation_gaps` and `host_underflows`, both
  scoped to that `(response_id, playback_generation_id)`.
- `starvation_gaps` is `0` for a generation whose ring never ran dry
  mid-stream, and `>= 1` for one that was starved and then resumed.
- The normal end-of-generation tail — ring dry for the whole presentation
  horizon while `_drain_and_complete` polls `fully_presented` — contributes
  `0`. A gap counts only when audio for the *same* generation resumes after
  the dry window. Without this rule the counter fires on every clean response
  and is worthless.
- Every isolation of the media lane appends one durable
  `surface.playback_lane_isolated` row naming the response, the generation,
  which terminal it was trying to commit, and why isolation happened.
- Every `surface.playback_started` row carries `estimated_output_latency_ns`:
  the host-reported output latency when the host reports a plausible one, and
  the configured `estimated_output_latency_s` default otherwise.
- `cursor_quality` is unchanged. Nothing is relabelled `measured_dac`.
- ADR-0006 §4.2 no longer claims `terminalize_playback` is the sole exit for
  playback without qualifying the isolation path.

## Affected contracts and files

### Slice 1 — starvation counters

- L5 `jarvis/surface/voice_tts.py:_callback` (`:1496-1598`) — count a
  starvation gap (dry-then-resumed for the same generation) while a lease is
  live. Two lifetime ints on the player; no change to `_CallbackReport`,
  `_CallbackReportRing`, or `PlaybackLedger` — the realtime report ring keeps
  its shape and its 2048-entry budget.
- L5 `jarvis/surface/voice_tts.py` (`:1454-1456`) — expose the counters as
  properties beside `underflow_count`.
- L5 `jarvis/surface/voice_media.py:_start_response` (`:2146-2183`) — snapshot
  both player counters onto `_ActiveResponse` where the lease is minted
  (`:2164`).
- L5 `jarvis/surface/voice_media.py:_ActiveResponse` (`:356-373`) — two int
  fields for the snapshots.
- L5 `jarvis/surface/voice_media.py:_commit_terminal` (`:3055-3092`) — add
  `starvation_gaps` and `host_underflows` as deltas against the snapshots.

### Slice 2 — the lane-isolation row (primary), the guard (secondary)

- L5 `jarvis/surface/voice_media.py:_isolate_terminal_debt` (`:3140-3168`) and
  `_isolate_fallback_cleanup` (`:2724-2734`) — both append one
  `surface.playback_lane_isolated` row through `emit_event`, beside the
  existing trace point, before `_request_shutdown_deadline`. Best effort: the
  append is wrapped so that a failure logs and isolation still completes.
  Isolation must never fail because its own record-keeping failed.
- L5 `jarvis/surface/voice_media.py:_response_terminal` (`:1993-2005`) — skip
  `_interrupt_active` when `active.terminal_commit_pending` is already set.
  Secondary; see "Reachability of the guard's window" below.
- `docs/adr/0006-full-duplex-voice-session.md` — the new event, and the
  isolation qualification on §4.2's "sole exit" claim.

### Slice 3 — real device latency

- L5 `jarvis/surface/voice_tts.py:AudioStreamPlayer.start` — read
  `getattr(stream, "latency", None)` after `_open_output_stream` returns and
  **before** `stream.start()` (`:730-736`, before `:766`). PortAudio fills the
  field at open, so reading pre-start is valid and removes any question about
  racing the realtime callback that consumes `_estimated_output_latency_ns`
  at `:1594`.
- L5 `jarvis/surface/voice_tts.py` — expose the effective latency in ns.
- L5 `jarvis/surface/voice_media.py:_play_response` (`:2185-2279`) — add
  `estimated_output_latency_ns` to the `surface.playback_started` payload
  (`:2209-2224`).

### Shared

- `tests/integration/test_wave2_streaming_media.py` — new cases;
  `_FakeOutputStream` (`:2452-2465`) gains a settable `latency`.

### Why `surface.playback_lane_isolated` and not a playback terminal

Isolation cannot honestly write a playback terminal, for three separate
reasons, any one of which is sufficient:

1. Three of the five call sites (`:2814` in `_fail_active`, `:2845` in
   `_interrupt_active`, `:3031` in `_response_task_done`) isolate *because*
   `_interrupt_snapshot` returned no settled snapshot. ADR-0006:614-628 makes
   `heard_through_sequence` and `submitted_samples` required keys on every
   playback terminal. Without a settled snapshot those values cannot be filled
   honestly, and inventing them is the exact class of false record this card
   exists to remove.
2. The fourth site (`:2989` in `_commit_terminal_durable`) isolates *because*
   the terminal CAS exhausted its retries. Routing through the same primitive
   again is the one thing already known not to work.
3. The playback identity `f"{response_id}:{generation}"`
   (`jarvis/state/lifecycle_terminal.py:233`) may already hold a terminal, in
   which case the CAS returns `AlreadyTerminal` and writes nothing.

A distinct non-terminal row sidesteps all three: it is appended with plain
`emit_event` (`jarvis/state/event_log.py:1624-1637`), it does not participate
in the playback terminal CAS, and it can therefore coexist with whatever the
boot reconciler later writes. `event_log.py` enforces an event-type
allowlist via `EventTypeRegistry`, so the new type requires one L2 registry
entry — a string and a schema note.

Payload: `session_id`, `response_id`, `turn_id`, `playback_generation_id`,
`terminal_type` (the terminal it was attempting), `error_type`, and
`isolation_reason` — one of `callback_publication_unsettled`,
`terminal_append_exhausted`, `task_escaped`, `fallback_cleanup_unproven`.
Those four values distinguish the five call sites and are the first question
anyone debugging total silence will ask.

### Why the terminal payload is the durable home for the counts

`_commit_terminal` (`voice_media.py:3055-3092`) is the single shared builder
for all three playback terminals and the only place every playback outcome
passes through. `terminalize_playback` keys the CAS on
`f"{response_id}:{generation}"` (`lifecycle_terminal.py:233`), so a field
written there is per-generation and per-row by construction — not the "one
cumulative counter" ADR-0006:347 forbids. Rejected alternatives: a player
property dies with the process; a `record_realtime_trace` point is explicitly
not an Event Log fact (`jarvis/shared/realtime_trace.py:1-8`), so it may be
measurement output but never the sole evidence for a durable claim; and
`surface.playback_checkpoint` (`voice_media.py:2921-2934`) is emitted only
when a whole segment crosses the audible horizon, so a response that starves
and never completes a segment would emit no checkpoint at all — the exact case
the counter exists for.

`estimated_output_latency_ns` goes on `surface.playback_started` because the
value is now device-dependent rather than a constant, it changes the meaning
of every `submitted_samples` and `heard_through_sequence` in the rows that
follow, and `playback_started` is emitted exactly once per lease
(`:2209-2224`).

### Reachability of the guard's window — verified

The question was whether L3 can emit `response.cancelled` / `response.failed`
(the two members of `_RESPONSE_TERMINAL_TYPES`, `voice_media.py:68`) against a
response whose media-side `terminal_commit_pending` is already set.

**Closed, definitively, for the case that motivated the question.** ADR-0008
D1 is enforced at `jarvis/runtime/__init__.py:2551-2574`: the response
terminal is written **before** render, with the comment "The opposite ordering
is unrecoverable — it would let a cancel land after the words were already
spoken", pinned by `test_completed_without_delivery_when_render_raises`.
`surface.response_emitted` is written by `render_response`, i.e. *after*
`response.completed`. The response lifecycle CAS admits one terminal per
response (`jarvis/state/lifecycle_terminal.py:40`), so once `response.completed`
lands no `response.failed` or `response.cancelled` can ever be appended. Since
a non-incremental lease does not start playback until `surface.response_emitted`,
and an incremental lease still cannot reach `_drain_and_complete` until
`_stream_with_prefix_fallback`'s `live=True` loop sees the buffer emitted, the
run is always already terminal before playback can complete. A response
terminal arriving after `surface.playback_completed` is impossible.

**One narrower window remains, and it is open in the configuration the owner
actually runs.** With `streaming_output.speak_from_segments: true`, playback
runs while the L3 run is still open. The repo default is `false`
(`config/jarvis.yaml:266`), but the owner's overlay sets it to `true`
(`~/.jarvis-allen-test/overlay/config/jarvis.yaml:257`) alongside
`streaming_output.enabled: true` (`:237`). So this is the live path, not a
hypothetical flag. If a provider error drives `_play_response` into
`_fail_active`, which sets `terminal_commit_pending` at `:2811` and then
awaits `_interrupt_snapshot`, and L3 independently fails or is cancelled in
that window, `_run_owned` drains the `response.failed` row and
`_response_terminal` re-enters `_interrupt_active` on an already-terminalizing
response.

**Verdict: keep the guard, and describe the real defect.** In that window
`_interrupt_active` calls `_cancel_active_io`
(`:2868-2878`), which does `task.cancel()` on `active.task` — cancelling
`_play_response` **inside its own `finally`** (`:2261-2279`), so
`_cancel_fallback` and `self._ducker.leave_output()` are skipped and the
system output lease stays held. `terminalize_playback` then dedups on the
playback identity and returns `AlreadyTerminal`
(`lifecycle_terminal.py:182-184`); `_commit_terminal` ignores the return, so
`_commit_terminal_durable` returns `True` (`:2988`) and `_release_active` runs
a second time, emitting a second `broadcast_voice_sync("spoken", …)` with a
contradictory `output_outcome`. This is a defect, not a redundancy: the
durable Event Log is correct either way (the CAS holds), but a held output
lease and a contradictory UI frame are not.

## Boundaries and non-goals

- Layers that may change: L5 (`jarvis/surface/`) only, plus
  `docs/adr/0006-full-duplex-voice-session.md`, plus exactly one L2 registry
  entry: `starvation_gaps`, `host_underflows`, and
  `estimated_output_latency_ns` are optional payload fields on existing event
  types, and `surface.playback_lane_isolated` requires one new
  `EventTypeSchema` entry in `jarvis/state/event_log.py` with
  `owner_layer="L5"`, matching every sibling `surface.playback_*` entry.
  `terminalize_playback` and `emit_event` are otherwise untouched.
- Must not change: `cursor_quality` semantics; `submitted_samples`,
  `total_samples`, `heard_through_sequence`, `heard_text`, `heard_text_hash`;
  the `_CallbackReport` / `_CallbackReportRing` shape; the tombstone/CAS
  ordering in `_interrupt_snapshot` (`:2996-3013`); the 0.5 s settle bound;
  the boot reconciler's `daemon_restart` behavior.
- Non-goal: relabelling anything `measured_dac`. `voice_ledger.py:9-12` states
  in its own words that the label is permitted "only when a backend supplies a
  real measured DAC/loopback mapping; the sounddevice backend used by Wave 2
  reports `estimated` or `unknown`." A host-API-reported latency is still an
  estimate. The field stays `estimated_output_latency_ns` and `cursor_quality`
  stays `estimated`.
- Non-goal: a recovery path for `_lane_isolated`. This card makes the failure
  provable; making it survivable is a separate card with its own design
  question (supervision and re-creation of the owner at
  `inherent_loop.py:1918`).
- Non-goal: correcting the boot reconciler's `reason: "daemon_restart"` label
  for an isolated lane. Named because it is the reason the new row is needed,
  but changing it means teaching the reconciler to read the isolation row —
  a separate change.
- Non-goal: interrupt fade / declick. It is a separate queued card, and
  ADR-0006 does not pre-authorize it — the deferral at `:436` sits inside D8
  and defers the Phase-1 `duck_gain` ramp and F11's unduck, i.e. barge-in
  ducking, not an interrupt envelope.
- Non-goal: retrying a failed response. There is no resume-from-cursor
  machinery, so a retry restarts from segment 0 and re-speaks the already-heard
  prefix — plausibly worse than silence.
- Non-goal: anything under `jarvis/decision/`.

## Rejected approaches

- Counting every zero-padded block. Fires on the normal end-of-generation tail
  (the ring is legitimately dry for the whole presentation-horizon poll in
  `_drain_and_complete`), producing a nonzero floor on every clean response and
  an acceptance criterion nobody can falsify.
- Carrying the count through `_CallbackReport` into `PlaybackLedger`. Would be
  per-generation for free, but requires widening the preallocated realtime
  report ring, and the fully-starved case (`actual <= 0`) writes no report at
  all — so the interesting event would still be invisible.
- Bumping `_timeline_epoch` on starvation. The epoch is copied from the lease
  at mint (`voice_tts.py:1087-1094`, `voice_ledger.py:161`), so a
  mid-generation bump propagates to nothing without rebuilding per-chunk epoch
  plumbing. Left to a future card.
- Writing a playback terminal from the isolation path. See "Why
  `surface.playback_lane_isolated` and not a playback terminal".
- Leaving the isolation row to the boot reconciler. It writes
  `reason: "daemon_restart"` (`playback_recovery.py:131`), which is a wrong
  label, and only after the restart that the isolation forced.

## Acceptance evidence

Every check below asserts on a durable Event Log row payload field or on the
voice-sync wire frame. No check calls a function with literal arguments and
asserts its return value; no check constructs an object and drives its own
methods asserting on that same object. Python unit tests are banned and
`tests/unit` is retired, so "an integration test in `tests/integration/`"
constrains nothing on its own — the observable is what constrains it.

New cases live in `tests/integration/test_wave2_streaming_media.py` and drive
the real `_callback` through `_CallbackPump` (`:205-224`, a real thread
invoking `_callback(out, 32, None, None)` at ~0.5 ms cadence) against the real
`StreamingTTSPipeline` and a real SQLite Event Log.

**Slice 1**

- Positive — starvation is visible: gate the fake session's second audio chunk
  until the generation ring has drained, then release it. The
  `surface.playback_completed` row for that `(response_id,
  playback_generation_id)` has payload `starvation_gaps >= 1` and payload
  `provider == "minimax_ws_streaming"`.
- Positive — the false-pass guard: same fixture with all audio written before
  the pump starts. The `surface.playback_completed` row has payload
  `starvation_gaps == 0`. Without this case the first one passes on the
  end-of-generation tail floor and proves nothing.

**Slice 2**

- Positive — isolation is provable: force `_interrupt_snapshot` to return
  `None` by injecting at the `settle_interrupted_generation` seam, then drive
  an interrupt. A `surface.playback_lane_isolated` row exists with payload
  `isolation_reason == "callback_publication_unsettled"`, payload
  `terminal_type == "surface.playback_interrupted"`, and payload
  `playback_generation_id` equal to the isolated generation.
- Positive — isolation still completes when its own append fails: make
  `emit_event` raise for the isolation row only. The lane is still isolated —
  observable as the next `submit_event` returning `MediaSubmitOutcome("closed",
  …)` — and the run does not raise.
- Positive — the guard: with `speak_from_segments: true` (the owner's live
  setting), drive `_fail_active` on an incremental lease and deliver
  `response.failed` in the same window. The recording broadcaster receives
  exactly one `spoken` voice-sync frame for that `turn_id`, not two with
  conflicting `output_outcome`, and exactly one `surface.playback_*` terminal
  row exists for that `(response_id, generation)` (the latter unchanged — the
  CAS already guarantees it). If the interleaving turns out not to be drivable
  from the harness, keep the two-line guard, say so in Progress, and do not
  invent a test that only exercises the guard's condition in isolation — that
  would be a unit test.

**Slice 3**

- Positive — latency reported: patch `_open_output_stream` with a fake whose
  `latency` is `0.035`. The `surface.playback_started` row has payload
  `estimated_output_latency_ns == 35_000_000`.
- Positive — degenerate `0.0` is *not* "zero latency": same fake with
  `latency = 0.0`. The `surface.playback_started` row has payload
  `estimated_output_latency_ns == 120_000_000` (the configured default), not
  `0`.
- Positive — attribute absent (a patched fake, or a host API that never fills
  the field): fake with no `latency` attribute. The `surface.playback_started`
  row has payload `estimated_output_latency_ns == 120_000_000`.
- Positive — absurd value rejected: fake with `latency = 12.0`. The
  `surface.playback_started` row has payload
  `estimated_output_latency_ns == 120_000_000`.

  These four rows are the point of the slice: separating a real value from a
  degenerate one that reads as valid. Do not collapse them.

**Provider pin**

- Any criterion that reads a sample count must also assert
  `payload["provider"] != "macos_say"`. `_run_macos_say`
  (`voice_media.py:2564-2571`) sets `provider_label = "macos_say"` and never
  calls `write_generation` (sole call site `:2550`), so `submitted_samples` and
  `total_samples` are both `0` on that path and `submitted == total` passes
  trivially as `0 == 0`. The `say` path is **live under repo defaults** —
  `config/jarvis.yaml:262` ships `enable_macos_say_fallback: true` — so this
  pin is not theoretical. (The owner's own overlay sets it to `false` at
  `~/.jarvis-allen-test/overlay/config/jarvis.yaml:253`; that file is outside
  the repo and does not govern the hermetic suite, which runs on repo and
  dataclass defaults. A `false` in a handoff note, `HANDOFF-6.md:28`, is a
  handoff claim, not the config.)
  The checks above deliberately assert on `starvation_gaps`,
  `isolation_reason` and `estimated_output_latency_ns`, none of which is a
  sample count, but the pin stays because a later reader will add one.

**Regression**

- `env -u MINIMAX_API_KEY uv run pytest -q -m "not live_llm and not
  live_codex"` — baseline `1057 passed, 64 deselected`; must be
  `1057 + <new cases> passed, 64 deselected`. `MINIMAX_API_KEY` must be unset:
  `tests/conftest.py:181-188` pops it for the session, and leaving it set makes
  `test_wave2_streaming_media.py` flake.
- `uv run lint-imports`, `uv run ruff check .`, `uv run mypy --strict jarvis`
  all clean. Report the printed counts; never infer them.
- Swift `191` tests still pass. Run only if anything under the Swift tree
  changes — it should not.

**Live run**

- Required, for slice 3 only. A standalone short-lived process opens one
  `sd.OutputStream` on the *current* system default output device with the same
  parameters `_open_output_stream` uses, prints the raw `stream.latency`, and
  closes it. The transcript must show that raw value. This run plays no audio,
  so it must **not** switch the system default output device.
- It must not stop or restart the owner's daemon (pid 45429, port 8009) or his
  InherentCard (pid 96300), and must not build, rebuild, launch, or delete
  anything under `.claude/worktrees/realtime-live-test`. A full daemon burn is
  not required. Note that `config/jarvis.yaml:246` ships
  `streaming_output.enabled: false`, so the Wave 2 media path is off under repo
  defaults and a daemon burn would need the live-test overlay anyway.
- If a later step does need audio: capture the pre-run system default output
  route first, restore it in a `finally`/`trap` on every exit path, and if the
  captured route is already the loopback ("BlackHole 16ch") restore "MacBook
  Pro Speakers" instead.

## Docs to sync

- `docs/adr/0006-full-duplex-voice-session.md:586-641` — add `starvation_gaps`
  and `host_underflows` to the `optional:` list of `surface.playback_completed`,
  `surface.playback_interrupted`, and `surface.playback_failed`; add
  `surface.playback_lane_isolated` with its required fields as a new non-terminal
  L5 event.
- `docs/adr/0006-full-duplex-voice-session.md:641` — qualify "The sole exit for
  `completed/interrupted/failed` playback is the L2 atomic append primitive
  `terminalize_playback`". There is a fourth exit that appends no terminal:
  lane isolation. Record that `_lane_isolated` is never reset, that wake refuses
  to re-admit while it is set, that the single owner construction site
  (`inherent_loop.py:1918`) is unsupervised, and that the process therefore
  speaks nothing further until restart. Record that the new
  `surface.playback_lane_isolated` row is the durable evidence, and that the
  next boot's reconciler will separately label the orphaned response
  `daemon_restart`.
- `docs/adr/0006-full-duplex-voice-session.md:362` — record that
  `surface.playback_started` additionally carries
  `estimated_output_latency_ns`, host-reported when plausible and the
  configured estimate otherwise.
- `docs/adr/0006-full-duplex-voice-session.md:347` — record how this card
  satisfies the starvation clause: starvation is reported per generation, per
  row, on the playback terminal; the per-chunk `timeline_epoch` mechanism the
  same sentence describes remains unimplemented and is out of scope here.
- `docs/spec.html` — none. The playback event payload contract is not mirrored
  there (grep for `submitted_samples` and `surface.playback` returns nothing);
  do not duplicate the fact.

## Open questions

(none)

## /goal condition

Implement `docs/goals/playback-underrun-observable.md`. Done when the
transcript shows:

1. `starvation_gaps` and `host_underflows` are written by
   `voice_media.py:_commit_terminal` onto all three playback terminals, scoped
   per `(response_id, playback_generation_id)`, counted in
   `voice_tts.py:_callback`; a gap counts only when audio for the same
   generation resumes after a dry window, so the end-of-generation tail is zero.
2. Both isolation sites (`_isolate_terminal_debt`, `_isolate_fallback_cleanup`)
   append a durable `surface.playback_lane_isolated` row via `emit_event`
   carrying `session_id`, `response_id`, `turn_id`, `playback_generation_id`,
   `terminal_type`, `error_type`, `isolation_reason`; a failed append logs
   without preventing isolation.
3. `estimated_output_latency_ns` is written onto `surface.playback_started`,
   read from the stream's `latency` before `stream.start()`, falling back to
   `estimated_output_latency_s` when missing, `0.0`, or implausible. Nothing is
   relabelled `measured_dac`; `cursor_quality` is unchanged.
4. `voice_media.py:_response_terminal` skips `_interrupt_active` when
   `active.terminal_commit_pending` is already set.
5. Raw output of `env -u MINIMAX_API_KEY uv run pytest -q -m "not live_llm and
   not live_codex"` is pasted in full and ends with
   `<1057 + new cases> passed, 64 deselected`. The 1057 baseline must not drop.
6. Raw output of `uv run lint-imports`, `uv run ruff check .`, and `uv run mypy
   --strict jarvis` is pasted, each clean. Counts quoted, never inferred.
7. Each payload assertion is shown passing, naming event type and field:
   `surface.playback_completed` `starvation_gaps >= 1` under an injected
   mid-stream ring gap, `== 0` on a clean response;
   `surface.playback_lane_isolated` `isolation_reason ==
   "callback_publication_unsettled"` and `terminal_type ==
   "surface.playback_interrupted"` when the settle seam is forced to `None`;
   the lane still isolated when that row's own append raises;
   `surface.playback_started` `estimated_output_latency_ns == 35_000_000` for a
   fake reporting `0.035`, `== 120_000_000` for each of `0.0`, no `latency`
   attribute, and `12.0`.
8. Every check asserts on an emitted Event Log row payload field or on the
   voice-sync wire frame. A test that calls a function with literal arguments
   and asserts the return value, or constructs an object and drives its own
   methods asserting on that same object, is a unit test regardless of
   directory, and Python unit tests are banned. A `record_realtime_trace` name
   may be measurement output but never sole evidence for a durable claim. The
   `terminal_commit_pending` guard is either covered by a case driving the
   `speak_from_segments: true` window (one `spoken` frame per `turn_id`, not two
   with conflicting `output_outcome`) or reported in Progress as landed without
   one; never invent a test exercising its condition in isolation.
9. A live run is shown: a standalone process opens one `sd.OutputStream` on the
   current default output device, prints the raw `stream.latency`, closes it,
   and the value is quoted. No audio played, default device not switched, the
   owner's daemon (pid 45429, port 8009) and InherentCard (pid 96300) not
   stopped or restarted, nothing under `.claude/worktrees/realtime-live-test`
   built, launched, or deleted.
10. Each "Docs to sync" entry was updated or explicitly judged unchanged, with
    the judgement stated. When the implementation changes a documented contract,
    invariant, ownership boundary, or externally relevant behavior, update the
    canonical document owning that fact; do not document what the code already
    makes clear; do not duplicate a fact across documents.
11. `git status` shows a clean tree; each slice is a separate commit made with
    the project commit skill, with a Progress line appended per slice.

If the card contradicts the repository, stop and report; do not redesign. Or
stop after 30 turns.

## Progress

- Slice 1 (starvation counters) — `_callback` counts a dry window only after
  the generation has committed a block and only when the *same* generation
  resumes, so the end-of-generation tail is 0. `surface.playback_completed`
  payload measured across 3 runs: gated mid-stream response
  `starvation_gaps: 1, host_underflows: 0, provider: minimax_ws_streaming`;
  clean single-segment response `starvation_gaps: 0`. Hermetic 1059 passed,
  64 deselected (baseline 1057).
- Slice 2a (lane-isolation row) — **card deviation, please confirm**: the card
  states `event_log.py` "enforces no event-type allowlist"; it does. A new type
  is rejected with `UnregisteredEventTypeError`, canary
  `tests/canary/test_emit_event_registered.py` enforces the same, and
  `docs/spec.html` §5.4 states outright that every `emit_event` type must be
  registered first. So the card's "No L2 change / the schema are untouched"
  boundary is unachievable as written. Registered one `EventTypeSchema` entry
  with `owner_layer="L5"` — the shape every existing `surface.playback_*` type
  already uses, and exactly the "string and a schema note" the card itself
  budgets. Nothing else in the card's design changed. Measured row:
  `{isolation_reason: callback_publication_unsettled, terminal_type:
  surface.playback_interrupted, playback_generation_id: 1, response_id: RISO,
  turn_id: TISO, error_type: RuntimeError, session_id: BOOT34c5...}`; under an
  injected append fault the row is absent, the run does not raise, and the next
  three submits return `closed`.
- Slice 2b (`terminal_commit_pending` guard) — the `speak_from_segments: true`
  window IS drivable from the harness, so the guard is covered, not just
  landed. `tests/integration/test_incremental_tts.py` gates the settle seam,
  lets `_fail_active` publish `terminal_commit_pending`, then delivers
  `response.failed` from a second thread inside that window. Observed with the
  guard reverted, the defect is sharper than the card predicted: not two frames
  but ONE frame with the wrong outcome —
  `['ui:spoken:T-RG:interrupted']` while the durable terminal is
  `surface.playback_failed`, i.e. the wire frame contradicts the Event Log.
  With the guard: `['ui:spoken:T-RG:failed']`, one terminal, no isolation row.
- Slice 3 (host output latency) — read from the stream at open, before
  `stream.start()`; plausibility bound `0.0 < s <= 1.0`. Measured
  `surface.playback_started.estimated_output_latency_ns`: `0.035` ->
  `35000000`; `0.0` -> `120000000`; no `latency` attribute -> `120000000`;
  `12.0` -> `120000000`. Live run on this machine's real default output device
  (MacBook Pro Speakers, 48 kHz mono, blocksize 0, latency "low", never
  started, no audio, route unchanged before and after):
  `raw stream.latency = 0.018708333333333334` -> `18708333` ns. The hardcoded
  120 ms was overestimating this device by 6.4x.
- Docs to sync — ADR-0006 §4.2 optional lists, the new event, the "sole exit"
  qualification, the §362 latency note and the §347 starvation clause all
  updated. `docs/spec.html`: judged UNCHANGED and verified, not assumed — it
  owns no playback payload fact (grep for `submitted_samples`,
  `surface.playback`, `starvation`, `host_underflow`,
  `estimated_output_latency` over `docs/spec.html` returns 0 hits). Its §5.4
  does own the rule that every `emit_event` type must be registered first, and
  that rule was followed rather than changed.
- Verifier round (opus, fresh context, `realtime-integration..HEAD`) — it
  independently reproduced the starvation semantics and the live canary and
  found no false implementation, plus 5 real issues. Fixed 4:
  (1) the §362 ADR sentence was wrong — `presentation_delay_ns` gates
  `record_audible` only, NOT `submitted_samples`; rewritten to name the heard
  side, the ~101 ms direction on this machine, and why 120 ms was never a
  safety margin (it under-stated any device slower than 120 ms, i.e. it broke
  round-backward in the unsafe direction);
  (2) **the prefill half of goal condition 1 had no covering evidence** — the
  clean-tail case starts its pump after the ring is full, so deleting the
  prefill guard left both starvation cases green. Added
  `test_a_normal_response_reports_no_starvation_for_its_prefill`, which runs
  the pump before the response exists (measured: 10 dry blocks with a live
  lease). Counterfactual: with the guard removed it fails `assert 1 == 0`;
  (3) a resume via a SHORT read was not counted (the branch required a full
  block), so a dropout that recovered with a partial block wrote
  `starvation_gaps: 0`. Any `actual > 0` now ends the dry window;
  (4) the guard newly made `_output_active.clear()` reachable while playback
  was still committing, so `is_speaking()` could go False with audio still
  playing and let Jarvis's own tail trigger the wake word. The guard now
  returns immediately and lets the in-flight terminal own the whole exit.
  Not changed, referred to the hub: the `> 1.0 s` fallback direction (falling
  back to 0.12 s is the least conservative option, but acceptance pins
  `12.0 -> 120_000_000`, so changing it would contradict the card); an
  over-count of at most 1 when an interrupt lands inside the callback (dry
  window was real, the resumed block is dropped); a single isolation can write
  two rows (both true); three of four `isolation_reason` values are uncovered.
- Hub ruling on Slice 2a's flagged deviation — card-authoring error, not a
  lane deviation: the card's "no event-type allowlist" / "schema untouched"
  claim was false when written, and the lane reported it instead of silently
  complying. Card corrected; the one `EventTypeSchema` registry entry
  (`owner_layer="L5"`) stands as accepted.

