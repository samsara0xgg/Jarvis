# ADR 0172 — A terminal speaks and listens with its own voice session, and the brain holds the provider and the turn

**Status:** Accepted
**Date:** 2026-10-07
**Supersedes:** none

## Context

- ADR 0170 splits voice at text. The terminal keeps the microphone, echo
  cancellation, wake, endpointing, ASR and playback. The brain synthesizes
  speech and streams it to the terminal. Playback progress comes back so the
  caption timeline and the heard prefix stay on the brain. On 2026-10-07 the
  owner approved building the remaining steps ("那去做吧").
- Two objects own voice in one process today:
  - The capture session (`DuplexVoiceSession`) runs wake, VAD, endpointing,
    partial and final ASR and barge-in. It reaches brain state through about
    thirteen callbacks: the word judge (an LLM call), turn-in-flight and
    recent-speech reads of the event log, run holds, cancels and supersedes,
    and the conversation-mode switches.
  - The media actor (`StreamingTTSPipeline`) does three things:
    - It reads the answer's `surface.response_*` rows from the event log.
    - It drives a provider session through the `TTSSession` protocol:
      connect, open, send, audio events, finish, abort and close.
    - It owns the player and its sample-cursor ledger, which it polls every
      5 ms. It writes `surface.playback_*` rows, and the heard prefix and
      captions fold from those rows.
- The ledger's audible cursor is the output callback's clock plus the device's
  presentation delay. The callback runs on the terminal. Barge-in stops audio
  on the terminal's own speaker, and the heard prefix freezes at that instant.
- Measured on 2026-10-07: the MacBook (on campus) reached the Pi over a
  Tailscale relay in about 80 ms per HTTP request, against 3 ms on loopback.
- The provider PCM stream is already a network stream: MiniMax sends hex
  int16 at 32 kHz with word times. The key must stay on the brain (ADR 0170).
- One microphone has one owner. An `all` daemon and a terminal on the same Mac
  cannot both open it.

## Decision

Run the capture session and the media actor on the terminal, unchanged, and
cross the link only at their two edges: the provider session and the turn.

Its limits:

- **Speaking.** The terminal's media actor gets a provider whose sessions are
  proxies. Each `TTSSession` call goes to the brain, which holds the real
  provider session and the key. The provider's audio events come back as they
  arrive. The resampler, trimmer, player, ledger, ducking, cues and the `say`
  fallback stay on the terminal.
- **The answer reaches the terminal as rows.** The brain streams the
  `surface.response_*` and `response.cancelled|failed` rows of a turn to one
  voice terminal: the one whose utterance opened the turn, otherwise the most
  recently connected one. The terminal appends them to a journal: a scratch
  event log in its runtime root, emptied at start, holding only rows in
  flight. Its media actor reads the journal as it reads the log today.
- **Playback reports are rows.** The `surface.playback_*` rows the media actor
  writes into the journal go to the brain as events, under their own event
  ids. They join the observer types on the brain's allowlist, so the heard
  prefix and captions fold on the brain unchanged.
- **Listening.** The terminal runs the whole capture session and local ASR. A
  final utterance goes to the brain as one `utterance.received`, tagged with
  the device and idempotent by its id. The brain does not accept it as a
  generic event. Each callback that touches brain state becomes one of two
  kinds:
  - A call over the link: the word judge, turn-in-flight, recent speech,
    cancel, supersede, the run-hold half of hold-output, the
    generation-cancel half of stop-speaking, and conversation mode.
  - A purely local act: playback stop, yield, pause, cues and the
    input-model warm-up.
- **Opt-in per terminal.** Voice runs on a terminal only when asked. On a Mac
  it runs only while that Mac's own daemon does not hold the microphone.

## Alternatives rejected

- **The brain runs the media actor and the terminal only renders**, like the
  native helper of ADR 0129. The ledger is polled every 5 ms. Over the
  measured 80 ms link, every audible-cursor update and every barge-in stop
  would land at least one round trip late. The heard prefix would need a
  discard acknowledgement before it froze, so it would claim words that were
  never played.
- **The terminal calls the provider itself.** This puts the MiniMax key on the
  terminal, which ADR 0170 forbids.
- **The brain transcribes uploaded audio.** ADR 0170 rejects this for a
  terminal that can run ASR.

## Consequences

- A terminal now keeps a journal. It is scratch: emptied at every start and
  never read back as the owner's state. A crash loses only the rows in flight.
- Each answer adds hops. The first provider audio crosses the link once more,
  and the word judge and the reads behind barge-in each pay a round trip. The
  owner's daily voice moves only if a measurement against `all` shows the cost
  is acceptable (ADR 0170).
- While the link is down, the terminal still hears the owner but cannot
  answer. It says that the brain is not reachable, as text does today.
- Captions and the voice face still go to the terminal's own UI until the UI
  connects to the brain.
- Testing on the owner's Mac means stopping its `all` daemon's voice first.
