# ADR 0040 — A Spoken Answer Is Its Spoken Form

**Status:** Superseded-by-0043
**Date:** 2026-09-24
**Supersedes:** none

## Context

- The local voice chain speaks the voice channel of an answer, and with no
  `<voice>`/`<document>` envelope the voice channel is the whole text. The
  backend prompt stopped asking for the envelope on 2026-09-21, so every
  spoken answer is now the written answer read aloud, list markers included.
  Measured on 2026-09-24: a weather lookup spoke 729 characters in English
  and 218 in Chinese, and answers on 2026-09-20 and 09-22 played for up to
  49 s, a no-tool one for 39 s.
- Before the answer, a turn that runs a tool is silent: a web search took
  4.6 s to its first word on 2026-09-24, all of it tool and model time.
- The backend already runs the fast preset (DeepSeek v4-flash). A no-tool
  answer finished in 1.15 s on 2026-09-24, and the 2026-09-04 bench put a
  short no-tool reply at 0.72 s median.
- spec §3.6.5: TTS plays only text Layer 3 has let through, so whatever is
  spoken has to be produced in Layer 3, not by the voice surface.
- `realtime.commentary` (ADR-0008 D6) already speaks one fixed short phrase
  when a turn's first tool starts. It was tier B on 2026-09-07, off until a
  live run. On 2026-09-24 Allen asked for it to be built now and will take
  that run himself before the branch merges.
- That run, 20:40 on 2026-09-24: a 41-character time answer with a bracketed
  aside played for 7.5 s and read as long-winded; every spoken form opened
  with "Allen，" because the prompt named him; an English web search heard
  "这就去办。". "What time is it?" was answered "现在是晚上9点15分。": the
  model copies `get_current_time`'s Chinese `spoken_time`, whatever the
  profile says about language.

## Decision

An answer that will be spoken (`voice_notify`) and runs longer than about six
seconds of speech (30 Chinese characters, 90 English characters), or carries
list, heading, quote, table, code, bold or bracket markup, gets one no-tool
request for a spoken form of one to three sentences in the language of Allen's
words that turn; so does an answer in the other language. The spoken
form goes in the voice channel and the unchanged answer in the document
channel. Commentary ships on, its phrase picked by the dispatched tool
(read-only, `spawn_worker`, other) and by the language of Allen's words.

Limits: only the model's own answer, never fixed Layer 3 text, a
confirmation ask (heard word for word), a Tier 0 read-back, a stream
correction run, a `gpt_live` turn, or an answer the model enveloped itself. A failed or empty request
speaks the whole answer as before. memory.db and the screen keep the document.

## Alternatives rejected

- **A front model with delegation, the GPT-Live shape.** The backend already
  answers a no-tool question in 1.15 s, so a separate front buys at most a few
  hundred milliseconds. It also adds a second model that can answer without
  asking the backend. What only it offers, talking on while the backend works,
  is not needed first.
- **Cut the written answer at a sentence end, as the Live bridge does.** A
  list answer's first sentence is its header: the 2026-09-24 weather answer
  would have been spoken as "今天（9月24日，周四）温哥华的天气：".
- **Ask the backend prompt for the envelope again.** The pre-v1 voice span was
  itself long: the 2026-09-20 answer to "The." played for 47 s from its voice
  span alone. A separate request with one job and no history keeps the span
  short.
- **Turn on routine streaming and speak from the first segment.** The
  streamed text has no envelope, so it would be spoken whole, which undoes the
  spoken form for no-tool answers like the 39 s one of 2026-09-22. The gain
  is bounded by the 1.15 s a no-tool answer takes today.

## Consequences

- Any spoken answer past about six seconds, a two-sentence reply included,
  pays one more fast-model request before it is spoken or shown: 0.72 s median
  on the 2026-09-04 bench. Typed turns pay it too,
  because their answers are spoken.
- The spoken form is a paraphrase and can drop or blur a detail. Only the
  document is on record; what Allen heard exists nowhere but the event log's
  voice span.
- Commentary's language follows Allen's words, not the answer's: a turn he
  starts in English hears an English phrase even if the answer comes back in
  Chinese.
- A model-written envelope now shows only its document on Resonance and in
  memory.db, so a conclusion placed only in its voice span is lost there.
