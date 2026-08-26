# ADR 0012 — Confirmation Flow + AuthorizationLease (deterministic consent · lease minting · `write_file` L3)

**Status:** Approved (2026-08-26, Allen)
**Date:** 2026-08-25
**Depends on:** ADR-0011 (Approved 058b8c8; implemented Steps 1-7 + §12 amendments on `worktree-phase2-impl` @ 6d398f3, burn 8/8 with T5 awaiting the TCC grant — consumes `confirmation_threshold`, `autonomy_ceiling=L3`, `requires_confirmation`/`requires_entity`, EntityRegistry + resolve-on-propose, `surface_for`). Read 0011 **as amended**: §12.4 N (registry stays 41; `entity.resolved` is the Day-1 registration, not 0011's §4 shape), §12.1 K + §12.2 J (shipped `pre_action_gate` signature takes a resolved `tool_def` and the packet's ledger/registry snapshots — this ADR's projection input follows that shape). Also ADR-0009 (registration idiom, `actor` provenance, attention-channel suppression wiring); phase0 schema-v1 (6-field EventTypeSchema, merged 2ccb5c2)
**Branch note:** stacked on `worktree-phase2-impl` (the 0011 implementation branch) — implementation must not start from main or from the superseded `worktree-phase2-adr0011`.
**Defers to future ADRs:** multi-slot pending confirmations; fold-free lease store (leases remain unpooled by design, see D2); post_action_check chain for `write_file` (read-back verification); prompt-side interface notes (only if live burn shows need — never as behavior patch); spec §3.4.10 ConfirmationRequest as entity-repair mechanism (medium-confidence resolution) — this ADR covers action confirmation only.
**Number note:** 0011 took Tool Surface v1; this pair reserved 0012 for the confirmation flow. Next free is 0013.

---

## 1. Context

### What exists today (post-0011)

- **The gate's confirm branch is reachable but unhandled.** With ceiling L3 (0011 D1) an L3 proposal survives check 3 and hits check 4: `needs_lease = risk_rank >= risk_rank(confirmation_threshold)` (`gates.py:238-240`), no lease → outcome `confirm_required`. Both call sites still collapse it into refuse: the LLM path checks `gate_outcome != "pass"` (`decision/__init__.py:1210-1225`, injects a synthetic error tool-result), the Tier 0 path likewise (`:972-975`, fixed refusal text at `:886`). **No L3 tool exists yet**, so today the branch is armed but never fired — this ADR ships the tool and the handling together, as each other's acceptance.
- **Lease validation exists and has never run.** `pre_action_gate` check 4 (`gates.py:237-261`) validates expiry + scope; `_lease_scope_permits` (`:283-309`) is fail-closed on a missing `allowed_tools` key. All five ActionRequest construction sites pass `authorization_lease=None` (`decision/__init__.py:935/1162/1705/1799/1916`); the `AuthorizationLease` TypedDict has 4 fields and **zero constructors repo-wide** (`shared/__init__.py:170-189` — its own docstring says never populated on Day-1). Known hole: `lease["expires_at_ms"]` / `lease["scope"]` are bare subscripts (`gates.py:249-250`) — a malformed lease raises KeyError out of the gate instead of refusing (blast radius: `turn.failed` via the watcher's catch, `inherent_loop.py:521-531`, not a daemon crash — still wrong).
- **The ask channel is dead code waiting.** `AttentionChannel` Literal has 3 values (`gates.py:475`); `notify.py`'s `ATTENTION_CHANNEL_TO_SURFACES` already maps all 9 including `ask_confirm → (osascript_banner, cli_stdout)` (`notify.py:28`) — unreachable today. TTS speaking is governed by the ADR-0009 D4 suppression set (`{queue_review, silent_log}`), not by the notify tuple.
- **Turns are strictly serial**: the watcher awaits each turn to completion (`inherent_loop.py:507-532`). A confirmation question must therefore *end its turn*; the answer is necessarily a new turn.
- **The LLM has zero conversation history**: `build_llm_messages` sends one user message per turn (`intent.py:65-110`); the reply turn cannot see the question. Whatever must survive between ask and answer must live in a projection and re-enter via packet notes (`decision/__init__.py:794-815` insert-at-0 idiom).
- **The flag-to-finalize idiom is an event scan, not parameter threading**: `_finalize_response` derives `limitation_emitted` by scanning `scratch.events` (`decision/__init__.py:2154-2157`) — the confirmation flag copies this exactly.
- **`prompts/jarvis_v1.md` has 4 instructions telling the LLM to "ask before acting"** — with no machine to receive the answer, they have been decorative. This ADR gives them semantics *without editing the prompt* (red line: behavior lives in gates/state machines, never prompt patches).

### Why this ADR

C5 (constitution): "工具成功、agent 自报、LLM 自信都不能单独证明目标达成" — and LLM 转述 cannot carry authorization either. I7 (invariant): L3+ actions **must** be confirmed. Today jarvis has no way to ask, no way to hear "yes", and no object that carries Allen's approval to the gate. This ADR builds that circuit: a durable ask, a deterministic ear, a lease that binds the approval to the exact frozen action — with the LLM structurally unable to touch any link of it.

### Spec touchpoints

- §3.5.2 (lines 815-824): AuthorizationLease is exactly nine fields — lease_id · granted_by · granted_to · allowed_tools · allowed_targets · expires_at · max_uses · reason · source_confirmation_event_id. "human_approved 不是 caller principal；它是 AuthorizationLease。真实 caller 仍然是 jarvis_llm…只是本次拥有 Allen 明确批准的临时能力"; audit form is "jarvis_llm 在 lease X 下调用 patch".
- §3.5.5 / §3.1 / §2.1: L3 decides & attaches the lease to the gated ActionRequest (`authorization_lease?` per §3.4.8), L4 re-enforces expiry/target at dispatch. (Spec never names the minter in so many words — inference declared, §6 V5.)
- §3.6.3 (line 1050): UserResponse's durable forms are `surface.user_intent · confirmation.accepted · confirmation.rejected · surface.dismissed · surface.clarified`. AttentionRouting channel enum includes `confirmation_request` (code vocabulary: `ask_confirm` — an established naming map, like `queue`→`queue_review`).
- §13.2 I7 (line 2557): "High-Risk Action Requires Confirmation. 删除、发送、push、merge、rebase、kill、install、secret/config/system 改动必须确认." §13.3: modes cannot break invariants — the L3+ confirm floor is not a dial.
- §13.4: "Prompt 可以告诉 LLM 规则，但 runtime 必须强制" — the deterministic grammar and template line are this sentence made mechanical.
- §3.4.3: a decision reads one fixed projection snapshot; fresh state requires a new decision — consistent with (and justifying) re-proposal from a frozen snapshot in a *new* turn.
- §3.3.9 / §3.5.11: bounded payloads; large content goes to artifacts, events carry references.

### Spec silences this ADR fills (declared, not cited)

1. **No `confirmation.requested` event exists anywhere in spec** — §3.6.3's durable list has only the five above; §5.4.3 parks `confirmation.*` in the lowest-priority unschema'd bucket (no payload shapes given for *any* confirmation event). But a pending confirmation must be foldable (projection = the only turn-surviving memory), so the ask must be durable. `confirmation.requested` is a **plan-original registration**, and every confirmation payload shape below is new design.
2. **No numbers**: TTL and max_uses appear in spec as bare field names. Confirmation TTL 10 min and lease TTL 60 s and max_uses=1 are design values (§6 V2).
3. **No UX mechanics**: consent detection, question phrasing, re-proposal, rejection handling — zero spec text (verified by exhaustive grep). All four are original design satisfying C5/I7/§13.4 in spirit.

---

## 2. Scope

**In scope:** P1 lease schema ×9 + gate lease validation v2 (KeyError fix, target scope, single-use) · P2 five event registrations · P3 PendingConfirmations projection + packet note · P4 `write_file` (the only L3 tool) · P5 ask path (confirm_required handled at both call sites, frozen snapshot, template line, `ask_confirm` channel) · P6 answer path (deterministic grammar, accept/reject, lease mint, deterministic re-proposal, fixed broadcasts).

**Out of scope:** any second L3 tool; multi-slot pending; confirmation via non-voice surfaces beyond what banner+stdout already give; prompt edits; §3.4.10 entity-repair confirmations; worker_agent leases (§14.5 case-by-case — no worker L3 path exists).

---

## 3. Decision

### D1. `write_file` — the only L3 tool, and the flow's acceptance instrument

| field | value |
|---|---|
| domain / risk | file_write / **L3** |
| read_only / requires_entity / requires_confirmation | false / **true** / **true** |
| result_semantics | ack (Execution Claim `executed` — write-was-accepted, not content-verified; read-back post_action_check deferred) |
| input | `{target: str, content: str, mode: "create" \| "overwrite" \| "append"}` |

Resolve-on-propose (0011 D4) extended for write targets: an existing file resolves normally; a non-existent target resolves iff its **parent directory** resolves within `search_roots`/bookmarks scope (the prospective path becomes the canonical target; the emitted `entity.resolved` uses the **Day-1 schema** — ADR-0011 §12.4 N — with outcome=`resolved`). Outside-scope or unresolvable → ref stays None → 0011 D3's gate arm refuses. The handler receives the canonical absolute path only, never the raw string; `mode="create"` refuses an existing file, `overwrite`/`append` require one.

### D2. AuthorizationLease ×9 + gate lease validation v2

The 4-field TypedDict is **replaced** (zero constructors — breaking reshape is free) by the nine §3.5.2 fields: `lease_id`, `granted_by` (`"allen"`), `granted_to` (caller principal), `allowed_tools`, `allowed_targets`, `expires_at_ms`, `max_uses` (always 1 in v1), `reason` (the template action line, human-readable audit), `source_confirmation_event_id`.

Gate check 4 rewritten:
1. **Shape**: malformed lease (missing/mistyped keys) → `lease_validated=False`, reason `"lease_malformed"` — *never* a KeyError escaping the gate (fixes `gates.py:249-250`).
2. **Expiry**: `expires_at_ms > now`.
3. **Scope**: `action_request.tool_name ∈ allowed_tools` **and** the request's canonical target ∈ `allowed_targets` (byte-equal path match; fail-closed if the request has a target and the lease lists none).
4. **Single-use**: the lease's `source_confirmation_event_id` must reference a confirmation the PendingConfirmations projection (D4) shows as `accepted` and **not yet consumed** — consumption is folded from the `gate.evaluated` pass event that carries `lease_id` in its payload. The gate stays pure: the projection is an input, the fold supplies the history. A replayed lease therefore fails in the gate itself (acceptance C5), not merely by construction.

Leases are never stored: minted in the accept handler, attached to exactly one ActionRequest, dropped. No lease store exists to leak or replay from.

### D3. Event registrations (five; ADR-0009 idiom, 6-field schema)

- **`confirmation.requested`** — owner L3, actor `jarvis_runtime`. required: `confirmation_id`, `action_snapshot` (frozen: `tool_name`, `caller`, `canonical_target`, `target_entity_ref`, `risk_level`, `args_meta` = non-content args + `{content_sha256, content_bytes, content_artifact}`), `template_line` (the exact rendered action line — durable so consent binds to recorded machine truth), `expires_at_ms`. The `content` argument itself is **staged to `artifacts_root/pending_writes/<confirmation_id>`** at request time (§3.3.9: bounded payloads; also survives daemon restart, since the projection refolds and the artifact persists).
- **`confirmation.accepted`** / **`confirmation.rejected`** — actor `user`, `source_event_id` → the requested event. required: `confirmation_id`, `utterance_raw`, `grammar_rule_id`.
- **`surface.dismissed`** / **`surface.clarified`** — placeholder registrations per the handoff (spec-named durable forms, no emitters yet; declared placeholders, ADR-0009 precedent).

Registry count: 41 (unchanged by 0011 — its planned `entity.resolved` was already a Day-1 registration, ADR-0011 §12.4 N) → 46. Additionally, the existing `gate.evaluated` registration gains optional payload key `lease_id` — D2's single-use check folds consumption from it, so the key must be declared, not smuggled.

### D4. PendingConfirmations projection — the flow's only memory

Single-slot dataclass folded from `confirmation.requested/accepted/rejected` + `gate.evaluated` (lease consumption): `{confirmation_id, snapshot, template_line, expires_at_ms, state: pending | accepted_unconsumed | consumed | rejected | superseded}`. Rules:

- **Single slot** (deviation V4): a new `requested` while one is pending marks the old `superseded` — "是" always binds the newest ask, no ambiguity. Within one turn, only the **first** `confirm_required` becomes an ask; further L3 proposals in the same turn get the synthetic refuse result.
- **Lazy TTL**: expiry is judged at fold time against `expires_at_ms` (10 min default, config-overridable — the live burn uses a short value). No expiry event, no timer.
- **Packet + note**: packet gains block 8 `pending_confirmation`; `format_pending_confirmation_note` renders id-free plain text (tool, target, expiry) and injects via the insert-at-0 idiom — so an unrelated turn's LLM knows an ask is outstanding (C4) and a paraphrased consent turn's LLM can talk about it without being able to act on it (C6). WS has no replay and a banner can be missed; the projection is the truth, the banner is a courtesy.

### D5. Ask path — decide() distinguishes `confirm_required` for the first time

**LLM path** (`decision/__init__.py:1210-1225` seam): on `confirm_required` — freeze the snapshot (stage content artifact, compute sha256), emit `confirmation.requested` (rides `scratch.events`), inject a synthetic tool result ("等待 Allen 确认，本回合不可执行") and **end the tool loop**. The draft = optional LLM 铺垫 (scrubbed as usual) + the **template action line appended verbatim by the runtime** — rendered from the frozen snapshot only, never from LLM text (§13.4; consent binds machine truth):

> 待确认：write_file → `<canonical_path>`（<mode>，<N> 字节，风险 L3）。回复「可以」执行，「不要」取消。

Template text is fixed-vocabulary and scrub-safe (no 完成/done/verified — same discipline as the Tier 0 texts).

**Tier 0 path**: boot validation (extending `validate_tier0_table`) forbids Tier 0 rows targeting `requires_confirmation` tools, so `confirm_required` is unreachable there; the path keeps its `!= "pass"` refusal as defense-in-depth (declared, not dead code — it is the enforcement of that boot rule at runtime).

**Attention**: `_finalize_response` scans `scratch.events` for `confirmation.requested` (the `limitation_emitted` idiom) → channel `ask_confirm`. `AttentionChannel` Literal (`gates.py:475`) gains the value; `notify.py:28` already maps it (banner + stdout); `ask_confirm` is **not** added to the ADR-0009 TTS suppression set — the question speaks. Turn ends (serial-watcher constraint); the answer is a new turn by construction.

### D6. Answer path — deterministic grammar, then everything follows from events

`_handle_utterance` gains one step **before** `tier_0_match`, active only while the projection holds a live (unexpired) pending entry: exact-sentence match against `config/confirm_grammar.yaml` (data-driven; same normalization tolerance as tier0_patterns — whitespace/case/punctuation/ASR spelling; zero generalization):

- **yes-set** (v1): 可以 · 好 · 好的 · 是 · 确认 · 执行吧 · 做吧 · yes · ok · go ahead
- **no-set** (v1): 不 · 不要 · 不用 · 否 · 取消 · 算了 · 别 · no · cancel

**Yes** → emit `confirmation.accepted` → mint lease (D2 fields; `expires_at_ms = now + 60_000` — the lease need only outlive gate + dispatch, seconds not minutes; `reason` = template_line; `source_confirmation_event_id` = the accepted event) → **deterministic re-proposal**: rebuild the ActionRequest from the frozen snapshot (new `action_id`, content re-read from the artifact and verified against `content_sha256` — mismatch aborts with a fixed error line), attach the lease, run the **full** `pre_action_gate` (no shortcuts: if policy/entity state changed since the ask, it fails closed and says so) → dispatch → fixed-template broadcast, `voice_notify`: 「write_file 已执行：`<path>`（<N> 字节）」 — backed by the ack→Execution Claim `executed`; wording deliberately stops at 已执行.

**No** → emit `confirmation.rejected`, clear the slot, fixed text: 「好，已取消：<template_line>」. No LLM in the loop on either branch.

**No-hit** (「行吧那就写进去吧」, anything paraphrased) → the turn proceeds normally (Tier 0, then LLM). The LLM sees the pending note but **has no path to a lease** — its worst case is proposing the action again, which produces a fresh `confirmation.requested` (superseding the old) and a re-ask. Safe failure is the design's load-bearing property; the ADR states it as an invariant: **no LLM output, under any phrasing, can cause an L3 dispatch — only the grammar can, and only via a minted lease through the full gate.**

**Expired pending** → grammar is skipped entirely (no live entry); a late 「可以」 is an ordinary utterance; the LLM answers it as conversation (its packet note shows no pending). C3 pins this.

### D7. What deliberately does not change

- `prompts/jarvis_v1.md` — untouched. Its four "ask first" instructions become *true* by machinery, not by editing. Any future prompt note about the mechanics is interface documentation gated on live-burn evidence, never a behavior fix.
- The five existing lease-less construction sites — untouched; only the deterministic re-proposal constructs a lease-bearing ActionRequest.
- `attention_policy()`'s existing 3-channel logic — `ask_confirm` is set by the finalize scan (like the limitation override), not by new policy branches.

---

## 4. Failure Modes

| failure | behavior |
|---|---|
| malformed lease reaches gate | `lease_validated=False`, reason `lease_malformed` — refuse, not KeyError (D2.1) |
| content artifact missing/hash mismatch at accept | abort re-proposal, fixed error line, slot → consumed (no retry with corrupt content) |
| daemon restart between ask and answer | projection refolds from the log, artifact persists — pending survives; banner is not re-shown (projection note covers it) |
| second L3 proposal same turn | synthetic refuse result; only the first becomes the ask |
| new ask while one pending | old marked `superseded`; "是" binds the newest |
| gate refuses the re-proposal (policy/entity drift since ask) | fixed line reporting the refusal reason; slot → consumed |
| "是" with no/expired pending | grammar inactive → ordinary turn |
| write_file handler I/O error | error observation → Limitation routing (existing machinery) |

## 5. Spec Deviations Declared

- **V1**: `confirmation.requested` is a plan-original event type (spec's durable list lacks an ask event); all five confirmation-adjacent payload shapes are original design (spec §5.4.3 has none). Registered properly per §5.4 before any emit.
- **V2**: confirmation TTL 10 min, lease TTL 60 s, max_uses=1 — design values filling bare field names; config-overridable (burn uses short TTL).
- **V3**: the four UX mechanics (exact-sentence grammar, runtime-appended template line, frozen-snapshot deterministic re-proposal, fixed-text rejection) are original mechanisms; spec is silent and their spirit is mandated by C5/I7/§13.4.
- **V4**: single-slot pending, though §3.4.4 sketches `ConfirmationRequest[]` — v1 restricts to one to keep bare 「可以」 unambiguous; multi-slot deferred.
- **V5**: lease minted by the L3 accept handler — spec never names the minter; inferred from the L3-decides/L4-enforces split (§3.5.5, §3.1, §2.1) and declared as inference.
- **V6**: channel name `ask_confirm` retained (code vocabulary) for spec's `confirmation_request` — existing naming map precedent (`queue_review` ≙ `queue`).

## 6. Build Order

Each step one commit, Tier-1 green, five-part body. 1→3 ordered; 4 after 2; 5 needs 2–4; 6 needs 5; 7 closes.

1. **Lease ×9 + gate v2** — schema replace, shape/expiry/scope checks, KeyError fix. Zero behavior change (all sites still pass None). Acceptance: data-driven gate table (malformed/expired/wrong-tool/wrong-target/valid).
2. **Registrations** — five events, registry 42→47. Acceptance: registry canary + emit-side schema checks.
3. **PendingConfirmations** — projection + packet block 8 + note. Acceptance: fold table (requested→accepted→consumed / rejected / superseded / lazy-expired).
4. **write_file** — registration + handler + write-target resolution extension. Acceptance: handler table (create/overwrite/append × exists/absent/out-of-scope) via direct dispatch under an injected lease.
5. **Ask path** — both call-site seams, snapshot freeze + artifact staging, template line, `ask_confirm` Literal + finalize scan, tier0 boot rule. Acceptance: scripted decide() turn produces requested-event + spoken template + ended turn.
6. **Answer path** — grammar + yaml + accept/reject handlers + mint + deterministic re-proposal + broadcasts + single-use fold check. Acceptance: grammar data-driven table; scripted yes/no round-trips.
7. **Live burn** — C1–C6 below, one session, burn log committed.

## 7. Acceptance

### Tier 1 (every commit)
lint-imports KEPT · ruff clean · mypy strict clean · hermetic tests pass · wall <30 s.

### Tier 2 — live burn (real daemon, real LLM, real voice where possible)

| row | scenario | pass condition |
|---|---|---|
| C1 | 「帮我把『买牛奶』写进 scratch 的 shopping 文件」→ 语音模板问话 → 「可以」 | file contains the content; audit chain on the log: `confirmation.requested` → `confirmation.accepted` → `gate.evaluated(pass, lease_id)` → dispatch → result; broadcast says 已执行, nothing stronger |
| C2 | same ask → 「不要」 | `confirmation.rejected`; file untouched; fixed cancel line |
| C3 | same ask → wait past (shortened) TTL → 「可以」 | no execution; ordinary LLM turn; no lease anywhere on the log |
| C4 | pending live → 「现在几点」 | Tier 0/LLM normal; pending intact; answer turn unaffected |
| C5 | scripted: replay a consumed lease into the gate | refuse via single-use fold check, reason names the consumed confirmation |
| C6 | same ask → 「行吧那就写进去吧」 | grammar no-hit → LLM turn; **no lease minted**; at most a fresh `confirmation.requested` (supersede) + re-ask; zero dispatch |

### Definition of Done
All 6 rows green in one burn log · grammar and gate tables committed · registry 46 · flagship C1 spoken end-to-end (voice in, TTS question out, voice 「可以」, TTS 已执行 broadcast).

## 8. Consequences

- I7 becomes enforced reality; the four prompt instructions stop being decorative; `confirm_required` stops being dead code at both call sites.
- The lease audit chain is complete and queryable: who asked (requested), who consented (accepted, actor=user, verbatim utterance + rule id), what exactly was authorized (template_line + snapshot + sha256), and the one dispatch it produced (gate.evaluated carries lease_id).
- The KeyError hole closes as a side effect of the schema replace, not a bolt-on guard.
- New config: `confirm_grammar.yaml`, TTL keys. No new dependencies, no new secrets.
- Carried debt: multi-slot pending, read-back post_action_check, worker-agent leases — all named in defer.

## 9. Module Map

| piece | location |
|---|---|
| AuthorizationLease ×9 | `jarvis/shared/__init__.py` |
| gate lease validation v2, `ask_confirm` Literal | `jarvis/decision/gates.py` |
| registrations ×5 | `jarvis/state/event_log.py` |
| PendingConfirmations fold | `jarvis/state/projections.py` |
| packet block 8 + note render | `jarvis/decision/packet.py` |
| ask/answer seams, grammar hook, mint, re-proposal, finalize scan | `jarvis/decision/__init__.py` |
| `write_file` handler + registration | `jarvis/execution/tools.py` |
| grammar table | `config/confirm_grammar.yaml` |
| content staging | `artifacts_root/pending_writes/` |
