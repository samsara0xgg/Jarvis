# ADR 0277 — A terminal sends the words first, and the brain judges them as they arrive

**Status:** Accepted
**Date:** 2026-10-15
**Supersedes:** none

## Context

- The brain moved to the Pi (ADR 0170) and the Mac became a voice terminal (ADR 0172). A call
  from the Mac to the brain is a tailnet round trip, 20 to 100 ms, plus the brain's time.
  When the brain lived on the Mac the same calls were function calls.
- The Mac's capture session still judges the final transcript before it sends it, one call
  after another: `elsewhere` (ADR 0219), `recent` (her last words, twice: once for Jev's
  request, once for the verdict), `words` (Jev, in conversation mode or over her, ADR 0130),
  `supersede` (ADR 0053, 0074), then the `utterance` itself, which waits until its row is
  in the log. The audio file (WAV, then `afconvert`, about 30 ms) is also written first.
- Measured 2026-10-15 on the terminal's trace: the utterance reached the brain a median of
  about 0.40 s after the final transcript. None of that time is the owner's work; every
  millisecond of it is added to the first word of her answer.
- With a fake brain a fixed 100 ms away, the old wiring sends the utterance 518 ms after the
  final transcript, after five waiting calls (`elsewhere`, `recent`, `recent`, `words`,
  `supersede`), and one tell (`begin`) among them.
- The judgments need nothing the Mac has. The regexes are in the repository, Jev's key and her
  recent words are on the brain, the log that says what another device heard is on the brain,
  and the answers to supersede are runs on the brain. The Mac only knew two facts: whether
  conversation mode was on and whether the words were said over her. The phone already works
  this way (ADR 0216): it sends the words with those flags and the brain answers with a verdict.

## Decision

The terminal sends the utterance as soon as the final transcript is normalized, with the two
flags, and the brain judges it as it arrives and answers with the verdict in the same reply.

- **One call on the way.** The `utterance` ask carries `judge: {conversation, over_her}`. The
  brain asks, in order, whether another device recorded the words (ADR 0219), then runs the
  word judge (regexes, then Jev; ADR 0053, 0102, 0130, 0153, 0216), starting Jev's one request
  as the words arrive (ADR 0139). For a `turn` it supersedes the unspoken answer of the
  earlier turn (ADR 0074) and then writes the row, so the new turn still cannot see the
  answer it replaces. The reply is `{verdict: "turn", event_uid}`.
- **Other verdicts write nothing.** `{verdict}` alone for `backchannel`, `unclear`, `echo`,
  `stop`, `wait`, `dismissed`, `quiet:<level>` and `elsewhere`. For `dismissed` and `quiet`
  the brain also ends every answer still being written, then supersedes, before replying.
- **The terminal carries out what is its own.** On the verdict it settles the barge-in (she
  goes on or stops), flips conversation mode, holds it for 「等我一下」, sets the quiet level,
  asks the goodbye line, and ends the turn as absorbed on the wire. It asks nothing first.
- **One judge.** The brain calls `word_judge.words_verdict` with the hooks it already binds
  for a daemon and the phone; no copy of the regexes or of Jev's mapping moves.
- **Local mode is unchanged.** A session with `judged_on_send` off (a Mac running alone,
  `runtime.role: all`) judges before emitting with its direct calls, which cost nothing.
- **The audio is named, then written.** The utterance carries `audio_artifact_ref` as
  `<dir>/<turn_id>.m4a`, known before the audio exists; the file is written on a thread of its
  own after the utterance is sent, and only for a turn.
- **A judge that fails leaves a turn,** and so does a supersede that fails: Jev, the other
  device's log and the unspoken answer never cost him his words.

## Alternatives rejected

- **Send the calls at once instead of one after another.** `elsewhere`, `recent` and `words`
  could go together, but the utterance still waits for the slowest and a verdict that says
  no turn must stop the utterance from being written: the brain would hold it anyway. Two
  round trips for the same result, and the judge split across two machines.
- **Send first, judge after, and retract.** A dismissal or an echo would be a turn for a
  moment: the model request starts at once (ADR 0272), a row is in the log, and a cancel
  racing it is what ADR 0219 and 0268 spent effort undoing.
- **Keep the regexes on the Mac and send only Jev's question.** It saves nothing on the
  path (the utterance goes with it) and keeps two copies of the vocabulary in step by hand.
- **Persist the audio first, send the path.** The file is not needed to send, only its name.
  Waiting for it put 30 ms of encoder in front of every turn.

## Consequences

- From the final transcript to the utterance leaving: no waiting call (a local mode session
  never had any). From there to the verdict: one round trip, plus Jev's wait (at most
  `words_timeout_ms`, 0.8 s as shipped) only where it was already paid, in conversation mode
  or over her. Test: with 100 ms a call the utterance leaves 0 ms after the final transcript
  and is answered 102 ms after it; the old wiring left at 518 ms.
- A supersede that has an earlier unspoken answer to drop still asks the terminal to drop its
  audio (ADR 0139), a brain to terminal round trip inside the same reply; most turns have none.
- Her stop over her words now follows the reply, not a call that preceded the send; she is
  already held or lowered (ADR 0216), so what he hears does not change.
- When the brain has no Jev bound, a dismissal found inside a sentence is a dismissal, as
  on a Mac running alone; before, the terminal always bound Jev's hook and a missing one read
  as a turn.
- The brain still answers `words`, `elsewhere`, `recent`, `note`, `begin` and `supersede` for a
  terminal built before this; a terminal that sends no `judge` has its utterance written as a
  turn. Deploy the brain first. Remove those ops when no terminal sends them.
- A new brain with an old terminal judges nothing on receipt (no `judge`), and a new terminal
  with an old brain has every utterance written as a turn: dismissals and listening sounds
  would become turns until the brain is updated.
- If afconvert is missing the WAV stays and the event names a `.m4a` that is not there; the
  audio is only a record (`records.audio_path`), and macOS always has the encoder.
- The utterance timeout (8 s) now also covers the judge; Jev's own limit is under 1 s.
