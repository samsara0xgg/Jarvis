# ADR 0256 — He and she can put one emoji on a message, and each sees the other's

**Status:** Accepted
**Date:** 2026-10-12
**Supersedes:** none

## Context

- On 2026-10-10 Allen asked for emoji reactions in the phone chat in both directions: he
  reacts to her messages, and she reacts to his. On 2026-10-12 he settled the behaviour:
  - It works like iMessage tapbacks: the six standard ones plus any emoji, one per person per
    message, on every iOS terminal.
  - His reaction never makes her reply. A question mark is no exception.
  - On a typed turn she may answer with a reaction alone. A spoken turn must still be spoken,
    because silence there reads as not heard.
  - A message he sends while she is still answering is marked with eyes ("seen") by the
    program. He knows this from Claude's desktop app.
  - He approved this design on 2026-10-12.
- Records are append-only, and the history sent to the model must keep a byte-stable front
  for the provider cache (ADR 0044). A reaction changes a message after the fact, so it cannot
  be written into that message's history text.
- History carries no record ids (words only). The model cannot name an older message, only
  answer the turn in front of it.
- A tool call costs a model round trip (about 1-2 s on Sonnet low, ADR 0253). The reply
  already carries markup the brain parses (`<voice>`, `<document>`, `stream_envelope.py`).
- A typed message sent during a running turn opens its own concurrent turn (intent pump,
  `max_concurrent_turns: 2`). It is not folded into the running one (ADR 0107).
- Devices can address a message: `GET /inherent/conversation` rows carry a stable record `id`.
  The phone learns the id of his own new typed message only from those rows. The `said` frame
  carries the turn id alone. `/phone/ws` forwards a row only to the device that opened its turn.

## Decision

A reaction is one emoji, by him or by her, on one message. The latest one per person per
message wins, and removing it is a reaction of nothing. Its limits:

- **Stored as events.** Each change is an event `conversation.reaction {target, by, emoji|null}`
  in the log, with `target` a record id. Current reactions are read from the log, and the
  newest event per (target, by) wins.
- **His reaction reaches her as an appended line, not an edit.** When he reacts, a record is
  appended with source `allen_reaction` and role user. It reads `[He reacted <emoji> to: «first 60
  chars»]`, or `[He took back <emoji> from: «…»]`. The history front stays stable. Nothing starts a
  turn; she reads it on her next one.
- **Her reaction is markup in her reply.** `<react>EMOJI</react>` anywhere in her reply puts
  that emoji on his message that opened the turn. The markup is stripped from speech and text.
  A typed-turn reply that holds only the markup writes no text row and plays nothing. A record
  with source `jarvis_reaction` and role assistant (`[You reacted <emoji>]`) keeps it in her
  history. There are two sources because history gives a record the role of its source. On a
  spoken turn the prompt says she must still speak. There is at most one `<react>` per reply;
  the first wins.
- **The program's eyes.** A typed message from him that arrives while another of his turns is
  in flight gets the eyes emoji (U+1F440) by her at once, through the same event. Nothing removes it.
- **Any single emoji.** The brain accepts one grapheme cluster with the emoji property and
  refuses everything else with 400.
- **Device wire:**
  - `POST /inherent/reactions {target, emoji|null}` sets his reaction (paired-device auth).
  - Rows from `GET /inherent/conversation` carry `reactions: {allen?, jarvis?}`.
  - `/phone/ws` sends a `reaction` frame `{target, by, emoji}` to every phone of his, whoever
    opened the turn.
  - The `said` frame with verdict `turn` also carries `record_id`, so the phone can show eyes on
    a line it just sent.
- **Rows that are not messages** (cards, step lines, picture notes) cannot be reacted to.
  Reaction records never show as rows.

## Alternatives rejected

- **A `react` tool.** It adds a round trip, about 1-2 s, to every answer that reacts. A
  reaction-only answer would also need a tool call followed by an empty final turn, which the
  answer gates read as a failed turn (ADR 0240, 0250).
- **Write the reaction into the target's history text.** It changes an earlier message and
  breaks the cached prefix on every reaction. Appending costs one short line.
- **Let her react to any earlier message by id.** History has no ids. Adding them puts tokens
  on every record for a rare case. The one case he named (eyes on a follow-up) is mechanical
  and needs no model.
- **Only the six standard tapbacks.** He asked for "正常 iOS 的消息"; iOS 18 tapbacks take any emoji.

## Consequences

- Day summaries, recall and the conversation tool now see reaction records among his words.
  They read as short bracketed lines and are not filtered out.
- A reaction-only typed answer leaves her side of that turn without a text row. A device that
  shows only text rows shows nothing there. The Mac app (`desktop/resonance`) must draw
  reactions before it shows this turn correctly.
- Concurrent turns can both answer a message that already got the eyes. The eyes mean "seen", not
  "answered together".
