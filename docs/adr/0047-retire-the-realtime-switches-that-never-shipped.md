# ADR 0047 — Retire the Realtime Switches That Never Shipped

**Status:** Accepted
**Date:** 2026-09-25
**Supersedes:** none

## Context

- Two realtime features ship `false` and have never been on in the live
  daemon: ADR-0006 D8's spoken barge-in (`single_audio_ingress.barge_in`
  `enabled` and its keyword stage) and ADR-0014 D14's durable confirmation
  expiry (`confirmation.durable_expiry`).
- Each already has a recorded rejection or a live replacement:
  - D8: ADR 0041 rejects it (15 times its false-candidate target on the
    MacBook); conversation mode's stop-on-speech is the interrupt in use.
  - D14: read-time expiry (`PendingConfirmationSlot.is_live`) already
    governs every reader. The sweep existed to clear an idle Swift card
    panel; ADR 0015 retired the card, and Resonance renders no confirmation.
- Allen's bar for the 2026-09-25 repository cleanup: no change to the
  current experience, and GPT-Live with its delegation path stays and is
  maintained.
- A switch that is off cannot change what runs. The only risk in removing
  one is removing code its on-path shares with a live path.

## Decision

Delete the code, config keys, tests and scripts that exist only for these
two features, and keep every piece a live path shares with them: the
conversation-mode interrupt callable and the `confirmation.expired` event
type with its folds.

Outside this decision, each for its own reason:

- ADR-0008's routine streaming (`response.routine_streaming` with
  `streaming_output.speak_from_segments`), which ADR 0040 rejects: its
  branches run through the per-turn decide loop, `drive_turn`, the render
  path and the TTS playback state machine, so retiring it needs a live run
  judged by ear.
- ADR-0006 D9's device-profile resolver and route observer: they run inside
  the live input epoch commit and device poll, so retiring them needs a
  hardware run.
- Commentary (ADR 0045 keeps its code behind its switch), partial ASR
  (tier B, a planned next step), and the v2 sequencer and hub (ADR 0015
  keeps them, and GPT-Live's delegation writes through the v2 input inbox).

## Alternatives rejected

- **Keep the two dormant behind their switches.** Each one's own ADR trail
  rejects it or names its replacement, so turning one back on already needs
  a new ADR; the switch saves nothing a `git revert` does not. Meanwhile the
  code sits inside files every voice change edits (`voice_session.py`,
  `inherent_loop.py`, `runtime/__init__.py`) and its tests run in every
  Tier 1 pass.
- **Retire every off switch in one pass.** Routine streaming and D9 sit in
  paths where a slip is heard as a broken answer or a dead mic, which the
  hermetic suite cannot rule out on its own; commentary was switched off
  hours earlier by an ADR that keeps its code for a flip back; partial ASR
  has no replacement; GPT-Live's delegation depends on the v2 input inbox.

## Consequences

- Bringing either back means reverting its commit and writing an ADR that
  overturns the rejection that retired it.
- `confirmation.expired` stays in the schema with no producer; the folds
  still accept it, so an existing log reads unchanged.
- D9 keeps computing a barge mode that nothing acts on; it only labels the
  input capability until a later change retires it with a hardware run.
- No Inherent route or payload changes: D8's PTT confirm ran inside
  `/inherent/asr-submit`, whose request and response shapes stay the same.
