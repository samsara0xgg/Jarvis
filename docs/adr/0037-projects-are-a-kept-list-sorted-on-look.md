# ADR 0037 — Projects are a kept list; activity is sorted into them when someone looks

**Status:** Accepted
**Date:** 2026-09-23
**Supersedes:** none

## Context

- Allen, 2026-09-23: Jarvis, not TimeSink, tracks his projects — for each
  one its name, how long he spent on it, and what happened recently (commits,
  investigations) — as visible state on the Resonance dashboard. Todos and
  reminders stay in Microsoft (ADR 0036); when Jarvis speaks about projects
  is a later decision. The spec already lists current project state on the
  dashboard (§18.5) and Drift Watch as a projection; ADR 0023 left project
  identification out.
- Measured on 2026-09-22 (local day, 10.5 h of foreground spans): 40 s
  carried a file path. The ChatGPT and Claude apps put the conversation title
  in `document`, Ghostty puts the Claude Code session name in the title,
  Chrome puts the page title. About 5.4 h of that day was job search (co-op
  postings, resumes edited in SharePoint, ChatGPT conversations about job
  descriptions), none of it in a repository.
- A Claude Code session's working directory does not name its project: the
  session "timesink code review" ran entirely in `~/Projects/jarvis`
  (418 of 418 `cwd` records).
- Seven days held 14,435 spans but only 1,258 distinct (app, domain, label)
  activities; the label, not the span, is the unit that carries meaning.
- ADR 0023 rejected background model loops: they pay while nothing changes.
  Observers never call a model (spec §3.4.1).
- TimeSink's categories are activity types with productivity scores
  (软件开发, 学习), orthogonal to projects.
- A model cannot invent entity ids (I6): project ids come from trusted config.

## Decision

Keep Allen's projects as a hand-edited list in config (`projects`: id, name,
repos, hints) and sort TimeSink activities into it with the configured
preset, only when the dashboard asks, only for activities that have no
answer yet. An activity is one (app bundle, domain, label), where the label
is the span's document, else its title, else its domain, else the app name.
The model sees the
labels and the project hints, answers through one forced tool, and may
answer "none"; each batch's answer is persisted as
`project.activity_classified`, tagged with the catalog's fingerprint, and an
answer counts only under the catalog it was made for, so editing the list
sorts everything again once. Reading never calls a model. The project view
is derived at read time and never stored: per project, foreground time per
local day for the last seven days, when it was last seen, the commits on any
local branch of its repos in that window, and its most recent activities;
time sorted to none and time not yet sorted are separate totals, never
spread over projects. The dashboard's demo conversation tile becomes the
projects tile. This decision adds no speaking, no per-project todos and no
drift signal.

## Alternatives rejected

- **A project is a watched repository** — 40 s of 2026-09-22's 10.5 h had a
  path, and that day's largest project (about 5.4 h of job search) has no
  repository.
- **Per-project path or keyword rules** — the signal is free-text titles
  ("分析公司背调岗位和JD", "Yilun Shi Resume sku.docx") that a rule list
  written in advance does not anticipate; 1,258 distinct labels in a week.
- **Attribute terminal time by the Claude Code session's working directory**
  — the "timesink code review" session ran in the Jarvis checkout for all
  418 of its records; the join would be confidently wrong.
- **Build projects into TimeSink** — Allen chose Jarvis on 2026-09-23;
  TimeSink cannot see commits, agent sessions or To Do, so the combined view
  would still be assembled here.
- **Sort spans, or re-sort every activity on each look** — 14,435 spans
  against 1,258 labels in a week; re-sorting an unchanged label buys the
  same answer again.
- **Sort in the background on a timer** — ADR 0023's reason: it pays while
  nothing changes and while Allen sleeps.

## Consequences

The first look after a catalog edit sorts about 1.3k labels in several
batched calls and takes minutes; until then that time shows as unsorted.
There is no correction path: a wrong answer stands until the catalog
changes, and an ambiguous session name ("9.22 2!!") stays "none". The same
conversation in two apps is two activities, and a site whose page titles
change splits into many. Every read scans seven days of spans, runs `git
log` in each project repository and folds every classification event; the
dashboard polls every 60 s, so reads run off the loop thread. Window and
conversation titles go to the configured provider, as they already do for
the daily report. Project context in conversation, per-project todos, drift
and the morning brief remain outside this decision.
