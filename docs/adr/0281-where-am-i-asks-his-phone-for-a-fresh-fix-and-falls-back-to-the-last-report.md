# ADR 0281 — `where_am_i` asks his phone for a fresh fix and falls back to the last report

**Status:** Accepted
**Date:** 2026-10-15
**Supersedes:** one clause. From ADR 0198, carried by ADR 0212: that the brain never asks the
phone for a location and answers from the phone's latest report alone. Everything else in both
stands, including the age in the answer and the per-device choice of ADR 0212.

## Context

- On 2026-10-14 `where_am_i` answered from a report 18 to 20 minutes old. The phone reports on a
  move of about 500 m or a stay (ADR 0197), so the report was right about where he last was and
  silent about where he is.
- ADR 0198 and ADR 0212 refused to ask the phone: ADR 0197 gives the host no way to wake it, and
  a push "takes seconds at the system's discretion".
- Since then the host has two ways to reach the phone:
  - `/phone/ws` (ADR 0209), a socket the phone opens while the app is in the foreground. iOS
    suspends the app, and its socket with it, soon after he locks the phone, yet the host may
    still list the socket as open for a while.
  - token-based APNs (ADR 0210), whose background push (`content-available`) wakes the app for
    a few seconds if the system allows it and the app has location permission.
- `where_am_i` runs on a worker thread and blocks the turn; a voice turn cannot wait long.
- A fix the phone takes is a location report like any other: the places and stays logic
  (ADR 0234) reads `phone.location_observed`.

## Decision

`where_am_i` asks his phone for a fresh fix and waits a few seconds, and answers with the last
report, and its age, when no fix comes.

Its limits:

- **Over the socket when there is one.** A phone with a live `/phone/ws` is sent `locate` and
  given `locate.socket_wait_s` (4 s). It answers `located` with a fix, or `error`: `denied` or
  `unavailable`.
- **By background push otherwise.** A phone with no socket, or one whose socket stayed silent for
  the whole wait, is sent a push with `content-available` and the request id, and given
  `locate.push_wait_s` (8 s). The woken app posts its fix as a location report carrying the id.
  A phone that answered with an error is not pushed.
- **One request id.** 1 to 64 of `A-Za-z0-9_-`, made per call; the socket's `located`, the push
  and the report all carry it, and an answer for an id nobody waits for is only a report.
- **A fix is always an ordinary report.** It is appended as `phone.location_observed` under the
  phone's name, with `request_id` and an optional `snapshot`, so a fix that arrives after the wait
  still counts for places and stays.
- **The answer says which it is.** `fresh` is true for a fix the phone took for this call, with
  its age; otherwise the result is the last report with its age and why no fix came. The model is
  told which in the note, and says how old the position is when that matters.
- **A snapshot is a picture.** A `located` may name an attachment the phone stored; the result
  carries its id and the note points the model at `look_at_picture`.
- **Not for `bus`.** The start of a bus trip still reads the last report; the wait is for
  `where_am_i` alone.

## Alternatives rejected

- **Keep answering from the last report.** A report was 18 to 20 minutes old on 2026-10-14, and
  on a bus that is kilometres.
- **Ask only over the socket.** The phone is in his pocket, locked, for most of the day, and a
  locked phone has no foreground app to answer on a socket.
- **Wait for the push without bound.** A turn that waits tens of seconds for a phone the system
  has not woken is worse than an answer with the age on it; background pushes are rate limited by
  iOS, so a miss is normal.
- **Keep pushing after a refusal.** `denied` and `unavailable` are the phone's answer; a push
  would wait out the clock for the same reply.
- **Poll the phone continuously.** ADR 0197's battery gate rules out continuous GPS.

## Consequences

- A call can take up to the sum of the two waits (12 s) when the app is suspended with its socket
  open and the push is lost. The settings in `locate:` bound it.
- The phone must have location permission "Always" for a background wake to take a fix, and iOS
  decides whether the push is delivered; a miss falls back and shows only in `not_fresh`.
- A fix with no place name takes the name of a kept place it lies in; unnamed ones have none,
  since the host does not reverse-geocode.
- The location report accepts two more optional fields, so a phone older than this ADR is
  unaffected and a newer one is refused nothing.
