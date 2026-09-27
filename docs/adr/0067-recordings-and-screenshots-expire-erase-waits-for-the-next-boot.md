# ADR 0067 — Recordings and screenshots expire; erasing everything waits for the next boot

**Status:** Accepted
**Date:** 2026-09-26
**Supersedes:** none

## Context

Before the public app, every recording (`memory/audio`, 197 files on Allen's Mac) and every
screenshot (`artifacts/screen_artifacts`) was kept forever, and nothing could take the user's
data out or delete it. The daemon's stderr went to one `logs/daemon.err.log`, appended by
launchd or by the Electron app, never cut (2 MB after two weeks), and since INFO became the
daemon's default on 2026-09-24 it held every recognised sentence and every GPT-Live user
fragment.

On 2026-09-26 Allen accepted the proposal: recordings 30 days and screenshots 7 by default,
changeable to 7, 30, 90 days or forever under Settings > Privacy & data; an export zip; one
button that clears recordings and screenshots, one that clears everything like a factory
reset; logs cut at 10 MB with 3 files kept; logs that say what happened, never what was
said. The page itself waits for Codex's design; this ADR covers the backend.

Constraints:

- `memory.db` and the event log are append-only (the event log's triggers refuse DELETE),
  and `records.audio_path` has no reader: nothing plays a recording back.
- The event log is open from the first moment of `bootstrap_runtime_app` until exit, on
  several threads; the daemon cannot close every connection and keep answering.
- launchd, the Electron app and every stdio MCP server the daemon starts hold the log
  open for append. Only launchd or the app can reopen it, at a respawn.
- The speech models under `models/` are 229 MB fetched at first boot, and the Electron app
  creates `logs/` once per launch, not per daemon respawn.

## Decision

Delete recordings and screenshots by file age once at boot and then hourly, leaving the
`memory.db` rows that name them; cut each `logs/*.log` past 10 MB by copying it aside and
truncating it in place; and carry out "erase everything" at the next boot, from a marker
file, before anything under the runtime root is opened, keeping only `models/` and `logs/`
and deleting the root's Keychain item.

## Alternatives rejected

- **Delete the `records` rows with their recordings** — the conversation is what the
  prompt and search read; a 30-day audio rule would silently shorten the history, and
  `memory.db` would stop being append-only for a column nothing reads.
- **Erase while the daemon runs** — unlinking the open event log leaves every thread
  writing a file that no longer exists, and the next open creates a fresh one beside
  connections still on the old inode; a boot has no open connections.
- **Rename the log and start a new one** — every writer keeps its descriptor and goes on
  appending to the renamed file, so it grows without bound under the new name.
- **A Python `RotatingFileHandler`** — it cuts only what `logging` writes; PortAudio,
  uncaught tracebacks and the MCP servers' stderr still land in the unbounded file
  (210 MCP validation dumps in `daemon.err.log` on 2026-09-26).

## Consequences

`records.audio_path` points at files that may be gone. The cut runs at most hourly, so a
log can pass 10 MB by one hour's writing, and lines written between the copy and the
truncate are lost. Erasing needs a restart, so the route exists only where launchd or the
app brings the daemon back. The Electron app's own storage (home layout, panel settings)
is not under the runtime root; the Settings page must clear it itself when it wires the
button. Logs written before this change still hold speech until they are cut.
