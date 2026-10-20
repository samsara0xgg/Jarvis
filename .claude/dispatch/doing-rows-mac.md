# Mac: the day pane's list as one row per stretch of what he was doing

Worktree: /Users/alllllenshi/Projects/jarvis-wt/notch-timeline-try, branch `feat/notch-doing-rows` (from feat/notch-polish-icon: the polished pane yilun is trying, plus the flat icon). Everything is in `desktop/resonance`. yilun's trial companion runs from ~/Projects/jarvis-wt/notch-timeline: never touch that worktree, never build there. Another worker changes the phone app in another worktree; never touch it.

## What
Implement `.claude/dispatch/doing-rows-spec.md` (beside this file; read it first, it is the whole design, agreed with yilun) in the Mac day pane. The code: `src/dayView.ts` (`DayReply`, `DayItem`, `DoingSpan`, the day model and its rows, the doing wording), `src/barView.ts` (`buildBar`, `peek`, the readout), `src/DayTimeline.tsx` (the list, open/close, `fit`/`slide`), `src/RowOpen.tsx` (an opened row), `src/day-timeline.css`, and the icons. Extend `DoingSpan` with `kind`, `detail`, `confidence`, `clues`, `place_id`, `item_ids` (optional). Reuse what is there: today's per-kind row text for the lines inside an opened segment, the open/close motion and its row-stays-under-the-pointer fit, the rail, `t([en, zh])`, the 这段在干嘛 entry (it now belongs to every segment row up to now). Delete what the new list makes dead; do not keep the old place grouping alive beside the new rows, except for the spec's "no doing segments" case.

## Verify
- `scripts/verify-notch-timeline.mjs`: give its fixture day realistic doing segments (night, ~10 segments including a long work stretch, a meal, rest with phone use, a commute ride, an unknown hole, a low-confidence one, one he stated; pictures, payments, a phone call, messages and media inside via `item_ids`), and checks for: rows = segments (order, titles, `不知道在干嘛`, the line under with place, `不太确定` and the counts, the length); night and plan rows kept; talk never drawn; an opened segment's lines (items by `item_ids`, talk/stay/sleep left out, unknown ids skipped) and the how-she-knows line per basis; bar pieces coloured by category; a reply with no doing spans gives today's list. Adjust existing checks only where they asserted the old list. Full run must pass, plus `verify-quiet-notch.mjs`, `verify-cursor-feed.mjs`, `verify-fit-window.mjs`.
- Screenshots (the pane, one opened work row, one opened rest row) to `.claude/dispatch/shots-doing-rows/`.
- Build: `PATH=/Users/alllllenshi/.local/share/mise/installs/node/24.14.1/bin:/usr/bin:/bin:/usr/sbin:/sbin TMPDIR=/tmp npm run build`. Run scripts with `COMPANION_PORT=5799 COMPANION_DAEMON_PORT=8799 CHROMIUM="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" node scripts/<name>.mjs` (these ports only).

## Rules
- Never launch Electron or the companion. No real brain, no network, no API calls. Never read keys, tokens or env files. Do not call any `mcp__hearthbot__` tool.
- Touch only this worktree. Commits: Conventional Commits in the repository's format (subject; a paragraph on why; a bullet per file; a `Tier 1:` line with the checks and counts), explicit paths, no co-author or AI trailer, one logical change per commit. No push.
- Match the code around you (comment voice, motion tokens in `motion.ts`, `t([...])`).

Report: commits; what each check asserts; screenshot paths; decisions where the spec was silent; gaps; under 300 words.
