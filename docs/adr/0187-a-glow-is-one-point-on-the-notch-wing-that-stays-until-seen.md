# ADR 0187 — A glow is one point on the notch wing that stays until seen

**Status:** Accepted
**Date:** 2026-10-07
**Supersedes:** none

## Context

- ADR 0160 named five levels a proactive card can be rated at: ledger, glow
  (亮一下), card, card with sound, speak. Delivery only ever had four: the
  job-mail path turns any level outside card, card_sound and speak into
  `ledger_only`, so a rating of 亮一下 pointed at something that did not exist.
- Allen, 2026-10-07, in the UI thread, picked the 北极星 notch proposal's rule
  for the wing right of the camera: things that reached him light a point there
  that jumps once and then rests, ordered by urgency, and there is no wing when
  nothing is there. Today only Claude and Codex sessions draw there.
- The wing has room for four groups in the 64 pt 点线环 look; with the
  Dashboard open about 87.5 pt remain beside the notch, and a fifth group
  (about 96 pt) would fold the wing away.
- The quiet levels (ADR 0153) already split cards from marks: at `no-pop` the
  wing keeps updating while cards are held, and only `dnd` freezes it. The
  moment hold (ADR 0161, ADR 0163) holds cards during a call or while Allen is
  away.
- Job-mail alerts already reach the desktop as rows of
  `GET /inherent/notices`, each carrying its level, and are served until
  `POST /inherent/notices/{id}` marks them seen or dismissed.
- Allen's job-mail rule table is his. Asked on 2026-10-07 whether a rejection
  should glow instead of raising a card, he kept the card, so no event is
  judged `glow` yet; the first producer is expected to be the reminders work.

## Decision

Deliver `glow` as one more amber point in the wing's turn group (轮到你) and
nothing else: no card, no cue, no speech; it stays until Allen opens or clears
it from the wing's list, and it is a mark, not a card, so it shows at `quiet`,
`no-pop` and through the moment hold, waits only at `dnd`, and is never folded
into a digest.

## Alternatives rejected

- **A fifth wing group for glows** — five groups need about 96 pt, more than
  the 87.5 pt left beside the notch with the Dashboard open, so the wing would
  fold every time the Dashboard opens.
- **A glow as a short-lived flash with no state** — a point that is gone when
  he looks up is the same as `ledger` for anyone away from the screen for more
  than the flash; the 2026-10-07 rule says it stays until seen.
- **Hold glows like cards at `no-pop` and during a call** — the wing already
  keeps updating at `no-pop` (ADR 0153), so a held glow would be the one mark
  that stops, and a silent point interrupts nothing during a call.
- **Push glows over the Inherent WebSocket** — the client already polls
  `GET /inherent/notices` every 5 s with the level on each row; a second path
  would need its own seen state and replay after a restart.

## Consequences

- Until a judge or producer picks `glow`, the path runs only in tests and
  the desktop checks, so a regression in it shows up nowhere Allen looks.
- A glow Allen never looks at is only in the wing's list, the job ledger and
  the Dashboard's For you list; nothing repeats it.
- Glows ride the job-mail notices route, so a daemon with `job_mail` off has
  no glow path; a non-mail producer (a reminder, a brief) has to write a
  `job_alert` row or move the route out of job mail first.
- The turn group now mixes sessions and notices, so its panel section holds
  two kinds of rows, and the glow rows are mouse-only in the wing's list.
