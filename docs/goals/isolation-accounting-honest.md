# Goal: isolation-accounting-honest

## Goal

Two accounting statements the daemon makes about itself stop being false: the
boot reconciler names lane isolation instead of blaming the restart, and the
host output latency that gates the heard cursor is never replaced by a smaller
number than the host reported.

## Why

Both are direct consequences of what merged at `a672496`, and each was
deliberately deferred by the lane that shipped it.

Slice 1 needed the isolation row to exist before the reconciler could read it;
`jarvis/state/event_log.py:913-916` says so in the registry comment it shipped
with — "without this row that outcome is unprovable after the fact and the
next boot's reconciler mislabels the orphan `daemon_restart`". The row exists
now. Today the ledger does not merely lack an explanation for the silence: it
actively attributes it to the restart, and the restart was itself forced by the
isolation (`_lane_isolated` is never reset, so the process speaks nothing more
until it is restarted — ADR-0006:669-673).

Slice 2 was flagged by an independent verifier; the implementing lane referred
rather than changed it because the previous card's acceptance pinned the
behaviour. The value gates `record_audible`, i.e. the heard side, so
under-stating it over-claims what the user heard — the exact direction
ADR-0006 §3 D6 forbids.

## Current behavior

Slice 1 — the reconciler cannot tell isolation from a restart:

- `reconcile_open_playback` scans only `surface.playback_started`,
  `surface.playback_checkpoint` and the three playback terminals
  (`jarvis/surface/playback_recovery.py:88-105`); it never reads
  `surface.playback_lane_isolated`.
- Every orphan it closes gets `"reason": "daemon_restart"` unconditionally
  (`jarvis/surface/playback_recovery.py:135`), and the docstring at `:78`
  states that as the contract.
- The isolation row carries `response_id` and `playback_generation_id`
  (`jarvis/surface/voice_media.py:3193-3206`), which is exactly the CAS
  identity `_cas_identity` already builds
  (`jarvis/surface/playback_recovery.py:53-70`). The join is available; nothing
  performs it.
- Registered at `jarvis/state/event_log.py:918-932`, `owner_layer="L5"`,
  required payload `session_id, response_id, turn_id,
  playback_generation_id, terminal_type, error_type, isolation_reason`.
- Four `isolation_reason` values, six call sites:
  `fallback_cleanup_unproven` (`jarvis/surface/voice_media.py:2762`),
  `callback_publication_unsettled` (`:2848`, `:2880`),
  `terminal_append_exhausted` (`:3025`), `task_escaped` (`:3068`).
- The append is best effort and swallows its own failure
  (`jarvis/surface/voice_media.py:3208-3215`), so the row can be absent for an
  isolation that really happened. `terminal_append_exhausted` is the case where
  it is *systematically* absent: that site isolates because the Event Log
  terminal append exhausted its retries
  (`jarvis/surface/voice_media.py:3019-3027`), and `_emit_lane_isolated` writes
  through the same `emit_event` on the same connection.

Slice 2 — the latency fallback is anti-conservative:

- `_host_output_latency_ns` (`jarvis/surface/voice_tts.py:1470-1477`) accepts
  the host value only when `0.0 < seconds <= self._MAX_PLAUSIBLE_OUTPUT_LATENCY_S`,
  which is `1.0` (`jarvis/surface/voice_tts.py:615`). Anything else returns
  `self._default_output_latency_ns`.
- `_default_output_latency_ns` comes from the constructor default
  `estimated_output_latency_s: float = 0.12` (`jarvis/surface/voice_tts.py:631`,
  stored at `:694-696`). **Neither production construction site passes it**
  (`jarvis/runtime/inherent_loop.py:1910-1915`, `:1960-1968`) and
  `config/jarvis.yaml` has no such key, so "the configured value" is literally
  the hardcoded 120 ms in the signature — there is no operator knob.
- So a host reporting `1.5` — a real Bluetooth-stack figure — is discarded and
  replaced with `0.12`, the least conservative option available.
- The value is read after `_open_output_stream` returns and before
  `stream.start()` (`jarvis/surface/voice_tts.py:757-762`), rides the lease as
  `estimated_output_latency_ns` on `surface.playback_started`
  (`jarvis/surface/voice_media.py:2239-2241`), and is set as
  `presentation_delay_ns` on every callback report
  (`jarvis/surface/voice_tts.py:1641`), which is the horizon gating
  `record_audible` — `submitted_samples` is ungated (ADR-0006:366).
- `record_audible` is documented "rounding backward"
  (`jarvis/surface/voice_ledger.py:245-251`).
- ADR-0006 §3 D6 line 349: "`cursor_quality` is `measured_dac`, `estimated`, or
  `unknown`, and all boundary calculations round backward." Line 372: "This may
  under-count a few words Allen actually heard. That is preferable to
  contaminating future context with words he did not hear." That is the
  round-backward rule, verified in the file, D6 spans lines 317-375.
- ADR-0006:366 already records that 120 ms was never a chosen safety margin and
  that the reference machine measures 18.7 ms.

## Target behavior

Slice 1:

- `reconcile_open_playback` also scans `surface.playback_lane_isolated` and
  collects the `(response_id, playback_generation_id)` identities it names.
- For an orphan whose identity has an isolation row, the
  `surface.playback_interrupted` row it appends carries
  `"reason": "media_lane_isolated"`.
- For an orphan with no isolation row, the row still carries
  `"reason": "daemon_restart"` — unchanged.
- Everything else about the reconciler is unchanged: the cursor carried from
  the last checkpoint or `_UNHEARD_CURSOR`, `source_event_id` pinned to the
  started row's uid, the `session_id` skip, idempotence across boots.

Why `media_lane_isolated` and not something else, against the vocabulary
already in use (`jarvis/surface/voice_media.py`,
`jarvis/surface/playback_recovery.py`, `jarvis/runtime/inherent_loop.py` today
emit `daemon_restart`, `daemon_shutdown`, `media_owner_shutdown`,
`foreground_superseded`, `superseded`, `user_stop`, `system_sleep`,
`playback_generation_lost_before_completion`, `tts_response_timeout`, …):

- It is snake_case and names the cause, like every other value.
- The `media_*` prefix is already established by `media_owner_shutdown` for
  "the L5 media owner caused this".
- It is the same noun as the event type it is joined to
  (`surface.playback_lane_isolated`), so the join is obvious to a reader.
- It is deliberately **not** one of the four `isolation_reason` values. Reusing
  those on the terminal's `reason` would make two different fields share one
  vocabulary and destroy the ability to tell which row wrote a given string.
  The specific cause stays one join away on the same identity.

Slice 2:

- `_host_output_latency_ns` never returns a value smaller than a positive
  number the host reported. A reported value above the ceiling is **clamped to
  the ceiling**, not discarded.
- Exactly these inputs still fall back to `_default_output_latency_ns`, and the
  reason is the same for all three — the host supplied no measurement at all,
  so there is nothing to be conservative about:
  - the attribute is absent (`getattr(stream, "latency", None) is None`);
  - the attribute is not a real number, or is a `bool`;
  - the value is `0.0` or negative. Zero is PortAudio's "not available"
    sentinel, and believing it literally would claim samples audible the
    instant they cross the callback boundary — strictly worse than any guess.
- A reported value in `(0.0, ceiling]` is used verbatim, as today. The 18.7 ms
  reference machine is unaffected; the correction that shipped at `a672496`
  stands.
- The ceiling stays a real bound (a garbage `1e9` must not freeze the audible
  horizon and with it `fully_presented`), but it now reads as "the largest
  latency this system will act on", not "above this we stop believing the
  host and substitute something smaller".

This necessarily changes an acceptance case the previous card pinned. Say so in
the commit body: `tests/integration/test_wave2_streaming_media.py:3740`, the
`absurd_value` case of
`test_playback_started_carries_the_host_output_latency_or_the_configured_one`
(`:3733-3796`), currently `(12.0, 120_000_000)`, must become
`(12.0, 1_000_000_000)` with an id that says clamp rather than fallback. This
is a deliberate supersession of that pin, not a contradiction the lane has
discovered — do not "fix" the implementation to keep the old expectation.

## Affected contracts and files

- L5 `jarvis/surface/playback_recovery.py:reconcile_open_playback` — add
  `surface.playback_lane_isolated` to the `iter_events_of_types` selection,
  collect isolated identities, branch the `reason` string. Update the docstring
  at `:78`, which currently states `daemon_restart` as the contract.
- L5 `jarvis/surface/voice_tts.py:_host_output_latency_ns` (`:1470-1477`) —
  clamp instead of discard; update the docstring and the `:757-761` comment
  above the call site, which enumerates the old rule.
- L2 `jarvis/state/event_log.py:913-916` — the registry comment justifying the
  isolation row still says the next boot "mislabels the orphan
  `daemon_restart`". After slice 1 it does not. Comment only; **no schema
  change**: `reason` is already required on `surface.playback_interrupted`
  (`jarvis/state/event_log.py:895-906`) and the registry validates key presence,
  not value membership.
- `tests/integration/test_boot_playback_reconciliation.py` — new coverage for
  both branches; existing `daemon_restart` assertions at `:60` and `:145` are
  the absence-path regression and must keep passing untouched.
- `tests/integration/test_wave2_streaming_media.py:3733-3796` — the parametrized
  latency pin changes as described above.

## Boundaries and non-goals

- Layers that may change: **L5 only** (`jarvis/surface/`). Both edits are
  inside existing modules using existing imports; no new cross-layer edge is
  introduced, so `lint-imports` should be unaffected.
- Must not change:
  - the L3 ResponseRun reconciler's `response.failed(reason="daemon_restart")`
    (`jarvis/decision/response_run.py:881-894`, ADR-0008 §4.4 line 955) — a
    different reconciler for a different lifecycle;
  - the playback CAS, `terminalize_playback`, or the at-most-one-terminal
    invariant;
  - the reconciler's cursor reconstruction (`_CURSOR_FIELDS`,
    `_UNHEARD_CURSOR`, `jarvis/surface/playback_recovery.py:39-50`) — the
    `reason` field is the only payload field this card touches;
  - `surface.playback_lane_isolated` itself: no new field, no new
    `isolation_reason` value, no change to which sites emit it;
  - the isolation append staying best effort and swallowing its own failure;
  - any Swift source. Nothing under the Inherent card is in scope.
- Non-goals:
  - **No conservative margin on top of the real measurement.** ADR-0006:366
    says such a margin "belongs on top of a real measurement as its own
    explicit term". Adding one silently is exactly the drift this program
    guards against; it is a separate decision the owner has not made.
  - Interrupt fade / declick. ADR-0006 D8's deferral at `:439` defers Phase 1's
    `duck_gain` ramp and F11's unduck — that is barge-in **ducking**. A fade on
    interrupt is a different behaviour and is **not** pre-authorized by that
    sentence; the owner has not ruled on it.
  - `_purge_after_drain` clearing the whole lane rather than only the stopped
    group.
  - A second durable path for the case where the durable path itself is broken.
    Slice 1 handles that case by behaving correctly with the row absent, not by
    finding another way to write it.
  - Any change to `cursor_quality` labelling. Nothing may become
    `measured_dac`; that label's bar is "a real measured DAC/loopback mapping"
    (`jarvis/surface/voice_ledger.py:1-12`) and no PortAudio-reported value
    clears it. A host-reported latency stays `estimated`.
  - Copying `isolation_reason` onto the terminal payload. That would need an L2
    registry change and duplicates a fact one join away.
  - Adding a `config/jarvis.yaml` knob for `estimated_output_latency_s`. The
    absence of one is noted as a fact, not as a gap to fill.

## Rejected approaches

- **`max(reported, configured)` for slice 2.** This is the phrasing the brief
  offered and it is wrong. `configured` is the hardcoded 120 ms, and the
  reference machine reports 18.7 ms, so `max` clamps the real measurement back
  to 120 ms on every fast device and silently reverts the entire correction
  that landed at `a672496` and is defended in ADR-0006:366. It fixes the slow
  device by breaking the fast one.
- **Raising `_MAX_PLAUSIBLE_OUTPUT_LATENCY_S` while keeping the discard.** Moves
  the anti-conservative cliff to a new number instead of removing it; a host
  reporting just above the new ceiling still drops to 120 ms.
- **Believing an unbounded reported value.** A garbage `1e9` would push the
  audible horizon past every deadline, so `fully_presented` never becomes true
  and the generation never ends. That is a liveness failure, not conservatism.
- **Writing the terminal from the isolation site instead of at boot.** Three of
  the four `isolation_reason` causes exist precisely because the callback
  publication never settled, so `heard_through_sequence` and
  `submitted_samples` cannot be filled honestly, and the fourth exists because
  the terminal CAS already exhausted its retries (ADR-0006:668-673).
- **Reusing an `isolation_reason` value as the terminal's `reason`.** Collides
  two vocabularies in one field; a reader could no longer tell which row a
  given string came from.

## Acceptance evidence

Every criterion below reads a payload field out of an `events` row. No
criterion may rest on `record_realtime_trace`
(`jarvis/shared/realtime_trace.py:1-7`, telemetry-only, in-memory deque) — it
is not an Event Log row.

Slice 1. The two fixtures already shipped at `a672496` produce both branches
without inventing a scenario; each leaves an open `surface.playback_started`
with no terminal, which is exactly what the reconciler consumes.

- Positive (row present):
  `tests/integration/test_wave2_streaming_media.py:1928-1980`
  (`test_an_unsettled_callback_publication_writes_a_durable_lane_isolated_row`)
  leaves response `RISO`, generation `1`, isolated with one isolation row and
  zero terminals. Run `reconcile_open_playback` over that same database and
  read back from the log: the single
  `type = 'surface.playback_interrupted'` row with
  `json_extract(payload_json, '$.response_id') = 'RISO'` has payload field
  **`reason == "media_lane_isolated"`**.
- Degenerate (row absent, the case that must not be assumed away):
  `tests/integration/test_wave2_streaming_media.py:1984-2046`
  (`test_isolation_completes_even_when_its_own_durable_row_cannot_be_appended`)
  isolates response `RISOF` with the isolation append injected to fail, and
  asserts `_isolation_rows(verdict) == []` at `:2044`. Over that database the
  reconciler's `surface.playback_interrupted` row for `RISOF` must have payload
  field **`reason == "daemon_restart"`**.
- Regression, absence path on clean orphans:
  `tests/integration/test_boot_playback_reconciliation.py` still reads
  `daemon_restart` at `:60` (both `checkpointed` parametrizations) and `:145`
  (the boot helper on its own connection), and
  `reconcile_open_playback(conn) == ()` on a second boot at `:80` still holds.
- Regression, live crash path: `tests/scenarios/test_live_crash_recovery.py:737`
  asserts `payload["reason"] == "daemon_restart"` after a real kill; a killed
  process writes no isolation row, so this must pass unchanged. It is
  `live_*`-marked and outside the hermetic run — the lane states that it is not
  re-run and why, rather than claiming it passed.

Why this set is not false-passable: a criterion asserting only
`reason == "media_lane_isolated"` on `RISO` would also pass on an
implementation that writes that string unconditionally. The `RISOF` case and
the four surviving `daemon_restart` assertions are what make the pair
discriminating — they fail on the unconditional implementation and on an
implementation that never reads the isolation row. `provider` pinning is not
needed here: none of these criteria counts samples, and the
`submitted == total` trivial-zero hazard of `_run_macos_say` does not apply to
a `reason` string. (`config/jarvis.yaml:262` does ship
`enable_macos_say_fallback: true`, so that path is live under defaults — it is
simply not what these criteria evaluate.)

Slice 2. One parametrized case set, one Event Log payload field.

- Positive:
  `tests/integration/test_wave2_streaming_media.py:3733-3796`
  (`test_playback_started_carries_the_host_output_latency_or_the_configured_one`)
  reads `estimated_output_latency_ns` out of the `surface.playback_started`
  `payload_json` for response `RL` at `:3789-3795`. After the change the table
  must read:
  - `(0.035, 35_000_000)` — plausible host value, used verbatim, **unchanged**;
  - `(0.0, 120_000_000)` — degenerate sentinel, falls back, **unchanged**;
  - `(None, 120_000_000)` — attribute absent, falls back, **unchanged**;
  - `(12.0, 1_000_000_000)` — **changed from `120_000_000`**, above the ceiling
    now clamps to the ceiling;
  - `(1.5, 1_000_000_000)` — **new**, the realistic slow-device value the
    verifier named; it is the same branch as `12.0` but is the case that
    motivates the change, so it is pinned by name.
- Why this set is not false-passable: it discriminates all three candidate
  implementations. `max(reported, configured)` fails the `0.035` case (it would
  read `120_000_000`). "Believe everything" fails the `0.0` and `None` cases.
  The current shipped code fails the `1.5` and `12.0` cases. Only the clamp
  passes all five.
- Regression: the full hermetic suite matches or exceeds the baseline recorded
  on `realtime-integration` at `a5ce427` — **1067 passed / 64 deselected** —
  with the delta explained. Tier 1 gates `lint-imports`, `ruff check .`,
  `mypy --strict jarvis tests scripts tools` exit 0. Swift is untouched by both
  slices, so the Swift baseline of 191 is not re-run; the lane states that
  rather than claiming it.

Hermetic command (`MINIMAX_API_KEY` unset per `tests/conftest.py:178-192`):

    env -u MINIMAX_API_KEY PYTHONPATH=. .venv/bin/python -m pytest -q \
      -m "not live_llm and not live_codex"

- Live run: **not required**, for both slices, and the lane must say so rather
  than skip silently.
  - Slice 1 is a fold over durable rows with no device, network, or LLM
    involvement; a live daemon restart would exercise the `daemon_restart`
    branch only, which is the branch that does not change.
  - Slice 2's changed branch is unreachable on real hardware: no PortAudio
    device on this machine reports above the ceiling (the reference figure is
    18.7 ms, ADR-0006:366), so a live run cannot execute the clamp. The
    unchanged verbatim branch is what already shipped and was already
    live-observed.
  - Consequence: **no audio device switch is needed**. Do not change the system
    default output device for this card.

## Docs to sync

- `docs/adr/0006-full-duplex-voice-session.md:674-678` — "The orphaned
  `surface.playback_started` is separately terminalized by the next boot's
  reconciler as `surface.playback_interrupted` with `reason: "daemon_restart"`;
  that label describes the restart, not the isolation, and reading the isolation
  row is the only way to tell the two apart." Every clause of this becomes
  false. Rewrite to: the reconciler joins the isolation row on
  `(response_id, playback_generation_id)` and writes
  `reason: "media_lane_isolated"` when one exists, `daemon_restart` when none
  does; the isolation row remains the only place the specific
  `isolation_reason` can be read.
- `docs/adr/0006-full-duplex-voice-session.md:363` — "a missing attribute, a
  degenerate `0.0`, or a value above one second are all treated as 'the host
  did not fill this field'." The third clause becomes false. Rewrite so a value
  above the ceiling clamps to the ceiling, and state why: this value gates the
  heard side, so it is never replaced by a smaller one.
- `docs/adr/0006-full-duplex-voice-session.md:366` — judge unchanged. It already
  states the correction and already reserves an explicit conservative margin as
  a separate future term; nothing there becomes false.
- `docs/adr/0006-full-duplex-voice-session.md:349` (§3 D6, round backward) —
  judge unchanged. Slice 2 makes the code satisfy this sentence rather than
  altering it.
- `docs/adr/0008-*.md:955` — judge unchanged. That is L3's ResponseRun
  reconciler, a different lifecycle.
- `docs/spec.html` — judge unchanged, and say so. Neither `daemon_restart` nor
  the reconciler's `reason` vocabulary appears there; §5.4's event list does not
  enumerate `reason` values.
- `jarvis/state/event_log.py:913-916` and
  `jarvis/surface/playback_recovery.py:78` and
  `jarvis/surface/voice_tts.py:757-761` — code comments and docstrings that
  state the old behaviour; updated as part of the change, not as documentation
  work.

## Open questions

(none)

## /goal condition

Implement `docs/goals/isolation-accounting-honest.md`. The goal is met when the
transcript shows all of the following as raw command output, not as claims.

(1) Slice 1, presence path: the raw output of a hermetic pytest run of
`tests/integration/test_boot_playback_reconciliation.py` and
`tests/integration/test_wave2_streaming_media.py` is shown and passes, and it
includes a check that reads the `surface.playback_interrupted` row back out of
the `events` table for the isolated response `RISO` and asserts its payload
field `reason == "media_lane_isolated"`.

(2) Slice 1, absence path: the same run includes a check that for response
`RISOF` — isolated with its own isolation row provably absent — the payload
field `reason` is still `"daemon_restart"`, and the pre-existing
`daemon_restart` assertions in `tests/integration/test_boot_playback_reconciliation.py`
(both `checkpointed` parametrizations, and the boot-helper case) still pass
unmodified. A run that only demonstrates the presence path does not satisfy
this condition.

(3) Slice 2: the parametrized latency check reading
`estimated_output_latency_ns` out of the `surface.playback_started`
`payload_json` passes with `0.035 -> 35_000_000`, `0.0 -> 120_000_000`,
`None -> 120_000_000`, `12.0 -> 1_000_000_000`, and `1.5 -> 1_000_000_000`. The
transcript states explicitly that the `12.0` expectation was changed from
`120_000_000` and that this is a deliberate supersession of the pin from the
previous card, not a contradiction to be worked around.

(4) No conservative margin was added on top of the host-reported latency, and
no value anywhere was relabelled `measured_dac`. The transcript says so.

(5) Regression: the raw output of
`env -u MINIMAX_API_KEY PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live_llm and not live_codex"`
is shown and is at least 1067 passed / 64 deselected, with any delta from that
baseline explained. The raw output of `lint-imports`, `ruff check .`, and
`mypy --strict jarvis tests scripts tools` is shown and each exits 0. Report the
printed counts; never infer them.

(6) Each entry under "Docs to sync" was either updated or explicitly judged
unchanged in the transcript, including `docs/spec.html`. When the change alters
a documented contract, invariant, ownership boundary, or externally relevant
behavior, update the canonical document that owns that fact; do not document
what the code already makes clear; do not duplicate a fact across documents.

(7) No system audio output device was switched, no daemon was started, stopped
or restarted, and nothing under `.claude/worktrees/realtime-live-test` was
built, launched or deleted.

(8) `git status` shows a clean tree and each slice is a separate commit with
explicit staged paths.

Or stop after 25 turns.

## Progress

- Slice 1 done. All citations re-pinned against the merged tree before any
  edit; one drift found and it does not change the work:
  `jarvis/state/event_log.py:895-906` names `surface.playback_failed`'s block,
  not `surface.playback_interrupted`'s — the identical `reason`-required block
  for `playback_interrupted` is at `:869-879`, so "already required, no schema
  change" holds. Every other citation matched exactly.
  `reconcile_open_playback` now selects `surface.playback_lane_isolated`,
  collects the identities it names, and branches `reason`.  Acceptance:
  `test_boot_playback_reconciliation.py` + `test_wave2_streaming_media.py`
  51 passed; `RISO` reads back `media_lane_isolated`, `RISOF` (isolation row
  provably absent) reads back `daemon_restart`; the four pre-existing
  `daemon_restart` assertions unmodified and passing.  The pair is
  discriminating by mutation: an unconditional `media_lane_isolated` fails 4
  tests, an implementation that never reads the isolation row fails 1.
  Tier 1 lint-imports KEPT 1/1, ruff clean 250 files, mypy strict clean
  248 files.  ADR-0006:674-678 rewritten.
- Slice 2 done. `_host_output_latency_ns` clamps instead of discarding: a
  positive report is never replaced by a smaller number, and only the three
  no-measurement inputs (absent attribute, non-real number, `<= 0.0`) fall
  back.  The latency table now reads `0.035 -> 35_000_000`,
  `0.0 -> 120_000_000`, `None -> 120_000_000`, `1.5 -> 1_000_000_000` (new),
  `12.0 -> 1_000_000_000` (**changed from `120_000_000`**, the deliberate
  supersession of the pin the previous card left, not a contradiction).  Five
  named cases pass individually.  No conservative margin was added on top of
  the host measurement and nothing was relabelled `measured_dac`.  Full
  hermetic run 1068 passed / 64 deselected in 60.58s vs the 1067/64 baseline;
  the +1 is exactly the new `slow_device_clamped` parametrization.  Tier 1
  lint-imports KEPT 1/1, ruff clean 250 files, mypy strict clean 248 files.
  ADR-0006:364 rewritten.  Judged unchanged and left alone: ADR-0006:366 and
  :349 (§3 D6), ADR-0008 §4.4 (a different lifecycle's reconciler),
  `docs/spec.html` (`grep -c 'daemon_restart\|estimated_output_latency'` = 0).
  Old goal cards under `docs/goals/` restate the superseded rule; they are the
  record of what those runs decided, not canonical contract documents, so they
  are left as written.
- Verifier (fresh context, opus, `realtime-integration..HEAD`) found no
  blocking defect and independently reproduced every gate number and both
  mutation results.  Two of its three low findings are fixed here: the slice 2
  commit body mislabelled the rewritten latency sentence as §4.2 when
  `:364` is inside §3 D6 (`:317-375`; §4.2 starts at `:607` — slice 1's own
  "§4.2" is correct), and ADR-0006:364 listed a proper subset of the code's
  fallback set, now "a missing attribute, a value that is not a real number,
  or a degenerate `0.0`".  Left alone deliberately: the catch-all `else` at
  `playback_recovery.py:123` (the verifier's own recommendation is not to
  touch it inside this card's boundary; behaviour is correct today), the
  pre-existing sub-nanosecond truncation at `voice_tts.py:1491` (unreachable),
  and the `?? .venv` entry — the worktree's `.venv` is a symlink and
  `.gitignore:7` says `.venv/`, which only matches a directory; it is lane
  environment, not lane work, and the hub's own instruction is never to stage
  it.
