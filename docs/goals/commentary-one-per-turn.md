# Goal: commentary-one-per-turn

## Goal
A user-originated turn speaks at most one lifecycle commentary phrase, and the
phrase it speaks is drawn from a small per-row-type set selected by a stable
digest of the action id.

## Why
Measured on the owner's own ledger (`~/.jarvis-allen-test/mac_events.db`, read
2026-09-05):

    turn        commentary surface.playback_started rows   distinct speech_text_hash
    Tceebc265   4                                          2
    T6300d44f   3                                          2

and in a second root (`~/.jarvis-allen-test/scratch-root/mac_events.db`)
`Te9b6b2fe` shows 3 rows with 1 distinct hash — the identical sentence three
times. In `Tceebc265` the turn dispatched three actions
(`Aba3db87a`, `A560a312b`, `A2e07c403`), each emitting
`action.dispatched` / `action.running` / `action.result_observed`; seven
commentary responses were emitted and four reached the speaker, three of them
carrying `结果回来了，我整理一下。`. Supersession is keyed per action, so three
actions produce three copies of the same sentence and nothing can suppress
them.

## Current behavior
- `_commentary_watcher` runs one durable cursor over the four D6 action types
  plus `surface.playback_started`, and opens one commentary run per qualifying
  row (`jarvis/runtime/inherent_loop.py:1679-1749`; the four types at
  `:1286-1291`).
- The only delivery-side dedup is `spoken: set[tuple[str, str]]`
  (`inherent_loop.py:1703`, enforced `:1719`, added `:1721`), documented as
  "one entry per (action_id, event type)" at `:1695-1696`. There is no rate
  limit, minimum interval, cooldown, debounce or throttle in that file — a
  case-insensitive grep for `rate_limit|ratelimit|min_interval|cooldown|debounce|throttle|last_spoken|since_last|quiet_period`
  returns 0 hits.
- Supersession is per action: `previous=open_by_action.get(action_id)`
  (`:1727`), `open_by_action[action_id] = opened` (`:1729`),
  `_retire_superseded_commentary` (`:1475`) which stops once
  `_commentary_reached_the_speaker` (`:1459`, a query for a
  `surface.playback_started` row with that `response_id`) is true. Phrases from
  different actions never see each other.
- `jarvis/decision/commentary.py` is pure: `commentary_intent_for` (`:53`)
  looks `event.type` up in `_D6_ROWS` (`:40-45`, four rows mapped verbatim to
  one fixed phrase each), returns `None` otherwise, and sets
  `subject_ref = action_id` (`:71`). Module docstring `:9-19`: "deliberately
  total and side-effect free — no clock, no DB read, no LLM, no timer" and
  "Whether an intent is *delivered* — origin, confirmation, coalescing — is the
  runtime observer's business."
- The turn is already known at the open seam:
  `turn_id = _commentary_turn_id(conn, action_event)` at
  `inherent_loop.py:1645`, inside `_open_commentary_in_worker_thread`
  (`:1622-1677`), which already opens its own connection for a durable read.
- `surface.playback_started` requires `turn_id`, `phase` and
  `speech_text_hash` (`jarvis/state/event_log.py:806-819`);
  `response.started` requires `turn_id` and `phase` (`:747-761`);
  `surface.response_emitted` requires `turn_id` and carries optional `phase`
  (`:920-935`).
- Empirically, `action.dispatched` DOES open the first run (the 10 ms cursor
  beats the terminal), and its phrase `我开始处理了。` is then cancelled as
  `superseded` by the same action's later rows before it reaches the speaker.
  The audible phrase today is therefore the LAST row of each action, not the
  first.
- `config/jarvis.yaml:190` claims the final answer "is never delayed, dropped
  or superseded by it" (verified present at tip 99f306e).

## Target behavior
- For any single `turn_id`, the number of commentary responses the runtime
  observer opens is at most one. The first row of that turn that actually
  produces a phrase wins; every later qualifying row in that turn is silent.
- Because the first opened phrase is no longer superseded by its own action's
  later rows, the phrase that reaches the speaker becomes the acknowledge
  phrase — an announcement that work is starting, the shape OpenAI's Realtime
  API calls a tool-call preamble (one short sentence spoken before the tool
  runs), rather than a report of work already observed.
- The cap is recorded only when a phrase is actually opened. A row suppressed
  for any existing reason (turn not user-originated, live pending confirmation,
  non-terminal row whose action already terminalized) does not consume the
  turn's single slot, so a turn whose first row is suppressed can still speak
  on a later row.
- `jarvis/decision/commentary.py` stays pure: no clock, no DB read, no LLM, no
  timer, no stored index, no randomness. The cap lives in the runtime observer.
- Each D6 row type maps to a small set of phrasings; the one used is a
  deterministic function of `action_id` via a stable digest (`hashlib.sha256`,
  never builtin `hash()`, which is per-process randomized by `PYTHONHASHSEED`).
- Phrasings follow OpenAI's preamble style guidance: one short sentence,
  varied, describing the action rather than internal reasoning, and never of
  the "I am now going to access the tool…" / "One moment while I process
  that…" shape. `我开始处理了。` is literally that shape and goes.

## Affected contracts and files
- L3 `jarvis/decision/commentary.py:_D6_ROWS` — one phrase per row becomes a
  tuple of phrasings; `commentary_intent_for` selects by stable digest of
  `action_id`. Purity unchanged; the only new import is `hashlib`.
- L3 `jarvis/decision/commentary.py:commentary_speech_text` — unchanged; the
  `<voice>` tag convention stays.
- runtime `jarvis/runtime/inherent_loop.py:_open_commentary_in_worker_thread`
  — after `turn_id` resolves at `:1645-1647` and BEFORE
  `rebuild_projections` at `:1648`, return `None` when this turn already has a
  commentary run. Implement as a durable read mirroring
  `_SELECT_COMMENTARY_PLAYBACK_SQL` (`:1454`): one indexed-by-type query for a
  `response.started` row whose payload has `phase='commentary'` and this
  `turn_id`. `start_response_run` writes that row first inside
  `_render_commentary` (`:1528`), so it is the earliest durable mark of "this
  turn already spoke".
- runtime `jarvis/runtime/inherent_loop.py:_commentary_watcher` docstring
  (`:1686-1698`) — "Coalescing is one entry per (action_id, event type)" is no
  longer the rule that decides audibility; restate it.
- What the two existing mechanisms still do under the cap:
  - `spoken` (per `(action_id, event type)`) still avoids repeating work: a
    duplicate row on a turn that never opened anything (system turn, live
    confirmation slot, stale non-terminal) is dropped before the thread hop and
    the projection rebuild. It no longer decides whether anything is heard.
  - Per-action supersession becomes unreachable for user-originated turns: an
    action belongs to exactly one turn, so a second commentary for the same
    action can only open inside a turn that is already capped, and `previous`
    is therefore always `None`. It is retained, not deleted; the implementation
    must not claim it is exercised.
- tests `tests/integration/test_lifecycle_commentary.py` — three existing
  tests assert the behavior the cap removes and must be reshaped, not deleted
  wholesale: `test_repeats_are_dropped_and_an_unheard_commentary_is_superseded`
  (`:593`, expects 2 phrases in one turn),
  `test_a_commentary_that_reached_the_speaker_finishes` (`:627`, expects a
  second `response.started` in the same turn), and
  `test_a_playing_commentary_is_completed_not_cut_off` (`:1038`, drives
  `_open_commentary_in_worker_thread` twice by hand with `previous=first`).
  `test_four_d6_rows_map_to_their_exact_intent_and_phrase` (`:96`) pins the old
  literal strings and must assert variant-set membership instead.
- config `config/jarvis.yaml:190` — the comment's "never delayed" claim.

## Boundaries and non-goals
- Layers that may change: L3 (`jarvis/decision/commentary.py`), runtime
  (`jarvis/runtime/inherent_loop.py`), plus config comment, tests and docs.
- Must not change: `commentary.py`'s purity (no clock, no DB read, no LLM, no
  timer, no module-level mutable state); the origin filter
  (`_commentary_turn_id`, `_COMMENTARY_ORIGIN_TRIGGER_TYPES`); the pending
  confirmation suppression; the non-terminal freshness check at `:1653-1657`;
  the commentary→final same-group enqueue-after-drain behavior; the attention
  channel; `desktop/` and the Swift suite.
- Non-goals: naming the specific tool in the phrase (the `action.dispatched`
  payload carries only `action_id` and optional `result_expected_by_ms`, so a
  tool-specific sentence is not derivable without a DB read, which would break
  purity); any per-action or per-turn budget larger than one; changing which
  four rows are D6 rows.

## Rejected approaches
- Short-window coalescing / minimum interval / debounce in the observer — a
  window suppresses a symptom on a schedule and leaves the second phrase
  possible whenever the schedule allows it; the per-turn cap removes the
  possibility. Explicitly retired by the owner's decision ("选项B 一句就够").
- Holding window state on `PresentationIntent` — spec §3.6.3 makes it an
  L3→L5 message contract that is never an Event Log row, so window state on it
  cannot survive a restart.
- A clock, cooldown or timer inside `commentary.py` — ADR-0008 D6 forbids
  three specific hallucinations ("马上好" with no evidence, "已经查到了" before
  `action.result_observed`, timer-based fake progress); purity makes them
  unreachable by construction rather than merely discouraged.
- Keying the cap on emitted `surface.playback_started` rows — playback starts
  after the open, so two actions can both open before either plays; the check
  would race exactly the way the bug does.
- Builtin `hash()` for variant selection — randomized per process by
  `PYTHONHASHSEED`, so the same action would say different things on different
  daemon starts and no test could pin it.
- Exempting the turn's first row from the non-terminal freshness check so the
  acknowledge always wins — that check exists so a fast action does not get an
  "I'm starting" phrase after it already finished; keeping it means the fast
  inline case speaks the result phrase once, which is correct.

## Acceptance evidence
- Positive (slice 1, the primary observable): a new test in
  `tests/integration/test_lifecycle_commentary.py` drives the real
  `_commentary_watcher` through the existing `_Observer` harness over a real
  event log: one user-originated turn, three distinct actions, each emitting
  `action.dispatched` → `action.running` → `action.result_observed`. It counts
  the rows the observer emitted — `surface.response_emitted` payloads with
  `phase == "commentary"` and this turn's `turn_id` — and asserts the count is
  1. It must not assert on `spoken`, `open_by_action`, or any in-memory state.
  It must assert only the count, not the phrase text, so its control failure is
  unambiguous.
  (`surface.playback_started` is the live observable; the hermetic harness has
  no TTS actor and never emits it — existing tests write it by hand — so
  counting it hermetically would count the test's own writes.)
- Control, run and pasted raw: with the cap removed
  (`git stash push -- jarvis/runtime/inherent_loop.py`, run, `git stash pop`),
  the same test fails showing a count greater than 1.
- Positive (slice 2): a test driving the same real watcher over six separate
  user turns with six distinct `action_id`s asserts that the emitted commentary
  `voice_text` values are all members of the declared acknowledge variant set
  AND that at least two distinct values appear. The selection is sha256-based
  and therefore deterministic across processes and platforms — this is not a
  coin flip, it passes always or fails always; if the chosen ids collide the
  fixture ids are changed, and the docstring must admit that the id set was
  chosen. Plus a source canary over `jarvis/decision/commentary.py` asserting
  it imports none of `time`, `random`, `secrets`, `datetime`, `os`, calls no
  builtin `hash(`, and defines no module-level mutable state.
- Regression: `PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and
  not live_codex"` — the lane confirms the baseline first; the number reported
  at tip 99f306e was 1046 passed / 64 deselected, to be confirmed, not assumed.
  Run it in a shell WITHOUT `MINIMAX_API_KEY` exported: with that key present
  `tests/integration/test_wave2_streaming_media.py::test_streaming_rollout_default_off_and_production_builder_gate`
  fails as `assert <TTSPipeline object> is None` (known flake).
- Gates: `PYTHONPATH=. lint-imports`, `PYTHONPATH=. ruff check .`,
  `PYTHONPATH=. mypy --strict` (the project's invocation), each pass line or
  exit 0 shown raw. `desktop/` is untouched; the Swift suite is not run.
- Live run: REQUIRED. The owner's bug was measured on
  `surface.playback_started`, a row no hermetic test produces. Start a daemon
  from this worktree on its own runtime root and port, with
  `realtime.enabled`, `response.response_run_lifecycle` and
  `commentary.enabled` true, after `set -a; source ~/.jarvis/env; set +a` (the
  file quotes the value and the loader does not interpret quotes, so a daemon
  fed by the file alone falls back to macOS `say` and terminals report both
  sample counts as 0 — that proves nothing). Ask one question that dispatches
  at least two actions. Canary, the exact query that produced the "before"
  table above, run against the new root:
  `SELECT json_extract(payload_json,'$.turn_id') t, COUNT(*) n,
  COUNT(DISTINCT json_extract(payload_json,'$.speech_text_hash')) d FROM events
  WHERE type='surface.playback_started' AND
  json_extract(payload_json,'$.phase')='commentary' GROUP BY t;`
  The new turn's row must read `<turn_id>|1|1`, and the emitted phrase must be
  one of the new variants.

## Docs to sync
- `docs/spec.html` §3.6.12 "Backpressure and coalescing" (`:1163-1172`) says:
  "Coalescing 分两层：Layer 3: attention candidate coalescing。多个相似
  heartbeat / report / sensor event 在短窗口内合成一个 routing decision。 /
  Layer 5: render-level coalescing。同一 subject / same surface / short window
  内只渲染最新或聚合版。" Those two layers stay as written — they are about
  attention candidates and render flooding, and neither is this path. The
  minimal edit is one added, clearly marked `<div class="note">` under that
  section stating the deviation: D6 lifecycle commentary delivery is not
  coalesced at Layer 3 or Layer 5; it is capped at one phrase per turn by the
  runtime observer over the durable Event Log, 见 ADR-0008 §10. Do not restate
  the reason in the spec and do not touch the two existing bullets.
- `docs/adr/0008-real-time-response-streaming.md:368` currently ends: "Repeated
  progress is coalesced; a timer may decide when to surface a new known state,
  but may not invent a new state." Amend that sentence in place — ADR-0008 has
  NO errata section (its headings are §1..§13; §10 is "Spec changes and
  explicit deviations", §12 "Out of scope"), so there is nowhere else for it to
  go. It becomes: at most one commentary phrase per turn, the first opened one
  wins, and no timer is involved on this path at all. Then add item 11 to §10
  (`:1187-1198`) recording ownership and why: `commentary.py`'s purity is a
  construction-level guarantee against D6's three forbidden hallucinations;
  `PresentationIntent` is ephemeral by spec §3.6.3 and never an Event Log row,
  so window state on it cannot survive a restart; and the runtime watcher
  already performs the durable event-log read this needs
  (`_commentary_reached_the_speaker`). Note for the implementer: ADR-0012 has a
  §10 "Implementation errata and reconciliations" and ADR-0006 has none —
  neither structure applies here, do not invent one.
- `docs/adr/0006-full-duplex-voice-session.md` — judge unchanged and say so.
  `:261` (the enqueue-after-drain paragraph) and `:776` (the Step 2 acceptance
  row "commentary→final never self-interrupts") both stand; the cap does not
  touch the handoff.
- `config/jarvis.yaml:190` — one-line fix. The comment claims the final answer
  "is never delayed, dropped or superseded by it"; a measured 1.502 s delay
  exists and is ADR-conformant (ADR-0006 `:261`/`:776` make "commentary→final
  never self-interrupts" the criterion, not "never delayed"). The ADR wins and
  the comment changes: drop "delayed", keep "dropped or superseded".

## Open questions

## /goal condition
Implement docs/goals/commentary-one-per-turn.md on the current branch. The goal
is met when all of the following appear in the transcript: (1) the diff shows
the per-turn cap in `_open_commentary_in_worker_thread` in
jarvis/runtime/inherent_loop.py placed after `turn_id` resolves and before
`rebuild_projections`, jarvis/decision/commentary.py still importing no
`time`/`random`/`secrets`/`datetime`/`os` and calling no builtin `hash(`, and
no change to the origin filter, the pending-confirmation suppression, or the
non-terminal freshness check; (2) the raw output of the new hermetic test that
drives the real `_commentary_watcher` over one turn with three actions each
emitting three lifecycle rows and asserts exactly one emitted
`surface.response_emitted` row with `phase == "commentary"` for that
`turn_id`, ending in a pass line; (3) the raw output of the SAME test run with
the cap removed, showing it FAIL with a count greater than 1, and the command
used to remove and restore the cap; (4) the raw output of the slice-2 test
showing at least two distinct commentary phrases across six turns with
distinct action ids, all members of the declared variant set, plus the purity
canary passing; (5) the raw output of `PYTHONPATH=. .venv/bin/python -m pytest
-q -m "not live_llm and not live_codex"` run in a shell without
MINIMAX_API_KEY exported, with its passed/deselected counts quoted and
compared to the baseline the lane confirmed for tip 99f306e; (6) the raw
output of `PYTHONPATH=. lint-imports`, `PYTHONPATH=. ruff check .` and the
project's `PYTHONPATH=. mypy --strict` invocation, each ending in a pass line
or exit 0, with no Swift suite run; (7) a live run from a daemon started off
this worktree on its own runtime root and port, with the env loaded via `set
-a; source ~/.jarvis/env; set +a` and evidence that TTS was MiniMax and not
macOS `say` (nonzero sample counts), one question dispatching at least two
actions, and the raw output of the per-turn commentary query
(`type='surface.playback_started'` AND `phase='commentary'`, GROUP BY
`turn_id`, counting rows and distinct `speech_text_hash`) showing the new
turn as `<turn_id>|1|1`, with the turn_id quoted; (8) docs/spec.html §3.6.12
carrying one clearly marked added note for the deviation with its two existing
bullets untouched, docs/adr/0008-real-time-response-streaming.md line 368
amended in place and a new item added to its §10, and
docs/adr/0006-full-duplex-voice-session.md plus config/jarvis.yaml each either
updated or explicitly judged unchanged, following the rule: update the
canonical document that owns a changed contract, do not document what the code
makes clear, do not duplicate a fact across documents; (9) each slice
committed with the project commit skill and `git status` clean; (10) a
Progress line per slice in the card. Or stop after 35 turns.

## Progress


- Slice 1 (per-turn cap): `_turn_already_spoke_commentary` reads
  `response.started` with `phase='commentary'` for the turn and short-circuits
  `_open_commentary_in_worker_thread` after `turn_id` resolves and before
  `rebuild_projections`, so none of the three suppression paths consumes the
  turn's slot. New `test_one_turn_with_three_actions_speaks_exactly_one_commentary`
  passes at 1 emitted commentary row; with the cap deleted it fails
  `assert 3 == 1`. Five existing tests reshaped, none deleted; per-action
  supersession retained and now proven unreachable from the watcher
  (`_cancel_reasons` shows only `shutdown`). Suite 1047 passed / 64 deselected
  (baseline confirmed 1046/64). lint-imports KEPT, ruff clean, mypy 246 files.
