# ADR 0351 — A device tool says at once that its device is not there, and the Dashboard reports only a page it showed

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** in ADR 0176, `show_on_dashboard` as a read-only tool that reports the page turned without the Dashboard's answer

## Context

- The Mac's tools run on the Mac, called by the brain over the terminal link (ADR 0170). A device
  that is away keeps its tools on the menu, and a call fails as not connected (ADR 0225). The state
  block says which devices are connected (ADR 0217).
- Three paths lose that failure:
  - Before `read_file` runs, the brain asks the terminal to resolve the file's name. A failed link
    resolves to nothing, the gate refuses the call for want of a target, and she says she cannot
    find the file. An offline Mac, a timeout and a missing file read the same.
  - A Tier 0 tool's error is one fixed line, 「这条指令执行出错，未产生结果。」. 「看一下我的屏幕」
    with the Mac asleep gets only that line.
  - A socket that stays open while the Mac sleeps holds a call for the full 30 s before it fails as
    a timeout. The receipt table (ADR 0319) counts a timeout as don't-retry, not as needing him.
- The terminal hears its Mac go to sleep and wake: its power observer writes `mac.sleeping` and
  `mac.awake` to the terminal's own log. The brain is not told.
- `show_on_dashboard` pushes a `present` op to every connected companion and returns "The Dashboard
  is on the X page" without waiting for an answer. With the Mac asleep, or no Dashboard connected,
  nothing turns and she says it did. The tool is marked read-only, so when she truly says she put a
  page on screen, no change backs the claim in the receipts (ADR 0320), and it is sent back.
- The Dashboard posts its view to the brain on every change and every 20 s while it is open
  (`POST /inherent/view`, ADR 0176).
- On 2026-10-21 Allen approved the plan for these tools (「就按你说的去吧」):
  - check first whether the Mac is there; if not, say 「你的 Mac 现在没连上」;
  - a Dashboard page counts as shown only when the Dashboard answers that it got it; otherwise say
    the Mac is not connected;
  - mark the Dashboard tool as one that changes something.

## Decision

Know before a device call whether the device can answer, and give every path the same plain failure
when it cannot.

- **Presence first.** A call goes to a device only when the device has shown within the last few
  seconds that it can answer:
  - the terminal tells the brain when its Mac goes to sleep and when it wakes;
  - a link that has been quiet answers a ping within 3 s before the call is sent.
  A device that cannot answer fails the call at once as not connected.
- **One failure, everywhere.** Not connected, asleep and no answer within the tool's own time are
  failures that need him (ADR 0319). Each failure names the device and when it was last there. The
  model gets it as the tool's result. The brain's resolver of a device's file names passes it on
  instead of resolving to nothing. Tier 0 says it from the zh/en table (「你的 Mac 现在没连上」)
  instead of its generic line.
- **The Dashboard answers.** `show_on_dashboard` is an action, not a read. It is done only when a
  view report showing that page, and that item when one was asked for, arrives within 3 s of the
  push. Otherwise it fails. With no Mac connected, the failure needs him and says so. Otherwise it
  says the Dashboard did not show the page.
- **Kept.** Device tools stay offered while their device is away (ADR 0225). Where they sit on the
  menu is ADR 0354's decision.

## Alternatives rejected

- **Lower the call timeout for every device call.** How long a tool may take and whether the Mac is
  there are separate questions. A Spotlight search over his disk or a screenshot's downscale can be
  slow while the Mac is there. This choice is wrong if every device tool's slowest real call is under
  5 s.
- **Take a device's tools off the menu while it is away.** ADR 0225 rejected it: each connect and
  disconnect would change the head of the cached prompt, and she would lose tools for a lid closed
  for a minute.
- **Keep the push unanswered, and word the result as "asked the Dashboard".** She would still say
  the page is up when nothing turned. The report that proves it already arrives within a second of a
  change.

## Consequences

- A page turn waits for the Dashboard's report. A lost report spends 3 s and reads as not shown,
  even if the page did turn.
- A call on a quiet link pays one ping over the private network first.
- The sleep notice needs a terminal at least as new as the brain. An older terminal is checked by
  ping alone.
- A Mac that sleeps without its notice reaching the brain, such as on a dropped network, still costs
  the ping's 3 s.
