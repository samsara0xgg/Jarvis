# ADR 0163 — The moment's hold reaches the cards the client makes itself

**Status:** Accepted
**Date:** 2026-10-04
**Supersedes:** none

## Context

- Allen, 2026-10-04, via the coordinator: nothing should pop and no sound
  should play while he is in a call or meeting, or idle, locked or away, for
  any card, not only job mail.
- ADR 0161 holds job alerts by the moment's facts (read from TimeSink when an
  alert is served), and says so itself: the client makes the agent pop, the
  needs-you card, the digest, the night glance and the morning card without
  asking the daemon, so a pop can still appear during a call (ADR 0160, last
  consequences). That gap is what this closes.
- The client already polls `GET /inherent/notices` every 5 s, but only while
  job mail is on; with it off the route is a 404 and the poll stops. `moment`
  is configured on its own (`moment.enabled`), so a daemon can know the hold
  with job mail off.
- ADR 0153 owns the quiet level and its `held` list: at `no-pop` the client
  queues nothing and brings what waited back as one digest, or the one card.
  ADR 0161 owns what holds and what is unknown.

## Decision

Tell the client the hold, and let it hold every card it makes itself the way
it holds them at `no-pop`.

Its limits:

- **The field.** `hold` is `call`, `away` or null. It comes from
  `decision.moment.client_hold`, which folds `hold_reason` (the one rule that
  holds job alerts) to two words: a call, or any of idle, locked and asleep.
  Unknown, stale or unreadable facts, and a moment that is off, are null. It is
  read when the request arrives (the moment's 5 s cache), never by a new loop.
- **The route.** `GET /inherent/notices` carries `hold` in every response.
  `GET /inherent/moment` answers `{hold}` for a daemon whose job mail is off,
  with the same local key; it is a 404 while `moment` is off. The client asks
  the first, and falls back to the second on a 404.
- **The client.** With `hold` set, an arriving finish, ask or needs-you card
  goes to `held` with no card and no cue, a card on the island goes back to
  `held` too, and the cue of any card is silent. When it ends they come up as
  `held` does at `no-pop`: one alone as itself, several as the digest. Nothing
  is dropped and no request is answered or expires: a held ask waits in the
  list and is answered from the digest or the card as before. The night glance
  and the morning card stay down until it ends; both are drawn from the
  daemon's night state, which keeps them.
- **Never in his way.** A card he brings up himself from the notch's list or
  the Dashboard shows through a hold and keeps its cue silent; the bedtime card
  is his own start of a run and is never held; the confirmation and ask cards
  (ADR 0160) arise from something he asked for and are never held.
- **Letting go.** A missing field (an older daemon), a 404 on both routes and
  an unreachable daemon hold nothing, the last one after three missed polls
  rather than one. A `call` the client has been told of for over 3 hours counts
  as unknown, so a stuck window cannot hide a card for a day (`away` is already
  bounded by ADR 0161's eight hours). Until the first answer after launch
  nothing is known and nothing is held.
- **Snapshots.** A card shown after waiting carries `held_by` (`call`, `away`
  or `quiet`) and `held_s`, whole seconds from when the client took it in, in
  the `situation` of its ADR 0160 snapshot; a digest carries its oldest item's.
- **Egress.** None; the field is a word on the local loopback.

## Alternatives rejected

- **Ask the daemon for every card instead** — the agent queue (fold, park, the
  1.5 s merge, the ten-minute reminder) and the night state live in the client
  and read its front window; a second copy in the daemon disagrees with it on
  the first restart, which ADR 0160 and 0153 already ruled out.
- **Poll only `/inherent/moment`, no field on the notices** — works, but
  doubles the 5 s traffic when job mail is on and lets the job-mail cards and the
  hold disagree for one tick; the field costs one key in a response already sent.
- **Keep held cards in the queue behind `hold` and show them one by one** — the
  digest of ADR 0153 already says several things waited in one card; showing
  five cards in a row after a call is the burst the hold exists to avoid.
- **Hold for as long as the daemon says call** — one wrong window title (ADR
  0161 lists them) would then hide every card until the window changes; the cap
  makes the worst case a few hours of silence, not an unlimited one.

## Consequences

- A call the facts cannot see (Discord voice, a window behind others for over
  90 s) is still not held, and the digest's title still reads "while you were
  away" after a call.
- Until the first poll answers after launch, a card can show for a moment: the
  hold is unknown then, and unknown holds nothing.
- The night glance is held while TimeSink calls him idle: if he wakes the
  screen in the night and looks without touching anything, the glance card
  waits for his next input.
- A held card leaves the queue at once, so a reminder timer that fires during a
  hold is dropped and the card returns in the digest instead.
- Startrail's own macOS banners and any other app are not under this hold.
