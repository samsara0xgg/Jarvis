# ADR 0313 — A day runs until he goes to bed, even past midnight, and the next page shows that tail

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** none

## Context

- ADR 0302 ends a day's waking part where the first night of sleep that begins within six hours
  after midnight begins. The day line it reads was folded from midnight to midnight, so a night
  that began after midnight was never among its facts and that rule never fired: every day ended
  at 00:00.
- 10-19 he worked at his Mac until 01:31, used his phone in bed and fell asleep at 02:33. 10-19's
  lane ended at 00:00 and 10-20's began when he woke at 08:57, so 00:00–02:33 belonged to no day;
  on the 10-20 page it was a black gap (his screenshot, 2026-10-21 00:24Z).
- Past midnight with no night recorded yet, the 10-19 lane counted as over, so its final run was
  written while he was still at his Mac, and asking to have the day tidied after midnight tidied
  10-20, which had no waking part yet ("no waking window").
- The sleep record reaches the brain hours late (10-20: 09:06 for a night that began at 02:33).
- The page draws one calendar day and already shows a sleep or a stay that crosses midnight on
  both days, with the same id and its real times.
- He asked for the fix (「动手」, 2026-10-21 00:25Z).

## Decision

Let a day's doing lane run past midnight until his night's sleep begins, and show the part past
midnight on the next day's page as well, with the same spans.

- A day's lane is read from its day line folded to six hours past its end, so ADR 0302's
  bedtime rule sees a night that begins after midnight.
- Past midnight, with no night begun and the six hours not over, the day runs on to now: he may
  still be up, so the day is not over and no final run is written for it.
- The next day's page serves, besides its own lane, the day before's doing spans that run past
  its midnight, with their ids and real times; a span holds only the items that page has.
- An answer about one of those spans is kept for the day before.
- Asked to tidy after midnight while the day before still runs, the brain tidies the day before.

## Alternatives rejected

- **Give each calendar day the waking time inside it (a morning piece before the night, then
  the day)** — the evening of 10-19 is one stretch (22:00–01:31 at the Mac with a friend on the
  call); cut at 00:00 it becomes two spans on two days, worded by two runs that never see each
  other, so one evening reads as two things.
- **Infer bedtime from his last activity when no night is recorded** — the Mac and phone go quiet
  for other reasons (10-20 13:08–14:19 he was driving with no screen use); a guessed bedtime
  would end days at the wrong time and the next morning's late sleep record would not correct it
  any sooner than the rule here.
- **Let the client fetch the day before to draw the tail** — two requests per page and two
  copies of the rule; the brain already folds both days.

## Consequences

- A day that is over is folded twice when its page is drawn (its calendar line for the items,
  its line to six hours on for the lane), and the morning after a late night folds the day before
  too.
- Until the night's sleep reaches the brain, the day before ends at midnight once six hours have
  passed; when the sleep arrives its inputs change and its final run is written again, so a late
  night costs two final runs instead of one.
- A stretch he answers on the next page that runs from the day before into the new day is
  refused; he answers each side on its own.
