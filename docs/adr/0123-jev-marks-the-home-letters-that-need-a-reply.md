# ADR 0123 — Jev marks the home's unread letters that need a reply

**Status:** Accepted
**Date:** 2026-10-02
**Supersedes:** none

## Context

- Allen, 2026-10-02, approved asking Jev (the hosted decision model ADR 0122
  already calls through OpenRouter) which of the unread letters on the
  companion home need his own reply, so the home can put those first and pop up
  only for them. His rule for it: nearly lossless, a missing mark over a wrong
  one. A letter wrongly marked "no reply needed" hides something he owes; a
  wrongly marked "reply" costs a glance.
- The home reads unread Primary mail itself, outside any model turn, and the
  companion polls the route every 5 minutes (ADR 0055). The same letters come
  back on every poll for as long as they stay unread.
- Until now nothing of a letter left the Mac except to OpenAI when Allen asks the
  model to read it. A question to Jev would send part of one to OpenRouter on a
  schedule, not on request; spec #egress is the list.
- Jev's `noul` question type answers with a probability that the answer is yes,
  so "very sure either way" is two bars on one number.
- The home route is awaited by the companion; a slow provider must not make the
  home slow.

## Decision

When `home.mail_reply.enabled` is true and `OPENROUTER_API_KEY` is set, ask Jev
once per unread letter whether the sender expects Allen to reply personally,
sending only the sender's display name and the subject, and give each letter a
`reply` of `yes` at probability `yes_at` (0.9) or above, `fyi` at `fyi_at` (0.1)
or below, and null in between, on a missing answer, and everywhere when the
feature is off.

- **Layer.** The question and the answer cache live in L3
  (`jarvis/decision/mail_reply.py`), over the transport ADR 0122 built; the
  settings are read and the object is built only in `runtime/`.
- **Egress.** Name and subject only: no address (a sender with no display name
  is sent as "unknown"), no body, no snippet. Every request carries
  `provider: {"zdr": true}`; a 404 is not repeated without it.
- **Once per letter.** The probability is cached by Gmail message id in memory
  for the daemon's life, bounded at 500; the bars apply when read, so changing
  them needs no new calls.
- **Deadline.** New letters are asked concurrently and the route waits at most
  `timeout_ms` (1500) in all. A letter whose answer is late or failed is unmarked
  for that poll and asked again at the next, five minutes later.
- **Companion.** Yes letters sort first, then unmarked, then fyi, newest first
  inside each; the pop-up fires only for a new yes letter, unless no letter has a
  mark at all (feature off), when it fires for the newest letter as before.
- **Cost.** Each answer's `usage.cost` is added to a running total and logged at
  info with the count. No event is written: the route runs on a worker thread
  and the Event Log connection belongs to the loop thread.
- **Default off** in the repo; Allen enables it in `settings.yaml`.

## Alternatives rejected

- **Send the body or a snippet too.** A name and a subject already separate a
  person from a notification (12 invented pairs, one live run on 2026-10-02:
  requests 0.86 to 0.94, notifications 0.05 to 0.08), and a body would move
  the most private part of a letter off the Mac every five minutes.
- **A fixed bar of 0.5, one mark per letter.** It forces a mark on every
  letter, and the owner's rule is the opposite; with 0.9 and 0.1 a letter in the
  middle shows nothing.
- **Ask on every poll.** The same unread letter would be sent again every five
  minutes for as long as it stays unread: 12 calls an hour per letter for an
  answer that does not change.
- **Cache failures too.** A timeout would then hide a letter's mark until the
  daemon restarts; one retry per poll costs at most one call per letter per five
  minutes.
- **Write a `route.surrogate_decided`-style event per call.** That event needs a
  turn, and `Home.mail()` runs on a worker thread while the Event Log connection
  may only be used on the loop thread; moving the write to the loop for a cost
  line was not worth it.

## Consequences

- Names and subjects of Allen's unread letters leave the Mac to OpenRouter and
  TypeSafe once he enables it; spec #egress says so.
- The cache is lost on restart, so each unread letter is asked again once per
  daemon life.
- A letter Jev is unsure about stays unmarked, so with the pop-up keyed to yes
  letters a real request Jev scores in the middle no longer pops up; it still
  shows in the list.
- Cost is only in the daemon log, not in the Event Log, so it is not queryable
  with the rest of the spend.
