# ADR 0160 — Every proactive card takes the same feedback row and snapshot

**Status:** Accepted
**Date:** 2026-10-04
**Supersedes:** none

## Context

- Allen, 2026-10-04, via the coordinator: the 「合适吗」 row of ADR 0155 and 0159
  (对 and five levels: 记下, 亮一下, 卡片, 卡片带声, 开口) is how he tunes what
  reaches him, and real clicks should be the main data. Job mail has it; the
  agent notices, the quiet digest and the night cards, which fire far more
  often, have none, so the log holds one kind of card only.
- Job mail's feedback is keyed by `job_alert` ids: `get_alert` is its 404, a
  `digest-` id expands to alert ids, and `alerts_for_client` serves every
  pending row. The other cards have no alert row; the client raises them
  itself from the agents list and the night state (`Notices.tsx`,
  `Companion.tsx`), so the daemon never learns they were shown.
- `attention_log` is already generic (source, event id, pack, judge, level,
  delivery, reactions) and `replay` runs over its packs.
- Agent output, commands and mail bodies can hold secrets; ADR 0155 keeps
  bodies out of the log.

## Decision

Every proactive card gets the same folded 合适吗 row, the client posts a
snapshot of each card when it is shown and Allen's reaction when he answers,
and the daemon keeps them in `attention_log` (source `card:<kind>`) and a new
`notice_feedback` table.

Which cards, by where they are made and the level they are shown at (`card_sound` when
its cue sounded: quiet `off`, private output, cue switches on; else `card`):

- **Row added, snapshot kind `pop`**: a session finished or stopped, the name
  pop. Made in `useNotices` from the host's or the board's agents list.
- **`wait`**: a finish that asks (ADR 0125), or a prompt answered elsewhere.
- **`req`**: a needs-you card with Allow or Deny, a plan or a question
  (ADR 0049, host requests). Its id is the request's.
- **`digest`**: 「N things while you were away」 when `no-pop` or `dnd` ends.
- **`night`**: the card when the screen wakes in the night (ADR 0093), level
  `card`, no cue. **`morning`**: the card the morning after, level `card`.
- **Already had it**: job-mail card and summary (ADR 0159). The channel-health
  alert is a job card and keeps its row; it now also logs a snapshot
  (source `job_health`).
- **No row**: the confirmation card (0062) and the ask card (0066, 0144) arise
  in the middle of something Allen asked for; the night's bedtime card is his
  own start of the run; a card he brings up from a list is the card already
  logged; the Dashboard pages (the morning brief block, usage, mail) are pulled
  by him; macOS banners of Startrail have no surface for a row and tell what the
  notch card tells; her spoken job line is the same event as its card.

Its limits:

- The post is `POST /inherent/cards/{id}` with the body of the notice route:
  `seen` carries `kind`, `level`, `facts`, `situation`; `feedback` and
  `dismissed` carry the reaction. The client mints the id (kind, session or
  request, epoch ms), so one card is one id however often it is shown again.
- `seen` stores one pack and is idempotent. The pack holds the kind, title,
  counts, agent type and tool name and the situation (front app flags, the
  daemon's quiet level, hour, weekday); never what an agent wrote, a command, a
  path or a body. A text value over 200 characters, a nested value or a pack
  over 1500 characters is refused.
- The rule that raised a card is fixed, so the judge is `card_rule` v1, and
  its level is the one the client showed. Reactions: 对, a level, `dismissed`,
  and `acted` (he used the card's own button or opened its session). A card
  shown 30 minutes with no reaction at all is written `ignored` at the next
  card. Unknown card: 404; any other reaction: 400.
- The row of a needs-you card or of the night card stays on the card after a
  click, which only says thanks; the pop, the digest and the morning card close
  as a mail card does.
- Additive only: `CREATE TABLE IF NOT EXISTS`, no schema version bump, never
  pruned, local, erased with `memory.db`.

## Alternatives rejected

- **Reuse `job_feedback`** — its key is a `job_alert` id and its 404 is
  `get_alert`; a card with no alert row would need a fake alert, which
  `alerts_for_client` would then serve to every client as a notice.
- **Post only the reaction, no snapshot at show time** — most cards get no
  click, so the table would hold the answers and never the denominator, and
  the pack and level shown could not be recovered after the card was gone.
- **Move the agent notices into the daemon** — the queue (hold, fold, park,
  the 1.5 s merge, the ten-minute reminder) is the client's and reads the
  front window; a second copy in the daemon would disagree with it on the
  first restart, which ADR 0153 already rules out for the quiet level.
- **Leave the pop without a row because it is small** — a pop is raised on
  every agent finish, the most frequent proactive event, and the pointer
  already holds it open; skipping it leaves the main data source empty.
- **Put the agent's last message or the command in the pack** — an agent's
  words and a Bash command can hold keys and paths; the title and tool name
  are enough to separate cards, and ADR 0155 keeps bodies out of the log.

## Consequences

- `level_shown` is what the client decided; the daemon cannot hear whether a
  cue played (the cue switches live in the companion's profile), so the pack
  carries `quiet` and the audio flag beside it.
- A needs-you card answered in the terminal, not on the card, reads as
  `ignored`: `acted` is posted only for the card's own controls.
- Session titles are now kept in `memory.db`, never pruned.
- `ignored` is written lazily, at the next card shown, not on a timer.
- Every card costs the client two posts to the daemon; with no `memory.db`
  the route is 404 and the client carries on without the log.
- `glow` (亮一下) is a level a card can be rated at; the job-mail path still
  raises only `card`, `card_sound` and `speak`.
