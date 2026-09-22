# Rotation protocol

Every long-running role rotates itself. Rotation is make-before-break:
the successor is running and holds the lease before the predecessor
stops.

## Thresholds (context used, from status.py)

    prepare  40%   rotate at the next atomic boundary
    hard     45%   take on nothing new; checkpoint and rotate immediately

Rotate below the threshold when the context is polluted (rejected
approaches, stale instructions) and record that in `drift`.

## Steps

1. Finish the current atomic action. For a lane: commit the slice. For a
   hub: finish processing the inbox item or dispatching the agent.
2. Write the manifest for your role (schemas below). Every fact that a
   command can check gets a `# verify:` comment; bootstrap.py runs them.
3. Write the session log:

       python3 .claude/skills/horizon/scripts/log-session.py \
         --end planned_rotation \
         --outcome "<what this generation completed, one line>" \
         --successor <role>-g<N+1> \
         --mix "tool-output~40%, active-work~30%, protocol~15%, history~15%" \
         --drift "<card-external work you did and why, or none>"

   `--mix` is an estimate and is stored as one. user_feedback events you
   logged during the session are merged in automatically.
4. Spawn the successor from the correct working directory. `--model` is
   mandatory and comes from `session_model` in env.md; never inherit the
   global default:

       claude --bg -n <role>-g<N+1> --model <session_model> --permission-mode auto "<prompt>"

5. Wait with the Monitor tool on `claude-harness/lease/<role>` until its
   session id is no longer yours. Then end your turn with a one-line
   handover note. Take no further action.

If spawning fails, log a `failure` event, leave the manifest in place, and
end your turn; the hub or Allen will relaunch.

## Manifest: hub  (claude-harness/manifest/hub.yaml)

The manifest describes the successor, written by you: `generation` is
N+1 (yours plus one), `predecessor` is your own session id
(`echo $CLAUDE_CODE_SESSION_ID`). Never write `none`.

    role: hub
    generation: 11
    predecessor: <your own session id>
    written: 2026-09-07T10:00:00-07:00   # from `date -Iseconds`, never typed
    integration_branch: realtime-integration
    integration_head: 7c2df70   # verify: git rev-parse --short realtime-integration
    lanes:
      a: {worktree: .claude/worktrees/lane-a, branch: lane/a, card: <slug>}
    pending_decisions:
      - <one line each, and who decides>
    focus: <one line: what you were about to do>

Not in the manifest: the card queue (read cards), environment (env.md),
protocol (this skill), history (sessions.jsonl).

## Manifest: lane  (claude-harness/manifest/lane-<id>.yaml)

    role: lane
    lane: a
    generation: 3            # N+1, the successor's
    predecessor: <your own session id>
    written: <ts>
    worktree: .claude/worktrees/lane-a
    branch: lane/a
    head: e7ee64a   # verify: git rev-parse --short HEAD
    card: <slug>
    checkpoint: |
      done: <what is committed>
      next: <the next slice>
      rejected: <approach — why, if any>

## Successor prompts

Hub:

    /horizon hub

    You are hub generation <N+1>. Bootstrap, then reconcile.

Lane (`/goal` is not a command in a `claude --bg` prompt; the lane's stop
condition is lane.md plus the card):

    /horizon lane <id>

    You are lane <id> generation <N+1>. Card: docs/goals/<slug>.md.
    Integration branch: <branch>.

If `claude --bg -n` finds the name taken by a stale session, append a
letter (`hub-g3b`). Names are cosmetic; the session UUID in
sessions.jsonl is the only join key.

## Lease

`lease/<role>` holds `<session id> <generation> <ts>`. bootstrap.py
overwrites it; status.py reports `lease=held|lost`. There is no handshake:
the successor taking the lease is the signal.
