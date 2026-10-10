# ADR 0219 — A device's voice acts only on its own turns, and words heard twice make one turn

**Status:** Accepted
**Date:** 2026-10-10
**Supersedes:** none

## Context

- On 2026-10-10 the brain ran on the Pi, the Mac was a voice terminal (ADR 0172) and the iPhone
  talked over `/phone/ws` (ADR 0209, 0216). The owner talked to the phone near the Mac, and the
  Mac's microphone heard the same words:
  - phone utterance at 21:44:02; the Mac's own utterance, woken by the wake word, at 21:44:04
    cancelled the phone's turn as `superseded`;
  - phone turn at 21:44:27; the Mac's utterance at 21:44:30 cancelled it as `barge_in`.
- Both cancels act on every open run. ADR 0074's supersede drops any recent voice turn's unspoken
  answer, and ADR 0006 D8's barge-in targets any open final run, so a device's microphone
  reached answers it never started. The phone already narrowed its own barge to its turns
  (ADR 0209); nothing else was narrowed.
- The owner's rule, 2026-10-10: things stay on the device he is using.
- Which device a turn came from is in the log: the name its opening row was written under
  (ADR 0212, 0217). A turn with no opening row is a background or reconciliation turn.
- Narrowing the cancels does not stop the second device from also taking the words as a turn of
  its own: two answers to one sentence.

## Decision

Let a device's voice cancel only runs of turns that device opened, and let the later of two
devices that heard the same words drop them as no turn.

Its limits:

- **Barge-in, supersede and exit act on one device's turns.** A run belongs to the device its
  turn's opening row names. Supersede's marking of recent turns is narrowed the same way.
- **The host's own microphone owns the Mac's turns and the turns with no opening row.** A
  voice terminal owns only turns opened under its name; a turn with no opening row is left alone
  by it. Paired phones' turns are never touched by the Mac's microphone.
- **The word judge asks first whether the words were heard elsewhere** (verdict `elsewhere`),
  before the quiet command, the regexes and Jev, so it costs no Jev request. An error in the
  question means no.
- **Heard elsewhere means:** another device recorded an `utterance.received`, any channel,
  within the last 8 s, and its transcript matches. Both are normalized (casefolded, punctuation
  and whitespace dropped, a leading wake phrase dropped) and match when the shorter has at least 2
  characters and one contains the other or their `difflib` ratio is at least 0.6.
- **The first device to record the words wins.** The later one is absorbed: no supersede, no
  turn, and a barge-in it was holding goes on as for a listening sound.
- **A typed say is never checked.** A spoken phone `say` is, flagged or not. A terminal asks the
  brain over the link; a brain that does not know the question, or does not answer within a
  fraction of a second, is read as no.

## Alternatives rejected

- **While the phone is in a live conversation, ignore the Mac's microphone.** He may turn to the
  Mac in the middle of it, and then the Mac would be deaf exactly when he is using it.
- **Prefer the phone whatever the order.** It needs cancelling a turn the Mac has already
  started. First heard is simpler, and the phone usually hears first because it needs no wake
  word (the 21:44 pairs were 2 s and 3 s apart, inside the window).
- **Match the exact text.** Two recognizers rarely agree: the phone's and the Mac's transcripts
  of one sentence differ in punctuation, in a homophone, or in the wake phrase the Mac keeps.
- **Cancel by channel instead of by device.** The phone's and the Mac's spoken turns use
  different channels, but two terminals use the same one, and a typed turn on a device would be
  missed. The opening row's device is the one fact every kind of turn has.

## Consequences

- A different sentence said to the Mac within 8 s that happens to resemble the phone's is
  dropped. It is rare, and he says it again.
- `hold_runs` (ADR 0053's hold on completion while he talks) stays global: a hold is on the
  registry, not on a device's turns, and releases when that device's words end.
- A turn typed on the Mac is the Mac's, so a voice terminal named differently from `mac` no
  longer cancels it by speaking. A device named `mac` still cannot be told from the host's own
  rows (ADR 0212).
- A terminal whose brain predates the `elsewhere` question never absorbs a line, and an
  unreachable brain does not delay it past the question's timeout.
- Two devices that ask before either has recorded can both take the words. The window is the
  time between the second device's recognizer finishing and the first one's row landing.
- A phone's resent `say` that its connection already recorded keeps its turn, even if the Mac
  heard the words after.
