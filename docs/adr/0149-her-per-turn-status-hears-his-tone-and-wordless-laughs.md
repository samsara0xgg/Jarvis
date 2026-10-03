# ADR 0149 — Her per-turn status hears his tone and wordless laughs

**Status:** Accepted
**Date:** 2026-10-03
**Supersedes:** none

## Context

- Allen, 2026-10-03: he laughs at her and she does not know. A lone 哈哈 or
  呵呵 is a listening sound (`voice_asr.is_backchannel`), dropped with no turn
  and no text, whether or not she is speaking; he found that saying 哈哈 to
  her "does not go in".
- SenseVoice decodes every committed clip whole and labels its emotion and its
  audio event. The emotion is written to `utterance.received` and never shown
  to the model; the event is not read at all (`result.event`).
- Replayed over the 725 kept voice turns of `mac_events.db` on 2026-10-03:
  emotion NEUTRAL 476, EMO_UNKNOWN 209, HAPPY 29, ANGRY 10, SAD 1; event
  Speech 646, BGM 78 (on 11% of ordinary clean speech), Breath 1, Laughter 0
  (none of the clips was a bare laugh, those are dropped before they are kept).
- The state block is per turn and sits after the cached prefix (ADR 0135);
  ADR 0148's `live_context` lines end it.

## Decision

The state block of her next turn ends with one line, "Voice cues (from audio,
may be wrong): ...", from the labels of Allen's last clip and his dropped
laughs, read once.

Its limits:

- It names a non-neutral emotion of the clip that became the turn, a sound
  event in it (Laughter, Cough, Sneeze, Cry, Breath, Applause), and each laugh
  (哈哈 or the Laughter event) dropped as a listening sound since she last
  answered, counted apart for those said while she was speaking. Neutral,
  unknown, Speech and BGM say nothing.
- A dropped clip stays no turn: no interrupt, no answer. Only the label is
  kept, in memory, never the audio, and not written to the event log.
- The cues are cleared when read and ignored after 120 s.

## Alternatives rejected

- **Make a lone 哈哈 a turn** — a turn over her voice stops her, so every laugh
  at one of her jokes would cut it off and start a model call.
- **Put the labels in the system prompt** — they change every turn, and a
  changed prefix is a cache miss (ADR 0135).
- **Surface BGM as "music playing"** — it was set on 78 of 725 ordinary
  clean-speech clips, 11%, so the line would be false about as often as it
  said anything.

## Consequences

- The emotion label is SenseVoice's guess: 10 of 725 clips read ANGRY, mostly
  ordinary questions, so she is told it may be wrong and nothing acts on it.
- A laugh that SenseVoice transcribes as other text, or one under the speech
  gate, is not counted; only 哈哈-like text and the Laughter event are.
- Cues from a clip whose turn is later superseded still reach the next turn.
