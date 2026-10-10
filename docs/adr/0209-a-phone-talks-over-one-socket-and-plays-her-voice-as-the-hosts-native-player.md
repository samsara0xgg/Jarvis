# ADR 0209 — A phone talks over one socket and plays her voice as the host's native player

**Status:** Accepted
**Date:** 2026-10-10
**Supersedes:** none

## Context

- On 2026-10-10 Allen asked that the phone app do everything at once ("手机上得要把所有功能都给它接进去，一次性来吧"),
  voice included, built in step with the app ("你跟他同步一起做吧"). The phone talks to the Mac running alone
  (ADR 0207) now and to the brain later.
- The phone runs its own wake entry points and ASR (Apple's on-device recognizer, decided
  2026-10-09), so it sends text. iOS gives it echo cancellation in the voice-chat audio session, so
  it hears Allen while she speaks.
- Her speech pipeline on the host is one media actor. It reads the answer's `surface.response_*`
  rows and splits them into speakable segments. It drives the MiniMax session (the key stays on the
  host, ADR 0170) and owns the player with its playback ledger. From that ledger it writes the
  `surface.playback_*` rows, which the heard prefix and the captions fold from.
- ADR 0129 already splits that player in two. The ledger, leases and heard-prefix accounting stay in
  Python, and a Swift helper renders the samples. The helper takes PCM, ACTIVE, DISCARD, GAIN and
  HOLD frames. It sends READY, REPORT (what played and when), STATUS and DISCARD_ACK. Python
  accounts from the reports unchanged, and an interrupt freezes only after the helper acknowledges
  the discard boundary. The helper is Swift, written for CoreAudio.
- ADR 0172 instead runs the whole media actor on a terminal and proxies the provider session. That
  terminal is the Python daemon. A Swift client would have to rewrite the segmenting and text
  cleanup, the provider session and the playback-row chain, and keep them in step with Python
  forever.
- A Mac running alone has no terminal link (ADR 0207). Its own media actor plays every turn that is
  not typed, on the Mac's speakers.
- The phone wakes for seconds in the background (ADR 0197), but during a conversation it holds an
  active audio session and stays running.

## Decision

A phone holds a conversation over one WebSocket on the host it paired with. It sends text, and it
plays her voice as that host's native player, over the socket instead of a pipe.

Its limits:

- **One socket, a device token only.** The phone conversation route opens on any host that listens
  beyond loopback (a brain, or a Mac running alone). It takes only a paired device's token, as the
  phone events route does. One phone connection is live per device, and a new one replaces the old.
- **The phone sends what it heard as text.** A final utterance is written once as
  `utterance.received` on a channel of its own. The phone mints the utterance id, so a resend writes
  nothing twice. A typed message goes on a typed channel and is answered without being played (ADR
  0181).
- **Her voice for the phone's turns is rendered by the host and played by the phone.** For each
  voice connection the host runs one more media actor, unchanged except for its player. The player
  sends the ADR 0129 frames to the phone and accounts from the phone's reports.
  - The phone timestamps its reports with its own clock. The host maps them to its clock with the
    smallest gap it has seen between a phone timestamp and its arrival. That errs late, so the
    heard prefix may lag but never claims a word the phone has not played.
  - The heard prefix, the captions and the playback rows come out of the host's ledger as they do
    for the Mac's speakers.
- **Each device speaks its own turns.** A turn opened from the phone is played only on the phone,
  and the Mac's own media actor leaves it alone. A turn opened on the Mac is never sent to the
  phone's speaker.
- **Barge-in stays exact.** When the phone hears Allen over her voice, it holds its output at once
  and tells the host. The host interrupts as it does locally: it cancels the generation, sends
  DISCARD, and freezes the heard prefix only after the phone's DISCARD_ACK.
- **The answer's text reaches the phone as rows.** The phone receives the response rows of its own
  turns, to show them as it plays.
- **The phone ends its own conversation.** On the Mac, dismissals and holds (ADR 0102) are judged
  inside the capture session, before an utterance reaches the log, so a phone utterance meets no
  judge on the host. The phone keeps its own short list of dismissal and hold words and its silence
  timer, and the host sends no end-of-conversation frame.

## Alternatives rejected

- **Run the media actor on the phone, as ADR 0172 does on a Mac terminal.** The phone would port the
  segmenter, the text cleanup, the provider session protocol and the playback-row chain with its
  hashes to Swift. Every later change to how she speaks would then need two implementations kept in
  step. The ADR 0129 helper is the only part already written in Swift, and it is about 800 lines.
- **Send the phone a finished audio file per answer.** The first audio would wait for the whole
  reply, which costs seconds against the streamed first chunk. Barge-in could not freeze the heard
  prefix at the word where Allen cut in.
- **Have the host infer what the phone played from what it sent.** Over the roughly 80 ms Tailscale
  relay measured in ADR 0172, a stop lands a round trip late. The heard prefix would then claim
  words the phone never played. The phone's own reports and its discard acknowledgement are what ADR
  0129 relies on for exactness.

## Consequences

- Each answer's audio crosses the network, 64 KB/s as 32 kHz int16. Over a cellular relay that is
  one more way for a turn to stutter. The phone's ring buffer absorbs jitter at the cost of a later
  first sound.
- The host runs a second media actor and a second provider session while the phone is in a
  conversation.
- The phone's player must keep the ADR 0129 report and discard semantics exactly. A change to that
  protocol changes two Swift renderers.
- A closed Mac lid ends the conversation. The phone says the host is unreachable.
