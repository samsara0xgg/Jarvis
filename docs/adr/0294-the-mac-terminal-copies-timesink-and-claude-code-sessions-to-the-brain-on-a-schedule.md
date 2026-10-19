# ADR 0294 — The Mac terminal copies TimeSink and Claude Code sessions to the brain on a schedule

**Status:** Accepted
**Date:** 2026-10-18
**Supersedes:** none

## Context

- Allen wants her to understand him from every source he can give her, raw data kept first (ADR 0259).
  His computer day is the largest source he has not given her: TimeSink records spans, window
  text read off the screen and a screenshot every few seconds, and his Claude Code sessions say
  what he worked on and how it ended (the log carries Claude Code's own recap of about 40% of
  them). Both live on the Mac, which the brain reaches only through a connected terminal (ADR 0170).
- Today the brain reads TimeSink on demand through the terminal (ADR 0023). When the Mac is asleep
  or off nothing is readable, and TimeSink itself keeps only about 18 days of screenshots, so what
  is not copied in time is gone for good. Claude Code sessions reach only the live notch board
  (ADR 0046), never the day record.
- TimeSink is a public app and Claude Code is not ours: everything that copies from them lives on
  the Jarvis side and only reads. TimeSink adds tables and columns between releases; a copier that
  names columns breaks silently the first time it does.
- Rows change at the source: an open span's end grows, a capture's last-seen time moves. A copy
  that stores every poll of a growing row stores it a dozen times an hour; one that stores only the
  first loses how it ended.
- A session log holds his prompts, the answers and tool output, including code. The owner wants
  the outcome of a session, not its content.
- Allen's rule for raw data: keep every field the source gives, with the source's own ids and times,
  and no aggregate of ours where the raw is available. Where keeping everything makes little real
  difference he accepts a trade (2026-10-18): fewer intermediate revisions of rows still changing.
  Screenshots he wants original, byte for byte.
- The brain's disk is an SD card (ADR 0170), and its databases must never run out of room
  (ADR 0238).

## Decision

A Mac terminal whose config turns it on copies TimeSink and its Claude Code sessions into the
brain's imports store (ADR 0259) on a schedule, and the brain keeps each shipment raw and
append-only.

- **Mirror, by schema.** Every TimeSink table except its migration ledger, every column, read
  from the schema each pass, whole rows, the database opened read-only. An entry's instant is its
  row's own time column in UTC, its kind the table, its text the OCR, title or label the row has.
- **Revisions.** A row's key ends in a short hash of its content, so each version kept is its own
  entry and readers take the newest per row. A row is shipped when it first appears; a changed row
  is shipped when it has settled (unchanged across two polls, or older than 48 hours) or when its
  last shipped version is an hour old, never in between. A session's record is shipped at most
  once per 30 minutes while it grows and always once its file has been quiet that long.
- **Sessions.** One record per session file of each configured account, counts and fields only:
  where, repo, branches, models, times, prompt, answer and tool-call counts, turn time, the latest
  cost state, every title, PR links and Claude Code's own recap text. No prompt, answer, tool
  input or tool output is ever copied.
- **Pictures.** Each screenshot is shipped once, original bytes, and kept whole-or-nothing in its
  own folder of the imports archive, with an entry that carries its digest, size and TimeSink's
  path. The brain answers the digest; the terminal moves on only when it matches.
- **Cursors on the Mac.** The terminal keeps its own cursors and per-row memory and advances them
  only after the brain has kept what they stand for, so a restart, a refusal or a lost answer sends
  again and never skips. The brain refuses at its disk floor and the terminal retries later.
- **Off by default,** and the brain's readers that were built for what he hands over leave these
  sources out unless asked for them by name.

## Alternatives rejected

- **Keep asking the terminal on demand** — the 18 days of screenshots and the Mac's sleep hours
  are already lost to it; a day record built from it has holes the brain cannot see.
- **A table per TimeSink table on the brain** — each TimeSink release that adds a column or table
  would need a migration here (its database has 27 tables today); one entries table with the row as JSON takes a new table with no change.
- **Ship every revision of a changing row** — an open span or an extended capture changes on
  every poll, 12 revisions an hour per row, for a difference nobody reads.
- **Ship session transcripts, or summarise them on the Mac** — the transcripts hold code and his
  words (the sessions of one day weigh gigabytes); a summary made on the Mac would be our
  aggregate in place of the raw fields.
- **Downscale screenshots** — would cut about 250 MB a day to about 60 MB; the owner chose originals.

## Consequences

- The first pass puts all of TimeSink's history on the Pi: about 0.3 GB of raw shipments, about
  0.35 GB of entries, and every screenshot TimeSink still holds (several GB), then about 250 MB
  of pictures a day with no age limit; the disk floor is the only brake until the SSD is fitted.
- A row that returns to an earlier content is not recorded again, so the newest by insertion can
  differ from the source for it. A row deleted at the source stays on the brain.
- A picture path already kept with other bytes is left as kept. A table without a rowid is
  skipped with a warning.
- TimeSink's settings table holds his account email; it is copied like every other row, and the
  entries (not the raw shipments) have key-shaped strings masked like all imports.
- Anything that wants these sources reads them with `newest`, by name; nothing reads them yet.
