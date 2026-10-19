# ADR 0299 — His things are one ledger in the event log, and every reader of them reads only it

**Status:** Accepted
**Date:** 2026-10-19
**Supersedes:** the clauses of ADR 0245 that keep memos, reminders and appointments as three stores behind `keep` and `kept` (its facts stand); ADR 0248's read-only `my_schedule`, which the ledger's tool replaces; the clauses of ADR 0274 that fold commitments from `commitment.*` and arm their interim reminders (its mail poller stands and writes items); the clause of ADR 0186 that gives an interview its own two reminders and Outlook event; the clauses of ADR 0288 that name plan sources `commitment:<id>`, `todo` and `his` and read the draft's inputs from commitments, To Do and two memory sections; the clause of ADR 0291 whose done-check sees only screen, words and commits; and the clause of ADR 0258 that keeps 承诺和待办 as a core-memory section. Everything else in those ADRs stands.

## Context

- His things live in six stores and every reader reads a subset (code on main a8ebde17): memos
  (`memo.captured`, read only by `kept`), reminders (ADR 0179), commitments from mail and his
  words (ADR 0274), the day plan's items (ADR 0288), the Outlook calendar and Microsoft To Do.
  Core memory's 承诺和待办 holds more. `my_schedule` reads neither memos nor the plan; the 04:00
  draft reads neither memos nor reminders; a commitment never reaches `kept(kind=memo)`.
- What that cost, Pi log 2026-10-13 to 10-18: 10-17 he said 明天下午和朋友打羽毛球, it became a
  memo, and on 10-18 she said his afternoon was free and the draft left it out. 10-18 00:39 she
  set an 11:00 reminder for an 11:30 pickup; the 04:00 draft never saw it and planned a bus
  to breakfast instead.
- No store can say a thing is finished. Commitments have only `confirmed` and `dropped`, memos
  nothing, a plan item's `done` lives in one day's plan. 10-18 a "Purchase confirmed" mail came
  at 00:17; the draft still made paying that invoice a fixed item and she asked him 4 times.
- The mail poller's only shape is an appointment: a card statement became a midnight appointment,
  and a margin call of 2026-09-15 and a marketing event were read in as his.
- His decisions: 2026-10-16 (doc 「Jarvis 工具清单与结构」) her schedule is her own copy, written
  into Outlook when connected; 「要修 / 要买」 things are untimed items of the same schedule; one
  tool per object. 2026-10-19 he approved this ledger (doc 「「你的事」总账：设计」), its states
  (to do, done, paid, cancelled, moved), and said progress within a thing is a later line.
- The brain's Outlook connection is absent on the Pi most days (ADR 0230), and an interview's only
  schedule copy is the Outlook event ADR 0186 writes.
- Every installed phone build draws day-line kind `event` and posts
  `POST /inherent/commitments/{hex}`; the desktop Today page ticks To Do through ADR 0051's
  checkbox.

## Decision

Keep every thing he has to go to or do as one item in one ledger folded from the event log, with
one closed set of states and the evidence for every change, and have every reader of his things
read only that ledger.

- **An item** is his words for it, whether he goes to it or does it, and when: a start (and end),
  a due time, a day, or nothing; optionally weekly up to a last date with skipped dates. It may
  carry a place or link, ring times, `private`, a life line it belongs to (驾照, 找工作, Jarvis;
  the list is shared with the day's spans), and where it came from (a mail, a turn, a picture, an
  Outlook event, a To Do task, the plan).
- **States** are open, done, paid (done, for a thing that was money) and cancelled; a move
  changes the time of an open item and keeps the time it had. A weekly item is done, paid,
  cancelled or moved per date; the series is cancelled as a whole. An item is `confirmed` when
  he said it or said it is his; what a mail or an unsure sentence made starts unconfirmed.
- **Every write says who and why**: her tool, the post-turn pass (ADR 0300), the mail poller, the
  mirror, the plan, the nightly review, a button; with the words, mail, picture note or record
  that proved it. Nothing is deleted; a wrong item is cancelled with its reason.
- **One write path in L2** that all of these call; it refuses a write that cannot be read back
  (a time without an offset, a state outside the set, a change to a closed item other than
  reopening it).
- **Her one tool** reads the ledger and adds, moves, closes, confirms and cancels items; it
  replaces the memo, reminder and appointment parts of `keep` and `kept`, and `my_schedule`.
  Facts about him leave for their own tool; the bus pin keeps its own ring.
- **Rings** stay ADR 0179 reminders, armed and cancelled by the ledger for an item's ring times.
  An item with a start from mail gets the ring `before_min` before it that ADR 0274 armed; an
  interview gets the two of ADR 0186. A private item's ring says only its time.
- **Mail** (ADR 0274's poller, unchanged in how it reads) is asked against the open items and may
  propose a new item to go to or do, a bill with its due date included, a move, done, paid or
  cancelled. A time already past when the mail is read is never a new item.
- **Outlook and To Do are mirrored in**: when the connection answers, events and tasks are read
  into items keyed by their own ids, their later changes follow, a task completed there is done
  here. The To Do checkbox writes both. Confirmed timed items are written into Outlook when it is
  connected, last; unconfirmed and untimed items never.
- **The plan orders the ledger**: a plan item that stands for an item names it (`item:<id>`) and
  its done is the item's state; meals, legs, routines and time for a goal stay the plan's own.
  The 04:00 draft reads the ledger's items of its horizon, the open ones past their time, and
  the untimed ones in the program's order (due first, then oldest).
- **The progress check** (ADR 0291) also gets where his phone placed him, by kept name or else by
  an open item's place, and his rides; its done marks the item. What matches no item goes to the
  decide step as what he did instead of the plan, and to the day's spans, which own it.
- **Readers**: her tool, the state block's next line and its count of unconfirmed items, the
  ledger block's today-so-far (ADR 0201), the draft, the opening's unconfirmed list, the plan
  watcher and its progress check, and the day line (still kind `event`; the commitment route
  confirms or cancels an item).
- **Carried in once**: every memo (exact repeats merged), pending reminder, live commitment,
  interview reminder and 承诺和待办 item becomes an item with its old id as its source. A mail item
  he never confirmed is cancelled when its time has passed and asked again under the new question
  when it has not. 承诺和待办 leaves core memory and its 3,000-character cap goes to 生活里的几件事;
  the night no longer writes to-dos.

## Alternatives rejected

- **Give every reader all six stores** — each of the 10-17/18 misses was a reader that skipped a
  store; a seventh reader repeats the mistake, and done or paid would be added six times over.
- **Make Outlook and To Do the store** — absent on the Pi most days (ADR 0230), no unconfirmed
  or evidence state, a wrong mail extraction lands in his real calendar (ADR 0274's reason), and
  a public build without Microsoft has no schedule.
- **A table beside the log** — a second store to keep in step with the brain's state (ADR
  0179's reason), and it loses the moves he asked to look back on (路考 from February to 10-22).
- **Keep memos as notes apart from to-dos** — he decided on 10-16 they are untimed to-dos; 24 of
  them were read by nothing but `kept`.
- **A progress fraction on each item** — he said on 10-19 that measuring progress is a separate
  line; a big thing is its smaller items, and minutes left today stay the plan's estimate.

## Consequences

- One fold now carries every thing he has; each reader that needs a day or a horizon folds the
  whole ledger, as the commitments and plan folds already do.
- A day line item and a route id keep the commitment's shape, so the phone needs no update; a
  later build can say done and paid.
- The interview Outlook event of ADR 0186 becomes the general write-back, which waits for the
  last step; until then an interview is in the ledger and rings, but is not in Outlook.
- Core memory's ceiling and caps are unchanged in total; the memory page shows ten sections.
- Asking the unconfirmed mail items again costs one `haiku-bg` call each, once.
