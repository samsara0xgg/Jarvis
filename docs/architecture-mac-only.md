# Mac-only Architecture (First Round)

## Status

Active. This is the current operating scope of Jarvis — single Mac domain,
no RPi / smart-home / cross-domain. RPi expansion is reserved for after the
Mac loop stabilises.

See [`spec.html`](spec.html) for the canonical federated 6-layer specification.

## Plan

1. The first scenario is Mac-only.
2. No RPi expansion until the Mac loop is stable.

## Diagram

```
                ════════ INPUTS  (L5 Surface) ════════
        Voice (ASR · VAD) · Inherent button · Mac observer
        (clipboard · screen · active app · git status)
        · Scheduler (in-proc + launchd) · Worker report
        · Multimodal staged image / file
                          │  canonical events / artifact_refs
                          ▼
            ╔══════════════════════════════════════╗
            ║  [L2 State Object]  脊柱               ║
            ║                                       ║
            ║  Event Log (append-only, immutable)   ║
            ║    单一物理库: mac_events.db (SQLite)  ║
            ║    payload bounded · artifact_ref     ║
            ║    versioned schema · UUIDv7 event_uid║
            ║         ↓ continuous fold             ║
            ║  Projections (read-only):             ║
            ║    Task Ledger · Status Board(Mac) ·  ║
            ║    Mode Runtime · Recent Trace ·      ║
            ║    Memory · Claim/Evidence ·          ║
            ║    Entity Registry · Drift Watch      ║
            ╚══════════════╤══════════════════════╝
                           │  trigger:
                           │   utterance.received /
                           │   agent.reported(prio=high) /
                           │   surface.user_intent /
                           │   scheduler.fired /
                           │   confirmation.accepted /
                           │   action.timeout_assumed
                           ▼
            ╔══════════════════════════════════════╗
            ║  [L3 Runtime Decision]  judgement     ║
            ║                                       ║
            ║   Situation Packet Assembler          ║
            ║   Effective Policy Resolver           ║
            ║   Intent Routing                      ║
            ║     ─ Tier 0 regex (closed pattern)   ║
            ║     ─ Tier 2 cloud LLM (default)      ║
            ║         (Tier 1 surrogate 后置)        ║
            ║   Attention Policy                    ║
            ║     hard gates → channel scoring      ║
            ║   Pre-action Gate                     ║
            ║     I1 · I5 · confirm · lease ·       ║
            ║     entity resolve check              ║
            ║   Result Interpreter                  ║
            ║     raw → claim/evidence events       ║
            ║   Pre-emit Gate v0                    ║
            ║     template + keyword risk guard     ║
            ║   ResponsePlan (sentence/full/struct) ║
            ╚══════════════╤══════════════════════╝
                           │  gated ActionRequest
                           │  (action_id · run_id ·
                           │   caller_principal ·
                           │   risk · timeout)
                           ▼
            ╔══════════════════════════════════════╗
            ║  [L4 Capability Execution]  hands     ║
            ║                                       ║
            ║   ToolRegistry per caller_principal   ║
            ║     observer · regex_router ·         ║
            ║     jarvis_llm · worker_agent ·       ║
            ║     background_subscriber ·           ║
            ║     system_maintenance                ║
            ║   SandboxPolicy (cmd · path · net)    ║
            ║   AuthorizationLease enforce          ║
            ║   ActionLifecycle 8-state             ║
            ║     proposed → authorized →           ║
            ║     dispatched → running →            ║
            ║     result_observed / failed /         ║
            ║     timeout_assumed / cancelled       ║
            ║   post_action_check chain             ║
            ║   WorkerRun (Codex / Claude Code /     ║
            ║     Hermes) · LegacyAdapter ·         ║
            ║   ArtifactStore (Mac local)           ║
            ║                                       ║
            ║   工具域: file/git/repo observe ·      ║
            ║     terminal / write_file (worker) ·  ║
            ║     clipboard · screen · notify ·     ║
            ║     spawn_worker · submit_report      ║
            ╚══════════════╤══════════════════════╝
                           │  RawResult + lifecycle events
                           ▼
                ════════ OUTPUTS (L5 Surface) ════════
        Voice TTS · Inherent panel · Mac notification ·
        Defer · Suppress
          (AttentionRouting channels:
           silent_log · queue · badge · voice_notify ·
           interrupt · confirmation_request ·
           delegate_agent     ※ 无 ambient_act)
                           │
                           └── 所有 observable effect ──┐
                               emit 回 [L2] Event Log    │
                                                         │
            (next decision 从 updated state 启动) ◀──────┘

   ┌─ [L1 Constitution] shapes invariants for all of above ─────┐
   │   C1 Allen-only · C2 State-Object identity ·               │
   │   C3 Reduce context/task load ·                            │
   │   C4 Supervisor not worker ·                               │
   │   C5 Evidence-bound privilege ·                            │
   │   C6 Surfaces replaceable, continuity not                  │
   └────────────────────────────────────────────────────────────┘

   ┌─ [L6 Deployment]  Mac single domain ───────────────────────┐
   │   mac_events.db (SQLite, append-only, WAL)                 │
   │   Mac artifact store (~/.jarvis/artifacts/)                │
   │   Mac sleep/wake:                                          │
   │     before-sleep: emit mac.sleeping, flush events          │
   │     after-wake:   emit mac.awake(slept_for_ms)             │
   │                   mandatory reconciliation scan            │
   │                   close orphan dispatched/running actions  │
   │   Scheduler: in-proc + launchd 唤醒兜底                     │
   │   Worker = bounded execution contexts (非独立 domain)      │
   │   ※ 无 SyncBridge / ImportedEvent / cross_domain.*         │
   │   ※ 无 多 domain schema 版本协商 / quarantine               │
   └────────────────────────────────────────────────────────────┘
```

## Dropped from the full federated 6-layer

- **L4 tools** — Hue / smart_home / MQTT publish / sensor read
- **L5 channels** — `ambient_act`; the remaining 7 attention channels stay
- **L2 events** — `cross_domain.*` family, `domain_availability.changed`,
  `domain_projection.stale`, `presence.inferred`, `sensor.observed`,
  `sensor.alert`, cross-publish of `mac.sleeping` / `mac.awake`
- **L6 objects** — `ImportedEvent`, `CrossDomainRequest`, `SyncBridge`,
  multi-`DomainOwner`, cross-domain schema versioning, quarantine
- **L3 triggers** — `cross_domain.response.received`,
  `domain_availability.changed`
- **DeferredExecution** — double-owner pattern reduces to Mac single owner
  + macOS `launchd` wake fallback

## What stays even Mac-only

L6 does **not** disappear. Mac itself sleeps, workers (Codex / Claude Code /
Hermes) need bounded execution contexts, deferred execution still needs a
wake-from-sleep story, the local artifact store still needs retention
policy. All of these live in `jarvis/deployment/`.

The architecture exists as a 6-layer system from day one — the L6 layer is
just trivially small (one domain, no transport).

## When RPi comes back

Future addition is **purely additive** — no restructuring of L1–L5:

- new `jarvis/deployment/transport/` (SyncBridge, MQTT bridge)
- new `jarvis/deployment/domains/rpi.py`
- new event family `state/events/types/cross_domain.py`
- new L3 triggers for `cross_domain.response.received`
- new L4 tools under `tools/<principal>/` for Hue / sensor etc.

The L1–L5 logic does not change. The `.importlinter` layer contract does
not change. This is the whole point of locking L6 as a separate layer.
