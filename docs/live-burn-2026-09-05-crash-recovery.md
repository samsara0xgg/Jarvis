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
`test(scenarios)` commits on lane/b (`dfd9c58`, `68d93b9`). Decision model
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
| 3 | `crash-20260905T193149Z`, port 52761 | **FAIL** (42.70 s): `kill window missed: RESP4c8207281acf4b32a632b3cc1c45ae88 reached response.completed before playback started` — the answer opened with a preamble and sealed at candidate 0 (finding 4a); no retry existed yet | `minimax_ws_streaming` |
| 4 | `crash-20260905T193651Z`, port 52986 | **FAIL** (147.47 s): question 1 sealed again as `RESP81973fcbe2194368b73788bcc8a1c470`; the first retry logic waited 120 s for a playback terminal the silent sealed answer never writes (finding 4a) | `minimax_ws_streaming` |
| 5 | `crash-20260905T194047Z`, port 53089, pid 97961 | **PASS** (26.21 s) — accepted trail below | `minimax_ws_streaming` |
| 6 | `crash-20260905T194201Z`, port 53143, pid 98865 | **PASS** (29.09 s): `RESP243f73a32b9046d881887d2d7dc089f9`, N = 27, `response.failed` id 28, `boot_high_water_id` 27, `after_id=27` | `minimax_ws_streaming` |
| 7 | `crash-20260905T194231Z`, port 53180, pid 99223 | **PASS** (29.39 s): `RESP98e5a4e3b6d746a3918164afef085245`, N = 37, `response.failed` id 38, `boot_high_water_id` 37, `after_id=37` | `minimax_ws_streaming` |
| 8 | `crash-20260905T195631Z`, port 53970 | **FAIL** (44.67 s): all three questions of the first list sealed at candidate 0 (`RESP0b3dc7944afd4d38a4547b909660a45d`, `RESPc6b7eca228644a16a35ad1ca698831a1`, `RESP7da51a625a504ff58d395ac71855186c`; first sentences `温哥华位于…` / `…主要有几个原因`, no explanatory marker) | `minimax_ws_streaming` |
| 9 | `crash-20260905T200854Z`, port 54619 | **FAIL** (38.03 s): `RESP6a81a66768c94c59b17ad99e8a2f7d34` took the full-text route (finding 4c) and the scenario of that moment only recognised seals; `kill window missed … before playback started` | `minimax_ws_streaming` |
| 10 | `crash-20260905T201151Z`, port 54881, pid 29583 | **PASS** (43.48 s) — committed scenario, retry path live: questions 1-2 sealed, question 3 opened the window; values below | `minimax_ws_streaming` |

Runs 1-2 came from the earlier session on this card (scenario before any
retry); runs 3-9 drove the retry design; run 10 is the committed scenario.
Runs 5-7 asked `用十句话介绍一下温哥华的气候和地理` first and it opened the
window at once; the committed list (`什么是海岸山脉…`, `用十句话介绍一下温哥华的气候和地理`,
`什么是温带海洋性气候…`, twice over) follows the calibration below. Audio
guard, every run: `SwitchAudioSource -c -t output` captured before, switched
to `BlackHole 16ch`, restored after (`restored=True`); runs 1-7, 9 and 10
captured `MacBook Pro Speakers`, run 8 captured `BlackHole 16ch` because
another lane's guard was mid-run at 19:56Z (back to `MacBook Pro Speakers`
by 20:03Z).

### Question calibration

Two rounds per question against a live daemon from this worktree
(`~/.jarvis-lane-b-test/calib-20260905T200502Z`), reading the `stream_emit`
gate row for candidate 0:

| Question | Permits | First sentences |
|---|---|---|
| `什么是海岸山脉 请详细介绍 至少十句话` | 2/2 | `海岸山脉是位于大陆边缘…`, `海岸山脉是指沿大陆海岸线延伸的山脉带。` |
| `什么是温带海洋性气候 请详细解释 至少十句话` | 1/2 | `温带海洋性气候，你可以把它理解成一种…` (sealed), `温带海洋性气候，简单说，就是全年温暖湿润…` |
| `温哥华是一座什么样的城市 请从气候和地理两方面详细介绍 至少十句话` | 0/2 | `好的，我从气候和地理两个方面给你详细说说…`, `好的，我为你从气候和地理两个方面详细介绍…` |

Earlier samples from the burn runs: `用十句话介绍一下温哥华的气候和地理` 4/8
(permitted `温哥华位于加拿大最西端，…，是一座山海相连的城市。` in run 5; sealed on
`好的，我用十句话给你介绍温哥华。` in runs 3-4 and on `温哥华位于加拿大不列颠哥伦比亚省西南角，被太平洋、海岸山脉和弗雷泽河围绕。`
in run 8); the two `不要开场白` variants 0/1 each. The first calibration
attempt (`calib-20260905T200018Z`) stopped at its first question, which
took shape 4b below. Run 10 then sealed `什么是海岸山脉…` and
`用十句话…` before `什么是温带海洋性气候…` opened the window: the rate is
noisy, hence two rounds.

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

## Run 10 (committed scenario, retry path live)

Warm-up `T77f1dafb` / `RESPeedba718ce5f4f72b74293275ba3638b` spoken through
`minimax_ws_streaming`. Question 1 sealed as
`RESP4deb68c3614a4c07a34a7258f4a3d68f` and question 2 as
`RESP1e3ff8d68bc1438f870637d2dc06e4d9` (both `emission_mode=routine_stream`,
gate reasons `outside_evaluated_candidate_form`, `routine_ceiling_not_met`);
each turn went quiet after 6 rows with 1 response run and 0 playbacks (shape
4a). Question 3 (`什么是温带海洋性气候 请详细解释 至少十句话`) opened the window:
`Tcccdf692` / `RESPc3be03112a5c4237b2a239c05f69c141`, rows 39-53
(`surface.playback_started` id 45, generation 2; permits through sequence 3,
`chunk_chars=151`), `SIGKILL pid=29583 at 2026-09-05T20:12:33.522+00:00`,
N = 53. Boot 2: `boot reconciliation closed 1 open response run(s)` at
offset 727, `tts_watcher started (after_id=53)` at offset 1032, the other
watchers `after_id=54`; `response.failed` rows with `id > 53`:
`[(54, 'RESPc3be03112a5c4237b2a239c05f69c141', 'daemon_restart')]`; 0
`surface.playback_started` for that response after N; `spoken_heard=None`
vs checkpoint `None`; trace `media_owner_started boot_high_water_id=53`.

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
   outer quotes when it copies the file (runs 2-10 provider
   `minimax_ws_streaming`). `~/.jarvis-realtime-test/env` has the same
   quoted line; `serve.sh` does not source a shell env first, so the 8006
   daemon's TTS provider deserves a check. Owner follow-up, not a code
   change: rewrite both env files without quotes, for example
   `sed -i '' 's/^MINIMAX_API_KEY="\(.*\)"$/MINIMAX_API_KEY=\1/' ~/.jarvis/env ~/.jarvis-realtime-test/env`.
2. **No boot-time playback terminal.** ADR-0008 §4.4 says "runtime asks L5 to
   close any active playback state; L5 PlaybackTerminalizer owns its event".
   After the restart the only appended row is `response.failed` (id 28 in
   run 5, id 54 in run 10); the killed generation 2 has no
   `surface.playback_failed/interrupted/completed`. `terminalize_playback`
   has one caller inside the live owner (`jarvis/surface/voice_media.py`),
   none in `serve_inherent`. The card anticipated this ("stop, record it in
   Progress, and report to the hub"); the trail confirms it in every run.
   Not fixed under this card. **Closed** by
   `docs/goals/boot-playback-reconciliation.md` (lane A): boot now runs an L5
   reconciler third in the startup barrier. Proved by the run of
   2026-09-05T22:36:01Z (root `~/.jarvis-lane-b-test/crash-20260905T223601Z`),
   which SIGKILLed pid 65786 on the orphan pair
   `(RESP89e7127f638d41c487df91bde0a7ccca, 2)` whose
   `surface.playback_started` is id 26 / `event_uid`
   `84d7c945150949a68cffa14794c1a884`: restart 1 appended exactly one
   `surface.playback_interrupted` (id 29, `reason="daemon_restart"`,
   `source_event_id` equal to that `event_uid`), restart 2 appended nothing
   for the pair and logged no reconciliation line at all.
3. **The heard cursor never advances in live streaming playback.** Run 5's
   warm-up played 956849 of 956849 samples through MiniMax
   (`cursor_quality=estimated`) yet wrote zero `surface.playback_checkpoint`
   rows and completed with `heard_text=""`, `heard_through_sequence=null`;
   run 2 (764586/764586) and lane A's live root (`~/.jarvis-lane-a-test2`,
   `surface.playback_completed` id 17, `heard_text` length 0) show the same.
   Mechanism: `PlaybackLedger` opens every chunk with
   `cursor_quality="unknown"`; `record_audible`
   (`jarvis/surface/voice_ledger.py:244`) takes the first observed quality
   for the ledger-level cursor (`:256-260`, which is where the terminal's
   `cursor_quality=estimated` comes from) but at chunk level only ever
   applies `_least_quality` (`:262-264`), and `unknown` ranks below
   `estimated`, so a chunk's placeholder is never replaced; `finish_segment`
   (`:191`) upgrades a chunk only when its end is already inside the
   audible horizon at close time, which for a streaming provider (segment
   final arrives while the audio is still playing) is never. `snapshot()`
   therefore breaks on the first chunk and `heard_through_sequence` stays
   `None`. Consequences: no checkpoint can exist before a crash,
   `spoken_heard` is always `None` in the fold, and the card's "heard
   prefix no longer than the last checkpoint" branch is unreachable live;
   ADR-0008 §4.4's "spoken assistant context: only the heard_text boundary"
   is empty in practice. Report to the hub; not fixed under this card.
4. **Whether a run opens a stream at all is not under the test's control,
   and a run that opens none speaks late or never.** Three shapes were
   observed, all of which the scenario now treats as "no window, next
   question" once the turn is quiet (no open response run, no open
   playback, 2 s without a new row for the `turn_id`):
   - (a) *zero-permit seal, silent*: the first sentence lacks the form
     `_supported_candidate` (`jarvis/decision/stream_risk.py:164-176`)
     admits — a preamble (`好的，我用十句话给你介绍温哥华。`) or a plain statement
     without an explanatory marker (`温哥华位于…围绕。`); `gate.evaluated`
     sequence 0 `buffer_full_text` (`outside_evaluated_candidate_form`,
     `routine_ceiling_not_met`), the stream seals with zero permits,
     `response.completed` lands with no `surface.response_open`, and the
     emitted answer (`surface.response_emitted` with `voice_text`) produces
     no `surface.playback_*` row at all — without an open there is nothing
     for the media owner to schedule (runs 3, 4, 8, 10; lane A's
     `docs/goals/sealed-stream-full-text-fallback.md` Current behavior).
   - (b) *zero-permit seal, suffix rejected, correction run*: in
     `calib-20260905T200018Z` the sealed run ended `response.failed`
     (id 9, `reason=suffix_rejected`) and a correction run
     `RESP67176d54cc404773a7df34be4fc822c5` opened under the same turn
     (id 10), ran two Tavily searches (`action.*` rows 13-28), completed
     (id 33), rendered a `kind=text` open with 35 chunks and
     `surface.response_emitted` (id 70), and spoke it (`surface.playback_started`
     id 71) until `surface.playback_failed` id 77 `reason=tts_response_timeout`.
     Same card as (a).
   - (c) *full-text route*: in run 9 the decision layer answered
     `什么是海岸山脉…` with `response.started` `emission_mode=full_text`,
     `output_risk_class=unknown` (id 18), no `stream_emit` gate row, a
     `pre_emit` gate `allow_completion_language`, `response.completed` before
     a `kind=text` open, and playback only after `surface.response_emitted`;
     the same request took `routine_stream` in both calibration rounds.
   None of the three is a defect under this card: (a) and (b) are lane A's
   fallback card, (c) is the route chooser working as designed, and the
   allow-list is narrow by design.

## Docs disposition

- ADR-0006 D10: bullet 6 confirmed by `boot_high_water_id == N == after_id`
  in runs 2, 5-7 and 10; unchanged. §6 F14 (shutdown mid-session) is not
  exercised by `SIGKILL`; F16 confirmed (0 re-speak rows); both unchanged.
- ADR-0008 §4.4: reconcile-once confirmed; the "runtime asks L5 to close any
  active playback state" bullet was contradicted by the trail (finding 2) and
  the "reconstructs the last heard checkpoint" bullet has no checkpoint to
  reconstruct (finding 3). Both were reports to the hub, not edits under this
  card; finding 2 is now closed by the boot reconciler and the bullet holds
  as written, so §4.4 still needs no edit. §7 F14 confirmed; F23 not
  exercised; unchanged.
- `docs/spec.html`: unchanged; its only high-water mentions are the
  projection snapshot semantics (`read_projection(name, high_water_mark?)`),
  which this run did not touch, and it carries no crash-recovery contract.
