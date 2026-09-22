# ADR 0027 — The daily report indexes the whole day and checks every completion claim before it summarizes

**Status:** Accepted
**Date:** 2026-09-21
**Supersedes:** 0025

## Context

- ADR 0025 capped the material (40 windows, 120 screen groups, 80 records,
  40 commits, 20 sessions), graded each item by the kind of source it
  cited, and let the model's own summary lead the saved report. Measured
  on the real stores for 2026-09-12/19/20:
  - A full one-line index of every window, screen group, record, commit
    and session is 27k–67k characters, smaller than the 65k the capped
    2026-09-20 material already spent, because the caps paid for 200-char
    OCR blobs while dropping 169 of 209 windows.
  - Among the dropped windows: "Thank you for applying! | Jobs at RBC"
    (0.4 min), two co-op application forms, a flight booking. Short items
    are exactly the ones a day's report must not lose.
  - The OCR excerpt was sliced from a summary already cut to 300
    characters including its title, so 1.8 % of the day's OCR text
    reached the model and none of "Application Submitted", "Application
    Received", "Thank You For Applying" did, although those captures were
    listed.
  - Ranking sessions by reply count kept two-turn Codex approval shims
    and dropped four real conversations on 2026-09-12; three configured
    paths are one repository, so every Jarvis commit was listed under
    three directories.
  - The proof label only said what kind of source a part cited. Real
    reports cited an audio-buffer fix as evidence for "UI animation
    polish", three unmerged feature commits as evidence for "merged to
    main", an RBC application form for "module online", and one item's
    prose named a commit that does not exist (838a3ff for 738a3ff).
  - "Code has a same-day commit, so it is completed" was written into the
    skill; a commit proves that a change was committed, nothing more.
- Allen's requirements: no important item may vanish, no completion may be
  overstated, every conclusion needs evidence that really supports it,
  "not verified" and "not done" are different statements, the query budget
  serves verification, and the served summary must agree with what was
  verified. Rules and checks, never longer prompts.
- DeepSeek v4-pro (1M context, tool calls, prompt caching) is the
  configured deep preset; one report a day costs cents.

## Decision

Keep ADR 0024's shape (skill = instructions, runtime = procedure, one flat
tool, saved text composed by code, versions through the briefing store)
and change what the model sees, what it may claim, and what is checked:

1. **Everything in, nothing cut.** Every window, record, commit, state
   event and session turn of the day goes in whole; every capture goes in
   with its whole OCR text after consecutive near-identical captures of
   the same window are collapsed (Allen: "不要做任何截断，全部扔进去").
   No per-section cap, no excerpt. The only limit is the model's context:
   a material budget of 900k characters, and when the whole day exceeds
   it the largest source falls back — screen text first, then Codex
   assistant turns other than the last — to one full line per item (key,
   time, app, title or first sentence), and the header states what was
   recorded but not served in full, with its count and size; those
   originals stay searchable and readable on request. Captures holding a
   milestone phrase (application submitted/received, order placed, sent,
   merged, deployed, passed, failed, error and their Chinese forms) are
   listed again at the top. Configured paths of one repository are listed
   once. Codex approval-shim sessions are folded into one count. The
   header states, per source, whether nothing was recorded, the store was
   unreadable, or everything was served.
2. **`completed` is a claim, not a verdict.** The skill's "same-day commit
   means completed" rule is deleted. Each part's status is the model's
   claim; the saved wording comes from a check:
   - Program checks, before any model call: a completed part with no
     refs, refs that are late commits, or a user record that is a
     question or request, is marked 引用无效 with the reason. A commit
     SHA in the prose that is not a same-day commit is named as an error;
     one that is a same-day commit but not in the part's refs is named as
     uncited. A part about merging whose commits are all on main is
     supported by the repository itself; a part about deployment,
     restart or "online" can only be supported by Allen's own words.
   - A verification call per item with a completed part: the model
     receives the item, each claimed part and the cited originals (commit
     header and stat, record text, capture text, session turns matching
     the item) and answers through `judge_claims` — per part a verdict
     (supported / partial / unsupported), what the evidence actually
     shows, and for screen text whether it is a third-party page or an
     agent's own words.
   - Rendered status per part: 已提交（提交 …，已在/未进 main）·
     用户确认完成（rN）· 页面显示已完成（sN）· 据代理自述已完成，尚未核实 ·
     部分完成：… · 声称完成，引用不支持 · 声称完成，引用无效：… ·
     声称完成，未核实（核查预算耗尽）; parts not claimed completed keep
     浏览/讨论/进行中.
3. **Budget serves the checks.** Drafting: three query rounds, then the
   report, then one retry (five calls). Verification: one call per item
   with a claim, at most twelve, in item order; a provider failure or an
   exhausted budget leaves the remaining claims 未核实, never assumed.
   Summary: one call. Eighteen model calls at most per report.
4. **Summary after verification.** The draft carries no summary. After
   the checks, the program writes the served section from the verified
   table — what is evidenced, what is self-reported or unsupported, what
   is in progress — and asks the model for one sentence on the day's main
   line given that table; a sentence that asserts completion words is
   replaced by a program-built one.
5. The report runs on the deep preset (`daily_report.preset: deep`,
   DeepSeek v4-pro, thinking on, `tool_choice=auto`).

## Alternatives rejected

- **Raise the caps, or index with short excerpts** — the caps did not buy
  capacity: a full index is smaller than the capped material, and any
  cap is a place where a 0.4-minute application page disappears; a short
  excerpt is where "Application Submitted" disappeared. Whole text costs
  more tokens (2026-09-20: ~0.9M characters of collapsed OCR) and DeepSeek
  caches the repeated prefix, so the price is one uncached read a day.
- **Let the drafting model verify its own claims in the same call** — the
  same context that produced the claim grades it; the second-round 9-12
  report shows the model marking every merged commit "in progress" when
  told to be strict. A separate call sees only the claim and the originals.
- **Verify by regenerating the report and diffing** — two drafts disagree
  on item boundaries, not on evidence; nothing in a diff says which
  citation was wrong.
- **Keep the model's summary and police it lexically** — a lexical gate
  cannot tell "code committed" from "deployed" in free prose; building the
  evidence sentences from the verified table makes overstatement
  structurally impossible there, and the one model sentence left is gated.
- **Drop `completed` from the schema so nothing can be overstated** — then
  the report cannot say what Allen confirmed done or what a confirmation
  page shows; the claim is what the check needs.
- **Write "be careful about completion" into the prompt** — the prior
  rounds did; the citations above were produced under those words.

## Consequences

A report now costs up to eighteen model calls and minutes on v4-pro
instead of three calls and half a minute on flash, and a day with screen
capture sends hundreds of thousands of tokens of OCR; prompt caching keeps
the repeated material cheap, and the first read is under a dollar. A
prompt that long strains the model's attention; the milestone lines, the
search tools and the separate verification call are the mitigations, and
the acceptance runs measure whether short items survive. The verification verdicts are model judgment too: a
supported verdict can be wrong, and an unsupported one can reject a true
completion; both are recorded with the reason and the originals stay
citable, so a reader can overrule. The 已在/未进 main facts are a
snapshot at generation time and rot as branches merge. The milestone
phrase list is fixed in code; a page that says it differently is found by
search, not by the index. Claude Code sessions remain unread. Reports
saved under ADR 0025's wording are not rewritten.
