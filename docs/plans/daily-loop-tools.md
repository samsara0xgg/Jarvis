# Daily-loop tools — implementation handoff

Branch: `codex/daily-loop-tools`.
Base: `d542a4d` from CC's `worktree-adr0019-impl`.
Scope: the agreed 10 new tools and the `search_records` extension. Local todos are
canonical; no Microsoft synchronization. Accepted decision: ../adr/0020-local-daily-loop-state.md.
Interface contract: ../spec.html#daily-loop-tools.

## Delivered

- Flat Tool adapters in `jarvis/execution/daily_tools.py`; the existing dispatcher
  still owns action serialization and lifecycle. Only registration and the old
  search_records definition change in `execution/tools.py`.
- Independent state modules for validated inputs, history reads, Git activity
  reads, and local todo/knowledge/briefing revisions. No conversation schema changes.
- `search_records` now returns structured excerpts with IDs and a snapshot cursor;
  `read_records` returns exact chunked originals. Callers expecting the former
  rendered-only output must adopt the documented result.
- Writes use immutable revision events, exact-request deduplication, optimistic
  version checks, and validated source references. Read-only Live exposure stays
  read-only. Metadata that cannot fit the output budget fails explicitly rather
  than silently clipping IDs or cursors.
- Git activity queries consume existing observation events. App activity reads
  TimeSink locally; screen/agent remain not_implemented. Historical health is unknown.
- Natural-language cues for todos, briefings, knowledge and activity/history
  requests reach the tool-offering path instead of tool-free routine replies.

## Verification

Focused acceptance:

```sh
python -m pytest -q tests/integration/test_daily_tools.py \
  tests/integration/test_flat_tool_dispatch.py \
  tests/integration/test_routine_pre_route.py
```

Latest focused result: 50 passed in 0.23s. Covers runner and inline dispatch,
restart persistence, cross-domain request deduplication, concurrent update conflict,
reference validation/rollback, timestamp offsets and DST boundaries, lossless long
text, stable snapshot pagination, missing-record continuation and no delivery side effects.

Real-model acceptance (requires the configured DEEPSEEK_API_KEY in the environment):

```sh
python -m pytest -q -s tests/integration/test_daily_tools_live.py --live-llm
```

Recorded run: 1 passed in 22.03s, deepseek-v4-flash selected all 11 interfaces,
zero tool errors. Its outgoing payload is limited to literal synthetic prompts,
these tool definitions and temporary-database results. No personal runtime prompt,
production memory or repository content is sent. This proves real model/tool
interaction, not the full voice/attention/background pipeline.

Architecture gate: 1 contract kept, 0 broken. New implementation modules pass
strict mypy (5 source files). Changed Python files pass ruff. Full-repository mypy
still reports three pre-existing errors in voice_live.py and test_launchd_install.py.
The ADR checker accepts the new ADR but the repository check has a pre-existing
missing ADR-0013 reference, reproduced in CC's worktree too.

## Coordination with CC

CC added worker tools in `1bad3a4` after this branch started. Its changes to tools.py
remove old worker registration/resource handling; this branch changes history-tool
registration and adds daily tools. Both changes touch tools.py and runtime/__init__.py,
so review the integration diff even when Git merges cleanly. Do not copy entire files.

Session branch `b521d24` modifies memory rendering and adds ID-based history access.
This branch reads the existing records table without altering that schema. Preserve
its session compaction and ID access when reconciling the two search_records definitions.

## Remaining daily-loop work

These are separate follow-up implementations, not capabilities provided by these tools:

1. Skill discovery/loading and the knowledge-consolidation / morning-briefing instructions.
2. Persist idle/lock/sleep reasons and collector health in TimeSink; add selected
   screen and external Agent collection. App/window/Chrome span reads are connected.
3. Background input watermarks, incremental consolidation, scheduling and wake catch-up.
4. Morning card/voice delivery, quiet periods, defer/acknowledge state and crash recovery.
5. GPT Live write admission through the existing permission policy, followed by real
   voice acceptance. Adding tools must not silently remove ReadOnlyToolRegistry.
6. Repeated real-day acceptance of the complete loop after those pieces are connected.

Do not treat save_briefing as delivery or a due_at field as a scheduled reminder.
Before merging, consolidate durable contract changes in spec/ADR and remove this
scratch handoff when its integration notes are no longer needed.


## TimeSink local connection (2026-09-20)

The activity tools now read TimeSink's installed SQLite store through
`jarvis/state/timesink.py`, configured by `observer.timesink` in config/jarvis.yaml.
TimeSink remains the sole app/window/Chrome collector. This work adds no MCP,
screenshot polling, idle event persistence, scheduler or production restart.

The adapter handles overlapping spans, UTC/DST conversion, clipped durations,
WAL reads, missing/incompatible stores, mutable-row cursor checks and revision-pinned
source references for knowledge/todos/briefings. Project filters explicitly exclude
unmapped app spans. External references are not archived; old versions can become
unavailable. Integration tests are in tests/integration/test_timesink_activity.py.

TimeSink acceptance evidence:
- Focused daily + TimeSink + flat dispatch + routing suite: 59 passed in 0.29s,
  including ActionRunner reads, source revision invalidation, and mutable source refs.
- Full hermetic integration/canary suite: 1090 passed, 1 skipped, 3 deselected,
  4 warnings in 62.14s. The additional runner test above was added and passed after
  that full run; the only later production edit was a config type annotation fix.
- Installed TimeSink live read: 2170 spans in a 24-hour interval, 145 pages with no
  duplicate IDs, exact count matches independent read-only SQL. Detail retrieval
  and source validation in a temporary briefing store passed. No network or captures.
  Metadata-only evidence: /tmp/jarvis-timesink-acceptance.json.
- Changed Python files pass Ruff; the 4 changed state/tool modules pass strict mypy.
  Full strict mypy checked 268 files and reports only the same 3 baseline errors.
- Import architecture: 1 kept, 0 broken (108 files / 335 dependencies).
- Source app and production Jarvis were not modified/restarted; changes remain in
  codex/daily-loop-tools and require integration with CC's concurrent branch.


## Screen collection handoff — discussed, not implemented

Allen accepted a first-pass design on 2026-09-20. Start from this branch's
TimeSink adapter and flat activity tools; do not add a second app/idle tracker.

- Identify foreground windows by process + window ID, not title alone.
- After a foreground-window change, wait 3 seconds on the same window.
  Restart the timer on another change. While scrolling/dragging, wait 1 second
  after it settles, but cap the deferral at 10 seconds.
- Check content changes every 30 seconds in a retained window. Enforce a
  10-second minimum between full captures; deduplicate unchanged frames.
- Reconfirm content every 5 minutes. Prefer accessible text, adding a screenshot
  for visual content; low-resolution local comparison need not retain every frame.
- Initial visual threshold: roughly 10% of image regions materially changed;
  validate against real apps, cursors, clocks, animations and video before fixing it.
- After 3 minutes without input, reduce checks to once every 2 minutes; reading,
  videos and meetings must not be equated with absence. Pause on lock/sleep/manual
  suspension, then restart stability checks on resume.
- Associate captures with timestamps, app/window identity and TimeSink spans;
  recheck window identity after capture and discard a mismatched sample.
- Keep acquisition local; summarize selected time segments later rather than
  sending each image to a model. Retention, exclusions, permission handling and
  durable collector health/state events still need a concrete implementation.
- Accept with an actual app-switch/read/develop/lock run and measure missed
  meaningful segments, duplicate captures and performance impact.

No screen collection, scheduler, delivery or new skill has been implemented here.
