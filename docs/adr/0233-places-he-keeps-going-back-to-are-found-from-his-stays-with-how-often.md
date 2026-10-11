# ADR 0231 — Places he keeps going back to are found from his stays, with how often

**Status:** Accepted
**Date:** 2026-10-11
**Supersedes:** none

## Context

ADR 0228 names a place only when he names it. On 2026-10-11 he added that the
places he goes to often belong in her long-term memory on their own, and that
how often he goes is itself worth knowing. Going weekly makes a place a regular
one. Monthly does not, but she should still know the rate.

- His `stay` items (phone visits) already hold every place and time he stayed.
  Counting them adds no new data.
- An iOS visit's centre wanders by tens of metres between visits to the same
  building, so one place is a cluster of nearby stays, not one point.
- ADR 0228 forbids a model or geocoder name posing as his word. The phone's
  string may be shown as what it is, but it is not a name he gave.
- Her prompt has room for a few lines on places, not for every café.
- A cluster recomputed on each read moves as stays are added. A place she asks
  about, and that he then names, needs an identity that does not move.

## Decision

The brain counts his stays per place: stays within 100 m of each other are one
spot, with the days visited and the last visit. A spot visited on at least 4
distinct days in the last 28 becomes a regular place, recorded once as
`place.found` with a fixed centre and id. From then on it is a place like a
named one, without a name until he gives it one.

She asks his name for a regular place once, in conversation, while he is
there. His answer names that same place (ADR 0228). Until then the ring and the
ledger show the phone's string for it, marked as unnamed. Visit rates are
derived for every spot, regular or not, and recall can answer them. The ledger
lists regular places with their rate.

Limits:
- No model picks names or regularity. The threshold is a constant.
- A regular place he forgets (`place.forgotten`) is not found again until it
  meets the threshold afresh, counting only stays after it was forgotten.
- She asks once per place. A question he leaves unanswered is not repeated.

## Alternatives rejected

- **Recompute clusters on every read, store nothing** — a cluster's centre
  drifts as stays are added. The place he named last month could split or
  shift, and its name would stop matching. This is reproducible by
  re-clustering any month of visits with one more week added.
- **Name regular places from the geocoder** — the string for one building
  changes between visits, and a home reads as a street address (ADR 0228).
- **Ask on a push notification as soon as a place qualifies** — he is often
  not there at that moment, and it is a question with no context. In
  conversation, while he is there, the answer is obvious to him.
- **Monthly threshold (2 days in 28)** — he said monthly is not "often". It
  would also fill the ledger with restaurants.

## Consequences

- A new spot takes up to four visits to become regular, so the ring shows the
  phone's string for it at first.
- Her conversation rules gain one standing question, a prompt change.
- A clustering bug misplaces visits for every reader at once, because they all
  read this one source.
