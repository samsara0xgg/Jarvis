# Goal: snapshot-handoff-backpressure

## Goal
A v2 snapshot whose plan is larger than the durable lane's byte budget **in
total, with every individual frame under it** is delivered page by page — the
handoff waits for the sender to free capacity instead of pushing the whole plan
in one uninterruptible burst — so the client adopts instead of being closed with
a false `client_backpressure`; and the one shape that can never be delivered, a
single frame larger than the whole lane budget, fails immediately with a
distinct honest reason instead of stalling out the adoption deadline.

## Why
The per-client-flow-control verifier already raised this as a non-blocking
follow-up: "a view whose encoded snapshot exceeds durable_bytes (about 16 pages)
or durable_frames closes every new client before adoption, because D11 shares
one lane between snapshot and durable and D8 caps the page but not the total"
(docs/goals/per-client-flow-control.md, Progress slice 5). The close is not just
premature, it is mislabelled: the client is told it applied backpressure when it
was never handed a byte.

## Current behavior

All line numbers below were re-pinned by grep against the working tree at
`eb4d90c` on `realtime-integration`.

- `_handoff` (jarvis/runtime/inherent_hub.py:401-440) enqueues every frame of
  the plan at :411-412, then awaits the ACK at :420-424. **That loop contains
  zero await points**, and `_enqueue` (:299-317) is plain sync, so the entire
  snapshot lands on `self._durable` in one uninterrupted burst.
- `_run_sender` (:455-472) is a separate task created in `start()` (:215-221,
  the sender at :216-218). It parks on `self._wake` (:462-463) and can only run
  when the handoff yields — which first happens at :421, *after* the last frame
  is enqueued. So the lane cannot drain while the plan is being pushed.
- `_enqueue`'s bound check (:304-307) trips `_fail_now("client_backpressure",
  ...)` (:308-312, `_fail_now` defined at :479) as soon as
  `self._durable_bytes + size > durable_bytes` (1_048_576, :109) or
  `len(self._durable) >= durable_frames` (256, :108). The client is closed 1008
  with a best-effort `server.resync_required` notice carrying
  `{"reason": reason}` (:503), mid-burst, before it has seen `snapshot.end`.
- **The reason code is factually false.** The client is not applying
  backpressure; it has not been given the chance to receive anything. The
  sender never got a turn.
- Reachable trigger: **one still-open response whose streamed segments exceed
  ~1 MiB.** Every existing cap misses it:
  - `INLINE_DOCUMENT_BUDGET_BYTES` (16384, jarvis/state/inherent_view.py:118)
    binds only at close — `bound_document` runs on the
    `surface.response_emitted` fold branch (:694-701) — and the design forbids
    cutting an open one (:41-45).
  - `RECENT_TERMINAL_GROUP_LIMIT` / `RECENT_TERMINAL_ACTION_LIMIT` (:119-120,
    documented :33-36) bound retained *terminal* state only.
  - `SNAPSHOT_PAGE_MAX_BYTES` (65536, jarvis/surface/inherent_presenter.py:46,
    applied at :313) bounds **one page**, and `_pack_pages` never splits an item
    (:296-301, and the comment at :303-307: "A single group above the cap gets a
    page of its own rather than being cut").
  - Nothing bounds the plan's **total**: `build_snapshot_plan` concatenates
    every section's pages (:356-360).
- The two existing durable-overflow tests
  (tests/integration/test_inherent_flow_control.py:107-150 and :153-194) pin
  *post-adoption* backpressure: both attach through `_adopt` (:80-85) against a
  small view and only then hold the socket. Neither covers the handoff burst.

## Target behavior

### The two shapes

The single bound `self._durable_bytes + size > durable_bytes` (:306) is tripped
by two different situations, and they get two different answers. Everything else
in this card follows from the split.

- **Fixable — the plan's TOTAL exceeds `durable_bytes`, every individual frame
  is under it.** Several open responses; one large group beside other sections.
  The only reason this fails today is that the sender never runs.
- **Unfixable by queue depth — ONE page frame is itself larger than
  `durable_bytes`.** One open response over ~1 MiB is exactly this case:
  `response_group_item` (jarvis/surface/inherent_presenter.py:213-242) inlines
  every segment of every response of the group into a *single* item, and an item
  above the page cap gets a page of its own (:303-307). At lane depth zero the
  check is still `0 + size > durable_bytes`, so **waiting for capacity waits for
  a level that can never be reached**.

### Fixable shape: deliver it

- The plan-enqueue loop yields. Before a frame that would exceed either durable
  bound, `_handoff` waits for the sender to pop enough frames, then enqueues.
  This races nothing: `begin_snapshot` sets `lane.live_frontier = None`
  (jarvis/runtime/inherent_view_sequencer.py:231) and `drain()` skips a lane
  whose frontier is `None` (:215) until `complete_snapshot` sets it (:290), so
  the lane is out of live fan-out for the whole snapshot phase. And
  `_durable_bytes` is a live depth gauge, not a lifetime total — incremented at
  :316, decremented on every pop at :344 — so waiting for capacity is
  meaningful.
- Such a snapshot is delivered whole and the client adopts, where today it is
  closed with `client_backpressure`.
- **One bound, the existing one.** The whole handoff — enqueue plus ACK — stays
  under `SNAPSHOT_ADOPTION_DEADLINE_S` (5.0, jarvis/runtime/inherent_hub.py:81,
  injected as `adoption_deadline_s` at :538). No second budget is introduced. A
  client that genuinely does not receive still fails, at the same deadline, now
  for the honest reason. An unguarded capacity wait would let a non-draining
  client hang the handoff forever — strictly worse than today's bug.

### Unfixable shape: fail fast, and say the true thing

- A frame whose own encoded size exceeds `durable_bytes` **must fail
  immediately, at enqueue time.** The new capacity wait must not apply to it: it
  must not consume the adoption deadline first. A fast honest failure beats a
  slow one, and a five-second stall ending in the same wrong answer is strictly
  worse than the behavior being fixed.
- It fails with a **distinct, honest reason** — not `client_backpressure`, which
  is the original lie in this defect. The client has done nothing wrong in
  either shape, and in this one it is not even a backpressure situation. Carry
  the new reason on the **existing** `server.resync_required` control frame
  (`_fail_now(..., notify=True)`, jarvis/runtime/inherent_hub.py:495-503, payload
  `{"reason": reason}`), closed 1008 through the existing path (:504). **Do not
  invent a new frame type or DTO.** Suggested value:
  `snapshot_frame_over_budget`; the implementation may choose a better string,
  but it must be distinct from every existing reason and it must name the frame,
  not the client.
- **The new reason value does not cross into the Swift client — verified, not
  assumed.** `grep -rn "resync_required\|resyncRequired" desktop/inherent-swift/`
  returns **zero matches**: nothing on the client reads the reason string, and
  there is no exhaustive switch over it. Swift does not even case on the
  *message type*: `events(fromFrame:)`
  (desktop/inherent-swift/InherentRealtime/RealtimeTransport.swift:248-281)
  handles `view.delta`, `snapshot.begin`, `snapshot.page` and `snapshot.end`,
  and routes everything else through `ServerEnvelope<EphemeralUpdate>` at
  :270-277. That decode calls `decodedKind`
  (desktop/inherent-swift/InherentRealtime/RealtimeViewDTOs.swift:155-157),
  which requires a payload `kind` field the notice's `{"reason": ...}` payload
  does not carry, so it throws and the transport returns
  `.socketFailed(reason: "protocol_error")` (:278-280). **That behavior is
  byte-identical for `client_backpressure` and for any new string**, so this
  card needs no Swift change and no Swift suite run. (The client's handling of
  `server.resync_required` is a pre-existing gap — see Boundaries.)

### Limits of the whole thing

- **This bounds SERVER QUEUE DEPTH only. State it, do not exceed it.** The
  client sends **one** ACK for the whole snapshot, not one per page:
  `_apply_ack` (:236-259) requires `(snapshot_id, through_cursor)` to equal the
  awaited pair (:246-248) before setting `_ack_event` (:249), and
  `snapshot.end`'s `content_hash` covers every page's bytes
  (jarvis/surface/inherent_protocol.py:330-337; presenter docstring :8-12 — "the
  bytes the hub sends are the bytes that were hashed"). So the client cannot ACK
  until it holds the entire snapshot plus `snapshot.end`. **The card does not
  make arbitrarily large snapshots adoptable** and promises nothing about
  client-side wait time.
- The handoff still consumes exactly one matching ACK. Progressive delivery
  makes an ACK arriving mid-enqueue newly reachable (`snapshot.begin` already
  carries `snapshot_id` and `through_cursor`, presenter :366-376), and it needs
  no new guard: the durable lane preserves order, so every queued page still
  leaves before any later delta.
- Rule 3 after adoption is unchanged: a durable enqueue that overflows a live
  client still closes it with `client_backpressure` (:304-312), as pinned by the
  two tests at :107-150 and :153-194.

## Affected contracts and files

- runtime `jarvis/runtime/inherent_hub.py:332-354` (`_next_frame`) —
  **ADDITION 1: a capacity-available signal**, beside the byte decrement at
  :343-344. None exists today: `self._wake` (:171) is sender-facing only
  (producers set it, the sender awaits it at :462-463) and `_release_window`
  (:327-330) tracks the unacked *window*, not lane depth.
- runtime `jarvis/runtime/inherent_hub.py:401-440` (`_handoff`) — the enqueue
  loop at :411-412 becomes a bounded await loop. **ADDITION 2 is a ruling, not a
  choice: the whole handoff stays under the ONE existing adoption deadline
  (:81, :538) rather than gaining a second budget of its own.**
- runtime `jarvis/runtime/inherent_hub.py:299-317` (`_enqueue`) — the durable
  lane's only entry: the unfittable-frame check and its distinct reason, plus
  whatever the capacity wait needs (a "would this fit" predicate, or an enqueue
  that reports instead of closing). Its post-adoption close path stays exactly
  as it is.
- runtime `jarvis/runtime/inherent_hub.py:495-504` — the new reason value rides
  the existing `server.resync_required` notice and the existing 1008 close. No
  new frame type, no protocol/DTO change.
- tests `tests/integration/test_inherent_flow_control.py` — three new cases.
  `_big_chunks` (:92-101) may gain response-id parameters; today it hardcodes
  one id triple ("RESPbig"/"RGRPbig"/"Tbig"). That helper change is allowed.
- docs `docs/adr/0014-inherent-realtime-ux.md` D11 (:788 shared queue, :802
  close rule).
- Cited, not edited: `jarvis/runtime/inherent_view_sequencer.py:240-291`;
  `jarvis/surface/inherent_protocol.py:330-345`;
  `jarvis/surface/inherent_presenter.py:290-322`;
  `scripts/smoke_inherent_sequencer.py:425`, the one place server-side that
  enumerates the reason vocabulary (`reason not in {"client_backpressure",
  "ack_stalled"}`) — that smoke drives post-adoption backpressure, not an
  oversized snapshot, so it neither sees nor needs the new value.

## Boundaries and non-goals

- **HARD NON-GOAL, READ THIS BEFORE TOUCHING THE SEQUENCER: `complete_snapshot`'s
  catch-up loop must NOT be made progressive.**
  `jarvis/runtime/inherent_view_sequencer.py:240-291`. Its synchronicity is a
  **correctness invariant**, stated in its own docstring at :243-246:

  > The catch-up is enqueued in full before ``live_frontier`` becomes ``B``,
  > all without yielding, so no drain can interleave a later row ahead of it.

  Any event committed between capturing `high` (:260) and finishing would be
  **dropped forever**: `drain()` skips the lane while `live_frontier is None`
  (:215), and the catch-up read has already fixed its upper bound. A session
  that hears "make delivery progressive" and applies it uniformly will silently
  break durability. This card makes the *hub's snapshot enqueue* progressive and
  nothing else.
- Catch-up is also not a pattern to copy. It is deliberately all-or-nothing: it
  materializes every frame while checking `CATCH_UP_FRAME_BUDGET` /
  `CATCH_UP_BYTE_BUDGET` and raises `CatchUpBudgetExceededError` **before**
  enqueuing anything (:280-286), then enqueues in one loop (:288-289) and sets
  the frontier (:290).
- `test_a_catch_up_over_either_budget_closes_the_client_with_nothing_partial_sent`
  (tests/integration/test_inherent_flow_control.py:400, asserting
  `socket.cursors() == []` at :420) **must pass UNEDITED**. An edit to that test
  is the tell that this non-goal was violated.
- **KNOWN RESIDUAL CEILING, decided rather than overlooked.** A single response
  group whose inline bytes exceed `durable_bytes` still cannot be delivered to
  any client, at all, by this card or by any amount of queue-depth management —
  the frame does not fit the lane. Such a client is now failed fast and
  truthfully instead of slowly and falsely, and that is the whole improvement
  for that shape. The real remedy is the artifact-reference path, and it is
  **currently blocked**: `DocumentReference`
  (jarvis/state/inherent_view.py:213-223) requires `event_uid`, documented at
  :217-218 as naming the `surface.response_emitted` row, which is written only
  at close (fold branch :694-701); an open response has no such row. Lifting the
  ceiling means unblocking that first, in a card that owns L2 — not here.
- Layers that may change: **runtime only** — `jarvis/runtime/inherent_hub.py` —
  plus tests and the ADR.
- Must not change: L2 `jarvis/state/inherent_view.py` content bounds; no cap is
  added to open responses; the presenter's pagination and
  `SNAPSHOT_PAGE_MAX_BYTES` stay as they are.
- **The configured limit defaults do not change.** `FlowControlLimits`
  (jarvis/runtime/inherent_hub.py:98-114) keeps `durable_frames = 256`,
  `durable_bytes = 1_048_576` and every other field; `inherent_flow_control_limits`
  (:614-622) and `config/jarvis.yaml` are untouched.
- **Do not fix the Swift side here.** The client currently turns every
  `server.resync_required` notice into `.socketFailed(reason: "protocol_error")`
  (RealtimeTransport.swift:270-281 via RealtimeViewDTOs.swift:155-157) instead
  of reading the reason. That is a real pre-existing gap, it is identical before
  and after this card, and the server closes the socket immediately afterwards
  anyway. Record it, do not repair it here.
- Non-goals: per-page ACKs or any protocol change; a second queue; a document or
  artifact path for a large open response; changing
  `SNAPSHOT_ADOPTION_DEADLINE_S`; touching `scripts/smoke_inherent_sequencer.py`.

## Rejected approaches

- **Artifact / `DocumentReference` for the oversized open response** — ADR-0014
  D8's own named mechanism (:556) — **is not constructible.**
  `DocumentReference` (jarvis/state/inherent_view.py:213-223) requires
  `event_uid`, documented at :217-218 as naming the `surface.response_emitted`
  row, which is written only at close (fold branch :694-701). An open response
  has no such row, and its `utf8_bytes` would be provisional.
- **Truncating the open response** — rejected by the design itself
  (jarvis/state/inherent_view.py:41-45): "Open responses are never cut: a live
  client extends them by `sequence` and a trimmed prefix would only send it into
  a resync loop."
- **A separate snapshot queue** — changes D11's documented single-queue contract
  (docs/adr/0014-inherent-realtime-ux.md:788) and still dies on a snapshot
  larger than whatever new budget it gets. It moves the wall.
- **Only fixing the reason code** — leaves the client unable to adopt.
- **Letting the capacity wait cover the unfittable frame** — it would wait for a
  level that can never be reached, spend the whole adoption deadline, and close
  with a reason as wrong as today's. Slower and no more honest.

## Acceptance evidence

Three tests. No more — no test per limit field, per budget or per frame kind.

**Every assertion names an observable artifact: the frames the socket actually
received, the close code, the reason string on the emitted
`server.resync_required` notice. Assertions on internal deque state, byte
counters or task state do not count as evidence.**

- **Test 1 — an over-total snapshot with every frame under budget is delivered
  and the client adopts.** Fail-then-pass, both raw outputs shown.
  - Build the oversized view **before attaching**: `rig.emit(
    "surface.response_open", _open_payload(...))` plus `_chunk_payload` chunks —
    the shape `_big_chunks` uses (:92-101) and the byte-limit test inlines at
    :167-176.
  - **Spread it over several response ids** so every page frame stays under
    `durable_bytes` while the total exceeds it — e.g. three open responses of
    ~400 KiB. `_big_chunks` hardcodes one id triple, so either emit inline or
    give it id parameters (allowed).
  - Then call **`_attach()` alone** (:71-77), *without* the ACK that `_adopt()`
    (:80-85) sends; read `end = socket.of_type("snapshot.end")[0]["payload"]`
    and send `_ack(...)` explicitly, exactly as the catch-up test does at
    :413-417. `_Rig` is at tests/integration/test_inherent_sequencer.py:407-491.
  - Observables: `socket.of_type("snapshot.begin")`, every `snapshot.page` the
    plan contains and `socket.of_type("snapshot.end")` all present on the
    socket; `socket.of_type("server.resync_required") == []`;
    `socket.close_state() is None` after the ACK. Today the socket shows the
    notice with `reason == "client_backpressure"` and
    `(1008, "client_backpressure")` before any `snapshot.end` — that is the fail
    arm.
- **Test 2 — a client that does not drain still fails on the adoption deadline;
  the budget still protects.** `_SlowSocket` (:55-69) held via `hold()`, the
  same over-total view, and a shortened deadline:
  `_Rig(tmp_path, adoption_deadline_s=0.2)` — the keyword exists
  (tests/integration/test_inherent_sequencer.py:415, :433) and the precedent is
  test_inherent_sequencer.py:701-716, which asserts `(1008, "resync_required")`
  after `_settle(0.4)`. The injected `_Clock` does **not** drive this: the
  deadline is an `asyncio.wait_for` on real loop time.
  - Observables: `socket.close_state() == (1008, "resync_required")`, and it is
    still `None` before the deadline elapses — no hang, no second budget.
- **Test 3 — a single over-budget frame fails immediately with the distinct
  reason.** One open response above `durable_bytes` (one item, one page of its
  own), a normal `_Socket`, and the **default 5 s** adoption deadline.
  - Observables: right after `await _attach(...)` returns — one `_settle()`,
    50 ms, two orders of magnitude below the deadline — the socket already shows
    `server.resync_required` with the new reason and
    `close_state() == (1008, "<new reason>")`, and no `snapshot.end`. Asserting
    the close is already visible under the untouched 5 s deadline *is* the proof
    that it did not wait for it.
- Regression: `PYTHONPATH=. .venv/bin/python -m pytest -q
  tests/integration/test_inherent_flow_control.py
  tests/integration/test_inherent_sequencer.py` raw output ends in a pass line,
  and the transcript states that the catch-up test at :400 and the two
  durable-overflow tests at :107-150 and :153-194 are **unedited**.
- Regression: `PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and
  not live_codex"` — capture the pre-change baseline count from a pre-change run
  and state the final count as that baseline plus exactly 3.
- Gates: `PYTHONPATH=. .venv/bin/lint-imports`,
  `PYTHONPATH=. .venv/bin/ruff check .` and
  `PYTHONPATH=. .venv/bin/mypy --strict jarvis tests scripts tools`, each
  exiting 0 with its printed counts. `PYTHONPATH=.` is mandatory: without it the
  editable install resolves to the main checkout and a provenance test fails
  spuriously against the wrong tree.
- Swift: **no Swift change and no Swift suite run** — the reason string never
  reaches a Swift switch (evidence under Target behavior). State this
  explicitly rather than silently skipping it.
- **Live run: not required** — the rig drives the real hub, real sequencer and
  real view state hermetically over a real Event Log; nothing here depends on an
  LLM or an external runtime. No audio device is touched; do not switch the
  system default output.

## Docs to sync

- `docs/adr/0014-inherent-realtime-ux.md` D11 rule 3 (:802), with the
  shared-queue bullet at :788 as its context — **amend in place**, covering
  three facts and nothing more:
  1. the close rule no longer fires during snapshot handoff merely because the
     plan is large; the handoff waits for lane capacity inside the existing
     adoption deadline, and rule 3 keeps its full force after adoption;
  2. a frame larger than the whole lane budget fails immediately with its own
     reason value on the same `server.resync_required` notice;
  3. **the residual ceiling**: a single response group whose inline bytes exceed
     `durable_bytes` still cannot be delivered, and the artifact-reference
     remedy is blocked because `DocumentReference.event_uid` names the
     close-time `surface.response_emitted` row that an open response does not
     have. Write it as a known, decided ceiling with its blocker.

  This is a real contract change, not a note. **CHECK FOR AN ERRATA SECTION
  FIRST — do not assume the structure.** A draft-time scan of the `## ` headings
  found sections 1-18 with no errata heading (§16 "Spec changes and explicit
  deviations" at :2096, file 2167 lines); verify that still holds before
  choosing where to write.
- `docs/adr/0014-inherent-realtime-ux.md` — record the **asymmetry** in one
  sentence: snapshot delivery is progressive, catch-up (`complete_snapshot`)
  stays atomic and must not be made progressive, because the frontier is set
  only after the last catch-up frame is enqueued. It is load-bearing now, and a
  future reader will otherwise "fix" the inconsistency and break durability.
  Put it in **one** place — the D11 rule 3 amendment or D8 step 6 (§5,
  :536-607) — not both.
- `docs/spec.html` — expected **unchanged**; say so in one line. The spec has no
  concept of this queue: a draft-time grep for `durable_bytes`,
  `client_backpressure`, `durable lane` and `snapshot handoff` returned 0
  matches. Confirm and state it.

## Open questions

## /goal condition

Implement docs/goals/snapshot-handoff-backpressure.md on the current branch.
Read it fully before touching code. Done when the transcript shows all of the
following as raw command output, not summaries. Every test assertion must name
an observable artifact — a frame the socket received, a close code, a reason
string — never internal queue or counter state. (1) Test 1, both arms: raw
pytest output of the over-total snapshot case FAILING on the tree as it stands,
with the socket showing server.resync_required reason client_backpressure and
close (1008, client_backpressure) before any snapshot.end; then raw output of it
PASSING, with snapshot.begin, every page and snapshot.end on the socket, no
resync notice, and no close after one ack. (2) Test 2: raw pytest output showing
a client whose socket never drains is still closed (1008, resync_required) at a
shortened adoption deadline, and was not closed before it — no hang, no second
budget. (3) Test 3: raw pytest output showing a single page frame larger than
durable_bytes closes immediately, under the untouched 5-second deadline, with a
DISTINCT reason on the existing server.resync_required frame and a matching 1008
close — not client_backpressure, and no new frame type or DTO. (4) Raw output of
tests/integration/test_inherent_flow_control.py and
tests/integration/test_inherent_sequencer.py ending in a pass line, plus an
explicit statement that
test_a_catch_up_over_either_budget_closes_the_client_with_nothing_partial_sent
and the two durable-overflow cases were NOT edited, and that complete_snapshot's
catch-up loop in jarvis/runtime/inherent_view_sequencer.py is untouched and
stays atomic. (5) Raw output of `PYTHONPATH=. .venv/bin/python -m pytest -q -m
"not live_llm and not live_codex"` with its count, together with the pre-change
baseline run, the final count stated as that baseline plus exactly 3. (6) Raw
output of `PYTHONPATH=. .venv/bin/lint-imports`, `PYTHONPATH=. .venv/bin/ruff
check .` and `PYTHONPATH=. .venv/bin/mypy --strict jarvis tests scripts tools`,
each exiting 0 with its printed counts. (7) A stated boundary check: the diff
touches only jarvis/runtime/inherent_hub.py, tests and docs — no change to
jarvis/state/inherent_view.py, jarvis/surface/inherent_presenter.py,
jarvis/surface/inherent_protocol.py, scripts/smoke_inherent_sequencer.py,
SNAPSHOT_PAGE_MAX_BYTES, SNAPSHOT_ADOPTION_DEADLINE_S, the FlowControlLimits
defaults or config/jarvis.yaml; and no Swift change, with the card's stated
reason (nothing in desktop/inherent-swift reads the resync reason string).
(8) An explicit "live run not required" line with its reason: the rig drives the
real hub, sequencer and view state hermetically; no LLM, no external runtime, no
audio device touched, system default output not switched. (9) Each entry under
Docs to sync updated or explicitly judged unchanged with a one-line reason — the
ADR-0014 D11 amendment carrying all three of its facts including the residual
ceiling and its DocumentReference blocker, the progressive-snapshot /
atomic-catch-up asymmetry recorded in exactly one place, and docs/spec.html —
following the rule: when the implementation changes a documented contract,
invariant, ownership boundary, or externally relevant behavior, update the
canonical document that owns that fact; do not document what the code already
makes clear; do not duplicate a fact across documents. (10) `git status` showing
a clean tree, every slice committed with the project commit skill, and one
Progress line per slice appended to the card. If the card contradicts the
repository, stop and report; do not redesign. Or stop after 45 turns.

## Progress
- Progressive snapshot delivery + the over-budget frame reason + 3 tests — efa5deb —
  the over-total case fails on the pre-change tree with
  `(['client_backpressure'], (1008, 'client_backpressure'), 0)` and passes after;
  3 new cases pass, flow_control + sequencer 30 passed, full suite 1072 passed
  (baseline 1069 + 3), lint-imports KEPT 1/1, ruff clean, mypy strict 244 files.
- ADR-0014 D11 rule 3 amendment — 1869c5f — rule 3 now carries the handoff's
  capacity wait inside the existing adoption deadline, the `frame_over_budget`
  close, the residual single-group ceiling with its `DocumentReference` blocker,
  and the progressive-snapshot / atomic-catch-up asymmetry in that one place;
  `docs/spec.html` unchanged (grep for `durable_bytes|client_backpressure|durable
  lane|snapshot handoff|frame_over_budget` returns 0).
