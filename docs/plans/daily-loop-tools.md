# Daily-loop tools — Claude Code handoff

Branch: `codex/daily-loop-tools`.
Completed implementation commit: `1ced9d3`.
Integrated dependency: CC's `worktree-adr0019-impl` at `37f65e7`, including
four Codex worker tools, the audit-chain removal, and the log-only Stop hook.
Continue from this branch; do not recreate the removed ActionRunner or audit chain.
Production Jarvis has not been restarted and main has not been modified.

## Completed scope

- Eleven flat tool interfaces: search_records, read_records, query_activity,
  read_activity, search_knowledge, save_knowledge, create_todo, list_todos,
  update_todo, save_briefing, get_briefing.
- Local event-derived todos, knowledge and briefings with request deduplication,
  version checks and source validation. No Microsoft synchronization or delivery.
- Exact paged conversation reads without changing memory.db. search_records now
  returns identified excerpts, replacing its former rendered-only response.
- Git observations and local TimeSink app/window/Chrome spans behind the same
  activity tools. TimeSink remains the only app collector and database writer.
- TimeSink reads use overlap, UTC/DST conversion, clipped durations, WAL snapshots,
  append watermarks and mutable-row revision checks. Gaps have unknown causes;
  project filters exclude unmapped app spans rather than inferring attribution.
- External source refs pin database identity, row and revision. Updated evidence
  requires a fresh query; prior TimeSink revisions are not archived.
- Daily requests reach the tool-offering route. GPT Live's read-only policy remains.
- Shared registry integration tested with spawn_worker, wait_worker, send_input,
  close_worker and daily tools present together without starting a worker.

## Entry points and contracts

- jarvis/execution/daily_tools.py: flat adapters and model-facing descriptions.
- jarvis/state/daily_contract.py: closed schemas, paging and source-ref guidance.
- jarvis/state/daily_records.py: original conversation retrieval.
- jarvis/state/daily_store.py: local revision writes and folds.
- jarvis/state/daily_activity.py: mixed activity queries and details.
- jarvis/state/timesink.py: read-only external SQLite adapter.
- config/jarvis.yaml observer.timesink: enabled local database path, wired only
  through jarvis/runtime/__init__.py. An absent block does not open the user's DB.
- docs/spec.html#daily-loop-tools: canonical interface and failure semantics.
- docs/adr/0020-local-daily-loop-state.md and 0021-read-local-timesink-activity.md:
  accepted architectural decisions.

Use returned source_refs for evidence, not an activity: lookup ID. A live-model
run exposed that ambiguity; the shared schema and tool descriptions now explain it.
The separate session branch b521d24 also changes conversation history access:
reconcile its response/registry changes when integrating it, preserving compaction.

## Verification on the integrated branch

- Complete hermetic suite: 957 passed, 1 skipped, 4 deselected, 4 warnings in 56.28s.
  Command: python -m pytest tests -q -m 'not live_llm and not live_codex' -x.
- Focused suite after source-ref wording clarification: 57 passed in 0.28s.
  Files: test_daily_tools.py, test_timesink_activity.py, test_flat_tool_dispatch.py,
  test_routine_pre_route.py under tests/integration/.
- Installed TimeSink read through shipped config and real dispatcher: 1914 rows
  over 24 hours, 128 pages, no duplicate IDs, independent SQL count matched.
  Exact detail and a source-referencing temporary briefing passed. No network or
  screen capture. Metadata-only evidence: /tmp/jarvis-timesink-acceptance.json.
- Real-model acceptance after the source-ref clarification: 1 passed in 20.10s;
  DeepSeek selected all eleven interfaces with zero errors, synthetic fixtures only.
  Command: python -m pytest tests/integration/test_daily_tools_live.py --live-llm -q.
  Evidence: /tmp/jarvis-daily-merged-synthetic-fixed/test_live_daily_tool_selection0/acceptance.json.
- Architecture: 1 contract kept, 0 broken. All 14 changed Python files pass Ruff;
  the six daily-loop implementation modules pass strict mypy.
- Full strict mypy reports 3 pre-existing errors in voice_live.py and
  test_launchd_install.py (223 files checked). Full Ruff reports 82 pre-existing
  errors only in unchanged .claude/skills/horizon/scripts files.
- The ADR checker still reports the pre-existing missing ADR-0013 reference.
  Both newly added ADRs conform to the file standard.
- Before integration, 1091 hermetic checks passed; the smaller integrated count
  reflects CC's removal of retired execution-chain tests, not skipped new tools.

## Screen collection — discussed and accepted, not implemented

Allen approved these first-pass parameters on 2026-09-20:

- Identify foreground windows by process + window ID, not title alone.
- After a window change, wait 3 seconds on the same window. Restart that timer
  on another change. While scrolling/dragging, wait 1 second after it settles,
  but cap deferral at 10 seconds.
- Check for content changes every 30 seconds in a retained window. Enforce a
  10-second minimum between full captures and deduplicate unchanged frames.
- Reconfirm content every 5 minutes. Prefer accessible text, with a screenshot
  for visual content; low-resolution local checks need not retain every frame.
- Start visual-change detection around 10% of image regions materially changed.
  Validate on real apps, cursors, clocks, animation and video before fixing it.
- After 3 minutes without input, reduce checks to once every 2 minutes. Reading,
  video and meetings are not absence. Pause on lock/sleep/manual suspension,
  then restart stability checks on resume.
- Associate timestamps, app/window identity and TimeSink spans. Recheck window
  identity after capture and discard a mismatched sample.
- Acquisition stays local; summarize selected time segments later rather than
  sending every image to a model. Define retention, exclusions, permission
  handling and durable collector health/state events before shipping.
- Accept with a real app-switch/read/develop/lock run; measure meaningful missed
  segments, duplicate captures and performance impact.

TimeSink currently pauses accounting on idle/lock/sleep but does not persist those
reasons. Reuse its native detection rather than adding a second app/idle tracker.
Screen collection can be referenced from Hermes's local computer_use capture/AX
implementation, but no Hermes service or new MCP server has been integrated here.

## Remaining daily loop

1. Persist TimeSink state/health reasons and implement the above content collector.
2. Add external agent activity history where worker/session data is available.
3. Skill discovery/loading plus night consolidation and morning briefing workflows.
4. Background watermarks, scheduling and wake-time catch-up.
5. Morning delivery, quiet periods, defer/acknowledge and delivery deduplication.
6. GPT Live write admission through policy, then multi-day and real-voice acceptance.

A due_at is not a scheduled reminder; saving a briefing does not deliver it.
No screen collector, new skill, scheduler or production deployment is included.
