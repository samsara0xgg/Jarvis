# ADR 0245 — What he asks her to keep is one write, and what she kept is one read

**Status:** Accepted
**Date:** 2026-10-11
**Supersedes:** the clause of ADR 0179 that makes `set_reminder`, `list_reminders` and `cancel_reminder` the model's reminder tools and gives `set_reminder` an `at` with a UTC offset, the clause of ADR 0066 that makes `remember` the model's writer of `[About the user]`, and the clause of ADR 0146's 2026-10-10 amendment that names `remember`. Everything else in those ADRs stands.

## Context

- The model had six tools for one job, "keep this for him": `create_memo`, `list_memos`,
  `set_reminder`, `list_reminders`, `cancel_reminder` and `remember`. Which one applies turns on
  whether the thing has a time and whether it is about him, and the model must pick before it has
  seen any result.
- Live, 2026-10-11: `create_memo` saved one memo ten times in one turn, its result being
  `{memo_event_uid, text}`, which never says "saved"; `set_reminder` ran seven times in one turn
  (set, cancel, set at guessed times, `at` being an ISO time with a UTC offset the model works
  out); `withdraw_card` was retried after "no card is waiting".
- Replay exam, 97 decision points, three models: 「回家记得修 X」, 「顺便记录下…」, 「回家给你加个
  emoji 功能」 and 「明天是我生日」 are answered with 「好，到家一起弄」 or 「记下了」 and nothing is
  saved, by Haiku, Luna and Sonnet; the same models take `create_memo` for `remember` and
  `set_reminder`. Case `mem-first-memo`: Luna on the old `create_memo` saved the same memo twelve
  times in one turn.
- The stores differ and stay as they are. A memo is a `memo.captured` event with no delete; a
  reminder is the `reminder.*` events ADR 0179's tick fires; a fact is a core-memory item
  (ADR 0146). Tier 0's `/note` and `/notelist` rows call the memo tools.
- ADR 0240 refuses an identical call inside one turn, and only until another write succeeds.
- ADR 0146's amendment (2026-10-10): after 「不是麦当劳 我在吃莲香」 the model filed what he was
  eating as a lasting preference and saved it four times.
- The repository has no parser for "tomorrow 9am".

## Decision

The model keeps things through one write tool, `keep(text, kind, at, topic, cancel)`, and reads
them back through one read tool, `kept(kind, query, page)`; the six tools are removed. `kind` is
`memo` (the default), `reminder` (with `at`, a local time such as `2026-10-12 15:00` in the
configured zone, or one with its own offset) or `fact` (a core-memory item); `at` alone means a
reminder. `keep(cancel=<id>)` cancels a reminder, and only a reminder has an id. Every write says
in one sentence what it did, and keeping the same thing again writes nothing: a memo's text
within ten minutes, a reminder with the same text and minute while it waits, a fact core memory
already holds. The events, the daemon's tick, the card and core memory's versions are those of
ADRs 0179 and 0146, unchanged. `/note` calls `keep` and `/notelist` calls `kept`.

A fact is filed only when he states a standing fact or asks her to remember it, never what he is
doing, eating or where he is now: ADR 0146's amendment holds, worded in `kind`'s description.

## Alternatives rejected

- **Keep the six tools and rewrite their descriptions.** The exam's no-call answers came with all
  six described, and the ten repeats came from a result that never says "saved"; the choice
  among three write tools is still made before a result is seen.
- **Make only the results honest.** It ends the repeats and leaves the exam's silent answers: the
  model reaches for none of six names on 「回家修 X」, while one name whose description carries the
  words he uses is a single candidate.
- **Two tools, `keep` for memos and facts and `set_reminder` for times.** The reminder keeps the
  offset-carrying `at` the model guessed seven times in a turn; a reminder is `keep` with an `at`.
- **Parse "tomorrow 9am" in `at`.** It needs a parser the repository does not have, and a wrong
  guess is then confirmed as a time he did not mean; the result names the local time it
  understood, and a wrong or past `at` is refused with the current time and an example.
- **Leave the repeat to ADR 0240.** It keys on exact arguments in one turn and lets a repeat run
  after any other write; a changed `kind`, spacing, case or the next turn writes again.
- **Delete a memo by id.** Memos are append-only events with no delete type; adding one changes
  the log's schema and the memory page for a request nobody made. `cancel` stays reminders-only.

## Consequences

- A memo and a fact have no id, so only a reminder can be cancelled by the model. The same note
  in other words within ten minutes is a second memo.
- A fact without `topic` takes its first words as the topic and replaces only itself: a changed
  fact needs the same `topic`, or both stay until the nightly pass. `remember`'s `section` is
  gone; a fact lands in the first section, as it did by default.
- `kept` lists reminders (soonest first), then memos and facts (newest first), eight to a page,
  each text cut at 240 characters. Its `summary` names reminders and memos and counts facts,
  naming them only for `kind=fact`; `/notelist` speaks that summary, so it reads the newest
  eight, in the tool's English, and no longer all memos oldest first.
- `at` without an offset is read in `work_state.timezone`, else the system zone; a reminder
  `at` that the old tool refused for lacking an offset is now accepted.
- The old names stay in `jarvis/shared/steps.py` so turns already in the log keep their labels.
  The tool exam reads them as `keep` and `kept` through `tool-aliases.json`; cases that expected
  a memo and refused a reminder (or the reverse) name one tool on both sides, and cannot pass.
