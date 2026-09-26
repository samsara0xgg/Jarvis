# Report format

## The fields of `report_daily_work`

The draft has no summary field: the summary is written by the runtime after it has
checked every claim of completion.

- `items`: merged by work item; several activities on one thing make one item. Each has:
  - `title`: the item's name (at most 60 characters).
  - `activity`: what was done, what was produced, how far it got; facts, not judgments.
    Every key fact carries how its source is named, see below. Any commit number written
    in the text must be one of that day's commits in the material and must appear in this
    item's `refs`; the runtime checks, and a wrong or uncited number is named under
    uncertainty.
  - `progress`: the item's progress, split by part, one part per entry:
    - `part`: the part's name, such as code, tests, deployment, merge, application; null
      when the item has only one part.
    - `status`: `browsed` / `discussed` / `attempted` (tried or in progress) /
      `completed`. `completed` is a claim: this part's product exists — a commit of that
      day, a sent application, the user saying it is done, a page showing it submitted —
      and this entry's `refs` cite the material that supports it.
    - `refs`: the material keys behind this part.

    When one thing has development, tests and deployment, split it into entries, each
    with its own status and citations. A commit proves only that its change was
    committed, not tests, a deployment or a merge; only the user's own words can prove a
    deployment, release or restart happened. Do not write committed code as `attempted`
    because the deployment lacks evidence, and do not let one `completed` cover a
    deployment without evidence.
- `decisions`: important decisions and changes of plan. `rationale` only when the
  material gives grounds, otherwise null.
- `open_items`: unfinished items, blockers, questions to confirm.
- `user_next_steps`: next steps the user stated outright; must cite the user's own
  record keys.
- `suggestions`: your own suggestions from the material, kept apart from the user's
  commitments.
- `uncertainties`: what is uncertain, conflicting evidence, gaps in the material.

## Naming sources in the text

When the text states a fact, put its source in the sentence, so someone reading only the
text can tell:

- A commit: `commit e17fbd2 (that day, on main)` / `commit 53a8e97 (that day, not on
  main)`.
- The user's own words: `the user said … (r3)`.
- An agent's (Codex, Claude …) own account in a terminal or session: `according to
  Codex, 979 tests passed (unchecked)`.
- What was seen on screen: `the screen showed …`.

## How the runtime checks `completed`

For every part marked `completed`, the runtime first rules by program: citing no key from
the material, citing only an old commit first seen that day, citing the user's question
or request, or using a commit as evidence of a deployment / release / restart are all
"citation invalid"; a "merge" part whose cited commits are all on main is proven by the
repository itself; a part citing only Codex sessions is "by the agent's own account, not
yet verified". Every other part goes, item by item, to an independent check call: the
checker sees only this item, the parts it claims and the cited originals (commit content,
record text, full capture text, the session's relevant turns), rules supported / partial
/ unsupported, and tells a third-party page from an agent's own account on screen. The
saved status text comes from this check, not from the `status` you filled in:

- committed (commits …, on main / not on main) · the user confirmed it done (rN) · a page
  shows it done (sN)
- done by the agent's own account, not yet verified (cN / sN)
- partly done: <the scope the originals actually show>
- claimed done, the citation does not support it (the original shows: …) · claimed done,
  citation invalid: <reason>
- claimed done, not verified (check budget used up / check failed)

`browsed` / `discussed` / `attempted` are not checked and are written as browsed /
discussed / attempted or in progress.

## The saved report (assembled by the runtime)

Headings and the runtime's fixed wording follow the user's language setting:

```
# Work report <date> (<zone>)
generation time, evidence window, model

## Summary
<one main-line sentence, written by the model after the checks; rewritten by the program when it asserts done / deployed / merged / passed>
Evidenced: 1 <item> (committed (commits e17fbd2, on main); the user confirmed it done (r3)); …
Claimed done but unverified or not upheld: 2 <item> (tests: done by the agent's own account, not yet verified (c4)); …
Also N in progress, N discussed, N browsed; see Work items.
## Work items
### 1. <item> — committed (commits e17fbd2, on main); the user confirmed it done (r3)
<activity and progress>
Sources: <citations>
### 2. <item> — code: committed (commits 53a8e97, not on main); tests: done by the agent's own account, not yet verified (c4); merge: claimed done, citation invalid: commits 53a8e97 are not on main
## Decisions and changes of plan
## Unfinished, blocked and to confirm
## Next steps the user stated
## Suggestions (from the model, not the user's commitments)
## Data coverage and uncertainty
## Sources
```

The summary lists only the evidenced items and the items claimed done but unverified or
not upheld; a mixed item is listed once, its title followed by each part's status text;
the rest are counted by their highest status. Parts claimed done without evidence are also
named, by item number and part name, under data coverage and uncertainty.

Entries of `user_next_steps` that cite none of the user's own words are removed from that
section and quoted under uncertainty.

Citations are numbers in the text (`Sources: #3, #7`); the full sources are listed one by
one at the end of the report.

A report over the text budget fails and the old version stays; the text and the source
index are never cut to save it.
