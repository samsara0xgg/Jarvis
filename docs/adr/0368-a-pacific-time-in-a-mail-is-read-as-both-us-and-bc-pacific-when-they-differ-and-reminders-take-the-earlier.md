# ADR 0368 — A Pacific time in a mail is read as both US and BC Pacific when they differ, and reminders take the earlier

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** none

## Context

- The job-mail reader maps "PST", "PDT", "PT" and "Pacific" to America/Vancouver
  (`decision/job_mail.py`).
- An interview time read from job mail becomes a thing in his ledger by itself, with two rings:
  the evening before and shortly before the start (ADR 0186, 0299).
- From 2026-11-01, BC stays at UTC−7 all year, while US Pacific goes back to UTC−8. The Pi's and
  the Mac's tzdata 2026c know both, checked on 2026-10-20.
  - A US employer's "10 am PST" is then 11:00 his time. It is stored as 10:00, so he arrives an
    hour early.
- "Pacific" is used loosely:
  - "PST" is written in summer for PDT;
  - a BC employer can write "PT" for BC's own time.
- Arriving an hour early costs him an hour. Arriving an hour late can cost him the job.
- On 2026-10-21 Allen approved the plan's line 「PST/PT 按美国太平洋时区换算成你的本地时间」
  (「就按你说的去吧」). Read as US Pacific only, a BC employer's "10 am PT" in winter would become
  11:00, and he would be an hour late.

## Decision

Read a Pacific time in a mail as both US Pacific and BC Pacific whenever the two differ on that
date, show him both, and set reminders from the earlier.

- When the two readings agree, as in US summer time, there is one time, as now.
- When they differ:
  - the interview line names both, with what the mail said ("10:00 or 11:00: the mail says
    10 am PT, BC or US Pacific");
  - the interview's thing and its rings start from the earlier, and the thing names both.
- Other zone labels a mail names (ET, CT, MT, UTC, an offset) are converted by their zone. A bare
  time is his own, as now.
- One reader of zone labels serves every mail reader that converts a zone a mail names.

## Alternatives rejected

- **US Pacific always, as the plan worded it.** A BC employer's "10 am PT" in winter becomes
  11:00, and he arrives an hour late.
- **BC always, as today.** A US employer's "10 am PST" in winter shows as 10:00 when it is 11:00,
  and nothing tells him which hour is right.
- **A model guesses which Pacific the sender meant.** The mail rarely says. A sender's domain or
  signature gives a guess either way, while showing both readings costs one line.

## Consequences

- He sees two times for any winter Pacific time, even when the sender meant one plainly.
- The ambiguity ends only if US Pacific also stays at UTC−7.
