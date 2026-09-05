# Live burn — 2026-09-05 (crash recovery, `kill -9` mid-speech)

Card: `docs/goals/crash-recovery-live-verification.md`. Contract under test:
ADR-0006 D10 (boot high-water, no re-speak) and ADR-0008 §4.4 / §7 F14
(reconcile an open response once to `response.failed(daemon_restart)`, never
resume the old stream). Every hermetic test of that contract rebuilds objects
in-process; this burn sends `SIGKILL` to a real `python -m jarvis serve`
subprocess while a `surface.playback_started` row exists for the response
and no response terminal does, boots again on the same runtime root and
port, and reads the same on-disk Event Log, the second boot's log and its
JSONL trace.

Scenario: `tests/scenarios/test_live_crash_recovery.py` (marked `live_llm`).
Tree under test: lane/b at `294f405` (realtime-integration merged) plus the
scenario edits committed with this document. Decision model
`deepseek-v4-flash` direct (shipped `fast` preset), TTS MiniMax WebSocket
streaming, macOS `say` fallback enabled as shipped.

Overlay (copied from `config/jarvis.yaml`, only these keys flipped):
`realtime.enabled`, the four `realtime.concurrency_safety` switches,
`realtime.response.response_run_lifecycle`,
`realtime.response.routine_streaming.enabled`,
`realtime.streaming_output.enabled`,
`realtime.streaming_output.speak_from_segments`. `single_audio_ingress` and
`input.intent_pump` stay at their shipped `false`.

Isolation: runtime root `~/.jarvis-lane-b-test/crash-<ts>/`, OS-assigned
port, `JARVIS_REALTIME_TRACE_JSONL=<root>/trace.jsonl` for both boots,
`JARVIS_VOICE_DISABLE_WAKE=1`. The daemon on 127.0.0.1:8006 (pid 53955,
root `~/.jarvis-realtime-test`) was never signalled; `lsof -iTCP:8006`
showed the same pid before and after every run.

## Invocation

```
PYTHONPATH=. .venv/bin/python -m pytest -q -s --live-llm -m live_llm \
    tests/scenarios/test_live_crash_recovery.py
```

`--live-llm` is required; `tests/conftest.py` skips `live_llm` items without
it. Secrets reach the daemon the production way: `~/.jarvis/env` is copied
to `<root>/env` for `load_env_file` (ADR-0009 D1), with one pair of outer
shell quotes stripped per value (finding 1).

## Runs

| Run | Root | Result | Warm-up TTS provider |
|---|---|---|---|
| 1 | `crash-20260905T161731Z`, port 49247, pid 58485 | **PASS** (30.35 s) | `macos_say` (finding 1) |
| 2 | `crash-20260905T162141Z`, port 49672, pid 64729 | **PASS** (21.99 s) | `minimax_ws_streaming` |
| 3 | `crash-20260905T193149Z`, port 52761 | **FAIL** (42.70 s): `kill window missed: RESP4c8207281acf4b32a632b3cc1c45ae88 reached response.completed before playback started` — the answer opened with a preamble and sealed at candidate 0 (finding 4); no retry existed yet | `minimax_ws_streaming` |
| 4 | `crash-20260905T193651Z`, port 52986 | **FAIL** (147.47 s): question 1 sealed again as `RESP81973fcbe2194368b73788bcc8a1c470`; the first retry logic waited 120 s for a playback terminal the silent sealed answer never writes (finding 4) | `minimax_ws_streaming` |
| 5 | `crash-20260905T194047Z`, port 53089, pid 97961 | **PASS** (26.21 s) — accepted trail below | `minimax_ws_streaming` |
| 6 | `crash-20260905T194201Z`, port 53143, pid 98865 | **PASS** (29.09 s): `RESP243f73a32b9046d881887d2d7dc089f9`, N = 27, `response.failed` id 28, `boot_high_water_id` 27, `after_id=27` | `minimax_ws_streaming` |
| 7 | `crash-20260905T194231Z`, port 53180, pid 99223 | **PASS** (29.39 s): `RESP98e5a4e3b6d746a3918164afef085245`, N = 37, `response.failed` id 38, `boot_high_water_id` 37, `after_id=37` | `minimax_ws_streaming` |

Runs 1-2 came from the earlier session on this card (scenario before the
question retry); runs 3-7 are the committed scenario. In every pass the
first long question (`用十句话介绍一下温哥华的气候和地理`) opened the window
at its first try. Audio guard, every run: `SwitchAudioSource -c -t output`
before = `MacBook Pro Speakers`, switched to `BlackHole 16ch`, after =
`MacBook Pro Speakers` (`restored=True`).

## Accepted trail (run 5)

Warm-up turn `T7d7a5808` / `RESPd272cde6083d41bba12307f8cf56917d`
(`用两句话介绍一下温哥华`): rows 1–17, `surface.playback_started` id 8
(generation 1), `response.completed` id 13, `surface.playback_completed`
id 17 (`provider=minimax_ws_streaming`, 956849/956849 samples,
`cursor_quality=estimated`, `heard_text` length 0 — finding 3).

Killed turn `T3485331b` / `RESP2b75e178e097471487044c6f158c27bb`, pre-kill
rows as read after the process was dead (`ts` = `ts_epoch_ms`, UTC):

```
id  type                              ts             note
18  surface.user_intent               ...270370      用十句话介绍一下温哥华的气候和地理
19  response.started                  ...270375      emission_mode=routine_stream
20  turn.started                      ...270376
21  response.request_admitted         ...270376
22  gate.evaluated                    ...271184      sequence 0 permit (request_routine_text, routine_text)
23  surface.response_open             ...271184      kind=stream
24  surface.response_chunk            ...271184      seq 0 温哥华位于加拿大最西端，被太平洋和海岸山脉环绕，是一座山海相连的城市。
25  surface.playback_started          ...271186      playback_generation_id=2 incremental=true
26  surface.playback_segment_prepared ...271187      seq 0
27  gate.evaluated                    ...271335      sequence 1 buffer_full_text (outside_evaluated_candidate_form, routine_ceiling_not_met)
```

`ts` prefix `1788637` elided. `surface.playback_started` (19:41:11.186Z)
precedes any response terminal; none existed at the kill.

Kill: `SIGKILL pid=97961 at 2026-09-05T19:41:12.694+00:00`
(`checkpoint_seen=False` after the 1.5 s grace — finding 3).
Pre-kill `MAX(events.id)` **N = 27**, `playback_generation_id` 2,
`chunk_chars=35`, `last_checkpoint_heard_text=None`.

Second boot log header (same root, same port; local time, UTC-7):

```
12:41:13,131 INFO jarvis.decision.llm Applied preset 'vision': ...
12:41:13,131 INFO jarvis.shared.pricing loaded pricing.json: 20 LLM models
12:41:13,144 INFO jarvis.decision.llm Applied preset 'fast': provider=openai model=deepseek-v4-flash
12:41:13,259 INFO jarvis.surface.voice_tts AudioStreamPlayer started: 48000Hz ch=1 blocksize=0 latency=low
12:41:13,259 INFO jarvis.runtime.inherent_loop JARVIS_VOICE_DISABLE_WAKE=1; skipping WakeListener spawn.
12:41:13,261 INFO jarvis.runtime.inherent_loop boot reconciliation closed 1 open response run(s)
12:41:13,276 INFO jarvis.runtime.inherent_loop user_intent_watcher started (after_id=28)
12:41:13,276 INFO jarvis.runtime.inherent_loop response_watcher started (after_id=28)
12:41:13,276 INFO jarvis.runtime.inherent_loop tts_watcher started (after_id=27)
12:41:13,276 INFO jarvis.runtime.inherent_loop system_trigger_watcher started (after_id=28)
```

Asserted rows and values:

| Assertion | Evidence |
|---|---|
| exactly one `response.failed(reason=daemon_restart)` for the killed run, none for the warm-up | rows with `id > 27` of type `response.failed`: `[(28, 'RESP2b75e178e097471487044c6f158c27bb', 'daemon_restart')]`; row 28 payload `{"reason":"daemon_restart","response_group_id":"RGRPc6027fd4ca865aa9949958b2449e0c6d","response_id":"RESP2b75e178e097471487044c6f158c27bb","retryable":false,"turn_id":"T3485331b"}`; warm-up has 0 `response.failed` |
| nothing re-speaks | `surface.playback_started` with `id > 27` for `(RESP2b75…, gen 2)`: 0; for the response_id at all: 0 |
| heard prefix bounded | `fold_conversation_history` → `spoken_heard=None`; last pre-kill checkpoint `None` (`record.consistent=True`) |
| boot 2 anchors at the pre-kill high-water | trace `media_owner_started attributes={'boot_high_water_id': 27, 'boot_id': 'BOOT627768dc19844be0857b95765fda4c33', 'command_queue_capacity': 64}`; log `tts_watcher started (after_id=27)` |
| reconcile before watchers | `boot reconciliation closed 1 open response run(s)` at log offset 727, `tts_watcher started (after_id=` at offset 1032 |

The other three watchers anchor at `after_id=28`: they are created after the
reconciler appended row 28, while the TTS high-water was captured before it,
exactly the D10 bullet-6 ordering.

Rows appended by the second boot: only id 28. No `surface.playback_*` row of
any kind was written for generation 2 (finding 2).

## Earlier passes (runs 1-2)

Run 2 (`RESPfce57d4b5fb442dfbe1a3d44e697c680`, N = 29, `response.failed`
id 30, `boot_high_water_id` 29, `after_id=29`, reconcile offset 727 <
watcher offset 1032, kill at 2026-09-05T16:22:02.693Z) had the same shape
as run 5. Run 1 (`RESPdb5188b3a84d4f41b98e59c3066717a8`, N = 26,
`response.failed` id 27, kill at 2026-09-05T16:18:00.815Z) passed the same
Event-Log assertions, but its warm-up `surface.playback_completed` (id 16)
carried `provider=macos_say`, `cursor_quality=unknown`; the trace shows
`tts_prefix_safe_endpoint_fallback error_type=InvalidStatus` for endpoint
index 0 and 1, then `tts_macos_say_started`. Cause in finding 1.

## Findings

1. **Quoted secrets never reach MiniMax through `<root>/env`.** `~/.jarvis/env`
   writes `MINIMAX_API_KEY="…"` (shell syntax). `load_env_file`
   (`jarvis/deployment/__init__.py:129`) takes quotes literally by contract
   (ADR-0009 D1), so a daemon that gets its key only from `<root>/env` sends
   a key wrapped in `"`, every WebSocket handshake fails with
   `InvalidStatus`, and playback silently falls back to macOS `say`
   (`enable_macos_say_fallback: true`). No log line at INFO records the
   fallback; only the trace does. The scenario strips one pair of matching
   outer quotes when it copies the file (runs 2-7 provider
   `minimax_ws_streaming`). `~/.jarvis-realtime-test/env` has the same
   quoted line; `serve.sh` does not source a shell env first, so the 8006
   daemon's TTS provider deserves a check. Owner follow-up, not a code
   change: rewrite both env files without quotes, for example
   `sed -i '' 's/^MINIMAX_API_KEY="\(.*\)"$/MINIMAX_API_KEY=\1/' ~/.jarvis/env ~/.jarvis-realtime-test/env`.
2. **No boot-time playback terminal.** ADR-0008 §4.4 says "runtime asks L5 to
   close any active playback state; L5 PlaybackTerminalizer owns its event".
   After the restart the only appended row is `response.failed` id 28; the
   killed generation 2 has no `surface.playback_failed/interrupted/completed`.
   `terminalize_playback` has one caller inside the live owner
   (`jarvis/surface/voice_media.py`), none in `serve_inherent`. The card
   anticipated this ("stop, record it in Progress, and report to the hub");
   the trail confirms it in every run. Not fixed under this card.
3. **The heard cursor never advances in live streaming playback.** Run 5's
   warm-up played 956849 of 956849 samples through MiniMax
   (`cursor_quality=estimated`) yet wrote zero `surface.playback_checkpoint`
   rows and completed with `heard_text=""`, `heard_through_sequence=null`;
   run 2 (764586/764586) and lane A's live root (`~/.jarvis-lane-a-test2`,
   `surface.playback_completed` id 17, `heard_text` length 0) show the same.
   Mechanism: `PlaybackLedger` opens every chunk with
   `cursor_quality="unknown"`; `record_audible`
   (`jarvis/surface/voice_ledger.py:244`) only ever applies
   `_least_quality`, and `unknown` ranks below `estimated`, so the
   placeholder is never replaced; `finish_segment` (`:191`) upgrades a
   chunk only when its end is already inside the audible horizon at close
   time, which for a streaming provider (segment final arrives while the
   audio is still playing) is never. `snapshot()` therefore breaks on the
   first chunk and `heard_through_sequence` stays `None`. Consequences: no
   checkpoint can exist before a crash, `spoken_heard` is always `None` in
   the fold, and the card's "heard prefix no longer than the last
   checkpoint" branch is unreachable live; ADR-0008 §4.4's "spoken assistant
   context: only the heard_text boundary" is empty in practice. Report to
   the hub; not fixed under this card.
4. **A routine stream that seals at its first candidate is silent, and the
   model's first sentence is not under the test's control.** Runs 3 and 4
   answered `用十句话介绍一下温哥华的气候和地理` with the preamble
   `好的，我用十句话给你介绍温哥华(的气候和地理)。`, which
   `_supported_candidate` (`jarvis/decision/stream_risk.py:164-176`) rejects
   (no explanatory form): `gate.evaluated` sequence 0 `buffer_full_text`
   (`outside_evaluated_candidate_form`, `routine_ceiling_not_met`), the
   stream sealed with zero permits, `response.completed` landed with no
   `surface.response_open`, and the emitted answer (`surface.response_emitted`
   id 26 in run 4, `voice_text` present) produced no `surface.playback_*` row
   at all — without an open there is nothing for the media owner to
   schedule, exactly lane A's `docs/goals/sealed-stream-full-text-fallback.md`
   Current behavior. The same question opened with `温哥华位于…，是…` and was
   permitted in runs 2, 5, 6 and 7. The scenario therefore treats a response
   terminal with no `surface.response_open` as "sealed before the first
   permit" and submits the next of three questions once
   `surface.response_emitted` has landed and 2 s passed with no playback (an
   observed playback is waited to its terminal instead, for when the
   fallback card lands). Both branches were exercised against the real
   run-3 and run-4 trails (`_Sealed(..., ('outside_evaluated_candidate_form',
   'routine_ceiling_not_met'))`, settled after 2.00 s / 2.10 s); in runs 5-7
   the first question opened the window, so the retry never fired in a
   passing run. Not a defect under this card: the seal fallback is lane A's
   card and the allow-list is narrow by design.

## Docs disposition

- ADR-0006 D10: bullet 6 confirmed by `boot_high_water_id == N == after_id`
  in runs 2 and 5-7; unchanged. §6 F14 (shutdown mid-session) is not
  exercised by `SIGKILL`; F16 confirmed (0 re-speak rows); both unchanged.
- ADR-0008 §4.4: reconcile-once confirmed; the "runtime asks L5 to close any
  active playback state" bullet is contradicted by the trail (finding 2) and
  the "reconstructs the last heard checkpoint" bullet has no checkpoint to
  reconstruct (finding 3). Both are reports to the hub, not edits under this
  card. §7 F14 confirmed; F23 not exercised; unchanged.
- `docs/spec.html`: unchanged; its only high-water mentions are the
  projection snapshot semantics (`read_projection(name, high_water_mark?)`),
  which this run did not touch, and it carries no crash-recovery contract.
