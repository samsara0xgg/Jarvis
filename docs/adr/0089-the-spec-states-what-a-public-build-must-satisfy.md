# ADR 0089 — The Spec States What a Public Build Must Satisfy

**Status:** Accepted
**Date:** 2026-09-29
**Supersedes:** none

## Context

- On 2026-09-29 Allen set the goal the spec now serves: anyone can download
  Jarvis and run it with one API key of their own, starting with Mac users of
  Claude Code and Codex. Offered three ways to bring the spec along (rewrite a
  slim v2 on a ten-chapter outline and archive the old file whole, prune it in
  place, or leave it until the first release), he chose "重写精简版".
- The review behind that choice
  (https://claude.ai/artifact/TFxXHx3Xkuv9oWv9pHehV2, main at 3aa0949) sorted
  the 216 sections of `docs/spec.html`, 153,400 characters of text, into four
  kinds: 25% the architecture and interfaces in use; 38% one feature's details
  (§3.3.5a, a table of tool inputs and outputs, is 21% alone); 29% never built
  or since retired (the Raspberry Pi federation, modes, attention scoring,
  typed memories, the Situation Packet, execution leases and sandbox); 8%
  positioning written for Allen alone. No section covered where data goes,
  threats, keys and spend, installing and removing, or the Claude Code and
  Codex interfaces the product depends on.
- 36 of the 95 commits on main from 2026-09-25 to 3aa0949 edited the spec,
  most of them following a UI change. README rule 5 sent every field list and
  endpoint there, as a second copy of what the code defines.
- Outside `docs/`, 107 files carry 543 `§` citations, about 300 of them into
  the spec and the rest into ADR sections; the ADRs cite the spec too. A new
  document under the old name with new numbers would re-point every one of
  those citations without a diff showing it.
- ADR 0019 warned that a spec which stops following the code still reads as a
  contract.

## Decision

Replace `docs/spec.html` with a v2 that states only what a public build must
satisfy, in ten chapters: who it serves, its processes, the state spine, the
rules for acting, what leaves the Mac, threats and guards, keys and spend, the
Claude Code and Codex interfaces, installing, updating and removing, and
surfaces and attention. v1 moves unchanged, apart from an archive banner, to
`docs/spec-v1.html`, where every `§` citation written before this ADR
resolves, as background and not contract. v2 is cited by anchor
(`docs/spec.html#egress`), never by `§`.

Its limits:

- v2 holds a format or a list only when another program depends on it (the
  Claude Code hooks, the Codex app-server, the model providers) or the owner
  needs it to know what leaves the Mac. For data that outlives a build (the
  event log, memory.db, settings) it holds the rules that keep the data
  readable, and the code holds the lists. What one build's processes say to
  each other is defined by the code that says it.
- Where an accepted ADR sets a rule the code does not meet yet, v2 says so
  beside the rule.

## Alternatives rejected

- **Prune v1 in place** — keeps the file name and most numbers, but with 29%
  of the text deleted and five chapters added, most chapters from §1 to §19
  change, so an old citation would still resolve to a section that no longer
  says what it said when the code cited it.
- **Leave v1 until the first release** — the spec keeps describing a
  Raspberry Pi federation, modes and leases that do not exist, and the topics
  a stranger's trust depends on stay unwritten while the release inventory
  already carries items (改14, 改15, 改17) that no rule answers.
- **Delete v1 or move it out of the repository** — about 300 citations in
  code, config and tests would dangle; rewriting them touches over a hundred
  files and changes no behaviour.
- **Keep README rule 5 as written** — one feature's details were 38% of v1
  (57,484 characters) and brought 36 of 95 recent commits into the spec, each
  a copy of something the code defines and can drift from.

## Consequences

- Two spec files sit side by side. A reader following an old `§` citation
  lands in a document that no longer binds, and has to check v2, the code and
  the ADRs for what holds now.
- README rule 5 changes: a wire format, field list or registry is owned by the
  code that defines it, and by the spec only on this ADR's terms.
- v2 says less than v1 did. A feature whose only written contract was a v1
  section now rests on its code, its tests and its ADR.
- A change in flight that adds a field list or an event type to v1 has to put
  it in v2 on the terms above, or leave it to the code.
