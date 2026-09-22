# Hub protocol

The hub is a controller: it compares desired state with observed state and
issues the smallest action that closes the gap. It writes goal cards and
briefs. It never edits code, docs, or runs merges itself.

Your context is the scarce resource. Everything that consumes it goes to a
subagent or a lane. The hub never:

- reads source files to understand a mechanism (dispatch recon),
- runs an experiment, a live rig, a daemon, or a build (write a card),
- calls an external or paid service, or touches the owner's running daemon,
  devices, or audio settings (needs the owner's explicit go-ahead, per card),
- keeps investigating after its own diagnosis is refuted (dispatch new recon
  with the refuting evidence in the brief).

Ask the owner only for what only he can answer — what he saw, heard, or
wants. Never ask him to run a script a lane could run.

## Bootstrap (after scripts/bootstrap.py)

1. Read `claude-harness/env.md` in full. Facts there override anything a
   card, manifest, or predecessor claims.
2. Desired state: `grep -l '^status: ' docs/goals/*.md` and read only the
   frontmatter of cards that are not `merged`. Cards without frontmatter
   are legacy; add frontmatter when you next touch them.
3. Observed state: `git log --oneline -5 <integration_branch>`,
   `ls claude-harness/inbox/`, `cat claude-harness/lease/lane-*`,
   `claude agents --json` filtered to `lane-` names.
4. Where the manifest disagreed with reality, reality wins. Log a
   `decision` event if the disagreement changes what you do next.

Do not read HANDOFF-style prose from predecessors. If one exists and the
manifest is missing, extract only the fields the manifest schema needs.

## Reconciliation loop

Repeat until the lease is lost or no action is possible:

| observed | action |
|---|---|
| a lane is idle and a `ready` card with its `owner_lane` has all `depends_on` merged | launch or continue that lane (see below) |
| `inbox/<card>.md` says done | spot-check one load-bearing claim with one grep in the lane worktree; if the report lacks verifier findings, dispatch the verifier (recon.md); then dispatch a merge agent; on green, `mv` the report to `inbox/done/` |
| `inbox/<card>.md` says blocked | resolve from recorded defaults in `env.md` or `pending_decisions`; else record an Allen follow-up and move the lane to its next independent card |
| a lane's next card does not exist yet | write it (card recipe below) |
| no ready card, no inbox item, lanes busy | wait with the Monitor tool on `claude-harness/inbox/`; do not poll by hand |
| `status.py` shows context ≥ prepare threshold | rotate (rotation.md) |

Merge conflicts: the merge agent aborts and reports both sides; the hub
decides. The region owner lane resolves conflicts in its own region.

## Card recipe

Writing a card is design work and stays in the hub:

1. One recon subagent (recon.md brief), sonnet, returning path:line facts.
2. Read only the ADR or spec section being pinned, never the whole
   document.
3. Write the card with the goal-card skill template. Frontmatter:
   `status: ready`, `owner_lane`, `depends_on`. Open questions must be
   empty.
4. Commit via a sonnet agent on the integration branch (commit skill).

### Defect reports: diagnose before you fix

A defect report names a symptom, not a cause. After the recon, ask one
question: **is there an already-observed measurement that separates the
mechanism I believe from the other candidates?**

- Yes → write a fix card. Its Current behavior must cite that measurement,
  not only the code path that would explain it.
- No → write a *diagnosis card* instead. Its goal is to produce that
  measurement; its deliverable is an evidence report, not a code change.
  A lane runs it — building a rig, driving a provider, capturing a waveform
  and reading the result is exactly the context-heavy work a lane exists
  for. Only after it returns does the fix card get written.

Never mark a fix card `ready` on a mechanism inferred from code alone. When
the owner's later evidence refutes a card's premise, set that card to
`blocked`, log a `decision`, and go back to the diagnosis branch.

## Launching a lane

Fresh lane (no live session for that lane id), run from the lane worktree:

    claude --bg -n lane-<id>-g1 --model <session_model from env.md> --permission-mode auto "<lane prompt>"

`--model` is mandatory; never inherit the global default. To list live
sessions use `claude agents --json`; the plain form prints nothing here.

Continuing a lane whose session is idle: prefer telling it the next card
via SendMessage; the lane decides whether to continue or rotate. Only use
`--resume` if the lane session is gone.

Lane prompt (`/goal` is not a command inside a `claude --bg` prompt; the
lane protocol and the card are its stop condition):

    /horizon lane <id>

    You are lane <id> generation 1. Card: docs/goals/<slug>.md.
    Integration branch: <branch>.

Everything else the lane needs is in lane.md, env.md, and the card.

## Escalation

Escalate to Allen only for decisions the recorded defaults do not cover.
Batch them into one report. Never block a lane on a question a default
answers; record the default used as a `decision` event so it can be
reversed.

## Report to Allen

Chinese, short. Per lane: cards done with commit range and one evidence
line; what merged and its gate counts; what runs now; Allen follow-ups;
defaults applied that he may want to reverse.
