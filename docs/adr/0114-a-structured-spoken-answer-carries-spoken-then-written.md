# ADR 0114 — A structured spoken answer carries spoken, then written

**Status:** Accepted
**Date:** 2026-10-02
**Supersedes:** none

## Context

- Allen wants, in a voice turn: a fast first word, a short spoken answer, the
  full written answer on screen, and the words on screen being the words
  heard.
- The old path writes the answer whole and then makes the spoken form in a
  second request (ADR 0040, 0043, 0045, 0099): 1.3 to 1.9 s more, and the
  screen text is not the spoken text.
- Speak-as-written (`docs/plans/speak-as-written-proposal.md`,
  `spoken_streaming`) removes that request and says each sentence as the model
  wrote it, asking for `<voice>`/`<document>` tags in the prompt. The A/B of
  2026-10-01 (`/mnt/project-files/ab-2026-10-01.md`): a 471-character answer
  was read for about 39 s and Allen cut in at 20.7 s; in a test the weak
  model left out the tags in 6 of 8 answers. Cutting its text by code was
  rejected.
- Probe, 2026-10-02 (gpt-6-luna, Responses API, streaming, 20 real turns,
  $0.0055) of a strict JSON schema `{"spoken": string, "written": string}`:
  20/20 valid, `spoken` always streamed first, the first spoken character at a
  median 0.75 s against 0.66 s for plain text, no first-use schema penalty;
  the language right 20/20, no markdown in `spoken`, `written` empty in chat
  and filled in list and report turns, a story entirely in `spoken`. A schema
  with tools in one request is accepted: a turn that needs a tool emits a
  clean call with no text, and answers in the schema after the result.
  Weak spots: `spoken` did not always point to the screen when `written` had
  details, and once `written` invented example commands.
- The companion's talk area shows a reply's `<document>` alone (ADR 0040: the
  document is the whole answer, the voice only its spoken form), and its brief
  caption level hides a spoken-only answer.

## Decision

With `realtime.response.spoken_streaming.structured` on (off by default, and
only effective with `spoken_streaming`), every request of a spoken-route turn
carries a strict JSON schema whose `spoken` field is said as it streams and
whose `written` field is for the screen only; and the talk area shows the
spoken line with the written part under it, at the brief and all caption
levels.

Its limits:

- The plan text stays the envelope of ADR 0040: `<voice>spoken</voice>
  <document>written</document>`, or the spoken text alone when `written` is
  empty. L5 speech, the history and memory work from it unchanged; only voice
  spans are spoken, never `written`.
- Spoken text goes through the existing assembler, gate and permits; only its
  source changes. A stream cut anywhere (barge-in, cancel) keeps the spoken
  text already unescaped and drops an unfinished `written`. Text that is not
  the schema's JSON is read as it is without the switch.
- ADR 0099's rule moves into the `spoken` field's description: two short
  sentences, unless the user explicitly asks to hear more. The prompt note
  asks for no tags.
- The daemon marks an answer whose document only adds to the voice with
  `written_apart` on its `surface.response_emitted` audit event, and the
  Inherent `done` message carries the document as `written` then. The
  companion shows both parts only for an answer that carries it; any other
  `<document>` keeps ADR 0040's meaning. Memory records both parts of such an
  answer.
- The written part writes itself in as before and is not lit by her voice
  (ADR 0112 lights the spoken line only).

## Alternatives rejected

- **Keep tags, tighten the prompt** — the weak model ignored them in 6 of 8
  answers, and the length stayed in the model's hands.
- **Cut or summarize the spoken text by code** — rejected by Allen.
- **Infer the structured case in the companion from the text** — a `<document>`
  that is the whole answer and one that adds to the voice read the same; any
  heuristic (the written part's length, a list) misfiles one of them, as the
  middle caption level's `worthReading` already shows for the old path.
- **Keep the second spoken-form request** — 1.3 to 1.9 s per long turn, and
  the screen text not the spoken text.

## Consequences

- A turn that needs a tool gains nothing before the call: the schema leaves
  the model no text to say first, so the tool-turn lead-in of the spoken note
  is gone on this path.
- A length risk remains in a field description: the weak model may still
  write a long `spoken`; there is no cut by code.
- `written` may invent, since nothing but its description stops it. Watch it
  in live turns before the switch goes on by default.
- The written part arrives with the `done` message, after the spoken stream,
  so it shows once the answer is complete, not while she speaks it.
- `docs/adr/0040`'s "the document is the whole answer" holds only without
  this switch; it is not superseded.
