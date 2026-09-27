# ADR 0065 — MiniMax reports its own balance; only OpenAI's is typed

**Status:** Accepted
**Date:** 2026-09-26
**Supersedes:** 0050

## Context

ADR 0050 had Allen type the OpenAI and MiniMax balances on the Usage page, on the belief
that neither provider has a balance API. On 2026-09-26 Allen asked whether that was true
and, told that MiniMax has one, said "那就帮我把minimax的API 接口做一下".

MiniMax: `GET /account/query_balance` on the API host answers a pay-as-you-go `sk-api-`
key with `available_amount`, `owed_amount` and the other wallet fields as decimal strings,
plus `base_resp`. It is in neither platform's API reference, but MiniMax's own CLI
(MiniMax-AI/cli, `mmx quota show`) has read it since 2026-08-01. A refused key gets a 200
whose `base_resp.status_code` is not 0. Per-day and per-model spend exists only behind the
console's cookie-authenticated `/account/amount`; no key reads it.

The estimate it replaces counted only the characters Jarvis itself synthesised, priced at a
number copied into config. Any other use of the key, a price change or a voucher made it
wrong, and the page could not tell.

OpenAI is unchanged. No endpoint returns the prepaid credit to an API, Admin or
service-account key; `/v1/dashboard/billing/credit_grants` answers only a browser `sess-`
key. OpenAI support said on its developer forum on 2026-09-06 and 2026-09-07 that there is
no such endpoint and no timeline.

## Decision

The usage observer reads MiniMax's balance from `/account/query_balance` with the TTS key
and shows available minus owed; Allen types a balance only for OpenAI, shown as that
recording minus the Costs spend since the start of its UTC day.

## Alternatives rejected

- **Keep the estimate as a fallback when the endpoint fails** — it prices only Jarvis's own
  characters, so it reads high by whatever the key spends elsewhere and by any price
  change, with nothing on the page saying so; an error row names the failure instead.
- **Read the console's `/account/amount` for per-model spend** — it answers only a browser
  session cookie, which the daemon would have to lift from a browser and which expires,
  the reason ADR 0050 rejected OpenAI's `sess-` key.
- **Keep typing MiniMax's balance** — every top-up becomes a manual step for a number
  MiniMax already serves to the key Jarvis holds.

## Consequences

The endpoint is undocumented: if MiniMax changes or drops it, the MiniMax card shows an
error until the collector follows, with no estimate behind it. mmx-cli sends only
`sk-api-` keys there, so an older key format may be refused. The collector calls the
international host; a China-platform key would need `api.minimaxi.com`. The amount is in
the account's currency, taken as USD on the international platform.

`tts.usage_observed` is still written, but nothing folds it now. Old `usage.balance_recorded`
rows for MiniMax are ignored, and `minimax_anchor_*` in YAML or settings is read by nothing.
