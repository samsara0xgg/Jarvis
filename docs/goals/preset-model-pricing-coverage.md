# Goal: preset-model-pricing-coverage

## Goal
Every model shipped in `config/jarvis.yaml` presets has a usable pricing row, so
`cost.recorded` carries a real `cost_usd` number instead of `null`, and a canary
fails whenever a shipped preset's model is unpriced.

## Why
The gap is not one model. All five shipped presets are unpriced — the log line
that surfaced this named only the default one. `data/pricing.json` was generated
on 2026-04-27 against a model set the presets no longer use (the 2026-09-04
DeepSeek switch, plus the grok-4.5 / grok-4.3 presets). Cost accounting is
therefore blind for 100% of real traffic, not for an exotic edge case.

## Current behavior

1. **`compute_cost_usd` on an unknown model returns `None`.**
   `jarvis/shared/pricing.py:27` is the only cost calculator. On a table miss
   (`jarvis/shared/pricing.py:57-66`) it logs the observed warning once per
   process per model (`_warned_models`, `jarvis/shared/pricing.py:22`) and
   returns `None`. It does not raise and does not fall back to 0. The warning
   text itself prescribes the fix: "Add it to LLM_MAP in
   scripts/refresh_pricing.py and re-run" (`jarvis/shared/pricing.py:62`).

2. **`cost.recorded` still emits, with `cost_usd: null`.**
   `cost_usd` is in `optional_payload`, not `required_payload`, on the registry
   entry (`jarvis/state/event_log.py:702-719`; required is only `("kind",
   "model")`). Two emit paths build the payload:
   - exactly-once path: `jarvis/state/cost_accounting.py:145-164` puts
     `cost_usd` in the always-present block (line 153), so the key is present
     and `null`. Note the `optional_values` filter at line 163 drops
     `None` fields — `cost_usd` deliberately sits above it and survives as null.
   - legacy path: `jarvis/decision/__init__.py:386-401` and `:542-561`.
   The docstring at `jarvis/decision/__init__.py:341-342` states the intent
   explicitly: "the event still emits, just with `cost_usd=None` (honest
   unknown vs. raising)."

3. **No consumer treats `None` as zero. Nothing consumes `cost_usd` at all.**
   This is the finding that keeps the card small. Full trace of every
   `cost_usd` / `cost.recorded` site outside tests:
   - Producers: `jarvis/decision/cost_guard.py:94,126,167,340`;
     `jarvis/decision/__init__.py:386,542,2476`;
     `jarvis/state/cost_accounting.py:81,153`.
   - Replay deserializer: `jarvis/state/cost_accounting.py:105` —
     `_optional_float(value.get("cost_usd"))` preserves `None` as `None`.
   - The only SELECT on the event type is an existence/dedupe check that never
     reads the amount: `jarvis/state/cost_accounting.py:252` (`SELECT event_uid
     ... WHERE json_extract(payload_json, '$.run_id') = ?`), and the fold at
     `jarvis/decision/__init__.py:505-509`, which only asks whether a row exists.
   - `grep -rn "SUM(\|sum(" jarvis/ | grep -i cost` returns nothing.
   There is no budget check, running total, report, projection, or gate over
   cost. ADR-0002 says so by design: "**No budget enforcement**: `cost.recorded`
   is observability only" (`docs/adr/0002-real-codex-flagship-scenario.md:2030`).
   **Verdict: no correctness defect in `None` handling. This card is data
   coverage plus a canary, not a `None`-semantics change.**

4. **`pricing.json` is script-generated, not hand-maintained.**
   `"source": "litellm"`, `"source_url":
   "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"`,
   `"generated_at": "2026-04-27T03:04:23+00:00"`. The generator is
   `scripts/refresh_pricing.py`; it writes exactly those fields in
   `build_pricing()` (`scripts/refresh_pricing.py:154-178`) and selects rows
   through the hardcoded `LLM_MAP` (`scripts/refresh_pricing.py:52-79`).
   **The fix therefore belongs in the generator, not in the data file.** A row
   hand-added to `data/pricing.json` would be destroyed by the next generator
   run and would contradict the file's own `source` provenance.

5. **All five shipped preset models are missing.** `config/jarvis.yaml:8-50`
   defines five presets; `LLM_MAP` contains none of their models:

   | preset | model | in `pricing.json`? |
   |---|---|---|
   | `fast` (`default_preset`) | `deepseek-v4-flash` | missing |
   | `deep` | `deepseek-v4-pro` | missing |
   | `vision` | `deepseek-v4-flash-vision-exp` | missing |
   | `grok-fast` | `grok-4.5` | missing |
   | `grok-instant` | `grok-4.3` | missing |

   `pricing.json`'s `llm` keys are the older set (`grok-4.20`, `grok-4.1-fast-*`,
   `claude-*`, `gpt-*`, `llama*`, `gemini-2.5-flash`) — zero occurrences of
   "deepseek", case-insensitive.

6. **All five exist upstream already**, under provider-direct keys matching each
   preset's `base_url`: `deepseek/deepseek-v4-flash`, `deepseek/deepseek-v4-pro`,
   `deepseek/deepseek-v4-flash-vision-exp`, `xai/grok-4.5`, `xai/grok-4.3`
   (verified against the live LiteLLM JSON, 3818 entries, 2026-09-05). So no row
   needs hand-authored numbers.

7. **Two load sites, both the same committed file.**
   `jarvis/decision/__init__.py:330` (repo-relative, `functools.lru_cache`
   at `:333`) and `jarvis/runtime/__init__.py:1491`
   (`repo_root / "data" / "pricing.json"`). `load_pricing_table`
   (`jarvis/shared/pricing.py:86`) flattens **only** the `llm` section and
   **silently skips any row whose `input_per_1m` or `output_per_1m` is null**
   (`jarvis/shared/pricing.py:134-139`).

8. **The `tts` section is dead data.** `load_pricing_table` never reads it and
   `config/jarvis.yaml` names no TTS model. Out of scope.

## Target behavior

- `LLM_MAP` in `scripts/refresh_pricing.py` maps all five shipped preset model
  ids to their upstream LiteLLM keys; `data/pricing.json` is regenerated by
  running the script, not hand-edited.
- `load_pricing_table(data/pricing.json)` returns a priceable entry (non-null
  `input` and `output`) for each of the five preset models.
- A `cost.recorded` row committed to the event log for a turn on
  `deepseek-v4-flash` carries a numeric `cost_usd` greater than zero.
- A canary fails whenever any `config/jarvis.yaml` preset model has no priceable
  row, naming each offending model and the remedy.
- The regenerated file keeps its generated provenance: `source: litellm`,
  `source_url` unchanged, `generated_at` set to the regeneration time.
- `notes` records that DeepSeek rows are the **peak** (standard, undiscounted)
  rate, following the existing `speech-2.8-turbo` note convention.

### Pricing values — what goes in which field

Do not type these by hand into `data/pricing.json`; they are what the
regenerated file must contain, so the implementer can sanity-check the output.
The generator already maps LiteLLM's per-token fields to the file's shape
(`scripts/refresh_pricing.py:113-127`): `input_cost_per_token` →
`input_per_1m`, `output_cost_per_token` → `output_per_1m`,
`cache_read_input_token_cost` → `cache_read_per_1m`,
`cache_creation_input_token_cost` → `cache_write_per_1m`.

DeepSeek publishes cache-hit and cache-miss input prices separately, and since
2026-08-16 splits peak vs off-peak (off-peak is exactly half; peak hours are
01:00-04:00 and 06:00-10:00 UTC, Mon-Fri). `pricing.json` has one flat rate per
field and no time dimension, so **the peak rate is the correct value** — it is
the standard, undiscounted rate and never underestimates spend. LiteLLM already
carries the peak figures, so the generator picks them up with no override.

| model | cache-miss input → `input_per_1m` | output → `output_per_1m` | cache-hit → `cache_read_per_1m` | `cache_write_per_1m` |
|---|---|---|---|---|
| `deepseek-v4-flash` | 0.44 | 1.32 | 0.014 | 0.0 |
| `deepseek-v4-pro` | 1.32 | 3.96 | 0.044 | 0.0 |
| `deepseek-v4-flash-vision-exp` | 0.44 | 1.32 | 0.014 | 0.0 |
| `grok-4.5` | 2.00 | 6.00 | 0.30 | (absent) |
| `grok-4.3` | 1.25 | 2.50 | 0.20 | (absent) |

Sources, verified 2026-09-05:
- DeepSeek first-party pricing page — https://api-docs.deepseek.com/quick_start/pricing
  (peak/off-peak table; off-peak is half of peak).
- LiteLLM snapshot (the file's own `source_url`), whose DeepSeek rows cite that
  same DeepSeek page in their `source` field. Its values match the first-party
  page exactly.
- xAI rows (`grok-4.5`, `grok-4.3`): **second-hand** — taken from LiteLLM,
  which cites https://docs.x.ai/docs/models. These were **not independently
  verified against xAI's own pricing page**; only the DeepSeek rows were
  confirmed first-party. Re-verify the xAI figures before relying on them.

Record the date and URL in the regenerated file's `notes` so a future reader can
tell when it went stale.

## Affected contracts and files

- L6/tooling `scripts/refresh_pricing.py:52-79` (`LLM_MAP`) — add the five
  preset model ids. Also `build_pricing()` `notes` (`:169-175`) — add the
  DeepSeek peak-rate note with its source URL and date.
- data `data/pricing.json` — regenerated output, committed. Tracked by git
  (`git ls-files data` returns exactly this one path).
- test `tests/canary/test_canary_preset_models_priced.py` (new) — the canary.
- test `tests/integration/` (new or extended) — the hermetic `cost.recorded`
  assertion.

## Boundaries and non-goals

- Layers that may change: none. This is data + tooling + tests; no `jarvis/`
  source module changes.
- **Must not change:** `jarvis/shared/pricing.py`. It is vendored
  ("DO NOT EDIT IN-PLACE — upstream sync only", `jarvis/shared/pricing.py:4-6`)
  and its `None`-on-miss behavior is correct and relied upon.
- **Must not change:** the `cost.recorded` registry entry
  (`jarvis/state/event_log.py:702-719`). `cost_usd` stays optional/nullable —
  an unknown model must still emit an honest `null`, never a fabricated 0.
- **Must not change:** the vendor banner on `scripts/refresh_pricing.py`. Edits
  are confined to `LLM_MAP` and the `notes` dict; `LLM_MAP` is the designed
  extension point, named as such by the runtime warning at
  `jarvis/shared/pricing.py:62`.
- **Must not stage `data/` symlinks.** `data/silero_vad.onnx` and
  `data/sensevoice-small-int8` are symlinks into other trees and are correctly
  untracked. Stage `data/pricing.json` by explicit path only; never `git add
  data/` or `git add -A`.
- **Do not touch** `jarvis/surface/voice_tts.py`, `config/jarvis.yaml`,
  `jarvis/runtime/inherent_loop.py` (Lane B) or `desktop/inherent-swift/`
  (Lane C). The canary **reads** `config/jarvis.yaml`; it must not write it.
- Merge-ordering hint: because the canary reads `config/jarvis.yaml`, a Lane B
  change to a preset's model will make it fail — that is the canary working,
  not a defect; land this after Lane B settles, or add the `LLM_MAP` entry with
  the new preset.
- Non-goals: budget enforcement or any cost gate; TTS pricing; backfilling
  `cost_usd` onto historical `cost.recorded` rows; changing how `None` is
  represented downstream.

## Rejected approaches

- **Hand-add rows to `data/pricing.json`.** The file is generated output
  (`source: litellm`, `generated_at`); hand-added rows are destroyed by the next
  `refresh_pricing.py` run and lie about their provenance.
- **Make `compute_cost_usd` return 0.0 on a miss.** Turns an honest unknown into
  a false zero — exactly the defect this card confirmed does not yet exist.
- **Pin a per-model price constant in code or config.** Duplicates the pricing
  table and drifts silently; the generator already exists.
- **Encode off-peak/peak time-of-day rates.** `pricing.json` has no time
  dimension and nothing consumes cost totals; peak-only is correct and simplest.
- **A pure data-file assertion (`table["deepseek-v4-flash"]["input"] > 0`).**
  That is a unit test of a data file and proves nothing about the emitted event.

## Acceptance evidence

Establish the baseline first, before any edit, and report it as `baseline=N`:

    PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and not live_codex"

Report the final count as `baseline + k` with `k` named; never quote a
pre-baked number.

- **Positive (hermetic, the real observable).** A new/extended test under
  `tests/integration/` drives a fake LLM client with known token counts through
  the **real** cost path (`CostRecorder` / `_emit_cost_recorded`) with the
  **real committed** `data/pricing.json` loaded via `load_pricing_table`,
  reporting `model_used == "deepseek-v4-flash"`. It then reads the artifact back
  out of the event log:
  `SELECT payload_json FROM events WHERE type = 'cost.recorded'`, and asserts
  the decoded payload has `cost_usd` non-null, a float, `> 0`, and consistent
  with the known token counts. Asserting on the committed SQLite row — not on a
  dict lookup — is what makes this an acceptance test.
  This is genuinely the same code path: `_pricing_table()`
  (`jarvis/decision/__init__.py:333`) and `JarvisRuntime`
  (`jarvis/runtime/__init__.py:1491`) both load that same committed file, and
  `CostRecorder._known_cost` (`jarvis/decision/cost_guard.py:83-101`) calls the
  same `compute_cost_usd`. Only the provider socket is faked. Anchor for the
  shape: `tests/integration/test_wave4a_response_run.py:1002-1010` already
  performs this SELECT — but note it deliberately passes `pricing_table={}`
  (`:979`); the new test must pass the real table.
- **Canary.** `tests/canary/test_canary_preset_models_priced.py` reads
  `config/jarvis.yaml` (`yaml.safe_load`, path from
  `tests.canary._helpers.repo_root()`), collects every
  `llm.presets.*.model` string, and compares against the keys of
  `load_pricing_table(repo_root() / "data" / "pricing.json")`. It **fails** when
  any preset model is absent from those keys, with a message naming each missing
  model and the remedy ("add to LLM_MAP in scripts/refresh_pricing.py and
  re-run"). Assert against `load_pricing_table` output, not the raw JSON `llm`
  keys: a row present but missing `input_per_1m`/`output_per_1m` is silently
  dropped (`jarvis/shared/pricing.py:134-139`) and would still price as `None`.
  Demonstrate the canary really bites: show it failing on the pre-fix
  `pricing.json` (or with one entry removed) before showing it pass.
- **Regression.** The full command above still passes at `baseline + k`, with
  `k` accounted for by the new tests. Zero pre-existing tests pin price values —
  `grep -rn "cost_usd" tests/` returns nothing and no test asserts a rate — so
  regenerating rows for the already-mapped models is safe; report the row diff
  rather than hand-freezing old prices.
- **Tier 1 gates.** `lint-imports`, `ruff`, `mypy --strict` — report printed
  counts, never infer them. `scripts/refresh_pricing.py` is ruff-exempt
  (`pyproject.toml:122`); the new test files are not.
- **Provenance.** Show that the regenerated `data/pricing.json` still has
  `source: litellm` and the unchanged `source_url`, a refreshed `generated_at`,
  and the new peak-rate note.
- **Live run: not required.** Per `.claude/rules/python-testing.md`, a live run
  is required for a new capability whose correctness depends on a real LLM; this
  is a data-coverage fix to an existing, already-live path, and its regression
  risk is carried by the hermetic test plus the canary. The one live-dependent
  assumption — that the emitted `model` string is the bare preset id
  `deepseek-v4-flash`, not a dated checkpoint like `...-0731` — is already
  established twice from repo evidence: the observed warning names exactly
  `'deepseek-v4-flash'`, and a recorded live run logged `cost.recorded
  model=deepseek-v4-flash` (`docs/goals/v2-input-endpoints.md:354`).
  If the implementer nonetheless opts into an optional live confirmation:
  **never change the macOS system default output device**; and the owner's live
  daemon on port 8009 with runtime root `/Users/alllllenshi/.jarvis-allen-test`
  and its running card must not be stopped, restarted, or written into — use a
  different port and a private runtime root.

## Docs to sync

- **none.** `grep -ni "cost\.recorded\|cost_usd\|pricing" docs/spec.html`
  returns zero matches — the spec owns no fact this change alters. ADR-0002
  describes the `cost.recorded` plumbing, but the contract it documents (owner
  layer L3, required vs optional payload fields, nullable `cost_usd`,
  observability-only with no budget enforcement) is unchanged; only data rows
  and the generator's model map change. Adding an ADR for a pricing-table
  refresh would document what the code already makes clear.

## Open questions

(none)

## /goal condition

Done when the transcript shows all of the following as raw command output, not
as claims:

1. A baseline line captured BEFORE the first edit: the raw output of
   `PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and not
   live_codex"`, with its pass count stated as `baseline=N`.
2. The raw output of `python scripts/refresh_pricing.py` (or the equivalent
   venv invocation), showing it wrote `data/pricing.json`. The regenerated file
   is shown to still carry `source: litellm` and its original `source_url`, a
   refreshed `generated_at`, and a `notes` entry recording that the DeepSeek
   rows are the peak (standard, undiscounted) rate with its source URL and the
   date 2026-09-05.
3. Raw output proving each of the five shipped preset models —
   `deepseek-v4-flash`, `deepseek-v4-pro`, `deepseek-v4-flash-vision-exp`,
   `grok-4.5`, `grok-4.3` — now resolves to a priceable entry through
   `load_pricing_table`, with `deepseek-v4-flash` at input 0.44 / output 1.32 /
   cache_read 0.014 per 1M.
4. The raw passing output of the hermetic acceptance test, which drives a fake
   LLM client through the real cost path with the real committed
   `data/pricing.json` and asserts on a `cost.recorded` row SELECTed back out of
   the event log — its `cost_usd` non-null, a float, and greater than zero. A
   test that only checks a dict lookup does not satisfy this.
5. The canary `tests/canary/test_canary_preset_models_priced.py` shown BOTH
   failing on an unpriced preset model (pre-fix state, or one entry removed) —
   with its failure message naming the missing model — AND passing afterwards.
6. The raw output of the full regression command again, passing at
   `baseline + k`, with `k` explicitly accounted for by the newly added tests.
7. The raw printed counts of `lint-imports`, `ruff`, and `mypy --strict`.
   Counts must be shown, never inferred.
8. `git status` output showing a clean tree after commit, and the commit
   staging `data/pricing.json` by explicit path. The transcript must show that
   NO symlink under `data/` (`silero_vad.onnx`, `sensevoice-small-int8`) was
   staged, and that no file under `jarvis/surface/voice_tts.py`,
   `config/jarvis.yaml`, `jarvis/runtime/inherent_loop.py`, or
   `desktop/inherent-swift/` was modified.
9. The documentation rule is honored: when the implementation changes a
   documented contract, invariant, ownership boundary, or externally relevant
   behavior, the canonical document owning that fact is updated; do not document
   what the code already makes clear; do not duplicate a fact across documents.
   This card judges "Docs to sync: none" — the transcript must either confirm
   that judgment with the `grep -ni "cost\.recorded\|cost_usd\|pricing"
   docs/spec.html` output showing no matches, or update the document it found.
10. `jarvis/shared/pricing.py` is unchanged, and `cost_usd` remains an optional,
    nullable payload field in `jarvis/state/event_log.py` — an unknown model
    must still emit `null`, never a fabricated 0.

Or stop after 12 turns and report what is blocking.

## Progress
- baseline — `pytest -q -m "not live_llm and not live_codex"` on
  realtime-integration (5759479): **baseline=1044 passed, 64 deselected**,
  captured before the first edit.
- data coverage — 4c0bf2e — `LLM_MAP` gains the five preset ids mapped to
  `deepseek/*` and `xai/*`; `scripts/refresh_pricing.py` re-run against 3818
  upstream entries wrote 25 LLM / 8 TTS rows. `load_pricing_table` now returns
  `deepseek-v4-flash {input 0.44, output 1.32, cache_read 0.014, cache_write
  0.0}`, `deepseek-v4-pro 1.32/3.96`, `deepseek-v4-flash-vision-exp 0.44/1.32`,
  `grok-4.5 2.0/6.0`, `grok-4.3 1.25/2.5` — all matching the card's table.
  Provenance kept (`source: litellm`, same `source_url`, `generated_at`
  2026-09-06T03:17:48+00:00) plus the new `notes.deepseek` peak-rate entry.
  Row diff for already-mapped models: the six xAI rows moved upstream since
  April (`grok-4.20` 2.0/6.0 -> 1.25/2.5; `grok-4.1-fast-*` 0.2/0.5 ->
  1.25/2.5, cache_read 0.05 -> 0.2); nothing else changed, no test pins a rate.
- canary — b0f9648 — `tests/canary/test_canary_preset_models_priced.py` bit on
  the pre-fix table, naming all five (`fast -> deepseek-v4-flash`, `deep ->
  deepseek-v4-pro`, `vision -> deepseek-v4-flash-vision-exp`, `grok-fast ->
  grok-4.5`, `grok-instant -> grok-4.3`) with the LLM_MAP remedy; passes on the
  regenerated table.
- acceptance — 23f67e7 — `tests/integration/test_cost_recorded_pricing.py`
  drives a scripted provider socket (1000 prompt / 500 completion / 200 cached)
  through the real `CostRecorder` with the real committed `data/pricing.json`,
  then SELECTs `payload_json FROM events WHERE type = 'cost.recorded'`:
  `cost_usd` is non-null, a float, `> 0`, and equals **0.001015** USD derived
  from those tokens and the committed rates. `model_used == deepseek-v4-flash`.
- gates — lint-imports KEPT (1 kept, 0 broken) · ruff "All checks passed!" ·
  mypy strict "no issues found in 246 source files" · full suite **1046
  passed** = baseline 1044 + 1 canary + 1 acceptance · wall 51.7s.
- docs to sync — confirmed `none`: `grep -nic
  "cost\.recorded\|cost_usd\|pricing" docs/spec.html` returns **0**.
- boundaries — `git diff realtime-integration..HEAD --stat -- jarvis/ config/
  desktop/` is empty: `jarvis/shared/pricing.py`, the `cost.recorded` registry
  entry, `config/jarvis.yaml`, `jarvis/runtime/inherent_loop.py` and
  `desktop/inherent-swift/` are untouched, and `cost_usd` remains an optional
  nullable payload field. No `data/` symlink was staged.
- live run — not required by the card; none performed.
- verifier (opus, fresh context, range `realtime-integration..HEAD`) — one
  confirmed defect, fixed: the data commit's Tier 1 line quoted 245 mypy files
  / 1045 tests, which were measured on a working tree that already carried the
  then-uncommitted canary. Reworded after measuring that commit's own tree:
  **244 files / 1044 passed** (`pytest --ignore` the two new test files, 50.6s).
  The other three bodies were confirmed accurate. The verifier independently
  reproduced `build_pricing()` against the upstream cache and found the
  committed `data/pricing.json` byte-equal to generator output (so the rows are
  provably generated, not hand-authored), proved the canary bites in three ways
  (all five removed, one removed, and a null `input_per_1m` that the loader
  drops silently), and proved the acceptance test fails when `cost_usd` is
  null. Its non-defect observations are accepted as scoped-out: the acceptance
  test derives its expected amount from the same table it loads (the card chose
  row-diff reporting over rate pinning), the streaming path
  (`CostRecorder.stream_events`) shares `_known_cost` and is not separately
  covered, and the canary skips a preset with no explicit `model` key (no such
  preset exists; `llm.presets.*.model` is what the card specifies). Borrowing
  `repo_root()` from `tests.canary._helpers` is the established pattern —
  seven existing integration and scenario files already do it.

---
