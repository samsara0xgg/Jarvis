# ADR 0404 — The phone and the watch are heard by Qwen through the brain, over the phone's line

**Status:** Accepted
**Date:** 2026-10-23
**Supersedes:** for a brain that hears (ADR 0400), the clause of ADR 0209 that the phone sends the words its own recognizer wrote. Everything else in ADR 0209 stands.

## Context

- ADR 0400 put the Mac's turns on Qwen's streaming recognizer, through the brain, over
  `/terminal/ws`; ADR 0402 made its captions Qwen's words so far.
- The iPhone and the iPad run the same app. They talk to the brain only over `/phone/ws`
  (ADR 0209) and hear with Apple's on-device recognizer, Chinese and English side by side, one
  of the two picked per utterance. The watch sends its microphone to the phone as 16 kHz Int16
  over WatchConnectivity; the phone hears it with the same recognizer and sends the words to the
  brain over HTTP.
- The owner, 2026-10-23: every terminal hears the same way. The capture, the wake word and the
  end of an utterance stay on each terminal; the brain only takes the utterance's audio,
  streamed to it. The screen shows the words changing one by one.
- The phone's wake word is found in its recognizer's running words, and an utterance ends 0.8 s
  after the recognizer's last new word with the level low.
- Qwen's key is on the brain only (ADR 0400).

## Decision

The phone's line carries the brain's `hear` frames, served by the same hearing as
`/terminal/ws`; `ready` says `hear: true` when the brain hears. The phone streams the utterance
it hears while listening, and a wake utterance from the wake hit with the 10 s before it, then
takes Qwen's final words at the commit (its own when they are missing) and shows Qwen's words
as the caption from the first of them. A spoken `say` carries `asr_engine`. A watch talk streams
through the phone's line the same way, the phone opening the line for it when it is closed.

## Alternatives rejected

- **The phone opens `/terminal/ws` too, for hearing** — there a voice terminal is one that
  speaks: it gets the row stream and answers are routed to it (ADR 0172), and a terminal that
  does not speak may not send `hear` frames. The phone would hold two sockets to the brain, and
  `/terminal/ws` a third kind of terminal, for one frame type the line it already holds carries.
- **The phone calls Qwen itself** — the key would live on every phone and iPad, where ADR 0400
  and the owner keep it on the brain.
- **The watch streams to the brain itself** — the watch has no line to the brain; its talk
  already goes through the phone, which holds the line.

## Consequences

- The microphone audio of each utterance the phone or the watch hears now goes, through the
  brain, to Alibaba's Singapore endpoint, while the brain has the key.
- His words over her keep the phone's own: a segment that starts while she speaks is not
  streamed, so an over-her judgement and the words it sends stay as ADR 0209 has them. A
  barge-in opens the stream with the segment's kept audio.
- The commit waits for Qwen's final words up to 4 s, as the Mac's does; a line that is down or
  a brain that does not hear leaves the phone's words, as `qwen_unavailable`.
- A watch talk keeps the phone's line open for its length, which the brain reads as the app
  being open (ADR 0210).
- The watch's turns go over HTTP, so their `asr_engine` is in the phone's trace, not on the
  brain's row.
