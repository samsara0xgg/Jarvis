# Goal: v2-input-endpoints

## Goal
The daemon accepts authenticated, idempotent `POST /inherent/submit/v2` and
`POST /inherent/asr-submit/v2` through a new L2 `InputSubmissionInbox`, so an
accepted receipt names the exact durable input event and the server-minted
`turn_id`, an identical retry never duplicates a turn, a different payload
under the same `request_id` is refused, and a Swift `InputSubmissionClient`
correlates its local pending input along
`request_id → input_event_uid/turn_id → response_group_id`.

## Why
`v2-envelope-and-identity` opened the authenticated socket and
`sequencer-and-snapshot` made it carry ordered deltas, but every v2 input still
has to go through the unauthenticated v1 `POST /inherent/submit`, whose lost
HTTP response duplicates a turn and whose `{"status","turn_id"}` receipt cannot
be tied to the response group the panel later shows.

## Current behavior
- The app registers five routes plus two conditionals
  (jarvis/surface/inherent_server.py:598 `create_app`): `/inherent/submit` :619,
  `/inherent/cancel-response` :643 (only when `deps.cancel_response_callable` is
  set), `/inherent/ws` :663, `/inherent/ws/v2` :698 (only when `deps.v2` is
  set), `/api/health` :707, `/inherent/image-submit` :712 (a 501 stub), and
  `/inherent/asr-submit` :720. No route under `/inherent/**/v2` exists, and no
  HTTP route is authenticated.
- v1 text submit strips the text, 400s when empty, and offloads
  `deps.submit_callable` (:619-640). The daemon binds that to a closure that
  mints `turn_id` itself and calls `emit_surface_user_intent`
  (jarvis/runtime/inherent_loop.py:2865-2888). The canonical payload is
  `{transcript, turn_id, channel="cli_stdin", language="zh-CN"}`
  (jarvis/surface/cli.py:168-208) — no request id, no `source_surface`, no
  idempotency, and the minted `turn_id` is returned only in the HTTP body.
- `_run_asr_submit` (inherent_server.py:515-596) is the whole ASR contract:
  501 when `deps.voice_pipeline_callable` is None, 400 on a missing/empty
  body, 415 outside `_ASR_ACCEPTED_CONTENT_TYPES` (:102), 413 above
  `_ASR_MAX_BYTES` (:101), 400 on an empty decode through
  `_decode_wav_to_pcm16_mono_16k` (:107), 422 on `VoicePipelineEmptyError`,
  503 on `VoiceInputBusyError`, 500 otherwise. It mints its own
  `turn_id = "T" + secrets.token_hex(4)` at :569 and returns
  `{status, text, emotion, turn_id}`. Its tests are
  tests/integration/test_inherent_server_asr_submit.py (200 :60, 413 :99,
  422 :121, 503 :145, 500 :168, 415 :191, no-broadcast :212).
- `VoicePipeline.run_turn` commits `utterance.received` **itself**, on its own
  connection, outside any caller transaction
  (jarvis/surface/voice_pipeline.py:195-217), and returns that Event.
- v2 authentication is one shared per-boot bearer token, not a user identity:
  `_run_v2_session` (inherent_server.py:475-513) reads the header through
  `_v2_presented_token` (:340) and calls `deps.token_matches` **before**
  `ws.accept()`, so a bad token surfaces as HTTP 403. The runtime binds
  `functools.partial(inherent_v2_token_matches, v2_token)` over the token
  rotated at boot (inherent_loop.py:3009, :3028;
  jarvis/deployment/__init__.py:259 `rotate_inherent_v2_token`, :303
  `inherent_v2_token_matches`). Nothing in `jarvis/` names an authenticated
  principal for that socket; `caller_principal` is L4's tool-dispatch identity,
  unrelated.
- `client_instance_id` is a client-supplied `ClientEnvelope` field
  (jarvis/surface/inherent_protocol.py:67), minted Swift-side per connect by
  `RealtimeTransportV2.mintIdentity(prefix: "I")`
  (desktop/inherent-swift/InherentCard/RealtimeTransportV2.swift:154, :180).
- ADR-0008's `claim_turn_once` **does not exist by that name**. The real
  primitive is `claim_input_once` (jarvis/state/input_claim.py:143), which
  reads `trigger_event.payload["turn_id"]` (:167), refuses a trigger without
  one, opens `BEGIN IMMEDIATE` (:170), appends `turn.started` reusing that
  exact id (:190-194), and raises `ConflictingTurnClaimError` when another
  trigger already used it. L3 mirrors this: `_handle_utterance` hydrates the
  claim's `turn_id` and only mints when no claim exists
  (jarvis/decision/__init__.py:1140-1156).
- The Event Log's transaction API is `append_event_in_transaction`
  (jarvis/state/event_log.py:1442), which requires a caller-owned transaction;
  `emit_event` (:1607) refuses one. The `BEGIN IMMEDIATE` precedent for a
  receipt-plus-append primitive is input_claim.py:170 and :215,
  authorized_dispatch_outbox.py:113/:184/:770, lifecycle_terminal.py:138.
  Registry validation accepts unknown payload keys — "Unknown keys are accepted
  Day-1 (spec §5.4 strict-mode is deferred)" (event_log.py:1469-1471) — so
  `surface.user_intent` (:909-916) and `utterance.received` (:277-290) can
  carry new payload fields with no registry change.
- The response-group fold learns a group's turn only from the
  `surface.response_open` payload: `_open` reads `turn_id` at
  jarvis/state/inherent_view.py:346 and stamps it on both `ResponseView` (:351)
  and a first-sight `ResponseGroupView` (:363). `fold` (:259) returns None for
  any row outside `RESPONSE_EVENT_TYPES`, so `surface.user_intent` and
  `utterance.received` advance the cursor and are invisible to the view.
  `InherentViewCheckpoint` (:119) holds only `through_cursor` and `groups`.
- The wire shape of `response.opened` is built at
  jarvis/surface/inherent_presenter.py:48-61 from a `ResponseView`, typed by
  `ResponseOpened` (inherent_protocol.py:200-215), and the snapshot group item
  at inherent_presenter.py:107-137 from a `ResponseGroupView`. Swift decodes it
  in `ResponseOpened`
  (desktop/inherent-swift/InherentRealtime/RealtimeViewDTOs.swift:151-175).
- Swift v2 is dormant and outbound-poor: `RealtimeTransportV2.enabled = false`
  (RealtimeTransportV2.swift:25), the reducer emits only `sendAck` and
  `requestResync` plus two presentation effects
  (InherentRealtime/RealtimeEffects.swift:58-64), `LocalPresentationEvent`
  (:25-34) has no input case, `InherentUXState`
  (InherentRealtime/RealtimeState.swift:497-532) has no pending-input map, and
  `applyOpened` (InherentRealtime/RealtimeReducer.swift:301-331) creates the
  group and response with no request correlation. There is no
  `InputSubmissionClient.swift`. Tests live in
  desktop/inherent-swift/InherentCardTests/Realtime/ (10 files) and
  .../InherentCardTests/ (RealtimeProtocolTests.swift, SubmitRequestTests.swift
  and 7 more), run by scripts/test_inherent_swift.sh.
- Hermetic test doubles already exist for this shape:
  tests/integration/test_inherent_sequencer.py `_Socket` :379-404 and `_Rig`
  :406-485 (a real Event Log, one sequencer, one hub, `emit` :433, `turn` :445,
  `noise` :461 which emits `utterance.received`, `connect` :468, `adopt` :477).
- scripts/smoke_inherent_sequencer.py (179 lines) is the daemon-level pattern:
  argparse over `--root`/`--url` (:103), token read from
  `<root>/inherent-v2.token` (:110), `websockets.sync.client.connect` with the
  Bearer header (:113), `_recv` asserting a message type (:60), `_emit_turn`
  writing rows straight into the log (:72). The synthesized-WAV helper for the
  ASR half is `_wav` at tests/integration/test_voice_ptt_end_to_end.py:29.

## Target behavior
- **Two new routes, registered exactly like the v2 socket.** `POST
  /inherent/submit/v2` and `POST /inherent/asr-submit/v2` are registered inside
  `create_app` only when `deps.v2 is not None` — no new flag. Each authenticates
  with the same mechanism the socket uses: `_v2_presented_token` over the
  `Authorization` header plus `deps.v2.token_matches`; a missing or wrong token
  is refused exactly as the socket refuses it (HTTP 403). `/inherent/image-submit/v2`
  is **not** added.
- **One L2 inbox owns the durability.** New `jarvis/state/input_submission_inbox.py`
  implements ADR-0014 :1319-1334 with the idempotency key
  `(authenticated_principal, client_instance_id, request_id)`:
  - Text: one `BEGIN IMMEDIATE` claims or resolves the payload hash, appends the
    canonical `surface.user_intent` through `append_event_in_transaction`
    carrying the server-minted `turn_id`, `source_client_request_id` and
    `source_surface="inherent_v2"`, stores the receipt (ids + result), and
    commits. There is no receipt-without-event and no event-without-result
    window.
  - ASR: the D21 processing lease (:1245-1256) — one short transaction claiming
    `(request_id, audio_sha256)` as `processing` — then decode/ASR outside any
    transaction, then a final `BEGIN IMMEDIATE` that verifies the same
    request/hash and stores
    `(request_id, audio_sha256, input_event_uid, session_id, utterance_id,
    turn_id, result)` before commit.
  - Identical `request_id` + payload hash returns the original result and
    appends nothing. The same `request_id` with a different payload hash is
    rejected with **HTTP 409** and appends nothing.
  - The inbox server-mints `turn_id` before the final transaction and writes it
    into the canonical payload and the receipt. Clients never choose one.
  - Common request fields are `request_id`, `client_instance_id`,
    `client_created_at_ms` (untrusted telemetry, never used for ordering or
    identity). The server stamps `source_surface=inherent_v2`.
  - The accepted response is `{status, request_id, input_event_uid, turn_id,
    session_id?}`; the ASR response additionally carries `utterance_id`, `text`
    and `emotion`, per :1236-1243.
- **The principal is the v2 token holder.** The v2 socket authenticates a shared
  per-boot bearer token and yields no per-user identity, so the inbox's
  `authenticated_principal` is one constant naming that credential (e.g.
  `"inherent_v2"`), supplied by the runtime, not parsed from the request. The
  key still separates clients through `client_instance_id`.
- **`turn_id` is reused, never replaced.** `claim_input_once`
  (jarvis/state/input_claim.py:143) already reuses the trigger payload's
  `turn_id` and mints nothing; this card pins that with a hermetic case rather
  than changing it: a v2 text submit's receipt `turn_id` equals the `turn_id`
  on the `turn.started` the pump appends for that trigger.
- **The view correlates the request to the group.** The L2 fold records
  `turn_id → source_client_request_id` from a `surface.user_intent` row that
  carries one — recorded before the `RESPONSE_EVENT_TYPES` check so the row
  stays irrelevant and produces no envelope — and stamps
  `source_client_request_id` on the `ResponseView` and `ResponseGroupView` it
  builds at `_open`, exactly as `turn_id` is already mirrored on both. The
  mapping is part of `InherentViewCheckpoint`, bounded (an entry is dropped once
  its group opens, and the map has an explicit cap), so a catch-up re-fold from
  a checkpoint stamps the same value. The presenter carries the field into the
  `response.opened` change and the `response_groups` snapshot item; the
  `ResponseOpened` DTO and its Swift twin gain one optional field. A group whose
  turn did not originate from an inbox input carries `null`.
- **ASR correlation is receipt-side only.** `VoicePipeline.run_turn` commits
  `utterance.received` itself and this card may not touch `jarvis/surface/voice_*`,
  so the ASR v2 handler cannot append that row inside the inbox transaction and
  cannot stamp `source_client_request_id` on it. Instead: the inbox mints the
  `turn_id` and passes it to the pipeline, and the final transaction resolves
  the receipt from the committed row. Recovery closes the duplicate window by
  lookup rather than by transaction — before re-running ASR for a retry whose
  lease is `processing` or expired, the inbox looks for an `utterance.received`
  already carrying that `turn_id` (the query shape of
  input_claim.py:127-134) and resolves the original result from it. Exactly one
  `utterance.received` per accepted request either way.
- **v1 stays byte-identical.** The v1 routes, `_run_asr_submit`'s body, and
  `BridgeBackend` do not change. The v2 ASR handler is a separate handler that
  calls the same `deps.voice_pipeline_callable` with the same positional
  `(pcm, turn_id, channel, language)` and reproduces the same bounds and error
  codes (413/415/422/503, plus 400/500/501) by **duplicating the check sequence**
  while reusing the module-level `_ASR_MAX_BYTES`, `_ASR_ACCEPTED_CONTENT_TYPES`
  and `_decode_wav_to_pcm16_mono_16k`. No shared validator is factored out,
  because doing so would rewrite the v1 function's body.
- **L5 stays inside its import rules.** The routes call injected callables on
  `InherentV2Deps` (one for text, one for ASR); the runtime binds them to the L2
  inbox with a per-call connection, the way `submit_callable` already opens
  `open_event_log` per call (inherent_loop.py:2882). `inherent_server.py` opens
  no database and mints no identity.
- **Swift gains an input client and one pending entry.**
  `desktop/inherent-swift/InherentRealtime/InputSubmissionClient.swift` (§12
  :1705) builds both v2 requests over an injected HTTP transport with a UUID
  `request_id` and an injected `client_instance_id` (the InherentRealtime module
  cannot reach `RealtimeTransportV2.mintIdentity`, which lives in the
  InherentCard target), retries the identical request once when the response is
  lost, surfaces the different-payload rejection as a typed error rather than a
  retry, and exposes the accepted receipt. One `LocalPresentationEvent` case and
  one `InherentUXState` entry keyed by `request_id` hold the local pending
  input; `applyOpened` resolves it when `response.opened` carries the matching
  `source_client_request_id` and ignores an unknown one. The app keeps v1:
  `RealtimeTransportV2.enabled` stays false, no UI wiring, no `BridgeBackend`
  change.

## Affected contracts and files
- L2 jarvis/state/input_submission_inbox.py (new) — receipts, payload-hash
  claim/resolve, processing lease, the `BEGIN IMMEDIATE` final transaction,
  server-minted `turn_id`, 409 on a payload-hash conflict, crash recovery.
- L2 jarvis/state/inherent_view.py — the `turn_id → source_client_request_id`
  map (fed by `surface.user_intent`, still an irrelevant row), one added field
  mirrored on `ResponseView`/`ResponseGroupView`, and the map inside
  `InherentViewCheckpoint`.
- L5 jarvis/surface/inherent_server.py — two new routes behind `deps.v2`, the
  shared bearer check, two new `InherentV2Deps` callables. `_run_asr_submit` and
  every v1 route unchanged.
- L5 jarvis/surface/inherent_protocol.py — request/response models for both
  routes; `ResponseOpened` gains one optional field.
- L5 jarvis/surface/inherent_presenter.py — the added field on the
  `response.opened` change and the `response_groups` snapshot item.
- runtime jarvis/runtime/inherent_loop.py — deps wiring lines only, beside
  :3022-3040.
- desktop/inherent-swift/InherentRealtime/InputSubmissionClient.swift (new);
  RealtimeViewDTOs.swift (one optional field), RealtimeState.swift +
  RealtimeEffects.swift + RealtimeReducer.swift (one pending-input entry, one
  local event case, one resolve branch);
  desktop/inherent-swift/InherentCardTests/Realtime/ (new test file).
- tests/integration (new Python tests); scripts (the live smoke).

## Boundaries and non-goals
- Layers that may change: L2 (one new module plus the view field), L5, runtime
  wiring, Swift `InherentRealtime` + its tests, tests, scripts.
- Must not change: the v1 routes and `_run_asr_submit`'s body;
  `jarvis/surface/voice_*` (lane B owns it); `BridgeBackend`,
  `NativeCardModel`, and the `InherentCard` app;
  `/inherent/image-submit` and its 501; the D8 snapshot/delta formats beyond the
  one added field; control frames over the socket (`cancel_action`,
  `confirmation_decision`, `stop_speaking`, `ptt_begin`/`ptt_cancel`,
  `dismiss_item` belong to the action-and-confirmation-projections and
  stop-speech-and-ptt-control cards); `jarvis/state/event_log.py`.
- Non-goals: `POST /inherent/image-submit/v2`, artifact staging, and
  content-addressed finalization — the advertised `image_input` capability
  stays false (inherent_loop.py:2758); PTT capture tokens and `input.ptt_begin`
  /`input.ptt_cancel`; making v2 the default or wiring the card UI to it;
  `session_id` on the receipt (nothing binds a realtime voice session to this
  path yet, so it is absent); a per-user principal.

## Rejected approaches
- Adding a version field or an `Authorization` check to the v1 `/inherent/submit`
  — D32 (:1776-1782) keeps v1 and v2 apart and requires v1 to stay
  byte-compatible.
- Factoring `_run_asr_submit`'s validation into a validator shared with the v2
  handler — it rewrites the v1 function's body, which lane B is concurrently
  editing; duplicating six checks is the smaller and safer diff.
- Letting the inbox call `emit_surface_user_intent` — it goes through
  `emit_event`, which refuses a caller-owned transaction (event_log.py:1629), so
  the receipt and the event could not commit together.
- Adding `source_client_request_id` to the `surface.user_intent` /
  `utterance.received` registry entries — unknown payload keys are already
  accepted (event_log.py:1469-1471), and event_log.py is outside this card.
- Making the fold treat `surface.user_intent` as a relevant row — it would emit
  a `view.delta` with no changes for every input row, adding wire traffic for a
  fact the next `response.opened` already carries.
- Deriving the client's pending-input resolution from `turn_id` instead of
  `source_client_request_id` — the client never learns the `turn_id` until the
  HTTP response returns, which is exactly the response a retry assumes was lost.
- Minting the `turn_id` in `claim_input_once` or in the route — the ADR
  (:1329-1333) puts the mint in the inbox before the final transaction, and
  `claim_input_once` already reuses the payload id.

## Acceptance evidence
- Positive (Python, hermetic): raw pytest output of the new integration tests
  ending in a pass line, covering — text submit happy path returning
  `status`/`request_id`/`input_event_uid`/`turn_id` with exactly one
  `surface.user_intent` appended carrying `source_client_request_id` and
  `source_surface="inherent_v2"`; an identical retry returning the same
  `input_event_uid` and `turn_id` with the row count unchanged; the same
  `request_id` with a different payload returning 409 with the row count
  unchanged; ASR v2 happy path plus 413, 415, 422 and 503 matching
  tests/integration/test_inherent_server_asr_submit.py; an unauthenticated or
  wrongly-tokened request to either route refused exactly as `/inherent/ws/v2`
  refuses one (HTTP 403); the crash-window property — a receipt row never exists
  without its input event, asserted by driving a fault between append and commit
  and showing neither row survives; `turn_id` reuse — the `turn.started` that
  `claim_input_once` appends for the v2 trigger carries the receipt's `turn_id`;
  and a `view.delta` whose `response.opened` carries the submitting
  `source_client_request_id`, driven through the existing `_Rig`/`_Socket`
  doubles (tests/integration/test_inherent_sequencer.py:379-485).
- Positive (Swift): raw output of `bash scripts/test_inherent_swift.sh` showing
  0 failures and the new `InputSubmissionClient` tests over a fake HTTP
  transport — a retried identical request is idempotent, a different-payload
  rejection surfaces as an error rather than a retry, a pending input resolves
  on the matching `response.opened`, and an unknown `source_client_request_id`
  is ignored.
- Layer proof: `grep -nE "^from jarvis\.|^import jarvis\."
  jarvis/state/input_submission_inbox.py` names only `jarvis.constitution`,
  `jarvis.shared` and `jarvis.state`.
- Regression: the full hermetic suite
  `PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and not live_codex"`
  shows at least the launch baseline (885 passed / 64 deselected at c87f5e0)
  plus this card's new tests; tests/integration/test_inherent_server_asr_submit.py,
  test_serve_inherent_smoke.py and test_inherent_sequencer.py pass unchanged;
  `bash scripts/test_inherent_swift.sh` shows at least the Swift count at launch
  (160) plus the new tests; `lint-imports`, `ruff check .` and
  `mypy --strict jarvis tests scripts tools` each exit 0 with printed counts.
- Live run: **required** (real LLM, DeepSeek presets unchanged). Start the
  daemon from this worktree on its own runtime root and a port other than 8006,
  with `realtime.enabled`, the v2 identity/hub flags and
  `realtime.inherent.v2_sequencer.enabled` on. Run a smoke script (extend
  scripts/smoke_inherent_sequencer.py or add a sibling in the same shape) that
  connects the v2 socket, POSTs a real question to `/inherent/submit/v2` with
  the Bearer token, re-POSTs the byte-identical request and prints the identical
  receipt, POSTs the same `request_id` with different text and prints the 409,
  then prints the `view.delta` `response.opened` carrying
  `source_client_request_id` and the `surface.response_emitted` that closes that
  turn; then POSTs a synthesized WAV (the `_wav` shape at
  tests/integration/test_voice_ptt_end_to_end.py:29) to
  `/inherent/asr-submit/v2` and shows the `utterance.received` row carrying the
  receipt's `turn_id`. Canary: `request_id`, `input_event_uid`, `turn_id`,
  `response_group_id` printed as one chain. Route audio to BlackHole 16ch via
  `SwitchAudioSource` and restore the previous device afterwards; do not touch
  the daemon on 127.0.0.1:8006.

## Docs to sync
- docs/adr/0014-inherent-realtime-ux.md:1298-1299 — "Until this route exists the
  advertised capability remains false" must be scoped to the image route, since
  after this card the text and ASR v2 routes exist while `image_input` stays
  false.
- docs/adr/0014-inherent-realtime-ux.md:1332 — `claim_turn_once` names a
  function that does not exist; the real primitive is `claim_input_once`
  (jarvis/state/input_claim.py:143). Correct the name or record the deviation.
- docs/adr/0014-inherent-realtime-ux.md:1746 (§12 file map) and :1860 (§14 step
  5) — expected unchanged; state that explicitly.
- docs/spec.html — expected unchanged (no new layer boundary, ownership rule or
  persistent-state semantic beyond what the ADR already owns); state that
  explicitly.
- If the code cannot meet an ADR rule as written, stop and report instead of
  editing the rule.

## Open questions
(none)

## /goal condition
Implement docs/goals/v2-input-endpoints.md on the current branch. The goal is met when all of the following appear in the transcript: (1) the diff adds `jarvis/state/input_submission_inbox.py` (idempotency key of authenticated principal + client_instance_id + request_id, one `BEGIN IMMEDIATE` that appends the canonical input event and stores the receipt together, the D21 processing lease for ASR, a server-minted turn_id, and a 409 on the same request_id with a different payload hash), `POST /inherent/submit/v2` and `POST /inherent/asr-submit/v2`, registered only when `deps.v2` is set and authenticated with the bearer check `/inherent/ws/v2` uses, one added `source_client_request_id` field through `inherent_view.py`, `inherent_presenter.py` and `inherent_protocol.py`, and `desktop/inherent-swift/InherentRealtime/InputSubmissionClient.swift` plus one pending-input entry in the reducer/state, with NO change to the v1 routes, to the body of `_run_asr_submit`, to `jarvis/surface/voice_*`, to BridgeBackend/NativeCardModel/the InherentCard app, to `/inherent/image-submit`, or to `jarvis/state/event_log.py`, and with `RealtimeTransportV2.enabled` still false; (2) raw pytest output of the new integration tests ending in a pass line and covering the text happy path receipt, an identical retry returning the same input_event_uid/turn_id while appending nothing, a different payload under the same request_id returning 409 while appending nothing, the ASR v2 happy path and its 413/415/422/503 parity with test_inherent_server_asr_submit.py, an unauthenticated request refused exactly as the v2 socket refuses one, a crash-window check showing no receipt exists without its input event, turn_id reuse where the appended `turn.started` carries the receipt's turn_id, and a `view.delta` whose `response.opened` carries the submitting `source_client_request_id`; (3) the layer grep showing `input_submission_inbox.py` importing only `jarvis.constitution`/`jarvis.shared`/`jarvis.state`; (4) raw output of the full hermetic suite run with `-m "not live_llm and not live_codex"` showing at least 885 passed and 64 deselected plus the new tests, and of `test_inherent_server_asr_submit.py`, `test_serve_inherent_smoke.py` and `test_inherent_sequencer.py` passing unchanged; (5) raw output of `bash scripts/test_inherent_swift.sh` with 0 failures, showing at least the launch count of 160 plus the new InputSubmissionClient tests (retry idempotent, different-payload rejection surfaced, pending input resolved by the matching delta, unknown request id ignored); (6) raw output of `lint-imports`, `ruff check .` and `mypy --strict jarvis tests scripts tools`, each exiting 0 with its printed counts; (7) a real live run against a daemon started from this worktree on a port other than 8006 with realtime and the v2 sequencer flags on and the DeepSeek presets unchanged, whose raw smoke output shows a text submit receipt, the byte-identical retry returning the identical receipt, a 409 for the same request_id with different text, the `view.delta` `response.opened` carrying `source_client_request_id`, the `surface.response_emitted` for that turn, and an ASR v2 submit of a synthesized WAV producing `utterance.received` with the receipt's turn_id — with the canary `request_id`, `input_event_uid`, `turn_id`, `response_group_id` printed as one chain, and a statement that audio went to BlackHole 16ch and the previous device was restored; (8) every entry under Docs to sync updated or explicitly judged unchanged, following the rule: when the change alters a documented contract, invariant, ownership boundary, or externally relevant behavior, update the canonical document that owns that fact, do not document what the code already makes clear, and do not duplicate a fact across documents; (9) each slice committed with the project commit skill and `git status` clean; (10) one Progress line per slice appended to the card. Or stop after 70 turns.

## Progress
- (none yet)
