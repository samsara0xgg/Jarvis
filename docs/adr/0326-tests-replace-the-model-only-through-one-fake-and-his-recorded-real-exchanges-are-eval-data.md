# ADR 0326 — Tests replace the model only through one fake, and his recorded real exchanges are eval data

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** ADR 0001 (rules H4, H7 and H9 only)

## Context

The owner accepted this on 2026-10-21, after the full-repo test audit.

ADR 0001 (2026-05) forbade replacing `LLMClient` or `decide`, and forbade
recorded model playback (H4, H7, H9), so that an iterating agent could not fake
the flagship Codex scenario. Those scenarios were deleted on 2026-09-20
(988b1a5a). What exists on main db6a2636:

- At least 19 integration files define their own fake chat client, and 3
  replace `runtime.decide` outright. The three canaries check module identity
  at import time and see none of it.
- The turn slice alone has 6 scripted clients; 5 still carry a
  `fresh_context` that no production code calls, so fakes drift from the real
  protocol without any check noticing.
- 12 % of escaped bugs (82 of 691) are model faults: prompts, tool
  descriptions, parsing of model output. They were found by his use, by
  reading his logs and by replays, never by a hermetic test.
- The best LLM-adjacent test in the repo, turn_guards' table of 120+ real
  replies from the 2026-10-12 replay exam, is recorded model output, which H7
  forbids. Tool asks went wrong 21 times in 43 between 2026-10-12 and 10-20,
  judged from his next message.
- The repository is public. His exchanges contain his words and data.

## Decision

Hermetic tests replace the model only through one shared fake that
implements the client protocol, never by patching `decide` or other
internals; recorded inputs and outputs from his real use are eval sets,
replayed before any prompt, tool-description or model change; new
LLM-dependent capability still needs one live run.

Limits:

- Eval sets live in his private runtime data, never in the public
  repository; the replay runners and their scoring live in the repository.
- Evals report scores against thresholds; they are not CI gates.
- Paid replay runs follow the project's cost rules.

## Alternatives rejected

- **Enforce H4, H7 and H9 as written** — removes the fakes from 19+ files and
  the turn_guards table, leaving `decide()` with no hermetic coverage; a live
  run costs money and varies run to run, so it cannot run on every push.
- **Let each file keep its own fake** — the status quo: 6 clients in one
  slice, 5 with a dead method, and no check that any matches the protocol.

## Consequences

- The shared fake must be typed against the real client protocol, so a
  protocol change breaks it at type-check time.
- Eval sets need curation and privacy care: they hold his words and must not
  leak into the repository, logs or CI.
- The three canaries (test_no_llm_substitution, test_decide_not_substituted,
  test_no_recorded_llm) are deleted; a new check forbids patching `decide`.
