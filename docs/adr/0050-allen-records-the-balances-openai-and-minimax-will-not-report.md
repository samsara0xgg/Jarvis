# ADR 0050 — Allen records the balances OpenAI and MiniMax will not report

**Status:** Accepted
**Date:** 2026-09-25
**Supersedes:** none

## Context

The Usage page shows a balance for DeepSeek because DeepSeek has a balance API. OpenAI has
none: the Admin key reads spend (`/v1/organization/costs`), not the prepaid credit left, and
the only reader of that number is the billing dashboard's undocumented endpoint behind a
browser session. MiniMax has none either; ADR-0018 estimated it from an anchor in
`config/jarvis.yaml`, which Allen had to re-copy after every top-up and which took a daemon
restart.

On 2026-09-25 Allen asked for OpenAI's remaining balance on the page and chose to type the
balance there himself ("余额选 B，MiniMax 也一起").

OpenAI's Costs API reports whole UTC days only, so spend cannot be split at the moment a
balance was read. MiniMax spend is known per TTS segment (`tts.usage_observed`).

## Decision

A balance Allen types on the Usage page is a `usage.balance_recorded` event, and the page
shows the latest one minus the spend observed since: OpenAI from the start of that UTC day,
MiniMax from the event's own time. For MiniMax the newer of the recorded and the configured
anchor wins.

## Alternatives rejected

- **Read the billing dashboard's endpoint** — it is undocumented and, per OpenAI's developer
  forum and CodexBar issue #877 (May 2026), answers only a browser session key, which the
  daemon would have to lift from a browser and which expires.
- **Keep the anchor in `config/jarvis.yaml`** — every top-up is a file edit plus a daemon
  restart, which is why Allen chose the page.
- **A runtime JSON file like `plugin-settings.json`** — it holds only the last value; the
  event log keeps every recording with its time, and the observer already folds from it.
- **Count OpenAI from the recording's next UTC day** — spend later that day would be missed,
  so the balance would read high; starting the day early errs low instead.

## Consequences

OpenAI's balance errs low by whatever was spent earlier that UTC day, and silently drifts
low when auto-recharge adds credit Jarvis never sees; recording again fixes both. A
recording older than four Costs pages (124 days) undercounts spend. The page says "since"
and "≈" because the number is an estimate. The YAML anchor stays only as the fallback until
Allen records one.
