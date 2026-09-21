# ADR 0024 — A task skill is instructions; the runtime owns the report's procedure

**Status:** Superseded-by-0025
**Date:** 2026-09-20

## Context

- Allen wants a written work report for a given day that he and GPT Live can
  read later. ADR 0020/0021/0022 made the day's raw material readable and ADR
  0023 persists a *current* state, but a current-state snapshot is not a day:
  it is bounded to the last two hours of screen text and today's spans, and it
  is deliberately short and claim-shaped.
- The material for one real day is large and uneven. A measured day on this
  machine holds 2189 app spans over 224 windows with no captures at all, and
  the next day holds 749 captures over 78 windows plus 22 commits and 10
  conversation records. A single prompt cannot be handed either extreme raw,
  and a report that silently reads "the latest few dozen rows" would claim
  coverage it does not have.
- The evidence lies in ways a prompt cannot fix. A screen shows "部署成功"
  while nothing was deployed; the same commit appears once per observed
  worktree; a commit written days ago is first seen today; an app is in front
  for 264 minutes without being worked in.
- `docs/plans/daily-loop-tools.md` lists "skill discovery/loading" as
  unbuilt, and the repository has no runtime skill mechanism at all —
  `.claude/skills/` is Claude Code's, not Jarvis's.
- The `briefing.revised` store already carries request deduplication, version
  checks and source-ref validation, and `get_briefing` already pages a saved
  report back.

## Decision

A task skill is a directory of instructions (`SKILL.md` plus
`references/*.md`) loaded from `jarvis.shared.skills` at import time, whose
frontmatter description is the tool description the decision model reads and
whose body is the system prompt of that task's own model call. Everything
executable stays in code: the daily work report is a runtime workflow behind
one flat tool, `daily_work_report`, that resolves the local calendar day,
gathers the day's evidence once under explicit caps, runs at most three
bounded model calls whose only tools are "give me these originals" and "report",
composes the saved text itself, and saves through the existing briefing
revision store.

The report's factual rules are enforced when the text is composed, not asked
for in the prompt: unknown refs are dropped and counted, a `completed` item
without a commit event or an Allen-authored record behind it is written as
unverified, a "next step" that cites no Allen-authored record becomes a
suggestion, and every cap that clipped material becomes an uncertainty.
Commits deduplicate by SHA across worktrees and carry their commit time
separately from their observation time. An existing report for that date and
zone is reused unless a regeneration was explicitly asked for; a failed model
call or a lost version check keeps the saved version and says so.

No delivery, scheduling, notification or todo write is part of this decision.

## Alternatives rejected

- **Extend `refresh_work_state` with a date** — its evidence is deliberately
  recency-shaped (2 h of captures, today's spans) and its record is one
  claim-shaped document whose identity is "latest". A day report needs whole-day
  aggregation, per-day identity, versions and reuse; sharing the record would
  make every dashboard poll overwrite yesterday.
- **Put the procedure in the skill markdown and let the model drive the daily
  tools** — the model would page `query_activity` itself over 2189 spans within a
  16 KB result cap, and the caps it silently hit would be invisible. The first
  live run needed 3 666 characters of material for a day whose raw rows exceed
  the cap by two orders of magnitude.
- **Ask the prompt to distinguish browsed from completed** — the first live run
  over real data proves the model will call a demo banner or a window title
  "completed"; the same run's report only stayed honest because the composer
  downgraded it. A rule that is only in a prompt is not a rule.
- **Write a new `daily_report.revised` event type** — `briefing.revised`
  already keys on (local_date, IANA zone) with versions, request deduplication
  and source-ref validation, and `get_briefing` already reads it back. A second
  store would need its own reader and would split "the report for that day"
  across two tables.
- **One model call, forced to report** — measured: DeepSeek asked for the
  originals behind ten keys on the first round of the first real run, and once
  asked again instead of reporting. A single forced call either loses the OCR
  detail or fails the run on a normal model behaviour.

## Consequences

Each generated report costs one or two `fast`-preset calls over a few thousand
characters; repeats cost nothing until a regeneration is asked for. The report
is only as good as the collectors: a day without screen captures yields a
title-level report, and the saved text says so rather than reading as thin
work. The saved layout is code, so changing the report's shape is a code change
and old versions keep the old shape. A model that cannot produce a valid report
leaves the day without one rather than with an empty one. Skills are static and
in-repo: adding one is a commit, and there is no runtime discovery, no
user-editable instruction file and no path by which read material becomes an
instruction.
