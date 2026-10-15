# ADR 0267 — History carries a program-written line of what each answer did

**Status:** Accepted
**Date:** 2026-10-14
**Supersedes:** the clause of ADR 0044 that makes the history only the text of each row. The rest of ADR 0044 stands.

## Context

- Live, 2026-10-14, she told Allen that an earlier turn had "not really saved" or "not really called" something, about turns
  that did call the tool. The history is built from memory.db `records`, words only (ADR 0044), so no trace of an earlier call
  reaches the model.
- The prompt says to claim something saved, set, sent or looked up only after the tool succeeded in this turn, otherwise to
  say plainly that she has not. With nothing in the history to show an earlier call, that rule makes her confess falsely,
  or run the write again.
- ADR 0044 kept calls and results out because the model copied a `[ts] source:` prefix into its answers. A line that
  names no time or speaker, and that the prompt already teaches her to read (`[You reacted 👍]`, ADR 0257), is not that prefix.
- The steps of a turn are already read from the Event Log for the phone: the fold in `jarvis/shared/steps.py`, one
  past-tense phrase per kind of step (ADR 0231). ADR 0231 rejected writing steps into `records`: a restart in the middle of
  a tool would leave no record, and the table could never be corrected for old turns.
- ADR 0263 rejected earlier turns' tool results as backing for the guard: the loop does not carry them as structured data.
- The provider's prefix cache needs the history above the newest row to stay byte-identical between turns.

## Decision

When the history is rendered, an answer whose turn ran at least one tool that succeeded is followed, in the same
`assistant` message, by a line of its own: `[You did: <phrases>]`. Its limits:

- **Read at render time from the Event Log, never stored.** One batched read over the answers shown, by the answer's
  `surface.response_emitted` uid, which is the record id. A log that cannot be read leaves the history without the lines.
- **Steps that succeeded only, in English whatever the language setting.** The phrases are the `step.<kind>.done` ones
  ADR 0231 shows under the answer, distinct and in order; the reader is the model, and the language table's English wording
  is the fixed one. A failed or unfinished step is not listed; a turn with none left gets no line.
- **Fixed once the turn ends**, so a later render of the same history is byte-identical.
- **Not her words and not backing.** The prompt says so and tells her never to write it; a copy in her answer is cut from the
  history, the conversation page and a record read by id. The guard (ADR 0240, 0263) still judges only this turn's calls.

## Alternatives rejected

- **Store the tool calls and results in `records` again (ADR 0027).** The calls cannot be re-worded for old turns, and the
  results are the large and private part; the phrases alone answer "did she call it", the only question the false
  confession asks.
- **Write the line into `records` when the turn ends (ADR 0231).** A restart in the middle of a turn loses it, and a changed
  phrase table would not reach old rows.
- **Put the phrases inside her answer text.** She then copies them as her own words; a separate line the prompt names as the
  program's is the shape she already leaves alone (`[You reacted 👍]`).
- **Let the guard count the line as backing (ADR 0263).** A line is a program's summary of kinds of steps, not which note was
  saved or which stop was set; a claim about one thing would pass on the strength of another.
- **Render the phrases in the language setting.** The history would change bytes when the setting does, and the same turn
  would read differently to the model across a change of language.

## Consequences

- The guard is turn-local, so an answer that says "I saved it earlier" can still be sent back once; the note it sends
  already lets her say it was done in an earlier turn.
- The line is as coarse as the phrases: it says a note was saved, not which. The note's content is still found with the
  conversation or memory tools.
- A line is rendered only for what the Event Log still holds; a turn that has left the log has none.
- A `[You did: ...]` she copies into her answer is cut from what is read back, not from what is spoken or streamed at that moment.
- One more Event Log read per history render, in the turn and in the prefix warm-up.
