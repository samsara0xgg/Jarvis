# ADR 0262 — One message is one turn, whichever route it takes

**Status:** Accepted
**Date:** 2026-10-14
**Supersedes:** none

## Context

- On 2026-10-14, on a bus with a weak network, Allen sent one typed message from the phone.
  The send hung and the words stayed in the composer. The conversation then held the same
  words as four records (seq 2733, 2734, 2736, 2739 within about 10 s) and she answered
  three or four times.
- A typed `say` over `/phone/ws` is deduplicated by device and `utterance_id` (ADR 0209), so
  resending it is safe. The phone resends it after 6 s and 10 s, and then goes to
  `POST /inherent/submit` when the socket is not ready or refuses the fields (ADR 0231).
- `POST /inherent/submit` takes no message id. Every call mints a new turn and a new record,
  so a submit that follows a `say` which actually landed (only its acknowledgement was lost)
  writes the words again, and so does a submit that the network delivered twice.
- A phone cannot know whether a message that got no answer was written. Only the brain can
  say, and it can only if the phone names the message.

## Decision

`POST /inherent/submit` accepts the phone's `utterance_id`, the same id its `say` carries,
and writes the words as that `say` would (same record uid, same turn id), so one message is
at most one record and one turn whichever route and however many times it arrives. The field
is optional and only a paired device may send it; a body without it behaves as before.

## Alternatives rejected

- **A separate `client_request_id` field with its own table.** A second key space beside
  `utterance_id` needs its own cleanup and still cannot match a `say` that landed first. The
  `say` uid is already derived from device and id, and reusing it costs no table.
- **Phone-side only: never fall back to HTTP once a `say` was sent.** The `say` may have been
  written or not, and the phone cannot tell. Without the fallback, a broken socket with a
  working HTTP path would lose messages that the brain could have taken.
- **Deduplicating on the words and a time window.** He does send the same words twice on
  purpose ("好" then "好"), and a window long enough to cover a bus signal would swallow them.

## Consequences

- The phone must create the id once per message and reuse it for every retry and for the
  HTTP fallback; an id reused for different words returns the first message's turn and drops
  the new words.
- Old phones, which send no id, keep writing a turn per call.
- The HTTP resend is answered with the first turn id only; it does not return `record_id`.
