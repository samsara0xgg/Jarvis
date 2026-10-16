# ADR 0275 — Jarvis is the assistant that runs its owner's day, and agents are one of the things it watches

**Status:** Accepted
**Date:** 2026-10-16
**Supersedes:** none

## Context

- On 2026-09-29 the owner set the goal the spec serves (ADR 0089): anyone can download
  Jarvis and run it on one key of their own, starting with Mac users of Claude Code and
  Codex. The spec opens on it, a notch companion for Mac developers that watches every
  agent, and chapter 8 calls the two agent programs the base of Jarvis's main value.
- On 2026-10-15 he named his first requirement of it: 「我觉得 Jarvis 得帮我计划每天，
  这是我给他的第一个要求」, a plan set with him each morning, kept to during the day,
  reviewed each night and paced toward a long-term goal.
- His own use is the only use on record, and he is the spec's target user: 452 Claude Code
  sessions were started on his Mac in the 30 days to 2026-10-15. In the brain's event log
  from 2026-09-24 to 10-15, 1 of 135 agent-finished cards was acted on and 45 of 52 agent
  needs-you cards were dismissed; the Agents window holds 14 sessions and none was started
  after 10-05. The proactive output he did act on was the reminders he set himself, for
  interviews and departures.
- Commitments are where the brain is thinnest. On 2026-10-15 the core-memory section for
  commitments held 1 item against 25 for people, and 2 places were saved. In the week before,
  two dated commitments were missed; the date of one had sat in his mail, read by the
  job-mail pass and set aside as unrelated to the job hunt.
- As of 2026-10 the general assistants ship voice conversation (GPT-Live, full duplex,
  2026-07-08), mail and calendar, scheduled briefs, cross-chat memory and cloud errands to
  every subscriber, and Claude Code's Remote Control, routines and background agents cover
  watching agent sessions away from the desk. None keeps the owner's record across his own
  devices or times speaking up by his present state: ChatGPT Pulse was retired on
  2026-06-17, and scheduled tasks run at most hourly.
- C4 already keeps the assistant from doing the work itself, whatever it watches.
- On 2026-10-16 the owner chose 「定」 on this direction and approved this text with 「可以」.

## Decision

Position Jarvis as the assistant that runs its owner's day: it remembers his life, plans
each day with him and sees it through, and speaks only when it is worth it, on the device
he is using; the agents it watches are one source of what needs him, not its purpose.

- **Spec.** Four places change with this ADR: the one-sentence positioning, the 「它是」 row
  that calls it the agents' manager, C3's list of what the owner should not have to reload,
  and the opening sentence of chapter 8. C4, chapter 8's interfaces and the rest of
  chapter 1 stay.
- **Limits.** No feature changes by this decision. Which cards stop speaking for agents, and
  the planning, nudging and record-keeping that serve the day, are decided in their own ADRs.

## Alternatives rejected

- **Keep the notch companion that watches agents.** Its best-fit user is the owner, who
  started 452 Claude Code sessions in 30 days, acted on 1 of 135 agent-finished cards and
  started no Agents-window session after 2026-10-05.
- **A record for understanding oneself** (one of three positionings offered on
  2026-10-14). A record without a daily use is not read: from 2026-10-13 the brain held 5 to
  17.5 h a day of Mac audio and 3,240 imported items, nothing had been drawn from the audio,
  and turns about his own past were 7% of his 1,338 turns from 2026-09-25 to 10-15.
- **A general assistant that wins on conversation, mail and errands.** Those ship to every
  subscriber of the large assistants; Jarvis's spoken answers on the Mac start p50 3.3 s
  after his speech ends (week of 2026-10-12, 17 turns), against full-duplex voice in GPT-Live.

## Consequences

- An owner who runs no coding agent becomes the main case: install and first run cannot
  assume Claude Code or Codex is present, and chapter 8 describes an optional part of the
  product rather than its centre.
- Every proactive output is weighed against the day, by what it costs him to miss it. The
  agent cards, acted on 1 time in 135, lose their default claim on the notch, and the ADR
  that next sets them has to say where they go instead.
- The brain has to hold commitments and places as state of their own; core memory's
  commitments section is a summary of him, not a store a day plan can be built from.
