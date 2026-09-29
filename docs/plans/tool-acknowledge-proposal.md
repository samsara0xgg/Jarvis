# ADR proposal — A Tool Turn Says One Acknowledge When It Works for Allen

**Status:** Proposed
**Date:** 2026-09-29
**Supersedes:** 0082 (on acceptance; it then gets the next ADR number)

## Context

- ADR 0045, kept by ADR 0082, ships `realtime.commentary.enabled: false`: a
  tool turn says nothing until its answer. It turned off ADR-0008 D6's
  lifecycle commentary because the phrase heard was often the result row's
  "结果回来了，我整理一下。": a fast tool's acknowledge was cancelled unheard by
  its own result row. On 2026-09-25 that phrase followed `get_current_time`
  mid-chat, a lookup Allen never asked for, and `tool_search` on two plugin
  questions.
- Allen, 2026-09-25: a quick turn needs no filler; a long job might say
  "稍等我一下", or only show that he is there. ADR 0045's own consequence:
  away from the screen there is no sign that Jarvis heard.
- ADR-0008 D6 forbids speech no lifecycle row makes true: no "马上好" without
  evidence, no timer-driven progress.
- The commentary code stayed (ADR 0045). It speaks at most one phrase per
  turn, none for a GPT-Live turn, none once the turn has ended, none over a
  pending confirmation; its acknowledge names what the tool does (a
  read-only tool looks something up, `spawn_worker` hands the work to Codex)
  in the language of Allen's words.
- The event log, 2026-09-26 to 2026-09-29, voice turns from
  `utterance.received` to their first `surface.playback_started`: 117 turns
  with no tool took p50 2.9 s, p90 5.3 s, and 55 of them were still silent
  at 3 s, 24 at 4 s.
- Of the 16 tool turns that did not wait on Allen (no confirmation card, no
  `ask_user`), 9 called a tool that works on his request (web search and
  fetch, mail, screen, lights, activity). One, a light switched in 0.4 s,
  was quick; the other 8 were silent for 6.2 to 24.4 s, and their first such
  call came 2.0 to 5.8 s after his words. The other 7 called only
  `tool_search`, the clock or memory, and took 3.8 to 17.2 s.
- The tools are fast and the model is slow: `tool_search`, mail reads and
  lights return in 0.0 to 0.5 s, web search in 1.5 to 2.7 s, web fetch in
  0.5 to 7.0 s, a screen look in 3.5 s; in those 16 turns, from the last
  tool result to the first audio took p50 3.9 s, p90 14.8 s.

## Decision

Speak commentary again for one row only: in a turn whose answer is spoken,
the first dispatch of a tool other than `tool_search`, `get_current_time`,
`remember` and the card tools says one short acknowledge, no earlier than
1.5 s after Allen's words ended and only while no answer audio has started.
No other lifecycle row speaks.

Limits: the wording, the one-per-turn cap and the GPT-Live, ended-turn and
confirmation exclusions are the commentary code's own; an answer that is
ready while the acknowledge plays waits for it to end. ADR 0082 otherwise
stands: the spoken form is short by default and an explicit request for
counting, reading aloud, verbatim repetition, detail or a length overrides
its limits; ADR 0045's rewrite trigger, language, exclusions, full-document
retention and whole-answer fallback are unchanged.

## Alternatives rejected

- **Stay silent until the answer (ADR 0045).** Eight of the nine measured
  turns that called such a tool were silent for 6.2 to 24.4 s.
- **Speak after a fixed silence, tool or not.** 55 of 117 no-tool turns were
  still silent at 3 s and 24 at 4 s: a timer puts filler before ordinary
  chat answers, which Allen said need none, and it speaks with no lifecycle
  row behind it (D6).
- **Say nothing when the tool returns within a second.** Mail reads and
  lights return in under 0.5 s, yet from the last result to audio took p50
  3.9 s, p90 14.8 s: the rule silences the turns that wait longest.
- **Speak every lifecycle row (D6 as shipped).** "结果回来了" followed a
  clock lookup Allen did not ask for and two tool searches (2026-09-25).
- **Let the model write the lead-in.** Measured for ADR 0043 (2026-09-25):
  2 to 3 of 8 tool turns answered with the sentence alone and never called
  the tool.

## Consequences

- The acknowledge comes when the model calls the tool, 2.0 to 5.8 s after
  Allen's words in the measured turns, not at once: a turn that thinks long
  before its first call is silent that long.
- A slow turn that uses only the silent tools stays silent (seven took 3.8
  to 17.2 s).
- An answer ready while the acknowledge plays waits up to its length, about
  1 s; in the eight slow turns the answer came 4.2 s or more after the first
  call.
- The silent tools are a fixed list: a new bookkeeping tool speaks until it
  is added.
- 1.5 s is chosen, not measured, and 16 turns is a small sample; a live run
  on the Mac is owed before acceptance.
