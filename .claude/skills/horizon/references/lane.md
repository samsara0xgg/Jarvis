# Lane protocol

A lane owns one file region, one branch, one worktree, and implements the
cards assigned to it one at a time. It never merges into the integration
branch and never redesigns a card.

## Bootstrap (after scripts/bootstrap.py)

1. Read `claude-harness/env.md`.
2. `git merge <integration_branch>`; on conflict, abort and write a
   blocked report (below). Do not resolve conflicts outside your region.
3. Read the card in full. If the manifest has a `checkpoint`, read it; it
   tells you what a predecessor finished, rejected, and was about to do.
4. Set the card's frontmatter `status: in_progress` and commit it.

## Per card

1. Recon: validate the card's Current behavior claims against the
   repository (recon.md, or do it yourself if the region is small). If the
   card contradicts the repository, stop and write a blocked report; do not
   redesign.
2. Implement in verifiable vertical slices. After each slice: commit with
   the commit skill, append a Progress line to the card, run `status.py`.
3. Run the card's acceptance and regression commands yourself. Only the
   final lines and counts enter the conversation: pipe through `tail -n 5`
   or equivalent. Never paste a full test run.
4. Dispatch the verifier agent (recon.md brief) in a fresh context.
5. Set `status: done`, commit, write the report to
   `claude-harness/inbox/<slug>.md`, then nudge the hub with SendMessage
   if a hub session is listed by `claude agents`. The file is the report;
   the message is a courtesy.

## Report format (inbox/<slug>.md)

    card: <slug>
    lane: <id>
    status: done | blocked
    branch: <branch>  range: <base>..<head>
    evidence:
      - <acceptance command> → <final line>
      - <regression command> → <final line>
    verifier: <verdict> — <one line>
    not checked: <list>
    blocked by: <only for blocked; one sentence, what unblocks it>

## After a card

Check `status.py`:

- context below prepare threshold and a next card is assigned → continue
  in this session; go to Bootstrap step 2.
- context at or above prepare threshold → rotate now (rotation.md). This is
  the cheap moment: everything is already externalized.
- context is small but polluted by rejected approaches from this card →
  rotate anyway, and say so in the session log `drift` field.

No next card assigned → write the session log with `end: complete` and end
your turn. The hub will message the lane or launch a fresh one.

## Mid-card rotation

Finish the current slice (commit it), then follow rotation.md. The
`checkpoint` block in the lane manifest is the only thing your successor
gets from you besides git and the card.
