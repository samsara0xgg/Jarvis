# Goal: listening-at-wake-arm

> **Citations pinned at `main` tip `508f863`** ("Merge branch 'lane/a' into
> realtime-integration"), each re-read at that commit while drafting. The
> implementation session MUST re-pin every `path:line` below against the tip it
> starts from and report any that moved; do not silently follow a stale line
> number.

## Goal

The single-audio-ingress wake path emits the **already-existing** `voice` wire phase
`listening` at the moment capture arms — so the card surfaces and shows `正在听…`
while the owner is still speaking — and emits the already-existing `empty` phase when
an armed capture expires without speech, so a false wake does not strand the card
on screen.

## Why

Owner-reported: after "Hey Jarvis" the card stays in the background and only surfaces
once he has finished speaking.

The load-bearing fact for scoping: **this needs no Swift change and therefore no
rebuild of the owner's binary.** His build at `20c55c3` already handles both phases —
verified with `git show 20c55c3:desktop/inherent-swift/InherentCard/NativeCardModel.swift`,
which has `case "listening": beginExternalVoiceCapture()` at its line 1231 and
`case "empty":` at 1237, byte-identical to tip. The whole change is Python.

## Current behavior

### The wake→arm window emits nothing a client can see

- Wake fires on the wake thread. `DuplexVoiceSession._enqueue_wake_detection`
  (`jarvis/surface/voice_session.py:1172`) puts a `WakeDetection` on a bounded queue
  and records the internal trace `audio_input_wake_detected` (`:1195-1202`). That
  trace never reaches `/inherent/ws`.
- The capture thread drains it in `_drain_detection_commands` (`:1229-1240`), which
  calls `self._assembler.arm(detection)` (`UtteranceAssembler.arm`, `:542`). `arm`
  sets `_state = ARMED` (`:547`) and mints `_utterance_id` / `_turn_id` (`:550-551`).
  It calls neither `_set_phase` nor any broadcaster.
- The **first** client-visible envelope of a wake turn is
  `self._broadcast("transcribing", turn_id=outcome.turn_id)` at `:1297`, inside
  `_handle_capture_outcome`, reached only after the assembler has endpointed the
  entire utterance and produced a `CapturedUtterance`. That is the pop-up the owner
  sees, and it is by construction after he stops talking.
- Verified exhaustively for this path: `grep -n 'self\._broadcast(' jarvis/surface/voice_session.py`
  returns exactly five sites — `:1277` (`error`, discontinuity), `:1297`
  (`transcribing`), `:1309` (`error`, commit queue full), `:1345` / `:1348` (`error`,
  ASR busy / ASR failed). All five are inside `_handle_capture_outcome` or
  `_commit_loop`; none can fire before the assembler has committed an utterance. The
  gate holds: **nothing is emitted between wake and commit.**

### The false-wake tail is already half-solved, and the unsolved half is the one this change creates

Two distinct false-wake shapes exist, and they do not share a terminal:

1. **Wake, speech, ASR returns nothing** — the owner's `wake: empty utterance` log
   line. `_commit_loop` (`:1316`) calls `pipeline.run_turn` (`:1329-1338`) without
   `broadcast=False`, so the default `broadcast: bool = True`
   (`jarvis/surface/voice_pipeline.py:109`) applies, and the pipeline itself sends
   `broadcast_voice_sync("empty", turn_id=turn_id, reason="no_speech")` at
   `voice_pipeline.py:181-183` **before** raising `VoicePipelineEmptyError`. The card
   already terminalizes on this. **Nothing to fix here.**
2. **Wake, then silence, armed timeout** — `WakeArmExpired`. `_handle_capture_outcome`
   (`:1259-1276`) records the `audio_input_wake_arm_expired` trace and `return`s. **No
   envelope at all.** Today that is harmless because nothing was ever shown. Once
   `listening` is emitted at arm, this path is exactly the D27 trap: the card would be
   raised, told never to auto-fade, and never told the turn is over.

### The client, at the owner's build

- `BridgeMessageRouter.dispatch` (`desktop/inherent-swift/InherentCard/BridgeBackend.swift:32-43`)
  reads `json["op"] as? String` and `json["payload"] as? [String: Any]` — **untyped
  `JSONSerialization` dictionaries, not `Codable`**. `case "voice"` (`:41`) forwards to
  `voiceState`. There is no enum decode on this path and therefore no throw.
- `NativeCardController.handleVoiceState` (`NativeCardController.swift:729-736`) is
  **phase-agnostic**: it sets `userHidden = false`, repositions, calls
  `fade.showInstant()`, then hands the payload to the model. Any `op:"voice"` envelope
  raises the window.
- `NativeCardModel.voiceState` (`NativeCardModel.swift:1227-1245`) switches on
  `payload?["phase"] as? String`, with cases `listening` / `transcribing` /
  `accepted` / `empty` / `error` and `default: break`.
  - `case "listening"` → `beginExternalVoiceCapture()` (`:1247-1271`): `cancelFade()`,
    `inputPlaceholder = "正在听…"`, `isListening = true`, `phase = .listening`.
    D27-compliant already — it cancels the fade rather than scheduling one.
  - `case "empty"` → `failExternalVoiceState(label: "no speech", variant: .warn)`
    (`:1312-1324`), which ends in `scheduleFade(1800)`.

## Target behavior

- Arming capture from a wake detection emits `voice` phase `listening` carrying the
  `turn_id` the assembler just minted, **before** any envelope produced by the frames
  `arm()` replays, and before `pipeline.run_turn` is entered.
- A turn that reaches commit emits `listening` then `transcribing` with the **same**
  `turn_id`.
- A `WakeArmExpired` outcome emits `empty` with that turn's `turn_id` and a reason
  distinguishing it from the ASR-side empty.
- A detection arriving while the assembler is already armed still arms nothing
  (`:1231-1236`) and still emits nothing — one visible turn, not two.
- No new `op`, no new payload key, no new phase string, no Swift file changed.

## Affected contracts and files

- L5 `jarvis/surface/voice_session.py:1229 DuplexVoiceSession._drain_detection_commands`
  — broadcast `listening` after `arm()` returns and before iterating its outcomes.
  `arm()` returns a fully-computed tuple, so broadcasting between the call and the
  loop is correct wire ordering.
- L5 `jarvis/surface/voice_session.py:405 UtteranceAssembler` — the minted `_turn_id`
  (`:551`) has no public reader; the class exposes `active` (`:517`) and `armed`
  (`:522`). Add a read-only `turn_id` property beside them. Do **not** thread a
  broadcaster into the assembler.
- L5 `jarvis/surface/voice_session.py:1259-1276` — the `WakeArmExpired` branch of
  `_handle_capture_outcome` gains one `self._broadcast("empty", turn_id=outcome.turn_id,
  reason=...)` before its `return`.
- `tests/integration/test_wave3_single_audio_ingress.py` — new checks; the
  `DuplexVoiceSession` harness at `:1581-1615` is the working model, and
  `tests/integration/test_wave2_streaming_media.py:258` is the recording-broadcaster
  stub shape.

## Boundaries and non-goals

- Layers that may change: **L5 only**.
- Must not change: the `voice` wire schema (`jarvis/surface/inherent_output.py:207-241`);
  `EndpointPhase` (`voice_session.py:83-89`); the `voice_capability` envelope
  (`inherent_output.py:286-323`); **any file under `desktop/`**; the legacy
  `voice_wake.py` path; the PTT path; `docs/spec.html`.
- Non-goals: barge-in, live partial transcript on the card, D25 lanes 2 and 3, the
  dormant `/inherent/ws/v2` transport, and any change to what `arm()` itself does.

## Rejected approaches

- **Add `LISTENING` to `EndpointPhase`.** The premise that the wire phase is an enum is
  false on both sides. `DuplexVoiceSession._broadcast` (`:1372`) takes `phase: str` and
  is called with string literals; `InherentBroadcaster.broadcast_voice` (`:207`) takes
  `phase: str` and its own docstring names `"listening"` as an expected value
  (`inherent_output.py:220-224`). `EndpointPhase` is internal — its only reader is the
  `endpoint_phase` property at `:493`; it is never serialized. Adding a member would
  also silently widen `mark_committed`'s state logic (`:507-514`) for no wire benefit.
  ADR-0006's line 188 remains true and unamended: the enum stays four members.
- **Route through the `voice_capability` channel.** Its `state` field carries
  `InputCapabilityState` (`jarvis/surface/voice_audio.py:429-438`:
  `stopped | available | suspended | wake_unavailable | output_unavailable |
  local_capture_unavailable | close_uncertain`) — device/capability availability, with
  no `listening` value and no per-utterance meaning. It is also monotonic-versioned and
  latest-retained (`inherent_output.py:313-320`), which is the wrong lifetime for a
  per-turn state. And the client's `handleVoiceState` is the only path that raises the
  window; `voice_capability` does not reach it.
- **Invent a new phase string (`armed`, `wake`).** The owner's binary would hit
  `default: break` (`NativeCardModel.swift:1241-1242`) — the window would pop up blank
  with no `正在听…` and no fade cancel. `listening` is the only string that makes his
  existing build do the right thing.
- **Emit from `_enqueue_wake_detection`.** No `turn_id` exists yet at that point, it
  runs on the wake thread, and it would emit for detections that `_drain_detection_commands`
  subsequently drops because the assembler is already armed (`:1231`).
- **Terminalize the false wake with a new `dismiss`/`idle` phase.** `empty` already
  maps to `failExternalVoiceState` → `scheduleFade(1800)` in the shipped binary. A new
  string needs a rebuild; reusing `empty` needs none.
- **Change `handleVoiceState` to be phase-aware.** It is already correct — and any
  `desktop/` edit forfeits the "no rebuild" property that makes this deliverable.

## Acceptance evidence

Every check below names the artifact it asserts on. No Python unit tests; `tests/unit`
is retired (`.claude/rules/python-testing.md`).

- **Positive 1 — `listening` precedes `transcribing`, same turn.** In
  `tests/integration/test_wave3_single_audio_ingress.py`, drive a real
  `DuplexVoiceSession` (harness shape at `:1581-1615`) with a recording broadcaster
  through one wake + speech + endpoint round. Assert the recorded
  `broadcast_voice_sync` calls for that round are, in order,
  `("listening", turn_id=T)` then `("transcribing", turn_id=T)` with the **same** `T`,
  and that `T` equals the `turn_id` on the `CapturedUtterance` the recording pipeline
  received. Assert the `listening` call is recorded **before** `pipeline.run_turn` is
  entered.
- **Positive 2 — the wire envelope.** Assert that
  `InherentBroadcaster.broadcast_voice("listening", turn_id=T)` puts exactly
  `{"op": "voice", "payload": {"phase": "listening", "turn_id": T}}` into the fake
  client's `send_json` — the dict itself, not a call count. (`:4472` already calls this
  method for ordering purposes but asserts nothing about the envelope.)
- **Positive 3 — the false-wake terminal.** Same session harness: one wake detection
  followed only by silent frames past `armed_no_speech_timeout_s`. Assert the recorded
  `broadcast_voice_sync` calls are exactly `("listening", turn_id=T)` then
  `("empty", turn_id=T, reason=<the armed-timeout reason>)` with the same `T`, that no
  `"transcribing"` call appears, and that `metrics().armed_no_speech_timeouts == 1`.
- **Regression 1 — no duplicate turn.** A second wake detection delivered while the
  assembler is already armed adds **no** further `broadcast_voice_sync` call.
- **Regression 2 — assembler identity untouched.**
  `tests/integration/test_wave3_single_audio_ingress.py::test_false_wake_armed_timeout_requires_a_fresh_wake_for_later_speech`
  (`:2002`) still passes by name.
- **Regression 3 — no client rebuild required.** `git diff --stat <base>..HEAD -- desktop/`
  prints **nothing**. This is the check that keeps the change deliverable to the owner;
  if it ever prints a file, the card's central claim is void — stop and report.
- **Regression 4 — Tier 1 and the hermetic suite.** `lint-imports`, `ruff`,
  `mypy --strict`, and the full hermetic pytest run, each with its printed counts shown
  raw, measured against a baseline captured **before** the first edit. Never take a
  number from this card.
- **Live run — required, and the daemon restart is the owner's, not yours.** The
  running daemon (pid 85617, port 8009) executes the old code; only a restart picks up
  the change, and the implementation session must not start, stop, or restart it, nor
  open a second audio-owning process (`_FakeBackend`'s real counterpart asserts a single
  default input owner). Deliver instead: a passive read-only WebSocket observer for
  `ws://127.0.0.1:8009/inherent/ws` that prints each envelope — attaching only registers
  one more client in `InherentBroadcaster._clients` (`inherent_output.py:110-128`) and
  disturbs nothing. **Canary the owner confirms:** while he is still speaking, the card
  reads `正在听…`, and the observer has already printed
  `{"op": "voice", "payload": {"phase": "listening", "turn_id": "T…"}}` before the
  `"phase": "transcribing"` line bearing the same `turn_id`. Second canary: a wake
  followed by silence prints `listening` then `"phase": "empty"`, and the card fades on
  its own.

## Docs to sync

- `docs/adr/0006-full-duplex-voice-session.md:172-173, :188` — **unchanged, and this
  must be stated explicitly rather than skipped.** §5's Input FSM already places
  `listening` at `dormant ──arm/wake/PTT──>`, and line 188's claim is about the internal
  `EndpointPhase` enum, which gains no member here. The change makes the code obey the
  existing ruling; it does not create a new one.
- `docs/adr/0014-inherent-realtime-ux.md:1583-1597 (D25), :1616-1626 (D27)` —
  **unchanged.** D25 already names `listening` as the Input lane's first user-visible
  state; D27 already forbids auto-fading it, and the shipped client already honors that
  via `cancelFade()` in `beginExternalVoiceCapture`. Nothing new to record.
- `docs/spec.html` — **unchanged.** It documents no `op:"voice"` phase set (`grep -n
  'transcribing' docs/spec.html` returns nothing).
- If the implementation session finds itself wanting to amend an ADR, that is a signal
  it has deviated from this card. Stop and report instead.

## Open questions

(none)

## /goal condition

Implement `docs/goals/listening-at-wake-arm.md`. Done when ALL of the following appear
in this transcript as raw command output, not as claims:

1. The re-pinned tip: `git rev-parse HEAD` and `git log -1 --oneline`, shown before any
   edit, plus a statement for each `path:line` citation in the card of whether it still
   resolves to the cited symbol. Report any that moved.
2. The measured Tier 1 + hermetic baseline BEFORE any edit — raw output and printed
   counts for `lint-imports`, `ruff`, `mypy --strict`, and the hermetic pytest run.
   Never take a number from the card.
3. The raw output of `grep -n 'self\._broadcast(' jarvis/surface/voice_session.py`
   shown before the edit — expected to be five sites, none of them reachable before
   the assembler commits — demonstrating that no client-visible envelope precedes the
   `"transcribing"` broadcast. If something already emits before it, STOP and report —
   the card's premise is void.
4. The diff, showing: a broadcast of the phase string `"listening"` in
   `_drain_detection_commands` placed after `arm()` and before its outcome loop; a
   broadcast of `"empty"` with a reason in the `WakeArmExpired` branch of
   `_handle_capture_outcome`; and a read-only `turn_id` property on
   `UtteranceAssembler`. `EndpointPhase` gains no member — show
   `grep -n 'class EndpointPhase' -A 8 jarvis/surface/voice_session.py` proving it is
   still exactly four members.
5. `git diff --stat <baseline>..HEAD -- desktop/` printing **nothing**. If any Swift
   file changed, STOP and report: the owner's card binary must not need a rebuild.
6. The raw pass output of the three positive checks and Regression 1 named in
   "Acceptance evidence", each asserting on the recorded `broadcast_voice_sync` call
   sequence / the `{"op": "voice", ...}` dict, not on a call count.
7. Each of those new checks shown FAILING once with the production change temporarily
   reverted, then restored. A check that passes without the fix is a false pass — stop
   and report.
8. `tests/integration/test_wave3_single_audio_ingress.py::test_false_wake_armed_timeout_requires_a_fresh_wake_for_later_speech`
   shown green by name.
9. The full Tier 1 + hermetic output after the change, with no regression against (2).
10. Each entry under "Docs to sync" updated or explicitly judged unchanged, with the
    reason stated. All four are expected to be unchanged. When a change alters a
    documented contract, invariant, ownership boundary, or externally relevant
    behavior, update the canonical document that owns that fact; do not document what
    the code already makes clear; do not duplicate a fact across documents.
11. The passive `/inherent/ws` observer script committed, with its path and the exact
    command the owner runs. Do NOT run a live wake test yourself.
12. `git status` clean, each slice committed per the commit skill, a Progress line
    appended per slice.

Constraints: never start, stop, restart, or send input to the owner's daemon (pid
85617, port 8009) or his InherentCard process. Never build, rebuild, launch, or delete
anything under `.claude/worktrees/realtime-live-test`. Never open a second audio input
owner. Read config from `config/jarvis.yaml` (`realtime.single_audio_ingress` at :272,
`partial_asr` at :311, `barge_in` at :327) and the owner's overlay directly, never from
this card.

If the card contradicts the repository, stop and report; do not redesign. Or stop after
20 turns.

## Progress

- (none yet)
