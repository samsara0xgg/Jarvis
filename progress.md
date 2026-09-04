# Current repair checkpoint

Current verified code SHA: 3a6856a6bb4aa7d7a612c4a98d963513a6f93eab.
Controller independently replayed all five review counterexamples successfully;
code repair review passed, with no known blocking code P0/P1. Its complete
non-live rerun: 442 passed / 63 deselected / 28.13 s; ruff PASS, strict mypy
178 files PASS, lint-imports 72 files / 200 deps / 1 kept, diff check PASS.
Its temporary .venv link was removed and this worktree was restored clean.

Historical review checkpoint 22e363a: NOT ACCEPTED (five reproduced P1 findings).
Historical evidence: /tmp/jarvis-resume-audit.jYH7jz/review-22e363a.md.
Code acceptance does not complete the overall goal: live/E2E remains unmet.
Live r4 failed on the first decision APIConnectionError before worker startup;
no E2E latency/audio/concurrency result. Later provider attempts remain paused
pending controller-managed exact destination/payload authorization. Local report:
/tmp/jarvis-takeover-live-22e363a-r4/acceptance.md.

Base HEAD: 6ed7280d2884c6a1e26a1c2d4a04d72060e729e5 (verified clean before edits).
Wave 3 ancestor: 7b69a53eb7f837b15abfaf76c0572fa8f818df3b (verified).
Historical implementation checkpoint: 84d9a06ac56b0784bad394edb5e6d6cbe5e68ef5
(superseded by the current verified code SHA above).
Module import: this worktree's jarvis/__init__.py; shared installed interpreter
/Users/alllllenshi/Projects/jarvis/.venv/bin/python. Branch codex/wave45-takeover-fix.

Evidence obtained:
- R3: real SQLite writer barrier reproduced 50 ms cancellation taking 5.212 s.
  Hot connection helper avoids migration writes and shares the total deadline;
  timeout/no-terminal/retry acceptance passed (pytest 0.08 s).
- R9: real thread-pool vision call reproduced ProgrammingError and zero cost
  rows. Production adapter now mints per-request client and SQLite connection.
- R11: placeholder keys reproduced both provider fallback regressions. Synthetic
  flat presets now retain provider default key environment variable.
- R9/R11 and R3 focused checks: 4 passed in 0.27 s (SDK fixtures, no live call).
- R5–R7: independent action agent reproduced seven real barrier/restart failures;
  repairs and process-close failure injection passed the original checkpoint.

Implemented and integration-verified: R1–R11, including durable outbox admission,
cancel fences/deadline, explicit debt/borrowing, shutdown and restart quarantine,
terminal consumption, vision/request accounting and legacy key compatibility.
Also fixed verified Codex close uncertainty and verify-command/lease cwd mismatch.
Remaining acceptance: successful live/E2E evidence and exact destination/payload
authorization, managed by the controller with the user; no automatic retry.
No claim of end-to-end audio or physical playback evidence has been made.

Independent-review correction evidence (parent checkpoint 22e363a):
- F1: authorize and admit sample/check lease TTL after BEGIN IMMEDIATE returns.
  Real competing SQLite writer across expiry rejects authorization with no
  outbox; admission rejects while retaining pending debt and no dispatch.
- F2: submission-order root reservations promptly refuse a conflicting root
  in the same turn while its predecessor owes cleanup. This is an explicit
  unsupported dependency, not support for serial same-turn workers. Real
  registry tests cover active/quiesced predecessors, production turn cleanup,
  no premature finalizer and successful work in the next turn. Explicit read
  verification children retain their existing quiescence/borrowing contract.
- F3: an outer finally settles client ownership on heartbeat, notification,
  and server-request exceptions as well as structured returns. Six real local
  SIGTERM-resistant child cases check close success versus kill failure;
  uncertainty retains lease/inflight, no quiesced fact and no stash finalizer.
- F4: response.request_admitted is a durable admission fact committed under
  the cancellation fence after request setup. The fence releases before I/O;
  this fact does not mean the provider ran or returned. Cancellation before
  admission prevents decision/reviewer calls; already admitted requests may
  return later. Pricing/fresh-context/network barriers cover both orderings.
- F5: turn.ended persists consumed_trigger_event_uid in the same commit as
  semantic completion, preserving its last-gate source chain. Failure of the
  separate optional marker cannot make the held watcher repeat completion;
  a true orphan is still processed. No unconditional pre-work consumption.

Final correction checks: 442 passed / 63 deselected in 29.19 s, full non-live
suite with explicit worktree PYTHONPATH and temporary layer-canary .venv link.
Thirty focused integration cases passed in 0.88 s. Ruff, strict mypy (178 files),
6-layer import contract (72 files / 200 deps), and diff check passed. The first
full run's sole failure was the cost canary's concrete variable-name rule;
the production local was renamed cost_recorder and the full suite rerun.
Independent local probes additionally reproduced the F1/F4/F5 review barriers
against these changes without changing source or contacting a provider.

Stop status: documentation closeout only, then all implementation, tests,
subagents, and live attempts stop. No subsequent Wave/features/automatic run.
Continuation requires a new user instruction; pending live evidence is not
permission to resume it independently.

Historical checkpoint evidence (84d9a06): 429 passed / 63 deselected, 27.93 s with explicit worktree
PYTHONPATH (needed by child Python processes using shared venv). A temporary
.venv symlink let the layer-import canary run; it was removed after validation.
Ruff, strict mypy (177 source files), 6-layer import contract and diff check passed.
Outbox rollback/ambiguous-post-admission and verify-cwd checks: 14 focused, 0.48 s.
Real process failure injection: ignored SIGTERM + failed kill retains active
ownership, does not emit worker.quiesced or run the stash finalizer.

Known architecture limits: two long drive_turn calls occupy both default pump
workers; cancelled-response success continuation and recovery after existing
milestones remain incomplete; pending outbox debt is retained, with safe L4
retry before admission and no blind replay after ambiguous committed admission.
No production rollout occurred. All shipped feature flags remain off.
