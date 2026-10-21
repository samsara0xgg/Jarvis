# ADR 0325 — Tests are sorted by level, assert what someone can observe, and run whole on every push

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** none

## Context

The owner accepted this on 2026-10-21, after the full-repo test audit.

On 2026-08-25 `tests/unit` was retired under a no-unit-test rule (now in
`.claude/rules/python-testing.md`). Every test since has been written as an
"integration regression pin". Measured on main db6a2636 (2026-10-20):

- 5,948 pytest items in 300 files, 131k lines (0.87 x the product code),
  914 s serial. The 1,000 slowest items take 86 % of the time, and most of it
  is waiting on real clocks: 306 of 330 s in the turn-pipeline files.
- No CI runs the suite. Authors run "targeted" subsets. Clean main had 9 red
  tests, one of them a canary red since 2026-10-19 that caught a real bug.
- Of 691 fix commits since 2026-08-25, 12 were found by an existing test.
  5 % were regressions; the rest were spec gaps (46 %), UI (16 %), model
  faults (12 %), real environment (11 %) and concurrency (9 %).
- Mutation run (180 mutants on covered lines): 77 % killed, 80 of 139 kills by
  a single test file. Line coverage 88 %, and 40 % of covered lines are run by
  one test file only. The suite protects what it covers; it is not redundant.
- The most valuable "integration" files are pure-logic tables (turn_guards,
  doing_run rejections, stays_from_fixes). The rule did not stop unit tests;
  it removed their name.
- Upkeep: 352 change detectors (asserting prompt sentences, copy, shipped
  config values), 684 couplings to private names, 497 real sleeps or polls,
  about 110 imports between test files in one slice alone, 19+ hand-written
  fake model clients and 11 fake clocks.
- The owner wants small changes to land after related checks only. That only
  stays safe if the whole hermetic suite is cheap enough for CI to run on
  every push.

## Decision

Organise tests by level (static, behaviour table, component, contract,
end-to-end smoke, eval), not by folder name; every test asserts what the
owner or another app can observe; the hermetic levels run whole in CI on
every push to main, under 3 minutes, in any time zone.

Limits that make the decision checkable:

- Assert event-log rows, HTTP/WebSocket payloads, ledger state and return
  values. Never private attributes or log wording. User-visible strings are
  asserted by their `lang.py` key; prompts by invariants plus one snapshot
  that a single command updates.
- No real sleep and no wall-clock assertion; time comes from an injected
  clock and zone.
- Fakes come only from `tests/support/`; a test file never imports another
  test file.
- A bug fix adds a row to the table that owns its behaviour class; a new test
  function only for a class that has no table yet.
- A canary declares its rule where the thing is defined (a registry flag, a
  pragma), never as an allowlist of file names.
- The live-run rule for new LLM-dependent capability is unchanged.

## Alternatives rejected

- **Keep the rule and delete tests to cut the cost** — the per-file review of
  all 304 files deletes only about 10 % of tests, and 40 % of covered lines
  are run by one file only, so deletion loses protection while the time
  (mostly waiting) stays.
- **Keep running targeted subsets by hand, no CI** — 12 catches in 691 fixes,
  and 9 reds that sat on main unseen, show the tests are not run when a
  regression lands.
- **A classic pyramid of mocked unit tests** — 46 % of escaped bugs are spec
  gaps at the edge of real stores (SQLite, the event log); mocking the store
  hides exactly those, while component tables on a real temp store already
  run in milliseconds.

## Consequences

- Production code needs seams it lacks: an injected clock and zone, and
  timing knobs (answer hold, attach waits, poll intervals) as parameters.
  Several live in hot files.
- Moving helpers out of test files is a one-time change across many files
  and collides with concurrent work.
- A red CI on main becomes a shared duty: the thread that pushed it fixes it.
- Prompt snapshots must be read when updated, or they become noise.
