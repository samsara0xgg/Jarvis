# ADR 0408 — His words over her on the phone are heard by Qwen, and a pause does not cut them

**Status:** Accepted
**Date:** 2026-10-24
**Supersedes:** in ADR 0404, the consequence that his words over her keep the phone's own. Everything else in ADR 0404 stands.

## Context

- On the phone, a segment that starts while she speaks is judged against her own recent words:
  her echo, or his words over her. His words hold her (she pauses, at most 8 s) and are sent
  when they end (ADR 0209). They end as any utterance does, 0.8 s after the recognizer's last
  new word with the level low.
- ADR 0404 left those words Apple's: a segment over her was not streamed to Qwen.
- 2026-10-23 23:39 on the iPad: one long sentence over her was sent in pieces, one at each
  pause, in Apple's words.
- A segment's audio is kept from its start until a stream opens (ADR 0404); the start of his
  words inside it is known from the first verdict that is not her echo.
- The owner, 2026-10-24, approved: while she speaks, his pauses do not cut the sentence, and
  those words are heard by Qwen.

## Decision

When his words over her hold her, stream them to Qwen from one second before the first of them,
end them after 1.5 s of quiet instead of 0.8 s, and send Qwen's final words, the phone's own
when Qwen's are missing.

## Alternatives rejected

- **Stream every segment over her from its start** — most of them are her echo, and the audio
  before his first word is her voice through the speaker, which Qwen writes down as words.
- **Keep the 0.8 s end** — it is the end that cut the 23:39 sentence into pieces: his pause in
  the middle of a sentence is longer than 0.8 s.
- **End held words only at the 8 s cap** — she would stay paused for the rest of the 8 s after
  he finished every time.

## Consequences

- She stays paused 0.7 s longer after his last word over her.
- Qwen's words start a second before his: the end of her own word can be in them.
- The send waits for Qwen's final words up to 4 s, as any of the phone's utterances does.
- His words over her on the Mac are not changed by this.
