# ADR 0026 — Live delegation outcomes outlive the session

**Status:** Accepted
**Date:** 2026-09-21
**Supersedes:** none

## Context

- A GPT-Live session (ADR-0016) hands a lookup to the backend and, while the
  session is open, speaks the answer as `session.commentary.append`. The
  backend turn does not stop when the session does: it finishes into the
  Event Log and memory.db, and then nothing tells Allen. The next session's
  brief may or may not carry the answer as a 200-character line and cannot
  tell a heard answer from an unheard one.
- ADR-0016 D5 turned any answer older than `delegation_timeout_s` (90 s)
  into a silent `thinking` append. Since D10 keeps the local speech chain off
  Live turns, a result later than 90 s has no voice at all, in any session.
  ADR-0016 is a pre-standard record and keeps its text; this ADR narrows D5
  and drops its §4 non-goal on resuming a pending result in a new session.
- The provider bills a session from `session.started` to `session.closed` by
  the second. The daemon logs the final `usage.seconds` and forgets it, so
  the ADR-0018 quota page cannot show Live. Sessions that no one opened by
  hand, such as a morning brief, will add to this.
- The provider's ACK proves receipt of an append, not playback, and the API
  emits no playback-done event (docs/gpt-live/live-conversations.md).
  "Delivered" can therefore mean at most "the provider accepted it into the
  session"; whether Allen heard it is a separate fact.
- Allen's direction, 2026-09-21: the backend is the long-lived session; voice
  connections, background tasks and the conversation have their own
  lifecycles; a voice close must not cancel a task and an old result must not
  leak into a session it does not belong to. "可以开始改吧直接改" on the
  recovery half of the phase C checklist.

## Decision

A Live delegation's outcome is a persisted fact told to exactly one session:
the session that asked, if it is still open when the outcome arrives, at any
age; otherwise the next session, at its start, as a commentary that names the
request and the result. Telling is recorded on the provider's ACK as
`live.result_delivered {turn_id, session_id, kind}`; a result the bridge
withholds because a newer request superseded it (ADR-0016 D5) is recorded
the same way with `kind = withheld`, so no session is told later. A new
session is told at most three outcomes, none older than 24 hours; the rest
stay in memory.db and the UI. Every session writes one
`live.session_usage {session_id, seconds, final, reason, server_reason}` at
close, with `final = false` when the server's `session.closed` carried no
usage.

Limits: an outcome that arrives while a different session is open waits for
the next start. Delivery is not hearing: no code path may treat
`live.result_delivered` as evidence that Allen heard the words.

## Alternatives rejected

- **Keep a late result as `thinking` (ADR-0016 D5 as written)** — measured
  2026-09-12: a `thinking` fact was spoken anyway, as the answer to a
  different question, so it is not quieter, only unaccountable; and with D10
  nothing else can voice a result older than 90 s.
- **Re-offer through the startup brief** — the brief is 1 500 characters of
  newest-first memory rows cut to 200 characters; a 938-character answer once
  left a 286-character brief. It cannot carry a result reliably and cannot say
  whether it was heard.
- **Mark delivered when the append is sent** — a send the socket drops is
  indistinguishable from one the provider accepted; the ACK is the only
  receipt the wire offers, and appends are rejected after send on the
  over-length error path.
- **Re-offer everything undelivered, forever** — a session opened after days
  away would open with a monologue billed by the second; three results and
  one day bound the cost and keep the rest reachable on demand.

## Consequences

- One outcome can be spoken twice when the ACK is lost after the words were
  spoken: the provider accepted the append but the daemon never saw the ACK.
  The duplicate is preferred to silence.
- A result that finishes while another session is already open waits for the
  session after it; a bus wake for a turn no pending record owns is ignored,
  as before.
- The first session after this lands may be told outcomes of the last day
  that were in fact delivered before the mark existed.
- `live.result_delivered` rows join the per-turn correlation index, and the
  type scan gains a time bound so the start-of-session lookup reads one day,
  not the whole log.
