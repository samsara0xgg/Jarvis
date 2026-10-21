# ADR 0361 — A tool decides each call's effect from its arguments and completes a call that needs a card, so the card shows what will run

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** in ADR 0321, a tool's effect being one value for all its calls; in ADR 0011 D2, `requires_confirmation` as one value per tool

## Context

- A tool has one risk level (ADR 0011 D2). `requires_confirmation` is one value per tool and is
  checked against that level at boot (`validate_requires_confirmation`). The gate, the card and
  L4's lease check (ADR 0321, I7) all read the tool's level.
- ADR 0319 already decides per call, from its arguments, whether a call reads or writes. The
  spec's tool standard puts a thing's look, add, change and cancel in one tool with an `action`
  enum (`docs/spec.html#tools`).
- Mail is one thing whose calls differ:
  - a search only reads;
  - a draft or an archive changes only his own mailbox and can be undone;
  - a send speaks for him to someone else and cannot be taken back.
- On 2026-10-21 Allen approved the plan's 「起草和发送都放进邮件工具」 (「就按你说的去吧」).
- A card is built from the frozen call's arguments as the model wrote them (ADR 0062, 0321). The
  letter card shows the model's `to`, `subject` and `body`.
- 2026-09-26/27: a letter meant for a friend went to his own UVic address.
  - The only address in the model's context was his own, written into `gmail_send`'s description
    (ADR 0063).
  - A name the model turns into an address itself can go wrong the same way.
- The standard takes people's names as his words and has the tool resolve them
  (`docs/spec.html#tools`).

## Decision

Decide each call's effect from its arguments. Let the tool complete a call that needs a card
before the call is frozen, so the card shows what will run.

- **Effect per call.**
  - A tool may declare each call's effect from its arguments:
    - a read (L0);
    - a change he can undo inside his own things (L1);
    - an outward or irreversible act (L3).
  - A tool that declares no per-call effect keeps one level for every call.
  - The gate, the card, L4's lease check and the receipt all take the call's effect.
  - Tier 0 and the boot check use the effect of the arguments a row fixes.
- **Completed before the card.**
  - A call that needs a card goes first to the tool's completion. The completion resolves what
    the model named:
    - a person's name to an address;
    - a reply to its thread and recipients;
    - a draft to the letter it holds.
  - The completion returns the full arguments. They are frozen, the card shows them, and dispatch
    runs them, with any edit he makes on the card (ADR 0062).
  - A completion that cannot resolve ends the call with a failed receipt and makes no card:
    - two candidates or more: needs_him, naming them;
    - nothing found: impossible, saying what to ask him for.
  - A completion only reads.
- His per-plugin and per-tool approval modes still override (ADR 0033). Plugin tools keep their
  hints and manifests (ADR 0321).

## Alternatives rejected

- **A second tool for sending, at L3.**
  - It keeps the per-tool level but splits one thing in two, which the standard forbids.
  - A reply takes three to four calls across four mail tools today.
  - The gate would stay the one place that decides per tool, after ADR 0319 moved read and write
    to the call.
- **The model writes the resolved address into the call.** The card then shows what the model
  typed. That is how the 09-26 letter went to his own address.
- **Resolve at dispatch, after the tap.** He would approve a name. The letter could then go to an
  address he never saw.

## Consequences

- A tool whose calls differ must declare them correctly. A wrong declaration runs an outward act
  with no card. The declaration is code, reviewed like a manifest.
- The completion is a read before the card, so a send waits for it.
- The boot check cannot prove a varying tool's consent. That tool's tests must cover each action.
