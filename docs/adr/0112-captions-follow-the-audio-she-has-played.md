# ADR 0112 — Captions Follow the Audio She Has Played

**Status:** Proposed
**Date:** 2026-10-01
**Supersedes:** none

## Context

- The talk area lights her spoken line character by character from a fixed
  speed (`SPEED` in `desktop/resonance/src/talk.ts`, 4.5 characters a second
  for Chinese, about a third of that cost for Latin letters) counted from the moment
  the line lands on screen. `talk.ts` itself says it is "estimated, not
  reported".
- Real speech speed varies with the voice, the words and the provider, so on a
  long line the lit text drifts from the audio, ahead or behind. The clock also
  starts before the TTS provider has produced its first audio.
- Over her voice, ADR 0100 lowers her at onset and holds her once the speech
  has `barge_in_confirm_voiced_s` of voice. A held answer moves no cursor
  (`AudioStreamPlayer.pause_generation`), but the timer keeps running until a
  `cutAt` arrives, so the captions move on after the voice has stopped.
- The daemon already knows the real position. `StreamingTTSPipeline` polls the
  playback ledger every `presentation_poll_s` (5 ms) and appends
  `surface.playback_checkpoint` events whose `heard_text` is the conservative
  played prefix, at word granularity when the provider reports word
  boundaries, at segment granularity otherwise. These reach only the event
  log; the companion never receives them.
- The companion already holds one WebSocket to the daemon whose envelopes are
  `{"op", "payload"}`, and `InherentBroadcaster.broadcast_op_sync` is the
  worker-thread bridge the media owner's broadcaster already offers.
- The heard text is the speech text (`_preprocess_for_speech`: markdown and
  emoji stripped, `<voice>` parts joined without a break), not the displayed
  `<voice>` text, which keeps its line breaks and link markup.

## Decision

Push the heard text of the answer playing now to the companion as a
`playback` op, and light her spoken line up to that text instead of the
timer.

Its limits:

- Payload `{"turn_id", "response_id", "heard"}`, with `heard` the whole
  conservative prefix played so far, sent at most about 16 times a second and
  only when it changed, plus once, unthrottled, from the terminal snapshot so
  a stop lands exactly. No message is sent while she is held: the caption
  stops because the position does.
- `heard` is matched against the displayed line as a prefix, skipping spacing,
  markdown marks and emoji on either side (`heard.ts`). When it is not a
  prefix (a markdown link, a rewritten line) the line falls back to the timer
  estimate.
- Without any `playback` message (an older daemon, the demo, a path that does
  not report), the timer estimate is unchanged. Once the daemon has reported
  on any line, a line it has not reported on waits up to `GRACE_MS` for its
  first word before the estimate takes over.

## Alternatives rejected

- **Report the character count only.** The speech text and the displayed text
  differ by markdown, emoji and line breaks, so a count of one is not a count
  of the other; the text maps exactly where a number needs a ratio.
- **Poll the event log from the companion.** `playback_checkpoint` is
  deduplicated by text hash for durability and written with retries; it would
  add a read path and a polling delay to what the open WebSocket carries for
  free.
- **Derive the position from the audible sample count and keep the speed
  model.** A held answer would stop it, but the drift on a long line, which
  comes from the speed model, would stay.
- **Send `paused` and `ended` states.** A held answer moves no cursor and the
  existing `spoken` voice op already ends a line; a state field would repeat
  what the position and `spoken` already say.

## Consequences

- When the provider reports no word boundaries, `heard` moves one segment
  (a sentence) at a time, so the captions advance in sentence steps while the
  audio does not.
- Each message carries the whole prefix, so a long answer sends a few
  kilobytes a second at most; a delta would be smaller and would need a
  resync after a dropped frame.
- A line whose heard text is not a prefix of the displayed line still runs on
  the timer and so still drifts or runs on while held.
- Acceptance is a live run: a long answer spoken with the captions in step,
  and a barge-in that holds her with the captions stopping with the voice.
