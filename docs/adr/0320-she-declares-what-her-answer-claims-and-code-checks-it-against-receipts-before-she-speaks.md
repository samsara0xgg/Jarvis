# ADR 0320 — She declares what her answer claims, and code checks it against receipts before she speaks

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** 0250; 0263; the clause of ADR 0240 that sends back an answer no call backs. ADR 0240's refusal of a repeated call stands.

## Context

- The guard (ADR 0240, 0250, 0263) reads her prose with closed lists of Chinese and English phrases
  and checks one boolean: did any write succeed this turn (`turn_guards.py:215`).
- The planning audit of 2026-10-20 ran incident phrases through it:
  - It misses 「已经存进备忘了」, 「设好啦」, 「灯关掉了」 (devices are not covered at all) and
    "I set it for 10:30". It also missed 「好，10:50 提醒你出门」, a false claim that passed live on
    2026-10-18 09:33.
  - It fires on 「到时候会提醒你的」 after reads, though the reminder existed: it reads no reminder
    state.
  - Each incident added an exception: the calendar line, the ride stop, the screen.
- Any write backs any claim. If a schedule add succeeds and a light call is refused,
  「提醒设好了，灯也关了」 passes.
- Its note says "Do it now: call the tool … without asking him first" (`UNBACKED_NOTE`). When the
  ride stop was already set, that note made her call `bus(get_off=…)` again to back a true sentence.
- Speech:
  - The claim hold lets the first clause out early.
  - Text the model writes before a tool call is spoken before the call runs. It is kept as
    commentary and joined to the final answer with a paragraph break.
  - On 2026-10-18 09:02, half a sentence about an 11:30 pickup (「记得：11:30 … 接你（X），」) was
    spoken before its call and then glued to the answer after it. A claim made before a call is heard
    before its result.
  - An answer whose first piece looks like a claim is silent until its whole generation ends
    (ADR 0263).
- The prompt contradicts itself on text before calls. Line 91 asks for no words before most calls,
  while the spoken-reply note says to start with the answer and put a line before every tool.
- Asking wins over acting:
  - Prompt lines 79-80 say every call keeps him waiting another round.
  - The guard spares questions but bounces 「我去查一下」.
  - Together they make 「要我帮你查吗？」 the safe reply. He answered 「你自己不会看吗」 and
    「为什么什么都要问我」.
- Receipts (ADR 0319) give each call's status and object, with ids she sees in this turn's results
  and in the history.
- Markup she writes can be cut before speech: `<react>` (ADR 0257), and the fields of the structured
  reply (ADR 0114).
- ADR 0317 allows no paid judge. A model judges what a sentence claims better than a phrase list;
  code can check whether a receipt exists.

## Decision

Have her answer open with a block that declares each thing it says was done, set up for later or
looked up, and each lookup it offers, with the receipt each rests on. Check the block in code before
any of the answer is spoken. Retire the phrase guard, the claim hold and their exceptions.

Its limits:

- **The block.**
  - It comes first in the answer and is cut from speech, the screen and the records, as `<react>`
    is. In the structured reply it is a field ahead of `spoken`.
  - Each entry is one of two kinds:
    - what the answer says, and the receipt it rests on: this turn's, or an earlier one shown in the
      history;
    - an offer, naming the tool that would do it.
- **The check.**
  - Each cited receipt exists in the projection and is done.
  - It is of the right kind: a change for something done, set, sent, shown or cancelled, and a read
    for a lookup. A promise ("I'll remind you at 10:50") rests on the change that set it up.
  - An answer with no block is checked as an empty one.
- **A failed check** sends the answer back once. The note names the failing entry and the receipts
  on record: "Rewrite the answer to match the receipts. Call a tool only if his words this turn ask
  for it."
  - The answer after it is checked again.
  - If it fails again, it is spoken as written and the failure is recorded with the entry.
- **Offers.** If an offered lookup can be done by a read-only tool on the menu, the answer goes back
  once with "do it instead of offering". An offer to change something stays an offer.
- **Nothing is spoken before the check.** Text the model writes before a tool call is neither spoken
  nor joined to the answer. A slow tool's lead-in is the program's line from the step table, said
  only when that tool's measured median time is slow (spec `#tools`).
- **Undeclared claims** are sentences that claim something with no entry. A Claude Code subagent
  counts them each night over the day's answers, and the count goes on the per-tool scorecard. The
  miss rate is measured, not assumed.
- **What goes:**
  - the phrase lists, the "did it" and "looked it up" rules, `may_claim_done` and the ride-stop
    exception;
  - the early first clause;
  - the prompt lines that ask her to police her own claims (the end of 90, 92, 127-128);
  - the lines that frame a call as a cost to avoid (79-80, 86);
  - line 91 and the spoken-reply note's line before every tool, since lead-ins are now the
    program's.

  The exact prompt text goes to Allen before it merges.

## Alternatives rejected

- **Keep the guard and add phrases.** Every incident added an exception, and the 2026-10-20 run
  still missed five phrasings and fired on one true sentence. A closed list cannot follow how she
  says things.
- **Judge every answer with a second model.** That adds a paid request, about a second, to every
  turn, and ADR 0317 rules out paid judges. A declared block costs a few tokens.
- **Check after speaking and correct aloud.** Speech has no recall. The doubled answers quoted in
  ADR 0263 are what that sounds like.
- **Keep speaking the text before a call.** That is the 09:02 half sentence, and it is how a claim is
  heard before its result.
- **Hold every answer until its stream ends.** First words would wait for the whole generation. The
  block is the first few tokens.

## Consequences

- First sound waits for the block. An empty block is a few tokens; each entry is about 30. Each turn
  records it under `turn.timing` (ADR 0276). An answer that opens with a claim no longer waits for its
  whole generation.
- A model that leaves a claim out of the block is not caught live. Only the nightly count sees it.
- How well each preset writes the block is a whole-turn exam question (ADR 0317). A weak preset can
  fail where the old guard's phrases would have caught it.
- A bounce still costs one request.
