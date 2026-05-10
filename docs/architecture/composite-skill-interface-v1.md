# Composite Skill Interface v1

Reviewed at: 2026-05-10

## Scope

This document reserves the contract surface for future composite skills.
It does not implement a planner, add autonomous orchestration, or change the
current CC command behavior.

Jarvis skills are split into two layers:

- `primitive`: executes one bounded capability through the normal Runtime Gate.
- `composite`: orchestrates multiple primitive skills while preserving trace,
  evidence, and partial-success semantics.

## Manifest Fields

Primitive skills may omit `orchestration`. Composite skills must include:

```yaml
classification:
  layer: composite

orchestration:
  allowed_subskills: []
  atomicity: best_effort # best_effort | all_or_nothing | compensating_actions
  rollback_policy: none  # none | undo_supported | manual
  partial_success_policy: explicit
```

## Hard Rules

1. A composite skill must call subskills through `ToolRegistry.execute()`.
2. A composite skill must not call executor internals directly.
3. Every subcall must produce `jarvis.tool_result.v1`.
4. Every subcall must be written to `trace.tool_calls`.
5. A composite result must include the subcall `call_id`, `skill_name`, `status`,
   `outcome.type`, and `verification_source`.
6. Composite `claim_policy.allowed_claims` must be derived from verified subcall
   evidence.
7. Composite `claim_policy.forbidden_claims` must include any forbidden claims
   from failed, partial, unverified, or skipped subcalls.
8. Partial success must be explicit; it must never be compressed into success.
9. Rollback is not allowed unless a primitive subskill exposes a verified undo
   path, such as `undo_delete_todo`.
10. High-risk subcalls still require their own Runtime Gate confirmation.

## Result Shape

```json
{
  "schema_version": "jarvis.tool_result.v1",
  "skill_name": "example_composite",
  "status": "partial_success",
  "outcome": {
    "type": "composite_result",
    "verified": true,
    "verification_source": "subcall_results"
  },
  "data": {
    "subcalls": [
      {
        "call_id": "call_1",
        "skill_name": "create_reminder",
        "status": "success",
        "outcome_type": "created",
        "verification_source": "reminder_store_ack"
      },
      {
        "call_id": "call_2",
        "skill_name": "obsidian_add_to_inbox",
        "status": "failure",
        "outcome_type": "failed",
        "verification_source": "none"
      }
    ]
  },
  "claim_policy": {
    "allowed_claims": ["reminder_created"],
    "forbidden_claims": ["all_subtasks_completed"]
  }
}
```

## Non-Goals

- No large autonomous planner in v1.
- No cross-tool rollback unless primitive undo is verified.
- No bypass around lifecycle, exposure, entity resolution, confirmation, or
  direct-execute policy.
- No composite skill should repair weak primitive contracts; primitive evidence
  must be fixed at the primitive layer first.
