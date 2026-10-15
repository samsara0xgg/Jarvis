# ADR 0263 — A sent-back answer is never spoken, and the state block backs what it shows

**Status:** Accepted
**Date:** 2026-10-14
**Supersedes:** the clause of ADR 0240 that lets the first answer stay spoken when the spoken route sends it back, and its Consequence that Allen hears a second answer after the first. The rest of ADR 0240 and ADR 0250 stands.

## Context

- Live, 2026-10-14, a Claude Sonnet preset on the unstructured spoken route (ADR 0241), typed turns while a
  bus ride was followed. Several replies were two answers glued together, the second contradicting the
  first: 「下车站已经设成 X…提醒已经设好了。你在 X 下车，还有 11 站…」, 「…这一轮我没有重新设」, 「我刚重新设了一次下车站，
  这次是真的设上了」.
- The ride's stop had been set turns earlier and the state block said so (`Bus ride: … His stop: X`, ADR
  0246). Asked again, she answered from it, saying the stop and the reminder are set. No call was made in
  that turn, so ADR 0240 read 「设好了」 as a claim no write backs and sent the answer back. The answer was
  already heard (the spoken stream feeds the speaker as text arrives), so the second request wrote a second
  answer after it, and called `bus(get_off=X)` again "to back" a state that was already true.
- `bus(get_off=X)` rewrote the same stop each time. ADR 0240's repeat block lives in the turn's calls
  (`_Scratch.calls`), so it never sees a call from an earlier turn.
- The assembler holds a sentence's end until one more character follows it, and speech has no recall.

## Decision

On the spoken route hold the text that may carry the claim, not the whole answer: the answer is cut into
pieces (a sentence, or a clause once a piece reaches 60 characters, or the first clause under
`first_clause_chars`), and from the first piece that may claim something was done or looked up
(`may_claim_done`, a superset of ADR 0240 "did it" and ADR 0250 "looked it up") that piece and all text after it
wait until the request ends; the guard then judges the answer and the held text is spoken if it stands and
dropped if the answer is sent back. Pieces before it go on as they complete, and a sentence they end is let
out of the assembler without waiting for the next piece. Nothing is held when the guard cannot fire this
request: a write has succeeded this turn, the one bounce is used, it is the last request, or a correction
run. An announced lookup ("let me check") is not held: it is heard before its call whether or not it is sent
back.

An answer about his get-off stop may say the stop or the warning is set when the state block carries the ride
line with a stop (`ride_stop_in_view`): a claim made only of set/remind phrases then counts as backed. The
answer must mention the stop (下车, 到站, "your stop", "get off"); 记下, 发出去 and the other kinds still need a
write, and 「查了」 still needs a call.

`bus(get_off=X)` for the stop already set writes nothing and says "Already set to this stop, nothing changed".

## Alternatives rejected

- **Hold every answer until the request ends.** Every first word then waits for the whole answer to be
  generated, in every turn, for a guard that fires on a few. The claim phrases are a closed list, so the held
  part is only the piece that matches and what follows it. (No timing was taken: no real model was called.)
- **Let it stream and tell the retry what was already said.** It keeps the doubled answer: the first half is
  heard, a false 「设好了」 included, and the model then has to retract it aloud, as the live replies
  quoted in Context do.
- **Hold only the claim sentence and let later sentences speak.** Speech is ordered; a later sentence heard
  before an earlier one it depends on ("so" and "also") cannot be put back in order.
- **Treat any `bus(...)` state in the block as backing every set-claim.** A reminder for tomorrow's meeting
  said in a turn on the bus would pass. The table has rows for this that must bounce.
- **Read earlier turns' tool results as backing.** The loop does not carry them as structured data, only as
  history text; the state block is what the program itself holds and re-states every turn.
- **Make the repeat block span turns.** Most identical calls across turns are meant (a poll, a list after a
  change); only a write whose effect is already in force is a no-op, and that is the tool's to know.

## Consequences

- First words of a reply that has no claim-shaped piece are unchanged: pieces go out where the assembler
  would have emitted them (a sentence end, or the first clause under `first_clause_chars`). A reply whose
  first piece matches (「我查了…」, 「记下了」) is silent until the stream ends, about the length of its
  generation; a claim that stands is spoken then, in one run.
- A piece cut at a clause end before a claim (a sentence over 60 characters) stays spoken when the answer is
  sent back, and the answer after the note follows it after a blank line.
- A genuine claim in a turn that also mentions his stop ("提醒你明天九点开会，已设好了" next to 「下车站设好了」) is
  read as the stop's and passes; the rule trusts the pair of conditions, not the sentence's meaning.
- The phrase list is still closed (ADR 0240): a claim worded another way passes, held or not.
- `ride_stop_seen` reaches the tool exam, which reads the ride line from the logged request's state block.
