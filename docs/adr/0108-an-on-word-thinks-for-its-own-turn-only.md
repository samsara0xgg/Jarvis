# ADR 0108 — An on-word thinks for its own turn only

**Status:** Accepted
**Date:** 2026-10-01
**Supersedes:** 0061, 0064

## Context

- ADR 0061 made an on-word (`llm.think.on_words`, such as 「想一想」) turn
  thinking on for the whole conversation: every later turn kept
  `llm.think.preset` until an off-word or a gap of more than 10 minutes. ADR
  0064 gave the companion the same rule to show, with the end of the ten
  minutes shown up to 30 seconds late.
- Allen, 2026-10-01: he said 「想一想」 once, and the turns after it stayed
  deep, the companion's deep colour with them. He had not expected that, and
  chose 「只管这一轮」: the sentence holding the on-word thinks, the next one
  without it gets the default preset again, and the companion's deep look
  ends with it.
- The default stays without thinking for latency. The 2026-09-26 replay of
  111 past utterances moved the median no-tool answer from 1.9 s to 3.1 s
  under low thinking, and every turn of a conversation-long mode paid that.
- Allen's own words still decide, not the model's judgment: asking the model
  first would add about 0.9 s to every sentence.
- ADR 0074: a sentence Allen pauses in arrives as two utterances, each its
  own turn. The first one's unspoken answer is cancelled as `superseded` and
  the second turn answers both. 「帮我想想，」 and 「明天的发布怎么排」 are one
  sentence to him, and only the first half holds the on-word.
- A turn can open more than one run: a commentary run completes while the
  turn goes on, and a failed run may be followed by a correction run.
- On the v1 wire the `open` op leaves only once the whole answer exists, and
  the daemon drops a silent turn by reading its `open` header, so nothing on
  the socket marks the start of thinking.

## Decision

An on-word in the sentence a turn answers gives that turn `llm.think.preset`;
every other turn gets the default preset. The sentence is the turn's own
words plus those of any earlier utterance ADR 0074 folded into it.

Its limits:

- Nothing else from earlier turns is read and nothing is stored.
  `llm.think.off_words` and the ten-minute gap mean nothing and are gone; an
  `off_words` an older settings.yaml still carries is ignored.
- `response.started` carries the preset's `reasoning_effort`, and the preset
  stays `luna-think`: the same model on /v1/responses at medium effort.
- The companion asks the daemon at start, every 30 seconds, and again as soon
  as Allen's words go in or an answer opens whether a turn is thinking, and
  which one (its id). A turn thinks from the moment its sentence's words are
  logged until every final run it opened has ended. One that never ends
  counts for ten minutes, or until newer words of his arrive.
- The deep look belongs to that turn: a deeper colour and a stiller face,
  with no stars or meteors, kept through the speaking of its answer and the
  moment after, and ended with the turn. The companion times a deep answer
  itself, from his words going in to that answer's `open`.

## Alternatives rejected

- **Keep the conversation-long mode and show it better.** ADR 0064 already
  showed it, with the deep colour, and Allen still did not expect the next
  turns to stay deep.
- **Shorten the gap to a minute or less.** The turn Allen named is the very
  next one, so any gap long enough to reach it keeps the surprise.
- **One-turn by default, plus a phrase that keeps thinking on.** Allen asked
  for one turn only. `off_words` and the gap would stay for a switch nobody
  has asked for.
- **The look holds until Allen's next words.** After the answer it would stay
  deep for as long as he is quiet, which is the deep colour that stayed.
- **The first response event ends the turn.** A commentary run completes
  mid-turn and a failed run can be followed by its correction, so the look
  would drop while the model still thinks.
- **A start op from `response.started`.** A turn that is dropped as silent
  (`queue_review`, `silent_log`) would show a thinking face for an answer
  that never comes.

## Consequences

- A follow-up that needs the same depth ("那第二个呢") answers at the default
  depth unless it says an on-word again.
- 「我想一想」 said to himself still makes that turn think, one turn instead
  of the conversation.
- A crash writes no end to the response, so the look stays up for up to ten
  minutes or until his next words.
- Between a failed run and the start of its correction, and between the
  first half of a split sentence being cancelled and the second half being
  logged, a read finds no thinking turn for an instant.
- A reloaded companion shows no seconds on answers it did not see open, and
  the seconds count the whole wait, tool calls included, not only reasoning.
- The companion compiles the daemon's Python patterns as JavaScript. One it
  cannot read leaves only the typing preview blind; the daemon still decides.
- Only a turn that opens a ResponseRun (`realtime.response.response_run_lifecycle`)
  can think; without one, every turn uses the default preset. A thinking
  turn's spoken-form rewrite thinks too, because it uses the same request
  client.
