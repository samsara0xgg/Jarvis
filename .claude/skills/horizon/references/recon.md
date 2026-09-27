# Recon, verifier, merge: fresh-context subagents

Three read-mostly roles share one return contract. They exist so the hub
and the lane never load raw code or raw tool output into their own
context. Always pass `model` explicitly.

## Return contract (all three)

Every claim is labelled with one of three words:

- **FACT** — something you saw. Cite the evidence, whichever kind it is:
  `path:line`, a runtime trace or log line, an emitted event or wire
  payload with its field values, a git sha or branch state, a sentence of
  the canonical spec or an ADR decision, a command and its output.
- **INFERENCE** — what you believe follows, stated as such, with the facts
  it rests on and what would falsify it. Inferences are wanted; unmarked
  ones are not. The hub adjudicates them, so give it something to
  adjudicate.
- **UNKNOWN** — a question the brief asked that the evidence does not
  settle, and what would settle it. Replaces a bare "not checked" list.

Two checks belong in every recon, whether or not the brief names them:

- **Adjacent work** — other cards, branches, lanes or uncommitted
  worktrees touching the same files or contracts, and where they are.
- **Semantic conflict** — a place where the change being contemplated
  would contradict a documented invariant, a neighbouring feature's
  assumption, or a decision an ADR already made.

No diffs pasted; counts and final lines only.

## Recon (sonnet)

Used by the hub before writing a card and by a lane before implementing.
Brief shape:

    Read-only recon in <worktree>. Question: <one sentence>.
    Look at: <paths, symbols, events, or logs — a handful>.
    Return FACT / INFERENCE / UNKNOWN per the return contract, covering
    <the facts the card needs>, plus adjacent work and semantic conflict.
    Mark every inference and say what would falsify it.

## Verifier (agent type `verifier`, model fable)

Used by a lane after implementation and by the hub when a report lacks
verifier findings. Advisory: it has no veto; the hub decides.

    Independent fresh-context verification. Worktree <path>, branch <b>,
    read-only. Card docs/goals/<slug>.md; diff range <base>..HEAD.
    Implementer claims: <paste report>. Check (1) every Acceptance evidence
    item is satisfied by the diff and by re-running the hermetic commands
    yourself, paste final lines; (2) regressions against Boundaries;
    (3) layer boundaries via `git diff --stat`; (4) docs describe exactly
    the implemented behavior. Return: Confirmed defects (path:line, repro),
    Speculative risks, Evidence re-run, Verdict accept | accept with fixes
    | reject, Not checked.

## Merge (sonnet)

Used by the hub only.

    Mechanical git task. Work only in <integration worktree>. Never push,
    never touch other branches or worktrees, never bare stash, do not edit
    files. Confirm clean tree and branch; `git merge --no-ff <branch>`; on
    conflict `git merge --abort` and report both sides. Then set
    `status: merged` in docs/goals/<slug>.md and commit it. Gates:
    <gate commands from env.md>. Return branch+sha, gate tails, failures,
    not run.
