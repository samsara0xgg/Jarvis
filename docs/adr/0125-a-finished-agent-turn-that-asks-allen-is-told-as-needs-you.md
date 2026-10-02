# ADR 0125 — A finished agent turn that asks Allen is told as "needs you"

**Status:** Accepted
**Date:** 2026-10-02
**Supersedes:** none

## Context

- Allen, 2026-10-02, approved asking Jev (the hosted decision model ADR 0122
  and 0123 already call through OpenRouter) whether a coding agent's finished
  turn ends by asking him for something, in his words "near-lossless, upgrade
  only": a finish may become a needs-you notice, nothing may be demoted.
- Both Startrail's host and the daemon's Claude board tell a finished turn as
  "done": a 5 s pop on the notch, no cue of its own, and no banner unless he
  turned `notify.done` on. A turn that ends with "which of the two do you
  want?" is only visible to him as a name flashing by, while a permission
  request or a plan waiting for approval gets a card, the ask cue and a banner.
- The agents do not say which kind of ending it is. Offline, on 246 real turn
  endings labelled by Claude (101 asks, 145 reports), Jev's `noul` probability
  at 0.95 caught 63 of the 101 asks and flagged none of the 145 reports; the
  median answer took 0.14 s and a call costs about $0.00003.
- Jev's answer cannot hold up a state change: the host's `done` must show at
  once, and the daemon's board is read every 1.5 s by the companion.
- Until now no text an agent wrote left the Mac except to the agent's own
  provider; spec #egress is the list.

## Decision

When `agents.turn_end_asks.enabled` is true and `OPENROUTER_API_KEY` is set,
ask Jev once per finished turn whether the last 600 characters of the agent's
final message ask Allen to decide, choose, approve, answer or provide
something, and when its probability reaches `at` (0.95) tell that finish as
the "needs you" notice a question would be: a card with the message's first
line, the ask cue, a banner of kind `wait`, and the queue's ask rank.

- **Upgrade only.** Below the bar, on a timeout, an error, no key, the feature
  off, or the daemon away, the finish is told exactly as before. Permission
  requests, plans, questions and errors never go through Jev.
- **One classifier, in the daemon.** `jarvis/decision/turn_end_asks.py` holds
  the question and an answer cache keyed by session id and a hash of the tail,
  over the transport ADR 0122 built; settings are read only in `runtime/`.
  Each call logs the session id, the probability, the decision and the cost,
  never the text. Every request carries `provider: {"zdr": true}`.
- **Two callers.** Startrail's host posts the tail to
  `POST /inherent/agents/turn-end` as a session enters `done`, without
  waiting, and sets `asks` on the session when the answer is `true`; the
  daemon's Claude board asks the same classifier without waiting for each
  `done` row that finished under 15 minutes ago, and serves `asks` on the row
  for as long as it holds an answer.
- **Ends like a done notice.** The notice holds only while the session is
  `done` and unread: opening it, answering it or starting its next turn ends
  it, as they end any finish.
- **Default off** in the repo; Allen enables it in `settings.yaml`.

## Alternatives rejected

- **A regex on the last line (a trailing question mark, "which", "do you
  want")** — it cannot read the Chinese or mixed endings the agents write, and
  a report that quotes a question would match; Jev answers without a pattern
  list to keep.
- **Classify inside the host with its own key** — the key lives in the daemon's
  `~/.jarvis/env`, and a second client would need its own transport, zdr
  handling and cost log; the host already reaches the daemon with the local key.
- **A lower bar to catch more asks** — 0.95 already leaves 38 of the 101 asks
  as plain finishes; the owner's rule makes a wrongly flagged report cost more
  than a missed ask, which is only today's behaviour.
- **Wait for Jev before showing `done`** — a 1.5 s ceiling on every finish for
  a feature that upgrades 6 in 10 asks; the finish shows at once and upgrades
  when the answer lands.
- **Ask on every board read** — the board is rebuilt every 3 s for as long as a
  session stays done; the cache keeps one call per turn ending.

## Consequences

- The last 600 characters of a finished agent message leave the Mac to
  OpenRouter and TypeSafe once Allen enables it; spec #egress says so.
- A finish may show as a pop first and become a card up to 1.5 s later: the
  pop is taken back when the card arrives.
- A timeout or error is remembered for that turn ending, so a missed upgrade
  is not retried; it stays a plain finish.
- The daemon's cache is lost on restart, so a board row that finished under 15
  minutes ago is asked once more; a Startrail session keeps `asks` in the
  host's own file.
- Cost is only in the daemon log, not in the Event Log: the call runs on a
  worker thread, as ADR 0123's do.
