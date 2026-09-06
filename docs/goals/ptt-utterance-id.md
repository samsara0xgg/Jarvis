# Goal: ptt-utterance-id

## Goal
A PTT submission's `/inherent/asr-submit/v2` receipt carries a real
`utterance_id` instead of `null`, and an idempotent retry of that submission
returns the same one.

## Why
Every layer between the pipeline and the HTTP response already declares and
carries the field; nothing ever supplies a value, so the shipped receipt is out
of contract with ADR-0014:1292-1301. This is a wiring gap, not a missing id
source — the fix is one mint at one site, not a new identity concept.

## Current behavior

All line numbers below were re-pinned by grep at branch `realtime-integration`
tip `6a036e6` (after the foreground-arbitration merge). See Drafter notes for
what moved.

The chain, end to end, top-down:

- The wire contract already has the field. `AsrSubmitV2Response.utterance_id:
  Identity | None = None` (jarvis/surface/inherent_protocol.py:399-404, field at
  :402). `_BASE_CONFIG` (:41) is `strict=True, extra="ignore", frozen=True` with
  no `exclude_none`, so the key is serialized as `"utterance_id": null` today —
  present-and-null on the wire, never absent.
- L5 fills it 1:1 from the outcome: `utterance_id=outcome.utterance_id`
  (jarvis/surface/inherent_server.py:760, inside the response construction at
  :754-763), from `InputSubmissionOutcome.utterance_id: str | None = None`
  (jarvis/surface/inherent_server.py:228-245, field at :243).
- The runtime fills the outcome 1:1 from the L2 receipt: `_v2_accepted`
  (jarvis/runtime/inherent_loop.py:3464-3475) copies
  `utterance_id=receipt.utterance_id` (:3472).
- L2 carries it durably and completely. `InputReceipt.utterance_id: str | None =
  None` (jarvis/state/input_submission_inbox.py:128-144, field at :141); DB
  column `utterance_id TEXT` (:76); selected by `_SELECT_RECEIPT_SQL` (:84-89);
  written by the resolve UPDATE (:378-384) and the accepted build (:402); read
  back into a receipt by `_receipt_from_row` (:508-521, field at :518).
- The value that L2 stores comes from the emitted event:
  `utterance_id = event.payload.get("utterance_id")`
  (jarvis/runtime/inherent_loop.py:3535), passed to `resolve_asr_request` at
  :3544 as `utterance_id if isinstance(utterance_id, str) else None`.
- **The break.** `_submit_asr_v2` (jarvis/runtime/inherent_loop.py:3500-3550)
  invokes the pipeline positionally at :3531 —
  `voice_pipeline_callable(pcm, claim.turn_id, "inherent_ptt", language)` —
  through the adapter `_build_voice_pipeline_callable`
  (jarvis/runtime/inherent_loop.py:1920-1947), whose closure `_call` (:1938-1945)
  calls `pipeline.run_turn(audio_bytes=..., turn_id=..., channel=...,
  language=..., broadcast=False)` with **no** `utterance_id=`.
- In `VoicePipeline.run_turn` (jarvis/surface/voice_pipeline.py:99 onward) the
  parameter is `utterance_id: str | None = None` (:110) and the payload key is
  set only under a guard: `if utterance_id is not None:` / `payload["utterance_id"]
  = utterance_id` (:210-211). So the PTT `utterance.received` payload has no
  `utterance_id` key, :3535 reads `None`, and the receipt is `null` every time.
- An id source already exists and is not the problem: the wake/duplex path mints
  `self._utterance_id = "U" + secrets.token_hex(8)` in the assembler's `arm()`
  (jarvis/surface/voice_session.py:550) and threads it into the same parameter
  at jarvis/surface/voice_session.py:1337, inside `_commit_loop`'s `run_turn`
  call (:1331-1340).
- Nothing pins the current null. `test_asr_submit_v2_happy_path_and_retry`
  (tests/integration/test_inherent_submit_v2.py:386-422) asserts self-equality
  on retry (`again.json() == body`, :420) and never asserts `utterance_id`, so
  the defect neither breaks a test nor would a regression be caught.

## Target behavior

1. `VoicePipeline.run_turn` mints an utterance id when the caller did not supply
   one, and **always** sets `payload["utterance_id"]` — the guard at
   voice_pipeline.py:210-211 becomes unconditional over a value that is now
   never None. Reuse voice_session.py:550's exact form, `"U" +
   secrets.token_hex(8)`, so both paths mint identically. (`voice_pipeline.py`
   does not currently import `secrets`; it is stdlib and layer-legal per the
   module docstring at :1-13.)
2. No signature change anywhere. The PTT path needs no new argument, the adapter
   `_build_voice_pipeline_callable` keeps its four-positional shape, and the wake
   path is untouched because it already passes an id explicitly.
3. **MANDATORY PRE-CHECK, before any production edit.** Grep every reader of the
   `utterance_id` payload key and every consumer of `InputReceipt.utterance_id`
   (the list found by the drafter is under "Affected contracts and files" — verify
   it, do not trust it) and confirm that none of them depends on the key being
   **absent**, as opposed to present-and-null, to tell a PTT turn from a wake
   turn. State the result in Progress.
   - **Explicit fallback:** if any reader does depend on absence, do NOT use the
     shape in (1). Fall back to minting at the submission boundary in
     `_submit_asr_v2` and widening the `voice_pipeline_callable` signature, and
     say in Progress *which reader* forced the fallback. Do not silently turn a
     distinguishing signal into a constant.
4. One mint per PTT submission, stable across retry. A press-to-release IS one
   utterance. The retry path that
   tests/integration/test_inherent_submit_v2.py:386-422 exercises returns the
   same receipt from L2 without re-running the pipeline (`_submit_asr_v2` returns
   `_v2_accepted(claim)` at :3529 when the claim is already an `InputReceipt`),
   so it must return the same `utterance_id`. This is the property most likely to
   break under either shape; assert it.
5. **TASK — answer, do not assume: the Swift consumer.** Locate how the Swift PTT
   client consumes the `/inherent/asr-submit/v2` response and record the answer
   in Progress with evidence. ADR-0005 §6 describes that response as consumed
   directly, not through the view/snapshot reducer path. Both outcomes are
   acceptable; asserting either without looking is not.
   - If it decodes `utterance_id`: say whether a populated value changes anything
     (rendering, state, equality, a test fixture), and make whatever change that
     implies.
   - If there is genuinely no consumer: record that as a finding with the file
     evidence that supports it.
   - Starting evidence found by the drafter, to verify rather than rediscover:
     `desktop/inherent-swift/InherentRealtime/InputSubmissionClient.swift` —
     `InputSubmissionReceipt` (:55-76) declares `public var utteranceID: String?`
     (:62) with `case utteranceID = "utterance_id"` (:72); the ASR response is
     decoded into it by `decode` (:205-220) via `submitAudio` (:153-178, path at
     :160). The remaining question the lane must settle is whether any *reader*
     of `receipt.utteranceID` exists.

## Affected contracts and files

Readers of the `utterance_id` payload key on `utterance.received` — the R1
check list (four, all Python; verify each):

- L4/runtime jarvis/runtime/inherent_loop.py:3535 — `event.payload.get("utterance_id")`,
  the defect site itself; wants presence.
- L2 jarvis/state/input_submission_inbox.py:553 — inside `_resolve_processing_lease`
  (:524-...), the crash-recovery path reads
  `_optional_str(payload.get("utterance_id"))` off the already-committed
  `utterance.received` row and stores it via the UPDATE at :559-564. Absence-
  tolerant by construction (`_optional_str`, :587).
- tooling scripts/replay_endpointing.py:108-109 — prints `payload.get("utterance_id")`
  in the replay trail. Display only.
- test tests/integration/test_endpointing_partial_asr.py:627 — asserts
  `committed[0].attributes["utterance_id"] == payload["utterance_id"]` on a wake
  commit; the trace attribute comes from voice_pipeline.py:227, which passes
  `utterance_id` unconditionally. Wake-path only, unaffected by a PTT mint.

Consumers of `InputReceipt.utterance_id`:

- L2 jarvis/state/input_submission_inbox.py:141 (field), :378-384 (resolve UPDATE),
  :402 (accepted build), :443/:462 (`_ReceiptRow`), :518 (`_receipt_from_row`),
  :553-564 (crash recovery).
- L4 jarvis/runtime/inherent_loop.py:3472 — `_v2_accepted`'s 1:1 copy.
- L5 jarvis/surface/inherent_server.py:243 (`InputSubmissionOutcome` field), :760
  (response construction).
- L5 jarvis/surface/inherent_protocol.py:402 — the declared wire field.
- Swift desktop/inherent-swift/InherentRealtime/InputSubmissionClient.swift:62,:72
  — the decode site (see the R3 task above).
- tests tests/integration/test_input_submission_inbox.py:203,:216 — round-trips a
  literal `"U1"` through the inbox; independent of where a mint happens.

Expected to change:

- L5 jarvis/surface/voice_pipeline.py:110,:210-211 — the mint and the now-
  unconditional payload key; `import secrets` added at the top.
- test tests/integration/test_inherent_submit_v2.py — one new test (see
  Acceptance evidence).

## Boundaries and non-goals

- Layers that may change: L5 (`jarvis/surface/voice_pipeline.py`) and tests. The
  fallback in Target behavior 3, if and only if it is forced, additionally
  touches L4 `jarvis/runtime/inherent_loop.py`.
- **RE-PIN AT LAUNCH.** Every `jarvis/runtime/inherent_loop.py` number in this
  card is from tip `6a036e6` and that file moves by hundreds of lines per merge —
  the foreground-arbitration merge already moved every one of them by +2 while
  this card was being written. Before using any inherent_loop.py line number,
  grep for the symbol (`_submit_asr_v2`, `_build_voice_pipeline_callable`,
  `_v2_accepted`) by name. The same caution applies, less urgently, everywhere
  else.
- Must not change: the wake/duplex mint at jarvis/surface/voice_session.py:550
  or its threading at :1337. The wake path already passes an id explicitly and
  must keep doing so.
- Must not change: any wake-path utterance semantics.
- Must not change: the L2 `InputReceipt` shape
  (jarvis/state/input_submission_inbox.py:128-144), its DB column (:76), or
  anything else in the inbox.
- Must not change: `_v2_accepted`'s 1:1 copy
  (jarvis/runtime/inherent_loop.py:3464-3475).
- Must not change: anything about `turn_id` — not its minting, not its shape,
  not its threading.
- Non-goal: `VoicePipeline` idempotency per `turn_id`. It is the third C5
  residue, a real and separate defect in the same file, and it is unexamined.
  Do not start it. If the acceptance test surfaces it, record the observation in
  Progress and stop there.
- Non-goal: `session_id` on the PTT receipt. Same structural shape, stated
  non-goal of the v2-input-endpoints card, out of scope here.

## Rejected approaches

- Widening `voice_pipeline_callable` to take an `utterance_id` and minting in
  `_submit_asr_v2`. Larger diff: a signature change on the `InherentDeps`
  callable contract, the adapter closure, and every test double for it — for a
  value the pipeline is already able to mint. Kept only as the forced fallback
  under Target behavior 3.
- Minting in the adapter `_build_voice_pipeline_callable`. Same size as minting
  in `run_turn` but puts identity in a shape-rewriting closure while the wake
  path mints elsewhere: two mint sites for one concept.
- Deriving the id from `turn_id`. Turns and utterances are distinct identities in
  ADR-0006; collapsing them is a contract change, not a wiring fix.
- Asserting `utterance_id` inside the existing
  `test_asr_submit_v2_happy_path_and_retry`. Its fake pipeline
  (`_transcribing_pipeline`, tests/integration/test_inherent_submit_v2.py:348-372)
  builds the payload by hand and never reaches `run_turn`, so an assertion there
  would test the fake. See Acceptance evidence.

## Acceptance evidence

- Positive: ONE new integration test covering BOTH properties — a PTT submission
  through `/inherent/asr-submit/v2` whose receipt carries a non-null
  `utterance_id`, and whose documented retry returns the SAME one. One test, not
  one per property.
  - The test must exercise the real mint site. `_transcribing_pipeline`
    (tests/integration/test_inherent_submit_v2.py:348-372) is a hand-built fake
    that never calls `VoicePipeline.run_turn`; a test wired to it would assert
    the fake's payload. Drive a real `VoicePipeline` (stub recognizer) through
    `_build_voice_pipeline_callable`, following the precedent in
    tests/integration/test_inherent_server_asr_submit.py and
    tests/integration/test_voice_ptt_end_to_end.py, which both construct a real
    `VoicePipeline`.
  - Run it and show it FAILING before the production edit, then passing after.
    Raw pytest output both times.
- Regression: `PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and not
  live_codex"`. Record your OWN pre-change baseline count by running it before any
  edit; do not trust a number quoted from anywhere else, merges are landing on
  this branch. The post-change count must be the recorded baseline plus the new
  case count, with zero failures.
- Gates, each shown exiting 0: `PYTHONPATH=. .venv/bin/lint-imports`,
  `PYTHONPATH=. .venv/bin/ruff check .`, and
  `PYTHONPATH=. .venv/bin/mypy --strict jarvis tests scripts tools`.
  `PYTHONPATH=.` is mandatory.
- R3 answered: the Swift consumer question in Target behavior 5 is answered in
  Progress with file evidence, whichever way it lands. If the Swift side is
  touched at all, `bash scripts/test_inherent_swift.sh` is shown ending with
  `Executed N tests, with 0 failures`; if it is not touched, say so.
- Live run: **not required.** The id is hermetically observable on the HTTP
  receipt; nothing here depends on real audio. Prefer skipping it. If the lane
  chooses an optional live PTT confirmation anyway, the audio rule applies in
  full: capture the pre-run system output route first, restore it in a
  `finally`/`trap`, and if the captured route is already `BlackHole 16ch`,
  restore `MacBook Pro Speakers` instead.

## Docs to sync

- docs/adr/0014-inherent-realtime-ux.md:1292-1301 — **no amendment expected.**
  The ASR response block already lists `utterance_id` (at :1296) alongside
  `session_id / turn_id / status / text / emotion`. The ADR is right and the code
  was wrong; this change brings the code into the contract the ADR already
  states. Do not add a sentence restating it. Say this explicitly rather than
  leaving the section blank.
- docs/spec.html — judge explicitly, expect unchanged. No layer boundary,
  ownership, invariant, or externally relevant behavior moves; the field was
  already declared at every layer.

## Open questions
(none)

## /goal condition

Done when the transcript shows all of the following as raw command output, not
summaries.

1. R1 pre-check FIRST, before any production edit. The transcript shows the grep
   output for readers of the `utterance_id` payload key and consumers of
   `InputReceipt.utterance_id`, and states in one line whether any reader depends
   on the key being ABSENT rather than present-and-null. If none does, the mint
   goes inside `VoicePipeline.run_turn` and the payload key becomes
   unconditional. If one does, the transcript NAMES that reader, the mint moves
   to the submission boundary with a widened callable instead, and Progress
   records which reader forced it. Either arm satisfies this item; skipping the
   check does not.
2. The proving test. Raw pytest output of the new single test FAILING before the
   production edit, then PASSING after. It asserts both properties in one test: a
   PTT `/inherent/asr-submit/v2` receipt carries a non-null `utterance_id`, and
   the identical retry returns the SAME value. The transcript states that the
   test drives a real `VoicePipeline` rather than the hand-built
   `_transcribing_pipeline` fake, which never reaches `run_turn`.
3. Regression. Raw output of
   `PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and not live_codex"`
   from BEFORE any edit, with its count quoted as the baseline, and again after,
   with zero failures and a count equal to that baseline plus the new case count.
   A count quoted from the card or from another session does not satisfy this.
4. Gates. `PYTHONPATH=. .venv/bin/lint-imports`, `PYTHONPATH=. .venv/bin/ruff check .`,
   and `PYTHONPATH=. .venv/bin/mypy --strict jarvis tests scripts tools` each shown
   exiting 0.
5. The Swift question answered, not assumed. Progress records how the Swift PTT
   client consumes the `/inherent/asr-submit/v2` response, with file:line
   evidence — either it decodes `utterance_id` and the transcript says whether a
   populated value changes anything, or there is no consumer and the transcript
   shows the evidence for that. If Swift changed, `bash scripts/test_inherent_swift.sh`
   is shown ending `Executed N tests, with 0 failures`.
6. Boundaries held, stated explicitly: the wake mint at voice_session.py:550 and
   its threading, the `InputReceipt` shape and its DB column, `_v2_accepted`'s
   1:1 copy, and everything about `turn_id` are all unchanged. `VoicePipeline`
   turn_id idempotency was NOT started. Every `jarvis/runtime/inherent_loop.py`
   line number was re-pinned by grepping the symbol name before use.
7. Live run NOT required and not performed; no audio device was touched and the
   system default output was not switched. If one was done anyway, the transcript
   shows the captured pre-run route and its restore, with `MacBook Pro Speakers`
   restored if the captured route was `BlackHole 16ch`.
8. Docs. Each entry under "Docs to sync" is updated or explicitly judged
   unchanged with a one-line reason, following the rule: update the canonical
   document that owns a changed contract, do not document what the code makes
   clear, do not duplicate a fact across documents. Expected here:
   docs/adr/0014-inherent-realtime-ux.md:1292-1301 judged UNCHANGED because it
   already states `utterance_id` on the ASR response and the code was the thing
   out of contract; docs/spec.html judged unchanged.
9. Each slice committed with the project commit skill, `git status` clean, and a
   Progress line per slice in the card.

Or stop after 30 turns.

## Progress
- (empty)
