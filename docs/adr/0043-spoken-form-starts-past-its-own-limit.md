# ADR 0043 — The Spoken Form Starts Past Its Own Limit

**Status:** Accepted
**Date:** 2026-09-25
**Supersedes:** 0040

## Context

- ADR 0040 made the local voice chain speak a short spoken form: an answer
  over 30 Chinese / 90 English characters, or with markup, gets one no-tool
  rewrite of at most three sentences and 60 Chinese characters (40 English
  words).
- The rewrite sees only the answer. On 2026-09-25 the 38-character answer
  to "你是谁" ("我是 Jarvis，你的个人 AI 助理。你是 Allen，住在 BC 省
  Victoria。") went to the rewrite because it passed 30 characters; with no
  question to go by, it dropped "我是 Jarvis" once and swapped the two
  sentences the next time. The answer was already inside the rewrite's own
  60-character limit, and the extra request cost about a second before
  the first word.
- ADR-0008 D6 commentary speaks a fixed phrase when a turn's first tool
  starts. Allen's 2026-09-25 voice test: it is the same sentence every time.
  He asked for the model to say it, since gpt-5.6-luna (72f56bd) is fast.
- On a silent rig the same day (gpt-5.6-luna, typed turns, 8 that need a
  tool), with no lead-in asked for, every turn called its tool. Asking for
  a lead-in made some turns answer with the sentence alone ("我查一下维多利亚
  明天的天气。") and never call the tool: 2 of 8 as a prompt line, 2 of 8
  as a required `lead_in` argument on every tool schema, 3 of 8 when that
  argument's description carried an example sentence.

## Decision

Speak an answer as written when it is plain text within the spoken form's
own limit (60 Chinese characters, about 40 English words); otherwise, or
when it is in the other language, make one no-tool rewrite that is given
Allen's words this turn along with the answer. Commentary keeps its fixed
phrases.

Limits, unchanged from ADR 0040: only the model's own answer, never fixed
Layer 3 text, a confirmation ask, a Tier 0 read-back, a stream correction
run, a `gpt_live` turn, or an answer the model enveloped itself; a failed or
empty rewrite speaks the whole answer; memory.db and the screen keep the
document. Commentary picks its phrase by the dispatched tool (read-only,
`spawn_worker`, other) and by the language of Allen's words.

## Alternatives rejected

- **Keep the 30-character trigger and only add the question.** The
  38-character answer above would still pay a rewrite that can only make it
  shorter than a limit it already meets.
- **Let the model write the commentary sentence.** Measured above: in 2 to
  3 of 8 tool turns the sentence replaced the tool call, against 0 of 8
  without it. A fixed phrase that repeats is better than a promise with no
  action behind it.
- **A separate model call to write the lead-in.** It adds a request per
  tool turn and is what ADR-0008 D6 rules out.

## Consequences

- Answers of 31 to 60 characters are now spoken verbatim, so a plain
  two-sentence answer plays for up to about 13 s instead of being cut down.
- The spoken form is a paraphrase and can drop or blur a detail. Only the
  document is on record; what Allen heard exists nowhere but the event log's
  voice span.
- Commentary still repeats: the same few phrases per tool kind.
