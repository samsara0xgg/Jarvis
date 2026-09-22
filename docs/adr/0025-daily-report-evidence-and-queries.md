# ADR 0025 — The daily report reads Git and Codex itself and may search the day

**Status:** Superseded-by-0027
**Date:** 2026-09-21
**Supersedes:** 0024

## Context

- ADR 0024 made the daily work report a runtime workflow behind one flat
  tool: the skill is instructions, the runtime gathers one day's evidence
  under caps, calls the model at most three times with two tools ("give me
  these originals", "report"), composes the saved text itself and saves it
  through the briefing store. That split stands and is restated here.
- The first full-day acceptance run (2026-09-20, real stores, DeepSeek
  fast, 2 calls, 17.7 s) showed four defects a prompt cannot fix:
  - The Git evidence came only from the repo observer, which never
    backfills a repository it started watching that day and misses commits
    that are not first-parent. The TimeSink repository's three same-day
    commits were absent while the screen said the collector shipped.
  - Proof was graded per item as "any cited ref is a same-day commit", so
    an unrelated Jarvis commit certified the TimeSink item. Every
    "已合入 main", "979 passed" and "restarted the daemon" came from OCR of
    a terminal showing Codex's own words and was written as fact.
  - The material listed the longest OCR text per window and hour and the
    forty busiest windows; the rest of the 749 captures was unreachable,
    so the model could not check whether a phrase ever appeared.
  - The deep preset (thinking on) rejected `tool_choice=required` with
    HTTP 400, so the configured escalation path did not run.
- Allen's constraints: fix quality through rules, material and queries, not
  through a blanket "待核实" label, a larger character cap or unbounded
  model calls; the summary must read quickly and the body must keep every
  citation re-checkable.
- Codex keeps every session of this machine as
  `~/.codex/sessions/YYYY/MM/DD/*.jsonl`; a local repository answers
  `git log` in ~10 ms; DeepSeek fast (3/3) and deep (1/1) call a tool under
  `tool_choice=auto` when told to.

## Decision

Keep the ADR 0024 shape — a skill is instructions, the runtime owns the
procedure, one flat tool, evidence gathered once under explicit caps, the
saved text composed by code, saved through the briefing store, reused
unless regeneration is asked for, a failure keeping the saved version —
and change what the model is given and may ask:

- The day's commits come from the local repositories themselves (every
  local branch, filtered by committer time in code, marked on main or not)
  with the observer contributing only the observation time; a commit the
  observer never saw is cited as `git:<repo>:<sha>`, which the store
  verifies with `git cat-file`.
- Codex session files of the day are material of their own kind, cited as
  `codex-session:<file>` and labelled as the agent's account; an item that
  cites only them is never proven.
- Every capture, window and record of the day is a citable key whether
  listed or not, and the model has a keyword search over all of them
  beside the originals request; it may query for two rounds, then must
  report, with one retry of an unusable report: four calls at most.
- The label after an item names what it cites — each same-day commit by
  SHA, each Allen-authored record by key — or says screen only, agent
  only, or nothing. A completed item with neither is named by number as an
  uncertainty, not relabelled. A next step without Allen's words is quoted
  as an uncertainty, not turned into a suggestion.
- 核心摘要 is the model's prose followed by every item under its status,
  completed split by whether a commit or Allen's words stand behind it.
- The report's model calls use `tool_choice=auto`; a bare reply is retried
  as a malformed report.

## Alternatives rejected

- **Keep the observer as the only Git source and backfill on first watch** —
  backfilling would emit hundreds of `project.commit_seen` events per new
  path (224 skipped on 2026-09-20 alone) into the log for the observer's
  own status-board use; the report needs the day's commits, which the
  repository already holds and answers in one call.
- **Label every completed item "模型判断完成，待核实"** — measured on
  2026-09-20: five of eleven items carried it; for two (whose content was
  "commit X exists") it was noise, for two (whose proof was an unrelated
  commit) it said nothing about what was wrong. Naming the SHA does.
- **Drop the model's summary and serve a per-item index** — the served text
  became one 621-character line of 「标题／状态／来源」 triples that no
  reader could skim, while the discarded prose had named the day's two
  lines of work and the unmerged, undeployed state correctly.
- **Raise the material caps instead of adding search** — the listed material
  already ran 41 533 characters (19 329 tokens); the 629 unlisted captures
  hold ~1.4 MB of OCR. A search returns the twenty lines that matter.
- **Read Claude Code transcripts as well** — a second file format for the
  same class of material; Codex was the agent whose words the report
  mistook for facts. Left for when a report is seen leaning on Claude's.
- **Detect thinking presets and choose the tool_choice per preset** — a
  provider-specific rule in the analyst; `auto` with an instruction to call
  behaved identically on every preset measured and the retry already
  covers a bare reply.

## Consequences

Each run shells out to `git` for every watched path (two calls per path)
and parses that day's Codex session files (76 MB on 2026-09-20, 0.2 s), so
the report's cost now depends on the number of watched repositories and on
Codex's file layout, which is not a published contract. A citation may
point at a repository path or a session file that later moves; the store
checks existence at save time only. Two query rounds mean a report can cost
four model calls where it cost two. The proof label is only as honest as
the citation: an item that cites a related commit and an unrelated one is
still labelled with both, and only a reader notices. Claude Code sessions
remain invisible to the report.
