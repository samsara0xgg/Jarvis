# Runtime Flow

Last updated: 2026-04-24 10:24 PDT
Quality target: 9.8/10 Phase 1 runtime flow

## Startup / Wiring Flow

```text
JarvisApp.__init__ (`jarvis.py:95-344`)
  -> EventBus / health tracker
  -> UserStore + AudioRecorder + SpeakerEncoder/Verifier + SpeechRecognizer + ASRNormalizer
  -> DeviceManager + PermissionManager
  -> LLMClient + ConversationStore
  -> MemoryManager + BehaviorLog + TraceLog + NLIClassifier
  -> Scheduler / OLED
  -> init tools.smart_home/time_utils/reminders/todos
  -> ToolRegistry
  -> IntentRouter + LocalExecutor
  -> InterruptMonitor
  -> lazy TTS + warmups
```

## One Runtime Turn

```text
User speech/text
  ↓
Input normalization / auth context
  ↓
Jarvis runtime turn handling (`jarvis.py`)
  ↓
Shortcut checks
  - escalation keywords
  - remember shortcut
  ↓
Memory hot path before LLM
  - MemoryManager.build_prompt_context()
  - Assembler.assemble()
  - Store.get_all_observations()
  - PromptContext.blocks + injected_observation_ids
  ↓
Router / local / cloud decision
  - IntentRouter.route_and_respond()
  - LocalExecutor for local actions
  - LLMClient for cloud/open-ended/tool-use
  ↓
Tool execution if needed
  - ToolRegistry.get_tool_definitions(user_role)
  - ToolRegistry.execute(name, args, user_id, user_role)
  ↓
Assistant response
  - may include <cited_obs>[ids]</cited_obs>
  ↓
TTS / Web / OLED output
  ↓
Trace flush
  - TraceLog.log_turn(...)
  - cited_obs_ids recorded
  - known gap: memory_query_ids not currently passed by runtime
  ↓
Cold path
  - Observer extracts observations
  - Store.add_observation(...)
  - next turn NLI detects previous outcome
```

## Memory v2 Flow

```text
Hot path:
jarvis.py:1004-1030
  -> MemoryManager.build_prompt_context(text, user_id, history, user_name, user_role, emotion, situation)
  -> Assembler.assemble()
  -> PromptContext.injected_observation_ids
  -> _last_memory_query_ids = {observation_ids: [...], top_k_scores: []}

Cold path:
jarvis.py after response
  -> MemoryManager.write_observation(turn_data, source_turn_id)
  -> Observer tool-call extraction
  -> Store.add_observation(chunk_id, markdown, source_turn_id)
```

## Trace / Eval Flow

```text
Assistant response
  -> parse <cited_obs>[ids]</cited_obs>
  -> filter against PromptContext.injected_observation_ids
  -> TraceLog.log_turn(..., cited_obs_ids=filtered_ids)
  -> [gap] TraceLog supports memory_query_ids, runtime not passing it yet

Next user turn
  -> detect_outcome(previous assistant text, current user text)
  -> TraceLog.update_outcome(previous_trace_id, signal, at_turn_id=current_trace_id)
```

## Web Flow

```text
create_app(jarvis_app) (`ui/web/server.py:173-342`)
  -> FastAPI app + CORS
  -> session create/delete
  -> LLM preset switch endpoint
  -> hidden mode endpoint
  -> /api/chat starts turn_id + abort_event
  -> browser cancel/new_chat flushes active playback
```

## Tool Flow

```text
LLM/local executor wants action
  -> ToolRegistry.get_tool_definitions(user_role)
  -> RBAC filter by role hierarchy
  -> ToolRegistry.execute(...)
     -> Python @jarvis_tool from `tools/`
     OR YAML skill via YAMLInterpreter
  -> result string returned to runtime/LLM
```

## Failure / Fallback Notes

| Area | Behavior |
|---|---|
| Health tracker / scheduler / OLED / router | Init is guarded; unavailable components log warnings rather than killing app startup. |
| Audio recorder | Fails if sounddevice missing; enforces 16kHz mono. |
| Memory hot path | Exceptions are caught and logged; turn continues without memory context. |
| Observer cold path | Empty/failed extraction writes nothing. |
| NLI outcome | Missing/failed NLI returns `None`; previous trace outcome is not updated. |
| Tool execution | Unknown/failed tools return error strings, not crashes. |

## Re-check Before Code Changes
Line numbers may drift. Before editing, re-read the relevant function around the listed anchors.
