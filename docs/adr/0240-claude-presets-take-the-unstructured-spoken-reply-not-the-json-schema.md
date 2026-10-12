# ADR 0240 — Claude presets take the unstructured spoken reply, not the JSON schema

**Status:** Accepted
**Date:** 2026-10-11
**Supersedes:** none

## Context

- ADR 0114 puts every request of a spoken-route turn under a strict JSON schema
  `{spoken, written}` when `realtime.response.spoken_streaming.structured` is on. It was
  measured on gpt-6-luna alone (20 real turns, 20/20 valid). On Claude the same schema goes
  out as `output_config.format`, and the JSON reply is the turn's whole answer: a reply that
  is only "我查一下…" ends the turn with no call.
- Replay exam of 2026-10-11, 80 real decision points, three runs each, Haiku 5.5 at medium
  effort with its preset's system note (ADR 0220): with the schema 53 of 80 cases pass all
  three runs (182 of 240 runs). The typical failure is a JSON reply of "我查一下…" or
  "I need to check…" and no call.
- The same requests with `_SPOKEN_REPLY_NOTE` in place of the structured note and no schema:
  63 of 80 (203 of 240). Sonnet 5.5 with the schema: 59 of 80.
- Of the 129 answers Haiku gave without a call on the unstructured route, 19 used
  `<voice>`/`<document>`, 110 were plain text, and 9 of those plain ones carried list markup
  without tags.
- Anthropic labels no message phase, so a line the model writes before a call is read as part
  of the answer (the structured reply has no place for one).

## Decision

A preset may say `spoken_structured: false`; a spoken-route turn on it sends the unstructured
spoken note and no text format on every request, the prefix warm after the turn included, and
the Claude presets (`haiku`, `haiku-think`, `sonnet`) say it. A preset that says nothing keeps
ADR 0114 as written.

The switch `spoken_streaming.structured` still decides first: off, no preset gets the schema.
The choice is the preset the turn runs on, read once when its run opens.

## Alternatives rejected

- **Keep the schema for Claude and tighten the note or the field descriptions** — the exam
  ran with the preset's system note already telling Haiku to answer a lookup with the call
  alone, and still lost 10 cases of 80 to the schema; the reply being the whole answer is
  the schema's shape, not its wording.
- **Turn `structured` off for everyone** — luna measured 20/20 valid with the screen/voice
  split ADR 0114 wants; the loss is on Claude only.
- **Choose by provider** — a later Claude preset may do better with the schema, and a
  per-preset field lets the exam say so without a code change.

## Consequences

- The talk area shows a `<document>` only when the model uses the tags (19 of 129 Haiku
  answers did); otherwise the screen shows what she says and nothing apart. `written_apart`
  is never set for these presets.
- A list written as plain text (9 of 110) is spoken as written; nothing on this route cuts
  it.
- A line before a call is spoken with the answer, not held for the acknowledge as a lead-in.
- Two shapes of spoken reply now exist at once, so a measurement is only valid for the shape
  its request was built with: the tool exam rebuilds each logged request for its preset's
  route before sending it.
- The exam scores the first step of a request. How Claude's `<voice>`/`<document>` or plain
  replies sound and show in live turns is not measured by it.
