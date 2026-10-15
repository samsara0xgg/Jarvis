# ADR 0272 — A spoken answer waits for the end of his thought, and the model request does not

**Status:** Accepted
**Date:** 2026-10-14
**Supersedes:** none

## Context

- A pause is not always the end of what he means. The Mac ends an utterance after 0.8 s of
  quiet (ADR 0006), the phone after 0.8 s of unchanged recognizer text (ADR 0271); a thought
  with a breath in the middle becomes two turns. Live 2026-10-14 21:36 a bus ride's thought
  was answered eight times. ADR 0053, 0074, 0268 and 0271 drop or merge the earlier turn
  when the next words come, but only while no word of its answer has gone out: an answer
  that has begun is barge-in's (ADR 0100). The first sentence of an answer leaves at
  p10 / p50 / p90 of 0.95 / 1.55 / 3.29 s after his words (chunks, since 2026-10-07; the
  start of playback is 0.97 / 1.70 / 3.98 s), so a continuation 1 to 2 s later often finds it
  already speaking.
- Offline eval, the 2026-10-02 replay: 531 final pauses of the Mac wake channel, labelled by
  one reader. 28 were the first half of a thought cut in two (C), 17 had a next line that
  added to or corrected them (S), 74 were followed by an unrelated line (N). A rule that
  holds a line, and joins the next one to it when that comes inside the hold, is scored by how
  many of C and S it joins and how many N it wrongly merges.
- A hold on the whole turn costs its full length on every turn. A hold on the answer only
  costs what the model has not already used: with the request sent at once, the added wait is
  the hold less the time the model takes to start answering.
- A line that is short and ends like a finished sentence is rarely half of something:
  「几点了」, 「关灯」.

## Decision

A turn he spoke sends its model request at once and holds what leaves her until 1.5 s after
his speech ended, unless the line is at most 10 characters and reads as finished.

- **Rule.** A spoken line of at most 10 characters for which `voice_asr.looks_complete` says
  complete gets no hold. Every other spoken line gets 1.5 s. Text, typed words and every
  non-voice surface get none.
- **From when.** From the end of his speech. The Mac's capture knows when its last speech
  frame was and tells the brain how long before the utterance that is; the phone's `say` has
  already waited its own 0.8 s, so the hold counts from its arrival.
- **What waits.** The answer's sentences, and with them the audio and the speaking state that
  follow them on both voice paths; and every tool that is not `read_only` (writes, sends,
  reminders, rides), at the place an action is dispatched. The same wait guards an answer
  that is not streamed, in front of the completion hold of ADR 0053. What does not wait: the
  model request, prefix warm, tools that only read, so the answer is ready when the hold ends.
  The commentary line at a tool's dispatch (ADR 0121) already starts no earlier than 1.5 s
  after his words and needs no change.
- **Not started.** For the whole hold the turn has said nothing, so ADR 0268's sweep on the
  Mac and ADR 0271's `continues` on the phone find an answer with no word out and cancel it,
  and nothing of it was ever heard. Cancelling during the wait is the cancel of any open run.
- **Setting.** One block, `realtime.response.answer_hold`, with `seconds` (1.5) and
  `short_chars` (10); both default to these values when the block is absent, and 0 seconds
  turns the hold off.

## Alternatives rejected

- **A fixed 1.5 s on every final.** Joins C 24/28 and S 16/17 but merges N 42/74, and adds a
  mean of 1325 ms to every turn (replay, same cut). The structural rule joins C 15-16/28 and
  S 12-15/17 and merges N 8-10/74 at 1.0 to 2.0 s (1.0 s: 15, 12, 8; 2.0 s: 16, 15, 11), on
  49.5% of finals; its mean added wait with the request sent at once is 11 ms at 1.0 s and
  169 ms at 2.0 s (first sentence), 8 and 74 ms (playback).
- **TurnSense (ts10, ts11), text models, as the hold decider.** Hold when p(complete) is at
  most 0.5: waits 44.8% and 39.9% of finals, joins C 24/28 but S only 2/17, and merges N 35
  and 38 of 74; at 0.05 it joins C 19 and S 0 for N 19. On 84 complete lines it called 32 and
  29 incomplete at 0.5, against 3 for Smart Turn. It keeps half of the unrelated lines open.
- **Smart Turn, an audio end-of-turn model, as the hold decider.** At p at most 0.05 it flags
  13 of the 28 C and none of the 17 S, on 5.5% of finals, with N 3. Beside the structural rule
  (17 of 28 C flagged) it adds two C that the rule misses (11 flagged by both, 6 by the rule
  only, 9 by neither). Two splits in 531 do not pay for a model on the capture path.
- **Jev as the hold decider.** From the 2026-10-02 endpoint experiment (523 final pauses,
  20 C and 17 S, a hold of up to 2.0 s): at p(complete) at most 0.10 it flags 31 lines and
  joins 5 C and no S for 6 unrelated merged; at 0.20, 71 lines, 12 C and 2 S for 12
  unrelated merged. The rule joins 16 C and 15 S at 2.0 s for 11 N. Jev was not rescored on
  the 531-final cut used above.
- **A longer quiet before the capture ends the utterance.** It delays the model request by
  the whole amount, so the hold costs its full length on every turn instead of 11 to 169 ms,
  and it moves barge-in and Tier 0 with it.
- **Holding the request too.** The same: the model's own start-up time is what hides the hold.

## Consequences

- A turn with no model (Tier 0, ADR 0122's Jev answers) has no start-up time to hide the
  hold: a spoken line over 10 characters, or one that ends on a connective, now waits the
  full 1.5 s from the end of his speech before its action or answer.
- 8 to 10 of 74 unrelated next lines inside 1.5 s are merged into one answer. ADR 0268 already
  merges them when the first answer is still unspoken; the hold makes that more often.
- The numbers are the Mac wake channel's, 2026-09-12 to 10-02, one labeller. The phone's own
  rate of split thoughts is not measured; its 0.8 s wait is counted before the hold.
- The wait runs on the thread that consumes the model's stream, so a held answer's tokens
  buffer in the provider connection until the hold ends.
- The hold is a number, not a decision: a rule that reads the line better than its length
  and its last word can replace the rule without touching what waits.
