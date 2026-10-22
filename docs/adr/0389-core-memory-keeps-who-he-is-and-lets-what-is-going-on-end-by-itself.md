# ADR 0389 — Core memory keeps who he is and lets what is going on end by itself

**Status:** Accepted
**Date:** 2026-10-22
**Supersedes:** the clauses of ADR 0146 that let a nightly change cite only the records of the day being read and remove the item it marks stale. Everything else in ADR 0146 stands.

## Context

- The nightly pass (ADR 0146) reads one day and may change an item only by citing that day's
  records. An item is retired only on a day that happens to show it is over. Nothing that is never
  mentioned again can leave: on the live note of 2026-10-21 (17,312 characters, 69 versions),
  生活里的几件事 holds 29 items, and at least six are over or repeated, among them a wake-word bug
  from 2026-09-13, the daily-report status of 2026-09-21, the spring exam season of 2026-04 and the
  job hunt written twice, once in English.
- Items imported from Claude chats (kind set) cite no record at all, so no nightly change can ever
  reach them, and some of them are the assistant's inference, marked 推断, loaded every turn beside
  his own words.
- The section caps (ADR 0258) are far from full, so "merge or retire to stay under the cap" never
  fires either.
- Two kinds of item share one lifecycle. Who he is (his story, values, body, taste, people, how he
  wants to be treated) is true until something says otherwise; silence means nothing. What is going
  on (a job hunt, a road test, a car order, a monthly check-in) is true only while it is alive;
  silence usually means it is over.
- The ledger (ADR 0201) already computes the last 14 days, the week and 30 days from his data, so
  recent activity does not need lasting items; but threads such as the car order or the monthly
  check-in run for months, beyond any window.
- A past-dated 承诺和待办 item was retired by date in code (ADR 0154) until that section left with
  ADR 0299; dates are the one kind of ending a program can see without a model. A thread line also
  carries dates that were history when it was written ("as of 10-01", "interviewed 10-14", the day
  the car was ordered), and only the dates still ahead of it say when it ends.
- A whole-note rewrite each night was rejected in ADR 0146 because one bad rewrite damages a note
  every turn trusts, with no check.
- On 2026-10-22 Allen agreed that history belongs in the raw data, not in what every turn carries
  (「保留历史只应该存在原始数据里面」), asked for the long-term design rather than a cleanup
  (「我们所有的东西都是长远最优」), approved this decision (「可以，没有问题」) and chose Opus 5.5
  for every memory job (「就都用 Opus 吧」).

## Decision

Core memory holds standing items and thread items with separate lifecycles, and once a month the
note is rebuilt as a reviewed version, so an item leaves the prompt when it stops being true and its
history stays in the store.

- **Standing items** (经历, 性格和价值观, 身体, 钱, 兴趣和品味, 你希望被怎么对待, 定下来的规矩,
  身边的人) change only on evidence: a rewrite or stale may cite records of any day, not only the
  day being read (the records any item already cites are offered beside it). Silence never retires
  them. Pinned items stay his to remove (ADR 0154).
- **Thread items** (生活里的几件事, 证件和硬期限) each carry a state (open or closed) and the day of
  their latest evidence. The nightly pass renews that day when a day shows the thread still going,
  without rewording it. A thread closes on evidence, or in code when every date it names that lay
  ahead of its last wording has passed. An open thread with no evidence for 21 days is not dropped:
  she first looks in his data (mail, calendar, the ledger) for its state, and only when nothing
  answers asks him once, at a fitting moment, whether it is still going; his answer is the evidence.
  At most one such thread is put to her a day.
- **Closed and stale items** leave the prompt and stay in the store with their history, searchable
  and restorable from the memory page.
- **Monthly rebuild.** On the first night of a month a model writes the note anew from the current
  version, his pinned items and the month's day summaries, under the same rules as the nightly pass.
  It lands as one version of origin rebuild only if every gate passes: each pinned item kept word
  for word, each dropped item listed with its reason, caps held, the answer not cut off. The memory
  page shows the rebuild's diff first, and undo works as for any version.
- **Inference is marked.** An item resting only on the assistant's reading, with no words of his
  behind it, renders as inferred. A rebuild keeps it only if a record of his words now supports it.
- **The first rebuild** runs once by hand under these rules and is reviewed with him before it
  lands; it is how the imported and bug-ticket items leave. The monthly job waits for it.
- **Every memory job runs on Opus 5.5**: the rebuild at effort high, the nightly pass, day
  summaries and compaction at medium.

## Alternatives rejected

- **A weekly sweep that retires whatever has gone quiet** — silence ends a road test but not his
  dogs or his teeth; on the live note, 32 of the 134 items are interview entries from one day,
  2026-10-14, and an age rule would put them first in line once he stops retelling them.
- **Keep the nightly pass alone and only allow citations of any day** — the pass still reads one
  day at a time, so an item nobody mentions is still never looked at; the 2026-09-13 bug would stay.
- **Derive what is going on from the ledger alone and drop thread items** — the ledger reaches back
  30 days and counts activity; the car order (2026-10-03, delivery 2026-12 to 2027-01) and the
  monthly check-in run longer than any window and are not counts.
- **Close a thread when every date it names has passed, history dates included** — the car order
  line names 2026-10-03, the job hunt line the day of an interview it still waits on; both would
  close the night after they were written.
- **Rewrite the whole note every night** — ADR 0146's reason holds: a nightly unchecked rewrite of
  what every turn trusts; a monthly one with gates, a diff he sees and undo is checkable.
- **Clean the note by hand once** — the rule that let the items stay is unchanged, so they grow
  back; the hand pass is kept only as the first rebuild, under the new rules.
- **Higher effort everywhere** — high effort makes a consolidator more eager to change things when
  the right answer is often no change, and its thinking counts toward the output limit whose
  cut-off rejects the whole day.

## Consequences

- A rebuild is one larger paid call a month: the note plus about 30 day summaries, roughly 40k
  input tokens, and a whole note of output. Opus at medium on every nightly job costs a few dollars
  more a month than Sonnet.
- A rebuild can still word an item worse than before; pinned items are safe, everything else
  depends on him glancing at the diff or undoing it.
- "Is this still going?" is a new kind of question she asks unprompted; with many open threads it
  can feel like nagging, so it is at most one a day.
- Thread state and last-evidence day are new fields on every thread item, and the memory page, the
  nightly prompt and the gates change with them. Ended items stay in every later version, so a
  version grows with what ended; only live items count toward the caps.
- A thread whose only dates were already history when written never closes by date; it waits for a
  record, a rebuild or the 21-day question.
- An item with no record behind it at all (first-run setup, `remember`, the old profile, the
  Claude import) cannot be told apart by code from one he set, so it keeps the kind set; the
  imported lines that are only inference leave through the first rebuild, which he reviews.
- An item about money can be standing (how he spends) or a thread (a position he holds); the model
  sorts it, and a wrong sort shows only as an item that lingers or leaves too early.
