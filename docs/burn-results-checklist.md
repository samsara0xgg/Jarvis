# Jarvis Burn Results Checklist

Source set: committed burn records in `docs/progress.md`,
`docs/live-run-bugs.md`, and `docs/live-burn-*.md` as of 2026-08-27.
No live burn was re-run while preparing this checklist.

Burn log language rule: burn logs are English-only. When the live input was
not English, record an English description in the prose log and keep exact raw
text in machine artifacts only when byte-level diagnosis requires it.

## Checklist

- [x] 2026-05-28 Increment 1, Tier-2 J/K/L real-Codex happy path:
  GREEN for 7 filled invariants under `--live-codex --live-llm`; 14 rows
  remained skipped. Later investigation found the reviewer refute was caused
  by tracked-only diff capture, not by a real Codex miss.
- [x] 2026-05-28 B-0014 follow-up, untracked-file diff capture:
  FIXED in `e599b8e`; RED-to-GREEN unit coverage plus Tier-1 green
  (`876` tests). No live re-burn was required for the verdict-independent
  Increment-1 rows.
- [x] 2026-05-28 Increment 2 variant, `verify_fail` L3:
  GREEN in 1 live burn (~62 s). `verify_diff` emitted observation plus error,
  Limitation evidence was `executed/limits`, and no `task.verified` or
  `task.no_op` was emitted.
- [x] 2026-05-28 Increment 2 variant, no `verify_command` route B:
  GREEN in 1 live burn (~152 s). The run produced an observation-only
  `verify_diff` bundle, reported Limitation evidence, no verified evidence,
  and no completion language.
- [x] 2026-05-28 Increment 2 variant, reviewer fail plus no verify command:
  GREEN in 1 live burn (~290 s). Reviewer refute stayed advisory at
  `reported` evidence level and could not create verified evidence or
  `task.verified`.
- [x] 2026-05-28 Increment 2 variant, true empty diff route A:
  GREEN in 1 live burn (~37 s). Codex submitted a report, produced an empty
  diff, emitted Execution plus `missing_diff_artifact` Limitation evidence,
  and emitted no `task.verified` or `task.no_op`.
- [ ] 2026-05-28 J12 `no_submit_report`:
  DEFERRED by design. Existing coverage is the deterministic unit guard for
  zero captured reports; a live organic refusal was not considered reliable
  with Codex 0.130.
- [ ] 2026-08-25 Phase 0 exit matrix at `dcac425`:
  PARTIAL/RED. Full live invocation reported 66 pass, 11 skip, 1 fail
  (card-scoped matrix 43 pass, 11 skip, 1 fail). The single systematic fail
  was L5: reviewer verdict changed from `refutes` to `supports` after full
  diff review became available. Decision needed: retune L5 or re-scope it.
- [ ] ADR-0009 residency and perception daemon burn:
  PARTIAL. Step 0 proved ctypes/IOKit sleep and wake callbacks in a plain
  Python process; full daemon-side manual smoke rows M1/M2/M3 remained
  Allen-supervised and were not recorded as complete live burn evidence.
- [ ] 2026-08-26 ADR-0011 Tool Surface v1 at `0e5f29a`:
  PARTIAL. Eight runnable Tier-2 rows passed (T1-T4, T6, T7, E1, E2);
  T5 `screen_look` remained pending because macOS Screen Recording permission
  requires Allen approval. Registry count stayed 41 because `entity.resolved`
  already existed.
- [ ] 2026-08-26 ADR-0012 Confirmation Flow at `ded19db`:
  PARTIAL/RED. C2-C6 passed, C1 failed because `write_file` selected append
  for a non-existing target, and spoken end-to-end C1 stayed pending. The burn
  also found machine confirmation asks armed only 10 of 20 ask attempts; prose
  confirmation text preempted the tool path in the other half.
- [x] 2026-08-26 web retrieval burn after `6b7508c` and `4b146b1`:
  GREEN with open caveats. `web_fetch` now extracts real body text before
  applying the text cap, sends a user agent, `web_search` backend resolution
  and cap handling passed, Tavily passed with a real key, and the provider/key
  mismatch guard worked.

## Open Burn Items

- Re-run or re-scope Phase 0 L5 after deciding whether the refutes path should
  stay live-covered under full-diff review.
- Grant macOS Screen Recording permission and run ADR-0011 T5.
- Fix `write_file` create-vs-append targeting, then re-run ADR-0012 C1 and the
  spoken flagship path.
- Fix or redesign confirmation ask arming so prose confirmation does not bypass
  `confirmation.requested`.
- Run the Exa web-search path with a real `EXA_API_KEY`.
- Add committed, repeatable harnesses for the ADR-0011 and ADR-0012 live burn
  rows; the current evidence is prose log only.
- Decide whether ADR-0009 M1/M2/M3 should get a recorded daemon-side live burn
  after Allen handles install, Screen Recording, microphone, and sleep/wake
  prerequisites.
