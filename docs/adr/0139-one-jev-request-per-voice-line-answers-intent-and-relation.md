# ADR 0139 — One Jev request per voice line answers intent and relation

**Status:** Accepted
**Date:** 2026-10-03
**Supersedes:** none

## Context

- Today a voice line can cost Jev up to two calls: ADR 0130's control-word question at the
  surface (short lines over her voice or in hands-free mode, she is held while it answers) and
  ADR 0122's instant-function question in the decision stage (every turn Tier 0 did not answer).
  Neither knows the line before it.
- Offline, 2026-10-03, `~/.jarvis/experiments/jev-oneshot-2026-10-03`, the intent question (the
  four control words, the seven instant functions, repeat, none: 13 options) asked inside one
  request with a relation question (new, supplement, correction, unrelated) and a think question:
  778 scored lines, 738/778 correct at 0.95 against 738/778 asked alone (3 wrong between
  actions in both, 4 acted on a none line against 5); same latency; 31% cheaper than three
  separate calls.
- Relation, 472 pairs (the labels are Claude's): at 0.98, 29 of the 30 lines it acted on were
  right (supplement or correction), 1 was a stray fragment taken as a supplement; at 0.95, 41 of
  45. Corrections are the weak spot: 11 real ones, 0 caught at 0.98 on the real set (15 of 16
  hand-written ones). The think question caught 0 of 17 think lines at 0.9: unusable.
- Today's ADR 0053 and 0074 sweep already drops the unspoken answer of every other voice turn
  of the last 10 s when a voice line is accepted, and ADR 0044 folds an unanswered user row into
  the next prompt, so a supplement already behaves as one request when the line before it has
  not been heard. What the sweep does not reach: a turn older than 10 s, and an answer that
  already began playing.
- Allen's bar: a wrong action is worse than none, so every bar stays where its question had it.

## Decision

With `realtime.jev_oneshot.enabled` and `realtime.surrogate_route.enabled`, the surface sends
one Jev request for each voice line the word lists call a turn, and every reader takes its
answer from that request: the control-word check of ADR 0130 (same gates and bar), the instant
function of ADR 0122 (same bar, the decision stage reads the request already in flight), and a
relation to the line before it (`window_s` 30 s). A `supplement` or `correction` at
`relation.at` (0.98) cancels the open, unspoken answer of that one earlier turn as
`superseded` and withdraws its card; a `correction` also stops that answer if it is already
audible. A turn that dispatched an action is left alone. Anything else, and every failure, is
today's behaviour.

- **Typed text and PTT keep the separate question.** They never pass the surface.
- **Switches.** `realtime.jev_oneshot.relation.enabled` gates the act (the question is always
  asked while the block is on, so the dataset holds it); both default off.
- **Dataset.** The request is one `oneshot` call line in ADR 0128's file; each act is a
  `relation` line with the target turn and what came of it.

## Alternatives rejected

- **Keep separate calls.** The merged request costs 31% less than three calls and measured the
  same intent accuracy; two calls per line also doubles the lines Jev sees.
- **Ask the think question too.** 0 of 17 think lines at 0.9 and 17 misses at every bar.
- **Let `new` or `unrelated` skip the 10 s sweep.** It would let two answers play where one
  was dropped; the relation numbers have 20 false actions at 0.9 and a hand-labelled set, too
  thin to take an answer away from Allen. Not built; the dataset records the relation of every
  line so it can be judged later.
- **Act at 0.95.** 4 of 45 acts wrong against 1 of 30 at 0.98.

## Consequences

- Every voice line leaves the Mac for OpenRouter (zero data retention), Tier 0 hits included,
  where today only the lines Tier 0 missed did. One request per line is dearer than today's
  single call, cheaper than the three calls the experiment compared.
- The assistant line in the state is what she said in the last minute (the surface's
  `recent_speech`), not the last two exchanges ADR 0122 sent; the experiment used the last
  answer at any age.
- A correction that arrives while her earlier answer is audible usually met barge-in first;
  the act matters when her voice carries on without the mic hearing him.
- `OPTIONS_VERSION` of ADR 0122 does not move: the merged question is its own dataset use.
