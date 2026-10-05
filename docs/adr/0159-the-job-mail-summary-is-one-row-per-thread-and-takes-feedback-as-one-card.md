# ADR 0159 — The job-mail summary is one row per thread and takes feedback as one card

**Status:** Accepted
**Date:** 2026-10-04
**Supersedes:** none

## Context

- Allen, 2026-10-04: the live summary of ADR 0158 read "3 job emails in the
  last two days, 3 about interviews" over three rows that were the same
  Reliable Controls thread (one with no role, two with slightly different
  roles), and offered nothing but a link. He wanted the card to say what is
  in it at a glance, show when the interview is, and let him rate it like a
  single card.
- ADR 0158 fixes the summary's header as "N job emails ... X about interviews".
  That wording and its one row per mail are what this decision replaces; the
  rest of 0158 (when a summary forms, never speaks, local rules, repair) stands.
- Company and role are guesses read by regex (0155, 0158), so a thread's mails
  carry different roles, some empty. The interview time is read by the same
  local regex and is often empty or only a sentence of up to 200 characters.
- A summary stands for several alerts, each with its own `job_feedback` rows,
  and the 合适吗 answers train a later judge per mail (0155).

## Decision

The summary shows one row per company and Gmail thread, says the one company
and kind when that is all it holds, shows the interview time as the row's key
fact, and takes the 合适吗 feedback once for the whole card.

Its limits:

- **Rows.** Alerts group by company (case-folded) and thread; a mail with no
  thread is its own row. A row's kind is the most important of its mails
  (offer, interview, rejection, then the rest), its role the longest stored in
  the group, its count the mails, its time the latest alert. A row alone leaves
  its count to the header.
- **Header.** When every mail is one kind of one company: "{company} {kind}有 N
  封新邮件" (English variant in the language table). Otherwise 0158's wording,
  with N and the interview count taken over mails, not rows.
- **Time.** The row shows the latest dated mail's `event_at` (24 h, local zone),
  else its `event_text` when 24 characters or fewer, else the latest mail's time
  in plain type. Nothing is read from Gmail and no model is called.
- **Feedback.** The 合适吗 row is the single-card component, folded by default.
  A level or 对 on the summary is logged as one `job_feedback` row per included
  alert, `level_shown` being the summary's level (`card_sound` when any included
  alert sounds at the quiet level in force, else `card`). Seen and dismissed end
  every included alert as before, dismissed logged at the summary's level.

## Alternatives rejected

- **Group by company only** — one company can hold several threads (an
  application, then a second round for another role); one row would hide that
  and pick one time for both. Threads are visible in the stored `thread_id`.
- **Ask a model to write the summary** — the rows are already typed facts, and
  a model call per card adds egress for text a template renders from stored
  fields.
- **Show `event_text` whatever its length** — it is a whole sentence up to 200
  characters; at the card's width it pushes the company off the row.
- **Log the summary's feedback once against the digest id** — `job_feedback` and
  the attention log are keyed by alert and mail, and a judge trained on them
  would never see the answer.

## Consequences

- A thread that mixes kinds (a receipt, then an interview) is one row of the
  best kind, so its receipt does not show; the header falls back to the mixed
  wording because not every mail is one kind.
- Two threads of one company with the same kind read as two rows under a company
  header; a company the regex split in two stays two rows.
- A level chosen on a summary is logged against mails Allen did not look at one
  by one; the training signal for those is coarser than a single card's.
