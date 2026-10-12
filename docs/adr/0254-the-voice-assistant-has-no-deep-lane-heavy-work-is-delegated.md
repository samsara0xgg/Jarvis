# ADR 0254 — The voice assistant has no deep lane; heavy work is delegated

**Status:** Accepted
**Date:** 2026-10-12
**Supersedes:** 0251

## Context

- ADR 0251 shipped a background deep lane on 2026-10-12 00:34: `think_deeper(task)` and the
  `llm.deep.on_words` phrases handed a task to Sonnet at high effort on its own channel and
  worker, with up to 40 tool calls and a 20-minute deadline, answering when ready.
- It repeated what Jarvis already did. ADR 0107 lets a turn keep looking something up while
  Allen asks other things and says what it answers when it is done; ADR 0019 delegates work to
  Codex workers. What 0251 added was larger limits, a stronger effort, a phone push when he has
  left and a notice after a restart.
- Heavy turns are rare: ADR 0178 counted 5 of about 780 voice turns since 2026-09-25 using more
  than 8 tool calls; the 2026-10-12 whole-turn replay on the Pi (87 real turns, real tools) never
  went past 4 calls on Sonnet.
- Allen, 2026-10-12: a voice assistant should not carry that load. Work that needs that much
  belongs to a coding agent such as Claude Code or Codex, which runs on his subscription and has
  the Mac's files, instead of a per-token API run inside Jarvis. How Jarvis hands work to them
  is not decided yet.

## Decision

The deep lane is removed: `think_deeper`, the `deep` channel and its worker, `llm.deep`, the
`sonnet-deep` preset and their tests are reverted to the state of `8f9dae3d`. A turn that needs
more time stays an ordinary turn under ADR 0107. ADR 0108's on-words keep thinking harder for
their own turn in the foreground. Heavy work is to be delegated to a coding agent; that design
waits for Allen.

## Alternatives rejected

- **Keep the deep lane.** It runs a second set of rules beside ADR 0107 (barge-in, busy, notices,
  confirmations), and its review found ten defects that came only from the second lane; at 5 heavy
  turns in about 780 the lane would rarely run, and each run costs API tokens a subscription agent
  would not.
- **Raise the voice turn's tool limit instead.** The same rare need, paid for on every voice turn
  that spins: ADR 0178 lowered the limit because he waited in silence.

## Consequences

- An ask that needs many lookups is answered within the voice turn's limits (ADR 0178) or not at
  all until delegation exists.
- The phone push for a result he left behind and the restart notice that 0251 built are gone with
  it; if ADR 0107's slow answers turn out to need them, they are added there.
