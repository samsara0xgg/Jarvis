---
title: What's left today, and what you did yesterday
description: Ask Jarvis what is still ahead today or what you got done yesterday, and every line of the answer can be traced back to a real record.
---

Jarvis answers "what's left today?" from your calendar and to-dos, and "what did I do yesterday?" from a report she writes each morning out of your own records.

## See what's left today

The Dashboard has a Today block with today's calendar events, every open to-do and, if you set a location, the weather. It reads your Outlook calendar and Microsoft To Do directly, with no language model in between, so it is quick and says exactly what those apps say. Tick a to-do there and it is checked off in Microsoft To Do; that click is the only thing the block writes.

You can also just ask. In conversation she has the same calendar and to-do tools ready from the start.

<!-- shot: Dashboard Today block, mid-afternoon: two remaining calendar events, three open to-dos (one due tonight), weather strip; sample data -->

## Get yesterday as a morning brief

Once a day, early in the morning (05:00 by default), the daemon writes the report for the day before. It reads that day's conversation records and git commits, the screen activity if you have switched that on, and your calendar and to-dos. The first time you open the Dashboard that morning, a Morning brief card gives the day's main line. Open it for what you did, what is still open, the next steps you named and what was decided. Ask her by voice about yesterday and she answers from the same saved report.

It runs on a schedule, never on request. When it ran on demand in testing, a spoken question was followed by about a minute of silence.

<!-- shot: Morning brief page, sample day: "Yesterday" rows tagged Done / In progress / Unchecked, "Still open" below; sample data -->

## Trust what it calls finished

The report is built not to overstate. A model drafts it, but "done" is only a claim. The program throws out claims with no citation, and a commit counts only as "committed": "merged" needs the commit to be on main, and "deployed" cannot rest on a commit alone. A separate model pass then reads the cited originals and rules on each remaining claim. When a coding agent says it finished, the report says so by the agent's own account, not yet verified.

A claim that could not be checked says that too. The brief shows each verdict as a tag: Done, Partly done, In progress, Unchecked, Discussed or Browsed.

## Follow a line back to its record

In the saved report, every work item ends with numbered citations, and a Sources list at the foot says what each number is: a conversation record, a git commit, a captured window. She can pull up the original behind any of them. The brief page is the readable cut, with the citations stripped; the tags carry the verdict and the full report carries the evidence.

## What she reads, and what stays off

Reading another app's data is opt-in. Screen activity comes from TimeSink, a separate recorder, and stays off until you turn it on. Codex conversations are off by default, and the report does not read Claude Code sessions. Calendar and to-dos appear only if a Microsoft account is connected.

An OpenAI model writes the report, so that day's material, screen text included when it is on, is sent there. The spec lists exactly what goes where.

Design notes: [written once a day](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0101-the-daily-report-is-written-once-a-day-never-on-request.md), [claims checked first](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0028-daily-report-checks-claims-before-summary.md).
