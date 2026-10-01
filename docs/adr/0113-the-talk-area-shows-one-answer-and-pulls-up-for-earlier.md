# ADR 0113 — The talk area shows one answer and pulls up for earlier

**Status:** Accepted
**Date:** 2026-10-01
**Supersedes:** none

## Context

- Allen, 2026-10-01: the talk area accumulated every line of the session
  into a long log she could scroll, and kept it for ten minutes after it
  folded, so a poke minutes later reopened on an old answer. He wants the
  area to hold what is being talked about now.
- The Dashboard Conversation page is the full record; the area is not
  the place to keep history.
- ADR 0102 keeps her listening after an answer (about 10 s of quiet, 60 s
  held), so a follow-up question arrives while the area is still up and
  belongs to the same session.
- The area is a 360 px scroll surface on a trackpad Mac. After the fingers
  lift, macOS keeps sending wheel events (inertia) for a second or more, and
  a wheel event does not say whether a finger is down.

## Decision

Each time the talk area opens it starts empty, and it shows only the latest
exchange of the session (what you said and her answers to it); an earlier
exchange of that session comes up above it when the reader, already at the
very top, keeps pulling up.

Its limits:

- A session is what happened since the area last opened. A follow-up while
  she still listens never closes the area, so it stays in the session.
- When the next question comes in, the answer on screen fades and slides up
  away; the earlier ones are put away again.
- The pull follows the fingers with growing resistance and a faint
  "Earlier" shows how far. Past the threshold one older exchange is loaded
  once and the content springs back; the next one needs a fresh pull at the
  top. With nothing older, no stretch and no hint.
- A pull counts only if its gesture began at the top (events less than
  `GESTURE_GAP` apart are one gesture), loads at the moment it crosses the
  threshold, and takes nothing after that from the same gesture or from
  inertia once the step sizes fall away. Reduced motion has a plain
  "Earlier" button instead of the stretch.
- The caption levels apply to what is displayed; an earlier exchange with
  nothing to show at the current level is not offered. The daemon sends
  nothing new.

## Alternatives rejected

- **Keep the log, only scroll to the newest** — it is what Allen turned
  down: two answers' written blocks stay on screen while she says a third.
- **Clear on a timer, not on open** — the ten-minute memory already showed
  an answer from minutes ago on every poke; a session is defined by the
  area opening, which needs no clock.
- **Load on release of the fingers** — a wheel event does not carry the
  lift; deciding at release means guessing it from silence, and the inertia
  that follows would be read as a second pull. Loading at the crossing and
  locking the rest of the gesture needs no guess.
- **A scroll anchor or button always visible** — adds permanent chrome for
  something used rarely; the hint appears only while pulling.

## Consequences

- The rubber band cannot be tuned without a trackpad. The damping, limit,
  threshold, gesture gap and inertia rule are constants at the top of
  `desktop/resonance/src/talk.ts` and were chosen without one.
- A mouse wheel, whose notches are further apart than `GESTURE_GAP`, never
  pulls; only the reduced-motion button reaches earlier exchanges with one.
- An earlier answer is gone from the area the moment the area folds; the
  Dashboard is the only place to read it afterwards.
