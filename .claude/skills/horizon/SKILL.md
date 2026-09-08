---
name: horizon
description: Run the repository under the bounded-context long-horizon harness as a Hub or a Lane. Use when a session is explicitly acting as a Hub or Lane, continuing an existing generation after rotation, or recovering harness state. Invoke once per generation.
argument-hint: "hub | lane <id>"
---

# Horizon

You are one generation of a long-running logical role. Sessions are
disposable; durable state is authoritative. Invoke this skill once per
generation, never again in the same session.

## Roles

Argument `hub` → read `references/hub.md`.
Argument `lane <id>` → read `references/lane.md`.

Always read `references/rotation.md` and `references/logging.md`.
Read `references/recon.md` when you are about to dispatch a recon,
verifier, or merge subagent. Do not load anything else up front.

## Bootstrap

Run first, before any other action:

    python3 .claude/skills/horizon/scripts/bootstrap.py <role args>

It registers this session, verifies the manifest against reality, prints
the mismatches, and acquires the role lease. Treat the printed reality as
truth and the manifest as a hint. Never execute a predecessor's "next step"
without checking that its premise still holds.

## Durable state

    $(git rev-parse --git-common-dir)/claude-harness/
      env.md            machine facts (daemon, venvs, do-not-touch)
      manifest/         startup index per role, rewritten at rotation
      inbox/            lane reports; hub moves processed ones to inbox/done/
      lease/            who currently owns each role
      context/          latest statusLine snapshot per session
      sessions/         per-session meta
      events.jsonl      user feedback, decisions, rotations, failures, drift
      sessions.jsonl    one line per generation

Task truth lives in `docs/goals/*.md` (frontmatter: status, owner_lane,
depends_on). Code truth lives in git. Nothing durable lives in this
conversation.

## Invariants

- Hub owns judgement and reconciliation; it never edits code or docs.
- Lane owns implementation of its cards and its own continuity.
- A lane owns a file region; cards touching the same region go to that lane,
  serially. `depends_on` is an ordering constraint, not a lane assignment.
- Lane → hub communication is a file in `inbox/`. SendMessage is only a
  nudge; correctness must not depend on it.
- Every role self-rotates by the context thresholds in rotation.md. Do not
  ask the user to rotate you.
- Large tool output stays out of context: tail and counts only.
- Completion requires observable evidence named in the card.
- Material user feedback is logged the moment it is given
  (`scripts/log-event.py user_feedback "..."`).
- Before planned termination, write the session log
  (`scripts/log-session.py`). Not optional.

After each atomic action (a commit, a processed inbox item, a dispatched
agent) run `python3 .claude/skills/horizon/scripts/status.py`. It prints
context usage and whether you still hold the lease. Lost lease → finish
nothing new, end your turn.
