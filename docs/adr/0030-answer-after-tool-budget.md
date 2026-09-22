# ADR 0030 — The tool budget and the answer are separate requests

**Status:** Accepted
**Date:** 2026-09-21
**Supersedes:** none

## Context

`decide()` bounds the Tier 2 loop at `max_tool_iterations` model requests
(default 5) so a model that never converges cannot run up cost. Every
request in that loop may propose tools. When the fifth request still
proposes tools, the runtime dispatches them, the loop ends, and the surface
speaks a fixed English string: `tool-use loop exhausted; turn incomplete.`
Turn T1796b684 on 2026-09-21 ended exactly there: the fifth tool result
carried the evidence the question needed, and no request was left to read
it. The string is internal vocabulary spoken in English to a Chinese
conversation. Cost control, cancellation, admission and the event record of
each request must stay as they are.

## Decision

After the tool budget is spent, make one more request with no tools,
carrying a runtime note (marked as not the user's words) that asks for the
answer from the tool results already held: facts with their evidence first,
then what could not be confirmed, nothing invented. This request runs under
the same cost recording, admission and cancellation checks as the loop's
calls and does not count against the tool budget. If it raises or returns no
text, the turn ends with a fixed Chinese limitation that names the budget,
never with an internal English error. Cancellation is never swallowed.

## Alternatives rejected

- **Raise `max_tool_iterations`.** The failing turn would have needed a
  sixth request either way; a larger bound spends more on transport before
  failing the same way, and the bound exists to cap cost.
- **Force text on the last loop iteration (`tool_choice="none"`).** The
  model then has to answer before seeing the results of the calls it wanted
  to make in that iteration; the question that failed needed the fifth
  call's result.
- **Translate the fallback string only.** Fixes the language, not the loss
  of an answer the tool results already contained.

## Consequences

One extra request per exhausted turn: for the failing turn's history size
about 30k input tokens, mostly cached, under $0.02 at the fast preset. A
turn that exhausts its budget now costs `max_tool_iterations + 1` requests
before it can fall back. The runtime note is a user-role message appended
after the last tool results; providers that reject a user message directly
after tool messages would fail the request, which the fallback text covers.
