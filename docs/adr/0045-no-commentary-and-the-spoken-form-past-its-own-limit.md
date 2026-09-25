# ADR 0045 — No Commentary; the Spoken Form Starts Past Its Own Limit

**Status:** Accepted
**Date:** 2026-09-25
**Supersedes:** 0043

## Context

- ADR 0043 kept ADR 0040's spoken form, moved its trigger to the rewrite's
  own limit (60 Chinese characters, about 40 English words) with Allen's
  words passed along, and kept ADR-0008 D6 commentary: one fixed phrase per
  tool turn, the first lifecycle row that reaches the speaker.
- A row not yet heard is cancelled by the next row of the same action. A
  tool that finishes within a second therefore never speaks its "我查一下"
  acknowledge; the result row's "结果回来了，我整理一下。" plays instead,
  with nothing before it. On 2026-09-25 it did so after `get_current_time`
  mid-chat (11:41, a lookup Allen never asked for) and after `tool_search`
  on two plugin questions (16:02, 16:03).
- Allen, 2026-09-25: the voice model does no long thinking, so a quick turn
  needs no filler; only a long job (his example: making the morning brief)
  might say "稍等我一下", "或者其实都不需要说话，就显示他在". Resonance already
  shows a turn as processing until its answer.
- No tool definition carries an expected duration, and D6 rules out a timer
  that speaks when no lifecycle row changed.

## Decision

Ship `realtime.commentary.enabled: false`: a tool turn says nothing until its
answer. Speak an answer as written when it is plain text within the spoken
form's own limit (60 Chinese characters, about 40 English words); otherwise,
or when it is in the other language, make one no-tool rewrite that is given
Allen's words this turn along with the answer.

Limits of the spoken form, unchanged: only the model's own answer, never
fixed Layer 3 text, a confirmation ask, a Tier 0 read-back, a stream
correction run, a `gpt_live` turn, or an answer the model enveloped itself; a
failed or empty rewrite speaks the whole answer; memory.db and the screen keep
the document. The commentary code stays; the switch turns it back on.

## Alternatives rejected

- **Keep only the acknowledge row, never the result row.** A fast tool's
  acknowledge is cancelled unheard by its own result row, so the two
  2026-09-25 plugin turns would have said nothing anyway, and the 11:41 clock
  lookup would still have announced a lookup Allen did not ask for.
- **Speak "稍等我一下" after a few seconds without an answer.** It needs a
  timer, which D6 excludes, and the only long in-turn job Allen named, the
  morning brief, is not built yet.
- **Let the model write the lead-in.** Measured on 2026-09-25 (ADR 0043):
  2 to 3 of 8 tool turns answered with the sentence alone and never called
  the tool, against 0 of 8 without it.
- **Keep the 30-character trigger and only add the question.** The
  38-character "你是谁" answer would still pay a rewrite that can only make it
  shorter than a limit it already meets.

## Consequences

- A slow tool turn (a web search takes 7 to 10 s) is silent until the answer;
  away from the screen there is no sign that Jarvis heard.
- Answers of 31 to 60 characters are spoken verbatim, up to about 13 s.
- The spoken form is a paraphrase and can drop or blur a detail; what Allen
  heard exists only in the event log's voice span.
