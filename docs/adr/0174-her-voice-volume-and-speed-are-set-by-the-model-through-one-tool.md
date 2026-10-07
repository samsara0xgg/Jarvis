# ADR 0174 — Her voice volume and speed are set by the model through one tool

**Status:** Accepted
**Date:** 2026-10-06
**Supersedes:** none

## Context

- Allen, 2026-10-06: he wants to say "louder", "faster", "back to normal" or "keep it
  like this" and have her voice change, without a Settings page. The only volume
  today is `realtime.tts_volume` (MiniMax `vol`, fixed at boot) and a player gain
  (ADR 0052); `voice_setting.speed` is a literal 1.0.
- Whether a line is about her own voice is language, not a pattern: "啥？你说啥"
  and "听不清" mean say it again, "turn it up" may be the music. A fixed phrase
  or Jev option would mistake these; the model reads the sentence.
- MiniMax reads `voice_setting` once, at `task_start`. The streaming pipeline
  connects the next answer's session while Allen is still talking (ADR 0053), so
  that session carries the voice of that moment, before any tool has run.
- Raw numbers from the model drift ("a bit" became 10, 20 or 50 in turn).

## Decision

Her voice is set by one model-only tool, `set_voice`, whose arguments are
direction words (`louder_a_bit`, `louder_a_lot`, `quieter_a_bit`, `quieter_a_lot`,
`faster_*`, `slower_*`), `reset` and `remember`; the program owns the
numbers and every TTS task start reads them from one shared holder.

Its limits:

- Steps: volume ×1.4 or ×2 louder, ×0.7 or ×0.5 quieter (about 3 dB and 6 dB, the
  same change at any level; 15 and 30 points were near the edge of hearing, 2026-10-06),
  speed 0.2 or 0.4. Bounds: volume 30 to
  300 %, speed 0.6 to 1.8, clamped, and the result says `at_limit`.
- No confirmation before a loud step (dropped 2026-10-06): 200 % is only about 6 dB
  louder, and the 300 % ceiling is where MiniMax's peaks reach about -2 dBFS, so no step
  can distort.
- The holder keeps `current` and `default`. Only `default` is saved
  (`voice-settings.json`, written by `remember`, cleared by `reset`) and loaded at
  boot. `current` returns to `default` when conversation mode goes from on to off.
- `vol` is the configured base volume times the percent. A session connected
  before the change is closed and reconnected when an answer takes it.

## Alternatives rejected

- **A Jev option and Tier 0 rows per direction** — "louder" is confused with the
  existing repeat option ("听不清") and with music or system volume; the model tells
  them apart from the sentence and already replies in her own words.
- **Absolute numbers in the tool** — models answered "a bit" with different steps
  in the same conversation; fixed steps are the same every time and the bounds
  are enforced in code, not in a prompt.
- **Change the player gain instead** — it would also scale audio already queued
  and the echo canceller's far end, and cannot change speed.
- **Persist `current`** — a volume set for one loud room would greet the next
  quiet morning; keeping it is an explicit `remember`.

## Consequences

- A turn that changes the voice and was prewarmed costs one extra handshake for the
  answer that follows. Audio already rendered keeps the old voice.
- A voice changed outside conversation mode stays until the next conversation ends
  or a restart; there is no timeout.
- The saved file holds one pair; `reset` forgets a remembered default for good.
