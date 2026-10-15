# ADR 0258 — Core memory holds up to forty thousand characters in eleven capped sections

**Status:** Accepted
**Date:** 2026-10-15
**Supersedes:** none

## Context

- Core memory (ADR 0146) is the note about Allen loaded into every turn. Its ceiling was 4,000
  characters over six sections; on 2026-10-14 the live note held 3,290 characters, most of them
  project status. Nothing in it said who he is beyond a name, a city, a birthday and his dogs.
- On 2026-10-14 Allen agreed to raise the ceiling to 40,000 characters (「可以」, 23:52Z) and to
  these principles for what she understands about him (「可以」, 2026-10-15 02:40Z): the note holds
  who he is, what he cares about, his patterns and their reasons, while details stay in the raw
  sources with date and source; his corrections are pinned and rank first; memory is append-only,
  keeping how he was then and how he is now.
- A life interview with him started on 2026-10-14. He confirmed its entries as written
  (「可以呀 你记录的不错」, 22:00Z). They are his own words and are worth more than anything a
  nightly pass infers.
- One shared ceiling lets the busiest kind of fact crowd out the rest: on 2026-10-14 project
  status was two thirds of the note.
- The note sits in the system prompt. Decision calls were about 75,000 input tokens on
  2026-10-14; a call that misses the prompt cache costs about $0.19 on Sonnet 5.5 and one that
  hits about $0.012. Two calls 31 s apart on 2026-10-14 both wrote the whole prefix and read
  none of it, because the request carries one automatic breakpoint at its end and nothing marks
  the stable system prompt as a prefix worth keeping on its own.

## Decision

Core memory has eleven sections, each with its own character cap, under a total ceiling of
40,000 characters; nothing in it is overwritten, and what Allen said himself ranks first.

- **Sections and default caps.** 经历 4,000; 性格和价值观 3,000; 身体 2,000; 钱 2,000;
  兴趣和品味 2,000; 你希望被怎么对待 3,000; 定下来的规矩 3,000; 证件和硬期限 1,000;
  承诺和待办 3,000; 身边的人 10,000; 生活里的几件事 7,000. The caps sum to the ceiling and are
  configuration. 身边的人 holds one line per person (relationship, recent events, what to keep
  in mind); 生活里的几件事 one line per thread of his life (where it stands, key dates, what it
  waits on). Full pages per person or per thread are not part of this decision.
- **Old sections map one to one**: 关于你 to 经历, 偏好 to 兴趣和品味, 常提到的人 to 身边的人,
  正在做的事 to 生活里的几件事; the other two keep their names. Older versions load through the
  same map. The live note is refiled once, item by item, as one version whose origin says so.
- **Caps are gates.** A nightly answer that leaves any section over its cap, or the whole over
  the ceiling, is rejected like any other failed gate. The consolidator sees each section's size
  and cap so it can merge or retire items first. 证件和硬期限 refuses an item that carries an
  identity number: it holds dates only.
- **Rank inside a section**: pinned items first (his corrections and his own words), then the
  most recent `since`. Order of use is not tracked and does not rank.
- **Append-only.** Versions stay as they are (ADR 0146). In addition, a rewrite keeps the item's
  earlier wordings, each with the day it held, on the item itself, so how he was then stays
  readable and searchable after the note says how he is now. The prompt shows the current
  wording only.
- **Interview entries** go in as pinned items marked as his own words with the day he said them.
  A nightly pass may suggest one is stale; only he removes it (ADR 0154).
- **Caching.** The stable part of the system prompt, ending with core memory, places and the
  standing ledger, carries its own cache breakpoint. Anything that varies by route or turn comes
  after it.

## Alternatives rejected

- **One shared 40,000 ceiling without section caps** — on the live note one section already
  held two thirds of the space; with a nightly writer that sees project news every day, people
  and history would be pushed out first.
- **Keep six sections and raise the cap** — 关于你 already mixed body, money, taste and
  history; the interview sorts his life into these kinds, and a section per kind is what lets
  each be capped and checked.
- **Trim to cap at render time instead of rejecting** — the dropped items would be invisible
  to every turn while the page still shows them; rejecting keeps what is shown and what is
  loaded the same.
- **Rely on the version table alone for history** — finding what an item said in July means
  walking every version since; keeping earlier wordings on the item is a few lines and makes
  them searchable.

## Consequences

- A full note is roughly 30,000 to 50,000 tokens more per call. With the cache hit that is about
  $0.01 more per call on Sonnet 5.5; a cold call writes it once, about $0.15 more. Cost grows
  with how much he tells her, not with time.
- The memory page, its English labels and the editor's section picker change with the sections.
- When the note passes 10,000 characters, the know-you bank compares 8,000, 20,000 and 40,000
  distillations and the ceiling is set to the smallest that stops gaining.
- `keep` writes to a section the model names; a fact kept without one goes to 经历.
