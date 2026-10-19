# ADR 0300 — What he says and shows in a turn is read into the ledger by one pass after it

**Status:** Accepted
**Date:** 2026-10-19
**Supersedes:** none

## Context

- His things reach the ledger (ADR 0299) from his words only when the conversation model calls a
  tool in the turn. Pi log 2026-10-13 to 10-18: the `unbacked` guard caught her saying she had
  kept something she had not 13 times in 5 days; 10-18 00:41 he sent a picture saying the
  written test was passed and the road test moved to 10-22, she praised him, and nothing was
  kept. A guard only catches a claim; that turn made none.
- A turn's model request already carries the latency he notices (ADR 0272, 0276); anything added
  before her answer is added to every turn.
- What runs at a turn's end today: the picture notes (ADR 0223) queue there, wait for his next
  words or 600 s, and rescan the last day at start for pictures with no note. Live-voice turns
  write their records without passing the turn driver, so only his records cover every turn.
- The mail poller (ADR 0274) is the pattern that works: one `haiku-bg` call per item, untrusted
  JSON checked field by field, a marker row per item with what the model was given and said,
  errors retried three times.
- What he says can be needed within hours: an 11:30 pickup said at 00:39 must reach the 04:00
  draft; 已经付了 said at 09:25 must stop her asking at once.
- A wrong "done" hides a thing he still has to do; a wrong new item nags. A quote can be checked
  against his words; a judgment cannot.
- Haiku 5.5 costs $0.10 in and $0.50 out per million tokens (data/pricing.json); a record with its
  context is about 5k tokens in and 1k out, about $0.001.
- His rules: Haiku by default, Sonnet only with his OK (2026-10-16); no prompt lines patched for a
  case (2026-10-16); judgment by the model, facts by the program (2026-10-11). 2026-10-19 he
  agreed to a pass after every turn on Haiku.
- The day's spans (lifelog layer two) want what he says he did or is doing as a clue; 2026-10-19
  that thread agreed to take it from this pass rather than read his words a second time.

## Decision

After each record of his is answered, one background `haiku-bg` call reads it in context and
returns what it changes in the ledger and what he says he did; the program keeps only what a
verbatim quote backs and writes it through the ledger's own write path.

- **When**: once per record of his (typed, spoken, live voice, phone), after her answer; a record
  with pictures waits until their notes exist or their window has passed. A marker row per record
  says pending, none, written or error; at start and every ten minutes the records of the last
  day with no marker are read, and on first start those of the last seven days.
- **What it reads**: that record and the few before it, her answer, the picture notes, the open
  items, today's plan, what her tools already wrote in that turn, and the local time and zone.
- **What it returns** is two separate kinds. Ledger actions: a new item, a move, done, paid,
  cancelled, confirmed; each one names its open item when it touches one, and quotes his words
  or the note. Stated doings: what he says he did or is doing, quoted, with when resolved
  against the record's own time (or none), where and who only when he said them, and past or
  ongoing; plans for later are ledger actions, never doings.
- **What the program checks**: a quote that is not in that record's words or notes drops its
  action; a time that does not read drops it; a new item already past when said is not new; a
  close or move must name an open item; a write her tool already made is not made again. An
  action he was unsure about is written unconfirmed; one he stated is confirmed.
- **What it writes**: ledger actions through ADR 0299's write path, by the pass, with the record
  id and quote as evidence. Stated doings as their own events, one set per record and extractor
  version, the latest per record standing; what they mean for the day's spans is that line's.
- **Her tool stays** for what he asks her to keep in so many words; the pass is what catches the
  rest, and the next turn's state block shows what it wrote.
- **The instructions are principles** (his own things only, not others', not jokes or
  hypotheticals; unsure means unconfirmed), never examples taken from his days.
- Every call is recorded with its input and raw answer, costed as `cost.recorded`, and can be
  replayed over a past stretch of the log.

## Alternatives rejected

- **Her in-turn tool and a stronger guard** — the guard fired 13 times in 5 days and still missed
  the 10-18 picture, which made no claim to catch.
- **One pass a night** — the 00:39 pickup was needed at 04:00 and 已经付了 at 09:25 was needed at
  once; batching saves nothing at $0.001 a record.
- **Inside the turn, before her answer** — every turn would wait for a second model call.
- **Sonnet** — not without his OK; the task is narrow and every action is quote-checked, so Haiku
  goes first and a replay of 10-13 to 10-19 says whether it misses too much.
- **Rules over his words** — 明天下午和朋友打羽毛球 has no clock time, and whether a sentence is
  his, someone else's or a joke is judgment.
- **A second pass for the day's spans** — two calls over the same words cost twice and can
  disagree about one sentence.

## Consequences

- About $0.001 a record, three cents for thirty; one more request to Anthropic per record, carrying
  only his words, her answer and the picture notes, to the same provider as the conversation.
- An item lands seconds after her answer, and a picture's up to ten minutes later; a question in
  that gap reads the ledger without it.
- A wrong action is written with its quote, so it can be seen and cancelled; how often it is
  wrong is measured by replay, not assumed.
- The first start reads a week of his records back once, at the same cost per record.
