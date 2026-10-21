# ADR 0331 — The writer says, in its own run, what each new key of his attention served

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** 0315

## Context

- A stretch's attention is summed by key (ADR 0330): an app with its page, document or window
  title, or a phone app. A key is not a project. Which project, person or show a key served is a
  judgement; in Jarvis code computes facts and a model judges, and a table of apps and sites is a
  rule per case, which he ruled out (2026-10-16).
- Keys recur: a key judged once is still right the next day, and the same thread keeps its name.
  His answers win over any model's (ADR 0302).
- ADR 0315 asked a separate model before each run, shown one line per key: its minutes, typing
  and first and last time that day, the known threads, and the day's Claude Code projects without
  their times. In the 10-19 replay of 2026-10-21 (Haiku 5.5, 66 keys) it put the 一起看 Claude
  Code window, Netlify Drop and four netlify.app pages titled 放映厅 under one thread, 放映厅, as
  rest. He was building it (04:13Z). Nothing it was shown said that the 一起看 session ran while
  the pages were open.
- The writer is already shown the whole day in time order: what was in front of him at the Mac
  each quarter hour with his typing, the Claude Code sessions with their times and recaps, his
  words with her, and the rest of the day's facts (ADR 0302).
- His rule, 2026-10-21 06:19Z: when a model judges wrong, change what it is given, never add a
  rule or an example to its prompt. 06:25Z: the separate model "knows too little".
- The writer runs on Opus, only at night and when he asks (ADR 0311): $4 in and $20 out per
  million tokens (data/pricing.json). One told key is about 20 tokens of answer.

## Decision

Have the writer say, in the same answer as the day's spans, what each new key of the day served.
Keep each answer per key in memory.db, and sum keys by it whenever a stretch is read.

- **What it is shown.** The day as before, then the threads told so far, each with its category
  and a few of its keys, then the keys of the waking window nobody has told yet that held him at
  least a minute, at most 150, longest first, by labels the program gives. A day with no such key
  shows neither.
- **What it says.** For each key, a thread's name and its category, or that the key serves
  whatever is around it.
- **Gated apart.** Every asked key must be told exactly once, with a name of at most 24
  characters and a category from the lane's list, or with neither. A refused telling asks the run
  again, as refused spans do. Each part is kept from the answer it passed in; a telling still
  refused leaves its keys untold until the next run, and the spans land.
- **Asked once.** A key once told is not asked again. **His word wins**: answers are appended,
  never rewritten, and his answer outranks any model's, whenever either was given.

## Alternatives rejected

- **A separate model call before the run (ADR 0315)** — shown one line per key, it told the
  放映厅 project rest on 10-19. Showing it the day in time order would repeat the writer's brief in
  a second call.
- **The separate call on Haiku** — an answer is kept until he corrects it, so a first answer
  that is wrong stays wrong; the 10-19 one was.
- **A line in the prompt on what a thing he builds is** — the model lacked the evidence, not a
  rule, and he ruled such lines out (2026-10-16, 2026-10-21).
- **Ask afresh each day** — the same window would be renamed from day to day, totals across days
  would split one project under two names, and the cost would repeat every night.
- **Tell keys when the page is read** — the page is drawn many times a day, and a model call there
  would add seconds and cost to each draw.

## Consequences

- The writer's answer grows by one line per new key: about $0.03 of Opus output for 70 keys, and
  page-title keys (ADR 0330) bring more new keys each day than domains did.
- A refused telling costs one more writer call.
- A key that first appears during the day shows as itself until the next run, at night or when
  he asks. A wrong first answer stays until he corrects it, and a site that later serves something
  else keeps its first thread.
- A later step that cuts a stretch by thread before the writer names it needs the day's new keys
  told first, which one call cannot do: such a step asks twice on a night with new keys.
