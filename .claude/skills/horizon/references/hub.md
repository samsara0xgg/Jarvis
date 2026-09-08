# Hub protocol

The hub is a controller: it compares desired state with observed state and
issues the smallest action that closes the gap. It writes goal cards and
briefs. It never edits code, docs, or runs merges itself.

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

## Launching a lane

Fresh lane (no live session for that lane id), run from the lane worktree:

    claude --bg -n lane-<id>-g1 --model <session_model from env.md> --permission-mode auto "<lane prompt>"

`--model` is mandatory; never inherit the global default. To list live
sessions use `claude agents --json`; the plain form prints nothing here.

Continuing a lane whose session is idle: prefer telling it the next card
via SendMessage; the lane decides whether to continue or rotate. Only use
`--resume` if the lane session is gone.

Lane prompt, first line must be the /goal condition from the card:

    /goal <condition>

    Invoke the horizon skill with args `lane <id>` before anything else.
    Card: docs/goals/<slug>.md. Integration branch: <branch>.

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
