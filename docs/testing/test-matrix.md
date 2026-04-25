# Test Matrix

Last updated: 2026-04-24 10:24 PDT
Quality target: 9.8/10 Phase 1 test matrix

## Status
No tests were run while creating/upgrading cockpit docs. This is a coverage map, not a health report.

## Core Matrix

| Area | Expected Behavior | Existing Test Files | Known Gap / Note |
|---|---|---|---|
| Runtime orchestration | Jarvis initializes modules, routes turns, stores history, traces turns | `tests/test_jarvis.py`, `tests/test_jarvis_trace.py`, `tests/test_jarvis_asr_integration.py` | Add explicit `memory_query_ids` runtime passthrough test |
| Memory Manager | v2 prompt context via `build_prompt_context`; v1 `query()` remains deprecated/compatible | `tests/test_memory_manager.py`, `tests/test_memory_e2e.py`, `tests/test_memory_edge_cases.py` | User isolation/retrieval budget not covered |
| Assembler | Builds identity/profile/observations/situation blocks and tracks injected ids | `tests/test_assembler.py`, `tests/test_memory_manager.py` | Add token cap / selection tests when implemented |
| Memory Store | Add/read/supersede/count/chunk/source_turn_id observations | `tests/test_memory_store.py` | No observation `user_id` tests yet |
| Retriever/Embedder legacy | Embeddings/retrieval behavior for v1/legacy paths | `tests/test_retriever.py`, `tests/test_embedder.py`, `tests/test_cache.py` | Confirm role after Memory v2 migration before refactor |
| Observer | Extracts observations; benchmark fixtures exist | `tests/test_observer.py`, `tests/test_observer_bench.py` | Row granularity still coarse |
| Trace / Migration | Schema, JSON fields, migration, outcome updates | `tests/test_trace.py`, `tests/test_trace_migration.py` | Trace API covered; runtime passthrough gap remains |
| Outcome / NLI | Feedback detection and no-update on `None` | `tests/test_outcome_detector.py`, `tests/test_outcome_detector_nli.py`, `tests/test_nli_classifier.py`, `tests/test_jarvis_outcome_async.py` | Need model health check if production eval depends on it |
| Tool Registry / Tools | Tool registration, YAML interpreter, time/reminders/todos/smart home | `tests/test_tool_registry.py`, `tests/test_jarvis_tool.py`, `tests/test_yaml_interpreter.py`, `tests/test_tool_time.py`, `tests/test_tool_reminders.py`, `tests/test_tool_todos.py`, `tests/test_tool_smart_home.py`, `tests/test_local_executor.py` | Watch tool count and RBAC behavior |
| Router / Commands | Intent routing, command parsing | `tests/test_intent_router.py`, `tests/test_command_parser.py` | Router confidence is self-reported, not calibrated |
| Audio input / ASR / VAD / Wake | Recording, ASR normalization, VAD, wake word | `tests/test_audio_recorder.py`, `tests/test_audio_recorder_vad.py`, `tests/test_asr_normalizer.py`, `tests/test_speech_recognizer.py`, `tests/test_vad_silero.py`, `tests/test_wake_word.py` | Hardware/manual validation still needed |
| Interrupt / heard response | Soft/hard interrupt and memory injection around interruptions | `tests/test_interrupt_monitor.py`, `tests/test_interrupt_integration.py`, `tests/test_interrupt_soft_stop.py`, `tests/test_interrupt_memory_injection.py` | Complex state; run targeted tests before TTS/web changes |
| TTS / playback | TTS engines, cache, streaming, stop/suspend/preprocess | `tests/test_tts.py`, `tests/test_tts_cache.py`, `tests/test_tts_minimax_ws.py`, `tests/test_tts_preprocessor.py`, `tests/test_tts_stop.py`, `tests/test_tts_suspend.py`, `tests/test_audio_stream_player.py`, `tests/test_llm_sentence_divider.py` | Real audio quality needs manual test |
| Web/UI/OLED | FastAPI web server, browser WS stream, health, OLED | `tests/test_web_server.py`, `tests/test_browser_ws_stream.py`, `tests/test_health.py`, `tests/test_health_integration.py`, `tests/test_oled_display.py` | Previous context mentions web/full-suite fragility |
| Auth/User/Speaker | Enrollment, user store, speaker identity/verification | `tests/test_enrollment.py`, `tests/test_user_store.py`, `tests/test_speaker_encoder.py`, `tests/test_speaker_verifier.py` | Tie into memory user isolation later |
| Devices / Smart home | Sim/MQTT/Hue device integrations | `tests/test_sim_devices.py`, `tests/test_mqtt_devices.py`, `tests/test_hue_integration.py`, `tests/test_device_manager.py` | Live hardware tests separate |
| Realtime data | Providers/cache/formatting | `tests/test_realtime_data.py` | External providers should stay mocked in CI |
| System/bench | System harness/reporter/baseline/benchmarks/pricing/behavior log | `tests/test_system_test_*`, `tests/test_bench_llm_v3.py`, `tests/test_observer_bench.py`, `tests/test_pricing.py`, `tests/test_behavior_log.py`, benchmarks | Full suite may need batching due previous segfault context |

## Recommended Targeted Test Sets

### R1: `memory_query_ids` runtime fix
```bash
pytest tests/test_trace.py tests/test_jarvis_trace.py tests/test_memory_manager.py -q
```

### R2/R3/R4: observation schema/retrieval/granularity
```bash
pytest tests/test_memory_store.py tests/test_assembler.py tests/test_memory_manager.py tests/test_jarvis_trace.py -q
```

### Outcome/NLI changes
```bash
pytest tests/test_outcome_detector.py tests/test_outcome_detector_nli.py tests/test_nli_classifier.py tests/test_jarvis_outcome_async.py -q
```

### Tool/routing changes
```bash
pytest tests/test_tool_registry.py tests/test_jarvis_tool.py tests/test_yaml_interpreter.py tests/test_intent_router.py tests/test_local_executor.py -q
```

### Web/TTS/interrupt changes
```bash
pytest tests/test_web_server.py tests/test_browser_ws_stream.py tests/test_tts.py tests/test_tts_minimax_ws.py tests/test_interrupt_integration.py -q
```

## Maintenance Rule
When a new bug is fixed, add the regression test here under the relevant area.
