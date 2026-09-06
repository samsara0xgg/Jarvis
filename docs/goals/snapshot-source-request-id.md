# Goal: snapshot-source-request-id

## Goal
The Swift `ResponseGroupSnapshot` DTO decodes the `source_client_request_id`
the Python snapshot already sends, and — only if a test first proves the gap is
real — the snapshot-apply path retires the matching optimistic `pendingInputs`
row the same way `applyOpened` already does.

## Why
Python emits this field on both `response.opened` and the response-group
snapshot item. Swift consumes it on `response.opened` only. So a submission
correlated on a live socket is correlated, and the same submission recovered
through a reconnect snapshot is not. The asymmetry is in one client struct.

## Current behavior
- Python carries the field end to end, and this card changes none of it:
  `ResponseGroupSnapshotItem.source_client_request_id: Identity | None = None`
  (jarvis/surface/inherent_protocol.py:304, on the model at :294-304), populated
  on the group view at jarvis/state/inherent_view.py:1083 and on the response
  view at :1072, serialized into the snapshot item at
  jarvis/surface/inherent_presenter.py:242 and into `response.opened` at :74.
  This is a verified fact about the tree at `88bf605`, not a task.
- The Swift `ResponseGroupSnapshot` struct
  (desktop/inherent-swift/InherentRealtime/RealtimeViewDTOs.swift:627-641) has
  five fields (:628-632) and five `CodingKeys` (:634-640). None of them is
  `source_client_request_id`. The value is dropped at decode and never reaches
  the reducer or the UI.
- The precedent, and the exact shape to mirror, is `ResponseOpened`: field
  `sourceClientRequestId: String?` at RealtimeViewDTOs.swift:175 under its D21
  doc comment at :173-174, `CodingKeys` entry at :188.
- `applyOpened` (RealtimeReducer.swift:327-335) is the only place in the reducer
  that clears an optimistic row: `state.pendingInputs.removeValue(forKey:
  requestID)` at :334, guarded by `if let requestID = opened
  .sourceClientRequestId` at :333.
- `adopt(_:into:)` (RealtimeReducer.swift:136-222), including its per-group loop
  at :141-188 and the state assignments at :190-221, never reads or writes
  `state.pendingInputs`.
- `pendingInputs` is declared at RealtimeState.swift:548 as
  `[String: PendingInputState]` keyed by `request_id` (D21 comment at :547),
  defaulted in the initializer at :561 and assigned at :573. It is populated in
  exactly one place, RealtimeReducer.swift:824, from
  `.local(.inputSubmitted(...))`, with bounded eviction at :825-832. Its only
  other reads are the assertions in
  InherentCardTests/Realtime/InputSubmissionClientTests.swift:138, :147, :168
  and :182-184.
- **Hypothesis, not an observed failure.** The recon inferred from the above
  that an optimistic input submitted before a disconnect, whose turn opened
  while the client was away, is rebuilt from the reconnect snapshot with its
  `pendingInputs` row still present — so the panel keeps showing it as unsent.
  Nobody reproduced this. No test today asserts anything about `pendingInputs`
  across a snapshot adoption. The card treats it as a claim to be tested, and
  the test decides whether the reducer is touched at all.

## Target behavior
Three outcomes, in this order.

1. A Swift test exists that drives a `pendingInputs` entry through a snapshot
   adoption whose group carries the matching `source_client_request_id`, and
   that test fails on the tree as it stands today, with its raw failure output
   quoted. (Unknown keys are ignored by Swift `Decodable`, so such a fixture
   decodes fine today — it simply loses the value.)
2. `ResponseGroupSnapshot` gains `sourceClientRequestId: String?` and its
   `CodingKeys` entry `case sourceClientRequestId = "source_client_request_id"`,
   mirroring RealtimeViewDTOs.swift:175 and :188 exactly — same optional type,
   same spelled-out key, same D21 comment style.
3. The snapshot-apply path clears `pendingInputs` for a group that carries the
   id, reusing the behavior at RealtimeReducer.swift:333-335 rather than
   inventing a second, differently-shaped clearing rule. Outcome 3 is
   conditional on outcome 1: see the fork in Acceptance evidence.

## Affected contracts and files
- L5 client `desktop/inherent-swift/InherentRealtime/RealtimeViewDTOs.swift:627-641`
  — one field plus one CodingKey on `ResponseGroupSnapshot`.
- L5 client `desktop/inherent-swift/InherentRealtime/RealtimeReducer.swift:136-222`
  — conditional: the group loop clears `pendingInputs` for a carried id.
- Tests `desktop/inherent-swift/InherentCardTests/Realtime/InputSubmissionClientTests.swift`
  — the natural home; it already owns every `pendingInputs` assertion.
- Fixtures `desktop/inherent-swift/InherentCardTests/Realtime/RealtimeTestFixtures.swift:312-325`
  — `snapshotGroup(...)` builds the group dict and has no parameter for the id;
  `Reducing.goLive(...)` at :369-389 adopts a snapshot; `Fx.opened(...)` at
  :25-35 already takes `sourceRequest:` and is the naming precedent.
- Docs `docs/adr/0014-inherent-realtime-ux.md:1362-1367`.

## Boundaries and non-goals
- Layers that may change: the Swift client (`desktop/inherent-swift/`) and
  ADR-0014 prose. Nothing else.
- Must not change: any Python production file — `jarvis/` gets no production
  change at all. If the session finds itself editing a Python file other than a
  test, it has misread the card: stop and report.
- Must not change: `applyOpened` (RealtimeReducer.swift:327-335); it is the
  precedent to reuse, not to edit.
- Must not change: `PROTOCOL_VERSION`, `VIEW_SCHEMA_VERSION`,
  `RuntimeCapabilities`, or any other DTO. Same wire-compat rule as the merged
  cancel-request card: Python models are `extra="ignore"` and Swift
  `CodingKeys` are spelled out, so an added optional field needs no version
  gate and no capability entry.
- Non-goal: the ASR v2 `utterance_id` gap. It is real and separately owned. It
  touches `jarvis/runtime/inherent_loop.py`, and another lane is in that file
  right now. Do not start it, do not open the file.
- Non-goal: any change to how `pendingInputs` is keyed or populated — the
  `request_id` key (RealtimeState.swift:548) and the bounded insertion at
  RealtimeReducer.swift:824-832 stay exactly as they are.

## Rejected approaches
- Adding the field to the Python snapshot item — already there
  (inherent_protocol.py:304, inherent_presenter.py:242). Nothing to do.
- Writing the reducer clear first and a test after. The bug is a code-reading
  inference; a test written after the fix would be written to pass and would
  prove nothing about whether the gap was real.
- A second clearing rule shaped differently from `applyOpened`. Two rules for
  one invariant is how they drift.
- Bumping `VIEW_SCHEMA_VERSION` or gating on a capability. An optional field
  both sides tolerate needs neither.

## Acceptance evidence
- **First, and it decides the rest.** The R2 test, run before any production
  edit, with its raw failure output quoted: a `pendingInputs` entry survives a
  snapshot adoption whose group carries the matching id. Then the same test run
  after the change, raw output quoted, passing.
  - **Fork.** If that test cannot be made to fail — something already clears
    the entry, or the scenario cannot be constructed — then the reducer change
    has no evidence behind it. In that case: ship the DTO field alone (target
    behavior 2), keep a decode test for it, record in Progress *why* the
    reducer was left unchanged, and report. Do **not** change the reducer on
    the strength of the theory.
- Positive, Swift: raw output of `bash scripts/test_inherent_swift.sh` ends with
  `Executed N tests, with 0 failures`. Baseline is 176 at `88bf605`; record it
  from a pre-change run and state N as that baseline plus the new case count.
  One case for the reducer behavior, at most one for the DTO decode. Do not
  spend a test per DTO field.
- Regression, Python — evidence of *absence*, not a new baseline: run
  `PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and not live_codex"`
  and quote the count. It must be identical to the pre-change count, because no
  Python file is touched. A changed count means the card was violated.
- Gates, each with its exit code shown, all expected untouched by a Swift-only
  change: `PYTHONPATH=. .venv/bin/lint-imports`,
  `PYTHONPATH=. .venv/bin/ruff check .`, and
  `PYTHONPATH=. .venv/bin/mypy --strict jarvis tests scripts tools`.
  `PYTHONPATH=.` is mandatory: without it the editable install resolves to the
  main checkout and a provenance test fails spuriously against the wrong tree.
- Live run: **not required**. No audio device is touched. Do not switch the
  system default output.
- `git status` clean at the end, every change committed.

## Docs to sync
- docs/adr/0014-inherent-realtime-ux.md:1362-1367 — one sentence recording that
  the response-group snapshot item carries `source_client_request_id` too, so a
  reconnecting client can retire an optimistic row it submitted before the
  disconnect. Today :1364-1367 states only that `response.opened` carries it;
  the Python code already does more than the ADR says.
  **Check for an errata section before writing** — do not assume the structure.
  A draft-time scan of the `## ` headings found sections 1-18 with no errata
  heading (§16 "Spec changes and explicit deviations" is at :2096); if that
  still holds, amend in place at :1362-1367.
- docs/spec.html — expected **unchanged**. A draft-time grep for
  `source_client_request_id`, `sourceClientRequestId`, `pendingInputs` and
  `pending_inputs` returned 0 matches; the spec does not own this fact. Confirm
  and say so in one line.

## Open questions

## /goal condition

Done when the transcript shows all of the following, as raw command output, not
summaries.

1. The proving test first. Before any production edit, the raw output of a
   Swift test run in which the new test FAILS, showing the stale `pendingInputs`
   entry surviving a snapshot adoption whose group carries the matching
   `source_client_request_id`. Then, after the change, the raw output of
   `bash scripts/test_inherent_swift.sh` showing it passing.
   FORK: if that test cannot be made to fail, the transcript says so with the
   evidence, ships ONLY the `sourceClientRequestId: String?` field plus its
   `CodingKeys` entry on `ResponseGroupSnapshot`, records under Progress why the
   reducer was left unchanged, and reports. The reducer must NOT be changed on
   the strength of the theory alone. Either arm satisfies this condition.
2. Swift count. The raw output of `bash scripts/test_inherent_swift.sh` ends
   with `Executed N tests, with 0 failures`. A pre-change baseline run is also
   shown (176 expected at `88bf605`), and N is stated as that baseline plus the
   new case count — one reducer case, at most one DTO decode case.
3. Python count unchanged. The raw output of
   `PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and not live_codex"`
   is shown with its count, and the transcript states that count is IDENTICAL
   to the pre-change count. It must be: no Python production file is touched. A
   Python production edit means the card was misread — stop and report.
4. Gates. `PYTHONPATH=. .venv/bin/lint-imports`,
   `PYTHONPATH=. .venv/bin/ruff check .`, and
   `PYTHONPATH=. .venv/bin/mypy --strict jarvis tests scripts tools` each shown
   exiting 0. `PYTHONPATH=.` is mandatory or a provenance test fails against
   the wrong checkout.
5. Live run NOT required. No audio device is touched; the system default output
   is not switched.
6. Boundaries held, stated explicitly: `applyOpened`
   (RealtimeReducer.swift:327-335), `PROTOCOL_VERSION`, `VIEW_SCHEMA_VERSION`,
   `RuntimeCapabilities`, every other DTO, and the keying/population of
   `pendingInputs` are all unchanged. The ASR v2 `utterance_id` gap is NOT
   started and `jarvis/runtime/inherent_loop.py` is NOT opened — another lane
   owns that file right now.
7. Docs. Each entry under "Docs to sync" is either updated or explicitly judged
   unchanged with a one-line reason:
   - docs/adr/0014-inherent-realtime-ux.md:1362-1367 — gains one sentence that
     the response-group snapshot item carries `source_client_request_id` too.
     CHECK for an errata section first rather than assuming the ADR's
     structure; amend in place only if none exists.
   - docs/spec.html — expected unchanged; confirm the grep finds nothing.
   Documentation rule: when the implementation changes a documented contract,
   invariant, ownership boundary, or externally relevant behavior, update the
   canonical document that owns that fact; do not document what the code
   already makes clear; do not duplicate a fact across documents.
8. `git status` shows a clean tree, with each slice committed via the commit
   skill and a Progress line appended per slice.

Or stop after 12 turns and report what is proven and what is not.

## Progress
- Proving test — pre-change run of `bash scripts/test_inherent_swift.sh`:
  `Executed 177 tests, with 1 failure`, raw failure
  `InputSubmissionClientTests.swift:192: error: ... testAMatchingSnapshotGroupResolvesThePendingInput : XCTAssertTrue failed`
  — the `pendingInputs` row survived a snapshot adoption whose group carried
  the matching id. The hypothesis reproduced, so the reducer arm applies and
  the DTO-only fork does not.
- DTO + reducer + test — d36e917 — `ResponseGroupSnapshot` gains
  `sourceClientRequestId` and its `CodingKeys` entry; the `adopt` group loop
  clears `pendingInputs` for a carried id, mirroring `applyOpened` in place
  (no shared helper: the card forbids editing `applyOpened`).
  Swift 177/177 pass (176 baseline + 1 reducer case; no DTO decode case — the
  reducer case decodes the field through the real DTO and every existing
  snapshot test covers the absent-field path). pytest 1066 passed, 64
  deselected — identical to the pre-change count, no Python file touched.
  lint-imports KEPT (1/1), ruff, mypy strict (243 files) all exit 0.
- Docs — ADR-0014: no errata section exists (18 `## ` headings, §16 "Spec
  changes and explicit deviations" at :2096), so amended in place after the
  correlation chain. docs/spec.html unchanged: grep for
  `source_client_request_id`, `sourceClientRequestId`, `pendingInputs`,
  `pending_inputs` returns 0 — the spec does not own this fact.
