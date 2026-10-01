# ADR 0109 — What is heard so far shows while you are still speaking

**Status:** Accepted
**Date:** 2026-10-01
**Supersedes:** none

## Context

- Allen, 2026-10-01: while he spoke, the companion's talk area did not
  follow his words, and he expected it to grow with them as he said them.
  Until now the surface got his words once, in `accepted`, after the
  utterance had ended and Whisper had finished it.
- The daemon already decodes the utterance so far with SenseVoice for ADR
  0006 D7's semantic endpoint. That lane runs only when
  `partial_asr.enabled` is on, ships off as tier B, and turning it on also
  changes when an utterance ends: after a pause, a finished-looking prefix
  ends it and an unfinished one holds up to `max_hold_ms` more. On the 200
  recordings replayed for it, about one in twelve ends at a mid-thought
  pause.
- A snapshot decode was measured at about 15 ms per second of audio for ADR
  0006 D7's budget, so one every 240 ms fits for an utterance of up to about
  15 seconds.
- The words are Allen's, and the surface is on the same machine; `accepted`
  already carries them over the same local socket.

## Decision

While an utterance is being spoken, the daemon sends the surface what has
been heard of it so far, and the surface shows it where it shows his words.
This is for showing only: it never decides when an utterance ends, and
`partial_asr.captions` switches it independently of `partial_asr.enabled`.

Its limits:

- It is SenseVoice's snapshot decode, not the transcript the turn is
  answered from; the accepted words still come from the final recognizer
  alone, and the surface drops any partial that arrives after them.
- It is sent only when it changed, at most once per `partial_asr.interval_ms`,
  and a decode that cannot keep up stops it for that utterance (the lane's
  existing degrade) without touching the endpoint.
- Nothing about it is stored, logged or sent to L3; the log records only that
  the lane ran.
- The push-to-talk path, which gets its words in the HTTP answer, has none.

## Alternatives rejected

- **Turn on `partial_asr.enabled` and show its revisions.** It would also
  switch the endpoint to the semantic hold, which has no live run behind it
  (tier B), so a display request would carry the one-in-twelve mid-thought
  ends with it.
- **A streaming recognizer for the display.** A second model to load and keep
  warm, for words the existing snapshot lane already produces.
- **Keep showing his words only once accepted.** That is the behaviour Allen
  said did not follow what he said.

## Consequences

- What is shown can differ from what is accepted: a different recognizer, no
  normalization, and an earlier word may change as later audio arrives.
- SenseVoice decodes about four times a second while he speaks, on its own
  thread; an utterance past about 15 seconds stops updating its
  words and the area shows what it last had.
- The surface learns a new voice phase, `partial`, with the words in `text`;
  a surface that does not know it ignores it, as the wire already allows.
