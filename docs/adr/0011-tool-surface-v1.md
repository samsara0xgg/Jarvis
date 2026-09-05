# ADR 0011 — Tool Surface v1 (7 read/observe tools · EffectivePolicy ×9 · ToolDefinition +4 · EntityRegistry v0)

**Status:** Approved (2026-08-25, Allen)
**Amended:** 2026-08-26 — §12 records implementation errata (§12.1 for Steps 1-3, §12.4 for Steps 4-7) and **two** forced design reconciliations that change behavior and **await Allen's confirmation**: §12.2 (item J, the entity gate) and §12.5 (item AK, Tier 0 templates and the Pre-emit Gate). §12.6 lists three accepted risks. §1-§11 are unedited.
**Date:** 2026-08-25
**Depends on:** ADR-0001/0002 (decide() loop, gates, Tier 0, Task Ledger), ADR-0003 (inherent daemon), ADR-0009 (Status Board projection idiom, registration idiom, `actor` provenance; schema-v1 migration landed via phase0-debts merge 2ccb5c2)
**Prepares:** ADR-0012 (Confirmation Flow + AuthorizationLease + `write_file`) — this ADR lands every contract 0012 consumes (`confirmation_threshold`, `requires_confirmation`, `requires_entity`, EntityRegistry, `surface_for`), so 0012 adds only the confirmation state machine and one L3 tool.
**Defers to future ADRs:** scheduler domain — `create_reminder`/`list_reminders` tools, `scheduler.scheduled/cancelled/fired/expired` events, DeferredExecution schema (spec §3.7.9), the wall-clock fire watcher (cut by Allen 2026-08-25 to shrink this batch); smart_home domain — Hue + `device` entities (RPi/home-node phase per spec §3.7.2, see §11); `idle_proactivity` policy dimension (spec §11.1); mode/lens/override events + real policy engine (Phase 4); RPi/home domain; image-submit endpoint (stays 501).
**Number note:** 0004/0006/0007/0008 pre-reserved by ADR-0003/0005 defer tables; 0010 reserved for Drift Watch by ADR-0009. 0011 is next free; 0012 is reserved by this pair for the confirmation flow.

---

## 1. Context

### What exists today

- **6 tools, all plumbing-proven**: `spawn_worker` (L2, async), `verify_diff` (L0), `create_task` (L1), `list_tasks` (L0), `get_current_time` (L0), `open_path` (L1) — registered in one place, `build_default_registry` (`jarvis/execution/tools.py:2489-2582`). The LLM menu is `ctx.tool_registry.for_caller(CallerPrincipal.JARVIS_LLM)` (`jarvis/decision/__init__.py:816-818`) — intrinsic caller filtering only, no policy filtering.
- **`ToolDefinition` has 10 fields** (`tools.py:446-494`): name, description, allowed_callers, risk_level, result_semantics, is_async, input_schema, handler, post_action_check, result_budget_s. None of the spec §14.2 surface/gating fields (`domain`, `read_only`, `requires_entity`, `requires_confirmation`) exist.
- **`EffectivePolicy` has 4 fields** (`jarvis/decision/policy.py:33-60`): mode, autonomy_ceiling, confirmation_required_at_or_above, allowed_tools_per_caller. `effective_policy()` (`policy.py:82-117`) hardcodes the single Collaborate preset: ceiling **L2**, confirmation at **L3** — which means the gate's confirm branch is unreachable: nothing ≥L3 can ever be proposed under an L2 ceiling.
- **The gate's entity check is a hole**: `pre_action_gate` sets `entity_trusted = True` unconditionally when `target_entity_ref is None` (`jarvis/decision/gates.py:209-211`). Every construction site passes None, so the check has never rejected anything.
- **`path_resolver` is pure and silent**: `jarvis/execution/path_resolver.py` resolves free text → path with zero event emission (module docstring, lines 1-17); `open_path_handler` (`tools.py:1915-2040`) emits `action.result_observed` + `lifecycle.transition` but nothing feeds an entity projection. There is no EntityRegistry projection at all — spec §3.3.7 names it as the L2 authority for canonical IDs.
- **Tier 0 discipline is settled**: `config/tier0_patterns.yaml` (5 entries, exact-sentence whitelist per its head comment — tolerates whitespace/case/ASR-spelling only; generalization belongs to the LLM), boot-validated by `validate_tier0_table` (`jarvis/decision/tier0.py:157-184`, rejects async targets, called from `runtime/__init__.py:371-382`).
- **Infra this ADR reuses**: OpenRouter-proxy LLM presets with `api_key_env` indirection (`config/jarvis.yaml` `llm.presets.*` → `https://openrouter.icu/v1`, key `OPENROUTER_PROXY_KEY`); `runtime.runtime_paths.artifacts_root` with the `voice_artifacts/` subdirectory precedent (`runtime/inherent_loop.py:735`); the projection FOLD→PACKET→RENDER→INJECT idiom (StatusBoard: `state/projections.py:756-806` fold, `decision/packet.py:222-287` render, `decision/__init__.py:811-815` inject); the ADR-0009 event-registration idiom with the 6-field schema (`actor` required since the phase0 schema-v1 migration).

### Why this ADR

Jarvis can orchestrate a Codex worker but cannot look anything up: no web, no vault search, no file reading, no screen. Spec §14's first principle — "Tool Surface 不是所有可用工具列表，而是 effective_policy 为当前主体生成的最小能力面" — has no mechanism: the menu is caller-filtered only, policy owns no tool dimension, and entity trust is vacuously true. This ADR lands the seven read/observe tools Allen scoped (2026-08-25), and the three contracts they force into existence: a policy object with the spec's nine dimensions, tool metadata the gate can act on, and an entity registry so `target_entity_ref` stops being decorative. ADR-0012's confirmation flow then plugs into these contracts instead of inventing its own.

### Spec touchpoints

- §14 (line 2588): tool surface = least-capability filter `filter(registry, by=(effective_policy, caller_principal, authorization_lease, mode, surface, active_task))`; "LLM 看到的不是 registry 本身，是过滤后的子集". §14.8: enforcement must be runtime-real, "registry 必须真的过滤".
- §3.4.5: `effective_policy` output is nine fields — autonomy_ceiling, confirmation_threshold, allowed_tool_surface, output_form, verification_level, interrupt_policy, memory_write_policy, task_policy, attention_defaults. "Mode 可以改变 defaults，但不能突破 invariant。"
- §11 (line 2313): "Policy Resolver 是纯函数：(mode_runtime_state, mode_presets, lenses, overrides, invariants) → effective_policy. 它只解释 state 和 config，不创造 state."
- §3.3.6: Mode Runtime State is an event-sourced State Object projection (from `mode.transitioned`/`lens.enabled`/`override.applied`); Mode Preset Table is static config; Effective Policy is computed per-decision, never stored.
- §14.2 (lines 2600-2617): the 15-field per-tool metadata block — source of the four field names adopted here.
- §14.4: risk taxonomy is **L0–L4** (five levels; L4 = system/admin: config·secrets·permissions·daemon control). §14.5: jarvis_llm default L0-L1 + partial L2; L3+ only under AuthorizationLease(human_approved).
- §3.3.7: "LLM 不能 invent entity IDs" — real IDs must come from Event Log, Entity Registry, trusted resolver, tool observation, trusted config, or imported cross-domain event. Entity Registry / Alias Projection holds "canonical entity IDs、aliases、resolver confidence". §3.3.8: `resolve_entity(ref, context)` is a State Object API. Invariant I2 (No Fabricated Entity IDs) is anchored here.
- §3.5.11 / §3.3.9: bounded payloads — tool output capped (`max_output_bytes` in §14.2), artifacts referenced by path, never inlined.
- §3.5.1: "No direct LLM-to-tool execution" — unchanged; all seven tools ride the existing propose → gate → dispatch path.

### Spec contradictions this ADR must pick through (and how)

1. **Two "9-field" EffectivePolicy lists.** §3.4.5 includes `attention_defaults`; §11.1 instead has `idle_proactivity`, and names two fields differently (`tool_surface`, `memory_policy`). Per the project's source-of-truth rule (§3 wins), this ADR adopts **§3.4.5's list and names** verbatim. `idle_proactivity` is deferred (header) and will join the struct when the §11.1 reconciliation lands in a future spec pass.
2. **Two 15-field ToolDefinition lists.** §3.5.3 (name/input_schema/action_type/timeout_default/required_entity_types/artifact_policy/…) and §14.2 (tool_id/domain/requires_confirmation/requires_task_binding/allowed_modes/…) overlap but disagree. The four fields added here use **§14.2's names** (they are the tool-surface/gating fields; §14.2 is the Tool Surface chapter). The defer table (§11 below) enumerates the remainder of *both* lists so neither silently disappears.
3. **§14.4 lists "search" under L0**, but that reads as local/state search. `web_search`/`web_fetch`/`screen_look` are classed **L1** here because their read crosses the network boundary (query text, URLs, screen pixels leave the machine) — more exposure than a local read, no local mutation, hence between L0 and L2. Declared as a deviation (§6, V3).

### Non-spec context

- `.importlinter` layer contract: tools live in `jarvis/execution/`, policy/gate/packet in `jarvis/decision/`, projections in `jarvis/state/`, config loading via existing seams; `runtime/` remains the only cross-layer wiring point.
- Allen's 2026-08-25 scope cut: `create_reminder` + `list_reminders` removed from this batch (they drag in a wall-clock scheduler subsystem — watcher, projection, four events); the remaining seven tools are all L0/L1 with zero new subsystems.
- Privacy fact, accepted by Allen: `screen_look` sends a screenshot to a cloud multimodal model via the existing OpenRouter proxy. The decision brain stays text-only; vision is encapsulated inside the tool.
- No unit tests (dev-mode rules): acceptance is per-step commands + one live burn matrix; branchy logic (resolver outcomes, SSRF guard, truncation) gets data-driven tables.

---

## 2. Scope

**In scope:** P1 EffectivePolicy ×9 + Mode Runtime State v0 (constant) · P2 ToolDefinition +4 fields, migration of the 6 existing tools, `surface_for` policy filtering, gate `requires_entity` rule · P3 EntityRegistry v0 projection + `entity.resolved` event + resolve-on-propose · P4 the seven tools (`search_notes`, `read_file`, `read_clipboard`, `web_search`, `web_fetch`, `open_url`, `screen_look`) + Tier 0 rows + config.

**Out of scope:** confirmation flow, AuthorizationLease expansion, `write_file` (all ADR-0012); scheduler/reminders (deferred, header); any L2+ tool; packet entity *menu* (resolve-on-propose makes it unnecessary for v1); Obsidian write access; browser automation beyond GET.

---

## 3. Decision

### D1. EffectivePolicy grows to the nine §3.4.5 fields; ceiling L2→L3; placeholders are declared decisions

`EffectivePolicy` (`policy.py`) becomes a frozen dataclass with exactly the §3.4.5 output fields:

| field | v1 value | consumer today |
|---|---|---|
| `autonomy_ceiling` | `"L3"` | gate check 3 (risk within ceiling) |
| `confirmation_threshold` | `"L3"` | gate check 4 (`needs_lease`), `surface_for` annotation |
| `allowed_tool_surface` | per-caller name map (today's `allowed_tools_per_caller`, renamed) | gate check 1, `surface_for` |
| `output_form` | `"conversational"` | **placeholder** |
| `verification_level` | `"standard"` | **placeholder** |
| `interrupt_policy` | `"collaborate_default"` | **placeholder** |
| `memory_write_policy` | `"propose_only"` | **placeholder** |
| `task_policy` | `"explicit_only"` | **placeholder** |
| `attention_defaults` | `{}` (empty map) | **placeholder** — `attention_policy()` remains a separate function until Phase 4 folds it in |

- **Renames**: `confirmation_required_at_or_above` → `confirmation_threshold` (spec name; single gate reference updates, `gates.py:238-240`); `allowed_tools_per_caller` → `allowed_tool_surface`. Mechanical, same semantics.
- **Ceiling L3**: with ceiling L2 nothing ≥L3 can be proposed and the gate's `confirm_required` outcome is dead code forever. L3 ceiling + L3 threshold = the spec's intended shape: L3 proposals are *possible*, and every one of them needs a lease (0012). Guard-rail sentence, binding on all future ADRs: per invariant **I7**, confirmation for L3/L4 is an invariant *floor* — `confirmation_threshold` is a dial that can move only below L3; no mode or policy engine may ever set L3+ to no-confirm.
- **Mode Runtime State v0**: a frozen `ModeRuntimeState` (mode=`"collaborate"`, lenses=(), overrides=()) returned by a constant provider on the State Object side. `effective_policy(mode_state: ModeRuntimeState = COLLABORATE_CONSTANT) -> EffectivePolicy` stays a pure function (§11: "只解释 state 和 config，不创造 state"). No `mode.transitioned`/`lens.enabled`/`override.applied` registration in this ADR — the projection becomes event-sourced when the policy engine ADR lands (deferred). The placeholders are *declared decisions*, not dead code: each is listed here with its Phase-4 consumer so a reviewer can tell intent from leftovers.
- `risk_rank` learns **L4** (§14.4 is five levels). No L4 tool exists; the rank function must still order it (fail-closed above ceiling).

### D2. ToolDefinition gains the four §14.2 fields with consumers; the 6 existing tools are migrated

Add to `ToolDefinition` (`tools.py`): `domain: str` (one of the §14.1 seventeen), `read_only: bool`, `requires_entity: bool`, `requires_confirmation: bool`. Every field has a consumer in this ADR or 0012: `domain` feeds audit payloads + future surface filtering; `read_only` feeds `surface_for` grouping and the Pre-emit scrub context; `requires_entity` feeds the new gate rule (D3); `requires_confirmation` feeds 0012's template line (and is derivable-but-explicit: for v1 it must equal `risk_rank(risk_level) >= risk_rank(confirmation_threshold)` — boot validation asserts consistency so the two can't drift).

Migration of the existing six:

| tool | domain | read_only | requires_entity | requires_confirmation |
|---|---|---|---|---|
| spawn_worker | agent_control | false | false | false |
| verify_diff | git | true | false | false |
| create_task | task_ledger | false | false | false |
| list_tasks | task_ledger | true | false | false |
| get_current_time | state_read | true | false | false |
| open_path | mac_gui | false | false* | false |

\* `open_path` keeps its internal resolve-then-act contract (it *is* a resolver caller); flipping it to pre-resolve would change a shipped tool for zero benefit. It participates in EntityRegistry as an **emitter** (D4), not a gate consumer.

**`surface_for(policy, registry, caller)`** (new, `jarvis/decision/policy.py`): wraps `registry.for_caller(caller)` and filters by `policy.allowed_tool_surface[caller]` and `risk_rank(tool.risk_level) <= risk_rank(policy.autonomy_ceiling)`. The LLM menu call site (`decision/__init__.py:816-818`) switches to it; tools above the ceiling simply do not exist in the menu (§14: least-capability surface; §14.8: the registry really filters, not the prompt). Tier 0's boot validation composes with it unchanged (regex_router's surface is already tiny).

### D3. Gate rule: `requires_entity` ∧ `target_entity_ref is None` → refuse

`pre_action_gate` check 2 gains one arm: if the tool's definition has `requires_entity=True` and the ActionRequest carries no `target_entity_ref`, then `entity_trusted=False` with reason `"entity_required: <tool> demands a resolved target"`. The existing None-passes arm (`gates.py:209-211`) remains for tools with `requires_entity=False` — closing it wholesale would break the six migrated tools; the hole is now *scoped* instead of universal, and 0012's `write_file` inherits the strict arm on day one. The gate signature gains the tool definition lookup it already implicitly depends on (registry passed alongside policy — pure function, no I/O).

### D4. EntityRegistry v0: projection + `entity.resolved` + resolve-on-propose

**Entry shape** (plan-original — spec gives no field-level schema for this projection, only "canonical entity IDs、aliases、resolver confidence"):

```python
@dataclass(frozen=True)
class EntityRegistryEntry:
    entity_id: str        # deterministic natural key: "file:<abs-path>" | "repo:<abs-path>" | "task:<task-id>" | "action:<action-id>"
    entity_type: str      # open enum, v1 folds: "file" | "repo" | "task"; "action" joined with ADR-0008 D10 ("device" joins with the smart_home ADR — no reshaping needed)
    canonical: str        # the resolved absolute path / task id
    aliases: tuple[str, ...]   # raw refs that resolved here (bounded: last 8, dedup)
    confidence: str       # "exact" | "fuzzy" | "bookmark" | "config"
    source_event_id: str | None   # event that registered it (None for config-seeded rows)
    last_seen_ms: int
```

**Fold, three routes + one seed** (FOLD→PACKET→RENDER→INJECT per the StatusBoard idiom, but *no packet note in v1* — see resolve-on-propose below for why the LLM doesn't need a menu):
1. Task Ledger task ids → `task:` entries (fold `task.created` etc. the ledger already consumes; piggybacks the existing fold pass).
2. `repo.state_observed` → `repo:` entries (ADR-0009's observer already emits these).
3. `entity.resolved` events (new, §4) → `file:` entries.
4. Seed: `config/file_targets.yaml` bookmarks ingested as `confidence="config"` entries at fold init (trusted config is a legitimate ID source per §3.3.7).

**Resolve-on-propose** (the LLM-facing contract, locked with Allen 2026-08-25): the LLM never sees or invents entity ids. For a tool with `requires_entity=True`, the LLM passes a free-text `target` argument; at ActionRequest construction time (pre-gate, in `_dispatch_one_tool_call`), the runtime runs the trusted resolver (path_resolver + registry lookup), fills `target_entity_ref` with the resulting `entity_id`, and emits `entity.resolved`. Resolution failure leaves the ref `None` → D3's gate arm refuses with the resolver's reason (including candidate list on ambiguity) → the LLM sees a structured tool-result error and may retry with a better ref. Every resolution attempt is auditable (`entity.resolved` with `outcome`), every failure is a gate event, and I2 (no fabricated IDs) holds by construction.

**Emitters**: `open_path_handler` on successful resolution (outcome=`resolved`; path_resolver itself stays pure — emission lives in the handler, same as its existing `action.result_observed`), and the new pre-gate resolve step. Registered `owner_layer=L2` (the Entity Registry is a State Object concern, §3.3.7), `actor="jarvis_runtime"`.

### D5. The seven tools

All sync, all registered in `build_default_registry`, all riding propose → gate → dispatch → `action.result_observed` → Result Interpreter (`result_semantics` mapping unchanged from the handler protocol). Output caps are per-tool `max_output_bytes`-style constants (bounded payloads, §3.5.11); truncation appends an explicit `…[truncated N bytes]` marker so the LLM knows it saw a prefix.

| tool | domain | risk | read_only | requires_entity | result_semantics |
|---|---|---|---|---|---|
| search_notes | obsidian | L0 | true | false | observation |
| read_file | file_read | L0 | true | **true** | observation |
| read_clipboard | clipboard | L0 | true | false | observation |
| web_search | browser | L1 | true | false | observation |
| web_fetch | browser | L1 | true | false | observation |
| open_url | mac_gui | L1 | false | false | ack |
| screen_look | screen | L1 | true | false | observation |

- **search_notes** — input `{query: str, max_results?: int ≤10 (default 5)}`. Case-insensitive token match over `*.md` under `obsidian.vault_root` (new config key, default `~/Documents/Obsidian Vault`); returns `path — matched line` rows. No index in v1 (vault is small); missing vault → empty result with a note, not an error.
- **read_file** — input `{target: str}` free text. Resolve-on-propose (D4) fills `target_entity_ref`; the handler reads the *resolved canonical path* (never the raw string), text files only (binary sniff → error observation), output cap 8 KiB.
- **read_clipboard** — no input. `pbpaste`, cap 8 KiB. Empty clipboard is a valid empty observation.
- **web_search** — input `{query: str, max_results?: int ≤8 (default 5)}`. Backend: the `ddgs` package (DuckDuckGo, no API key, no new account — chosen over Brave/Exa APIs purely to avoid provisioning a new secret; the backend sits behind one function so a keyed API is a config-sized swap later). Returns numbered `title — url — snippet` rows. Timeout 15 s; backend breakage (anti-bot) degrades to an error observation, never a crash.
- **web_fetch** — input `{url: str}`. `httpx` GET (promoted from transitive to declared dependency), timeout 20 s, ≤3 redirects, streamed with an 8 KiB text cap. HTML → title + tag-stripped readable text; no JS rendering (declared limitation — SPA pages return their shell). **Egress guard in-handler** (L4 sandbox duty, §3.5.3 note): scheme ∈ {http, https} and the resolved address must not be loopback/link-local/RFC1918 — the daemon holds local sockets (uvicorn :8006) that a fetched URL must not be able to probe.
- **open_url** — input `{url: str}`. Same scheme allowlist, then `open <url>` (default browser). This *acts* on the GUI (read_only=false) but touches no durable state; result is an ack (Execution Claim `executed` — the browser opening is not verified).
- **screen_look** — input `{question?: str}`. `screencapture -x` → `artifacts_root/screen_artifacts/<ts>.png` (voice_artifacts precedent) → downscale to ≤1568 px wide (`sips`) → one vision call through a new `llm.presets.vision` preset (same OpenRouter proxy + `OPENROUTER_PROXY_KEY`, multimodal model; the decision brain never sees pixels, only the returned text observation). Payload carries the artifact *path*, never image bytes (§3.3.9). First run in the daemon context triggers the macOS Screen Recording TCC prompt for the Python binary — the DoD includes granting it once; denial degrades to an instructive error observation.

### D6. Tier 0 rows — only where an exact sentence fully determines the arguments

The whitelist discipline (exact sentences, zero generalization) is incompatible with parameterized tools: no exact sentence can carry an arbitrary query/URL/path. So Tier 0 rows ship only for the fixed-argument tools:

- `read_clipboard`: 「剪贴板里有什么」「读一下剪贴板」
- `screen_look`: 「看一下我的屏幕」「看一眼屏幕」(question=None)

This **refines the handoff line "每个只读工具配 Tier 0 精确句式"** — declared in §6 (V4) rather than silently narrowed. Parameterized invocations of every other tool route through the LLM (Tier 1/2), exactly where the head comment of `tier0_patterns.yaml` says generalization belongs. Both rows are sync tools → `validate_tier0_table` passes unchanged.

### D7. Config additions (`config/jarvis.yaml`)

```yaml
llm:
  presets:
    vision:                      # screen_look only — multimodal, same proxy, same key
      model: gpt-5.5
      base_url: "https://openrouter.icu/v1"
      api_key_env: OPENROUTER_PROXY_KEY
      max_tokens: 1024
tools:
  obsidian:
    vault_root: "~/Documents/Obsidian Vault"
  web:
    search_max_results: 5
    fetch_max_bytes: 8192
    timeout_s: 20
  screen:
    vision_preset: vision
    max_width_px: 1568
```

New dependencies: `ddgs`, `httpx` (explicit). No new secrets, no new accounts.

---

## 4. Event Type Registry Changes

One new type (ADR-0009 idiom, 6-field schema):

- **`entity.resolved`** — owner_layer `L2`, actor `jarvis_runtime`, schema_version 1.
  required: `ref_raw: str`, `outcome: "resolved" | "ambiguous" | "not_found"`.
  optional: `entity_id`, `entity_type`, `canonical`, `confidence`, `candidates` (≤5, ambiguity case), `resolver` (`"path_resolver" | "bookmark"`), `tool_name`.
  Emitted by `open_path_handler` (success only, outcome=resolved) and the pre-gate resolve step (all outcomes). Registry count 41 → 42.

No other registrations. (`scheduler.*` left the batch with the reminder tools; `confirmation.*`/`surface.*` belong to ADR-0012.)

---

## 5. Failure Modes

| failure | behavior |
|---|---|
| ddgs backend breaks (anti-bot churn) | error observation with backend name; LLM narrates the limitation; no retry loop in-handler |
| web_fetch hits SSRF guard | in-handler refuse → error observation naming the guard (not a gate refuse — the URL is an argument, not an entity) |
| web_fetch oversized / non-text body | streamed cap + truncation marker / content-type note |
| screen_look TCC denied | error observation: "Screen Recording permission missing for <python path> — System Settings → Privacy" |
| vision preset misconfigured / proxy down | error observation; screenshot artifact still saved (evidence survives the model failure) |
| resolver ambiguity on read_file | ref stays None → gate refuse, reason carries ≤5 candidates → LLM retries with a specific one |
| vault_root missing | empty search result + note (not an error — Allen may rename the vault) |
| clipboard empty / non-text | valid empty/typed observation |

Un-guarded pre-existing hole explicitly **not** touched here: malformed-lease KeyError (`gates.py:249-250`) — 0012 owns lease construction and hardens that seam with it.

---

## 6. Spec Deviations Declared

- **V1**: §3.4.5's nine-field list adopted over §11.1's (which swaps `attention_defaults` for `idle_proactivity` and drifts two names). §3-wins rule; `idle_proactivity` deferred, not dropped.
- **V2**: the four new ToolDefinition fields use §14.2 names; the remaining fields of *both* §14.2 and §3.5.3 are deferred (see §11), not merged into a hybrid neither section specifies.
- **V3**: `web_search`/`web_fetch`/`screen_look` are L1 although §14.4 lists "search"/"screen observe" under L0 — network egress of query/screen content is more exposure than a local read. Conservative direction (up-classification), so I7/threshold semantics are unaffected.
- **V4**: Tier 0 rows only for fixed-argument tools (D6), refining the handoff's "每个只读工具" line — exact-sentence discipline cannot carry parameters, and diluting it to templates would reopen the generalization door Tier 0 exists to close.
- **V5**: EntityRegistryEntry shape and the resolve-on-propose contract are plan-original designs filling declared spec silences (§3.3.7 names the projection but gives no schema; no spec text describes how the LLM supplies entity refs). Both satisfy the §3.3.7 ID-source allowlist and I2 by construction.
- **V6**: `requires_confirmation` stored explicitly though derivable from risk vs threshold — redundancy is boot-validated (D2) so it cannot drift; explicit storage is what lets 0012 render the template line without recomputing policy.

---

## 7. Build Order

Each step = one commit, Tier-1 green, its acceptance command in the body (CLAUDE.md five-part template).

1. **Policy ×9** — EffectivePolicy nine fields + renames + `ModeRuntimeState` constant + ceiling L3 + `risk_rank` L4. Acceptance: data-driven table over `effective_policy()` output; gate canaries still green (confirm branch now *reachable* but nothing triggers it — no L3 tool exists yet).
2. **ToolDefinition +4 + surface_for** — field additions, six-tool migration table, `surface_for` at the menu call site, boot validation (`requires_confirmation` consistency). Acceptance: menu snapshot for JARVIS_LLM == 6 tools pre-P4; validate_tier0_table green.
3. **Gate entity arm** — D3 rule + registry lookup pass-in. Acceptance: data-driven gate table (requires_entity × ref-present × outcome).
4. **EntityRegistry v0** — projection + `entity.resolved` registration + bookmark seed + open_path emission + resolve-on-propose helper. Acceptance: E1/E2 (below) in a scripted run.
5. **Local tools** — search_notes, read_file, read_clipboard + Tier 0 rows. Acceptance: T3/T4/T6 live.
6. **Web tools** — web_search, web_fetch (+ deps, egress guard), open_url. Acceptance: T1/T2/T7 live; SSRF data-driven table.
7. **screen_look** — artifact dir, sips downscale, vision preset, TCC note. Acceptance: T5 live.

Steps 5-7 are independent of each other (any order); 1→4 are strictly ordered.

## 8. Acceptance

### Tier 1 (every commit)
lint-imports KEPT · ruff clean · mypy strict clean · hermetic tests pass · wall <30 s.

### Tier 2 — live burn (single session, real daemon, real LLM)

| row | utterance (via decide(), full chain) | pass condition |
|---|---|---|
| T1 | 「搜一下 SQLite WAL 模式的优缺点」 | web_search dispatched; ≥1 result row in observation; LLM answer cites a result |
| T2 | 「把 https://example.com 的内容抓下来看看」 | web_fetch observation contains page text; cap respected |
| T3 | 「我 vault 里关于 ADR 的笔记有哪些」 | search_notes returns ≥1 path from the real vault |
| T4 | 「读一下 jarvis 的 CLAUDE.md」 | resolve-on-propose fills `target_entity_ref`; `entity.resolved(outcome=resolved)` on the log; content observation |
| T5 | 「看一下我的屏幕」(Tier 0 hit) + 「屏幕上现在开着什么」(LLM path) | screenshot artifact exists; text observation names something actually on screen |
| T6 | 「剪贴板里有什么」(Tier 0 hit) | pbpaste content in observation |
| T7 | 「用浏览器打开 anthropic.com」 | browser opens; ack claim `executed`, no completion inflation |
| E1 | T4's side effect | EntityRegistry projection contains the `file:` entry; bookmark seeds present at boot |
| E2 | 「读一下 xzqk9 文件」(garbage target) | gate refuse via D3 arm; `entity.resolved(outcome=not_found)`; LLM relays the limitation, no hallucinated content |

### Definition of Done
All 9 rows green in one burn log (`docs/`, ADR-0009 precedent) · TCC granted once for screen · registry count 42 · menu snapshot documented.

## 9. Consequences

- 0012 starts with every contract in place: it adds `write_file` (one registry entry with `requires_entity=True, requires_confirmation=True`), the confirmation state machine, and lease minting — no policy/metadata surgery.
- The gate is stricter only for new tools (D3's arm); zero behavior change for the shipped six — regression surface is the menu call site swap (step 2's snapshot pins it).
- Two new dependencies (`ddgs`, `httpx`); one new LLM preset; no new secrets.
- Tier-1 wall-clock: the seven handlers are sync and hermetic-testable via injected fakes (subprocess/network seams follow the existing handler-injection pattern); budget stays <30 s.
- Known debt carried, not created: malformed-lease KeyError (0012), `event_log.py:697` stale "no actor column" comment (any next commit touching that file), packet-side entity menu (only if resolve-on-propose proves too blind in practice).

## 10. Module Map

| piece | location |
|---|---|
| EffectivePolicy ×9, ModeRuntimeState, surface_for | `jarvis/decision/policy.py` |
| gate entity arm | `jarvis/decision/gates.py` |
| resolve-on-propose helper | `jarvis/decision/__init__.py` (`_dispatch_one_tool_call` seam) |
| EntityRegistry projection | `jarvis/state/projections.py` |
| `entity.resolved` registration | `jarvis/state/event_log.py` (registry block) |
| seven handlers + registrations | `jarvis/execution/tools.py` (or `tools_surface.py` if tools.py's size demands a sibling — importlinter-neutral either way) |
| Tier 0 rows | `config/tier0_patterns.yaml` |
| config | `config/jarvis.yaml` |

## 11. Defer Table

| item | where it went |
|---|---|
| `create_reminder`, `list_reminders`, scheduler watcher, `scheduler.*` events, DeferredExecution | future scheduler ADR (Allen cut, 2026-08-25) |
| smart_home domain — Hue control, `device` entities + discovery | RPi/home-node phase. Spec §3.7.2 assigns Hue **authority** to the RPi domain (invariant 7: "Mac 不直接拥有 Hue truth"); the canonical Mac path is `cross_domain.request.dispatched(action_type=hue_control)` over MQTT (§3.7.3, §16). Mac-direct bridge control would violate both and is **not** taken as an interim step. |
| `idle_proactivity` (§11.1) | policy engine ADR (Phase 4) |
| §14.2 remainder: `caller_principal` list-form, `side_effect`, `requires_task_binding`, `requires_evidence`, `max_output_bytes` (per-tool constants for now, not schema field), `allowed_modes`, `allowed_surfaces` | when a consumer exists |
| §3.5.3 remainder: `action_type`, `timeout_default`, `required_entity_types`, `side_effect_domain`, `artifact_policy` | when a consumer exists / spec reconciliation |
| mode/lens/override events; event-sourced Mode Runtime State | policy engine ADR |
| packet entity menu | only if resolve-on-propose proves insufficient |
| Obsidian index for search_notes | if vault scale demands it |
| image-submit endpoint (501) | untouched (handoff decision 9) |

## 12. Amendments (implementation pass, 2026-08-26)

Started after §7 Steps 1-3 shipped and extended as Steps 4-7 landed. **§1-§11 above are unedited** — an amendment records the correction, it does not rewrite the approved text. Three kinds of entry: **errata** (§12.1, §12.4), where the ADR's prose was wrong or under-specified and the code is right; **reconciliations** (§12.2, §12.5), real design changes taken because §8's own acceptance rows or the shipped system are otherwise broken; and **accepted risks** (§12.6), properties that are deliberate and should be re-read as decisions. No decision Allen made is re-opened; no scope is added.

### 12.1 Errata — prose corrections, no design change

**Naming and value drift.** The code is authoritative; the ADR text was written against mis-remembered symbols.

| § / line | ADR text | shipped | note |
|---|---|---|---|
| §3 D1, line 84 | mode `"collaborate"` | `"Collaborate"` (`jarvis/decision/policy.py:35`, `:158`) | `PolicyMode = Literal["Collaborate"]` predates this ADR (`git show 058b8c8:jarvis/decision/policy.py:29`); the lowercase form does not type-check. |
| §3 D1, line 84 | `COLLABORATE_CONSTANT` | `COLLABORATE_MODE_STATE` (`policy.py:163`) | Divergence documented in the constant's own docstring (`policy.py:166-167`). |
| §3 D1, line 85 | "`risk_rank` learns **L4**" | already present | `_RISK_LADDER = ("L0", "L1", "L2", "L3", "L4")` at `git show 058b8c8:jarvis/decision/policy.py:65`. No work was required; Step 1 changed nothing here. |

**B — §3 D1 vs §10 on where `ModeRuntimeState` lives. Code follows §10; the reason is *not* a layer violation.** §3 D1 (line 84) puts the constant provider "on the State Object side" (L2); §10's Module Map (line 282) puts `ModeRuntimeState` in `jarvis/decision/policy.py` (L3). `.importlinter` orders `decision | execution | surface | deployment` **above** `state`, and higher layers may import lower ones — so `decision → state` is permitted, and five `jarvis/decision/*` modules already do it (`decision/__init__.py:105-106`, `packet.py:25`, `gates.py:37`, `resolver.py:44`, `result_interpreter.py:53`). **There is no layer violation on either placement.** §10 was followed because: (i) the L2 rule this project actually holds is "when *mutable* mode state exists, it belongs in L2 rather than L3" (`policy.py:8-13`), and v0 is a frozen zero-input constant, not state, so the rule is not triggered; (ii) an L2 module that folds no events and reads no rows would be a placeholder, not a projection; (iii) `policy.py` deliberately restricts its own imports to stdlib + `jarvis.shared` (`policy.py:17`), which an L2 constant would break for no benefit. When the policy-engine ADR makes Mode Runtime State event-sourced, it moves to L2 and D1's wording becomes correct.

**D — `effective_policy()` signature.** §3 D1 (line 84) gives `effective_policy(mode_state: ModeRuntimeState = COLLABORATE_CONSTANT)`, omitting `allowed_tool_surface` entirely. Implemented literally it breaks the sole production call site, `effective_policy(_allowed_tool_surface(ctx.tool_registry))` (`jarvis/decision/__init__.py:759`), which passes the surface **positionally**. Shipped signature keeps `allowed_tool_surface` first and positional and makes `mode_state` keyword-only (`policy.py:174-178`). Purity is unaffected — the resolver still only interprets its arguments (§11).

**F — `surface_for` name filtering is fail-closed.** §3 D2 (line 104) specifies filtering "by `policy.allowed_tool_surface[caller]`". Literal subscripting raises `KeyError` for any caller absent from the map. Shipped code uses `.get(caller, frozenset())` (`policy.py:297`) — an unmapped caller gets an empty surface, not a crash. Same shape as gate check 1 (`gates.py:242`).

**G — `domain` is typed `str`; nothing enforces the §14.1 enum.** §3 D2 (line 89) requires `domain` to be "one of the §14.1 seventeen" but ships it as `domain: str` (`jarvis/execution/tools.py:510`), so a typo registers cleanly. The seventeen, verbatim from spec §14.1 (`docs/spec.html:2598`):

`state_read` · `file_read` · `file_write` · `terminal` · `git` · `browser` · `screen` · `clipboard` · `mac_gui` · `smart_home` · `task_ledger` · `agent_control` · `memory` · `obsidian` · `notification` · `scheduler` · `hardware`

All five values the Step-2 migration ships are in that list: `agent_control` (`tools.py:2576`), `git` (`:2481`), `task_ledger` (`:2602`, `:2623`), `state_read` (`:2641`), `mac_gui` (`:2663`). **Declared debt:** the membership check belongs beside the `requires_confirmation` invariant in the boot-validation block Step 2 added, `jarvis/runtime/__init__.py:389-401` (bootstrap step 3c) — one more pure validator raising `PolicyConsistencyError`, same failure shape.

**H — `surface_for` cannot currently subtract anything.** Neither §3 D2 nor §9 says so, and it belongs on the record before §14.8's "registry 必须真的过滤" is read as an accomplished property.

- *Name arm is a structural identity.* The policy's `allowed_tool_surface` is derived from the same registry the filter walks: `_allowed_tool_surface` builds `frozenset(t.name for t in registry.for_caller(principal))` for every principal (`jarvis/decision/__init__.py:2448-2455`), and `surface_for` then keeps tools from `registry.for_caller(caller)` whose name is in that set (`policy.py:297-303`). At the sole production call site (`decision/__init__.py:759` feeding `:888`) the two sets are equal by construction, for *any* registry. Nothing is ever removed.
- *Ceiling arm is inert.* All six registered tools are ≤L2 — `spawn_worker` L2, `create_task`/`open_path` L1, the rest L0 (`tools.py:2476`, `:2571`, `:2597`, `:2618`, `:2636`, `:2658`) — under an L3 ceiling. Even ADR-0012's planned L3 `write_file` passes (`risk_rank("L3") <= risk_rank("L3")`). The arm first bites at L4, and no L4 tool exists.

The mechanism is wired and correct; it is **dormant, not proven**. The first real subtraction arrives when a mode preset narrows `allowed_tool_surface` below the registry, or when the ceiling drops below L3 — both in the policy-engine ADR.

**I — §10's Module Map omits the Step-2 boot validator.** It landed as `validate_requires_confirmation` + `PolicyConsistencyError` in `jarvis/decision/policy.py:309-349` (pure; takes an iterable of tool defs and a threshold), called from `jarvis/runtime/__init__.py:394-401` as bootstrap step 3c, converted to `RuntimeBootstrapError` (`runtime/__init__.py:136`). Missing Module Map row: `requires_confirmation` boot invariant → `jarvis/decision/policy.py` (check) + `jarvis/runtime/__init__.py` (call site).

**K — the gate takes a resolved `ToolDefinition`, not a registry.** §3 D3 (line 108) says the gate gains "the tool definition lookup … (registry passed alongside policy)". The shipped signature instead takes the caller's **already-resolved** definition as a required keyword-only parameter: `pre_action_gate(action_request, policy, ledger_snapshot, *, tool_def: _EntityGateToolLike | None)` (`jarvis/decision/gates.py:168-174`), where `_EntityGateToolLike` is a one-property Protocol reading only `requires_entity` (`gates.py:151-165`). Reason: the two call sites resolve `tool_def` through two *different* lookups — `_find_registered_tool_def` is caller-blind over `registry.get_definitions()` (`decision/__init__.py:2424-2445`, Tier 0 path), `_find_tool_def` is `JARVIS_LLM`-scoped (`:2413-2421`, LLM path) — so a third, gate-internal lookup could act on a different definition than the caller resolved. Taking the caller's value removes that divergence. Still a pure function, no I/O, and strictly less coupling than passing the registry.

**L — §1's "the check has never rejected anything" is half wrong.** §1 (line 20) frames the whole entity check as a hole. Only the `None` arm was vacuous. The **non-None arm has always been real validation**: it tests `target_entity_ref` for membership in the Task Ledger projection and refuses on miss (`gates.py:278-290`; identical at the pre-ADR baseline, `git show 2ccb5c2:jarvis/decision/gates.py`). §1's companion claim that "every construction site passes None" is also wrong — only the Tier 0 path hardcodes `None` (`decision/__init__.py:1006`); the LLM path fills the ref from the resolver (`:1163`) or from `scratch.active_subject_ref` (`:1197-1198`) and passes it through (`:1235`). Corrected framing: **D3 closes the `None` arm; the non-None arm was never open.** §12.2 turns on this.

**M — `requires_entity` has no boot-validation counterpart.** Unlike `requires_confirmation` (item I), nothing checks `requires_entity` against the Tier 0 table. A future Tier 0 row naming a `requires_entity=True` tool would boot clean and then refuse at every dispatch, because the Tier 0 path hardcodes `target_entity_ref=None` (`decision/__init__.py:1006`) and D3's new arm refuses exactly that. D6's two planned rows are safe — `read_clipboard` and `screen_look` are both `requires_entity=false` (§3 D5 table) — so nothing breaks today. **Cheap follow-up, land with Step 5:** extend `validate_tier0_table` (`jarvis/decision/tier0.py:157`) to reject a whitelist entry whose tool has `requires_entity=True`, alongside its existing async-target rejection. Same block, same failure shape.

### 12.2 Reconciliation J — the entity gate refuses `read_file` on both branches

**This is the one item in §12 that changes behavior rather than correcting prose. Allen: please confirm.**

**The gap.** §3 D3 (line 108) explicitly *preserves* check 2's existing non-None arm, which tests `target_entity_ref` for membership in the Task Ledger's task ids. §3 D4 (line 117) specifies that resolve-on-propose fills `target_entity_ref` with an entity_id shaped `"file:<abs-path>"`. A `file:` id can never be a Task Ledger task id. So after Step 4, `read_file` (`requires_entity=true`, §3 D5) is refused on **both** branches:

| `target_entity_ref` | arm taken | result |
|---|---|---|
| `None` (resolver failed) | D3's new arm | refuse — `entity_required: read_file demands a resolved target` (intended, E2) |
| `"file:/…/CLAUDE.md"` (resolver succeeded) | pre-existing ledger arm | refuse — `is NOT in Task Ledger` (**not** intended) |
| `"task-abc"` (a real task id) | pre-existing ledger arm | pass |

**Evidence.** Direct probe against the real `pre_action_gate` at Step-3 HEAD — `tool_def.requires_entity=True`, ledger holding one task `task-abc`, policy allowing `read_file`, risk L0 — reproduces all three rows exactly as tabulated, the middle one with `outcome=refuse`.

**Consequence for §8.** Two of this ADR's own Tier-2 acceptance rows are **unsatisfiable as written**: **T4** ("resolve-on-propose fills `target_entity_ref`; `entity.resolved(outcome=resolved)` on the log; content observation") and **E1** ("EntityRegistry projection contains the `file:` entry"). T4 never reaches a content observation and E1's side effect never happens, because the gate refuses first. This is not an implementation defect — it follows directly from D3 and D4 as approved.

**Resolution (decided by the implementation session, 2026-08-26).** Check 2's non-None arm widens to accept a ref that is **either**:

1. a known Task Ledger task id — existing behavior, byte-for-byte unchanged, **or**
2. a known `entity_id` in the EntityRegistry projection.

Keeping (1) untouched is what preserves the shipped `verify_diff` / `spawn_worker` path, which passes bare task ids (`decision/__init__.py:1163`, `:1197-1198`, `:1235`) and is pinned by the existing gate canaries. (2) is what makes D4's `file:` ids trustworthy — and it is the only widening that satisfies I2 (no fabricated entity IDs), since the EntityRegistry is precisely the projection of ids the trusted resolver produced (§3.3.7's ID-source allowlist).

**Plumbing.** The EntityRegistry snapshot reaches the gate the same way the Task Ledger snapshot already does — folded into the SituationPacket (`decision/packet.py:121`) and passed as a parameter (`decision/__init__.py:1032-1033`, `:1263-1264` pass `packet.task_ledger_snapshot`). `pre_action_gate` stays a pure function with no I/O.

**Cost, stated plainly.** This lands in **Step 4**, which therefore makes a **second** signature change to `pre_action_gate` one step after Step 3's. Accepted deliberately: folding it into Step 3 would require Step 4's projection to exist first, and swapping the two steps would leave D3's arm untestable.

**What does not change.** D3's `None` arm and its refuse reason; D4's entry shape, `file:` id format, and resolve-on-propose contract; §8's T4/E1 row text (they become satisfiable, not rewritten); the six migrated tools' behavior; every other approved decision in §1-§11.

### 12.3 Build-order status (§7)

| step | scope | commit |
|---|---|---|
| 1 | Policy ×9 + `ModeRuntimeState` + ceiling L3 | `a96ee88` |
| 2 | ToolDefinition +4 + `surface_for` + boot validation | `7e221bf` |
| 3 | Gate entity arm (D3) | `6ff67fc` |
| 4 | EntityRegistry v0 (+ reconciliation J) | `758af59` |
| 5 | Local tools + Tier 0 rows (+ reconciliation AK) | `3fb37d2` |
| 6 | Web tools | `b68f0e3` |
| 7 | `screen_look` | `dbda5d7` |

All seven build steps are committed on `worktree-phase2-impl`. §8's Tier-2 live
burn is a separate step; row **T5 cannot be completed by any commit** (see item
AH). Every step ran the four Tier-1 gates green, and every step's implementation
was reviewed adversarially before commit — §12.4 records what those reviews
found.

### 12.4 Errata from Steps 4-7 (implementation pass, 2026-08-26)

Same rule as §12.1: the code is authoritative, the ADR prose was written
ahead of the implementation. §1-§11 stay unedited.

**N — §4 is obsolete in whole; this ADR registers ZERO new event types, and §8's
"registry count 42" can never be met.** §4 specifies a *new* `entity.resolved`
registration with required `ref_raw` + `outcome`, optional `entity_id` /
`entity_type` / `canonical` / `confidence` / `candidates` / `resolver` /
`tool_name`, `owner_layer=L2`, and "Registry count 41 → 42". Every part of that
is wrong. `entity.resolved` has existed since Day-1
(`jarvis/state/event_log.py:250-265`) with a different, load-bearing schema —
required `entity_type`, `natural_ref`, `resolved_to`, `confidence`,
`candidates`, `match_basis`, `outcome`; optional `resolver_warning`;
`owner_layer=L3` — already emitted by the task resolver
(`jarvis/decision/__init__.py`, `_emit_entity_resolved`) and pinned by
`tests/scenarios/test_flagship.py:350`. Both new emitters (the pre-gate resolve
step and `open_path_handler`) write that schema with `entity_type="file"`. The
registry stays at **41**, so §8's Definition-of-Done line "registry count 42" is
unsatisfiable as written and should read "registry count unchanged at 41".

Two sub-points that follow from it, both recorded rather than fixed:

- §3 D4 asks for `owner_layer=L2` ("the Entity Registry is a State Object
  concern"). Shipped is L3, inherited from the Day-1 registration.
- `open_path_handler` is L4 and now emits this L3-declared type. `emit_event`
  does not enforce `owner_layer`, and no canary covers `entity.resolved` the way
  `tests/canary/test_canary_cost_recorded_l3_only.py` covers `cost.recorded`, so
  the inconsistency passes silently. Reconciling it — either re-declaring the
  owner layer or adding an enforcement canary — is deferred, but it is now on
  the record rather than inherited by accident.

**O — §12.2's plumbing paragraph is necessary but not sufficient; the packet's
registry snapshot predates the event the same dispatch emits.** §12.2 says the
EntityRegistry snapshot "reaches the gate the same way the Task Ledger snapshot
already does — folded into the SituationPacket". Implemented literally, that
still fails T4 and E1: `_dispatch_one_tool_call` emits `entity.resolved` and
*then* gates, but `packet.entity_registry` was folded before that event existed,
so a **first-time** file resolution is refused with `is NOT in Task Ledger or
EntityRegistry` — precisely the outcome §12.2 exists to remove. Probed directly
at Step-4 HEAD. Shipped fix: `EntityRegistry.with_resolved_event()` overlays the
just-emitted event through the same private entry builder fold route 3 uses, so
the overlay is byte-identical to the next full refold. The event is already
durable in the log; only the snapshot lags. This is a completion of §12.2, not a
further widening of trust.

**P — §3 D4 gives no alias tie-break rule.** "bounded: last 8, dedup" does not
say what happens to a ref that resolves again. Plan-original fill: a duplicate
keeps its existing position (no reordering), a new ref appends, and the oldest
drops once the count exceeds 8.

**Q — the `confidence` value for a failed resolve is unspecified.** D4's enum
`"exact" | "fuzzy" | "bookmark" | "config"` describes registry *entries*, not
the payload of a `not_found` event. Shipped `"none"` for that case. Separately,
a bookmark hit produces `confidence="bookmark"` and the task-ledger fold route
produces `"exact"`, so all four documented values are now reachable.

**R — D3 and D4 together left the entity gate bypassable, and neither section
anticipated it.** The task-ref resolver runs *before* the `tool_def` lookup and
assigns `target_entity_ref` from raw, unvalidated LLM JSON. A `requires_entity`
tool called with a stray `task_id` or `natural_ref` argument alongside its
`target` therefore received a **task id**, skipped resolve-on-propose entirely,
emitted no file `entity.resolved`, and passed check 2's ledger arm — §8 row E2's
own scenario returning `pass` and dispatching with an unresolved target. Probed
and reproduced. Shipped fix: the `tool_def` lookup is hoisted above the task-ref
resolver and the whole resolver block is skipped for `requires_entity` tools.
Consequence worth noting: an unknown tool name now returns before the task
resolver runs, so it no longer emits a task-flavored `entity.resolved` for a
tool that does not exist.

**S — §3 D5 mandates an output cap for every tool; `search_notes` shipped
without one and needed two.** D5 line 138 requires per-tool
`max_output_bytes`-style caps with an explicit truncation marker. A single
matched line is unbounded, so one pathological note (minified JSON, base64, a
long CSV row) produced a 2,000,096-character payload bound for both the event
log and the cloud model. Shipped: a per-line cap **and** an aggregate cap, so
one pathological line cannot consume the whole budget.

**T — see §12.5 (reconciliation AK).** D6's Tier 0 row for `read_clipboard` interacts with the
Pre-emit Gate in a way no section anticipated. Recorded as a reconciliation
because it changes behavior.

**U — neither §3 D5 nor §5 acknowledges what `read_file` can actually reach.**
D5 line 151 constrains it to "text files only, output cap 8 KiB" and §5's
failure-mode table has no row for a resolver hit on a credential file. In
practice `read_file` inherits `open_path`'s containment — anything text-shaped
under `~/Projects`, `~/Documents`, `~/Desktop`, `~/Downloads` and the configured
bookmark directories, via a one-level scan plus recursive Spotlight — and ships
those bytes to a cloud model. That includes any repository's `.env`, `.pem`, or
`secrets.yaml`. Verified *not* reachable: `~/.ssh/*`, `~/.aws/credentials`,
`~/.zsh_history`, browser cookie databases, `$CODEX_HOME/auth.json`,
`/etc/passwd`; symlink escape and `..` traversal are both blocked by the
resolver's resolve-then-contain check. No denylist was added because none was
scoped — but `open_path` shows a file on Allen's own screen while `read_file`
transmits it to a third party, and that difference should be a written decision
rather than an inherited side effect.

**V — §3 D7 does not say how a per-tool config value reaches a handler.** The
existing precedents (`observer.poll_interval_s`, `tier0_table`) are consumed
inside `decide()` via `DecideContext`. `tools.obsidian.vault_root` and
`tools.web.*` instead have to reach a *handler bound at registry-build time*.
Shipped pattern: a closure factory (`_make_<tool>_handler(config) -> ToolHandler`)
with `full_config` loaded before `build_default_registry` runs. New pattern
class; a future ADR should name it if more config-needing tools land.

**W — D5 does not define `read_file`'s behavior on a non-`file:` entity ref.**
D4 fixes the id shape as `"file:<abs-path>"` but says nothing about a non-None
ref that lacks the prefix — reachable in principle now that §12.2 gives check 2
two universes. Shipped: an explicit prefix check folding into the same
`no_resolved_target` error path as the `None` case.

**X — §12.1 item M's validator is narrower than item M's own text.** M says
"reject a whitelist entry whose tool has `requires_entity=True`". The shipped
validator's input set is derived from the `REGEX_ROUTER` surface, so a row
naming an entity-required tool *outside* that surface is caught by the
pre-existing `allowed_tool_names` arm with a generic message instead. Today the
new arm has no reachable input, since `read_file` is not in the regex-router
surface. Correct but weaker than advertised.

**Y — §3 D5 and §3 D7 disagree on web timeouts.** D5 line 153 gives `web_search`
a 15 s timeout and line 154 gives `web_fetch` 20 s; D7 ships a single
`tools.web.timeout_s: 20`. Shipped: the one configured value applies to both.
The schema's own naming supports this — `search_max_results` and
`fetch_max_bytes` are tool-prefixed and `timeout_s` deliberately is not.

**Z — "at most 3 redirects" does not say how many requests that is.** Shipped:
up to 3 redirects *followed*, i.e. at most 4 requests; a 4th redirect response
is refused with `too_many_redirects`.

**AA — §3 D5's egress guard specification is an incomplete denylist.** D5 line
154 requires "scheme ∈ {http, https} and the resolved address must not be
loopback/link-local/RFC1918". Implemented literally that misses, and adversarial
probing confirmed it allowed: CGNAT `100.64.0.0/10` — **the Tailscale range**,
live and reachable on this machine, which CPython's `is_private` deliberately
excludes — IPv4 and IPv6 multicast (including SSDP `239.255.255.250`), IPv6
site-local `fec0::/10`, and NAT64 `64:ff9b::/96` with an embedded private
address. Shipped: the predicate is inverted from a denylist enumeration to an
allowlist of globally-routable addresses, with explicit refusals for the four
classes above; NAT64 unwraps the embedded IPv4 and re-checks it.

**AB — the guard specification does not address validated-string ≠ used-string.**
D5 describes what to validate but not what to *use*. Validating with `urlsplit`
and then handing the original string to macOS LaunchServices or to httpx means
three parsers with three interpretations, and that gap was exploitable: a 4-part
leading-zero host (`http://0177.0.0.1/`) is decimal to `getaddrinfo`
(`177.0.0.1`, public, allowed) and octal to every browser (`127.0.0.1`), which
was confirmed end-to-end with `open` reaching a throwaway loopback server.
Shipped: the guard returns a canonical re-serialized URL and both call sites use
only that, plus strict `ipaddress` parsing for IP-literal-shaped hosts so
legacy literal forms never reach `getaddrinfo`.

**AC — §3 D7's "no new secrets, no new accounts" is a credential claim, not a
supply-chain one.** `ddgs` was chosen over Brave/Exa purely to avoid
provisioning a key (D5 line 153 says so). It pulls in nine transitive packages,
including `lxml`, `fake-useragent`, and `primp`, a Rust HTTP client whose stated
purpose is impersonating browser TLS fingerprints. The backend sits behind one
function, so a keyed search API remains a config-sized swap.

**AD — an exact truncation byte count is not obtainable for a chunked response
without draining it.** D5 requires the `…[truncated N bytes]` marker. For a
response with no `Content-Length`, computing an exact `N` means reading the
whole body — which, with a per-operation rather than wall-clock timeout, let a
1-byte-per-second server hold the synchronous decide() loop for 30 s against a
5 s configured timeout. Shipped: `Content-Length` is trusted when present;
otherwise reading stops at the cap and the marker says "at least N bytes"
rather than claiming a count that was never measured. A wall-clock deadline
now covers the whole fetch including every redirect hop.

**AE — `tools.screen.max_width_px` names a width bound; the shipped primitive bounds both dimensions.** §3 D5 line 156 and §3 D7 line 186 both say "downscale to ≤1568 px wide". The correct `sips` primitive for "shrink to fit within N, never upscale, preserve aspect ratio" is `-Z` / `--resampleHeightWidthMax`, which bounds the **larger** of the two dimensions. For a landscape screenshot the two readings coincide, so behavior matches intent for every real input; the config key name is simply narrower than what it controls.

**AF — §5 collapses the TCC failure shapes, and one of them is undetectable.** The table has a single "screen_look TCC denied" row. In practice `screencapture` fails in at least four distinguishable ways: non-zero exit, exit-0 with no output file, exit-0 with an empty output file, and exit-0 with a **silently all-black image**. The first three are detected and all render the same actionable message, because a non-zero exit is not attributable to permission denial specifically (disk-full looks the same). The fourth cannot be detected without decoding pixel data, which would mean a new imaging dependency; it is a **declared non-goal**, not an oversight, and is noted in the handler.

**AG — §3 D6's "(question=None)" shorthand is a trap if read literally.** Tier 0's `args:` schema is `str -> str` only, so there is no way to encode a `None`. Writing `args: {question: "None"}` would pass the four-character *string* `"None"` to the handler. The correct — and only — encoding is to omit the `args:` block entirely, so the tool receives `{}` and the handler's own default applies.

**AH — §8's Definition of Done contains a step no commit can perform.** "TCC granted once for screen" is a human-in-the-loop action: the first real `screencapture` in the daemon's process raises the macOS Screen Recording prompt, which needs Allen physically present to approve. Row **T5 therefore stays open** in the burn log rather than being recorded green, and the DoD is not fully met until Allen grants it and re-runs that one row.

**AI — putting `vision` under the shared `llm.presets.*` block makes it visible to the decision client.** A direct consequence of D7's chosen config shape, not an implementation slip: the decision loop's own `LLMClient` parses the same block, so `get_presets()` now lists `"vision"` and `switch_model("vision")` would strand the decision loop on a multimodal model. Nothing calls it, and the vision request always goes through a separate injected client, but the preset is reachable where it has no business being.

**AJ — nothing prunes any `artifacts_root` subdirectory.** `screen_artifacts/` follows the `voice_artifacts/` precedent the ADR cites, and that precedent has no retention or cleanup logic anywhere in the tree. Screenshots of Allen's screen therefore accumulate on disk indefinitely. No pruner was built, to avoid inventing a retention policy the ADR does not specify — but the growth is now on the record, and it is worth noting the accumulating files are full-screen captures, i.e. the most sensitive artifact class this system writes.

### 12.5 Reconciliation AK — Tier 0 templates gate the claim, not the quotation

**This is the second item in §12 that changes behavior rather than correcting
prose. Allen: please confirm.**

**The gap.** §3 D6 ships a Tier 0 row for `read_clipboard` whose response
template interpolates the clipboard's contents. It is the first Tier 0 template
to splice **user-controlled** text into a draft; `{spoken_time}`,
`{spoken_date}` and `{opened_name}` are all tool-produced scalars. The rendered
draft then goes to the Pre-emit Gate, whose completion scan does not distinguish
Jarvis's own words from quoted third-party text.

**Evidence.** Probe against the real `pre_emit_gate` with one open task in the
ledger: a clipboard containing `任务已完成，请查收` and one containing
`verified locally` both produce `downgrade_required=True`; ordinary text does
not. `已完成` and `verified` are canonical `COMPLETION_REGEXES` entries.

**Consequence.** On `downgrade_required=True`, `_finalize_response` attempt 1
re-prompts the LLM — on the path whose entire purpose is to never call an LLM —
and Allen, having asked what is on his clipboard, receives limitation framing
about an unrelated task. 完成 is among the most common words in written Chinese,
Allen copies text constantly, and having open tasks is the normal state of this
system, so this would fire routinely. The head comment's rule
(`config/tier0_patterns.yaml`, "template: NEVER use completion-class words") is
satisfied by the literal and silently violated by the substitution.

**Resolution.** On the Tier 0 path the gate evaluates the **template literal** —
the closed, Allen-authored whitelist text that constitutes Jarvis's actual
claim. Interpolated tool output is quoted data, not a claim. The user still
receives the fully rendered response, contents included. The LLM path is
untouched: there the whole draft is Jarvis's own words and stays fully gated.
Separately, every value interpolated into a Tier 0 template is now capped to a
short spoken preview, since 8 KiB of clipboard spliced verbatim into a *spoken*
response is unbounded TTS.

**Proven both directions.** A Tier 0 clipboard containing `已完成` no longer
downgrades, while an LLM-path draft of `任务已完成。` with no verified evidence
still does. A fix that merely disabled the gate would fail the second half.

**What does not change.** `COMPLETION_REGEXES`, the gate itself, the LLM path,
the Tier 0 whitelist discipline, §8 row T6's pass condition ("pbpaste content in
observation" — the full content still lands in `action.result_observed`).

### 12.6 Accepted risks, recorded rather than mitigated

Three properties of this batch are deliberate and should be re-read as decisions, not as gaps someone forgot to close:

1. **`read_file` reach** (item U) — any text file under the resolver's search roots, including credential files, transmitted to a cloud model.
2. **`screen_look` privacy** — §1 already records that a screenshot goes to a cloud multimodal model via the OpenRouter proxy, and Allen accepted it. Worth restating alongside AJ: those same screenshots also persist on disk forever.
3. **DNS-rebinding TOCTOU on `web_fetch` / `open_url`** — the egress guard resolves once to validate and httpx (or the browser) resolves again to connect, so an attacker controlling a low-TTL record could rebind in between. Pinning the connection to the validated address would break SNI and virtual-host handling for too little gain at this threat level. Literal-IP URLs have no such gap, as a side effect of item AB's strict-parsing fix.
