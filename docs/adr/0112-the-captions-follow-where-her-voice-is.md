# ADR 0112 — The captions follow where her voice is

**Status:** Accepted
**Date:** 2026-10-01
**Supersedes:** none

## Context

- Allen, 2026-10-01, a long English story read aloud with captions on: the
  lit words fell about two sentences behind her voice, and when he spoke to
  interrupt her the voice stopped but the lit words kept moving.
- The talk area lit her words by a clock started when the answer began.
  The daemon told it only that she had finished (`spoken`), so a clock had to
  stand in for everything between: the voice's own rate, the wait for the
  first audio, gaps while synthesis catches up, and the hold of ADR 0100,
  which pauses her for about as long as the final recognizer takes to judge
  his words. A constant cannot follow any of these, and the repository holds
  no measured English rate to fit one to.
- The daemon already knows where she is. The output ledger maps each spoken
  segment onto the sample clock, MiniMax gives a word end for each word, and
  the player knows when it holds her.
- `heard_text`, the position the checkpoint events carry, is conservative on
  purpose: a segment that played at reduced gain is never counted, and the
  prefix stops there for the rest of the answer. A soft barge-in ducks her
  first (ADR 0100), so after one that turned out to be a cough the
  checkpoint no longer moves while she goes on talking.
- The text on screen is not the text synthesized: speech drops emoji and
  markdown marks, and segments are trimmed.

## Decision

While she speaks, the daemon tells the surface where her voice is, whenever
it changes: how much she has played, how far the segment playing now reaches,
and whether she is held in place. The surface lights her words from that,
carries it on at its pace estimate only until the next report and never past
the end of that segment, and stops the lit words while she is held and when
she is stopped.

Its limits:

- It is for showing only: nothing is stored, logged or sent to L3, and the
  checkpoint events keep their conservative meaning.
- Position is counted in letters and digits, so speech and caption text agree
  whatever marks, spaces or markdown differ between them.
- A stop is reported when the audio is cut, ahead of the terminal `spoken`
  that follows the durable commit.
- Without a report (a daemon that sends none, or before the first audio) the
  surface keeps the clock estimate from when she began.

## Alternatives rejected

- **Raise the speed constant for Latin text.** Whatever the rate, the clock
  still runs on through a hold (the reported interruption) and cannot absorb
  first-audio latency or a starved gap; and no recording in the repository
  gives a rate to fit.
- **Broadcast the checkpoint's `heard_text`.** Without word ends it moves a
  whole segment at a time, and after a duck it stops for the rest of the
  answer, which would freeze the captions after every soft barge-in that was
  only noise.
- **Send character offsets into the speech text.** Speech text is trimmed per
  segment and stripped of markup the caption keeps, so offsets drift from the
  caption's by exactly the amount that makes a caption look wrong.

## Consequences

- The lit words advance in steps of one word where the provider gives word
  ends, and one segment otherwise, smoothed by the pace estimate in between.
- Caption text whose letters differ from the speech's (a markdown link's URL,
  which speech drops) runs ahead of her by that many letters from there on.
- About four small messages a second go over the local socket while she
  speaks; a surface that does not know the phase `playing` ignores it, as the
  wire already allows.
