# ADR 0394 — The writer reads every word he and she said that day, whole

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** none

## Context

- The writer of the doing lane (ADR 0302) reads what he and she said that day as clues. Every
  clue was cut to 140 characters, and his and her words were thinned to 80 lines a day, every
  n-th line kept.
- What he says he did outweighs what a fact suggests (ADR 0302). Thinning drops lines by count,
  not by meaning: a line that says what he was doing is as likely to go as any other.
- His rule, 2026-10-21 06:19Z: when a model judges wrong, change what it is given. 06:33Z he
  asked why the day's talk with her is not simply given to the writer when it tidies the day; at
  06:36Z he agreed to give it in full.
- Read-only count on the Pi, 2026-10-13 to 10-20: the busiest day (10-14) held 121 lines, 2,025
  characters of his and 9,694 of hers, 43 lines over the cut; 10-19 and 10-20 held 25 lines and
  about 2,500 characters each. The writer runs on Opus at $4 per million input tokens
  (data/pricing.json), only at night and when he asks (ADR 0311).
- The after-turn pass (ADR 0300) also extracts what he says he did, for this lane. Nothing reads
  it: the writer reads the words themselves.

## Decision

Give the writer every line he and she said in the window, whole, each on one line, in the order
said. Other clues stay cut and thinned as they were.

## Alternatives rejected

- **Keep 80 lines of 140 characters** — on 10-14 every second line was kept, so 60 of 121 were
  not shown, chosen by position, not by what they say, and 43 were over the cut.
- **His lines whole, hers cut** — his short lines often answer her question, which is what makes
  them readable; her lines were 9,694 of the 11,719 characters on 10-14.
- **The after-turn pass's extract instead of the words** — a second model's reading of the same
  words, made one turn at a time; the writer reads the day at once.

## Consequences

- The writer's input grows with the day's talk: on a day like 10-14 by at most its 11,719
  characters, about $0.04 per run at most, and by little on a quiet day like 10-19.
- A day of very long talk has no cap; the run's input grows with it.
- A cited word clue is stored whole in the day's run.
