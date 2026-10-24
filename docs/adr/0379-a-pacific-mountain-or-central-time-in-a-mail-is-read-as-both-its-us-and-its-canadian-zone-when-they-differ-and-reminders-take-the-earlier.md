# ADR 0379 — A Pacific, Mountain or Central time in a mail is read as both its US and its Canadian zone when they differ, and reminders take the earlier

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** ADR 0368

## Context

- Job mail names an interview's time with a zone label: PT, MT, CT, ET, UTC or an offset
  (`decision/job_mail.py`). The time becomes a thing in his ledger by itself, with two rings:
  the evening before and shortly before the start (ADR 0186, 0299).
- In 2026 three Canadian provinces stopped changing their clocks, and their US namesakes did not:
  - BC stays at UTC−7 from 2026-11-01, while US Pacific goes back to UTC−8 (tzdata 2026c);
  - Alberta stays at UTC−6 since 2026-06-18, while US Mountain goes back to UTC−7 (2026c);
  - Manitoba stays at UTC−5 from 2026-10-31, while US Central goes back to UTC−6 (2026e).
  - Eastern did not split: Toronto and New York keep the same rules.
- Labels are written loosely:
  - "PST" is written in summer for PDT;
  - a BC employer can write "PT" for BC's own time, or "MST", tzdata's name for BC's winter time;
  - an Alberta or Manitoba employer can write "MT" or "CT" for its own time.
- ADR 0368 read Pacific both ways and every other label by one zone: MT as Edmonton, CT as
  Winnipeg. On 2026-11-12, a US or BC employer's "10 am MST" is 10:00 his time; read as Edmonton,
  it shows as 09:00. A US employer's "10 am CST" is 09:00 his time; read as Winnipeg, it shows as
  08:00.
- Arriving an hour early costs him an hour. Arriving an hour late can cost him the job.
- On 2026-10-21 Allen approved reading a winter Pacific time both ways (ADR 0368,
  「就按你说的去吧」). Alberta's and Manitoba's changes were found the same day, while that
  decision was being built.
- On 2026-10-21 the Pi and the Mac had tzdata 2026c, which does not have Manitoba's change.

## Decision

Read a Pacific, Mountain or Central time in a mail as both its US zone and its Canadian zone
whenever the two differ on that date, show him both, and set reminders from the earlier.

- Pacific is America/Los_Angeles and America/Vancouver, Mountain is America/Denver and
  America/Edmonton, Central is America/Chicago and America/Winnipeg.
- When the two readings agree, there is one time.
- When they differ:
  - the interview line names both, with what the mail said and which two zones it can mean
    ("10:00 or 11:00 (the mail says 10 am PT, BC or US Pacific)");
  - the interview's thing and its rings start from the earlier, and the thing names both.
- Eastern, UTC, GMT and an offset are converted by their one zone. An offset beats a name in the
  same sentence. A bare time is his own.
- One reader of zone labels serves every mail reader that converts a zone a mail names.

## Alternatives rejected

- **Pacific both ways, every other label by one Canadian zone (ADR 0368).** From 2026-11-01 a US
  or BC employer's "10 am MST" shows an hour early, and a US employer's "10 am CST" too.
- **US zones only.** On 2026-11-12, an Alberta employer's "10 am MT" is 09:00 his time and shows
  as 10:00, and a Manitoba employer's "10 am CT" is 08:00 and shows as 09:00: he arrives an hour
  late.
- **A model guesses the sender's zone.** The mail rarely says. A sender's domain or signature
  gives a guess either way, while showing both readings costs one line.

## Consequences

- He sees two times for any winter Pacific, Mountain or Central time, even when the sender meant
  one plainly.
- The readings are only as current as the brain's tzdata: with data older than 2026e a winter
  Central time reads one way, and with data older than 2026c Pacific and Mountain do too.
