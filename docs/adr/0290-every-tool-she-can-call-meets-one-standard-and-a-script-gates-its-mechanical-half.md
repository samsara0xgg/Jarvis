# ADR 0290 — Every tool she can call meets one standard, and a script gates its mechanical half

**Status:** Superseded-by-0317
**Date:** 2026-10-17
**Supersedes:** none

## Context

- On 2026-10-16 the brain offered 54 built-in tools, 43 of them on the menu
  every request carries (about 9k tokens). Tools overlapped. 「我投了X岗位」
  went to `keep`, not `record_application`. Descriptions ran up to 2,451
  characters (`weather`). They mixed Chinese with English and
  "the user" with "he". Of the 47 built-in tools on today's menu, 32 break
  at least one rule a script can check.
- Tools had no written standard. The spec covered risk levels and the gate
  (`#risk`, `#gate`) but nothing on what a tool must be.
- The standard was drafted from three sources. One was Jarvis's own tool
  contract and exam from 2026-10-11. Another was the leaked Siri and Muse
  prompts taken apart that day. The third was Anthropic's guide on writing
  tools for agents (2025-09-11).
- The owner settled its open points between 2026-10-16 and 2026-10-17:
  - no cap on resident tools;
  - no single speed target;
  - descriptions in English;
  - one web search whose depth she picks (ADR 0289);
  - no Siri-style "three lookups, each a new direction" rule for now.
  He approved the whole standard on 2026-10-17 (「定 按你建议」).
- `scripts/check_adrs.py` set the precedent for a format check. It lists the
  files that predate a standard and fails only new breaks, so the list can
  only shrink.

## Decision

Hold every tool the model can call to `docs/spec.html#tools`. In the gate,
`scripts/check_tools.py` checks its mechanical rules for every built-in tool
on the menu:

- a lowercase snake_case name;
- an English description of at most 600 characters that says "he";
- a description on every parameter.

The 32 tools that break a rule today are listed with those rules. Each
leaves the list when it is rebuilt, and none joins it.

- A new or rebuilt tool lands with an ADR. The ADR names the need and three
  of his own phrasings, says why the per-turn state and existing tools cannot
  answer, and states where the tool sits.
- It also brings at least five exam items from his real conversations. Each
  must pass three runs out of three on the current brain model, with no
  regression on the rest of the exam.
- Plugin tools (`mcp__`) are their servers' definitions and are not checked.

## Alternatives rejected

- **A cap on resident tools.** The owner declined one (2026-10-16). The
  measured cost of the menu is its token count, and ADR 0287 already
  removes the deferred tools from the cached prefix. A cap would push a
  tool he uses off the menu to stay under a number.
- **Rewriting all 32 descriptions now.** Most of them break the rules
  because the tools themselves overlap (`recall`, `search_records` and
  `conversation`, for one), so a new description would be rewritten again
  when the tools merge. They are fixed as each is rebuilt.
- **Siri's limit of three lookups, each in a new direction.** ADR 0240
  already refuses an identical repeat, and ADR 0178 caps a spoken turn at 8
  tool calls. Nothing in his logs yet shows a turn wandering through
  different lookups.
- **Checking the rules by review alone.** The 2,451-character `weather`
  description passed review on 2026-10-16. A length is a number a script
  reads.

## Consequences

- Each rebuild costs a paid exam on the brain model (Sonnet). Every such
  run needs the owner's consent, so a rebuild cannot land without asking.
- The script checks form, not judgment. Overlap, honest read-only labels and
  whether a return is speakable stay with review and the exam.
- The system prompt holds the shared rules once, and a tool description may
  not repeat them. Changing those rules is a prompt change, which goes to
  the owner first.
