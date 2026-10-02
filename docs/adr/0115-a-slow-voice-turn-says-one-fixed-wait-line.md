# ADR 0115 — A slow voice turn says one fixed wait line

**Status:** Accepted
**Date:** 2026-10-02
**Supersedes:** 0099

## Context

- Allen, 2026-10-02: in voice mode, when a turn is taking a while, she should
  say one short fixed line so he knows she heard him. He chose the pools below
  and the 2.5 s.
- ADR 0045 shipped `realtime.commentary.enabled: false` because every
  lifecycle row spoke, and 「结果回来了」 landed with nothing before it after
  a tool that finished within a second. ADR 0099 kept that switch off while
  superseding 0045 for the spoken-form rules; this ADR supersedes 0099 so the
  switch has one owner, and it carries 0099's remaining decision (an explicit
  request for counting, reading aloud, verbatim repetition, detail or a
  length overrides the summary limits) forward unchanged.
- ADR-0008 D6 and ADR 0045 ruled out a timer that speaks when no lifecycle row
  changed, and D6 named 「马上好」 with no evidence as forbidden. The owner has
  now decided both: a clock is allowed, and the pool includes 「马上好」 and
  "Almost there.", which claim progress nobody can know.
- Measured on the owner's log (2026-09-26 to 09-29): `web_search` 1.9 s,
  `screen_look` 3.3 s, `refresh_work_state` 7.6 s median, `daily_work_report`
  58 s, every other tool under 0.6 s; from a turn's last tool result to its
  audio 3.9 s median.
- The machinery already exists: a durable-cursor watcher opens a
  `phase="commentary"` ResponseRun with the suppression rules (turn ended, one
  per turn, a pending confirmation) and the L3 decision in
  `jarvis/decision/commentary.py`, which must stay free of clocks and dice
  (`tests/canary/test_canary_commentary_module_is_pure.py`).
- "The answer has started" is `surface.response_open` of the turn with a phase
  other than `commentary`: the first answer segment commits it with its first
  chunk in a spoken_streaming turn, and the whole answer does at once in any
  other turn. `response.started` opens the run before the model is asked, so it
  does not count.

## Decision

Turn `realtime.commentary.enabled` on, and let a turn Allen spoke
(`utterance.received`) say one fixed line, at random from a pool in the
language table in the language of his words, when either happens:

- a tool on the slow list (`SLOW_TOOLS` in `jarvis/shared/lang.py`, the same
  table ADR 0114's status line reads) is dispatched, no earlier than 1.5 s
  after his words; or
- 2.5 s after his words with nothing of the answer started, whether or not a
  tool is involved.

Pools: 「稍等。」「等一下。」「正在办。」「马上好。」 and "One moment." "Hold on."
"On it." "Almost there." In a spoken_streaming turn whose model wrote its own
line before a call (`lead_in`), that line is said instead, unless it runs past
60 characters or states a result.

This amends ADR-0008 D6, which keeps the rest of its rules: its no-timer rule
gives way to the 2.5 s clock, and its ban on 「马上好」 gives way to the
owner's pool.

Limits: at most one line per turn; never once the answer has started or the
turn has ended; never over a pending confirmation; never for a typed turn or a
GPT-Live turn; no model writes it and nothing is requested beyond the TTS of
the phrase. The 2.5 s clock and the random pick live in the runtime watcher;
the L3 function stays pure and is handed the picker.

## Alternatives rejected

- **Keep commentary off and rely on the status line under the ball (ADR
  0114).** It is silent: away from the screen there is no sign she heard him,
  which is what Allen asked to fix.
- **Speak at every tool dispatch, as D6 did.** Every tool but the five on the
  slow list returned in under 0.6 s, so most tool turns would announce work
  that is over before the phrase finishes (the 2026-09-25 complaint).
- **Only the slow list, no clock.** A turn with no tool, or a quick tool
  followed by a 3.9 s median wait for the model's audio, would stay silent for
  the same wait.
- **Keep the sha256 pick inside L3.** Stable per action, but the owner chose a
  random pick as the existing conversation lines use, and a clock-free,
  dice-free L3 is what the canary pins.
- **Leave 「马上好」 out.** It claims progress she cannot know and D6 named it
  forbidden; it stays because the owner chose it, and a false one costs a
  sentence, not an action.

## Consequences

- Every voice turn that is still silent at 2.5 s speaks, including ones that
  would have answered a moment later; the clock starts at `utterance.received`,
  which commits after the recognizer finishes, so it runs a little behind the
  end of his words.
- A line can be heard just before an answer whose first sentence is still
  being synthesized: "answer started" is the committed text, not the first
  audio.
- The ADR-0008 D6 note that the watcher awaits each row in turn, which kept the
  one-line cap race-free, no longer holds: the clock row waits seconds, so each
  row runs in its own task and a process-wide lock covers the check and the
  write.
- The live run on the Mac that a newly enabled realtime switch needs
  (`tests/canary/test_canary_realtime_adoption_tiers.py`) is still owed.
