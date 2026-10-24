# ADR 0315 — A model says once what each new key of his attention served, and his word wins

**Status:** Superseded-by-0393
**Date:** 2026-10-21
**Supersedes:** none

## Context

- ADR 0314 tells each stretch by key: an app with its site, document or window title, or a phone
  app. A key is not a project. On 10-19 his Jarvis work was spread over Claude Code windows,
  GitHub and an Electron app. Untitled Claude desktop windows served Jarvis, Typlus, 一起看 and
  his sister's phone plan at different hours. WeChat is a channel to people, not a task. Read by
  key, a stretch of one project looks like many small things.
- Which project a window served is a judgement. In Jarvis code computes facts and a model judges.
  A table of apps and sites is a rule per case, and he has ruled those out (2026-10-16).
- Keys recur. The same project windows, sites and apps come back day after day, so a key judged
  once is still right the next day, and the same thread keeps the same name.
- The writer runs only at night and when he asks (ADR 0311). The page is drawn many times a day
  and calls no model.
- His answers win over the writer (ADR 0302). He approved the two-level timeline, whose second
  level lists projects, on 2026-10-21 00:28Z.

## Decision

Before a run of the doing lane, ask a model about each key of that day that nobody has told yet.
Keep its answer per key in memory.db, and sum keys by it whenever a stretch is read.

- **What it says.** For each key, a thread's name and its category. Or it says the key serves
  whatever is around it, as an untitled window or a login page does.
- **Asked once.** Only keys that held him at least a minute that day are asked, by labels the
  program gives. A key once told is not asked again.
- **His word wins.** Answers are appended, never rewritten. A key reads its newest answer, and
  his answer outranks any model's, whenever either was given.
- **Gated.** Every asked key must be told exactly once, with a name of at most 24 characters and
  a category from the lane's list, or with neither. A refused answer is asked again once. If it
  fails again, nothing is kept and the run goes on.
- **Off until prompted.** No key is told unless `doing.threads_prompt` is set. The model is
  `doing.threads_preset`, else the writer's.

## Alternatives rejected

- **A table of apps and sites** — on 10-19 one app, Claude desktop, served four threads at
  different hours. A table needs a new entry for each case, which is the patching he forbade.
- **The writer tells keys inside its run** — the writer's answer is per day, so the same key could
  get a different name each night. The rows a later step cuts by thread are needed before the
  writer sees them.
- **Ask afresh each day** — the same window would be renamed from day to day. Totals across days
  would split one project under two names, and the cost would repeat every night.
- **Tell keys when the page is read** — the page is drawn many times a day, and a model call there
  would add seconds and cost to each draw.

## Consequences

- A wrong first answer stays until he corrects it. A site that later serves something else keeps
  its first thread.
- A key that first appears during the day shows as itself until the next run, at night or when
  he asks.
- The answer is only as good as the titles. A generic title is told to go with its surroundings,
  so it follows the thread around it even when that is wrong.
- A day with new keys costs one more model call before its run.
