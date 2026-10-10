# ADR 0222 — A phone voice answer always ends, and only its own device follows it

**Status:** Accepted
**Date:** 2026-10-10
**Supersedes:** none

## Context

- Live 2026-10-10 15:24 PT. The brain ran on the Pi, the Mac was a voice terminal (ADR 0172) and
  the iPhone talked over `/phone/ws` (ADR 0209, 0216). Phone turn `T7b125d95bff95338`, 「拜拜爸爸嗯」
  at 15:24:08, was judged a dismissal and its phone had already closed its voice. Its answer
  completed at 15:24:12. No `surface.playback_*` row was ever written for it, and the Mac's
  companion showed "speaking" until it was restarted at 15:28.
- **Why the companion never heard `spoken`.** The companion follows every turn on its push socket:
  a turn's `open` makes it the one she is answering, and only `voice spoken` (or `failed` /
  `cancelled`) ends that. On a Mac running a terminal with `--serve-ui` the socket is the
  terminal's loopback `/inherent/ws`, which relays the brain's push socket and adds the terminal's
  own `voice` ops (ADR 0183). Of the four places that send `spoken`:
  - the brain's response watcher sends `spoken` / `no_voice` after every `done` when it has no
    speech pipeline, as a brain has none (ADR 0170). The terminal drops that frame because its own
    media actor will send the real one;
  - the log watcher that sends `suppressed` for a silent channel does not exist on a brain;
  - the terminal's actor is streamed only the turns the brain speaks to it, and `phone_voice` is a
    silent channel for that stream (ADR 0209), so it never sees the phone's turn;
  - the phone's own actor has no broadcaster.

  So no `spoken` for a phone turn can reach that companion, whether the phone played it or not.
  The companion's `open` and `append` for the turn had already put it on "speaking".
- **Why the turn had no end row.** The phone's media actor is per voice connection and reads the
  turn's rows only while the connection is open. With no actor (a phone that never asked for voice,
  one that left, a host that cannot speak), nothing reads the rows. When the actor closes it ends
  the answer it was playing and says nothing of the answers queued or still buffering behind it.
  An answer with no end row is read by the next turn as heard whole (ADR 0106).
- A playback terminal row needs a session and a playback generation; an answer that never reached
  a lease has neither, and `surface.speech_dropped` is the row for it (ADR 0106).
- The owner's rule, 2026-10-10: things stay on the device he is using (ADR 0218, 0219). A turn's
  device is the name its opening row was written under (ADR 0212, 0217). The host knows which
  device a socket is from the token it carries; the companion does not know its own device.

## Decision

End every phone voice answer that no phone actor will play with the row the actor's ledger would
have written, and send a turn's push-socket ops only to the device that opened it.

Its limits:

- **The end is `surface.speech_dropped`.** Its reason says why: `no_phone_voice` (the answer
  finished while its device had no actor), `phone_disconnected` (the actor closed with the answer
  not ended), or the run's own end, `response_cancelled` / `response_failed`. One row per answer:
  none is written for an answer that already has a playback terminal or a drop.
- **Two writers, split at the actor.** A log-wide watcher ends an answer that finishes while its
  device has no actor built before the answer opened. When a phone's voice closes, it ends what
  its actor held and did not end, once the actor has stopped. An actor that holds an answer ends
  it, as before.
- **Only answers opened after the host booted.** Earlier ones are the boot's reconciliation.
- **A turn's ops reach a client only if that client is the device that opened it, or the turn is
  the host's.** The ops that describe one turn are `open`, `append`, `done`, `voice`, `tool`,
  `failed` and `cancelled`. A turn the host opened (`mac`), a turn with no opening row, and every
  other op go to every client.
- **A push-socket client is the device its token names;** the local key is the host. A client
  registered without a device is sent everything.
- **The companion is unchanged.** It already ends a turn it never hears.

## Alternatives rejected

- **The companion ignores turns of other devices.** It connects to loopback and carries no device
  name; the brain and the terminal's relay would both have to tell it, and an older build would
  still follow everything. ADR 0218 rejected the same split for cards.
- **The terminal stops dropping `no_voice`.** The terminal cannot tell from its journal whether
  the brain streams it a turn: `no_voice` rides the push socket and the rows ride another with a
  50 ms poll, so passing it would end a Mac turn's speaking face before its audio, and a phone turn
  would still show "speaking" from its `open` to its `done`, seconds for a spoken answer.
- **Send the Mac `spoken` for the phone's turn and keep following it.** The Mac would still enter
  "speaking" at the turn's first chunk, which the owner's rule forbids.
- **Keep the phone's actor alive after its socket closes.** Its player is the socket; there is no
  device to play to. A phone with no voice connection never had an actor to keep.
- **Write `surface.playback_interrupted` or `failed` for the unplayed answer.** Both need a
  session and a playback generation an unplayed answer lacks (ADR 0106).

## Consequences

- The Mac companion's live socket no longer carries a phone conversation: its reply text, tool
  line and failures. The conversation of record (`GET /inherent/conversation`) still lists it.
- Whoever opens a socket as a device that is not the opener of a turn never sees it, including a
  second Mac terminal. A device named `mac` still cannot be told from the host (ADR 0212).
- The next turn after an unplayed phone answer is told it was never spoken aloud and only shown
  on screen, which is true: the phone shows the rows.
- The watcher and a closing voice can both find an answer unended at the same moment and each
  write a drop; the projection reads one as unspoken either way.
- A turn's device is read once per turn on the loop thread, from the opening row.
