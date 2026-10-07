# ADR 0185 — The daily spend card is served whether or not job mail is on

**Status:** Accepted
**Date:** 2026-10-06
**Supersedes:** 0173

## Context

- ADR 0173 raises one silent notch card when a local day's recorded
  `cost.recorded` dollars reach `spend_cap.daily_usd` (default 1.00, no model is
  stopped), as a `job_alert` row keyed `spend-cap-<date>` at level `card`.
- It started the watcher and the notice routes inside the `job_mail` block, and
  `config/jarvis.yaml` ships `job_mail.enabled: false`. With the shipped config
  the limit is configured and the card is never made or served.
- Job mail has to stay off by default: its poller reads Allen's Gmail and calls
  Jev, and the Jobs page and `job_ledger` tool are 404 / absent while it is off.
- The rows' quiet level (ADR 0153), the moment's hold (ADR 0161, 0163) and the
  seen / dismiss feedback are the notice pipeline's, and must stay one pipeline.

## Decision

The spend watcher runs and the daemon serves the job-alert rows whenever
`spend_cap` is a usable block, with `job_mail` on or off, through the same two
functions `JobMail.notices` and `JobMail.act` call; the limit's other rules are
ADR 0173's unchanged:

- Once a minute, off the loop thread, the sum of today's `cost.recorded`
  `cost_usd` (a null price adds nothing) is read through the `(type, ts)` index;
  the day is the Mac's local day.
- One card per local date, even across a restart; a card made at the limit is not
  made again if the day passes twice the limit; a new day starts from zero.
- A block that is absent or not a positive number means no limit. Nothing is
  stopped, switched or slowed.
- With job mail off only the alert rows are served: `GET /inherent/notices` and
  `POST /inherent/notices/{id}`; the Jobs page, its routes and the `job_ledger`
  tool stay absent.

## Alternatives rejected

- **Build `JobMail` whenever `spend_cap` is set, without starting its poller** —
  its constructor takes Jev's route and the Gmail connections, and its presence
  turns on `/inherent/jobs`, the `job_ledger` tool and the Jobs page, which the
  shipped config has off.
- **A separate spend notice route and card type** — the client already renders,
  holds, silences and acknowledges `job_alert` rows; a second path would copy that
  logic and drift from the quiet level and the hold.
- **Set `job_mail.enabled: true` in the shipped config** — it starts a Gmail
  poller and paid Jev calls for a limit whose purpose is to watch paid calls.

## Consequences

- Only priced OpenAI calls are in the total (a row with no price adds nothing).
  MiniMax TTS, Jev on OpenRouter, Tavily, Exa and `gpt-live-1` are not, so the
  real day is higher than the figure on the card.
- The check is at most a minute late.
- `GET /inherent/notices` now answers on a daemon with job mail off whenever a
  limit is set, which is the shipped state; a client that took a 404 there as
  "no job mail" gets an empty list instead.
