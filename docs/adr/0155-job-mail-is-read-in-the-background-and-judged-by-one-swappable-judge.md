# ADR 0155 — Job mail is read in the background and judged by one swappable judge

**Status:** Accepted
**Date:** 2026-10-04
**Supersedes:** none

## Context

- Allen, 2026-10-04: he is job hunting for co-op and developer roles and wants
  Jarvis to read his mail without being asked, keep a ledger of who wrote what,
  and tell him by a rule: a receipt only goes in the ledger, a rejection is a
  card, other useful job mail is a card with a sound, an interview or an offer
  is spoken once and also a card. The quiet level (ADR 0153) holds all of it.
- The mail that matters is not in his home list. `home.mail()` reads only
  `category:primary` and drops no-reply senders (ADR 0123), and the first mail
  he wants caught, an application receipt, comes from a no-reply address in
  Updates.
- Jev, TypeSafe's decision model, answers only `noul`, `choice` and `score`
  questions. A free-text question is refused with HTTP 400 (`invalid_union`,
  "Expected 'noul' | 'choice' | 'score'", probed 2026-10-04), so a company, a
  role or an interview time cannot be asked of it.
- A subject such as "Update on your application" is used for rejections,
  interview invitations and offers alike. Typing needs the body.
- ADR 0123 and 0148 say a body never goes to Jev. That is true of the home list
  and the mail page, and stays true there. This path has no turn and nobody to
  ask first, so the only egress that is narrow enough is Jev with
  `provider.zdr: true`, not a general model.
- His taste in what reaches him is not settled. The rule table will change, and
  a later judge may be learned from his reactions; a rule baked into delivery
  code leaves no record of what it saw, so no other judge can be tried on past
  events.

## Decision

Jarvis reads Allen's recent Gmail in the background with `gmail_search` and
`gmail_get` only, types job-hunt mail with Jev, and delivers each event at the
level of one swappable judge whose every decision is logged with the exact
context it saw.

Its limits:

- Gmail is read, never changed: no label, archive, trash or send. The code
  refuses any other Gmail tool, and a test asserts it.
- Egress has two steps. Jev first sees the sender display name, the domain of the
  sender address (never the address) and the subject. A mail it is sure is not
  job mail (0.9) stops there. For the rest Jev also sees the first 3000
  characters of the plain-text body, for typing only. Bodies go nowhere else.
- Only typed facts are kept in `memory.db`: sender name and domain, subject,
  kind, company, role, an interview sentence and time read locally by regexes,
  and the alert. Never the body. A mail held back with at least 0.2 chance of
  being job mail also keeps its header facts so Allen can audit what was
  skipped; other held-back mail keeps only that chance.
- The pipeline is event, context pack, judge, delivery, feedback, log. A
  `ContextPack` holds the source, the event id, its typed facts and the
  situation at decision time (hour, weekday, the quiet level, whether speech is
  allowed). A `Judge` is `ContextPack -> Judgement`; a `Judgement` is a level
  (`ledger`, `card`, `card_sound`, `speak`), a reason, and the judge's id and
  version. The daemon is given the judge at wiring time; job mail's rules are
  `rule_judge_v1`, which also keeps a mail older than 48 hours at `ledger`.
  There is no registry and no loader: the seam is one function.
- Delivery acts on the level and is held by the quiet level at read time: quiet
  takes the sound off, no-pop and dnd return nothing and the alerts wait, and
  back at off two or more that waited are one digest. She speaks one fixed
  line, decided once when the alert is made: only at quiet off, speech not
  muted, no live conversation, at most once per ten minutes. An alert made
  while held never speaks later.
- Each decision is one row in `attention_log` with the full pack as the judge
  saw it, the judgement, what delivery did (shown, held, spoken, suppressed,
  ledger only, each with a time) and Allen's five-level reactions as they
  arrive. `replay(judge, rows)` returns what another judge would have said about
  the stored packs and writes nothing.
- If Gmail cannot be read in three cycles in a row, or for two hours, one
  health alert says so, at most once per twelve hours, under the same quiet
  level; recovery says nothing.
- All of it is off by default (`job_mail.enabled: false`).

## Alternatives rejected

- **Header-only Jev, no body** — a subject cannot tell an offer, an interview
  and a rejection apart ("Update on your application" is all three); it would
  need a labelled sample showing header-only typing at the body's accuracy to
  win, and none exists.
- **Gmail's own filters (Primary, or a sender allow-list)** — the receipts and
  recruiter mail that matter arrive from no-reply senders in Updates and
  Promotions; the home query excludes exactly them, so the first test mail
  would be missed.
- **Jev for company, role and time** — refused by the API with HTTP 400 today;
  the regexes are the ceiling until Jev takes free text, and a person's name as
  a display name reads as the company.
- **The mail-summary preset (gpt-6-luna) for typing** — it would send the body
  of every non-skipped mail to OpenAI on a timer with no user prompt; Jev with
  `zdr` sends it to a zero-retention route only.
- **The rule table inside the delivery code** — it leaves only the outcome in the
  log, so a changed rule or a learned judge cannot be run over past events, and
  his reactions cannot be set beside what the rule saw.

## Consequences

- Mail bodies for non-cleared job-looking mail leave the Mac for OpenRouter and
  TypeSafe, and the Jev log keeps what was sent, so it now holds those bodies
  (local only, never pruned). The spec's egress, retention and Jev-log rows say so.
- A mail Jev never answers (cap, timeout, no key) is retried three times and
  then kept as an error; a mail beyond the 100 newest ids of a poll is not read.
- `attention_log` is never pruned and grows by one pack per job mail; its
  packs are typed facts, so it never holds a body, but it does hold the
  situation he was in when a card reached him.
- The first judge is a table, so what Allen wants beyond it only changes when
  he or a later ADR changes the table or swaps the judge.
