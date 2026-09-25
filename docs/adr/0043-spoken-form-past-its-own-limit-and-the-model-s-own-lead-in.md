# ADR 0043 — Spoken Form Past Its Own Limit, and the Model's Own Lead-in

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
- The conversation model moved to gpt-5.6-luna on 2026-09-25 (72f56bd):
  first token about 0.7 s on short history, and it writes no text beside a
  tool call unless asked (0 of 105 replayed turns).
- ADR-0008 D6 commentary speaks a fixed phrase when a turn's first tool
  starts. Allen's 2026-09-25 voice test: the lead-in is the same sentence
  every time. His call the same day: let the model say it, since the model
  is fast now.
- D6's call-graph rule is that no model is called only to produce the
  phrase. Words the model writes in the response that proposes the tool
  cost no extra call.
- Asking for that sentence in the system prompt, as text beside the call,
  failed on a silent rig on 2026-09-25: of 8 turns that needed a tool, 3
  wrote the sentence, 3 did not, and 2 answered with the sentence alone
  ("我先查一下你已保存的备忘录。") and never called the tool.

## Decision

Speak an answer as written when it is plain text within the spoken form's
own limit (60 Chinese characters, about 40 English words); otherwise, or when
it is in the other language, make one no-tool rewrite that is given Allen's
words this turn along with the answer. Give every tool the model sees one
more required argument, `lead_in`, the sentence to say before the work
starts; Layer 3 takes it out before the tool reads its arguments, and when it
is one short plain sentence (30 Chinese / 90 English characters, no markup)
the commentary acknowledge speaks it instead of a fixed phrase.

Limits, unchanged from ADR 0040: only the model's own answer, never fixed
Layer 3 text, a confirmation ask, a Tier 0 read-back, a stream correction
run, a `gpt_live` turn, or an answer the model enveloped itself; a failed or
empty rewrite speaks the whole answer; memory.db and the screen keep the
document. The fixed phrases stay as the fallback when the model writes no
lead-in or one too long to speak, and the per-turn one-commentary cap and
every D6 suppression rule still apply.

## Alternatives rejected

- **Keep the 30-character trigger and only add the question.** The
  38-character answer above would still pay a rewrite that can only make it
  shorter than a limit it already meets.
- **A separate model call to write the lead-in.** It adds a request per tool
  turn and is exactly what D6 rules out; the tool-proposing response already
  carries the text for free.
- **Ask for the lead-in as text beside the call.** Measured above: it came
  3 times in 8, and twice the sentence replaced the tool call. A required
  argument cannot be written without making the call.
- **Speak any length.** A model that puts its plan in the lead-in would read
  a paragraph before any result; the cap falls back to the fixed phrase.

## Consequences

- Answers of 31 to 60 characters are now spoken verbatim, so a plain
  two-sentence answer plays for up to about 13 s instead of being cut down.
- The lead-in is model text: nothing mechanical stops it from claiming a
  result before the tool returns, which a fixed phrase could not do.
- Every tool schema the model sees is one argument larger than the tool's
  own, and every tool call carries a few more output tokens.
