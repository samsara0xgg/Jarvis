# ADR 0188 — The weather at home is read by one model tool, without a search

**Status:** Accepted
**Date:** 2026-10-06
**Supersedes:** none

## Context

- A weather question went to `web_search`: the wait line, a search of about 4 s, then the
  answer, about 7 s after Allen stopped speaking.
- The home view already reads Open-Meteo for the `home.weather` latitude and longitude with a
  4 s timeout (ADR 0051), so that service is an allowed egress for the same place.
- Native tools in the registry are on the model's menu from the first request; only plugin
  tools wait behind `tool_search` (ADR 0127, ADR 0169).

## Decision

When `home.weather` is set, the registry carries one read-only model tool, `weather`, with
no arguments: it makes one Open-Meteo request for that place and returns now, today and
tomorrow (high, low, chance of rain, conditions) and the next 12 hours, in plain words and
the place's local time. The place is `home.weather.name` or "home". Its description sends
other cities and dates beyond tomorrow to `web_search`, and a failure is a tool error that
says to do the same. It is L1 with no confirmation, and its dispatch says no wait line
(ADR 0136).

## Alternatives rejected

- **Prefetch the weather for a predicted `web_search` group (ADR 0140).** Jev's web group has
  34% recall (ADR 0140), and a prefetch is a request for turns that may not
  need it; a tool the model calls is one request exactly when asked.
- **A second Open-Meteo client in the execution layer.** The home's request and its parsing
  live in the runtime layer; the tool takes that read as a function, so there is one client.
- **A `day` argument.** The result already holds both days; the model picks.

## Consequences

- Jev still groups a weather line under `web_search`, so a predicted-group wait line
  ("looking it up") can play before this tool answers; removing weather from that group's
  description is a Jev change that needs its own eval.
- The home view's request now also asks for hourly rain chance, daily low and daily code; its
  parsing is unchanged.
