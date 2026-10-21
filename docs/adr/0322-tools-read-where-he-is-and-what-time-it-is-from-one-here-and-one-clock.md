# ADR 0322 — Tools read where he is and what time it is from one Here and one clock

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** in ADR 0198, the rules that any paired phone's latest report is where he is and that it has no age limit; in ADR 0281, the clause "Not for `bus`". ADR 0212 stands: here follows the device that spoke.

## Context

- The tools that need his position read it in different ways:
  - `bus`, `map_search` and `weather` get `here_phone`, the latest report in the log however old
    (`read_phone_here`: "There is no age cutoff").
  - Only `where_am_i` gets the Locator (ADR 0281). It asks the phone for a fresh fix over its socket
    (4 s), then by push (8 s); on 2026-10-18 a fix took 1.4 s.
  - `save_place`'s "here" takes the latest fix up to 30 minutes old, from any device, and never a
    fresh one.
  - The spec says the bus's "here" uses the same reading as `where_am_i`. It does not.
- The no-cutoff rule assumes the phone reports whenever he moves (ADR 0198). But the phone reports
  only after he has moved about 200 m and come to rest (`LocationCollector.swift`), so while he is
  on the way the last report can be far behind.
  - On 2026-10-14 a bus search started from a report a day and 15 hours old.
  - On 2026-10-18 `map_search` searched around a point 8 hours old.
  - Inside a kept place, `bus` says he is still at home with no warning.
- `latest_phone_fix` takes any paired device's report, so an iPad left at home can be "here" while
  his iPhone is downtown.
- Asked about the present, she answers from the state lines and is told to say how old a fact is
  (prompt line 83). On 2026-10-18 she disclaimed an 8-hour-old position twice instead of fetching a
  new one.
- Time:
  - The spec already says tools turn his words into local time and the model computes no zone
    (`#tools`).
  - Tools read the wall clock themselves, in a dozen modules.
  - Several modules fix their own zone. The transit ones rightly use the agency's zone; others
    should use his.
  - `work_state.timezone` is empty by default.
- ADR 0317's tool layer runs each item with the clock frozen at the item's moment. A tool that reads
  the wall clock or the log's latest report on its own escapes that.
- A tool gets `conn`, `runtime_paths` and `action_id` from its `ToolContext`, and anything else
  through a closure the runtime wires.

## Decision

Give every tool, through its context, one Here and one clock from the runtime.

- **Here** says where he is under one policy. It prefers the device that opened the turn. It fetches
  a fresh fix when the last report is too old to trust.
- **The clock** gives the time in his one configured zone.

Its limits:

- **One Here.**
  - It is the Locator plus one policy. `bus`, `map_search`, `weather`, `save_place`, the start of a
    ride and `where_am_i` read it, and no tool reads the log's reports itself.
  - **The device.** The one the turn came from (ADR 0212), else the phone he carries. A report from
    another device is "here" only on that device's own turn.
  - **Age.** A report older than Here's limit triggers a fresh fix, with the waits ADR 0281 set.
    `where_am_i` still asks for a fresh fix every time, because finding out exactly where he is is
    its whole job. If no fix comes, the result gives the last place, its age and its device, and
    says it may not be where he is now.
  - **One answer.** Its reading carries the position, accuracy, age, device and whether it is fresh.
    Every tool passes it on in its receipt (ADR 0319) as the place it used.
- **One clock.**
  - Tools take the time only from the clock in their context. The runtime gives the real one, and
    the exam gives one frozen at the item's moment.
  - His zone comes from one setting, defaulting to the system zone.
  - A source's own times are converted by code from the zone that source names. The model never
    converts.
  - At boot the zone database must know his zone's current rule.
- **State lines follow Here.** The state block's line for his place uses Here's age limit. Past it,
  the line says his place is unknown now, how old the last report is, and that `where_am_i` asks the
  phone. Prompt line 83's "saying how old a fact is" goes for his place. The text goes to Allen
  first.

## Alternatives rejected

- **Give each tool the Locator.** Every tool would then carry its own copy of one policy, and copies
  drift: `map_search` copied `bus`'s reading after ADR 0281 said "Not for `bus`".
- **Always fetch a fresh fix.** That adds 1.4 to 12 seconds to every call that needs his position,
  while a report a few minutes old is as good.
- **Keep the no-cutoff rule and show the age.** On 10-18 the age was shown, and she disclaimed an
  8-hour-old position twice instead of fetching a new one.
- **Freeze time in the exam by patching the wall clock.** A module that keeps its own zone or reads
  time another way escapes the patch, and the item then passes on today's date instead of its own.

## Consequences

- A call that needs his position can wait up to 12 seconds when the phone answers neither the socket
  nor the push.
- Here's age limit is a number to tune: too short and calls wait for fixes, too long and a stale
  point passes as fresh.
- Every tool that reads the time changes, and so does every test that relies on the wall clock.
- An iPad turn reads the iPad, so if he left it on the couch it answers for the couch.
