# ADR 0303 — A daily report of his Claude Code work is written on the Mac and read into the archive

**Status:** Accepted
**Date:** 2026-10-19
**Supersedes:** none

## Context

- ADR 0294 copies each Claude Code session's metadata to the brain and none of what was said.
  On 2026-10-19 he said the metadata alone tells her little: he spends most of his day in
  Claude Code (two accounts, the desktop app's Code tab and this project's threads on his Mac),
  and what she needs is what each line of work got done, decided and left open, per project, per
  day.
- The transcripts are on his Mac (`~/.claude/projects`, `~/.claude-b/projects`): about 1.2 MB of
  dialogue text a day once tool calls, tool output, thinking and subagents are left out (7 days
  measured 2026-10-19: 920 MB of files, 8.7 MB of text).
- He asked for the report to be written with his subscription's Opus 5.5 and subagents, not the
  API: a Claude desktop scheduled task runs on the Mac with his quota, which he says is ample. A
  scheduled task runs only while the app is open and catches up on the next launch.
- He often sleeps at 2-3 a.m.; a day cut at midnight puts his evening's work into the next day.
  The brain holds his sleep blocks (ADR 0293) on the day line.
- The archive already reads one daily report shape (ADR 0295's letter) into `import_entries` and
  draws it on the day line; the brain is on the tailnet and the Mac terminal already ships to it
  with its device token (ADR 0294), whose file route takes images only.
- Tool output can hold secrets; a model asked not to copy them may still copy them.

## Decision

The Mac cuts each day's dialogue text by a fixed program, a scheduled Claude task writes the
day's report from it, and the brain reads the report like the Claude chats letter.

- **His day** ends when he falls asleep: the start of the longest sleep block on the day line
  that begins between 18:00 that day and 14:00 the next; with none, 05:00 the next day. Day D
  runs from day D-1's end to day D's.
- **Material.** `python -m jarvis.surface.claude_code_day material` writes, for each closed day
  of the last 7 with no report, one file per session with the day's messages: his typed words
  and Claude's text replies, timed, with the session's title, project folder and account. Tool
  calls and results, thinking, subagent transcripts, command output and injected reminders are
  never read into it. The report task's own sessions are left out by a marker in its prompt.
- **Report.** A scheduled task (Opus 5.5, subagents allowed) reads the material and writes
  `~/.jarvis/claude-code-days/<date>.md` in ADR 0295's letter shape with the header
  `jarvis-claude-code-daily: 1`: one `##` section per project (it groups sessions by what they
  were about, not by folder) with `sessions`, `time`, `summary`, `decisions`, `open`, `learned`
  (what the work shows of his preferences) and `mentions`.
- **Shipped and read.** The terminal ships each report file whole when its bytes change, under
  source `claude-code-daily` (Markdown, at most 256 KiB). The brain reads every new or changed
  file with the letter's parser into `import_entries` as source `claude_code_daily`; a rewrite is
  a new revision and the newest stands for the day. The day line draws it as kind `claude_day`
  with id `claude-code-day-<date>`; the archive tool takes the source.
- **The dialogue is kept.** The same command writes `dialogue/<date>.md` beside the material: the
  day's session files one after another, each under its `# Session <id>` header, after a preface
  with the day, zone and window. Unlike `material/`, which the report task deletes, it stays on
  the Mac, and the terminal ships it (at most 8 MiB) as `dialogue/<date>.md` under the same source.
  The brain reads it into `import_entries` as source `claude_code_dialogue`, one `session_dialogue`
  entry per session (key: day, session, hash of the section, so a rewrite adds only changed
  sessions), keeps each version raw, and does not put it on the day line or in the semantic
  index. The `archive` tool finds it by words and shows a short stretch around the match; it is
  read only on demand, never put into her context.

## Alternatives rejected

- **The brain writes the report with the API.** Haiku writes a thinner report than Opus, Opus
  through the API is ruled out (2026-10-12), and the transcripts would first have to travel to
  the Pi.
- **The scheduled task reads the transcripts itself.** A model choosing what to read has no
  fixed boundary: the day cut, the subagent and tool-output exclusions would hold only as well as
  it follows its prompt.
- **One section per session.** He has dozens a day, many a single question; per project shows
  what moved.
- **Cut at midnight.** His work after midnight belongs to the evening before.

## Consequences

A day's report exists only after the Mac app has run the task; a closed laptop delays it, and
a day whose sleep has not reached the brain is cut at 05:00. The report is a model's reading:
what it leaves out is not in the report, though the cut dialogue is in the archive and the
transcripts stay on the Mac. The dialogue archive grows by about 0.1 to 0.6 MB a day (measured
2026-10-19: 0.04 to 0.3 MB for six days) in `memory.db` and the same again raw.
