# ADR 0121 — The wait line waits 4 seconds, then follows up

**Status:** Accepted
**Date:** 2026-10-02
**Supersedes:** none

## Context

- Live test, 2026-10-01: the 2.5 s clock of ADR 0116 fired twice right before
  a plain answer, 0.6 s and 0.15 s ahead of it. In turn Tbd3d5d36 the answer's
  first chunk was committed at 05.785 and began playing at 05.799; the line was
  rendered at 05.916, queued behind the answer in its group, and played at
  11.061, after the whole answer. The clock was due at 05.398: the
  answer-started check ran then, before the projection rebuild and the render.
  In turn T42e99f29 "On it." preceded "Yes." by 0.2 s, which is the early clock
  working as designed.
- Allen, 2026-10-02: the tool status line (ADR 0115) did not show in the
  companion because the wait line's `open` ended the turn's wait there, as an
  answer's does.
- Measured on the owner's log since 2026-09-26, turns with no tool, from the
  end of his words (`utterance.received`) to the answer's
  `surface.response_open`: median 2.8 s, p75 3.6 s, p90 5.0 s, p95 6.5 s
  (n=233). A 2.5 s clock fires on 139 of 233, 79 of them within 1 s before the
  answer; a 4.0 s clock fires on 39 of 233, 13 within 1 s.
- Turns with a tool (n=85): the first `action.dispatched` at a median 2.5 s,
  p90 3.9 s; the answer at a median 8.5 s, p75 16.5 s, because every tool means
  at least one more model round of about 3 s.
- Allen, 2026-10-02: a wait of about 2 s needs no voice (the text under the
  orb is enough), a long wait does, and if it keeps taking long she should say
  another line later so there is at least some feedback.
- Tier 0 (`regex_router`) turns end within about 1 s, so the 1.5 s floor and
  the turn-ended check keep them silent.

## Decision

A voice turn says its first wait line 8.0 s (first shipped as 4.0 s) after his words with nothing of the
answer started, tool or not; the dispatch of a tool speaks only for
`LONG_WAIT_TOOLS` (ADR 0117), no earlier than the 1.5 s floor. While the
answer still has not started, a follow-up from `commentary.still` ("Still
working on it.") is said at 25 s after his words, at most two
lines per turn (first shipped as 12 s and 25 s, three lines), never over a pending confirmation. The answer's first chunk or
playback start, a turn's end, and a pending confirmation are checked last,
right before the line is rendered and once more right after; a line that is
emitted but has not reached the speaker when the answer starts is cancelled and
never played: the media owner drops a line that has not started playing when
its group's answer opens, and a line already playing finishes. The wire marks a
wait line's `open`, `append`, `done` and `playing`/`spoken` envelopes with
`response_phase: "commentary"`, and the companion treats them as speech only
(no text, no end of the wait). The model's `lead_in` replaces the first line
only. The rest of
ADR 0116 and ADR 0117 is unchanged. `SLOW_TOOLS` stays for ADR 0115's status
line and no longer feeds the commentary decision.

## Alternatives rejected

- **Keep 2.5 s.** It fires on 139 of 233 toolless turns and 79 of those within
  1 s before the answer; 4.0 s fires on 39 and 13.
- **Speak at every tool dispatch.** The first dispatch lands at a median 2.5 s,
  where the answer is 6 s away on average, but a wait of about 2 s is what the
  owner said needs no voice; the 4.0 s clock already covers the tool turns whose
  answer is late.
- **Keep one line per turn.** A 16.5 s p75 answer after a tool leaves 12 s of
  silence after the first line; the owner asked for later feedback.
- **Leave the lost-race line to the answer-started check alone.** The log shows
  the check passing and the answer starting before the line rendered; only
  dropping the unheard line removes the race.
- **Cancel the line's run in L3 when the answer opens.** Media starts a line
  before its `playback_started` row exists, so a cancel then would interrupt a
  playing line and purge the answer queued behind it (reproduced in a replay);
  the media owner knows what has started.
- **Show the wait line as text in the talk area.** The status line and the
  spoken line already say it; text for a line that is gone in two seconds adds
  a row that the answer then replaces.

## Consequences

- A toolless turn that answers between 2.5 s and 8.0 s is silent for that
  stretch, with only the status line under the orb.
- A long-wait tool dispatched late can say its line shortly before a follow-up
  at 25 s; the cap and the clocks do not look at each other.
- 2026-10-02, the owner found the line said too often ("hold on" every little
  while): in 6 hours 16 of 83 turns heard 26 lines, and 8 of those 16 were
  answered before 8 s. The clock moved to 8.0 s and the follow-ups to one at
  25 s, two lines at most.
- The 8.0 s and 25 s are hand-kept numbers from one week of the owner's
  log; if the model's latency changes they move by hand.
