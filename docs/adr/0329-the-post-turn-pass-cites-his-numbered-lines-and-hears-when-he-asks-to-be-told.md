# ADR 0329 — The post-turn pass cites his numbered lines and hears when he asks to be told

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** the clauses of ADR 0300 that make each action quote his words or the note and drop an action whose quote is not in that record's words or notes. Everything else in ADR 0300 and ADR 0328 stands.

## Context

- ADR 0300's pass has the model copy a sentence of his, or of a picture's note, as each action's
  evidence; the program drops the action when the copy is not in his words.
- The first backfill on the Pi (2026-10-20) dropped the 10-18 00:41 screenshot of his passed ICBC
  written test because Haiku wrote 「12:43」 as 「12点43」; the replay before it had copied the
  same note right. Five more drops quoted the message before his. Both are copying going wrong,
  not judgment: the evidence is a fact the program can copy itself.
- A thing the pass writes never rings. Most of the time that is right: an 11 点去吃 Fujiya said in
  passing should not start a reminder, and unprompted lines are off since 2026-10-19. When he says
  提醒我 and she did not set it in the turn, nothing rings either.
- The pass never marks a thing private, so a ring for a therapy appointment it wrote would say the
  title aloud, where one her tool keeps says only the time (ADR 0179).
- In the replay of 10-13..10-19 with this change, a 「yes」 at 10-18 00:39 answered her offer of a
  reminder at 11:00, which she then set; the pass read the same yes as a ring 30 minutes before
  the 11:30 thing, the same minute.
- 2026-10-20 the line-number change was drafted for him; 2026-10-21 the ring and private change
  joined it, and he said 「动手」 to both.

## Decision

The program numbers the sentences of his message and of his pictures' notes; the model answers
with line numbers and copies nothing. A new thing also says whether he asked to be told and
whether it is private.

- **Lines**: his message is cut at line breaks and at sentence ends (。！？；!?; and a full stop
  before a space), one sentence a line, `m1`, `m2`…; a long sentence with no stop stays one line.
  The notes of his pictures are cut the same way, `p1`, `p2`… across pictures. Her answer and the
  messages before his are shown unnumbered.
- **What it cites**: each action names the lines that make it so, at most six. The program copies
  those lines in their order as the evidence, joined by a space, with 「…」 where a line is
  skipped, cut at the evidence length; the source is `picture` when a `p` line is among them.
  An action that cites no line, or a label that is not one of his lines, is dropped with that
  reason.
- **When he asks to be told** (`remind`, new things only): absent when he did not ask; `[]` when he
  asked without saying how early; whole minutes before otherwise, at most a week. Asked without
  minutes, a thing rings as one her tool keeps would (a thing to go to
  `commitments.remind_before_min` ahead, one to do at its time). Minutes before a whole day name
  no time he meant, so a day or a date-only due rings the evening before. A thing he was unsure
  about, or one with no time, does not ring. A ring due in the minute of a reminder she set in that
  turn (between his record and her answer, not cancelled) is not made: he said yes to her offer
  and she set it already.
- **Private** (new things only): counselling, therapy, mental health, legal and medical matters;
  a ring for it says only its time.

## Alternatives rejected

- **Match quotes loosely** (normalise digits and punctuation) — every new spelling the model finds
  is another rule, and a loose match can land on the message before his.
- **Default rings on every thing the pass writes** — a thing said in passing would start talking
  to him, which is the unprompted line he turned off.
- **Leave rings to her tool** — she misses 提醒我 in some turns; that miss is what the pass is for.

## Consequences

- No action is lost to a copy that differs from his words, and none can rest on the message before
  his, since it has no number.
- The evidence is whole sentences, a little longer than a quote; a thing backed by a long spoken
  sentence carries all of it, up to the evidence length.
- The extractor version changes, so a replay compares across versions.
- A ring the pass sets can be wrong when it misreads 提醒我; it shows in the state block and in a
  schedule read with the thing it rings for, where it can be removed.
