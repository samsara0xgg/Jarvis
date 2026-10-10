# ADR 0218 — A card shows and pushes only on the device it belongs to

**Status:** Accepted
**Date:** 2026-10-10
**Supersedes:** two clauses. From ADR 0153: the daemon never holds a permission prompt from a
`~/Projects` session, and the notch never shows one. From ADR 0210: a waiting card or Claude Code
prompt is pushed to every registered phone without an open socket. Everything else in both ADRs
stands.

## Context

- The owner, 2026-10-10:
  - 「我在手机上用的话，千万不要把卡片推到电脑上了，除非我明确要求。然后电脑上用的话，暂时也不要推到手机……目前来说先只做本机的」;
  - 「project 的这个你先把它搞定吧……任何卡住的都可以让我来给它权限」.
- The confirmation card and the ask card are one slot each, and every device reads them. The
  Mac's notch popped a card that a phone turn raised. A card that a Mac turn raised was pushed to
  the phone whenever the phone's conversation socket was closed (ADR 0210).
- A card's turn has an opening row naming its device (ADR 0212, ADR 0217). Two kinds of card
  have none:
  - one raised with no turn, such as the Dashboard's mail send (ADR 0148);
  - one from a turn with no opening row.
- Answering a card starts a turn. That turn was written under `mac` whichever device answered, so
  a card it raised followed the wrong device.
- Claude Code prompts come from sessions on the Mac. ADR 0153 left `~/Projects` prompts unheld
  and off the notch:
  - those are the project threads' Remote Control sessions;
  - their many pops crowded the notch;
  - Claude's app shows the same prompt.

  Those sessions now stall unseen, because the owner rarely notices a prompt in Claude's app.
- Whether he is at the Mac is TimeSink's presence, read through the moment (ADR 0161). It is
  `active`, `idle`, `locked` or `asleep`. On a brain, reading it is a terminal round trip. It is
  unknown when the Mac is shut or TimeSink cannot be read.
- On the phone, a push button answers a Claude Code prompt only when exactly one waiting prompt
  has the push's words. Otherwise the phone asks him to open the app.

## Decision

Show and push a card only on the device it belongs to:

- a card a turn raised belongs to the device that turn came from;
- a Claude Code prompt shows on the Mac, where its session runs, and is pushed to the phone only
  while he is away from the Mac;
- any other card goes to where he is.

Its limits:

- **Reads.** A confirmation or ask card is served only to reads from the device whose turn
  raised it, and every other device reads none. A card with no device is served to every
  device. Claude Code prompts are on every board read: the Mac is where the session runs, and
  the phone is its remote.
- **Pushes.**
  - A card from a turn is pushed only to that device, and only when it is a phone with a push
    registration and no open conversation socket. A computer's card is never pushed.
  - A card with no device is pushed to the phones only if he is away from the Mac when it is
    asked.
  - Away means TimeSink does not say `active`. Unknown counts as away, so a stalled session is
    never left silent.
- **A held Claude Code prompt follows him.** If he is at the Mac when it is held, it is pushed
  once he leaves, while it still waits. The check runs with the push watcher, every 5 s. Each
  prompt is pushed at most once.
- **Answering a card starts its turn under the device that answered.**
- **`~/Projects` prompts are held and shown like any other.** Their sessions' other pops (done,
  error, stopped) stay off the notch.
- **No card is sent to another device on request.** "Unless I ask" waits for its own decision.
- **Quiet levels and holds are unchanged.** From `no-pop` up, nothing is held or pushed. The
  moment's holds and the "in Claude" rule of ADR 0153 apply as before.

## Alternatives rejected

- **Each client filters the cards by device.** The companion and the phone app live in two
  repositories and would both have to agree. An old build would show everything. The push sender
  needs the same rule anyway, and the host knows which device made every request from its token.
- **Push every card to the phone whenever the Mac is idle.** A card from a Mac turn would follow
  him to the phone, which the owner ruled out.
- **Always push `~/Projects` prompts and keep them off the notch.** While he sits at the Mac, his
  phone would buzz for a prompt that a glance at the notch could answer.

## Consequences

- A card raised by a phone turn does not appear on the Mac, even after he sits down at it. He
  answers it on the phone, or in words to her.
- When the moment is unknown, every Claude Code prompt and every card with no device is pushed.
  This happens with TimeSink off, unreadable, or the Mac shut.
- A Claude Code prompt reaches the phone some time after he leaves the Mac: up to TimeSink's idle
  threshold, plus 5 s.
- Several `~/Projects` prompts waiting at once share the words "tool · Projects". A lock-screen
  button then asks him to open the app instead of answering.
- A held `~/Projects` prompt also shows in Claude's app, and the first answer wins (ADR 0049).
