# ADR proposal — A Spoken Turn Speaks the Model's Own Words As It Writes Them

**Status:** Proposed
**Date:** 2026-09-29
**Supersedes:** 0099 (on acceptance; it then gets the next ADR number)

## Context

- Allen's goal for a turn he speaks (2026-09-29): from the end of his words
  to her first word about 1.5 s at the median (about 4 s now), and that
  first sentence is the answer itself; a tool turn as quick, with one line
  about what she is doing, then the result; nothing said to be done before
  it is; what she says and what the screen shows come from one answer, with
  no second model call before she speaks.
- 46 spoken chat turns since the switch to gpt-6-luna (2026-09-26 to 09-29),
  from `utterance.received`: first audio p50 3.14 s, p90 6.62 s. The answer
  is written whole before anything is spoken (p50 2.52 s). 25 of them then
  went to ADR 0099's spoken-form rewrite, a second request of p50 1.33 s:
  those turns took p50 5.06 s, the 21 spoken as written p50 2.35 s.
- On the real prompt (41.6k tokens, 24 tools, 500 history messages)
  gpt-6-luna's first token comes 1.47 s after the request with a cold cache
  and 1.26 s with a warm one, and its first clause 0.05 s after that
  (2026-09-29, 50 calls).
- ADR-0008's routine stream already speaks durably permitted sentences as
  they are written, but only in a turn pre-routed as plain chat with no tool
  on offer, and its classifier (`routine-zh-en-v1`) permits only explanatory
  sentences that do not open with 我/你/I/you; the first refusal seals the
  turn. Run over the first sentence of the 48 chat answers on record since
  2026-09-26, it permits 1; 34 fall outside its evaluated forms ("我在，能
  听见。", "明白了。"). The Pre-emit Gate passes every draft unchanged (ADR
  0019), so a refused sentence is still spoken, only later.
- ADR 0040 stopped asking the main model for a `<voice>` span because the
  pre-v1 span ran long (47 s for one answer); that prompt set no length.
  The length default and the explicit-request override live in ADR 0099's
  rewrite prompt.
- The tool loop sends each request whole, and text the model writes next to
  a tool call goes only into history. Of the 16 tool turns between
  2026-09-26 and 09-29 that did not wait on Allen, 8 called a tool working on
  his request and were silent for 6.2 to 24.4 s; tools mostly return within
  0.5 s, and from the last tool result to the first audio took p50 3.9 s.
- The Responses API labels each message `commentary` or `final_answer`, and
  a stream names the label before the message's first text (probe,
  2026-09-29). Chat completions has no label, and a response's text arrives
  before its tool calls. Asked to say one line before a tool call, on 8
  tool-needing utterances with the real prompt gpt-6-luna said the line and
  called the tool 4 times, called silently 3 times, answered from knowledge
  once, never ended on the line, and labelled every message right.
  gpt-5.6-luna ended 2 of 8 on the line, labelled `final_answer`; ADR 0043's
  2 to 3 of 8 were gpt-5.6-luna too.
- ADR-0008 D6: nothing is spoken that no lifecycle row makes true. The
  fixed acknowledge built behind `realtime.commentary.enabled` (a5a867d)
  speaks once per turn, at the first dispatch of a tool that works for
  Allen, no earlier than 1.5 s after his words.
- First live run of this decision (2026-09-30, 8 spoken turns): a chat
  answer's audio began 3.0 s after the end-of-speech cut, 1.8 s of it the
  first token. 7 of 8 turns called a tool, each extra request adding about
  3 s; replayed offline, the same utterances called the same tools as often
  through chat completions without the voice note (2026-09-30, 24 calls). A
  line written with a `tool_search` call waited 5 s for the first working
  dispatch. OpenAI's citation markup reached speech.
- Voice agent frameworks (OpenAI Realtime tool preambles, LiveKit, Pipecat,
  Vapi) stream the model's sentences into speech and let the model say the
  line before a tool call. None rewrites the answer with a second model call
  before speaking it.

## Decision

In a turn Allen speaks, stream every model request and speak the model's own
words as it writes them: the spoken part of its answer sentence by sentence,
and its line before a tool call when that call is dispatched. No second model
call rewrites either.

Limits:

- The prompt asks for the spoken reply first: the answer itself, by default
  at most about 60 Chinese characters or 40 English words with no markup, as
  long as needed when Allen asks outright for counting, reading aloud,
  verbatim repetition, detail or a length. What belongs only on screen
  follows in the `<voice>`/`<document>` envelope.
- A sentence is spoken as soon as today's sentence assembler forms it; markup
  or an overlong sentence ends incremental speech, and the rest is spoken
  as written when the answer is complete.
- The line before a call is spoken when the call it came with is
  dispatched, even a call that only finds a tool or reads the clock, in
  place of the fixed acknowledge and under its rules: once per turn, no
  earlier than 1.5 s after his words, not once the turn has ended, not over
  a pending confirmation, never for GPT-Live. A call that is refused or
  waits for confirmation dispatches nothing, so its line waits for the next
  dispatch. A line over 60 characters, or one that states a result, gives
  way to the fixed acknowledge, which otherwise speaks at the first dispatch
  of a tool that works for Allen.
- A response that ends on a `commentary` message without a tool call gets
  one more request.
- Typed turns, GPT-Live, Tier 0 read-backs, confirmation asks, the repeat of
  the last answer and other fixed Layer 3 text keep today's path; ADR 0099's
  rewrite remains only there.

## Alternatives rejected

- **Keep the rewrite and stream its output.** It can start only once the
  whole answer is written, p50 2.52 s after `utterance.received`, and then
  waits for its own first token.
- **Rewrite with a smaller, faster model.** The same floor: no audio before
  the whole answer, p50 2.52 s, against 1.3 to 1.5 s for the main model's
  first clause.
- **Stream under `routine-zh-en-v1`.** It lets the first sentence of 1 in 48
  chat answers through and never streams a turn with tools on offer.
- **A fast front model that talks while the backend works (the GPT-Live
  shape).** On the same prompt gpt-5.6-luna's warm first token was no faster
  (1.26 s), and cutting history to 12 messages saved only 0.2 to 0.4 s. A
  second model that can answer without the backend is what ADR 0040
  rejected.
- **Speak the line as soon as it is written.** gpt-5.6-luna ended 2 of 8
  tool turns on it; spoken at dispatch it is true by construction, at the
  cost of the time the model takes to write the call.
- **Chat completions without the label.** A response's text arrives before
  its tool calls, so a stream cannot tell a line before a call from an
  answer until the response ends.
- **A required `lead_in` argument on every tool.** ADR 0043: in 2 to 3 of 8
  turns the sentence replaced the call.
- **Cut the written answer at a sentence end.** ADR 0040: a list answer's
  first sentence is its header.

## Consequences

- A sentence once spoken cannot be revised by a later one, and ADR 0074 can
  no longer drop an answer unheard once its first sentence has played.
- What she says is the model's own wording, not a summary of it: how long
  it runs depends on the model following the prompt, measured only by the
  live run.
- A completion claim in an answer written without a tool call is spoken as
  soon as it is written; ADR 0019 already removed the check that would have
  judged it.
- The line is heard when its call is dispatched, after the model has also
  written the call's arguments; a turn that thinks long before its first
  call stays silent that long.
- Every spoken turn becomes a streamed Responses API request; the model's
  own labels decide what is a line before a call.
- This removes the rewrite and the wait for the whole answer, not the first
  token (1.3 to 1.5 s), the end-of-speech detection (0.83 s) or the start of
  speech synthesis; the 1.5 s median needs work on those too.
- A live run on the Mac is owed before acceptance.
