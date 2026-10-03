# ADR 0146 — Core Memory Is a Versioned Note the Night Consolidates

**Status:** Accepted
**Date:** 2026-10-03
**Supersedes:** none

## Context

- The user's lasting facts live in `profile`: a name line, a few seed lines and one
  line per `remember` topic. Only the model's own `remember` calls and the ask card
  write it, so a fact the user states and the model does not file never reaches the
  next conversation, and a fact that stops being true stays until someone rewrites
  its topic (ADR 0066 rewrites rows in place, so the old text is gone).
- The prompt loads the whole block every turn: it has to stay short, and a wrong or
  stale line costs every later turn.
- A model's reading of a day is not evidence (ADR 0142): `records` is the transcript.
  A change to a note that every turn trusts needs a citation a person can check.

## Decision

Keep the user's lasting facts as core memory: one append-only table of versions of a
document with six fixed sections, whose latest row is current and replaces `profile` in
every prompt. `remember` and first-run setup append a version at once. Each night, in
the day-summary job, a model reads each unconsolidated past day with the current note
and proposes typed changes (add, rewrite, stale), each citing that day's records by short labels (r1, r2, ...) that are mapped to
record ids before storing;
the whole list lands as one version only if every gate passes, and is otherwise dropped
and asked once more. A day that fails both attempts stops the chain and is retried on
the next run. Older versions are kept for review and undo; `profile` is migrated once
into the first version and never deleted or read again.

## Alternatives rejected

- **Let the model rewrite the whole note each night** — the note is trusted every
  turn, so one malformed or overreaching rewrite damages it with no check; typed
  changes with cited sources can be gated item by item and shown as a diff.
- **Extend `profile` with the same rows and a nightly writer** — `profile` rows are
  rewritten in place (ADR 0066), so there is no version to compare, review or undo.
- **File facts only through `remember`** — that is `profile` today: it relies on the
  model choosing, mid-turn, to call the tool, so a preference stated in passing is
  lost; a nightly pass over the whole day does not depend on that choice.

## Consequences

- Core memory is derived from `records` and `remember` calls and can be wrong: nothing
  may read it as evidence where a record can be read.
- A day without a day summary is skipped by the nightly pass; if its summary is
  written after a later day was consolidated, that day is never consolidated.
- A nightly day costs one paid call, two when rejected; a failing day blocks every
  later day until it passes.
- The prompt block now carries section headings and more than facts about the user;
  it keeps the label `[About the user]`.

> **Amendment (2026-10-03) — Jev reviews each add and rewrite, log-only by default.**
> Before a version is written, each `add` and `rewrite` goes to Jev (ADR 0122) as one
> question: keep, garbled, one_off or not_users. Its verdict and confidence are stored
> in the version's `changes` (`reviewed`) and every call is in the Jev dataset; `stale`
> is not reviewed, and a failed call only logs a warning and lands the day unreviewed.
> Dropping is off by default (`review_drop`): on 33 real nightly items, a 0.9 bar dropped
> all 7 bad ones but also 23 of 26 good ones, and Jev cannot see a mishearing. With
> `review_drop` on, only `keep` at `review_min_confidence` lands, each other change is
> dropped and logged, and a Jev failure rejects the day like a gate.
