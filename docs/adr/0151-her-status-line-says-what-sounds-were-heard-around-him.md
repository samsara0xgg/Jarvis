# ADR 0151 — Her status line says what sounds were heard around him

**Status:** Accepted
**Date:** 2026-10-03
**Supersedes:** none

## Context

The voice session already holds Allen's microphone audio, echo-cancelled, 24 hours a day, and
Jarvis hears only his words from it. A cough, a sigh, a phone ringing or music playing is
context a person in the room would have. Apple SoundAnalysis (`SNClassifySoundRequest`,
version1, 303 labels) runs on this Mac at about 0.3% of one core.

Allen's live test on 2026-10-03 (3 s windows, 0.5 overlap) settled how far to trust it. Real,
confirmed: cough 95-99%, sigh 92-99%, typing 91-99%, laughter 43-59%, sneeze 50%, ringtone
52-61%. False: door 66%, bird 31%, drawer 77%, pig_oink 58-69%. Allen's decision: false
positives cost more than misses, so every label needs 0.9; his real laugh scored under that
and is the one exception below. Breathing is dropped. Her own voice leaks into
the mic even after echo cancellation, and audio is private: it must never be written down.

## Decision

Run SoundAnalysis in a Swift helper (`native/ambient_sounds`) fed the voice session's
diagnostic-lane frames, and put one line of recent whitelisted labels at or above 0.9 into the
state block as the last `live_context` producer, with laughter alone also reported as
"maybe" (with its confidence) when its family scores 0.4 to 0.9 in two consecutive windows,
dropping any result whose 3 s window overlaps
her playback (plus 0.5 s) or a muted microphone; only label names and times are kept, in
memory, `realtime.ambient_sounds: false` turns it off, and she never speaks about a sound
unprompted.

## Alternatives rejected

- **Per-label lower bars stated as fact (sneeze 0.5, ringtone 0.5, door 0.66)** — they matched
  the confirmed events, but the same test produced door 66% and drawer 77% that never
  happened; one 0.9 bar leaves none of those and keeps cough, sigh and typing at 92-99%.
- **A second microphone capture inside the helper** — a second owner of the input device is
  what the single audio ingress exists to prevent, and the helper would hear her own voice
  without the echo cancellation the ingress applies.
- **Running SoundAnalysis in Python** — no Python binding exists; the helper adds one process
  that exits when its stdin closes, like `native/voice_out`.

## Consequences

A real sneeze or ringtone scored below 0.9 is not reported, and a laugh is only ever a guess. A sound that began before
the last turn that showed a line is not shown again, and a sound heard while she spoke is
lost. The helper is built with `swiftc` on first start in the background, so the line is empty
until that finishes; if it keeps dying (five restarts) the line stays off until the next
daemon start.
