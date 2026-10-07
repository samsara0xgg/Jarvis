# ADR 0189 — The bus times are read by one model tool, from Google Routes

**Status:** Accepted
**Date:** 2026-10-06
**Supersedes:** none

## Context

- Allen rides BC Transit in Saanich and Victoria and asks when to leave for home or for the
  UVic campus. A trip needs stop-to-stop routing, transfers and walking, not one timetable row.
- Plugin and MCP tools sit behind `tool_search` (ADR 0127, ADR 0169), which costs a hop on
  every question; native tools are on the menu from the first request (ADR 0188).
- The reference Google Maps MCP server calls the legacy Directions API, which a new Google
  Cloud project cannot enable. The Routes API (`computeRoutes`, `TRANSIT`) is the current one.
- The Routes response gives scheduled and estimated stop times and no field that marks a time
  as realtime, so Jarvis cannot say whether a departure is live.
- Routes Essentials has a monthly free request cap; a few voice questions a day stay inside it.

## Decision

When `GOOGLE_MAPS_API_KEY` is in the daemon's environment, the registry carries one read-only
model tool, `transit`, that makes one `computeRoutes` request (travel mode `TRANSIT`, a field
mask of only the fields it reads, a 5 s timeout) and returns up to 3 options in plain words and
Vancouver time. The words `home` and `school` map to `transit.home` and `transit.school` in
the settings (an address or `lat,lng`); an unset `home` falls back to the `home.weather`
latitude and longitude, and an unset `school` is a tool error. It is L1 with no confirmation,
and its dispatch says no wait line (ADR 0136). It acts only when the model calls it; there is
no proactive "leave now" heads-up.

## Alternatives rejected

- **The Google Maps MCP server.** Behind `tool_search`, so a second hop before the first
  request, and it targets the legacy Directions API that a new project cannot turn on.
- **BC Transit GTFS and GTFS-realtime feeds.** They hold stops, trips and arrivals, so Jarvis
  would have to do the stop search, walking legs and transfers itself; `computeRoutes` returns
  that in one request.
- **A proactive heads-up before the usual bus.** Needs a schedule of Allen's days and a card
  policy; deferred until the voice tool has been used.

## Consequences

- Each question sends the origin and destination text to Google, so the places Allen names
  leave the Mac; the saved places are his own values in `settings.yaml`, never in the repo.
- Realtime delays are not visible: the tool states the times Google returns and the model must
  not call them live.
- Past the free monthly cap the requests are billed to the key's Google Cloud project.
