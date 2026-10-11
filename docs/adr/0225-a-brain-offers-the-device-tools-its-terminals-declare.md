# ADR 0225 — A brain offers the device tools its terminals declare

**Status:** Accepted
**Date:** 2026-10-11
**Supersedes:** none

## Context

- ADR 0170 moved execution of the device tools to the terminal, but left deciding which of them
  exist with the brain's own config. On the Pi (cut over 2026-10-10) that config describes a Mac
  it is not. An audit of the brain on 2026-10-11 found nothing Mac-only run on the brain itself.
  The faults were all a switch read on the wrong host:
  - `search_notes` is offered only when the brain's `tools.obsidian.vault_root` is set, yet it
    runs against the terminal's vault. With the Pi's blank and the Mac's set, she never sees it.
    With the Pi's set and the Mac's blank, a call fails as "not connected", which is false.
  - The Agents page needs `observer.claude_sessions.enabled` on both hosts. With the brain's off
    the page is empty and says nothing, although the brain never reads Claude Code's files.
  - The brain validates its own `tools.open_path` bookmarks at boot, and a relative one stops the
    brain, which never opens anything.
- The terminal already says in its `hello` which tools it runs, built from its own config. The
  brain logs that and otherwise ignores it.
- The owner, 2026-10-11, agreed to the rule 「电脑上的东西，开关和设置只放在电脑上」.
- The menu is part of the cached prompt prefix, so it should not change with every reconnect.
  The state block already says each turn which devices are connected (ADR 0217).

## Decision

On a brain, offer a device tool exactly when a paired terminal has declared it in its latest
`hello`, remembered across disconnects and restarts, and read no device setting from the brain's
own config.

Limits:
- A device tool is one that runs on the terminal, or that needs a terminal operation:
  `screen_look` needs `screen_capture`. Every other tool is the brain's and is unaffected.
- The remembered declarations are per paired device and are forgotten when that device is
  unpaired. Without any, no device tool is offered.
- A terminal that is not connected keeps its tools on the menu. A call then fails with the plain
  "not connected" error, and the state block already says the device is away.
- With a terminal connected that has not declared a tool, the error names that device and says it
  does not offer the tool.
- The brain ignores its own `tools.obsidian.vault_root`, `tools.open_path` and
  `observer.claude_sessions.enabled`. The Agents page is served whenever terminals can connect,
  and whether Claude Code's files are read is the terminal's own setting alone.
- A Mac running alone (`role: all`) is unchanged.
- Plugin packages stay the brain's: the brain runs their servers and skills, so it reads them from
  its own disk. Where they come from is not decided here.

## Alternatives rejected

- **Show device tools only while a terminal is connected** — every connect and disconnect would
  change the tool list at the head of the prompt and miss the provider's prefix cache for the
  next turn, and she would lose tools for a laptop lid closed for a minute.
- **Keep the brain's settings and copy the Mac's values to it** — two copies of one fact drift,
  as they had by the first evening after the cutover.
- **Ask the terminal for its tools at boot** — the brain often starts with no terminal connected,
  and would boot with no device tools until a restart.

## Consequences

- A tool the terminal stops declaring stays off the menu only after that terminal says hello
  again. Until then, a call fails with the device's own error.
- The brain writes one more small file in its runtime root (`terminal-tools.json`).
- Settings for device features have to be changed on the Mac, even when the owner is on the brain's
  Settings page.
