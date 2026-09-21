# ADR 0024 — The backend session request: rules and profile, history, state on the user message

**Status:** Accepted
**Date:** 2026-09-21
**Supersedes:** none

## Context

- Allen settled the backend session prompt on 2026-09-21 ("System Prompt
  就采用我最后发的简洁 v1", then "go直接开干" on the seven-step plan, "用 B，
  仓库状态卡删掉", "之前的老记忆先别删但是也先别用了 就用一周前开始的吧").
  The new text is a personal-assistant identity in Chinese, ~1.2k characters,
  and says "遵循本轮交互方式和输出格式要求" and "遵循 Profile 中的语言偏好"
  — it presupposes that the request carries a profile and a per-turn state.
- The request before this decision: an English 9.9k-character prompt aimed
  at a coding command center, then six to seven consecutive `user` messages
  — the whole memory.db history as one text block (57,954 characters, all
  of it recorded during testing since 2026-09-12), a time line, and up to
  five `[system context]` cards (repo Status Board, open actions, open
  tasks, evidence, pending confirmation) — then the utterance. Dynamic
  content sat ahead of the stable block, the inverse of spec §10.5, so the
  provider's prefix cache covered little.
- The `<voice>`/`<document>` envelope was taught only in that prompt file.
  Without the tags the splitter returns the whole text to both channels.
  Voice is moving to the GPT-Live provider, which re-voices the backend's
  answer (ADR-0016 D4), so the backend has no reason to shape speech.
- ADR 0019 had already reduced the Pre-emit Gate to a routine stamp: no
  completion-keyword interception, retry or refusal template remains. Two of
  the five cards (open tasks, evidence) went with it.
- hermes-agent (checkout of 2026-09-10) builds its system prompt once per
  session with identity, tool guidance and the user profile in a stable
  tier, and injects every per-turn addition into the current user message
  behind a fence that says it is not user input; history is one row per
  message. Its per-turn state never becomes a separate message.
- Two providers in use accept consecutive `user` messages; nothing
  guarantees the next one will.

## Decision

The backend request is four parts, in this order: the `system` field is the
rules file plus the profile block (`[关于 Allen]`, one line per `profile`
row, omitted when empty); the first `user` message is the history block —
the current summary, then every `records` row after the summary anchor and
on or after `session.history_since`, with the retired envelope tags removed
from the text; the last `user` message is this turn's utterance with one
`[当前状态｜程序提供，不是用户说的话]` block prepended, holding the time line,
the interaction mode derived from the trigger's channel, a pending
confirmation ask if one is live, and a one-line count of actions still
running; `tools` is the registry surface for `jarvis_llm`. No other note
enters the request: the Status Board card is retired (the observer keeps
feeding the dashboard). Records before `history_since` stay in memory.db,
reachable through `search_records` / `read_records`, and are never shown to
the model nor folded into a summary. The backend is not asked for
`<voice>`/`<document>`; the splitter's tag-less path is the normal one.

## Alternatives rejected

- **The per-turn state as its own `user` message before the utterance** —
  same content for the model, but every turn then carries two adjacent
  `user` messages; a provider that merges or rejects them changes behaviour
  silently. One message per turn has no such case.
- **A rolling seven-day window instead of a fixed start** — the summary
  anchor and a rolling floor drift apart: rows older than the window but
  newer than the anchor would be neither summarised nor shown, which is the
  gap the verbatim-until-compacted design of the summaries table exists to
  prevent.
- **Delete the pre-cutoff rows** — 156 rows / 37,842 characters of test
  runs are still the only record of what was tried; `read_records` reaches
  them by id and the cost of keeping them is zero once they are out of the
  prompt.
- **Keep the envelope requirement in the backend prompt** — 190 tagged
  answers in the history would keep teaching the tags by example, and the
  backend would keep spending output on speech shaping that Live discards.
- **Keep the repo Status Board card** — it answers "仓库现在什么状态" for a
  coding command center; the settled identity is a personal assistant, and
  the card cost a stale-rule, a poll-interval plumb-through and one message
  every turn whether or not a repo mattered.
- **Profile as the first `user` message** — the model sometimes answers it;
  spec §10.5 lists the stable profile in the cached head, which is the
  system field.

## Consequences

- The local MiniMax speech chain now receives the whole answer as speech
  until the voice provider boundary replaces it; that chain was already
  slated for replacement by Live.
- The history block is still one text block, not one message per row; tool
  calls and results are not in memory.db. The role-based history with
  provenance columns is a separate decision.
- `history_since` is a hand-set date in `config/jarvis.yaml`; moving the
  start of history is an operator action, and the Live brief (ADR-0016 D7)
  does not read it.
- The time line carries minutes and a "距上次交流" gap, so the tail of the
  request changes every turn; only the system field and the history block
  are cache-stable, by design.
- The pending-confirmation and running-actions lines now render in Chinese
  inside the state block; ADR-0012 D4's wording and ADR-0009 D6's note
  rendering are replaced here, their files stay as written.
