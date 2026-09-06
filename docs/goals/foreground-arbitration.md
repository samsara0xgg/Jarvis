# Goal: foreground-arbitration

## Goal
A cross-group response may take the foreground speech lane only when an explicit L3 policy says it wins; otherwise it declines instead of silently displacing the incumbent or queueing behind it.

## Why
ADR-0008 D8 gives the coordinator the policy ("which response may occupy the lane") and ADR-0006 gives the playback actor the mechanism (generation ids, lease activation, the drain lane). L5 currently owns both: `_schedule_response` displaces any cross-group incumbent with no policy check of any kind, and enqueues a cross-group candidate into `_after_drain` when the incumbent is tombstoned. Both contradict a written ADR sentence.

## Current behavior
All four branches of `_schedule_response` (jarvis/surface/voice_media.py:1977-2036), pinned at `realtime-integration` cecff8f:

1. **Lane free** — `active is None` -> `_start_response(response)` (jarvis/surface/voice_media.py:1988-1990). Correct, unchanged by this card.
2. **Incumbent tombstoned** — `active.terminal_commit_pending` -> capacity check, then `self._after_drain.append(response)` **regardless of `response_group_id`** (jarvis/surface/voice_media.py:1991-2007). No group comparison happens on this path at all; the group check at :2008 is only reached when this branch does not return.
3. **Same group** — `active.response.response_group_id == response.response_group_id` -> capacity check (overflow terminalizes at :2009-2018), else `self._after_drain.append(response)` (jarvis/surface/voice_media.py:2008-2027). Correct, unchanged by this card.
4. **Different group** — falls through to `await self._interrupt_active(reason="foreground_superseded")` unconditionally (jarvis/surface/voice_media.py:2028). On success the whole drain lane is purged (:2032-2035) and the candidate starts (:2036). On failure the candidate is terminalized and popped (:2028-2031).

Contradiction A — branch 4 vs ADR-0008 D8. docs/adr/0008-real-time-response-streaming.md:451 says verbatim:

> Otherwise it remains pending until explicit foreground policy selects it or degrades to document/silent delivery; it cannot displace another group.

Branch 4 has no policy of any kind: any cross-group candidate displaces the incumbent by arriving.

Contradiction B — branch 2 vs ADR-0006. docs/adr/0006-full-duplex-voice-session.md:261 says verbatim:

> `enqueue_after_drain` accepts only a response in the same `response_group_id`, is idempotent by `(response_id, phase)`, and does not mint a playback generation until the current response reaches a playback terminal.

Branch 2 enqueues a different-group candidate into `_after_drain` whenever the incumbent's terminal commit is still pending.

Supporting facts:
- `_interrupt_active` (jarvis/surface/voice_media.py:2721-2752) takes only `reason: str` and returns `bool`. It is a mechanism; it cannot enforce a policy itself.
- `_ResponseBuffer` (jarvis/surface/voice_media.py:294-310) already carries `row_id: int` (:296), `response_id`, `response_group_id`, `turn_id`, `phase`, `channel`, `gate_mode`. `row_id` is the event log's own monotonic row id, set at construction (jarvis/surface/voice_media.py:1875-1876) from `_hydrate_event_row` (:81, called at :1820) — commit order into the log.
- `turn_id` is `"T" + uuid.uuid4().hex[:8]` (jarvis/state/input_submission_inbox.py:180-182): random, not orderable.
- `stable_response_group_id` is a UUIDv5 over the turn id (jarvis/shared/realtime.py:275-284): one-way, and nothing recovers a turn from a group.
- Declining the lane today already has a code path: terminalize + pop, no playback opened, audio dropped (jarvis/surface/voice_media.py:2028-2031, the `_interrupt_active` failure branch).
- `.importlinter:20-30` is a `layers` contract; `decision` and `surface` are same-level siblings (:26) and may not import each other.

## Target behavior
- A pure L3 function `decide_foreground` implements exactly this table, with no clock and no IO:

      lane free                                                 -> activate
      same response_group_id                                    -> enqueue_after_drain
      different group AND candidate.row_id > incumbent.row_id    -> supersede
      different group otherwise                                 -> decline

- Ordering is by `_ResponseBuffer.row_id` — the event log's monotonic commit order, already on the buffer at the displacement site. The question at the lane is "which response was admitted to the log later"; `row_id` answers exactly that.
- Both cross-group entry points (branch 2 and branch 4 above) route through that verdict. A different-group candidate must never enter `_after_drain`: it takes the lane or it declines. The same-group behavior at branch 2 is unchanged; the lane chooses how to express the cross-group verdict there.
- `decline` means the decline path that already exists (jarvis/surface/voice_media.py:2028-2031): `self._registry.terminalize(response.response_id)` plus pop from `self._responses`. No terminal event beyond that, no audio produced. This ships the **refusal** half of D8:451 and not its "degrades to document/silent delivery" half.
- Response *text* is not voice_media-owned (`surface.response_open` / `surface.response_chunk` have their own consumers), so declining the lane silences audio only.
- Ordinary answers are not at risk: the answer to the user's newest utterance always carries the highest `row_id` among live candidates, so it always supersedes. `decline` can only ever hit a straggler from an earlier turn arriving late — which today cuts off the newer answer. This strictly improves "ordinary answers get spoken".
- With the callable absent (`None`), behavior is byte-for-byte today's.

## Affected contracts and files
Three seam sites, mirroring `cancel_response_callable` exactly — one site each. That precedent is the shape to copy: its docstring at jarvis/surface/inherent_server.py:357-367 states that "The injected-callable shape is what keeps `jarvis/surface` free of any `jarvis.decision` import", and the field is declared `Callable[[str, str, str], str] | None = None` at jarvis/surface/inherent_server.py:377 (on `InherentDeps`, jarvis/surface/inherent_server.py:310).

1. **L3 pure function** — `jarvis/decision/response_run.py`, new `decide_foreground` next to `request_response_cancel` (:730, the precedent's own pure function). Signature `Callable[[str, int, str, int], str]`: `(incumbent_group, incumbent_row_id, candidate_group, candidate_row_id) -> outcome`. Scalars only. Export it from `__all__` (:820-844). That module already owns `ResponseRun`, `ResponseRunRegistry`, group ids (`start_response_run` derives the group at :679) and the cancel entry point.
2. **Runtime builder** — `jarvis/runtime/__init__.py`, a `make_*_callable` next to `make_response_cancel_callable` (:1720-1782), re-exported the way `request_response_cancel` is imported at :93.
3. **L5 construction site** — a new keyword parameter on `StreamingTTSPipeline.__init__` (jarvis/surface/voice_media.py:582, ctor signature :585-596), typed `| None = None`, consumed at the displacement point inside `_schedule_response`. There is exactly ONE production construction of `StreamingTTSPipeline`:

   **RE-PIN AT LAUNCH** — `jarvis/runtime/inherent_loop.py:1855-1863` (the `voice_media.StreamingTTSPipeline(...)` call inside the streaming-capable branch). Every other construction in the tree is a test fixture.

Wiring reference, all of which move by hundreds of lines between cards:

- **RE-PIN AT LAUNCH** — `jarvis/runtime/inherent_loop.py:3820-3824`: `cancel_response_callable = make_response_cancel_callable(runtime) if ... else None`, the builder-call precedent.
- **RE-PIN AT LAUNCH** — `jarvis/runtime/inherent_loop.py:3842-3868`: the single `InherentDeps(...)` construction, with `cancel_response_callable=cancel_response_callable` at :3851, the injection precedent.
- **RE-PIN AT LAUNCH** — `jarvis/runtime/inherent_loop.py:1549`: A6 lifecycle commentary opens its run through `start_response_run(...)`, which derives the group from the turn (jarvis/decision/response_run.py:679) — which is why commentary lands on the same-group branch and never produces a cross-group input.

Find every `jarvis/runtime/inherent_loop.py` site by grep before editing; do not trust the numbers above.

Contract gate: `.importlinter:20-30`. `decision` and `surface` are siblings (:26); L5 may not import L3, which is the entire reason for the injected-callable seam.

## Boundaries and non-goals
- Layers that may change: L3 (`jarvis/decision/response_run.py`), runtime (`jarvis/runtime/__init__.py`, `jarvis/runtime/inherent_loop.py`), L5 (`jarvis/surface/voice_media.py`), tests, docs.
- Must not change: `_interrupt_active`'s signature or mechanism; the lane-free branch (:1988-1990); the same-group branch's capacity check and enqueue (:2008-2027); the drain-lane purge on a successful supersede (:2032-2035); `_start_response`'s suspend/isolate/shutdown guards (:2038-2046); playback generation minting.
- **No new `jarvis/decision/foreground.py`.** The policy lives in the existing `jarvis/decision/response_run.py`. A new module buys nothing.
- **The callable being `None` keeps today's behavior**, mirroring `cancel_response_callable: ... | None = None`. Every existing fixture construction of `StreamingTTSPipeline` must keep working with no edit.
- Non-goals, all pending C7's `foreground_output` lease: a `pending` outcome; any re-evaluate-on-drain wake-up; any priority model; the "degrades to document/silent delivery" half of D8:451. No silent/document delivery path exists in `voice_media` today and this card does not add one.
- Non-goal: a `phase`/"not a primary response" row in the table. A6 commentary reuses the turn's `response_group_id`, so it lands on the same-group branch; that row has no reachable cross-group input. Do not ship a rule nothing can trigger.
- **Hard constraint:** `tests/integration/test_wave2_streaming_media.py:1686` `test_after_drain_same_group_and_foreground_supersede` **MUST PASS UNEDITED**. Under the `row_id` rule it does: RS (:1736-1743) is submitted strictly after RN (:1728-1735), so all five assertions (:1726, :1750, :1751, :1752, :1753) hold. Its `G1`/`G2`/`G3` and `TC`/`TFN`/`TN`/`TS` are hand-made string literals, never run through `stable_response_group_id`. If that test fails, this card's ordering rule is wrong: **stop and report. Do not edit the test and do not redesign in-lane.**

## Rejected approaches
- **Order by `turn_id`** ("the candidate is the primary response of a strictly newer turn") — unimplementable. `turn_id` is `"T" + uuid.uuid4().hex[:8]` (jarvis/state/input_submission_inbox.py:180-182), a random prefix with no order.
- **Order by `response_group_id`** — `stable_response_group_id` is a UUIDv5 hash (jarvis/shared/realtime.py:275-284): one-way, unordered, and nothing recovers a turn from a group. A turn-ordered rule would also have made the :1686 fixture's outcome undefined, since its group and turn ids are hand-made literals.
- **Amending `test_after_drain_same_group_and_foreground_supersede`** — not needed under the `row_id` rule and not authorized. It passing unedited is the evidence that the rule is right.
- **A `phase`/commentary row in the table** — unreachable. Commentary reuses the turn's group (`start_response_run`, jarvis/decision/response_run.py:679), so it is always a same-group input.
- **A frozen input record for the callable** — one call site, and L5 cannot import L3 to construct one anyway. Pass four scalars.
- **Building a document/silent delivery path** — no such path exists in `voice_media`, and the routing needs C7's `foreground_output` lease. `decline` reuses the existing terminalize-and-pop.
- **A new L3 module `jarvis/decision/foreground.py`** — `jarvis/decision/response_run.py` already owns `ResponseRun`, the registry, group derivation and `request_response_cancel`. A second module for one pure function is pure ceremony.

## Acceptance evidence
- Positive 1 (hermetic integration, `tests/integration/`): a cross-group candidate whose `row_id` is LOWER than the incumbent's declines instead of superseding — the incumbent reaches `surface.playback_completed` and the candidate never opens playback.
- Positive 2 (hermetic integration): a different-group candidate never enters `_after_drain` when the incumbent is tombstoned (`active.terminal_commit_pending` is set).
- Positive 3 (optional): a direct table test of `decide_foreground` ONLY if it costs one test, not four. Allen's standing rule holds: no test per enum member, per branch, or per field.
- Regression: `PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and not live_codex"` — branch baseline is 1065 passed / 64 deselected; no new failure.
- Regression: `PYTHONPATH=. .venv/bin/python -m pytest -q tests/integration/test_wave2_streaming_media.py::test_after_drain_same_group_and_foreground_supersede` passes with the test file's diff showing that test unedited.
- Gate: `PYTHONPATH=. .venv/bin/lint-imports` exits 0 (the L5/L3 boundary is the whole point of the seam).
- Gate: `PYTHONPATH=. .venv/bin/ruff check .` exits 0.
- Gate: `PYTHONPATH=. .venv/bin/mypy --strict jarvis tests scripts tools` exits 0.
- Live run: **not required** — the whole card is in-process scheduling arbitration. A live daemon adds no evidence a hermetic pipeline test does not already produce, and no audio device is touched, so the output-routing hazard does not arise.

## Docs to sync
- `docs/adr/0008-real-time-response-streaming.md:427` — the sentence "`supersede_foreground`, `interrupt_response`, `request_action_cancel`, `handle_action_terminal`, and `shutdown` are unbuilt." **Conditional amendment:** amend that line only if the shipped code actually introduces methods by those names. If it does not (the expected case: this card ships an arbitration *decision*, not those methods), leave :427 alone and add a short note that foreground arbitration policy is now built and where it lives. ADR-0008 has NO errata section, so amend in place — the ADR-0006 precedent, commit 7a190bb.
- `docs/adr/0006-full-duplex-voice-session.md:261` — **not amended.** This card makes that sentence true rather than changing it. Say so explicitly.
- `docs/spec.html` — **judge explicitly.** Either name the section updated, or state which section was read and why it needs no change. Do not duplicate a fact ADR-0008 already owns.

## Open questions
(none)

## /goal condition
Implement docs/goals/foreground-arbitration.md on the current branch. Read the card fully before touching code. The goal is met when all of the following appear in the transcript:

(1) the diff shows a pure `decide_foreground` in jarvis/decision/response_run.py (NOT a new jarvis/decision/foreground.py), a builder in jarvis/runtime/__init__.py next to `make_response_cancel_callable`, a new `| None = None` keyword on `StreamingTTSPipeline.__init__` in jarvis/surface/voice_media.py wired at the single production construction site in jarvis/runtime/inherent_loop.py, and no `jarvis.decision` import anywhere under jarvis/surface;

(2) the diff shows BOTH cross-group paths in `_schedule_response` routed through the verdict — the tombstoned-incumbent branch no longer appends a different-group candidate to `_after_drain`, and the different-group branch no longer calls `_interrupt_active` unconditionally;

(3) the raw output of a pytest run over the new integration tests, ending in a pass line, covering: a cross-group candidate with a lower `row_id` declines (incumbent reaches `surface.playback_completed`, candidate never opens playback), and a different-group candidate never enters `_after_drain` when the incumbent is tombstoned;

(4) the raw output of `PYTHONPATH=. .venv/bin/python -m pytest -q tests/integration/test_wave2_streaming_media.py::test_after_drain_same_group_and_foreground_supersede` showing 1 passed, together with evidence that this test is UNEDITED (its diff is empty). If it fails, stop and report — do not edit the test, do not redesign;

(5) the raw output of the full hermetic suite `PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and not live_codex"` with its printed passed/deselected counts quoted and no new failure versus the 1065 passed / 64 deselected baseline;

(6) the raw output of `PYTHONPATH=. .venv/bin/lint-imports`, `PYTHONPATH=. .venv/bin/ruff check .`, and `PYTHONPATH=. .venv/bin/mypy --strict jarvis tests scripts tools`, each shown in full and each exiting 0;

(7) no live run is required or claimed;

(8) every Docs-to-sync entry is updated or explicitly judged unchanged, with the judgement stated in the transcript: docs/adr/0008-real-time-response-streaming.md:427 amended only if the shipped code introduces `supersede_foreground` or `interrupt_response` by name, otherwise left alone with a short note added stating that foreground arbitration policy is built and where; docs/adr/0006-full-duplex-voice-session.md:261 explicitly judged unchanged (the card makes it true); docs/spec.html explicitly judged — name the section updated or the section read and why it needs none. Follow the rule: when the change alters a documented contract, invariant, ownership boundary, or externally relevant behavior, update the canonical document that owns that fact; do not document what the code already makes clear; do not duplicate a fact across documents;

(9) each slice committed with the project commit skill, `git status` shown clean, and a Progress line appended to the card per slice.

Or stop after 40 turns.

## Progress
- Arbitration slice — 8a93c65 — `decide_foreground` (L3) + `make_foreground_decision_callable`
  (runtime) + `foreground_decision_callable` on `StreamingTTSPipeline`, wired at
  `jarvis/runtime/inherent_loop.py:1863`. Both cross-group paths of
  `_schedule_response` route through the verdict. New
  `tests/integration/test_foreground_arbitration.py` 3 passed; the two behavioral
  cases fail with the callable set to `None` (`('ROLD', 0) in provider.opened`,
  `('RNEXT', 0) in provider.opened`). Suite 1068 passed / 64 deselected (baseline
  1065 + 3 new); lint-imports KEPT 1/1; ruff clean; mypy strict clean (244 files).
  `test_after_drain_same_group_and_foreground_supersede` 1 passed, its file diff
  against `realtime-integration` is empty.
