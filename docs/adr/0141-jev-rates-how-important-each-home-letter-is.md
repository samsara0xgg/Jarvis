# ADR 0141 — Jev rates how important each home letter is

**Status:** Accepted
**Date:** 2026-10-03
**Supersedes:** none

## Context

- The home lists unread letters newest first, so an interview invitation can sit below a
  newsletter. ADR 0123 and 0124 marks (reply, junk) are thresholds, and a wrong mark costs more
  than a missing one.
- Offline, 2026-10-03, `~/.jarvis/experiments/jev-mail-sort-2026-10-03`, 43 letters (18 real, 25
  invented; the labels are Claude's, not Allen's): importance as a four-option choice (urgent,
  important, normal, low) ranked by expected score `3*P(urgent) + 2*P(important) + P(normal)`
  put 8 of 9 urgent letters above every normal or low one (pairwise 0.96). As a threshold it
  fails: the one real interview letter has P(urgent) 0.84, below 0.9 and 0.95.
- Category (8 options) was right on 41 of 43; the same request answers both questions with the
  reply and junk questions at no extra round trip (86 requests, $0.0025, median 140 ms).

## Decision

With `home.mail_reply.importance.enabled`, add an importance question and a category question to
the existing per-letter Jev request (sender name and subject only, ZDR, once per letter), and
return the letter's expected score as `importance` (0 to 3) and its `category` in
`GET /inherent/mail`, so the home can show the highest score first. Category is recorded, not
shown; reply and junk questions, bars and marks do not change.

- **No threshold.** The score only orders letters; nothing is marked, hidden or archived by it.
- **Unrated letters carry neither field**, so the home keeps today's order for them.
- **Dataset.** ADR 0128's raw reply holds every probability; the `decision` line adds the score,
  the four probabilities and the category.
- **Off by default.** Off, the request, the payload and the log are today's.

## Alternatives rejected

- **Importance as a yes/no mark at a bar.** 0 of 1 real urgent letters caught at 0.9 and at
  0.95 (P 0.84).
- **A separate reply-helps question.** No better than today's reply question on the interview
  letters (0.82 to 0.90 against 0.72 to 0.91) and weaker on real people (0.58 against 0.93).

## Consequences

- Two more questions per letter raise the request's input and output tokens; the cost per
  letter is still of the order of $0.00003.
- The score is Jev's reading of the sender and subject alone, validated on a small sample with
  Claude's labels: a good letter with a dull subject sinks, a loud promotion can rise.
- A letter rated on an earlier poll keeps its score until the daemon restarts, like its marks.
