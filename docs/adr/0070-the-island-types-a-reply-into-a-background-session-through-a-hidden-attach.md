# ADR 0070 — The Island Types a Reply into a Background Session Through a Hidden Attach

**Status:** Accepted
**Date:** 2026-09-26
**Supersedes:** none

## Context

- Allen, 2026-09-26, on the notch lab (artifact LpiL82fsheAM4GvmYN384X, rounds
  9 and 10): from the island's list, open a session's page and answer it
  there without going to Ghostty; the conversation both in the island and in a
  window ("两个都要"). New sessions move to the Agents window, which drives
  them through the official SDKs; that is another session's work.
- 18 of his 19 sessions that day were `claude --bg` sessions. Claude Code
  offers no way to send input to a running background session except its own
  client, `claude attach <job>`, which draws the session in a terminal.
- Probes on 2026-09-26 with throwaway sessions: Ghostty's `input text` into a
  tab showing the session delivered the line, but only a tab already attached
  can take it, and `new tab` raised Ghostty and took Allen's screen twice; a
  pseudo-terminal the prober owned, running `claude attach`, given the line,
  Enter and then a hang-up, delivered it with nothing on screen and the
  session still running.
- Later on 2026-09-26 the daemon's own code did the same: the line was in the
  session's transcript 4.4 s after the request and the session answered it.
- A background session's transcript records each line it receives as a user
  entry; the terminal screen is the only other sign.

## Decision

The daemon types Allen's line into an idle background session by running
`claude attach <job>` on a pseudo-terminal it owns, writing the line and
Enter, and closing the client, and reports success only once the line is in
the session's transcript.

Limits: only background sessions at their input box (not working, no dialog
open); interactive sessions are answered in their own terminal; one line, one
reply at a time; Codex threads are read in the island and answered in Codex.

## Alternatives rejected

- **Ghostty `input text`** — it needs a tab already attached to the session,
  and opening one raised Ghostty over whatever Allen was doing in both probe
  runs.
- **Drive the session through the Agent SDK** — a `claude --bg` session is not
  a child of the SDK, which can only start or resume sessions itself; resuming
  one that the background supervisor still runs would put two writers on one
  session.
- **Report success once Enter is written** — the attach client gives no sign
  that its input box took the keys; without the transcript check a dropped
  line looks the same as a delivered one.

## Consequences

- A reply takes about 4 to 5 s, most of it waiting for the attach client.
- A terminal tab showing the session shows the typed line too.
- A change in how `claude attach` starts can break replies; the transcript
  check turns that into an error on the island, not a silent loss.
