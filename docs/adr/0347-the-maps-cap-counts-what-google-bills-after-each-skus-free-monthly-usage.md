# ADR 0347 — The maps cap counts what Google bills, after each SKU's free monthly usage

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** in ADR 0283, pricing every map request at list price with the free monthly usage ignored

## Context

- `maps_spend` prices every map request at Google's list price and refuses paid calls from 90% of
  `maps.monthly_usd` (default 10 USD). ADR 0283 left out Google's free monthly usage, knowing the
  count would run high.
- Since March 2025 each Google Maps Platform SKU has its own free monthly usage, set by its tier:
  about 10,000 billable events for Essentials SKUs, 5,000 for Pro and 1,000 for Enterprise. It
  resets at midnight Pacific time on the first of the month.
- At list price, ten bus searches a day (0.02 USD each) and five map searches a day (0.035 USD
  each) come to about 11 USD a month. That passes the 9 USD stop, and bus and `map_search` then
  refuse him, in a month where every request is inside the free usage and Google bills nothing.
- ADR 0345 and ADR 0346 add requests: an area lookup, a walking-time matrix, a second search when
  nothing is inside the area, and the place reader's search.

## Decision

Price only the requests past each SKU's free monthly usage, and apply the cap and its 90% stop to
that sum.

- Each SKU's requests are counted over the billing month, Pacific time, from the event log, as the
  spend is today.
- The free usage sits beside the price in the one table of SKUs. Where Google's figure for a SKU is
  uncertain, the table keeps the higher price and the smaller free usage.
- The refusal she passes on says what has been billed against the cap, as today.

## Alternatives rejected

- **Raise the cap.** The cap would still count free requests as spent. It would also let real
  billing, once the free usage is gone, run further before the stop.
- **Read the bill from Google's Cloud Billing API.** That needs a service account and a billing
  export the brain does not have, and the export lags by hours. The event log already counts every
  request.

## Consequences

- The free usage belongs to the whole billing account. Requests from another app on the same
  account are invisible here, so the count can run low.
- Google can change its free usage or prices; the table has to be kept in step with the price
  list.
- In a heavy month the stop now comes later, at the point where Google starts to bill.
