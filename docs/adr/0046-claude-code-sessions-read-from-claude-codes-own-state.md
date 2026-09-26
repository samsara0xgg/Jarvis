# ADR 0046 — Claude Code Sessions Are Read From Claude Code's Own State

**Status:** Accepted
**Date:** 2026-09-25
**Supersedes:** none

## Context

- Allen, 2026-09-25: his Claude Code sessions should reach Jarvis the way
  his Codex sessions do, so the Resonance Agents page (design lab
  N2mxc7W2jBstwBZJh53dsN) shows every agent in one window and replaces
  Open Island. Then: "你不用太管 Codex 是什么样子，自己做好就行", the two
  are different systems.
- The page needs, per session: needs-you / working / done, a title, project
  and branch, where it runs, his last words and what it is doing now.
- Allen works mostly in background sessions: on 2026-09-25
  `claude agents --json` listed 25 background sessions and 1 interactive.
- Claude Code already keeps this state itself. `claude agents --json` is
  the CLI's own listing: every live interactive and background session with
  `status` (busy / idle / waiting, plus `waitingFor`), and for a background
  job the agent-view `state` (working / blocked / done). Each job's
  `~/.claude/jobs/<id>/state.json` holds a one-line `detail` and a `tempo`;
  each transcript carries `last-prompt` and `gitBranch` entries.
- Hooks only see events. The Stop hook does not fire on an Esc interrupt and
  nothing fires when a terminal is closed or the process killed (found in
  the 2026-09-23 Open Island comparison), so a hook-fed row can stay
  "running" forever. No hook payload carries the session name or a
  background job's blocked / done state. Every hook event in
  `~/.claude/settings.json` already runs Vibe Island's bridge.
- The daemon runs under launchd, whose PATH has no `~/.local/bin`.

## Decision

Read Allen's Claude Code sessions on request from Claude Code's own state:
`claude agents --json` for the roster and live status, the job's
`state.json` and the transcript tail for the rest; install nothing into
Claude Code and write none of its files.

Limits: read-only; it answers no permission request and does not yet jump to
a session; the board is rebuilt at most every 3 s.

## Alternatives rejected

- **Hooks POSTing to the daemon, like Codex** — rows go stale after Esc and
  after a closed terminal (no Stop, no SessionEnd), the name and job state
  are missing, and it adds a second handler to every event in a settings
  file Vibe Island already hooks.
- **Reading `~/.claude/sessions/*.json` directly instead of the CLI** — on
  2026-09-25 that directory held 49 registry files for the 26 sessions the
  CLI listed, and `~/.claude/jobs` 44 jobs for 25 listed; Jarvis would
  have to redo Claude Code's own liveness and archive filtering.

## Consequences

- The job `state.json` fields and transcript entry types are undocumented.
  A Claude Code update can blank the prompt, branch, activity or answer
  columns without an error, while the phase, which comes from the CLI, keeps
  working.
- It is polled, not pushed: a new request for Allen shows up within the
  page's poll interval plus up to 3 s, and each rebuild spawns the CLI
  (0.19 s wall measured).
- A transcript line longer than the 512 KB tail (a huge tool result) hides
  the prompt and branch until newer lines are written.
