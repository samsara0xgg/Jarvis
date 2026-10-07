# ADR 0181 — A typed turn takes the spoken route and is never played

**Status:** Accepted
**Date:** 2026-10-07
**Supersedes:** none

## Context

- A turn typed into the talk field arrives as `surface.user_intent` on channel
  `cli_stdin`. It ran the batch `chat` loop: a non-streamed request with no
  service tier and none of the spoken route's request shape, so it also shared
  less cached prompt prefix with spoken turns. A spoken turn goes out on the
  Responses API, streamed, with the `spoken_reply` format (ADR 0114).
- The batch answer is one prose text with attention channel `voice_notify`, so
  it was played and, when long or marked up, first rewritten to a spoken form
  by a second model request (ADR 0045). The card showed the rewrite and, below
  it, the whole answer: the same content twice. One rewrite on 2026-10-07 also
  came back in Chinese while `reply_language` was `en`.
- Allen, 2026-10-07: a typed turn looks like a spoken one (`spoken` as her
  lead line, `written` below) and goes out the same way, but is silent.
- ADR 0166 gives the Fast service tier to a spoken turn because the first
  spoken word is waited on, at a premium price, and says a typed turn has a
  screen to wait on. ADR 0178 bounds a typed turn by `llm.max_tool_iterations`.

## Decision

Run a turn Allen typed (`surface.user_intent` on `cli_stdin`) on the spoken
route, with the spoken request shape and the same reply note, but start no
playback for it and make no spoken-form request for it; everything keyed on
his voice (the voice tool cap, the Fast service tier, Jev's parallel start)
stays keyed on a turn he spoke.

## Alternatives rejected

- **Keep the batch path and only drop playback and rewrite.** It removes the
  doubled card but leaves the request off the spoken shape: on 2026-10-07 the
  typed turn Tb6f3e503 was a non-streamed `chat` request while the spoken
  T1664e252 was a streamed Responses request, so the two never share the
  cache prefix.
- **Give typed turns the Fast tier too.** The ADR 0166 measurement is the first
  token before the first spoken word (1.04 s to 0.68 s median); a silent card
  has no such word, and the tier is billed at a premium.
- **Leave the rewrite in place and hide the written part.** The rewrite is a
  model call for text nobody hears, and it ignored `reply_language` once.

## Consequences

- Allen cannot get a typed question answered aloud; he must ask by voice.
- `cli_stdin` is in the speaker's silent set, so "exit" from the surface no
  longer cancels a typed turn's answer in flight; nothing of it can speak.
- The state block tells the model a typed turn is on "Channel: voice" and that
  `spoken` is read aloud, which is not true of it; the shared note is kept
  unchanged so the cached prefix is shared.
- A typed turn that falls back to the batch path (provider is not OpenAI, or
  the route is off) still makes no rewrite request and plays nothing.
