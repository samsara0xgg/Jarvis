# ADR 0236 — A phone's late discard ack settles the interruption from the reports in hand

**Status:** Accepted
**Date:** 2026-10-11
**Supersedes:** the barge-in clause of ADR 0209: that the host freezes the heard prefix only after the phone's DISCARD_ACK.

## Context

- ADR 0209 gave the phone the ADR 0129 player protocol. On an interruption the media actor sends
  DISCARD, then waits for the DISCARD_ACK before it freezes the heard prefix. Every REPORT for a
  sample played before the DISCARD arrives before that ack.
- The actor gives the settle half a second. The framed player waits 50 ms per try, because the Mac's
  helper acks one render callback (about 11 ms) after the frame.
- If the settle does not come, the actor fails closed. It isolates its lane, and every later answer
  is dropped unsaid. For the Mac's helper that is right: an ack that never comes means the helper is
  wedged, and the actor shuts down.
- A phone is a network away. ADR 0172 measured about 80 ms over a Tailscale relay. A phone that
  heard Allen over her also holds its output (ADR 0216), and a held renderer may not run the
  callback that acks.
- On 2026-10-11 at 12:29:34.616 PT the Pi isolated a phone's lane with reason
  `callback_publication_unsettled` (event 80742). This is reproducible by
  `test_a_turn_said_over_her_to_a_phone_that_acks_late_stops_her_and_the_next_answer_plays`
  with the fix reverted. The words he said over her became a turn whose answer was emitted
  2 s later and never played. The phone waited, heard nothing, and went back to listening. Both
  answers ended as `speech_dropped` when it disconnected. It was the only isolation in the Pi's
  log.
- The ack adds nothing to the heard prefix when the phone held first. Its REPORTs went out on the
  same ordered socket before the words it said over her. It adds at most a round trip of audio
  when a stop reaches a phone that is still playing.

## Decision

The phone's player waits up to 0.3 s for the DISCARD_ACK, inside the actor's settle window. Without
the ack it settles from the REPORTs already in. The interruption is then written and the lane stays
open for the next answer.

- A phone that holds its output acks a DISCARD as soon as it receives it.
- The Mac's helper is unchanged: a late ack is still retried, and a missing one still isolates its
  lane.

## Alternatives rejected

- **Wait longer for the ack.** No bound covers a held phone that does not render, and the actor's
  half-second window would have to grow for the Mac too. The lane would still close on the first
  ack that misses it, as on 2026-10-11.
- **Rebuild the phone's actor after an isolation.** It keeps the fail-closed path for a peer whose
  lateness is the network's, not a fault. The answer in flight is still lost, and the phone gets
  no audio until the rebuild.
- **Freeze at once, without waiting for the ack.** A stop that reaches a phone still playing would
  lose a round trip of heard words on every interruption, not only when the ack is late.

## Consequences

- When the ack is late, the heard prefix can fall short of what the phone played by up to a round
  trip of audio. It is never longer than what the phone played.
- The actor's loop is busy for up to 0.3 s while it waits. That loop is this phone's alone.
- A REPORT that arrives after the settle does not change the interruption already written.
