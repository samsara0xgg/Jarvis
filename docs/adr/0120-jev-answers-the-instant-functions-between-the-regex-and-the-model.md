# ADR 0120 — Jev answers the instant functions between the regex and the model

**Status:** Accepted
**Date:** 2026-10-02
**Supersedes:** none

## Context

- Allen, 2026-10-02, approved adding Jev, TypeSafe's hosted decision model
  reached through OpenRouter, as the middle layer of his original design:
  regex, then a small surrogate gate model, then the model. At first only its
  high-confidence direct decisions count; later a local model is distilled from
  the logged decisions. The regex whitelist (spec #acting) cannot list every
  way to say "what time is it", and a turn it misses waits a whole model round.
- Offline eval on 505 of Allen's turns (labels written by Claude, not by
  Allen), one choice question over Tier 0's instant functions: median call
  latency 0.14 s. At confidence 0.9 or above it routed 92 turns right, missed
  22, and routed 23 wrongly; 12 of those 23 were "how are you" labelled small
  talk, which the none option now names.
- With the previous two exchanges in the state, routing right at confidence
  0.9 or above went from 88 to 92 of 114.
- The call sends Allen's words off the Mac to a provider that is not in the
  list the spec gave before this change; spec #egress is the list.
- The Tier 0 path already carries everything downstream of a hit: the
  Pre-action Gate, confirmation rules, the tool, the template answer. A route
  that reaches it by any other way would have to copy all of that.
- Provider keys reach the daemon as environment names, filled from the
  Keychain item `Jarvis` or `~/.jarvis/env` (ADR 0009 D1).

## Decision

When no Tier 0 row matched and `realtime.surrogate_route.enabled` is true, ask
Jev one choice question over the Tier 0 functions that take no argument from
Allen's words (plus the repeat shortcut), and when it answers with confidence
at or above `min_confidence` (0.9) and not `none`, run that function through the
Tier 0 path unchanged; every other outcome falls through to the model.

- **Layer.** The decision and the HTTP call live in L3 (`jarvis/decision/`,
  beside the model client that already makes provider calls); the settings are
  read and the object is built only in `runtime/`. The option set is the
  Tier 0 rows with no captured or fixed argument, so `note_capture` and the
  `open_*` rows are not offered; a test fails when the table and the options
  drift apart.
- **State.** The previous two exchanges within ten minutes, Allen's words and
  her voice answer cut to 200 characters, then `User: <words>`.
- **Failure.** Past `timeout_ms` (400), an HTTP error, a missing key, an
  unreadable answer, or a confidence under the bar: the model answers as if
  Jev did not exist. The first failure of each kind is logged once at warning.
- **Record.** Every call, accepted or not, failed or not, is one
  `route.surrogate_decided` event carrying the options version, so the turn's
  Jev choice can be set beside what the model did and a local model trained on
  the pairs. The key is never in an event or a log line.
- **Key.** `OPENROUTER_API_KEY`, read from the environment at each call, filled
  like the other provider keys.
- **Default off** in the repo; Allen enables it in `settings.yaml`.

## Alternatives rejected

- **Keep the regex only.** The whitelist is exact sentences by design (spec
  #acting), so it misses every other phrasing, and each miss waits a whole
  model round against Jev's 0.14 s median call.
- **Accept lower confidence.** The eval's 23 wrong routes are already at 0.9 or
  above and each speaks a wrong function aloud; a lower bar can only add to
  them.
- **Run Jev and the model in parallel and take whichever is first.** Every
  turn would then pay for both, and a wrong Jev route could already be spoken
  when the model's answer arrives; falling through only on a miss costs one
  0.14 s call at worst and nothing when Tier 0 hit.
- **A separate answer path for Jev's choices.** It would copy the Tier 0
  gate, confirmation and template handling, and the two paths would drift.
- **Read the key from the `jarvis-eval-openrouter` Keychain item directly.**
  No other provider key is read by service name; the daemon's keys are
  environment names, so a second mechanism would be the only one of its kind.

## Consequences

- Allen's words and two earlier exchanges leave the Mac on every turn Tier 0
  did not answer, once he enables it; spec #egress says so.
- A turn that Jev does not accept is 0.14 s slower in the median and up to
  about 0.4 s plus one connection phase slower on a timeout: the timeout bounds
  each HTTP phase, and a late answer is discarded rather than used.
- A wrong route (23 of 505 turns in the eval) is a spoken answer to a question
  Allen did not ask; the event log is how they are found.
- The option descriptions are part of the measured behaviour: changing one
  means a new options version, and the logged choices before it no longer
  compare.
- If another ADR takes number 0120 first, this one needs renumbering.
