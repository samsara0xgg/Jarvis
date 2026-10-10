# ADR 0217 — Each turn says which device it came from and what the host knows of the others

**Status:** Accepted
**Date:** 2026-10-10
**Supersedes:** none

## Context

- Since 2026-10-10 the brain runs on the Pi, and the MacBook and the iPhone are its devices
  (ADR 0170). A turn can come from either. Typing on the phone and typing on the Mac use the same
  channel, so the state block's channel line cannot tell them apart. The device is recorded only
  as the name its opening row was written under (ADR 0212).
- Until now the state block said only where she runs and which terminals are connected. A phone
  is not a terminal, so on a phone turn she was not told the phone was there at all. On
  2026-10-10 she answered a phone turn as if he were at the Mac.
- What she knows about "now" sits in five places, each computed on its own:
  - the state block's lines;
  - the ledger (ADR 0201);
  - tools she has to decide to call (`where_am_i`, `query_activity`);
  - the moment, which only alerts read (ADR 0161);
  - the push sender's socket check (ADR 0210).
- On 2026-10-10 the owner approved one module on the host that works "now" out by code and that
  every consumer reads: 「派上做一个「此刻」模块 go 改」.
- On a brain, any fact from the Mac is a round trip over the terminal link, about 80 ms over a
  relay (ADR 0183), and a turn would wait on it. The event log, the paired devices, the terminal
  roster, the phones' conversation sockets and the push registrations are all on the host.
- The phone's latest place is already in the log (ADR 0197), and its daily places already ride
  the system prompt (ADR 0201). Coordinates are read only by `where_am_i`.

## Decision

Work "now" out on the host, by code, from what the host already holds, in one module that both the
turn's state block and the card routing of ADR 0218 read. The state block says three things: the
device the turn came from, each paired device with what the host knows of it, and the phone's
last named place with its age.

Its limits:

- **Facts only.** No line tells her what to do with them.
- **The origin is the opening row's device** (ADR 0212). It is named only for a turn that has one
  and was opened by a paired device or, on a Mac running alone, by the Mac itself.
- **A device's kind comes from what it did.**
  - One connected now as a terminal is a computer.
  - One registered for push, or with a conversation socket open, is a phone.
  - Anything else is named without a kind.
- **Nothing on the turn's path asks a terminal.** Whether he is at the Mac (TimeSink's presence,
  through the moment) is read only when a card is routed, off the turn. She still reaches the
  Mac's activity through `query_activity`.
- **The place line carries the place's name and its age, never coordinates.** A latest report
  that is a departure says he left that place. A report with no place name gives only its age.
- **The quiet level and the conversation state stay out of the prompt.** What she says to his
  own words does not change with either.

## Alternatives rejected

- **Add only the line the deployment thread proposed**, "he is talking from the iPhone". It
  covers one kind of turn. Push and the notch would still each decide the device on their own,
  and they disagreed: a card from a phone turn popped on the Mac's notch, and a card from a Mac
  turn was pushed to the phone.
- **Read the Mac's presence on every turn.** Each turn would pay the terminal round trip, about
  80 ms over a relay (ADR 0183), and wait the link's whole timeout when the link stalls. When the
  Mac's state matters to an answer, `query_activity` reads it.
- **A tool she calls to learn where he is talking from.** She would decide to call it from his
  words alone, and that is the same guess that produced the 2026-10-10 answer.

## Consequences

- The phone's place name now reaches the provider on every turn
  (`docs/spec.html#egress`). Before, it went only when `where_am_i` was called, or as the day's
  longest place in the ledger.
- A device paired but neither connected nor registered appears by its name only. A computer whose
  terminal is down reads like any other unconnected device.
- A device named `mac` still cannot be told from the host's own rows (ADR 0212). On a brain, a turn
  opened under `mac` has no origin line.
- Any consumer that needs a fact not in the module adds it to the module, not to its own reading.
