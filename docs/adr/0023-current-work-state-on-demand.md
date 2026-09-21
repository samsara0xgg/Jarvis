# ADR 0023 — Current work state is one persisted record, analysed on demand

**Status:** Accepted
**Date:** 2026-09-20
**Supersedes:** none

## Context

- Allen wants Jarvis to know what he is working on: open the dashboard and
  see the last integrated picture, ask "我现在在做什么 / 今天主要做了什么 /
  之前说的事情进展如何" and get an answer that investigates the latest
  data. ADR 0021/0022 made the raw material readable (TimeSink app spans,
  screen OCR, state reasons) and ADR 0020 keeps todos, knowledge and
  conversation records local, but nothing combined them and nothing
  persisted a conclusion; every question re-read everything from scratch.
- TimeSink rows keep extending while a window stays in front, so "new data
  since id N" misses updates; the read adapter already answers by time
  overlap and pins revisions, so copying the store would only duplicate it.
- A background model loop is off the table for this version: the cost is
  unbounded, the value at 3 a.m. is nil, and observers must stay silent
  (spec §3.4.1). Screen text and old records are untrusted material; the
  analysis must not be able to act on anything it reads.
- The dashboard and the conversation must show one truth: two analysers
  with two caches would disagree within minutes.

## Decision

One record, `work_state.revised`, holds the current work state: latest
row wins, versions are checked on write, every claim carries a basis
(`stated` = Allen said it, `observed` = app/window/screen data, `inferred`
= the model's reading) plus the source refs it cites, and the record names
its evidence window, coverage, the latest data instant and the analysis
time. Absence of data is written as unknown, never filled in.

One refresh workflow, owned by the runtime, serves both entry points: the
`refresh_work_state` tool the decision model calls when asked about
current or recent work, and the dashboard's `POST /inherent/work-state/refresh`.
It gathers bounded evidence (today's spans aggregated, the last two hours
of screen text, two days of records, open todos, active knowledge, Git
observations, the user's optional note) and, when a question is asked,
retrieves the older records and captures its topic words match, so the
defaults are a floor, not the search. Every cap that clipped material is
stated in the material and saved with the record. The fingerprint covers
the bounded material, source revisions, durations, state events, coverage, local day,
question and note, never the request clock alone; the
configured preset is called only when it changed or a re-analysis was
requested, otherwise the saved record is reused. The model receives no
tools except the report schema, and the record's rules are enforced on
save: unknown refs are dropped, "stated" needs an Allen-authored record or the note,
"observed" needs TimeSink data, inferred claims force an uncertainty, and
"now" exists only for observations inside the recent window and carries
the cited screen instant, not a newer unrelated observation. Malformed model reports
fail without replacing the saved record. A refresh with the same input as the running one joins it; a
different input waits and runs on the saved result. Each run retains its own response
for its callers even if a subsequent run finishes first; a stale writer loses to
the version check.

Background work is a 5-minute head poll (`timesink.state_observed`,
emit-on-change on max ids and latest span end / capture last-seen / state
event, so an extending row counts as an update) that records collector
health and never calls a model. The view keeps three clocks apart: when
the head was last checked, the newest observation it found, and when the
analysis ran. Every poll, including unchanged polls, and each manual refresh updates
the in-process check clock; event history still records only changed heads. After restart
the last persisted observation is the fallback until the first poll. Opening the dashboard reads the record; only an explicit
refresh or a work-state question runs an analysis. "Today" is cut in the
configured local zone.

## Alternatives rejected

- **Analyse in the background every N minutes** — pays for model calls
  while nothing changed and while Allen sleeps; the evidence fingerprint
  gives the same freshness at the moment someone actually looks.
- **Copy TimeSink rows into the event log as the "sync"** — duplicates a
  store that is already read transactionally by overlap; the copy would
  still have to re-copy extending rows and would drift on every edit.
- **Let the decision model assemble the state ad hoc from the existing
  tools each time** — no persistence across restarts, the dashboard would
  need its own copy of the prompt, and two prompts diverge.
- **Store the state in the todo/knowledge revision store** — those records
  are per-item commitments with request deduplication; the work state is
  one document whose only identity is "latest", and its writer is the
  runtime, not the model's tool arguments.

## Consequences

Each refresh with new evidence costs one `fast`-preset call on a few
thousand characters; repeated clicks and repeated questions without new
data cost nothing. The state is only as current as TimeSink and memory.db
are, and the record says so through `observed_until` and coverage. Todos
are never changed by the analysis; a link is a claim with a basis, not a
completion. The dashboard replaces the placeholder health module with the
state module. Project identification rules, task marking, agent activity
collection, night consolidation and morning delivery remain outside this
decision.
