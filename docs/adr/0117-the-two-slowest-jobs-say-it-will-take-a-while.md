# ADR 0117 — The two slowest jobs say it will take a while

**Status:** Accepted
**Date:** 2026-10-02
**Supersedes:** none

## Context

- Allen, 2026-10-02: for the slowest jobs the fixed wait line of ADR 0116
  should tell him it will take a while, so he knows he can talk about
  something else meanwhile. He chose the wording below.
- Measured on his log (2026-09-26 to 09-29): `daily_work_report` 58 s median,
  `refresh_work_state` 7.6 s median and 13.5 s p90; the other slow tools are
  under 3.5 s, where 「稍等」 is true and "ask me something else" is not worth
  saying.
- ADR 0107 (`realtime.response.slow_results`) already lets him ask something
  else while a slow turn runs and delivers the slow answer after her current
  speech, so the invitation is true.
- ADR 0116's cap is one line per turn, and its clock row speaks without
  knowing which tool will follow.

## Decision

A slow-tool dispatch of `daily_work_report` or `refresh_work_state`
(`LONG_WAIT_TOOLS`, next to `SLOW_TOOLS` in `jarvis/shared/lang.py`) says one
line from `commentary.long_wait` instead of `commentary.wait`: 「这个要等一会儿，你可以先问别的。」
「这个得花点时间，有别的事可以先问我。」「要等一会儿，你先忙别的也行。」 and "This will take a
little while. You can ask me something else meanwhile." "This one takes a bit.
Feel free to ask me something else.", picked at random in the language of his
words. Every other rule of ADR 0116 is unchanged: the 1.5 s floor, one line per
turn, a model `lead_in` replaces the line, and the 2.5 s clock row still says
the normal pool.

If the clock row already spoke before the dispatch, the cap suppresses the long
line; that turn hears one 「稍等」-type line only.

## Alternatives rejected

- **Make the clock row say the long line too.** At 2.5 s the runtime does not
  know the turn is slow: web_search (1.9 s) and a model that is merely late both
  reach it, and inviting him to ask something else for a 3 s wait is noise.
- **Let the long line follow an earlier clock line (two lines per turn).**
  Breaks ADR 0116's one-line cap, which exists so a turn never talks over
  itself, and the owner asked for the cap to stay.
- **Put the long set on every tool over 2 s.** `web_search` (1.9 s) and
  `screen_look` (3.3 s) return before an invitation to talk of something else
  can be used.

## Consequences

- A slow call dispatched after 2.5 s of silence gets the short line, not the
  long one, so the invitation is missing in exactly the turns where the model
  was slowest to ask for the tool.
- The set is a hand-kept list of two tools; a tool that becomes slow must be
  added to `LONG_WAIT_TOOLS` by hand.
