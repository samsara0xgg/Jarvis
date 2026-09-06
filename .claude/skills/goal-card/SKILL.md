---
name: goal-card
description: Write, launch, or verify against a goal card in docs/goals/ for an autonomous /goal implementation run. Use when a settled design must be handed to a fresh implementation session, when starting a /goal run from a card, or when checking a finished run against its card.
---

# Goal card

One file per run: `docs/goals/<slug>.md`, slug in lowercase kebab-case.
The card is the only thing the implementation session and the verifier
receive from the design session. Exploration is compressed into it, not
discarded.

## Handoff rule

The card is ready only when "Open questions" is empty. The design session
does not edit canonical docs; it records design intent in the card. The
implementation session updates canonical docs after the facts land in code.

The card names contracts, boundaries, and acceptance evidence. It does not
contain an ordered step plan. Files listed under "Affected contracts and
files" are a starting hint; the implementation session decides which files
change and in what order, and records that under Progress as it goes.

## Template

    # Goal: <slug>

    ## Goal
    <one sentence: what becomes true>

    ## Why
    <only if non-obvious>

    ## Current behavior
    - <observed fact> (<path:symbol>)

    ## Target behavior
    - <fact that must hold after the change>

    ## Affected contracts and files
    - <layer> <path:symbol> — <what changes>

    ## Boundaries and non-goals
    - Layers that may change: <list>
    - Must not change: <behavior, file, or contract>
    - Non-goals: <list>

    ## Rejected approaches
    - <approach> — <why not>

    ## Acceptance evidence
    - Positive: `<command>` prints or exits <expected>
    - Regression: `<command>` still prints or exits <expected>
    - Live run: <required | not required> — <which real run, which canary value>

    ## Docs to sync
    - <docs/spec.html §x | docs/adr/NNNN | none> — <fact that changes>

    ## Open questions
    (must be empty before handoff)

    ## /goal condition
    <paste-ready, under 4000 characters, see below>

    ## Progress
    - <slice> — <commit sha> — <evidence line>

## Writing the /goal condition

The condition is judged by a small model that reads only the transcript. It
cannot run commands or open files. So:

- Phrase it as evidence that must appear in the transcript: "the raw output of
  `<command>` is shown and ends with `N/N pass`", "`git status` shows a clean
  tree", "each entry under Docs to sync was updated or explicitly judged
  unchanged".
- Include the regression check and, when required, the live-run canary value.
- Include the documentation rule: when the implementation changes a documented
  contract, invariant, ownership boundary, or externally relevant behavior,
  update the canonical document that owns that fact; do not document what the
  code already makes clear; do not duplicate a fact across documents.
- End with a bound: "or stop after N turns".

## Launching the implementation session

Stay in the design session if its context is still clean. Start a fresh
session when the design transcript is polluted with rejected approaches or
stale instructions. Either way:

1. First message: "Implement docs/goals/<slug>.md. Read it fully before
   touching code."
2. Then `/goal <condition from the card>`.

During the run:

- Work in verifiable vertical slices. After each slice, commit with the
  commit skill and append a line to Progress.
- Intermediate gates may be delegated to subagents that report failures
  only. The final acceptance and regression commands are run by the main
  session, so the judge sees the raw output.
- If the card contradicts the repository, stop and report; do not redesign.

## Verification

After the goal is met, hand the card, the diff range, and any narrow-check
findings to the `verifier` agent in a fresh context.
