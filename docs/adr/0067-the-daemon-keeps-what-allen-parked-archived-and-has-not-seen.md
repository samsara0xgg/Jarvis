# ADR 0067 — The Daemon Keeps What Allen Parked, Archived and Has Not Seen

**Status:** Accepted
**Date:** 2026-09-26
**Supersedes:** 0057

## Context

- ADR 0057 kept Allen's turn in the companion's own profile: which finished or
  stopped sessions he has not seen, and which he cleared from beside the notch.
- Allen, 2026-09-26, on the notch lab 刘海里的任务 (artifact
  LpiL82fsheAM4GvmYN384X, rounds 7 to 10): a place to park what he will not
  handle now, out of his turn and quiet until he takes it back ("只要一个 park
  就行"); build rounds 7 to 10 ("第七到第十版全做").
- The same day a separate Agents window was started (another session: its own
  window and a long-lived agent host), with an archive the terminal's Agent
  View lacks. Asked whether the notch's clear is that archive, Allen split it
  in two: "暂存：我等一会儿处理；归档：应该已经干完了，不用了，就直接扔掉。留着不删的
  唯一好处就是，未来如果说要回忆的话，可能可以找到".
- The companion's profile is Electron `localStorage`, readable only inside its
  own renderer partition; the agent host is another process.
- ADR 0057's reasons for the companion deciding what is unread still hold: the
  daemon builds the session board only when someone reads it, so it sees no
  transitions of its own, and only the GUI app has the macOS Automation grant
  that reads Ghostty.

## Decision

The daemon keeps, per session id, whether Allen has not seen a session, when
he parked it and when he archived it, in one file every surface reads and
writes; the companion still decides what is unread and seen: a session that
finishes or stops while he is not looking is unread, and 1.5 s on its Ghostty
terminal or on its page in the island, or opening it, sees it.

Limits: a mark nobody writes for 30 days is dropped; a mark belongs to one
session id, so a session resumed under a new id starts clean.

## Alternatives rejected

- **Keep the marks in the companion's profile (ADR 0057)** — the Agents
  window's host is a separate process and cannot read the companion's
  `localStorage`, so an archive made in the notch would not show in the
  window's Archived list.
- **One put-away: the notch's clear is the archive, parking lives only in the
  companion** — Allen named two different intents (handle later, done with);
  parked sessions kept only in the companion would vanish from the window,
  where he would look for "the ones I set aside".
- **The daemon derives unread from a session's finish time against the last
  time it was seen** — `updated_ms` is the newest of transcript mtime and the
  job's `updatedAt`, which move without Allen seeing anything, so a seen
  session would come back as unread.

## Consequences

- The companion reads the marks once when she starts; a mark another surface
  writes while she runs shows after her next start, and her next change to
  that session writes all three of its marks back as she last knew them.
- The file sits in the state layer beside the plugin settings file; it is not
  in the event log, so nothing replays how the marks changed.
- A second daemon root (a test rig) keeps its own file, so its marks are not
  Allen's.
- Claude session ids and Codex thread ids share one key space in the file.
