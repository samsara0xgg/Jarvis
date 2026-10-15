# ADR 0273 — The live captions skip a slow decode, and SenseVoice stays read while the Mac listens

**Status:** Accepted
**Date:** 2026-10-15
**Supersedes:** 0111

## Context

- ADR 0111 shows what has been heard so far while Allen speaks: SenseVoice decodes the
  utterance so far every 240 ms on its own lane, and the surface shows each changed
  hypothesis. It reused the semantic endpoint's degrade, so one decode over 240 ms, or three
  snapshots in a row the lane could not take, stopped the captions for the rest of the
  utterance.
- That budget assumed about 15 ms per second of audio. On 2026-10-15 Allen saw no words at
  all until the whole sentence arrived. The trace of his test turns: the first caption decode
  after the wake took 701 ms and 666 ms (the model idle 9 and 6 minutes, the Mac at 14.1 of
  15 GB of swap), and with another process holding three cores, decodes of about one second of
  audio took 140-265 ms against about 40 ms. Every one of those utterances degraded on its
  first over-budget decode; the captions were already reaching the companion (its socket
  frames, recorded the same afternoon).
- The lane keeps only the latest snapshot and runs one decode at a time, so a slow decode
  cannot build a backlog; it only lowers how often the words change.
- The semantic endpoint (`partial_asr.enabled`) is different: it waits on these decodes to
  end an utterance, so a slow lane there delays the end of every turn.
- A SenseVoice decode of 0.1 s of silence takes a few ms of CPU and reads every weight.

## Decision

While an utterance is spoken, send the surface what has been heard of it so far, and when
the captions run without the semantic endpoint, let a slow or crowded decode skip only that
update; keep the SenseVoice model read by decoding 0.1 s of silence after every 15 s without
a decode, for as long as the process listens.

- The captions are for showing only: the accepted words still come from the final recognizer,
  and the surface drops a partial that arrives after them. Nothing about them is stored or
  sent to L3.
- A decode that raises still stops them for that utterance.
- With `partial_asr.enabled` on, the degrade keeps its ADR-0006 D7 rule.

## Alternatives rejected

- **Keep the 240 ms budget and stop the captions** — on 2026-10-15 it stopped every test
  utterance, including ones whose later decodes took 40 ms.
- **A second SenseVoice instance for the captions** — a final pass holding the shared lock
  delays one caption decode by one decode (11 s of audio: 145 ms alone, 286 ms queued behind
  one, replayed 2026-10-15); once a slow decode only skips, that is one late update, not worth
  another 240 MB model in a process already being swapped.
- **Read SenseVoice only within ADR 0270's five minutes after speech** — the wake utterance
  is the one that comes after a longer silence; it is the turn that was cold.

## Consequences

- On a busy Mac the captions keep decoding the whole utterance so far back to back, so a long
  utterance costs more CPU than before, where the captions had stopped.
- The first words after a cold model can still appear a decode late; only the stop is gone.
- The keep-warm thread decodes silence four times a minute for the life of a listening process.
