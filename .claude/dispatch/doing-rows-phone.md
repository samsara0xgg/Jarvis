# Phone: the day list as one row per stretch of what he was doing

Worktree: /Users/alllllenshi/Projects/jarvis-wt/phone-timeline, branch `feat/timeline-doing-rows` (from origin/main 7c5ea67e). Phone app only (`ios/`). Another worker changes the Mac app in another worktree; never touch any other worktree.

## What
Implement `.claude/dispatch/doing-rows-spec.md` (beside this file; read it first, it is the whole design, agreed with yilun) in the phone timeline. The code: `ios/Jarvis/Sky/DayTimelineModel.swift` (`build`, `arrange`, `row(...)`, `doingWords`, `TLEntry`, `TLSeg`, `model.doing`), `DayTimelineRows.swift`, `DayTimelineRowViews.swift`, `DayTimelineOpen.swift` (an opened row), `DayTimelineBar.swift`, `DayTimelineActions.swift`, and the decoding of the day reply (`DayReply`, spans: add `place_id`, `item_ids`, `detail`, `confidence`, `clues` where missing, all optional). Reuse what is there: today's per-kind row text for the lines inside an opened segment, the existing open/close, the rails' look, `tlDur`, the 这段在干嘛 entry (it now belongs to every segment row up to now). Delete what the new list makes dead; do not keep the old place grouping alive beside the new rows, except for the spec's "no doing segments" case.

## Verify
- Unit tests in `ios/JarvisTests` (extend `DayTimelineTests` or a new `DayTimelineDoingRowsTests`): rows from a reply with doing segments (order, titles, `不知道在干嘛`, the line under with place and `不太确定`, the counts, the length); the night and the plan rows kept; talk never drawn; an opened segment's lines (items by `item_ids`, talk/stay/sleep left out, unknown ids skipped), the how-she-knows line per basis; bar segments by category colour; a reply with no doing spans gives today's list.
- A debug scene in `Sky/StarTimelineScene.swift` (`时间线:按在干嘛`) with a realistic day: night, ~10 segments including a long work stretch, a meal, rest with phone use, a commute ride, an unknown hole, a low-confidence one, one he stated; pictures, payments, a phone call, messages and media inside. Screenshots (list, one opened work row, one opened rest row, the bar) to `.claude/dispatch/shots-doing-rows/`.
- Full `JarvisTests` pass and the Release build compiles: `/tmp/claude-501/b.sh build|rel|test` (it points at this worktree). Boot the simulator 7B18D7B7-2106-4E1E-A76E-978DE49104EB only for a run and shut it down right after (`xcrun simctl shutdown 7B18D7B7-2106-4E1E-A76E-978DE49104EB`): the Mac is short of memory.

## Rules
- Never install to the phone, never launch anything on yilun's devices. No calls to the real brain or any API, no network. Never read keys, tokens, env files or the Keychain. Do not call any `mcp__hearthbot__` tool.
- Touch only this worktree's `ios/` (and `.claude/dispatch/`). Commits: Conventional Commits (`feat(ios): ...`) in the repository's format (subject; a paragraph on why; a bullet per file; a `Tier 1:` line with the check and its count), explicit paths, no co-author or AI trailer, one logical change per commit. No push.
- Match the code around you: its comment voice, design points (440 x 956), the app's dark look.

Report: commits; what each check asserts; screenshot paths; decisions where the spec was silent; gaps; under 300 words.
