# ADR 0153 — Quiet modes are one daemon-owned level the surfaces read

**Status:** Accepted
**Date:** 2026-10-04
**Supersedes:** none

## Context

- Allen, 2026-10-04 ("可以，就这样"): the notch pops, cards, cue sounds and
  Claude Code prompts all arrive while he works in front of Claude Code, and
  the only control is muting every sound. He wants whole-computer quiet in
  steps, switched by voice or her right-click menu, never switching itself off.
- `docs/spec.html#attention` says the first real mode needs an ADR first.
- The companion front end must not own the answer: it renders what the daemon
  says, as it already does for the mute switches (ADR 0015). A second copy in
  `localStorage` disagrees with the daemon after a restart or on another
  surface.
- Claude Code permission prompts are held for the notch only while a companion
  read the board in the last 10 s (`claude_hooks.LISTENER_S`); a prompt held
  for a surface that shows nothing stalls the agent unseen.
- A fixed-phrase verdict (ADR 0102) is only tested in conversation mode or
  over her voice, and a bare 安静 already means "stop talking" over her voice.

## Decision

Jarvis keeps one persisted level, `off`, `quiet`, `no-pop` or `dnd`, in the
daemon; every surface reads it from the controls state and none stores it.

Its limits:

- The levels are cumulative. `quiet`: she never speaks unprompted and every
  cue sound is off, cards and pops still show; he still gets her voice when he
  talks to her. `no-pop`: also no cards and no name pops, the agent marks
  still update, and Claude Code prompts are not held for the notch. `dnd`: also
  no visible reaction to anything external, everything is held, and what is
  held arrives together when he leaves `no-pop` or `dnd`.
- It is set by `POST /inherent/controls {"quiet": level}`, by fixed phrases
  tested before every other verdict and answered with a fixed line, with no
  model and no turn, and by her menu. It survives a restart and never ends by
  itself.
- A phrase wins over the stop meaning of 安静: only whole 安静一点 and
  安静模式 switch it; a bare 安静 or 安静一下 still only stops her.
- Reminders he set himself are not held by any level.

## Alternatives rejected

- **A preference in the companion's `localStorage`** — it is lost on a profile
  reset and never reaches the daemon, which must decide the prompt hold and
  her own speech; two copies disagreed after every daemon restart in the
  mute-switch history that ADR 0015 replaced.
- **Four independent switches** — the three he approved only make sense as a
  ladder (`dnd` without `quiet` would still ring); independent flags allow 16
  states of which 3 are meaningful.
- **Let the LLM interpret "安静一点"** — a turn costs seconds and a model call
  to flip a flag, and it may answer instead of doing it; the dismissal
  phrases (ADR 0102) already avoid that.

## Consequences

- The controls state and its tests gain a field; every client of
  `/inherent/controls` sees it.
- Any future unprompted speech must read the level first; none exists today.
- A held prompt released to Claude Code's own dialog cannot be answered from
  the notch until he leaves `no-pop`.
- Typed text does not pass the fixed phrases; typing "勿扰" reaches the model.
