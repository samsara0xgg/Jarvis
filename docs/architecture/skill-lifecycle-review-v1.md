# Skill Lifecycle Review v1

Reviewed at: 2026-05-10

## Scope

Phase 2 defines lifecycle and exposure boundaries for Jarvis primitive skills.
It does not change CC command behavior, remove skills, or implement composite
skill planning. Phase 3 has since completed the `get_weather` rewrite and
promoted it back to `active`.

Current YAML skills are treated as primitive skills. Future composite skills
may orchestrate these primitives, so a primitive being unsuitable for direct
user exposure is not by itself a reason to delete it.

## Lifecycle States

| Status | Meaning | Default Exposure |
| --- | --- | --- |
| `active` | Reliable enough for current Jarvis use. | LLM allowed; regex/direct only when the skill allows. |
| `experimental` | Potentially useful, but not yet proven. | Hidden by default except dev/explicit opt-in. |
| `rewrite_required` | Valuable capability with an incomplete implementation or contract. | Limited LLM exposure; no new regex/direct path. |
| `deprecated` | Legacy capability kept for compatibility, not recommended. | Hidden from default tool surface. |
| `disabled` | Hard disabled. | Runtime should reject calls. |

## Current Classification

### Active

| Skill | Notes |
| --- | --- |
| `smart_home_control` | Core physical-control skill; keep regex path and trace. |
| `smart_home_status` | Entity/status foundation for smart-home control. |
| `get_current_time` | Low-risk read-only primitive; spoken formatting still needs polish. |
| `set_timer` | Core assistant capability; add stronger timer evidence later. |
| `add_todo` | Low-risk state-changing personal capture. |
| `list_todos` | Low-risk read-only todo query. |
| `complete_todo` | Low-risk when `todo_id` is explicit. |
| `delete_todo` | Implemented as archive/soft-delete with undo token; does not permanently delete through the default LLM path. |
| `undo_delete_todo` | Restores archived todos by undo token. |
| `create_reminder` | Core assistant capability; time parsing needs later hardening. |
| `list_reminders` | Low-risk read-only reminder query. |
| `complete_reminder` | Low-risk when `reminder_id` is explicit. |
| `obsidian_add_to_inbox` | Low-risk append-only capture. |
| `get_weather` | Weather v2 uses Open-Meteo with explicit forecast type, coverage, location, timezone, freshness, and claim policy. |
| `type_to_focused` | Local text input now verifies focused target before paste; terminal, browser, external-message, password/payment, and unknown targets do not receive text without clarification or rejection. |
| `mac_gui` | Broad local GUI primitive now returns structured operation, target, risk, and pre/post execution evidence; high-risk close/lock actions require confirmation. |
| `cc_tell` | Preserved by user request; delivery does not imply completion. |
| `cc_slash` | Preserved by user request; command policy unchanged. |
| `cc_interrupt` | Preserved by user request. |
| `cc_show` | Read-only CC status primitive. |

### Rewrite Required

None.

### Deprecated

| Skill | Notes |
| --- | --- |
| `cc_approve` | Permission approval through Jarvis is high-risk and not part of the default skill surface. |
| `cc_deny` | Permission denial proxy is not part of the default skill surface. |
| `get_exchange_rate` | Low-priority realtime-data skill without strong source/freshness contract. |

### Disabled

None.

## Phase 3 Backlog

1. Smart home enhancements: entity registry, alias provenance, capabilities, and postcondition verification.
2. Reminder/timer enhancements: timezone, due/fires-at timestamps, recurrence, missing-field clarification.
3. Renderer improvements: voice/document split for long todo/reminder lists and raw IDs.
