# ADR 0332 — A finished rollout loses its flag and its old path

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** none

## Context

- On 2026-09-07 commit 728afae6 ("adopt realtime tier A") shipped twelve realtime switches on,
  each with live-burn evidence. Among them: `input.intent_pump`, `streaming_output.enabled`,
  `single_audio_ingress.enabled`, `concurrency_safety.transactional_event_append`,
  `concurrency_safety.lifecycle_terminal_cas`, `response.response_run_lifecycle` and
  `response.independent_response_cancel`. Four guarded a new path over an old one that stayed in
  the tree: the serial intent watcher, the legacy wake listener, the batch `TTSPipeline` and the
  turn without a ResponseRun. The two wave-1 switches (transactional append, terminal
  compare-and-set) only decided whether the others downgraded.
- 44 days later (2026-10-21) no commit on main has set any of them false:
  `git log -G'<key>: false' -- config/jarvis.yaml` lists only the commit that introduced the key
  (2026-08-31 to 2026-09-03) and 728afae6. The same rule was applied once before: `action_runner`
  and `true_async_workers`, flipped in the same commit, went in 988b1a5a on 2026-09-20.
- The old paths were not free. They kept `_wave4_response_activation` and
  `_single_ingress_activation` as ladders that downgraded one switch when another was off, kept
  `JarvisRuntime.llm_session_factory` optional, and had their own tests: the 2026-10-20 audit
  counted 11 test hits for the watcher and 15 for the wake listener. The seven commits that
  retire the switches above and the v2 sequencer below delete 11,927 lines and add 756
  (`git show --shortstat` on each).
- Forcing the two response switches and the two wave-1 switches on in every test config failed 8
  of 6,086 tests, all expected: tests of the old path or of a downgrade. The old paths were
  reachable only from tests that asked for them.
- `realtime.inherent.v2_sequencer.enabled` was added on 2026-09-05 and has been false since. Its
  only consumer, `/inherent/ws/v2`, has no caller in `desktop/`, `ios/`, `native/`, `scripts/` or
  `tools/`, and ADR 0015 keeps the v2 wire off.
- ADR 0047 (2026-09-25) kept two things out of its own cleanup because retiring them needs a
  person: routine streaming, which runs through the render path and is judged by ear, and the
  device-profile resolver and route observer, which run inside the live input epoch and need a
  hardware run.

## Decision

Delete a rollout flag, its config key, its old path and the tests that pin only that path in one
change, once the shipped value has been the new path for 30 days or more and no device the owner
runs sets the old value.

- The date is `git log -G'<key>' -- config/jarvis.yaml`, and the retiring commit's message cites
  it with the proof that nothing else reaches the old path. The same message names what a device
  that still sets the old value would lose, so the owner can check his Mac and Pi before it lands.
- A key with a second meaning stays: `single_audio_ingress.enabled` is also the per-machine
  "this machine listens" switch, and `realtime.enabled` is the parent of the block.
- A switch that was never on follows ADR 0047, except one whose only consumer has no client in
  the repository: the v2 sequencer, wire and hub are deleted whole.
- Where the old path is the only one that has run on hardware or by ear, retirement still waits
  for that run, as ADR 0047 says for the route observer and routine streaming.

## Alternatives rejected

- **Keep each old path behind its flag as the fallback.** For these switches the old path has
  not run in a live daemon since 2026-09-07, which `git log` on the config shows (above), and a
  flip-back needs a restart onto code no one has run for 44 days. `git revert` of one retiring
  commit restores the same code in the state its tests last ran. A recorded flip-back of any of
  them would refute this.
- **Warn first and delete after a deprecation window.** The shipped config sets none of the old
  values, so the window ends with no device having used it, and the warning needs the branch it
  announces. A device found setting an old value is the case the commit message's check covers.
- **One ADR per flag, as the audit's R6 proposed.** Every flag passes the same two tests, a date
  and a device check, and the commit carries both with its numbers. A flag whose retirement turns
  on a judgment the commit cannot hold (ADR 0047's two) is the exception this ADR already names.
- **Wait for a hardware run of each retirement.** ADR 0047 needs one because the new path there
  was never on. Here the new path is the one the owner's devices have run daily since 2026-09-07,
  and a run adds nothing the daily use has not. If a retirement breaks what he hears, the revert
  above is one commit.

## Consequences

- A bad new path can no longer be switched back from config. The way back is `git revert` of the
  retiring commit and a restart.
- A device whose settings turn `single_audio_ingress.enabled` off, or whose single ingress
  declines at boot, has text and whole-WAV push-to-talk upload only: the legacy wake listener is
  gone. A boot where the media actor cannot start (`realtime.enabled` false, an invalid
  `streaming_output` block, no TTS endpoint) is text-only; the batch pipeline no longer speaks.
- `realtime.enabled: false` no longer switches the ResponseRun lifecycle off; it still downgrades
  routine streaming, spoken streaming and commentary, which need the realtime parent.
- A terminal older than ADR 0277 (2026-10-15) gets `unknown_op` for the six ops the brain stopped
  answering; every terminal must be on a build from that date.
- An overlay that still names a retired key is ignored, not an error.
- Two switches that meet the rule stay for now. `concurrency_safety.confirmation_dispatch_outbox`
  is taken by the tool registry in `jarvis/execution/tools.py`, which another change owns.
  `concurrency_safety.exactly_once_cost_accounting` fails 112 of 6,113 tests in 23 files when
  forced on, because their model stubs predate ADR 0326's one scripted model. Each retires under
  this rule when that blocker is gone.
