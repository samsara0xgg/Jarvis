# ADR 0041 — Wave Mode Listens Without a Wake Word

**Status:** Accepted
**Date:** 2026-09-24
**Supersedes:** none

## Context

- Allen wants to test full-duplex talk with the local voice chain (not
  GPT-Live): while Resonance shows its wave, he talks without "Hey Jarvis"
  and can cut Jarvis off by speaking. On 2026-09-24 he asked for the wake
  word to open the wave too, and accepted that this departs from ADR-0006
  D9.
- Today every utterance needs a wake hit: the capture arms only from one and
  drops back after `armed_no_speech_timeout_s` (3 s). Clicking the orb into
  the wave only changes Resonance's own state; the daemon hears nothing of
  it.
- A wake hit while Jarvis speaks is suppressed, so the only way to talk over
  it is a typed turn. A new turn does supersede playback, but only once its
  answer is emitted, seconds after Allen stops talking.
- ADR-0006 D9 allows natural barge-in only on a device profile that passed
  its own acceptance run; none has, and the route observer that would key
  it is off. D8's spoken barge-in needs a wake phrase plus a keyword and
  partial ASR, and on 2026-09-05 raised 7.53 false candidates a minute on
  the MacBook speakers against a 0.5 target.
- The reSpeaker XVF3800 is plugged in. With the Mac output on the
  Multi-Output Device that feeds it a reference, its conferencing channel
  (the one the daemon reads) cancelled speaker music to about -90 dBFS up to
  volume 0.5 on 2026-09-14.

## Decision

While Resonance is in wave mode, entered from the orb or by the wake word,
the daemon listens without a wake word, and speech that starts while Jarvis
is speaking stops what is audible and cancels an answer still being
written.

Limits: mic mute and GPT-Live still close it; the switch lives only in the
daemon's memory, is set by the surface, and drops when the last surface
disconnects; D9's per-profile acceptance gate does not apply in this mode.

## Alternatives rejected

- **D8's two-stage spoken barge-in (wake phrase + interrupt keyword).** It
  asks Allen to say "Hey Jarvis, 停" to interrupt, which is the wake word he
  wants to stop saying, and it measured 15 times its false-candidate target
  on the MacBook.
- **Stop playback only when the new utterance is transcribed.** Jarvis would
  talk over Allen for the whole utterance plus the acoustic endpoint and
  ASR; the 2026-09-24 questions ran 2-3 s before their pause.
- **The daemon enters conversation mode on the wake word by itself.** With
  no surface open nothing could leave it, and the Mac would keep sending
  every sentence in the room to the backend.

## Consequences

- Nothing checks the audio route. On the built-in mic and speakers (no
  echo cancellation) Jarvis hears itself, stops itself, and can answer its
  own words; the mode is only usable on headphones or on the reSpeaker with
  the Multi-Output reference.
- A cough or a door during Jarvis's speech stops it: the stop fires at
  Silero speech onset, with no transcript behind it.
- Any speech in the room while the wave is open becomes a turn.
- Capture during Jarvis's speech runs the `record` VAD profile, which was
  never measured against playback; `output_active_vad_mode` is the knob.
- The wake word opens the wave only when Resonance is running.
