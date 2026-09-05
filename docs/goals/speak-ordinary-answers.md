# Goal: speak-ordinary-answers

## Goal
A direct answer to a user utterance is spoken by default; only system-triggered turns and explicitly silent verdicts stay text-only.

## Why
Allen, 2026-09-05: "出声，因为本身就是语音助手". Today every ordinary answer lands on `queue_review`, which no one reads back and which both TTS gates suppress, so the voice assistant never speaks an answer. The spec never required this; it is a fall-through in `attention_policy`, and ADR-0009 D4 silenced the whole channel to keep 3am system turns quiet.

## Current behavior
- `attention_policy` returns `queue_review` as its final default, so `surface.user_intent` / `utterance.received` triggers without a verified Postcondition are queued (jarvis/decision/gates.py:932; docstring rules at :847-890 justify the default only by the timeout/failed Limitation case).
- F1 "no task to refer to" hard refusal is hard-coded `queue_review` (jarvis/decision/__init__.py:1241); a hard refusal after exhausted retries is promoted from `silent_log` to `queue_review` (jarvis/decision/__init__.py:3119-3120).
- `_tts_watcher` drops the whole `surface.response_open/chunk/emitted` triple for `_TTS_SILENT_CHANNELS = {queue_review, silent_log}` (jarvis/runtime/inherent_loop.py:173, :1131-1137, condition at :397-403); `voice_media` re-checks the same set (jarvis/surface/voice_media.py:71, :1837-1840).
- `queue_review` is write-only: no projection, panel, or surface reads it back; its only effect is "text on stdout/WS, no audio" (jarvis/surface/notify.py:23; grep of `queue_review` across jarvis/).
- The daemon path speaks only via `_tts_watcher`; `voice_media` schedules `response.speech_text()` which takes `<voice>` segments only (jarvis/surface/voice_media.py:328-329, :1893-1899).
- The comment block at jarvis/runtime/inherent_loop.py:181-190 states that `queue_review` is the default verdict for an ordinary utterance.

## Target behavior
- For a plain user utterance with no verified Postcondition, no Limitation, and no `worker.reported` trigger, `attention_policy` returns `voice_notify`.
- The F1 hard refusal and the exhausted-retry hard refusal return `voice_notify` (they are direct answers to the user).
- `worker.reported` handling is unchanged: `voice_notify` only with a Limitation or verified Postcondition; `queue_review` when `needs_human_review`; otherwise `silent_log`. `ask_confirm` post-hoc override is unchanged.
- System-triggered turns (supervisor sweep, orphan closure, `action.timeout_assumed` / `action.failed` reconciliation) still reach TTS on a non-speaking channel; `test_canary_system_turns_never_tts` stays green without edits.
- A live plain question through the daemon produces `surface.response_open` with `attention_channel == "voice_notify"` and real playback (a `surface.playback_started` row or the TTS pipeline's first-PCM trace for that response), proving ordinary drafts carry `<voice>` text.

## Affected contracts and files
- L3 jarvis/decision/gates.py:attention_policy — final default and docstring rules block plus `Returns:` list.
- L3 jarvis/decision/__init__.py — the two hard-coded `queue_review` refusal sites (:1241, :3119-3120). Leave the `DecideResult` dataclass default (:857) alone.
- runtime jarvis/runtime/inherent_loop.py:181-190 — comment only; the suppression set stays.
- docs/spec.html §12.4 (output channels, ~:2406-2422) and §3.6.4 (~:1052-1062) — one additive sentence: a direct answer to the user's utterance defaults to `voice_notify`; `silent_log` is for turns that need no voice.
- docs/adr/0009-residency-and-perception.md D4 (:111) — amendment note in the ADR-0002 :1125-1150 style: D4 governs system-triggered turns; ordinary answers now route `voice_notify`.

## Boundaries and non-goals
- Layers that may change: L3 (`jarvis/decision`), runtime comment text, docs.
- Must not change: `_TTS_SILENT_CHANNELS` and `voice_media`'s mirror set; the `AttentionChannel` Literal (no new channel name, because jarvis/decision/stream_risk.py:218 would downgrade every stream to `full_text`); `ATTENTION_CHANNEL_TO_SURFACES`; the `worker.reported` branches; the confirmation override; the streaming route, TTS pipeline, or any L5 code.
- Non-goals: making `queue_review` itself audible; a review queue surface; changing what system turns say; Focus/Rest mode gating (spec §12 scoring is not implemented and is out of scope).

## Rejected approaches
- Remove `queue_review` from `_TTS_SILENT_CHANNELS` — makes system turns and needs-review worker reports speak; fails the canary by design.
- Introduce a new channel such as `answer_voice` — the stream-risk allowlist and permit binding would need widening in two more places for no product gain.
- Fix at L5 by inspecting trigger type in the watcher — spec §3.6.4 forbids L5 from upgrading a channel; the decision is L3's.

## Acceptance evidence
- Positive (hermetic): a scenario or integration test under tests/ (no tests/unit) drives `attention_policy` with a `surface.user_intent`-triggered packet and empty claim evidence and asserts `"voice_notify"`; the same test asserts `worker.reported` without Limitation still yields `"silent_log"` and with `needs_human_review=True` yields `"queue_review"`. Raw pytest output shown.
- Positive (live, required): with a daemon started from the implementation worktree on its own runtime root and port (do not touch the daemon on 127.0.0.1:8006), submit one plain question through the Inherent submit endpoint (see `~/.jarvis-realtime-test/ask.sh` and `show-turn.sh` for the mechanism); show the event trail with `surface.response_open.payload.attention_channel == "voice_notify"` and the playback evidence for that response id. Canary value: the response_id whose `attention_channel` is `voice_notify` and whose playback row or first-PCM trace is shown.
- Regression: `PYTHONPATH=. .venv/bin/python -m pytest -q tests/canary/test_canary_system_turns_never_tts.py tests/canary/test_stream_risk_boundaries.py` passes; the full hermetic suite (live tests excluded, the repo's usual invocation) shows no new failure versus the branch baseline; `lint-imports`, `ruff check .`, and the project's mypy invocation exit 0. Raw output shown.
- Live provider: DeepSeek direct presets are the committed default; keys via `set -a; source ~/.jarvis/env; set +a`.

## Docs to sync
- docs/spec.html §12.4 and §3.6.4 — ordinary answers default to `voice_notify`.
- docs/adr/0009-residency-and-perception.md D4 — amendment note.
- docs/adr/0002-real-codex-flagship-scenario.md — judged unchanged unless the routing table at :1102-1108 is contradicted; say so explicitly.
- jarvis/runtime/inherent_loop.py:181-190 comment — corrected (code comment, not a doc).

## Open questions
(none)

## /goal condition
Implement docs/goals/speak-ordinary-answers.md on the current branch. The goal is met when all of the following appear in the transcript: (1) the diff shows `attention_policy` in jarvis/decision/gates.py returning `voice_notify` on its final default path, the two hard-coded `queue_review` refusal sites in jarvis/decision/__init__.py returning `voice_notify`, and no change to `_TTS_SILENT_CHANNELS`, `voice_media`'s silent set, the `AttentionChannel` Literal, or `ATTENTION_CHANNEL_TO_SURFACES`; (2) the raw output of a pytest run over the new or updated test under tests/ (not tests/unit) that asserts `voice_notify` for a plain user utterance, `silent_log` for `worker.reported` without Limitation, and `queue_review` for `needs_human_review=True`, ending in a pass line; (3) the raw output of `PYTHONPATH=. .venv/bin/python -m pytest -q tests/canary/test_canary_system_turns_never_tts.py tests/canary/test_stream_risk_boundaries.py` ending in a pass line with 0 failed; (4) the raw output of the full hermetic suite with live tests excluded, plus `lint-imports`, `ruff check .`, and the project's mypy invocation, each ending with a pass line or exit 0; (5) a live run against a daemon started from this worktree on a runtime root and port other than the one on 127.0.0.1:8006, showing the event trail of one plain question with `surface.response_open` carrying `attention_channel: voice_notify` and the playback evidence (a `surface.playback_started` row or first-PCM trace) for that response_id, with the response_id quoted; (6) docs/spec.html §12.4 and §3.6.4 and docs/adr/0009-residency-and-perception.md D4 updated, and docs/adr/0002-real-codex-flagship-scenario.md explicitly judged unchanged or updated, following the rule: update the canonical document that owns a changed contract, do not document what the code makes clear, do not duplicate a fact across documents; (7) each slice committed with the project commit skill and `git status` clean; (8) a final Progress line per slice in the card. Or stop after 40 turns.

## Progress
