# ADR 0312 — The doing writer follows one standard, cuts at Mac quarter hours, and reads background about him

**Status:** Accepted
**Date:** 2026-10-20
**Supersedes:** none

## Context

- The doing lane's writer (ADR 0302, ADR 0311) wrote 10-19 22:09–00:00 two ways on the same
  input: once one span, "building the watch-together page and watching the film with a friend",
  once a film span and a work span with a question. Earlier drafts of the same evening came out
  as 2 spans and as 7.
- The prompt left five things to the model's judgement with no measure: when to join or split,
  how fine to cut ("most last from half an hour to a few hours"), what counts as one thing, what
  level a title names, and when it is unsure ("high when several things agree"). Where the
  evidence was clear the answers agreed; where it was mixed, either reading was defensible and the
  model picked one.
- The evidence that evening was mixed: in 23:00–23:15 the film page held 4 minutes of the
  foreground, WeChat, the screening-room page and Claude about 2 each.
- A span may start or end only at a program-given time. Inside a long Mac block the only times
  were clue moments, so a change of what held the Mac between clues could not be cut where it
  happened.
- He judged the cause a missing common standard, not the model (2026-10-20 18:18Z), asked that
  what stays unsure be asked once in the evening review, and said to build it (18:54Z).
- He agreed (18:16Z) that the writer be given background about him: his projects, his routine,
  the people around him, and what he corrected; evidence outranks memory. One correction is that
  day only: a thing that happened once is not a habit (18:16Z).
- These already exist as stores: core memory (ADR 0258) holds his ongoing things and the people
  around him; routines (ADR 0279) count a weekday habit only after at least 3 distinct weeks; his
  answers about past spans are kept by time (ADR 0302).

## Decision

Give the doing writer one written standard, let it cut at every quarter-hour edge at his Mac with
each quarter's lead computed by the program, and show it background about him computed from the
existing stores, never summarised by a model.

- **The standard** (in `doing.prompt`, as general rules): a span is one purpose; same purpose
  must join and different purposes of at least 30 minutes each must split; a span lasts at
  least 30 minutes except travel and workouts the facts show; a title is an action and an object
  named at the level that holds all day; confidence is high for two or more independent sources,
  medium for one, low for inference alone; a span mostly of quarters mixed between purposes, or
  with two equally supported readings, is low and asks, and is never settled by a guess.
- **Quarter hours.** Each quarter in MAC FOREGROUND carries its lead: the one app, site or
  document that held over half of the minutes he was in front, else mixed. Every quarter edge
  inside the waking window is a time a span may start or end at.
- **About him.** A background block: core memory's ongoing things; the routines counted for
  that weekday; the people of core memory named in the day's clues or facts; and his answers about
  the 30 days before, each as one dated instance with what it corrected. It is not part of the
  inputs digest, so a change in it rewrites no day by itself.
- Gates and his answers' precedence (ADR 0302) stay as they are; no new gate checks the standard.

## Alternatives rejected

- **Cut only where a quarter's lead changes** — two neighbouring mixed quarters have no lead to
  change, so the 10-19 23:00 stretch, mixed then mixed with another purpose, would get no cut.
- **A gate that rejects a span over mixed quarters unless it is low** — a stretch of coding moves
  between editor, terminal and browser and is mixed by app while being one purpose; the gate
  would reject correct answers.
- **Wait for his evening answer only** — the same input still gave two different days, so what
  he is asked would itself change between runs.
- **Let a model write a profile of him for the writer** — a model generalises one instance into a
  habit, the very error he named; counts belong to code.

## Consequences

- TIMES grows by up to 4 labels per hour at the Mac, about 1–2K input tokens on a long day,
  plus the background block; a whole-day Opus run stays near $0.14–0.16.
- Whether a quarter's apps serve one purpose or several is still the model's judgement; the
  standard names the test but cannot compute it until projects are sorted by activity.
- The background is only as good as core memory and the answers; while his routine has under 3
  weeks counted, that part is empty.
