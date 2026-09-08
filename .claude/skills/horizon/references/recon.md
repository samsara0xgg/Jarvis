# Recon, verifier, merge: fresh-context subagents

Three read-mostly roles share one return contract. They exist so the hub
and the lane never load raw code or raw tool output into their own
context. Always pass `model` explicitly.

## Return contract (all three)

- Every claim carries `path:line`.
- Facts are labelled `observed` (seen in the file or in command output) or
  `inferred`.
- End with `not checked:` listing what the brief asked for that was not
  examined.
- No advice beyond the brief's question. No diffs pasted; counts and final
  lines only.

## Recon (sonnet)

Used by the hub before writing a card and by a lane before implementing.
Brief shape:

    Read-only recon in <worktree>. Question: <one sentence>.
    Look at: <paths or symbols, at most a handful>.
    Return: for each of <fact list the card needs>, path:line and
    observed/inferred. Not checked at the end. No recommendations.

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
