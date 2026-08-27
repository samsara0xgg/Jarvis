# Live burn 2026-08-26 — web retrieval (`web_fetch` / `web_search`)

Scope: the two L4 web tools, after `6b7508c` (fetch) and `4b146b1`
(search). Re-run with:

```
PYTHONPATH=. python scripts/burn_web_fetch.py     # exit 0 = all pages yielded text
PYTHONPATH=. python scripts/burn_web_search.py    # exit 0 = no blocking failure
```

Both hit the real network. `burn_web_search.py` needs no credential to
run — the keyed providers are probed with a dummy key on purpose — and
adds a live end-to-end section when one IS configured.

## What prompted this

Production turn `Te3815a16` ("帮我查一下，明天从温哥华飞中国上海的机
票"): five tool-loop iterations, none of which produced an answer, and
the surface emitted the developer string `tool-use loop exhausted; turn
incomplete.` Trace:

| # | tool | outcome |
|---|------|---------|
| 1 | `get_current_time` | fine |
| 2 | `web_search` | SEO landing pages, no fares |
| 3 | `web_fetch` kayak | HTTP 200, 150 KB, **zero body text extracted** |
| 4 | `web_search` | landing pages again |
| 5 | `web_search` | `ddgs backend error: No results found.` |

## R1 — `web_fetch` could not read ordinary pages · GREEN

Root cause: the 8 KiB `fetch_max_bytes` was applied to the **socket
drain** (`_http_get_one_hop`), so the parser only ever saw the first
8 KiB of raw markup. Real pages spend far more than that on `<head>`,
so extraction returned nothing and the tool reported "cut off before
any body text was parsed" — on ordinary sites, routinely.

Fix: `fetch_max_bytes` is now purely the download/DoS bound (2 MiB);
the new `fetch_max_text_bytes` (8 KiB) caps the text **after** HTML
extraction.

| page | before | after |
|------|--------|-------|
| kayak YVR→PVG (150 KB) | 0 bytes extracted | 34,319 bytes extracted → capped to 8,192 |
| Wikipedia PVG airport | HTTP 403 | HTTP 200, 107,839 extracted → capped to 8,192 |
| example.com | ok | ok, unchanged (127 bytes, neither cap fires) |

**3/3 pages yield real body text; was 0/3.** The kayak `<title>` alone
now carries a fare (`C$ 523 CHEAP FLIGHTS from Vancouver to Shanghai
Pudong`), which is more than the whole turn managed before.

### R1b — no `User-Agent` · GREEN

Found by this burn, not by the production trace. `_http_get_one_hop`
sent no headers at all, so httpx's default UA reached servers, and
Wikipedia answers that with a bare 403 ("Please set a user-agent").
Now sends a self-identifying `Jarvis/1.0 (+…)` plus `Accept` /
`Accept-Language`. Deliberately NOT a browser UA: this client runs no
JS, and claiming otherwise earns bot-detection blocks rather than
avoiding them.

## R2 — `web_search` backend is pluggable · GREEN (keyless paths only)

`ddgs` returns link-plus-blurb, so content costs a second `web_fetch`
iteration — against a 5-iteration bound, that is what exhausted the
loop. `tools.web.search_provider` now selects `exa` / `tavily` /
`ddgs`, with the credential resolved from the env var named by
`search_api_key_env` (same indirection as `llm.presets.*`).

- Backend resolution: **9/9** — keyed-provider-without-key degrades to
  `ddgs`, unknown provider degrades, case- and whitespace-tolerant,
  empty string counts as unset. The degrade warns AND the provider that
  actually answered is written into the result payload, so it is never
  silent.
- Endpoint shape: `api.exa.ai/search` and `api.tavily.com/search` both
  return **401** to a dummy key — URL and auth header are right (a 404
  would have said otherwise).
- `ddgs` live: still answers (3 rows), so the default path is intact.

### Tavily, live with a real key · GREEN

Configured as `search_provider: tavily`, key in `~/.jarvis/env` (0600).
Burn drives the daemon's own chain — `load_env_file` → config →
`_web_search_provider_config` → `_resolve_search_backend` → backend:

```
config provider = 'tavily', key = resolved
tavily returned 3 rows
  -> 3/3 rows carry substantial page text
```

### R4 — wrong Tavily field preferred · GREEN

Found by the burn above. The first cut preferred `raw_content` (whole
parsed page) over `content` (Tavily's query-relevant extract). Against
the 1,500-char per-result budget that is backwards — the opening bytes
of a page are nav chrome:

| field | first bytes of the same result |
|-------|-------------------------------|
| `raw_content` | `[](https://www.flightsfrom.com/) Which airport are you flying from? … ### Menu * [Home]…` |
| `content` | `…operated by Air Canada and China Eastern Airlines and the flight time is 13 hours and 50 minutes. The distance is 5634 miles.` |

`content` is now preferred and `raw_content` is no longer requested at
all (billed payload we would discard). After the fix, rows open with
real schedule data (`HX238 Hong Kong Airlines 15:30 HKG 2.5h Nonstop…`).
A result whose full page really is needed goes to `web_fetch`, which
can read it now.

The same reasoning was applied to Exa (`highlights` preferred over
`text`) but is **inferred there, not measured** — see below.

### R5 — provider/key-name mismatch is refused · GREEN

`search_api_key_env` now derives from the provider when left blank.
An explicit name belonging to a *different* known provider is refused
outright: `search_provider: tavily` + `search_api_key_env: EXA_API_KEY`
would have posted the Exa credential to Tavily's server. That is a
credential leak, not a typo to tolerate. A neutral name
(`JARVIS_SEARCH_KEY`) is still allowed — this is a footgun guard, not a
naming policy. Burn asserts both branches.

### Not verified

**The Exa path has still never run against a real key.** Its request and
response shapes come from Exa's docs and are unexercised past the 401,
and its `highlights`-over-`text` preference is reasoning carried over
from Tavily rather than anything measured. Setting `EXA_API_KEY` and
flipping `search_provider: exa` remains a live-burn item.

## R3 — aggregate output cap overshot · GREEN (pre-existing)

Found by R2's cap assertion. `_cap_rows_total_bytes` (was
`_cap_search_notes_total_bytes`) appended its `…[truncated N bytes]`
marker **past** the budget, so the sum exceeded `max_bytes` by the
marker length every time the cap fired — measured **8,216 bytes against
an 8,192 cap**. The marker now comes out of the remaining budget:
**8,191 ≤ 8,192**. `search_notes` was subject to the same overshoot and
is fixed by the same change.

## Still open

- **The loop bound itself is untouched.** `max_tool_iterations` is
  still 5 and the LLM still gets no signal that it is running out, so
  it cannot hand back a partial answer on the last iteration. R1+R2
  reduce the iterations a research question costs, but the cliff is
  unchanged.
- **The exhaustion fallback is a developer string.** `decide()` emits
  the English `tool-use loop exhausted; turn incomplete.` straight to
  the voice surface, while the Tier-0 sibling in the same module uses
  Chinese user-facing text (`_TIER0_TOOL_ERROR_TEXT`).
- **Live fares need JS.** No search-plus-fetch combination reaches them;
  that is a browser-tier capability, out of scope here.
- **`total_bytes` is the compressed length.** Observed on kayak:
  `Content-Length` 150,451 against 1,142,461 bytes actually drained
  (gzip). Pre-existing, and now mostly harmless since the drain cap is
  2 MiB, but the reported `total_bytes` and any shortfall derived from
  it are not comparable to the decoded body.
