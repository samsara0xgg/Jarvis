# Spec completion audit — 2026-09-07 (HEAD 728afae)

Question: how much of `docs/spec.html` is actually built, and did the recent
realtime program (431 commits, +35672 lines) break the six-layer design?

Answer: roughly 40% of the spec is built (roughly 55-60% excluding what
ADR-0001 explicitly defers), and the structure is intact. Four drift audits
raised seven candidate deviations; adversarial refutation killed five and
downgraded both survivors to maintainability notes. The two real defects found
are both pre-existing, not products of the recent window.

## Method

Sixteen subagents: eight layer surveys reading `docs/spec.html` section
ranges against the code that should implement them, four drift audits over the
recent commit window, and four adversarial refutation passes (one per drift
audit, instructed to default to `refuted=true`). 2.04M subagent tokens, 729
tool calls, 0 agent failures.

Provenance matters for reading this document:

- The eight completion percentages are **subagent-derived counts** against
  spec commitments (fields present, invariants enforced, channels reachable),
  not a measured metric. Each survey recorded the rationale for its number.
- `lint-imports`, the canary suite, and the two defects in
  § Pre-existing defects were **verified in the main context**, not delegated.
- Drift findings were produced by one agent and refuted by a second that
  re-read the cited spec lines and code independently.

Drift window: `7b69a53..HEAD` — 431 commits, 114 files, +35672/-790 lines,
almost entirely realtime voice and streaming.

## Completion by layer

| Slice | Spec sections | % | Summary |
|---|---|---|---|
| L1 Constitution + Policy/Mode + Invariant Gate | S1, S2, S3.2, S11, S13 | 45 | 10 of 12 invariants enforced with citable code; the policy object is a placeholder |
| L2 Event Log + State Object + Projections | S3.3, S4, S5, S6 | 58 | The strongest layer; the event spine is real and trigger-enforced |
| L2 Task Ledger | S7 | 25 | The thinnest layer; a conversation model wearing Task Ledger vocabulary |
| Evidence Model + Memory Boundary | S8, S9 | 32 | Evidence roughly two-thirds real; Memory is zero on main |
| L3 Decision | S3.4, S10, S12, S17 | 42 | Three gates real; Attention Policy is a five-branch tree, not the specced layer |
| L4 Execution | S3.5, S14, S15 | 42 | Tool Surface safety mechanics solid; Agent Protocol largely absent |
| L5 Surface | S3.6, S18, S19 | 38 | Two of eight surfaces built to a high standard, six absent or crippled |
| L6 Deployment + Federation | S3.7, S16 | 28 | Nearly everything missing is ADR-0001-deferred, not drift |

Weighting the eight slices by spec line count gives roughly 40%.

## Structure is intact

Four independent checks, all run in the main context at HEAD 728afae.

**Import contract.** `lint-imports` — 98 files, 309 dependencies analyzed,
`6-layer architecture KEPT`, 1 contract kept, 0 broken.

**Canary suite.** `tests/canary` — 221 passed, 1 skipped. This is not an
ordinary test directory; 56 of its files are architectural guards
(`test_layer_ownership_boundaries`, `test_no_projection_writes`,
`test_status_not_stored`, `test_stream_emission_owners`,
`test_canary_reviewer_in_l3`, `test_canary_cost_recorded_l3_only`,
`test_canary_voice_layer_imports`, `test_canary_realtime_adoption_tiers`).

**ADR-0001 acceptance criteria H1-H13**, all present and green
(`docs/adr/0001-mac-only-flagship-scenario.md:860-915`):

| | Guards | Canary |
|---|---|---|
| H1 | Only L2 writes events / projection tables | `test_no_projection_writes.py` |
| H2 | Every emitted event type is registered | `test_emit_event_registered.py` |
| H3 | Pre-emit Gate token cannot be stale or absent | `test_pre_emit_required.py` |
| H4 | `LLMClient` is not substituted by a mock | `test_no_llm_substitution.py` |
| H5 | Layer imports (pytest-visible mirror of lint-imports) | `test_layer_imports.py` |
| H6 | Every ruff ignore carries a justification comment | `test_lint_ignores_justified.py` |
| H7 | No recorded/cassette LLM traffic | `test_no_recorded_llm.py` |
| H8 | No hardcoded `~/.jarvis` outside `jarvis/deployment/` | `test_no_hardcoded_runtime_root.py` |
| H9 | `jarvis.decision.decide` is not patched or wrapped | `test_decide_not_substituted.py` |
| H10 | Resolver is LLM-free; `entity.resolved` carries candidates | `test_resolver_purity.py` |
| H11 | Task status is derived, never stored | `test_status_not_stored.py` |
| H12 | Each gate body contains its MUST-check primitives | `test_gate_contracts.py` |
| H13 | Layer ownership beyond what the DAG alone allows | `test_layer_ownership_boundaries.py` |

**Immutability is enforced by the database, not by convention.**
`jarvis/state/event_log.py:148-161` installs SQLite `BEFORE UPDATE` and
`BEFORE DELETE` triggers on the `events` table that `RAISE(ABORT, ...)`
unconditionally. Cross-layer invariant 2 (S3.8, `docs/spec.html:1372-1383`)
therefore cannot be violated by application code at all. The single
`UPDATE events` in the tree (`jarvis/state/event_log.py:1318,1324`) is the
labelled schema-v1 migration.

## Drift audit: seven candidates, five refuted

### Audit 1 — does L5 Surface now hold L3 decision logic?

One candidate finding, refuted.

**F1. TTS `required_gate_mode` falls back to the most permissive risk class**
(cosmetic, REFUTED, high confidence). `jarvis/surface/voice_tts.py:2828`,
`jarvis/surface/voice_media.py:1953` and
`jarvis/runtime/inherent_loop.py:1477` all default an absent
`required_gate_mode` to `"sentence"` rather than the conservative
`"full_text"`. Refuted on three grounds: the default is an explicit
cross-layer contract decision in ADR-0005 §5.3 with the field deliberately
optional in the event schema (`jarvis/state/event_log.py:983-993`) for legacy
emitters; both live production emission sites
(`jarvis/decision/response_run.py:713`, `jarvis/surface/cli_render.py:180`)
always populate it from the L3-derived plan, so the fallback is unreachable
from any live decision path; and `git merge-base --is-ancestor` places all
three code sites and ADR-0005 before `7b69a53`, i.e. outside the audited
window entirely.

That this audit could find only one candidate — itself out of window — is the
substantive result. See § What the new work got right.

### Audit 2 — does new L2 state bypass the Event Log?

One candidate finding, refuted.

**F2. `decision_trigger_consumptions` marker can outlive its turn**
(contract, REFUTED, high confidence). `jarvis/state/trigger_consumption.py:29-38`
lets the marker row alone satisfy `trigger_was_consumed()` via `UNION ALL`
before any `turn.ended` exists, and the marker is written mid-loop at
`jarvis/runtime/__init__.py:2483-2488`. The auditor inferred a crash window in
which a trigger is skipped forever with no durable record. Refuted on the call
path: the only caller is `_system_trigger_watcher`
(`jarvis/runtime/inherent_loop.py:3439`) whose poll cursor is anchored at boot
to `_latest_id()` (`inherent_loop.py:579-587`), so a pre-crash row is never
re-examined regardless of the marker; `_SYSTEM_TRIGGER_TYPES`
(`inherent_loop.py:3182-3185`) is only `action.timeout_assumed` and
`action.failed`, excluding two of the four trigger types named; and
`reconcile_open_responses` (`jarvis/decision/response_run.py:877-896`), wired
at boot, force-closes every still-open run as
`response.failed(reason="daemon_restart")`, so the trigger's fate stays
reconstructible from the log.

### Audit 3 — is `runtime/` still the only wiring point, and is `shared/` an escape hatch?

Two candidate findings, both surviving, both downgraded.

**F3. `action_admission` crosses L3/runtime into L4 by ambient ContextVar**
(claimed contract, SURVIVES, medium confidence, reclassified). Verified:
`jarvis/shared/action_admission.py:1-32` is a ContextVar-backed guard; the
sole binder is `jarvis/runtime/__init__.py:2481`; the sole consumer is
`jarvis/execution/tools.py:4995` inside `ToolRegistry._emit_dispatched`.
`dispatch()`'s signature (`jarvis/execution/tools.py:4814-4819`) carries no
admission parameter and none of the three call sites in
`jarvis/decision/__init__.py` (1628, 2123, 4281) passes one. The refuter
downgraded severity: CLAUDE.md's literal rule is satisfied because runtime
remains the sole binder, no spec or ADR text mandates explicit-parameter
threading, and the claimed thread-propagation failure is dormant since
`decide()` runs synchronously inside the same `bind_action_admission` block.
Standing verdict: a design-consistency note — this is the one cross-layer
datum in the codebase that is not a typed frozen dataclass passed as a real
argument.

**F4. Runtime hand-writes the L3 `gate.evaluated` event**
(cosmetic, SURVIVES, high confidence). `jarvis/runtime/inherent_loop.py:1599-1629`
(`_emit_pre_emit_verdict`) emits `gate.evaluated` with the same payload shape
as `jarvis/decision/__init__.py:3270-3296`, because the commentary path calls
`pre_emit_gate` directly rather than through `decide()`. The two `reasons`
lists are currently identical only because both were kept in sync by hand
(`inherent_loop.py:1618-1622` versus `decision/__init__.py:3636-3642`). The
gate genuinely runs, so ADR-0001's gate contract is satisfied; the standing
risk is that a future payload change lands in only one of two producers.

### Audit 4 — has config/ become a hidden decision layer?

Three candidate findings, all refuted.

**F5. `interrupt_policy` (S11 dimension 2) is decided by config, not by the
Policy Resolver** (claimed structural, REFUTED, high confidence). The code
facts hold: the `barge_in` config block is parsed at
`jarvis/surface/voice_interrupt.py:260-292` and wired at
`jarvis/surface/voice_session.py:918-921`, while
`EffectivePolicy.interrupt_policy` is a hardcoded `"collaborate_default"`
(`jarvis/decision/policy.py:230`) that no code reads. Refuted as drift because
this is a declared Day-1 stub: `policy.py:116` names the field a placeholder
with "Future consumer: interrupt handling, wired when the policy engine
(Phase 4) lands", ADR-0011 D1 lists `interrupt_policy` as a placeholder by
name, and ADR-0006 D8 explicitly designs the config-driven `BargeInRouter` as
the interim mechanism. The refuter's one concession: ADR-0006/0008/0011 never
cross-reference each other's ownership language for this dimension, which is
worth a line in a future ADR touching the area.

**F6. `commentary.enabled` is a bare config switch, not a policy output**
(claimed contract, REFUTED, high confidence). Same class as F5.
`config/jarvis.yaml` `commentary.enabled` feeds
`Wave4ResponseFlags.lifecycle_commentary` (`jarvis/shared/realtime.py:107,130`)
with no `EffectivePolicy` reference, and `jarvis/decision/commentary.py:1-53`
is pure and total. Refuted because ADR-0011's defer table
(`docs/adr/0011-tool-surface-v1.md:297`) names `idle_proactivity (§11.1)` as
deferred to the Phase-4 policy engine explicitly.

**F7. Two realtime config values are read as untyped dict lookups**
(cosmetic, REFUTED, high confidence). `jarvis/runtime/inherent_loop.py:2108`
(`tts_volume`), `:2113` (`output_device`) and `:2116-2117`
(`streaming_requested`) are plain `Mapping.get` calls while every other
`realtime.*` value goes through a typed `from_mapping` dataclass. Refuted
because the surrounding comments (`inherent_loop.py:2103-2107`, `:2112-2114`)
state the exemption is a deliberate fail-loud-over-fail-silent choice: a type
guard would turn a mistyped key into a silent fallback to the wrong speakers.

## What the new work got right

The refutation passes produced more evidence for the design holding than
against it. Selected findings, all independently verified by the refuting
agent:

**Decision authority stayed in L3 through the whole realtime program.**
Two-stage barge-in detection lives in `jarvis/surface/voice_interrupt.py`
(a `BargeInRouter` that never imports `jarvis.decision` or `jarvis.runtime`
and never writes a durable event), but the authorization is
`jarvis/runtime/__init__.py:1895` reading `run.interrupt_policy` off the live
`ResponseRun`, whose policy is authored in `jarvis/decision/response_run.py`.
Cross-group foreground arbitration ("supersede vs decline vs enqueue") is
`decide_foreground`, a pure function at
`jarvis/decision/response_run.py:754-780`; `jarvis/surface/voice_media.py:2066-2163`
only enacts the returned verdict through an injected callable. Whether to
speak at all is read, not decided, in L5: `voice_media.py:1933` checks the
incoming `attention_channel` — already an L3 verdict — against
`_TTS_SILENT_CHANNELS`. ADR-0006's own claim that "Voice executes permitted
presentation; it does not derive risk"
(`docs/adr/0006-full-duplex-voice-session.md:965`) matches the code.

**New L2 modules hold no private truth.** `jarvis/state/input_claim.py` has no
private tables at all — `turn.started` and `consumer.adopted` are the only
truth, and idempotency is done by querying the immutable events table.
`jarvis/state/lifecycle_terminal.py` and `jarvis/state/stream_emission.py`
have zero side tables; every compare-and-swap is a query against events
followed by `append_event_in_transaction`.
`stream_emission.py:376-405` reconstructs "what was durably exposed, never
what memory believes was spoken" from `surface.response_chunk` events only.
`jarvis/state/committed_event_bus.py:17-21` states in its own docstring that
the Event Log remains the recovery source and a broken subscriber must never
look like a commit failure. The operational tables that do exist
(`authorized_dispatch_outbox`, `confirmation_consumption_claims`,
`cost_accounting_dispositions`) are written in the same transaction as their
canonical event and re-validated against those events on read, raising rather
than trusting the cache.

**No SQLite write exists anywhere outside `jarvis/state/`.** `INSERT INTO
events` occurs exactly once in the tree, at
`jarvis/state/event_log.py:1627`. The one repo-wide grep hit elsewhere
(`jarvis/decision/resolver.py:16`) is a docstring stating the rule.

**The three `stream_emission.py` files are a clean layered split, not smeared
logic.** `jarvis/shared/` holds only the contract dataclasses; `jarvis/state/`
owns the transactional persistence; `jarvis/surface/` is a 24-line
pass-through whose docstring says semantics remain owned by L3.

**`jarvis/surface/inherent_presenter.py`** is an explicitly scoped pure
mapping: "The presenter never derives canonical action state, confirmation
validity or cancellability" (`inherent_presenter.py:16-17`), and no branch
contradicting that was found.

**728afae is unusually well-evidenced for a config flip.** It names the exact
twelve switches turned on, cites three live-burn documents, records a live
boot showing `input_owner=single_ingress` / `models_ok=true` / no downgrade,
and adds `tests/canary/test_canary_realtime_adoption_tiers.py` pinning both
halves — `test_canary_tier_a_switches_ship_enabled` (the twelve stay True) and
`test_canary_tier_b_and_c_switches_ship_disabled` (the remaining eight stay
False). The three tier-C switches that stay off each carry a checkable reason
in `docs/adr/0006-full-duplex-voice-session.md:1023-1033`: `barge_in` missed
D9's false-candidate target by 15x in the 2026-09-05 burn (7.53/min against a
0.5/min target), `v2_sequencer` has no Swift client
(`RealtimeTransportV2.enabled` is false and unreferenced), and
`durable_expiry` writes rows that cannot be unwritten. `realtime.enabled`
itself defaults true as of `config/jarvis.yaml:151`, changed atomically in the
same commit as the ADR amendment and the canary.

**Wave-flag downgrade rules are centralized** at
`jarvis/runtime/__init__.py:645-693`, `:834-878` and `:883-931` — one function
per wave resolving the whole precondition graph with one warning and one trace
record per downgrade, rather than re-deriving preconditions at each call site.

## Pre-existing defects

Both were verified by hand in the main context, and both predate the audited
window.

**L1 is a document, not an enforced authority.** `jarvis/constitution/` has
never been imported by any runtime module. A repo-wide grep for
`import`/`from` statements naming `constitution` returns zero hits outside the
package itself; every reference is a docstring or comment (for example
`jarvis/decision/result_interpreter.py:508` citing
`jarvis/constitution/__init__.py:164` for the evidence-ceiling rule).
`tests/canary/test_layer_ownership_boundaries.py:20,124` only records that
importing it is permitted, never that anything must. So S2's "invariants 下穿
所有层" happens by an engineer hand-keeping L3 code in sync with L1 prose. If
`AUTONOMY_LEVELS` or a principle's wording changed, nothing downstream would
notice or break. Built 2026-05-17 in `b31b759` and untouched since.

**L3 performs a filesystem write.** `jarvis/decision/__init__.py:3909` calls
`artifact_path.write_text(content_str, encoding="utf-8")` inside
`_stage_and_request_confirmation`, staging pending write content to a side
path so the event payload stays bounded (the ADR-0012 D3/D5 rationale is cited
at `__init__.py:3877-3889`). It does not write the real target file. Whether
staged-artifact writing counts as the side effect S3.4 forbids L3 is a
judgment the spec text does not resolve explicitly, but it is the only
filesystem write in the layer. Introduced 2026-08-26 in `7de20f1`
("confirm_required becomes a real ask").

## Per-layer detail

Missing items are marked ADR-deferred (with the deferring document) or not
deferred.

### L1 Constitution + Policy/Mode + Invariant Gate — 45%

Counted: L1's 5 key objects (3/5 codified), S11's 9 policy dimensions
(3 live, 5 inert, 1 absent), S11.3 lenses (0/5), S11.4 composition (absent),
S11.2 dual-axis mode (0/12), S13's 12 invariants (10 enforced), S13.1's 3 gate
positions (3/3 wired).

Implemented:

- Constitution as frozen data — C1-C6, non-goals, 5 autonomy axes, all
  `MappingProxyType`/frozen dataclass/tuple (`jarvis/constitution/__init__.py:23-189`).
- I1 no state without event — `jarvis/state/event_log.py:1491-1656`.
- I2 events immutable — `jarvis/state/event_log.py:148-161`.
- I3 projections read-only — frozen dataclasses, `fold_projections` the sole
  rebuild path (`jarvis/state/projections.py:1-34,101-1671`); structural
  rather than a runtime check.
- I4 no action without effective policy — `EffectivePolicy` is a required
  positional parameter of `pre_action_gate` (`jarvis/decision/gates.py:301-306`).
- I5 tool surface enforced — `jarvis/decision/gates.py:450`,
  `jarvis/decision/policy.py:286-313`.
- I6 entity ID cannot be invented — `jarvis/decision/gates.py:175-244`.
- I7 high-risk requires confirmation — `jarvis/decision/gates.py:527`;
  `confirmation_threshold` pinned to L3 at `policy.py:225-226`, and ADR-0011
  guardrails it as a floor that mode may lower but never raise.
- I8/I11 worker cannot self-verify — worker self-reports fold to
  `level=reported` only (`jarvis/decision/result_interpreter.py:490-510`).
- I9 execution is not postcondition — the code-fixed
  `_SEMANTICS_TO_CLAIM` table (`jarvis/decision/result_interpreter.py:82-86`)
  means an ack cannot become a verified postcondition.
- I10 claim must not exceed evidence — `pre_emit_gate`
  (`jarvis/decision/gates.py:771-863`) plus the completion-keyword regex list
  (`jarvis/decision/pre_emit_phrases.py:20-40`).
- All three gate positions wired into the live loop: `pre_action_gate`
  (`gates.py:301`), Result Interpreter (`result_interpreter.py:146`),
  `pre_emit_gate` (`gates.py:771`).

Partial:

- Only 3 of 5 L1 key objects are codified; User Contract (S3.2.5) and Success
  Definition (S3.2.3) exist only as spec prose.
- `jarvis.constitution` is never imported — see § Pre-existing defects.
- 5 of 9 S11.1 dimensions (`output_form`, `verification_level`,
  `interrupt_policy`, `memory_write_policy`, `task_policy`) are declared
  `EffectivePolicy` fields carrying hardcoded constants that no code reads
  (`jarvis/decision/policy.py:78-119`).

Missing:

- `idle_proactivity` (S11.1 dim 1) — ADR-deferred,
  `docs/adr/0011-tool-surface-v1.md:43`.
- S11.3 lenses (audit / confidential / verbose / quiet / dry_run) — not
  deferred. None of the five names appear anywhere outside `spec.html`; the
  only trace is an always-empty `ModeRuntimeState.lenses` tuple.
- S11.4 `effective_policy` composition order — ADR-deferred,
  `jarvis/decision/policy.py:6-13` and ADR-0011 D1. `effective_policy()`
  (`policy.py:174-234`) returns one hardcoded Collaborate preset; grep for
  `personal_overrides`, `per_turn_overrides`, `mode_preset_table` returns zero
  hits, and both call sites invoke it with defaults.
- S11.2 dual-axis mode — consistent with ADR-0001's Mac-only scope for the
  Home axis, but no ADR defers the Work axis. `PolicyMode` is
  `Literal["Collaborate"]` (`policy.py:35`).
- I12 memory is not current state — ADR-deferred,
  `docs/adr/0001-mac-only-flagship-scenario.md:78-80`. Vacuously satisfied:
  nothing writes memory, and no enforcement exists for when it lands.

### L2 Event Log + State Object + Projections — 58%

Counted: event-log core mechanics roughly 90% faithful; 38 of 72 spec-named
event types registered; evidence level ladder complete but TTL staleness at
zero; 5 of 8 spec projections exist.

Implemented:

- Full `events` table schema with all four spec indexes
  (`jarvis/state/event_log.py:93-143`).
- Trigger-level append-only enforcement (`:148-162`).
- Registry-gated emit validating type, schema version and required payload
  before INSERT (`:1580-1596`).
- Claim and Evidence as event-derived records, not a mutable table
  (`jarvis/state/projections.py:631-754`).
- Projections are pure replayable folds with zero SQL writes
  (`projections.py:1-7,1636-1697`).
- Evidence ladder `reported < observed < executed < verified < accepted` with
  relation-aware `strongest_level_for` (`projections.py:86-96,201-226`).

Partial:

- `actor` and `ingestion_node` are stamped on write but structurally
  unreadable: the `Event` dataclass has no field for either
  (`jarvis/shared/__init__.py:103-131`) and the canonical SELECTs omit both
  columns (`event_log.py:1743-1753`), making `idx_events_actor_ts` write-only.
  `jarvis/surface/repo_observer.py:76-78` works around this by duplicating
  actor into the payload.
- `EventTypeSchema` (`event_log.py:203-235`) is missing 6 of the 11 required
  S5.4.1 fields: `producer`, `correlation_fields`, `projection_consumers`,
  `evidence_semantics`, `cross_domain_policy`, `artifact_policy`.
- S5.3 freshness/TTL: `freshness_ms` and `observed_at` ride through as opaque
  payload keys, never compared against a clock; no `stale_warning` is ever
  synthesized.
- Status Board and Entity Registry fold sources diverge from the literal S6
  table cell, each with a cited ADR-0009 rationale
  (`projections.py:866-870,968-1003`).

Missing:

- Mode Runtime State projection — not deferred. `mode.transitioned`,
  `lens.enabled`, `override.applied` are unregistered and never emitted;
  `jarvis/decision/policy.py:142,163` fabricates a fixed mode instead.
- Memory Projection — not deferred. `memory.proposed`, `observation.added`,
  `feedback.received` are unregistered; no `MemoryProjection` symbol exists.
- Drift Watch projection — not deferred. No `DriftWatch` symbol anywhere;
  `project.commit_seen` folds only into a passive `StatusBoard.latest_commits`
  cache (`projections.py:943-954`) with no evaluation or alerting.
- 34 of 72 spec-named event types unregistered — 14 are ADR-0001 cross-domain
  scope; the remaining 20 are spec-declared P2 phasing (S5.4.3) rather than an
  ADR deferral. Notable absentees: `intent.classified`, `task.activated`,
  `task.archived`, `run.completed`, `run.aborted`, `agent.spawned`,
  `agent.terminated`, `surface.failed`, `multimodal.staged`.

### L2 Task Ledger — 25%

Implemented:

- Task as a first-class event-folded object with derived-never-stored status
  (`jarvis/state/projections.py:102-147,301-364,484-570`).
- The iron rule: agent self-report cannot produce `verified_complete`; that
  requires `task.verified` plus a supporting non-refuted Postcondition claim
  at `verified` or `accepted` (`projections.py:301-336`).
- `run.started` with run/task correlation folded into `run_ids`
  (`jarvis/execution/tools.py:1043-1047`).
- `worker.reported` bridged back to its task via `run_id`
  (`projections.py:551-566`).

Partial:

- Task attributes: only goal, derived status, evidence link and `run_ids`
  exist. Rationale, owner, next step and needs-review have no field anywhere.
- `run.started` is emitted from exactly one call site with `runner` hardcoded
  to `"codex"` (`tools.py:166,1046-1047`), so `run_ids` can never contain a
  jarvis self-run or a human run as S7.5's own example shows.
- Run object: started-only. No `runner_type`/`runner_id`/`intent` split, and
  `run.completed`/`run.aborted` are never emitted anywhere.
- Turn-to-Task binding: the substrate exists (`task.created` correlation
  carries both ids, `tools.py:1754-1766`) but no function computes the task
  set for a turn.
- Task status: the code Literal is
  `{open, reported_complete, verified_complete, failed, cancelled, unverifiable}`
  against a spec enum of
  `{running, reported_complete, evidence_collected, needs_review, verified_complete, accepted, blocked}`.
  `_derive_status` only ever returns three of them.
- Worker sessions under a Task: `record_worker_identity`
  (`jarvis/execution/action_runner.py:443-457`) is git-stash cleanup plumbing,
  not a durable session record.

Missing, none deferred:

- Session / Episode as a first-class object (S7.4).
- **Active Task slot (S7.6)** — the most consequential gap in this slice.
  `SituationPacket` (`jarvis/decision/packet.py:50-104`, read in full) has no
  such field; `task.activated` is never emitted; the nearest analog,
  `scratch.active_subject_ref` (`jarvis/decision/__init__.py:914-916`), is
  re-derived every turn and forgotten, with no `entered_at` and no
  `promotion_reason`. Without it the runtime has no persisted notion of which
  task is currently primary across turns.
- Promotion criteria (S7.9). The spec's 8 triggers appear nowhere in code or
  in the live system prompt — `prompts/jarvis_v1.md` (the default per
  `jarvis/runtime/__init__.py:174`) contains no mention of `create_task`, the
  Task Ledger, or promotion. Promotion is unguided LLM discretion.
- `run.completed` / `run.aborted` lifecycle events.
- The `Run` name collision. `ResponseRun` / `OpenResponseRun`
  (`jarvis/decision/response_run.py`, `jarvis/state/response_runs.py`) is
  ADR-0008's per-turn LLM streaming FSM — it carries no `task_id`, no
  `runner_type`, and is unrelated to task advancement. It is the more built
  "Run"-named subsystem in the repo and it is not the spec's Run.

Note: the audit brief asserted `active_task` appears in
`state/projections.py`, `execution/tools.py` and `decision/resolver.py`. That
did not reproduce — direct grep of those three files returns zero hits. The
only occurrence is `jarvis/decision/gates.py:796`, inside a docstring naming
the spec's field.

### Evidence Model + Memory Boundary — 32%

Implemented:

- Typed `Claim` and `Evidence` records (`jarvis/shared/__init__.py:141-181`).
- Five-level evidence ladder with ranked comparison
  (`jarvis/shared/__init__.py:60`, `jarvis/state/projections.py:86-95`).
- `supports`/`refutes`/`limits` relation required on every evidence row
  (`jarvis/state/event_log.py:539`).
- Rule 1 — agent self-report supports only a Report claim
  (`jarvis/decision/reviewer.py:10-13,50-54`).
- Rule 2 — tool success supports only an Execution claim
  (`result_interpreter.py:81-87`).
- Rule 3 / S8.8 — Postcondition claim necessary for verified completion
  (`projections.py:306-370`).
- Rule 6 — absent evidence recorded as a Limitation claim
  (`result_interpreter.py:751-766,798-817`).
- Rule 7 / I10 — Pre-emit Gate enforces output ≤ evidence
  (`gates.py:771-874`, `decision/__init__.py:177-193,3464`).
- S8.10 strong-versus-weak output wording enforced mechanically
  (`gates.py:841-848,761-768`).
- A de-facto evidence pipeline: `jarvis/decision/reviewer.py` (291 lines),
  `jarvis/execution/verify_command_detect.py` (166), `diff_capture.py`, and
  the dual-slot `interpret_verify_diff_bundle`
  (`result_interpreter.py:602-899`).
- Append-only claim correction / supersede
  (`result_interpreter.py:318-402`, `projections.py:640-696`).

Partial:

- 5 of 7 claim classes reachable. Coverage Claim has zero code. Acceptance is
  scaffolded but dead — `claim.accepted`'s registry entry states no emitter
  exists yet because it needs a human-input surface, and grep confirms zero
  emit sites.
- Rule 4 scope is a coarse free-text string (`exit_code=0`), not the spec's
  targeted-versus-full-suite distinction.
- Rule 5 freshness is registered but never populated — zero emit sites for
  `freshness_ms` or `observed_at` anywhere.
- Evidence record: only 3 of 12 fields typed; the rest live in an untyped
  payload dict.
- Claim record: `subject_type`/`subject_id` collapsed into one free-text
  `subject_ref`; `required_for_completion` and `updated_at` absent.
- S8.9 code-task template: "no unrelated dirty diff" is handled procedurally
  by `diff_capture.py`'s auto-stash isolation rather than as an explicit claim.
- S8.9 Mac-operation template: the pre-target-verification leg was not
  confirmed present or absent — flagged as not checked.

Missing:

- Coverage Claim class — not deferred.
- Smart Home acceptance-claim template — ADR-deferred,
  `docs/adr/0001-mac-only-flagship-scenario.md:411`.
- **The entire S9 Memory Boundary on main** — not deferred by any ADR. Every
  S9 subsection has zero code; the only trace is
  `jarvis/decision/policy.py:117`'s unconsumed
  `memory_write_policy = "propose_only"`. All other "memory" grep hits are the
  Python term "in-memory".

  This gap has a complication worth recording: a spec-grounded ADR
  (`docs/adr/0013-memory-and-session.md`, status Proposed) was drafted and
  substantially implemented on unmerged branches — session lifecycle
  (`jarvis/decision/session.py`, 324 lines), a 10-check promotion gate
  (`jarvis/decision/memory_gate.py`, 1125 lines), a memory read path
  (`jarvis/decision/memory_context.py`, 874 lines) and `prompts/memory_write_v1.md`
  — across 8 commits (`4799010..795c7ef`, last 2026-08-27) on
  `worktree-adr0013-design` / `worktree-adr0013-impl`. Neither branch merged,
  and `worktree-adr0013-impl` has since diverged sharply from main
  (merge-base `a0f6264` against HEAD `728afae`). This is stranded work, not a
  deliberate spec deferral; landing it or consciously abandoning it is the
  highest-leverage decision for this slice.

### L3 Decision — 42%

Implemented:

- S17 Tier 0: closed regex whitelist, full-sentence anchored, table-driven
  from `config/tier0_patterns.yaml` (`jarvis/decision/tier0.py:71-154,230-257`).
- S17 Tier 2: cloud LLM as stateless calculator with a tool-use loop on Tier 0
  miss (`jarvis/decision/intent.py:65-141`, `decision/__init__.py:1237,1511`).
- S17 Tier 1 correctly absent — the spec itself freezes it.
- S10.6: the packet is assembled by the L3 assembler, never by State or the
  LLM (`jarvis/decision/packet.py:123-169`).
- S10.2 blocks 3, 4 and 5 (environment, recent interaction, evidence) are
  solidly built and bounded (`packet.py:245-330,348-424`,
  `decision/conversation.py:11-13,46-93`).
- Effective Policy Resolver as a pure function (`policy.py:39-119,174-234`).
- L3 carries no cross-layer imports and no subprocess/socket IO beyond the
  sanctioned Tier 2 LLM call.

Partial:

- S10.2 block 2 Active Task Context stripped to `task_id` and `goal` for every
  open task; status is derivable but never called by the note-builder, and
  `next_action`/`missing_evidence`/`owner`/`blocking_issue` have no renderer
  (`decision/__init__.py:3646-3672`).
- S10.2 block 1 Turn Context has no structured field on `SituationPacket` at
  all (`packet.py:49-105`).
- S12.5 hard gates: `attention_policy()`'s signature has no
  confidence/risk/reversibility/mode/feedback parameters, so 6 of the 7 named
  hard gates cannot be evaluated inside it; the 7th lives in `pre_action_gate`
  and is stitched on by a post-hoc scan (`gates.py:906-911`).
- The L3 filesystem write — see § Pre-existing defects.
- `config/tier0_patterns.yaml` defines 7 entries against the spec table's
  cited 17; direction of the discrepancy undetermined.

Missing, none deferred:

- S10.2 block 6 Memory/Profile hints — no field, no renderer, no consumer.
  `prompts/jarvis_v1.md:15-16` explicitly tells the LLM not to imply
  persistent memory, confirming the gap is deliberate-by-omission.
- S10.5 cache strategy is inverted. The spec requires the dynamic packet
  *after* the stable prefix so per-turn variation does not invalidate prefix
  caching; `_insert_system_notes` builds the stack with repeated
  `messages.insert(0, ...)` (`decision/__init__.py:994-1032`), and the code's
  own docstring (`:991-992`) flags the deviation as known and unfixed.
- S10.7 per-mode packet emphasis — moot while only one mode exists.
- S12.3: 11 of 15 candidate-intervention input fields are read nowhere.
  `attention_policy()`'s only inputs are `trigger_event.type`, a claim/evidence
  lookup and two booleans (`gates.py:922-928`).
- S12.4: 6 of 10 output channels have no producer. `AttentionChannel`'s
  Literal (`gates.py:903`) cannot express `badge_card`, `soft_suggest`,
  `interrupt_now`, `ambient_act`, `delegate_agent` or `suppress` — even though
  `jarvis/surface/notify.py:21-39` already maps five of them, making that L5
  plumbing unreachable dead code.
- S12.5 channel scoring formula — no weighted value/cost computation exists
  anywhere.
- S12.5 feedback adjustment loop — no enum, table or event type for
  accepted/ignored/dismissed/corrected/annoyed/useful. Attention choices can
  never self-correct.
- S12.6 per-mode attention defaults — blocked on the single-mode gap;
  `EffectivePolicy.attention_defaults` is an empty placeholder.

### L4 Execution — 42%

Implemented:

- All 6 spec caller principals as an enum (`jarvis/shared/__init__.py:41-56`).
- `AuthorizationLease` with the exact 9 spec fields and real
  single-use/scope/expiry semantics (`shared/__init__.py:187-227`,
  `jarvis/state/authorized_dispatch_outbox.py:1-90`).
- L0-L4 risk ladder as an ordered comparator consumed at the gate
  (`jarvis/decision/policy.py:120-134`).
- Defense-in-depth caller enforcement at both the L3 gate and L4 dispatch
  (`jarvis/execution/tools.py:4718-4725`).
- `requires_entity` with resolve-on-propose and a full-outcome audit event
  (`decision/__init__.py:3744-3808`).
- `post_action_check` chained verification (`tools.py:441-470,5725-5750`).
- `confirm_required` live end-to-end (`gates.py` GateOutcome,
  `decision/__init__.py:2028-2062`).
- The reported-versus-verified split enforced structurally
  (`tools.py:1806-1824`, `gates.py:936-1013`).
- `spawn_worker` is task-bound, requiring `task_id` (`tools.py:5691-5700`).
- No generic `execute_anything` tool exists.

Partial:

- Per-tool metadata: 9 of 15 spec fields present. Missing `side_effect`,
  `requires_task_binding`, `requires_evidence`, `max_output_bytes`,
  `allowed_modes`, `allowed_surfaces` — all six named in ADR-0011 §11's defer
  table, so declared rather than silent.
- Domain grouping is inert. `domain` is set on every tool but grep finds zero
  read sites; the docstring's claim that it feeds audit payloads is not true —
  `action.dispatched`'s payload (`tools.py:4876-4894`) carries no domain.
- S14.5 caller-by-layer matrix does not exist. `autonomy_ceiling` is one
  global value shared by every principal; 3 of the 6 principals
  (`worker_agent`, `background_subscriber`, `system_maintenance`) have no tool
  referencing them in `allowed_callers` at all, so their default is vacuously
  empty.
- WorkerReport: 8 of 9 fields flow end-to-end; `evidence_submitted` is
  accepted by the MCP schema and declared optional in the event schema but
  never copied by `_worker_report_extras` (`tools.py:900-921`), so a worker's
  cited evidence IDs never reach the durable event. The status vocabulary
  `{ok, partial, failed, blocked}` also differs from the spec's
  `{reported_complete, blocked, failed}`.

Missing:

- **SandboxPolicy on the worker_agent path (S3.5.12)** — not deferred, and the
  one gap representing live unmitigated risk. The only boundary is Codex's own
  `workspace-write` sandbox (`jarvis/execution/codex_action.py:292-320`),
  which bounds file writes to the spawn cwd but does nothing about `git push`,
  `rm -rf` inside the writable root, or reading a secret file inside the repo.
  Grep for push / reset --hard / destructive / secret across
  `codex_action.py` and `action_runner.py` returns zero hits.
- The Task / Agent Run / Agent Session object model and the 12-state Agent Run
  FSM — not deferred. Zero grep hits repo-wide for `agent_run`, `AgentRun`,
  `verified_by_jarvis`, `accepted_by_allen`, `reported_blocked`. Nothing in
  the system can represent "this specific agent run is stuck" as durable state.
- Agent Context Package as a structured envelope (S15.3) — not deferred.
  `spawn_worker`'s only input is `task_id`; none of scope / allowed_files /
  do_not_touch / current_known_state / relevant_evidence / acceptance_claims /
  verification_commands / output_contract is assembled per run.
- Blocked-report structure (`blocker_type`, `what_tried`, `why_blocked`,
  `question_for_allen_or_jarvis`) — not deferred. A blocked report is
  structurally indistinguishable from any other.
- The agent stuck protocol (S15.7) — not deferred, and structurally precluded:
  every mid-run Codex elicitation is auto-accepted by `_auto_respond_server_request`
  (`codex_action.py:472-511`) so the turn does not hang. There is no
  `waiting_for_input` state and no live channel from a running worker to
  Allen; the only ask surface is the terminal `status=blocked` field submitted
  after the run has already ended.
- `LegacyAdapter` (S3.5.10) — ADR-deferred, ADR-0011 §11 (no legacy-envelope
  tool exists to adapt while smart_home is deferred).
- `RawResult`'s spec-named bounded-payload fields (`summary`, `artifact_ref`,
  `truncated`, `size_bytes`, `content_hash`, `mime_type`, `retention_policy`)
  — not deferred. Bounding discipline is real but achieved ad hoc per tool
  rather than through one enforced contract.

### L5 Surface — 38%

Implemented:

- Voice: full-duplex wake capture, acoustic plus semantic endpointing,
  keyword/PTT barge-in, streaming TTS with a macOS `say` fallback
  (`voice_session.py:761-859`, `voice_interrupt.py:1-110`,
  `voice_tts.py:3404-3436`).
- Inherent Panel: v2 WS server, pure ViewModel-to-wire presenter, bounded
  per-client lanes with snapshot-plus-delta handoff, and a 14239-line Swift
  client (`inherent_presenter.py`, `runtime/inherent_hub.py`,
  `runtime/inherent_view_sequencer.py`, `desktop/inherent-swift/`).
- L3 channel to L5 physical-surface mapping with Pre-emit-gated emission
  (`notify.py:21-39`, `cli_render.py:244-533,362-375`).
- `UserResponse` durable form emitted by L3, not L5
  (`decision/__init__.py:4007`).
- Input adapter raw-to-canonical discipline with no significance filtering at
  the adapter (`repo_observer.py:327-356`).

Partial:

- Mac Notification: `deliver_banner`/`deliver_voice` exist but both production
  daemon entry points hardcode `available_surfaces=frozenset()`
  (`inherent_loop.py:844,1856`), so the dispatch loop skips every physical
  surface there. They are reachable only through the legacy CLI path. No rate
  limiting exists anywhere near `deliver_banner`, so even that path violates
  S18.12 rule 4.
- Obsidian: read-only. `search_notes` exists; no write/create note tool. The
  generic `write_file` could target the vault but is not Obsidian-aware and
  lives in L4.
- Freshness propagation: the Swift client already declares and decodes
  `freshnessMs` (`RealtimeViewDTOs.swift:297,309`) but
  `inherent_presenter.py:132-134` never populates it — "the shipped decoder
  reads its absence as fresh". Every action renders fresh regardless of true
  staleness, which is the exact failure mode S3.6.9 names.
- Surface fallback: a real mechanical TTS fallback exists, but the canonical
  `surface.failed(surface_id, reason, fallback_used)` event is never emitted.
  Both sites defer the deviation to "ADR-0007", which does not exist in the
  repo.
- Agent/Worker handoff exists but entirely under `jarvis/execution/` (L4), not
  `jarvis/surface/`.
- The Inherent Panel ephemeral lane has no production producer;
  `enqueue_ephemeral` is exercised only by tests
  (`runtime/inherent_hub.py:33-35`).

Missing:

- Dashboard / Cockpit (S18.5) — not deferred. Zero matches repo-wide. The
  Inherent Panel's action sections partially cover the content on the wrong
  surface per the spec's low-versus-high-interruption distinction.
- OLED / Room Display (S18.7) — ADR-deferred, `docs/adr/0001-legacy-scan.md`
  DEFER-STAGE-2.
- Environment Action / Hue (S18.8) — ADR-deferred,
  `docs/adr/0001-legacy-scan.md:72`.
- Multimodal staging (S3.6.8) — not deferred. `multimodal.staged` is fully
  specified and entirely unimplemented; the event type is not even registered.
- Dynamic surface selection (S3.6.4, S18.10, S18.11) — not deferred. The
  implementation is a static 9-entry dict keyed only on the channel string
  (`notify.py:21-39`), with no runtime evaluation of availability, freshness
  preference, recent activity, fallback chain or focus state.
- `InputAdapter` / `OutputSurface` shared abstraction (S19) — not deferred.
  Each adapter is bespoke and directly coupled to its transport; there is no
  common contract (availability probe, failure threshold, cooldown) a new
  adapter must satisfy.
- Mac Observer focus / clipboard / screen / window (S19) — not deferred. Of
  the five signals S19 lists only git is a real continuous observer; clipboard
  exists only as an on-demand L4 pull tool.
- ESP32 sensors and the git post-commit hook (S19) — not deferred explicitly,
  though both are RPi-adjacent and consistent with Mac-before-RPi scoping.

### L6 Deployment + Federation — 28%

Implemented:

- Mac sleep/wake protocol (S3.7.8): best-effort `mac.sleeping` plus
  `worker.suspended_by_sleep` before sleep, mandatory `mac.awake` and
  reconciliation on wake, fail-closed `worker.terminated_by_sleep` and
  `action.timeout_assumed` (`jarvis/deployment/sleep_wake.py:573-761`, wired
  at `jarvis/runtime/inherent_loop.py:4459` inside `serve_inherent`). The
  reconciler is live, not dead.
- Supervisor sweep for orphaned actions past `result_expected_by`
  (`sleep_wake.py:767-853`, wired at `inherent_loop.py:3508-3560,4429-4432`).
- launchd LaunchAgent residency (`jarvis/deployment/launchd.py:247-495`).
- Per-domain single-writer lock with stale-pid recovery
  (`jarvis/deployment/process_lock.py:109-222`).
- Runtime root bootstrap and artifact store placement
  (`jarvis/deployment/__init__.py:75-92,173-209`).

Partial:

- Scheduler / DeferredExecution (S3.7.9) — ADR-deferred (ADR-0009 §12,
  ADR-0011). Zero `scheduler.*` event types registered and no code in
  `jarvis/deployment/`. This is the largest S3.7 subsection with zero code.

Missing, all ADR-deferred by ADR-0001's Mac-only scope (reaffirmed in
ADR-0002 V4, ADR-0003, ADR-0005, ADR-0009 §12 and ADR-0011):

- S16.1 node partitioning (RPi5 home node, `home_events.db`, Mac's read-only
  mirror).
- S16.2 resource master arbitration.
- S16.3 cross-domain communication. No `cross_domain.*` event types are
  registered and no MQTT code exists in the repo.
- S16.4 federation failure behavior.

The deferral is total rather than partial: not even a stub or a
registered-but-unused event type exists.

## Recommended work order

1. **Wire `jarvis/constitution/` in, or move it to `docs/`.** Today it is a
   constitution nobody reads. Either L3 imports its values or it should stop
   presenting as code.
2. **Build the Active Task slot (S7.6).** It is the one gap that leaves every
   layer above it guessing: the Situation Packet's task block, any relevance
   scoring in Attention Policy, and L4's task-scoped permissions all depend on
   a persisted answer to "which task is primary right now".
3. **Rename `ResponseRun`.** Resolve the collision with the spec's Run before
   the Task Ledger is built; afterwards it is far more painful.
4. **Enforce SandboxPolicy on the worker_agent path.** The only live,
   unmitigated safety gap in the audit.
5. **Move the L3 `write_text` out, and collapse the duplicated
   `gate.evaluated` producer.** Both are small, bounded changes (F4 above and
   § Pre-existing defects).
6. **Decide the fate of the stranded ADR-0013 memory work.** Land it or
   abandon it deliberately; leaving 2300 lines on a diverging branch is the
   worst of both.

Housekeeping: the working tree at audit time carried uncommitted, unignored
artifacts from earlier agent runs — `COORDINATION.md`, `findings.md`,
`task_plan.md`, `output/`, `docs/archify/`, `docs/burn-results-checklist.md`,
`action-runner-check.txt`, and two `.local-2026-09-05` files. None are code;
they should be committed, ignored, or removed.
