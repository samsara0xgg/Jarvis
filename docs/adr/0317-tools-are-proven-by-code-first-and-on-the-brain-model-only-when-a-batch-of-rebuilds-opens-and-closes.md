# ADR 0317 — Tools are proven by code first, and on the brain model only when a batch of rebuilds opens and closes

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** 0290

## Context

- ADR 0290 (2026-10-17) holds every tool to `docs/spec.html#tools`. It lets a new or rebuilt
  tool in only with five items from his real conversations, each passing three runs of three
  on the current brain model, with no regression on the rest of the exam. Held to the letter
  for the 49 built-in tools `check_tools` sees, one pass of that exam is 735 brain turns: about
  $54 at the 2026-10-20 mean of $0.073 per turn.
- On 2026-10-21 the owner capped all paid API spend at $30–50 (「你最多只能用30到50块」). The
  same day he set the order: the cheap model while building, the brain model at the start and
  for the last pass (「动手前面可以先用海苦，然后最后一遍再考虑用 Sonnet…开工和最后也行」).
  Every brain-model run already needs his OK. The `haiku` preset costs a twentieth of the
  brain's per token.
- In his real use from 2026-10-12 to 2026-10-20, 20 of 43 tool asks went wrong and 4 went half
  right. 14 of the 20 were facts a tool returned wrong or short: a reminder left out of a read,
  a radius ignored, an eight-hour-old position, a bus dropped. The tool repeats each of these on
  the same inputs with no model. The rest were her saying done what was not, one card asked
  three times, the claim check misfiring, and asking what she could look up.
- The exam today runs whole turns only, so every item is a paid call. `tool_exam_steps` reads
  providers live at run time, so an item's right answer moves with the clock. Its `--fake` mode
  already sends scripted replies through the real `decide()`.
- `check_tools` and the exam read a hand-assembled copy of the daemon's registry. It has
  drifted. The daemon serves `archive`, whose description is 1,039 characters, and the worker
  tools; the gate checks neither.
- The spec's rules for a result and an error are checked nowhere: a speakable summary first,
  about 4,000 characters, no uuid, path or hash, one of four error kinds. One fault comes back
  as `invalid_argument` (43 sites), `invalid_arguments` (50) or `bad_arguments` (1).
- Exam items carry his words and his data. Nothing may be typed into his live conversation to
  test her (owner, 2026-10-10).

## Decision

Prove every tool in three layers, cheapest first and judged by code, and run her whole turns on
the brain model only when a batch of rebuilds opens and when it closes.

- **One standard, gated on the real menu.** Every tool the model can call is held to
  `docs/spec.html#tools`. In the gate, `scripts/check_tools.py` checks three rules on the
  catalog the daemon serves: a lowercase snake_case name, an English description of at most
  600 characters that says "he", and a description on every parameter. The exam reads the same
  catalog, and no copy of it exists. Tools that broke a rule when the standard was set
  (2026-10-17) are listed with those rules, and each leaves the list when it is rebuilt. No tool
  written after that date joins it. Plugin tools (`mcp__`) are their servers' definitions and
  are not checked.
- **Tool layer.** An item holds:
  - his words, and the moment and place he said them;
  - the arguments;
  - what each provider answered, recorded once;
  - the facts expected: what the result must hold, what must change in state, what must not.

  The tool runs on a copy of his data with the clock set to that moment. A provider call the
  recording lacks fails the item. Code checks the facts and the spec's rules for a result and an
  error. No model is called.
- **Plumbing layer.** Scripted model replies run through the real decision loop. Code checks
  what the program does with each: a claim no call backs, a card, a bad argument, an error. No
  model is called.
- **Whole-turn layer.** It holds only what the model's own choice decides: looking instead of
  asking, which call a claim rests on, a correction across turns. It stays small.
  - While a batch is built, and for any change outside a batch, it runs on the `haiku` preset.
    An item that passed on Haiku's last run and now fails blocks the change.
  - On the brain model it runs once before a batch's first change, and again to accept the
    batch. At acceptance every item passes three runs of three, and none that passed the first
    run fails.
- **Entry.** A new or rebuilt tool lands with two things:
  - an ADR: the need, three of his phrasings, why the per-turn state and the existing tools
    cannot answer, and where the tool sits;
  - at least five tool-layer items from his real conversations, all passing.

  No tool-layer or plumbing item that passed before may fail after.
- **Held out.** Each layer's items are split in two: a set the builder reads, and a set only the
  runner reads. Both count.
- **Spend.** A brain-model run is quoted from its items' real prompt lengths, and starts only
  on the owner's OK. The runner meters each response's usage and stops at the run's cap. No paid
  call judges an answer. What code cannot judge goes to a Claude Code subagent, and a split
  verdict goes to the owner.
- **Where it runs.** Items and recordings that hold his words or his data live outside the
  repository, beside that data. They run on a copy of it, never in his live conversation.

## Alternatives rejected

- **Keep 0290's entry rule.** One pass is about $54 (735 brain turns at $0.073), more than the
  whole paid budget, and the rule asks for a pass per rebuild.
- **Whole turns on Haiku only.** These items exist because the brain model's own choices decide
  them. Haiku choosing right does not show what the brain model chooses. Two models that gave
  the same verdict on every run of every item would refute this.
- **A paid model as judge.** A judge reads the whole turn, so it costs about as much as the
  turn it judges and doubles every run. Code reads the state change, the calls and the shape of
  the result exactly.
- **Live provider reads in the exam.** The right answer to 「明天几点日出」 or to the next bus moves
  with the clock. An item written on 2026-10-20 fails on 2026-10-22 with no change to the tool.
- **Keep the hand-assembled registry and fix its drift.** It missed `archive` and the worker
  tools while the gate stayed green. A copy needs each new tool registered twice.

## Consequences

- A tool-layer item is only as current as its recording. A provider that changes the shape of
  its answer passes the exam until the item is recorded again.
- Between a batch's opening and closing, only Haiku sees her behavior. A description or prompt
  change that hurts the brain model shows at acceptance, not when it lands. A change outside a
  batch waits for the next batch to meet the brain model.
- The whole-turn set is small. Behavior outside it can regress unseen until his use shows it.
- Items from his conversations need a copy of his data. They run where that data lives, or on
  a copy taken from there; a cloud checkout alone cannot run them.
- A batch cannot close without the owner's OK for its brain-model run.
- The first check on the real menu finds the tools the copy hid, `archive` among them. They
  broke the rules when the standard was set, so they join the list once, at that switch. After
  that the list only shrinks.
- The script checks form, not judgment. Overlap and honest read-only labels stay with review.
  The system prompt holds the shared rules once, and a tool description may not repeat them.
  Changing those rules is a prompt change, which goes to the owner first.
